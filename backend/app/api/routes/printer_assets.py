"""Public, non-secret display assets shipped inside a validated plugin package."""

from importlib import import_module
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.app.printers.registry import registry

router = APIRouter(prefix="/printer-plugins", tags=["printer-plugins"])
_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}


@router.get("/{plugin_id}/assets/{asset_path:path}")
async def plugin_asset(plugin_id: str, asset_path: str):
    registry.get(plugin_id)
    namespace = registry.loaded_sources[plugin_id]["module_namespace"]
    module = import_module(namespace)
    if not module.__file__:
        raise HTTPException(404, "Plugin asset not found")
    root = (Path(module.__file__).parent / "assets").resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() not in _IMAGE_TYPES or not target.is_file():
        raise HTTPException(404, "Plugin asset not found")
    return FileResponse(target, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
