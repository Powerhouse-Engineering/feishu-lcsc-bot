import re
from typing import Optional, Tuple

from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from easyeda2kicad.easyeda.easyeda_importer import Easyeda3dModelImporter


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


def fetch_step_file(lcsc_id: str) -> Tuple[str, bytes]:
    api = EasyedaApi()
    cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
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
    file_name = sanitize_step_filename(model_name, lcsc_id)
    return file_name, step_bytes


def get_lcsc_model(lcsc_id: str) -> Tuple[Optional[str], Optional[bytes]]:
    try:
        return fetch_step_file(lcsc_id)
    except Exception:
        return None, None
