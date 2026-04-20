import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import lark_oapi as lark
import requests
from lcsc_step_downloader.core import fetch_step_file

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


log = logging.getLogger("feishu-lcsc-bot")


LCSC_ID_RE = re.compile(r"(?<![A-Za-z0-9])[Cc]\d{3,}(?![A-Za-z0-9])")
LCSC_EXPLICIT_NUMERIC_RE = re.compile(r"\blcsc(?:[_\-\s]*id)?\s*[:=#]?\s*(\d{3,})\b", flags=re.IGNORECASE)


HELP_TEXT = (
    "Send an LCSC link or LCSC ID and I will return the STEP file.\n"
    "Examples:\n"
    "- C2040\n"
    "- https://www.lcsc.com/product-detail/..._C2040.html"
)


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

    numeric_hit = LCSC_EXPLICIT_NUMERIC_RE.search(msg)
    if numeric_hit:
        return f"C{numeric_hit.group(1)}"

    return None


def _fetch_step_file(lcsc_id: str) -> Tuple[str, bytes]:
    return fetch_step_file(lcsc_id)


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

    lcsc_id = _extract_lcsc_id(normalized)
    if not lcsc_id:
        fc.send_text(
            chat_id,
            "Could not find an LCSC ID in your message. Send a value like C2040 or an LCSC product link.",
        )
        return

    fc.send_text(chat_id, f"Fetching STEP file for {lcsc_id}...")
    try:
        file_name, step_bytes = _fetch_step_file(lcsc_id)
        file_key = fc.upload_to_im_file(step_bytes, file_name, mime_type="application/step")
        fc.send_file(chat_id, file_key)
        fc.send_text(chat_id, f"Done. Sent {file_name}")
    except Exception as exc:
        log.exception("Failed generating STEP for %s", lcsc_id)
        if _is_scope_denied_error(exc, ["im:resource:upload", "im:resource"]):
            fc.send_text(
                chat_id,
                "Bot is missing Feishu permission to upload files (`im:resource:upload` or `im:resource`). "
                "Please enable permission and publish app release, then try again.",
            )
            return
        fc.send_text(chat_id, f"Failed to download STEP for {lcsc_id}: {exc}")


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

        fc.send_text(chat_id, "Please send an LCSC link or ID as text.")
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
            fc.send_text(chat_id, "Bot is online. Send an LCSC link or ID like C70078.")
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
            "to allow sending STEP files via /im/v1/files."
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
