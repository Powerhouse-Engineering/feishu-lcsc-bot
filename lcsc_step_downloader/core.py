import io
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from easyeda2kicad.easyeda.easyeda_importer import Easyeda3dModelImporter


VALID_3D_MODEL_TYPES = {"STEP", "WRL"}


def sanitize_step_filename(base_name: str, lcsc_id: str) -> str:
    raw = (base_name or "").strip()
    if not raw:
        raw = lcsc_id
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not safe:
        safe = lcsc_id
    if not safe.lower().endswith(".step"):
        safe = f"{safe}.step"
    return safe


def sanitize_archive_filename(base_name: str, lcsc_id: str) -> str:
    raw = (base_name or "").strip()
    if not raw:
        raw = f"{lcsc_id}_kicad_library"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not safe:
        safe = f"{lcsc_id}_kicad_library"
    if not safe.lower().endswith(".zip"):
        safe = f"{safe}.zip"
    return safe


def _tail(text: str, max_chars: int = 1200) -> str:
    content = (text or "").strip()
    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def _normalize_lcsc_id(lcsc_id: str) -> str:
    value = str(lcsc_id or "").strip().upper()
    if not value:
        raise RuntimeError("LCSC ID is empty")
    if value.isdigit():
        value = f"C{value}"
    return value


def _parse_model_selection(raw: str) -> List[str]:
    cleaned = str(raw or "").strip()
    if not cleaned:
        return ["STEP"]

    tokens = [token.strip().upper() for token in cleaned.replace(",", " ").split() if token.strip()]
    if not tokens:
        return ["STEP"]

    if all(token in {"NONE", "NO", "OFF", "FALSE", "0"} for token in tokens):
        return []

    selected: List[str] = []
    seen = set()
    for token in tokens:
        if token not in VALID_3D_MODEL_TYPES:
            continue
        if token not in seen:
            selected.append(token)
            seen.add(token)
    if not selected:
        return ["STEP"]
    return selected


def _jlc2kicad_root() -> Path:
    configured = (os.getenv("JLC2KICAD_ROOT", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "JLC2KiCad_lib-master").resolve()


def _run_jlc2kicad(
    lcsc_id: str,
    output_dir: Path,
    extra_args: Optional[Sequence[str]] = None,
    timeout_sec: int = 300,
) -> subprocess.CompletedProcess[str]:
    normalized_id = _normalize_lcsc_id(lcsc_id)
    root = _jlc2kicad_root()
    package_dir = root / "JLC2KiCadLib"
    if not package_dir.exists():
        raise RuntimeError(f"JLC2KiCadLib folder not found at {root}")

    cmd = [
        sys.executable,
        "-m",
        "JLC2KiCadLib.JLC2KiCadLib",
        normalized_id,
        "-dir",
        str(output_dir),
    ]
    if extra_args:
        cmd.extend([str(x) for x in extra_args])

    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root) if not existing_pythonpath else f"{root}{os.pathsep}{existing_pythonpath}"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=max(60, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"JLC2KiCadLib generation timed out after {timeout_sec}s for {normalized_id}") from exc

    if proc.returncode != 0:
        combined = _tail("\n".join([proc.stdout or "", proc.stderr or ""]))
        if "PackageNotFoundError: JLC2KiCadLib" in combined:
            raise RuntimeError(
                "JLC2KiCadLib is not installed in this Python environment. "
                "Install with `pip install -e ./JLC2KiCad_lib-master`."
            )
        raise RuntimeError(f"JLC2KiCadLib generation failed: {combined or 'unknown error'}")

    return proc


def _zip_directory(folder: Path, archive_root_name: str) -> bytes:
    out = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            arcname = (Path(archive_root_name) / path.relative_to(folder)).as_posix()
            zf.write(path, arcname)
            file_count += 1
    if file_count == 0:
        raise RuntimeError("No files were generated in the KiCad library output")
    return out.getvalue()


def _fetch_step_file_easyeda2kicad(lcsc_id: str) -> Tuple[str, bytes]:
    normalized_id = _normalize_lcsc_id(lcsc_id)
    api = EasyedaApi()
    cad_data = api.get_cad_data_of_component(lcsc_id=normalized_id)
    model = Easyeda3dModelImporter(cad_data, download_raw_3d_model=False).create_3d_model()
    if not model:
        raise RuntimeError("3D model metadata not available for this component")

    model_uuid = str(getattr(model, "uuid", "") or "").strip()
    if not model_uuid:
        raise RuntimeError("3D model UUID not available for this component")

    step_bytes = api.get_step_3d_model(model_uuid)
    if not step_bytes:
        raise RuntimeError("STEP file is unavailable for this component")

    model_name = str(getattr(model, "name", "") or "").strip()
    file_name = sanitize_step_filename(model_name, normalized_id)
    return file_name, step_bytes


def _fetch_step_file_jlc2kicad(lcsc_id: str) -> Tuple[str, bytes]:
    normalized_id = _normalize_lcsc_id(lcsc_id)
    with tempfile.TemporaryDirectory(prefix=f"jlc2kicad_{normalized_id}_") as tmp:
        output_dir = Path(tmp) / "out"
        _run_jlc2kicad(
            normalized_id,
            output_dir=output_dir,
            extra_args=["--no_symbol", "--skip_existing", "-models", "STEP"],
            timeout_sec=240,
        )

        step_files = sorted(list(output_dir.glob("**/*.step")) + list(output_dir.glob("**/*.STEP")))
        if not step_files:
            raise RuntimeError("JLC2KiCadLib produced no STEP file")

        best = max(step_files, key=lambda p: p.stat().st_size)
        data = best.read_bytes()
        if not data:
            raise RuntimeError(f"Generated STEP file is empty: {best.name}")
        return sanitize_step_filename(best.stem, normalized_id), data


def fetch_component_library_archive(lcsc_id: str) -> Tuple[str, bytes]:
    normalized_id = _normalize_lcsc_id(lcsc_id)
    model_selection = _parse_model_selection(os.getenv("KICAD_LIBRARY_MODELS", "STEP"))
    symbol_lib = f"LCSC_{normalized_id}"
    footprint_lib = f"LCSC_{normalized_id}_footprint"
    archive_folder = f"{normalized_id}_kicad_library"
    archive_name = sanitize_archive_filename(archive_folder, normalized_id)

    args: List[str] = [
        "-symbol_lib",
        symbol_lib,
        "-footprint_lib",
        footprint_lib,
    ]
    if model_selection:
        args.extend(["-models", *model_selection])
    else:
        args.append("-models")

    with tempfile.TemporaryDirectory(prefix=f"kicad_library_{normalized_id}_") as tmp:
        output_dir = Path(tmp) / "library"
        _run_jlc2kicad(normalized_id, output_dir=output_dir, extra_args=args, timeout_sec=300)
        archive_bytes = _zip_directory(output_dir, archive_folder)
        return archive_name, archive_bytes


def fetch_step_file(lcsc_id: str) -> Tuple[str, bytes]:
    backend_order_raw = (os.getenv("STEP_BACKEND_ORDER", "easyeda2kicad,jlc2kicad") or "").strip()
    backends: dict[str, Callable[[str], Tuple[str, bytes]]] = {
        "easyeda2kicad": _fetch_step_file_easyeda2kicad,
        "jlc2kicad": _fetch_step_file_jlc2kicad,
    }
    names: List[str] = [x.strip().lower() for x in backend_order_raw.split(",") if x.strip()]
    if not names:
        names = ["easyeda2kicad", "jlc2kicad"]

    attempts: List[str] = []
    for name in names:
        fn = backends.get(name)
        if not fn:
            attempts.append(f"{name}: unknown backend")
            continue
        try:
            return fn(lcsc_id)
        except Exception as exc:
            attempts.append(f"{name}: {exc}")

    raise RuntimeError("All STEP backends failed: " + " | ".join(attempts))


def get_lcsc_model(lcsc_id: str) -> Tuple[Optional[str], Optional[bytes]]:
    try:
        return fetch_step_file(lcsc_id)
    except Exception:
        return None, None
