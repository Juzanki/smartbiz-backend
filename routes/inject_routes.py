# backend/routes/injector_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, Optional, List, Literal
from pathlib import Path
from threading import Lock
import tempfile
import json
import os
import time

router = APIRouter(prefix="/injector", tags=["SmartInjectGPT"])

# ---------- Paths & Globals ----------
BASE_DIR = Path(__file__).resolve().parents[1]  # go up from routes/ -> backend/
FILE_MAP_PATH = BASE_DIR / "SmartInjectGPT" / "scripts" / "file_map.json"

_file_map_lock = Lock()
_file_map: Dict[str, str] = {}  # tag -> relative_or_absolute_path


# ---------- Utilities ----------
def _load_file_map() -> Dict[str, str]:
    """
    Load file_map.json using UTF-8 (with BOM tolerance).
    """
    if not FILE_MAP_PATH.exists():
        raise RuntimeError(f"file_map.json not found at {FILE_MAP_PATH}")
    with FILE_MAP_PATH.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("file_map.json must be a JSON object of {tag: path}")
    # ensure all values are strings
    for k, v in list(data.items()):
        if not isinstance(v, str):
            raise RuntimeError(f"file_map.json tag '{k}' has non-string path")
    return data


def _ensure_loaded():
    global _file_map
    if not _file_map:
        with _file_map_lock:
            if not _file_map:
                _file_map = _load_file_map()


def _resolve_safe_path(mapped_path: str) -> Path:
    """
    Resolve a target path safely under BASE_DIR (prevents path-escape).
    Accepts relative or absolute paths in file_map.json. Absolutes must
    still reside under BASE_DIR after resolution.
    """
    p = Path(mapped_path)
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    else:
        p = p.resolve()

    try:
        p.relative_to(BASE_DIR)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path. Access outside base directory is not allowed.",
        )
    return p


def _atomic_write_text(target: Path, content: str, encoding: str = "utf-8") -> int:
    """
    Atomically write text to `target` using a temp file and replace.
    Returns the number of bytes written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode(encoding)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target.parent)) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, str(target))  # atomic on same filesystem
    return len(data)


def _append_text(target: Path, content: str, encoding: str = "utf-8") -> int:
    """
    Append text to `target` (creates if missing). Not atomic for the whole file,
    but uses fsync to reduce loss risk.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode(encoding)
    with open(target, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return len(data)


def _replace_between_markers(
    text: str,
    new_block: str,
    start_marker: str,
    end_marker: str,
) -> str:
    """
    Replace content between start_marker and end_marker (inclusive of markers).
    If markers are missing, they are inserted with the new block appended.
    """
    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker, start_idx + len(start_marker)) if start_idx != -1 else -1

    if start_idx != -1 and end_idx != -1:
        before = text[: start_idx + len(start_marker)]
        after = text[end_idx:]
        # Keep markers, place block between them with surrounding newlines for clarity
        middle = f"\n{new_block}\n"
        return f"{before}{middle}{after}"
    else:
        # Append markers + block to the end
        suffix = "\n" if not text.endswith("\n") else ""
        block = f"{suffix}{start_marker}\n{new_block}\n{end_marker}\n"
        return text + block


# ---------- Schemas ----------
WriteMode = Literal["overwrite", "append", "markers"]

class InjectRequest(BaseModel):
    tag: str = Field(..., description="Key in file_map.json")
    response: str = Field(..., description="Content to write")
    mode: WriteMode = Field("overwrite", description="How to inject content")
    create_backup: bool = Field(True, description="Create a .bak timestamped copy before modifying the file")
    encoding: str = Field("utf-8", description="Text encoding for writes")
    start_marker: str = Field("# <inject:start>", description="Start marker text (markers mode only)")
    end_marker: str = Field("# <inject:end>", description="End marker text (markers mode only)")
    max_kb: int = Field(2048, ge=1, le=1024 * 64, description="Reject payloads larger than this many KB to avoid accidents")
    model_config = ConfigDict(extra="forbid")


class InjectResponse(BaseModel):
    message: str
    file: str
    bytes_written: int
    mode: WriteMode
    backup_file: Optional[str] = None


class TagInfo(BaseModel):
    tag: str
    path: str


# ---------- Startup: preload map (optional) ----------
try:
    _ensure_loaded()
except Exception as e:
    # Defer failure to runtime endpoints so app can still boot if desired.
    # You can log this instead of raising.
    pass


# ---------- Endpoints ----------
@router.get("/map", response_model=List[TagInfo], summary="List all available tags")
def list_tags():
    """
    Returns the list of tag -> path mappings from file_map.json.
    """
    _ensure_loaded()
    return [TagInfo(tag=k, path=v) for k, v in sorted(_file_map.items())]


@router.post("/reload", response_model=List[TagInfo], summary="Reload file_map.json from disk")
def reload_map():
    """
    Reloads file_map.json and returns the new mapping.
    """
    global _file_map
    with _file_map_lock:
        _file_map = _load_file_map()
    return [TagInfo(tag=k, path=v) for k, v in sorted(_file_map.items())]


@router.post(
    "/inject",
    response_model=InjectResponse,
    summary="Inject response text into a mapped file",
)
def inject_code(item: InjectRequest):
    """
    Inject the provided text into the file mapped by `tag`, using one of:
    - overwrite: replace the entire file content atomically
    - append: append content to the end of the file
    - markers: replace content between `start_marker` and `end_marker`,
      inserting markers if not present
    """
    _ensure_loaded()

    tag = item.tag.strip()
    if tag not in _file_map:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid tag: {tag}")

    # Size guardrail
    size_bytes = len(item.response.encode(item.encoding, errors="strict"))
    if size_bytes > item.max_kb * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload too large: {size_bytes} bytes (> {item.max_kb} KB)",
        )

    # Resolve and validate the target path
    target_path = _resolve_safe_path(_file_map[tag])

    # Optional backup
    backup_path: Optional[Path] = None
    if item.create_backup and target_path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = target_path.with_suffix(target_path.suffix + f".bak.{ts}")
        try:
            # Copy contents to backup atomically-ish
            content = target_path.read_bytes()
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target_path.parent)) as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_name = tmp.name
            os.replace(tmp_name, str(backup_path))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create backup: {e}",
            )

    # Perform the write
    try:
        if item.mode == "overwrite":
            written = _atomic_write_text(target_path, item.response, encoding=item.encoding)
        elif item.mode == "append":
            written = _append_text(target_path, item.response, encoding=item.encoding)
        else:  # markers
            existing = target_path.read_text(item.encoding) if target_path.exists() else ""
            updated = _replace_between_markers(
                text=existing,
                new_block=item.response,
                start_marker=item.start_marker,
                end_marker=item.end_marker,
            )
            written = _atomic_write_text(target_path, updated, encoding=item.encoding)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write file: {e}",
        )

    return InjectResponse(
        message=f"Code injected successfully into tag '{tag}'",
        file=str(target_path),
        bytes_written=written,
        mode=item.mode,
        backup_file=str(backup_path) if backup_path else None,
    )
