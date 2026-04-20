import json
import logging
import os
import re
import sys
import threading
import time
import io
import zipfile
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import lark_oapi as lark
import requests
from lcsc_step_downloader.core import fetch_component_library_archive, fetch_step_file
from lcsc_step_downloader.part_data import (
    BomParseResult,
    build_bom_report_csv,
    choose_unit_price,
    fetch_part_snapshot,
    format_compare,
    format_part_info,
    parse_bom_bytes,
    parse_bom_text,
)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


log = logging.getLogger("feishu-lcsc-bot")


LCSC_ID_RE = re.compile(r"(?<![A-Za-z0-9])[Cc]\d{3,}(?![A-Za-z0-9])")
PART_PREFIXED_SPACED_RE = re.compile(r"(?<![A-Za-z0-9])[Cc]\s*(\d{3,})(?![A-Za-z0-9])")
PART_EXPLICIT_NUMERIC_RE = re.compile(
    r"\b(?:lcsc|jlcpcb)(?:[_\-\s]*(?:id|part(?:[_\-\s]*(?:number|no))?|pn))?\s*[:=#]?\s*([Cc]?\d{3,})\b",
    flags=re.IGNORECASE,
)


HELP_TEXT = (
    "LCSC/JLCPCB Bot capabilities:\n"
    "- Generate full KiCad library ZIP (symbol + footprint + 3D model)\n"
    "- Generate STEP-only file on request\n"
    "- Show live part info (stock, price tiers, package, lifecycle)\n"
    "- Compare multiple parts side-by-side\n"
    "- Parse BOM from text, CSV, or XLSX file uploads\n"
    "- Accept LCSC/JLCPCB part IDs and LCSC/JLCPCB product links\n"
    "- Notify if no library/model is available for the requested part\n"
    "- KiCad output can be imported into Altium (direct Altium generation is not supported)\n"
    "- Works in 1:1 chats\n"
    "\n"
    "Commands:\n"
    "- /help : show this message\n"
    "- /ping : health check (returns pong)\n"
    "- /info <PART_ID> : show part info\n"
    "- /compare <PART_ID> <PART_ID> ... : compare parts\n"
    "- /bom <rows> : parse pasted BOM rows (or upload CSV/XLSX file)\n"
    "- /step <PART_ID> : STEP-only output\n"
    "- /library <PART_ID> : force library ZIP output\n"
    "\n"
    "Examples:\n"
    "- C2040\n"
    "- /info C2040\n"
    "- /compare C2040 C2871814 C8596\n"
    "- /bom C2040,10\\nC8596,5\n"
    "- /bom C2040,10 C8596,5 C7423108 x2\n"
    "- https://www.lcsc.com/product-detail/..._C2040.html\n"
    "- jlcpcb part number 2040\n"
    "- https://jlcpcb.com/partdetail/.../C2040\n"
    "- /step C2040\n"
    "- /library C2040"
)

INFO_CMD_RE = re.compile(r"^/info(\s|$)", flags=re.IGNORECASE)
COMPARE_CMD_RE = re.compile(r"^/compare(\s|$)", flags=re.IGNORECASE)
BOM_CMD_RE = re.compile(r"^/bom(\s|$)", flags=re.IGNORECASE)
STEP_CMD_RE = re.compile(r"^/step(\s|$)", flags=re.IGNORECASE)
LIBRARY_CMD_RE = re.compile(r"^/library(\s|$)", flags=re.IGNORECASE)


def _is_scope_denied_error(exc: Exception, scope_tokens: List[str]) -> bool:
    text = str(exc or "")
    if "99991672" not in text:
        return False
    low = text.lower()
    return any(tok.lower() in low for tok in scope_tokens)


class MessageDeduper:
    def __init__(self, capacity: int = 2000) -> None:
        self.capacity = max(100, int(capacity))
        self._queue: Deque[str] = deque()
        self._seen: Set[str] = set()
        self._lock = threading.Lock()

    def seen(self, message_id: str) -> bool:
        key = str(message_id or "").strip()
        if not key:
            return False
        with self._lock:
            if key in self._seen:
                return True
            self._seen.add(key)
            self._queue.append(key)
            while len(self._queue) > self.capacity:
                dropped = self._queue.popleft()
                self._seen.discard(dropped)
        return False


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_token: Optional[str] = None
        self._tenant_token_exp: float = 0.0
        self._lock = threading.Lock()

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        with self._lock:
            if self._tenant_token and now < self._tenant_token_exp - 60:
                return self._tenant_token

            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
            payload = {"app_id": self.app_id, "app_secret": self.app_secret}
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Feishu token request failed {resp.status_code}: {resp.text[:500]}"
                )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu token request error: {data}")

            token = str(data.get("tenant_access_token") or "").strip()
            expire = float(data.get("expire", 3600))
            if not token:
                raise RuntimeError(f"Token response did not include tenant_access_token: {data}")

            self._tenant_token = token
            self._tenant_token_exp = now + expire
            return token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_tenant_access_token()}"}

    def post(self, path: str, json_payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
        url = "https://open.feishu.cn/open-apis" + path
        resp = requests.post(url, headers=self._headers(), json=json_payload, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu POST failed {resp.status_code} {url}: {resp.text[:800]}")
        return resp.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        url = "https://open.feishu.cn/open-apis" + path
        resp = requests.get(url, headers=self._headers(), params=params or {}, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu GET failed {resp.status_code} {url}: {resp.text[:800]}")
        return resp.json()

    def post_multipart(
        self,
        path: str,
        data: Dict[str, Any],
        files: Dict[str, Tuple[str, bytes, str]],
        timeout: int = 60,
    ) -> Dict[str, Any]:
        url = "https://open.feishu.cn/open-apis" + path
        resp = requests.post(url, headers=self._headers(), data=data, files=files, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu POST(multipart) failed {resp.status_code} {url}: {resp.text[:800]}")
        return resp.json()

    def send_text(self, chat_id: str, text: str) -> None:
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = self.post("/im/v1/messages?receive_id_type=chat_id", payload)
        if resp.get("code") != 0:
            raise RuntimeError(f"send_text failed: {resp}")

    def send_file(self, chat_id: str, file_key: str) -> None:
        payload = {
            "receive_id": chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        }
        resp = self.post("/im/v1/messages?receive_id_type=chat_id", payload)
        if resp.get("code") != 0:
            raise RuntimeError(f"send_file failed: {resp}")

    def upload_to_im_file(self, file_bytes: bytes, file_name: str, mime_type: Optional[str] = None) -> str:
        data = {"file_type": "stream", "file_name": file_name}
        files = {"file": (file_name, file_bytes, mime_type or "application/octet-stream")}
        resp = self.post_multipart("/im/v1/files", data=data, files=files, timeout=120)
        if resp.get("code") != 0:
            raise RuntimeError(f"upload_to_im_file failed: {resp}")
        file_key = str((resp.get("data") or {}).get("file_key") or "").strip()
        if not file_key:
            raise RuntimeError(f"upload_to_im_file returned empty file_key: {resp}")
        return file_key

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
        timeout: int = 120,
    ) -> bytes:
        mid = str(message_id or "").strip()
        fkey = str(file_key or "").strip()
        if not mid or not fkey:
            raise RuntimeError("download_message_resource requires message_id and file_key")

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{mid}/resources/{fkey}"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={"type": resource_type},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Feishu resource download failed {resp.status_code} {url}: {resp.text[:800]}")

        content_type = str(resp.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            payload = None
            try:
                payload = resp.json()
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                raise RuntimeError(f"download_message_resource failed: {payload}")

        data = resp.content or b""
        if not data:
            raise RuntimeError("download_message_resource returned empty content")
        return data


def _configure_logging() -> None:
    level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
    log_path = (os.getenv("LOG_PATH", "logs/bot.log") or "").strip()

    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _coerce_message_content(raw_content: Any) -> Dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    if isinstance(raw_content, str):
        body = raw_content.strip()
        if not body:
            return {}
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
            return {"text": body}
        except Exception:
            return {"text": body}
    return {}


def _collect_text_candidates(node: Any, out: List[str]) -> None:
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append(text)
        return
    if isinstance(node, list):
        for item in node:
            _collect_text_candidates(item, out)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in {"text", "href", "url", "title", "name", "content"}:
                _collect_text_candidates(value, out)
            else:
                _collect_text_candidates(value, out)


def _extract_lcsc_id(text: str) -> Optional[str]:
    msg = str(text or "")
    hit = LCSC_ID_RE.search(msg)
    if hit:
        return hit.group(0).upper()

    spaced_hit = PART_PREFIXED_SPACED_RE.search(msg)
    if spaced_hit:
        return f"C{spaced_hit.group(1)}"

    numeric_hit = PART_EXPLICIT_NUMERIC_RE.search(msg)
    if numeric_hit:
        token = str(numeric_hit.group(1) or "").strip().upper()
        if token.startswith("C"):
            digits = token[1:]
            if digits.isdigit():
                return f"C{digits}"
        elif token.isdigit():
            return f"C{token}"

    only_digits = msg.strip()
    if only_digits.isdigit() and len(only_digits) >= 3:
        return f"C{only_digits}"

    return None


def _extract_lcsc_ids(text: str) -> List[str]:
    msg = str(text or "")
    out: List[str] = []
    seen: Set[str] = set()

    for hit in LCSC_ID_RE.finditer(msg):
        pid = hit.group(0).upper()
        if pid not in seen:
            seen.add(pid)
            out.append(pid)

    for hit in PART_PREFIXED_SPACED_RE.finditer(msg):
        pid = f"C{hit.group(1)}"
        if pid not in seen:
            seen.add(pid)
            out.append(pid)

    for hit in PART_EXPLICIT_NUMERIC_RE.finditer(msg):
        token = str(hit.group(1) or "").strip().upper()
        pid = None
        if token.startswith("C") and token[1:].isdigit():
            pid = f"C{token[1:]}"
        elif token.isdigit():
            pid = f"C{token}"
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)

    if not out:
        # Fallback for compact compare-style inputs: "/compare 2040 8596 123456"
        for token in re.findall(r"\b\d{3,}\b", msg):
            pid = f"C{token}"
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
    return out


def _fetch_step_file(lcsc_id: str) -> Tuple[str, bytes]:
    return fetch_step_file(lcsc_id)


def _fetch_component_library_archive(lcsc_id: str) -> Tuple[str, bytes]:
    return fetch_component_library_archive(lcsc_id)


def _parse_request_mode(text: str) -> Tuple[str, str]:
    normalized = str(text or "").strip()
    if STEP_CMD_RE.match(normalized):
        return "step", normalized[len("/step") :].strip()
    if re.match(r"^step(\s|$)", normalized, flags=re.IGNORECASE):
        return "step", normalized[len("step") :].strip()
    if LIBRARY_CMD_RE.match(normalized):
        return "library", normalized[len("/library") :].strip()
    return "library", normalized


def _command_payload(text: str, command: str) -> str:
    pattern = rf"^/{re.escape(command)}(?:\s+(.+))?$"
    m = re.match(pattern, str(text or "").strip(), flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return str(m.group(1) or "").strip()


def _send_file_upload_permission_hint(fc: FeishuClient, chat_id: str) -> None:
    fc.send_text(
        chat_id,
        "Bot is missing Feishu permission to upload files (`im:resource:upload` or `im:resource`). "
        "Please enable permission and publish app release, then try again.",
    )


def _process_info_command(fc: FeishuClient, chat_id: str, payload: str) -> None:
    lcsc_id = _extract_lcsc_id(payload)
    if not lcsc_id:
        fc.send_text(chat_id, "Usage: /info <PART_ID>. Example: /info C2040")
        return
    fc.send_text(chat_id, f"Fetching part info for {lcsc_id}...")
    try:
        snapshot = fetch_part_snapshot(lcsc_id)
        fc.send_text(chat_id, format_part_info(snapshot))
    except Exception as exc:
        log.exception("Failed fetching part info for %s", lcsc_id)
        fc.send_text(chat_id, f"Failed to fetch part info for {lcsc_id}: {exc}")


def _process_compare_command(fc: FeishuClient, chat_id: str, payload: str) -> None:
    ids = _extract_lcsc_ids(payload)
    if len(ids) < 2:
        fc.send_text(chat_id, "Usage: /compare <PART_ID> <PART_ID> [...]. Example: /compare C2040 C8596")
        return
    max_parts = max(2, int(os.getenv("COMPARE_MAX_PARTS", "5")))
    selected = ids[:max_parts]
    fc.send_text(chat_id, f"Comparing {len(selected)} parts: {', '.join(selected)}")

    snapshots = []
    failures = []
    for pid in selected:
        try:
            snapshots.append(fetch_part_snapshot(pid))
        except Exception as exc:
            failures.append((pid, str(exc)))
            log.exception("Failed loading compare data for %s", pid)

    if snapshots:
        fc.send_text(chat_id, format_compare(snapshots))
    if failures:
        detail = "; ".join([f"{pid}: {err}" for pid, err in failures[:5]])
        fc.send_text(chat_id, f"Some parts could not be compared: {detail}")


def _bundle_library_archives(libraries: Dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for part_id, payload in libraries.items():
            zf.writestr(f"{part_id}.zip", payload)
    return out.getvalue()


def _process_bom_entries(fc: FeishuClient, chat_id: str, parsed: BomParseResult, source_label: str) -> None:
    if not parsed.entries:
        message = f"No valid part IDs found in {source_label}."
        if parsed.notes:
            message += f" {' '.join(parsed.notes)}"
        fc.send_text(chat_id, message)
        return

    max_parts = max(1, int(os.getenv("BOM_MAX_PARTS", "20")))
    selected_ids = list(parsed.entries.keys())[:max_parts]
    truncated = len(parsed.entries) - len(selected_ids)
    fc.send_text(chat_id, f"Processing BOM: {len(selected_ids)} unique parts from {source_label}...")

    report_rows: List[Dict[str, Any]] = []
    total_cost = 0.0
    cost_parts = 0
    success_ids: List[str] = []
    errors: List[str] = []

    for part_id in selected_ids:
        qty = int(parsed.entries.get(part_id) or 1)
        row: Dict[str, Any] = {
            "Part ID": part_id,
            "Qty": qty,
            "Status": "ok",
            "Error": "",
        }
        try:
            snap = fetch_part_snapshot(part_id)
            unit_price = choose_unit_price(snap.price_tiers, qty)
            ext = round(unit_price * qty, 6) if unit_price is not None else None

            row["Name"] = snap.title or snap.description or ""
            row["Manufacturer"] = snap.manufacturer or ""
            row["MPN"] = snap.mpn or ""
            row["Package"] = snap.package or ""
            row["Stock"] = snap.stock if snap.stock is not None else ""
            row["Unit Price USD"] = f"{unit_price:.6f}" if unit_price is not None else ""
            row["Ext Cost USD"] = f"{ext:.6f}" if ext is not None else ""

            if ext is not None:
                total_cost += ext
                cost_parts += 1
            success_ids.append(part_id)
        except Exception as exc:
            log.exception("BOM lookup failed for %s", part_id)
            row["Status"] = "error"
            row["Error"] = str(exc)
            errors.append(f"{part_id}: {exc}")
        report_rows.append(row)

    summary_lines = [
        f"BOM summary ({source_label}):",
        f"- Rows: {parsed.total_rows}",
        f"- Matched rows: {parsed.matched_rows}",
        f"- Unmatched rows: {parsed.unmatched_rows}",
        f"- Unique parts processed: {len(selected_ids)}",
    ]
    if truncated > 0:
        summary_lines.append(f"- Truncated: skipped {truncated} extra unique parts (BOM_MAX_PARTS={max_parts})")
    if cost_parts > 0:
        summary_lines.append(f"- Estimated total cost (USD): ${total_cost:.4f}")
    else:
        summary_lines.append("- Estimated total cost (USD): unavailable (missing price tiers)")
    if errors:
        summary_lines.append(f"- Parts with errors: {len(errors)}")
    fc.send_text(chat_id, "\n".join(summary_lines))

    try:
        report_bytes = build_bom_report_csv(report_rows)
        report_name = f"bom_report_{int(time.time())}.csv"
        report_key = fc.upload_to_im_file(report_bytes, report_name, mime_type="text/csv")
        fc.send_file(chat_id, report_key)
    except Exception as exc:
        log.exception("Failed sending BOM report file")
        if _is_scope_denied_error(exc, ["im:resource:upload", "im:resource"]):
            _send_file_upload_permission_hint(fc, chat_id)
        else:
            fc.send_text(chat_id, f"Failed to send BOM report file: {exc}")

    generate_libs = (os.getenv("BOM_GENERATE_LIBS", "0") or "0").strip().lower() not in {"0", "false", "no", "off"}
    if not generate_libs:
        return
    if not success_ids:
        return

    max_libs = max(1, int(os.getenv("BOM_MAX_LIBS", "5")))
    library_targets = success_ids[:max_libs]
    fc.send_text(chat_id, f"Generating KiCad libraries for {len(library_targets)} BOM parts...")

    bundled: Dict[str, bytes] = {}
    lib_errors = []
    for part_id in library_targets:
        try:
            _, archive_bytes = _fetch_component_library_archive(part_id)
            bundled[part_id] = archive_bytes
        except Exception as exc:
            log.exception("Failed generating BOM library for %s", part_id)
            lib_errors.append(f"{part_id}: {exc}")

    if bundled:
        try:
            bundle_bytes = _bundle_library_archives(bundled)
            bundle_name = f"bom_kicad_libraries_{int(time.time())}.zip"
            bundle_key = fc.upload_to_im_file(bundle_bytes, bundle_name, mime_type="application/zip")
            fc.send_file(chat_id, bundle_key)
            fc.send_text(chat_id, f"Done. Sent {bundle_name}")
        except Exception as exc:
            log.exception("Failed sending BOM libraries bundle")
            if _is_scope_denied_error(exc, ["im:resource:upload", "im:resource"]):
                _send_file_upload_permission_hint(fc, chat_id)
            else:
                fc.send_text(chat_id, f"Failed to send BOM libraries bundle: {exc}")

    if lib_errors:
        fc.send_text(chat_id, "Some BOM libraries could not be generated: " + "; ".join(lib_errors[:5]))


def _process_bom_text_command(fc: FeishuClient, chat_id: str, payload: str) -> None:
    content = str(payload or "").strip()
    if not content:
        fc.send_text(
            chat_id,
            "Usage: /bom <rows> (one per line, or compact in one line like `C2040,10 C8596,5`), "
            "or upload a CSV/XLSX file directly in this 1:1 chat.",
        )
        return
    parsed = parse_bom_text(content, _extract_lcsc_id)
    _process_bom_entries(fc, chat_id, parsed, source_label="text payload")


def _process_bom_file_message(
    fc: FeishuClient,
    chat_id: str,
    message_id: str,
    content: Dict[str, Any],
) -> None:
    file_key = str(content.get("file_key") or "").strip()
    file_name = str(content.get("file_name") or content.get("name") or "uploaded_bom").strip()
    if not file_key:
        fc.send_text(chat_id, "Could not read file metadata from message. Please resend the BOM file.")
        return

    fc.send_text(chat_id, f"Downloading BOM file `{file_name}`...")
    try:
        file_bytes = fc.download_message_resource(message_id=message_id, file_key=file_key, resource_type="file")
    except Exception as exc:
        log.exception("Failed downloading BOM message resource message_id=%s file_key=%s", message_id, file_key)
        if _is_scope_denied_error(exc, ["im:message", "im:message.p2p_msg:readonly"]):
            fc.send_text(
                chat_id,
                "Bot is missing Feishu permission to download message resources. "
                "Please grant message read/resource scopes and publish app release.",
            )
            return
        fc.send_text(chat_id, f"Failed to download BOM file: {exc}")
        return

    try:
        parsed = parse_bom_bytes(file_bytes, file_name, _extract_lcsc_id)
    except Exception as exc:
        log.exception("Failed parsing BOM file %s", file_name)
        fc.send_text(chat_id, f"Failed to parse BOM file `{file_name}`: {exc}")
        return

    _process_bom_entries(fc, chat_id, parsed, source_label=file_name)


def _process_library_or_step_request(fc: FeishuClient, chat_id: str, text: str) -> None:
    mode, payload = _parse_request_mode(text)
    if not payload:
        fc.send_text(chat_id, HELP_TEXT)
        return

    lcsc_id = _extract_lcsc_id(payload)
    if not lcsc_id:
        fc.send_text(
            chat_id,
            "Could not find an LCSC/JLCPCB part ID in your message. "
            "Send a value like C2040, a JLCPCB part number like 2040, or a product link.",
        )
        return

    try:
        if mode == "step":
            fc.send_text(chat_id, f"Fetching STEP file for {lcsc_id}...")
            file_name, file_bytes = _fetch_step_file(lcsc_id)
            mime_type = "application/step"
        else:
            fc.send_text(chat_id, f"Generating KiCad component library for {lcsc_id}...")
            file_name, file_bytes = _fetch_component_library_archive(lcsc_id)
            mime_type = "application/zip"

        file_key = fc.upload_to_im_file(file_bytes, file_name, mime_type=mime_type)
        fc.send_file(chat_id, file_key)
        fc.send_text(chat_id, f"Done. Sent {file_name}")
    except Exception as exc:
        if mode == "step":
            log.exception("Failed generating STEP for %s", lcsc_id)
        else:
            log.exception("Failed generating KiCad library for %s", lcsc_id)
        if _is_scope_denied_error(exc, ["im:resource:upload", "im:resource"]):
            _send_file_upload_permission_hint(fc, chat_id)
            return
        if mode == "step":
            fc.send_text(chat_id, f"Failed to download STEP for {lcsc_id}: {exc}")
        else:
            fc.send_text(chat_id, f"Failed to generate KiCad library for {lcsc_id}: {exc}")


def _process_lcsc_request(fc: FeishuClient, chat_id: str, text: str) -> None:
    normalized = str(text or "").strip()
    if not normalized:
        fc.send_text(chat_id, HELP_TEXT)
        return

    if normalized.lower() in {"/help", "help", "/start"}:
        fc.send_text(chat_id, HELP_TEXT)
        return
    if normalized.lower() in {"/ping", "ping"}:
        fc.send_text(chat_id, "pong")
        return

    if INFO_CMD_RE.match(normalized):
        _process_info_command(fc, chat_id, _command_payload(normalized, "info"))
        return
    if COMPARE_CMD_RE.match(normalized):
        _process_compare_command(fc, chat_id, _command_payload(normalized, "compare"))
        return
    if BOM_CMD_RE.match(normalized):
        _process_bom_text_command(fc, chat_id, _command_payload(normalized, "bom"))
        return

    _process_library_or_step_request(fc, chat_id, normalized)


def handle_p2_im_message_receive_v1(fc: FeishuClient, dedup: MessageDeduper, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        event = data.event
        msg = event.message
        message_id = str(msg.message_id or "").strip()
        chat_id = str(msg.chat_id or "").strip()
        chat_type = str(msg.chat_type or "").strip().lower()
        msg_type = str(msg.message_type or "").strip().lower()

        sender_type = ""
        sender_id = ""
        try:
            sender_type = str(event.sender.sender_type or "").strip().lower()
            sid = event.sender.sender_id
            sender_id = str(getattr(sid, "open_id", None) or getattr(sid, "user_id", None) or "").strip()
        except Exception:
            sender_type = ""
            sender_id = ""

        # Log as early as possible so we can debug delivery/filters.
        log.info(
            "Event received: message_id=%s type=%s chat_type=%s sender_type=%s sender=%s",
            message_id or "-",
            msg_type or "-",
            chat_type or "-",
            sender_type or "-",
            (sender_id[:8] + "***") if sender_id else "-",
        )

        if dedup.seen(message_id):
            log.info("Duplicate event ignored: message_id=%s", message_id)
            return

        if sender_type and sender_type != "user":
            log.info("Ignoring non-user sender_type=%s message_id=%s", sender_type, message_id)
            return

        # Keep bot focused on 1:1 chats, but tolerate equivalent non-group labels.
        if chat_type in {"group", "topic"}:
            fc.send_text(chat_id, "This bot only works in 1:1 chats.")
            return

        content = _coerce_message_content(msg.content)
        log.info("Incoming message id=%s type=%s chat_type=%s", message_id, msg_type, chat_type or "unknown")

        if msg_type == "text":
            text = str(content.get("text") or "").strip()
            if not text and isinstance(msg.content, str):
                text = msg.content
            _process_lcsc_request(fc, chat_id, text)
            return

        if msg_type == "post":
            candidates: List[str] = []
            _collect_text_candidates(content, candidates)
            _process_lcsc_request(fc, chat_id, " ".join(candidates))
            return

        if msg_type == "file":
            _process_bom_file_message(fc, chat_id, message_id=message_id, content=content)
            return

        fc.send_text(chat_id, "Please send text commands, part IDs/links, or a BOM CSV/XLSX file.")
    except Exception:
        log.exception("Unhandled error in message callback")


def handle_p2_im_chat_access_event_bot_p2p_chat_entered_v1(fc: FeishuClient, data: Any) -> None:
    try:
        event = getattr(data, "event", None)
        operator_id = ""
        chat_id = ""
        if event is not None:
            try:
                chat_id = str(getattr(event, "chat_id", "") or "").strip()
            except Exception:
                chat_id = ""
            try:
                operator = getattr(event, "operator_id", None)
                if operator is not None:
                    operator_id = str(
                        getattr(operator, "open_id", None)
                        or getattr(operator, "user_id", None)
                        or ""
                    ).strip()
            except Exception:
                operator_id = ""

        log.info(
            "P2P chat entered event: chat_id=%s operator=%s",
            chat_id or "-",
            (operator_id[:8] + "***") if operator_id else "-",
        )
        if chat_id:
            fc.send_text(
                chat_id,
                "Bot is online. Send an LCSC link/ID like C70078. Use /help for /info, /compare, /bom, /library, /step.",
            )
    except Exception:
        log.exception("Unhandled error in p2p-chat-entered callback")


def handle_p2_im_message_message_read_v1(data: Any) -> None:
    try:
        event = getattr(data, "event", None)
        count = 0
        if event is not None:
            ids = getattr(event, "message_id_list", None)
            if isinstance(ids, list):
                count = len(ids)
        log.debug("Read-receipt event received: message_count=%s", count)
    except Exception:
        log.exception("Unhandled error in message-read callback")


def log_scope_diagnostics(fc: FeishuClient) -> None:
    required_scopes = {
        "im:message:send_as_bot",
        "im:message",
        "im:message.p2p_msg:readonly",
    }
    optional_scopes = {
        "im:chat.access_event.bot_p2p_chat:read",
        "im:message.group_at_msg.include_bot:readonly",
    }
    try:
        resp = fc.get("/application/v6/scopes", timeout=30)
    except Exception:
        log.exception("Failed to read granted scopes from /application/v6/scopes")
        return

    if resp.get("code") != 0:
        log.warning("Could not load granted scopes: %s", resp)
        return

    data = resp.get("data") or {}
    items = data.get("scopes") or []
    granted: Set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        if int(it.get("grant_status") or 0) != 1:
            continue
        scope_name = str(it.get("scope_name") or "").strip()
        if scope_name:
            granted.add(scope_name)

    missing_required = sorted(required_scopes - granted)
    upload_scope_ok = ("im:resource:upload" in granted) or ("im:resource" in granted)
    missing_optional = sorted(optional_scopes - granted)
    if missing_required:
        log.warning(
            "Missing required scopes for p2p message receive/send: %s. "
            "Without these, bot may connect via WS but receive no user messages.",
            ", ".join(missing_required),
        )
    else:
        log.info("Required p2p scopes present.")

    if not upload_scope_ok:
        log.warning(
            "Missing file-upload scope: enable `im:resource:upload` (or `im:resource`) "
            "to allow sending generated files via /im/v1/files."
        )

    if missing_optional:
        log.info("Optional scopes missing: %s", ", ".join(missing_optional))


def main() -> None:
    if load_dotenv:
        load_dotenv()

    _configure_logging()

    app_id = (os.getenv("FEISHU_APP_ID", "") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET", "") or "").strip()
    verify_token = (os.getenv("FEISHU_VERIFICATION_TOKEN", "") or "").strip()
    encrypt_key = (os.getenv("FEISHU_ENCRYPT_KEY", "") or "").strip()
    dedup_capacity = int(os.getenv("DEDUP_CAPACITY", "3000"))
    ws_log_level_name = (os.getenv("FEISHU_WS_LOG_LEVEL", "INFO") or "INFO").strip().upper()
    ws_log_level = getattr(lark.LogLevel, ws_log_level_name, lark.LogLevel.INFO)

    if not app_id or not app_secret:
        raise RuntimeError("Missing required env: FEISHU_APP_ID and FEISHU_APP_SECRET")

    log.info("Starting Feishu LCSC bot")
    log.info("Using FEISHU_APP_ID=%s***", app_id[:6] if len(app_id) > 6 else app_id)
    log.info("Using FEISHU_WS_LOG_LEVEL=%s", ws_log_level_name)

    fc = FeishuClient(app_id, app_secret)
    log_scope_diagnostics(fc)
    dedup = MessageDeduper(capacity=dedup_capacity)

    dispatcher_builder = lark.EventDispatcherHandler.builder(encrypt_key, verify_token)
    dispatcher_builder.register_p2_im_message_receive_v1(lambda data: handle_p2_im_message_receive_v1(fc, dedup, data))
    dispatcher_builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
        lambda data: handle_p2_im_chat_access_event_bot_p2p_chat_entered_v1(fc, data)
    )
    dispatcher_builder.register_p2_im_message_message_read_v1(handle_p2_im_message_message_read_v1)
    dispatcher = dispatcher_builder.build()

    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        log_level=ws_log_level,
        event_handler=dispatcher,
    )
    ws_client.start()


if __name__ == "__main__":
    main()
