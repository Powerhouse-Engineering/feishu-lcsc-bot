import csv
import glob
import io
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None


EASYEDA_USER_AGENT = "JLC2KiCadLib/1.2.3 (https://github.com/TousstNicolas/JLC2KiCad_lib)"
LCSC_WEB_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"
NUXT_SCRIPT_RE = re.compile(r"window\.__NUXT__=.*?</script>", flags=re.IGNORECASE | re.DOTALL)


@dataclass
class PriceTier:
    ladder: int
    unit_price_usd: float


@dataclass
class PartSnapshot:
    lcsc_id: str
    title: Optional[str]
    description: Optional[str]
    manufacturer: Optional[str]
    mpn: Optional[str]
    package: Optional[str]
    lifecycle: Optional[str]
    stock: Optional[int]
    min_order_qty: Optional[int]
    moq_reel: Optional[int]
    moq_reel_unit: Optional[str]
    datasheet_url: Optional[str]
    product_url: Optional[str]
    category: Optional[str]
    price_tiers: List[PriceTier]
    params: List[Tuple[str, str]]


@dataclass
class BomParseResult:
    entries: Dict[str, int]
    total_rows: int
    matched_rows: int
    unmatched_rows: int
    notes: List[str]


def normalize_lcsc_id(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        raise RuntimeError("Part ID is empty")
    if raw.startswith("C") and raw[1:].isdigit():
        return raw
    if raw.isdigit():
        return f"C{raw}"
    raise RuntimeError(f"Invalid part ID: {value}")


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if "." in text:
            return int(float(text))
        return int(text)
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    try:
        return float(text)
    except Exception:
        return None


def _resolve_node_binary() -> str:
    configured = str(os.getenv("NODE_BIN") or "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise RuntimeError(f"NODE_BIN is set but not executable: {configured}")

    candidates: List[str] = []
    which_node = shutil.which("node")
    if which_node:
        candidates.append(which_node)
    candidates.extend(
        [
            "/usr/bin/node",
            "/usr/local/bin/node",
            "/opt/homebrew/bin/node",
        ]
    )
    candidates.extend(
        sorted(
            glob.glob("/home/*/.nvm/versions/node/*/bin/node"),
            reverse=True,
        )
    )

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise RuntimeError(
        "Node.js runtime not found. Install Node.js or set NODE_BIN in the bot environment."
    )


def _extract_lcsc_detail_via_node(nuxt_script: str, timeout_sec: int = 20) -> Dict[str, Any]:
    node_bin = _resolve_node_binary()
    node_code = """
const fs = require("fs");
const code = fs.readFileSync(0, "utf8");
global.window = {};
try {
  eval(code);
  const nuxt = window.__NUXT__ || {};
  const data0 = (nuxt.data || [])[0] || {};
  const detail = data0.detail || {};
  process.stdout.write(JSON.stringify(detail));
} catch (e) {
  process.stderr.write(String(e && e.message ? e.message : e));
  process.exit(1);
}
"""
    proc = subprocess.run(
        [node_bin, "-e", node_code],
        input=nuxt_script,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_sec)),
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed parsing LCSC page data: {err or 'unknown parser error'}")

    try:
        parsed = json.loads(proc.stdout or "{}")
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        raise RuntimeError("Failed decoding LCSC page JSON payload") from exc
    raise RuntimeError("LCSC page payload is empty")


def _build_datasheet_url(detail: Dict[str, Any]) -> Optional[str]:
    link = str(detail.get("pdfLinkUrl") or "").strip()
    if link:
        return link
    pdf_path = str(detail.get("pdfUrl") or "").strip()
    if not pdf_path:
        return None
    if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
        return pdf_path
    if pdf_path.startswith("/"):
        return f"https://www.lcsc.com{pdf_path}"
    return f"https://www.lcsc.com/{pdf_path}"


def _parse_price_tiers(detail: Dict[str, Any]) -> List[PriceTier]:
    tiers: List[PriceTier] = []
    raw_items = detail.get("productPriceList") or []
    if not isinstance(raw_items, list):
        return tiers

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        ladder = _safe_int(item.get("ladder"))
        price = _safe_float(item.get("usdPrice"))
        if price is None:
            price = _safe_float(item.get("currencyPrice"))
        if price is None:
            price = _safe_float(item.get("productPrice"))
        if ladder is None or price is None:
            continue
        if ladder <= 0 or price < 0:
            continue
        tiers.append(PriceTier(ladder=ladder, unit_price_usd=price))

    tiers.sort(key=lambda x: x.ladder)
    deduped: List[PriceTier] = []
    seen = set()
    for tier in tiers:
        if tier.ladder in seen:
            continue
        deduped.append(tier)
        seen.add(tier.ladder)
    return deduped


def _parse_params(detail: Dict[str, Any], limit: int = 8) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    raw = detail.get("paramVOList") or []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("paramNameEn") or item.get("paramName") or "").strip()
        value = str(item.get("paramValueEn") or item.get("paramValue") or "").strip()
        if not name or not value or value == "-":
            continue
        out.append((name, value))
        if len(out) >= max(1, int(limit)):
            break
    return out


def _fetch_easyeda_meta(lcsc_id: str, timeout_sec: int = 25) -> Dict[str, Optional[str]]:
    headers = {"User-Agent": EASYEDA_USER_AGENT}
    resp = requests.get(
        f"https://easyeda.com/api/products/{lcsc_id}/svgs",
        headers=headers,
        timeout=max(5, int(timeout_sec)),
    )
    if resp.status_code != 200:
        return {}
    try:
        payload = resp.json()
    except Exception:
        return {}

    if not payload.get("success"):
        return {}
    result = payload.get("result") or []
    if not isinstance(result, list) or not result:
        return {}

    symbol_uuid = None
    footprint_uuid = None
    if len(result) >= 1:
        symbol_uuid = str((result[0] or {}).get("component_uuid") or "").strip()
    if len(result) >= 2:
        footprint_uuid = str((result[-1] or {}).get("component_uuid") or "").strip()

    out: Dict[str, Optional[str]] = {}
    if symbol_uuid:
        symbol_resp = requests.get(
            f"https://easyeda.com/api/components/{symbol_uuid}",
            headers=headers,
            timeout=max(5, int(timeout_sec)),
        )
        if symbol_resp.status_code == 200:
            try:
                symbol_data = symbol_resp.json().get("result") or {}
                c_para = ((symbol_data.get("dataStr") or {}).get("head") or {}).get("c_para") or {}
                out["easyeda_name"] = str(c_para.get("name") or symbol_data.get("title") or "").strip() or None
                out["manufacturer"] = str(c_para.get("Manufacturer") or "").strip() or None
                out["mpn"] = str(c_para.get("Manufacturer Part") or "").strip() or None
                out["package"] = str(c_para.get("package") or "").strip() or None
                out["jlc_class"] = str(c_para.get("JLCPCB Part Class") or "").strip() or None
            except Exception:
                pass

    if footprint_uuid:
        foot_resp = requests.get(
            f"https://easyeda.com/api/components/{footprint_uuid}",
            headers=headers,
            timeout=max(5, int(timeout_sec)),
        )
        if foot_resp.status_code == 200:
            try:
                foot_data = foot_resp.json().get("result") or {}
                foot_c_para = ((foot_data.get("dataStr") or {}).get("head") or {}).get("c_para") or {}
                out["datasheet_url"] = str(foot_c_para.get("link") or "").strip() or None
            except Exception:
                pass

    return out


def fetch_part_snapshot(lcsc_id: str, timeout_sec: int = 30) -> PartSnapshot:
    normalized = normalize_lcsc_id(lcsc_id)
    page_resp = requests.get(
        f"https://www.lcsc.com/product-detail/{normalized}.html",
        headers={"User-Agent": LCSC_WEB_USER_AGENT},
        timeout=max(8, int(timeout_sec)),
    )
    if page_resp.status_code >= 400:
        raise RuntimeError(f"LCSC page request failed with HTTP {page_resp.status_code}")
    html = page_resp.text
    nuxt_match = NUXT_SCRIPT_RE.search(html)
    if not nuxt_match:
        raise RuntimeError("Could not locate product data in LCSC page")

    nuxt_script = nuxt_match.group(0).rsplit("</script>", 1)[0]
    detail = _extract_lcsc_detail_via_node(nuxt_script, timeout_sec=timeout_sec)

    product_code = str(detail.get("productCode") or "").strip().upper()
    if product_code and product_code != normalized:
        normalized = product_code

    manufacturer = str(detail.get("brandNameEn") or "").strip() or None
    mpn = str(detail.get("productModel") or "").strip() or None
    package = str(detail.get("encapStandard") or "").strip() or None
    datasheet_url = _build_datasheet_url(detail)
    snapshot = PartSnapshot(
        lcsc_id=normalized,
        title=str(detail.get("title") or "").strip() or None,
        description=str(detail.get("productIntroEn") or detail.get("productNameEn") or "").strip() or None,
        manufacturer=manufacturer,
        mpn=mpn,
        package=package,
        lifecycle=str(detail.get("productCycle") or "").strip() or None,
        stock=_safe_int(detail.get("stockNumber")),
        min_order_qty=_safe_int(detail.get("minBuyNumber")),
        moq_reel=_safe_int(detail.get("minPacketNumber")),
        moq_reel_unit=str(detail.get("minPacketUnit") or "").strip() or None,
        datasheet_url=datasheet_url,
        product_url=str(page_resp.url or "").strip() or None,
        category=str(detail.get("parentCatalogName") or "").strip() or None,
        price_tiers=_parse_price_tiers(detail),
        params=_parse_params(detail, limit=8),
    )

    # Fill missing fields from EasyEDA metadata when available.
    easyeda = _fetch_easyeda_meta(snapshot.lcsc_id, timeout_sec=timeout_sec)
    if not snapshot.title and easyeda.get("easyeda_name"):
        snapshot.title = easyeda["easyeda_name"]
    if not snapshot.manufacturer and easyeda.get("manufacturer"):
        snapshot.manufacturer = easyeda["manufacturer"]
    if not snapshot.mpn and easyeda.get("mpn"):
        snapshot.mpn = easyeda["mpn"]
    if not snapshot.package and easyeda.get("package"):
        snapshot.package = easyeda["package"]
    if not snapshot.datasheet_url and easyeda.get("datasheet_url"):
        snapshot.datasheet_url = easyeda["datasheet_url"]
    if easyeda.get("jlc_class"):
        snapshot.params.append(("JLCPCB Part Class", str(easyeda["jlc_class"])))

    return snapshot


def choose_unit_price(price_tiers: List[PriceTier], qty: int) -> Optional[float]:
    if not price_tiers:
        return None
    q = max(1, int(qty))
    chosen = None
    for tier in sorted(price_tiers, key=lambda x: x.ladder):
        if q >= tier.ladder:
            chosen = tier
        elif chosen is None:
            chosen = tier
            break
        else:
            break
    return chosen.unit_price_usd if chosen else None


def format_part_info(snapshot: PartSnapshot) -> str:
    lines: List[str] = [f"{snapshot.lcsc_id}"]
    if snapshot.title:
        lines.append(f"Name: {snapshot.title}")
    if snapshot.description and snapshot.description != snapshot.title:
        lines.append(f"Description: {snapshot.description}")
    if snapshot.manufacturer:
        lines.append(f"Manufacturer: {snapshot.manufacturer}")
    if snapshot.mpn:
        lines.append(f"MPN: {snapshot.mpn}")
    if snapshot.package:
        lines.append(f"Package: {snapshot.package}")
    if snapshot.lifecycle:
        lines.append(f"Lifecycle: {snapshot.lifecycle}")
    if snapshot.stock is not None:
        lines.append(f"Stock: {snapshot.stock}")
    if snapshot.min_order_qty is not None:
        lines.append(f"Min Order Qty: {snapshot.min_order_qty}")
    if snapshot.moq_reel is not None and snapshot.moq_reel > 0:
        unit = snapshot.moq_reel_unit or "units"
        lines.append(f"Reel MOQ: {snapshot.moq_reel} {unit}")
    if snapshot.category:
        lines.append(f"Category: {snapshot.category}")
    if snapshot.datasheet_url:
        lines.append(f"Datasheet: {snapshot.datasheet_url}")
    if snapshot.product_url:
        lines.append(f"LCSC Page: {snapshot.product_url}")

    if snapshot.price_tiers:
        tiers = ", ".join(
            [f"{tier.ladder}+ @ ${tier.unit_price_usd:.4f}" for tier in snapshot.price_tiers[:8]]
        )
        lines.append(f"Price Tiers (USD): {tiers}")
    else:
        lines.append("Price Tiers (USD): unavailable")

    if snapshot.params:
        param_text = ", ".join([f"{k}: {v}" for k, v in snapshot.params[:6]])
        lines.append(f"Key Params: {param_text}")
    return "\n".join(lines)


def format_compare(snapshots: List[PartSnapshot]) -> str:
    if not snapshots:
        return "No parts to compare."
    lines = ["Comparison (LCSC/JLCPCB):"]
    lines.append("ID | MPN | Package | Stock | Unit@1 USD")
    for snap in snapshots:
        unit_price = choose_unit_price(snap.price_tiers, 1)
        unit_text = f"${unit_price:.4f}" if unit_price is not None else "n/a"
        mpn = snap.mpn or "-"
        package = snap.package or "-"
        stock = str(snap.stock) if snap.stock is not None else "n/a"
        lines.append(f"{snap.lcsc_id} | {mpn} | {package} | {stock} | {unit_text}")
    return "\n".join(lines)


def _is_header_row(cells: List[str]) -> bool:
    non_empty = [x for x in cells if x]
    if not non_empty:
        return False
    alpha_count = sum(1 for x in non_empty if re.search(r"[A-Za-z]", x))
    return alpha_count >= max(1, len(non_empty) // 2)


def _read_text_file(file_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return file_bytes.decode(enc)
        except Exception:
            continue
    return file_bytes.decode("utf-8", errors="ignore")


def _rows_from_csv_text(text: str) -> List[List[str]]:
    content = text.replace("\r\n", "\n").replace("\r", "\n")
    sample = content[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:
        pass
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    rows: List[List[str]] = []
    for row in reader:
        rows.append([str(cell or "").strip() for cell in row])
    return rows


def _rows_from_xlsx(file_bytes: bytes) -> List[List[str]]:
    if openpyxl is None:
        raise RuntimeError("XLSX parsing requires openpyxl. Install it and retry.")
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        sheet = wb.active
        rows: List[List[str]] = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([str(cell or "").strip() for cell in row])
        return rows
    finally:
        wb.close()


def _normalize_rows_to_dicts(rows: List[List[str]]) -> List[Dict[str, str]]:
    clean_rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not clean_rows:
        return []
    width = max(len(r) for r in clean_rows)
    padded = [r + [""] * (width - len(r)) for r in clean_rows]

    first = padded[0]
    if _is_header_row(first):
        headers = [x.strip() or f"col_{idx+1}" for idx, x in enumerate(first)]
        body = padded[1:]
    else:
        headers = [f"col_{idx+1}" for idx in range(width)]
        body = padded

    out: List[Dict[str, str]] = []
    for row in body:
        item = {headers[idx]: row[idx].strip() for idx in range(width)}
        out.append(item)
    return out


def _extract_quantity_from_row(row: Dict[str, str]) -> int:
    qty_keys = [k for k in row.keys() if re.search(r"(qty|quantity|amount|count|pcs)", k, flags=re.IGNORECASE)]
    for key in qty_keys:
        value = row.get(key)
        qty = _safe_int(value)
        if qty is not None and qty > 0:
            return qty
    return 1


def parse_bom_bytes(
    file_bytes: bytes,
    file_name: str,
    id_extractor: Any,
) -> BomParseResult:
    name = str(file_name or "").strip().lower()
    notes: List[str] = []
    if name.endswith(".xlsx"):
        rows = _rows_from_xlsx(file_bytes)
    else:
        text = _read_text_file(file_bytes)
        rows = _rows_from_csv_text(text)

    row_dicts = _normalize_rows_to_dicts(rows)
    entries: Dict[str, int] = {}
    total_rows = len(row_dicts)
    matched_rows = 0
    unmatched_rows = 0

    for row in row_dicts:
        part_id = None
        preferred_keys = [
            key
            for key in row.keys()
            if re.search(r"(lcsc|jlcpcb|part|supplier|mpn|model)", key, flags=re.IGNORECASE)
        ]
        candidates = [row.get(k, "") for k in preferred_keys] or list(row.values())
        for value in candidates:
            part_id = id_extractor(value)
            if part_id:
                break
        if not part_id:
            unmatched_rows += 1
            continue
        qty = _extract_quantity_from_row(row)
        entries[part_id] = int(entries.get(part_id, 0)) + max(1, qty)
        matched_rows += 1

    if not entries:
        notes.append("No valid LCSC/JLCPCB part IDs were found in the BOM.")
    return BomParseResult(
        entries=entries,
        total_rows=total_rows,
        matched_rows=matched_rows,
        unmatched_rows=unmatched_rows,
        notes=notes,
    )


def parse_bom_text(payload: str, id_extractor: Any) -> BomParseResult:
    lines = [line.strip() for line in str(payload or "").splitlines() if line.strip()]
    entries: Dict[str, int] = {}
    matched = 0
    unmatched = 0

    def _extract_qty(text: str) -> int:
        qty_match = re.search(r"(?:qty|quantity|x|[*,:=])\s*(\d+)", text, flags=re.IGNORECASE)
        if not qty_match:
            return 1
        return max(1, int(qty_match.group(1)))

    for line in lines:
        # Support compact multi-part lines like:
        # C2040,10 C8596,5 C7423108 x2
        compact_hits = list(re.finditer(r"(?<![A-Za-z0-9])[Cc](\d{3,})(?![A-Za-z0-9])", line))
        if len(compact_hits) > 1:
            for idx, hit in enumerate(compact_hits):
                pid = f"C{hit.group(1)}"
                next_start = compact_hits[idx + 1].start() if idx + 1 < len(compact_hits) else len(line)
                span = line[hit.end() : next_start]
                qty = _extract_qty(span)
                entries[pid] = int(entries.get(pid, 0)) + qty
                matched += 1
            continue

        pid = id_extractor(line)
        if not pid:
            unmatched += 1
            continue
        qty = _extract_qty(line)
        entries[pid] = int(entries.get(pid, 0)) + qty
        matched += 1

    notes: List[str] = []
    if not entries:
        notes.append("No valid part IDs found in text payload.")
    return BomParseResult(
        entries=entries,
        total_rows=len(lines),
        matched_rows=matched,
        unmatched_rows=unmatched,
        notes=notes,
    )


def build_bom_report_csv(rows: List[Dict[str, Any]]) -> bytes:
    columns = [
        "Part ID",
        "Qty",
        "Name",
        "Manufacturer",
        "MPN",
        "Package",
        "Stock",
        "Unit Price USD",
        "Ext Cost USD",
        "Status",
        "Error",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in columns})
    return out.getvalue().encode("utf-8")
