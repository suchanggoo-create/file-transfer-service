from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .storage import (
    UploadMeta,
    complete_upload,
    current_received_bytes,
    ensure_dir,
    new_upload_id,
    open_part_for_append,
    parse_range_header,
    read_meta,
    resolve_under_root,
    storage_root,
    utc_now_iso,
    write_meta,
)
from .tar_stream import tar_directory_stream


app = FastAPI(title="Large File Transfer Service", version="0.1.0")


# In-process locks to avoid interleaved writes for same upload_id
_upload_locks: Dict[str, Any] = {}


class CreateUploadRequest(BaseModel):
    path: str = Field(..., description="Relative path under storage root, e.g. dir/a.bin")
    total_size: int = Field(..., ge=0, description="Total bytes of the file")
    overwrite: bool = Field(True, description="Overwrite if final file exists")


class CreateUploadResponse(BaseModel):
    upload_id: str
    path: str
    total_size: int
    received: int


class UploadStatusResponse(BaseModel):
    upload_id: str
    path: str
    total_size: int
    received: int
    created_at: str


class CompleteUploadResponse(BaseModel):
    upload_id: str
    path: str
    total_size: int


class BrowseEntry(BaseModel):
    name: str
    path: str
    type: str  # "file" | "dir"
    size: Optional[int] = None
    mtime: Optional[float] = None


class BrowseResponse(BaseModel):
    path: str
    entries: List[BrowseEntry]


def _get_lock(upload_id: str):
    import asyncio

    lock = _upload_locks.get(upload_id)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[upload_id] = lock
    return lock


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/uploads", response_model=CreateUploadResponse)
def create_upload(req: CreateUploadRequest) -> CreateUploadResponse:
    root = storage_root()
    final_path = resolve_under_root(root, req.path)
    if final_path.exists() and not req.overwrite:
        raise HTTPException(status_code=409, detail="file already exists")
    if final_path.exists() and final_path.is_dir():
        raise HTTPException(status_code=409, detail="a directory exists at the target path")

    upload_id = new_upload_id()
    meta = UploadMeta(
        upload_id=upload_id,
        rel_path=req.path,
        total_size=req.total_size,
        created_at=utc_now_iso(),
    )
    write_meta(meta)
    return CreateUploadResponse(
        upload_id=upload_id,
        path=req.path,
        total_size=req.total_size,
        received=current_received_bytes(meta),
    )


@app.get("/api/uploads/{upload_id}", response_model=UploadStatusResponse)
def upload_status(upload_id: str) -> UploadStatusResponse:
    try:
        meta = read_meta(upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="upload not found")
    received = current_received_bytes(meta)
    return UploadStatusResponse(
        upload_id=meta.upload_id,
        path=meta.rel_path,
        total_size=meta.total_size,
        received=received,
        created_at=meta.created_at,
    )


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int) -> JSONResponse:
    try:
        meta = read_meta(upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="upload not found")

    lock = _get_lock(upload_id)
    async with lock:
        received = current_received_bytes(meta)
        if offset != received:
            raise HTTPException(
                status_code=409,
                detail={"message": "offset mismatch", "expected_offset": received, "your_offset": offset},
            )

        try:
            f = open_part_for_append(meta)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to open part file: {e}")

        wrote = 0
        try:
            f.seek(offset)
            async for chunk in request.stream():
                if not chunk:
                    continue
                f.write(chunk)
                wrote += len(chunk)
                if received + wrote > meta.total_size:
                    raise HTTPException(status_code=400, detail="received bytes exceed total_size")
        finally:
            f.close()

        new_received = received + wrote
        return JSONResponse({"upload_id": upload_id, "received": new_received, "total_size": meta.total_size})


@app.post("/api/uploads/{upload_id}/complete", response_model=CompleteUploadResponse)
def complete(upload_id: str) -> CompleteUploadResponse:
    try:
        meta = read_meta(upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="upload not found")

    # Ensure parent dir exists before move
    ensure_dir(meta.final_path.parent)
    try:
        complete_upload(meta)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"complete failed: {e}")

    return CompleteUploadResponse(upload_id=meta.upload_id, path=meta.rel_path, total_size=meta.total_size)


@app.get("/api/browse", response_model=BrowseResponse)
def browse(path: str = "") -> BrowseResponse:
    root = storage_root()
    target = resolve_under_root(root, path) if path else root
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if target.is_file():
        raise HTTPException(status_code=400, detail="path is a file; browse expects a directory")

    entries: List[BrowseEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name == ".uploads":
            continue
        st = child.stat()
        rel = str(child.relative_to(root))
        if child.is_dir():
            entries.append(
                BrowseEntry(name=child.name, path=rel, type="dir", size=None, mtime=st.st_mtime)
            )
        else:
            entries.append(
                BrowseEntry(name=child.name, path=rel, type="file", size=st.st_size, mtime=st.st_mtime)
            )

    return BrowseResponse(path=path, entries=entries)


async def _file_stream(path: Path, start: int, end_inclusive: int, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    import anyio

    async with await anyio.open_file(path, "rb") as f:
        await f.seek(start)
        remaining = end_inclusive - start + 1
        while remaining > 0:
            n = min(chunk_size, remaining)
            data = await f.read(n)
            if not data:
                break
            remaining -= len(data)
            yield data


@app.get("/api/download/file")
async def download_file(path: str, range_header: Optional[str] = Header(default=None, alias="Range")) -> Response:
    root = storage_root()
    target = resolve_under_root(root, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    size = target.stat().st_size
    r = parse_range_header(range_header, size)
    content_type, _ = mimetypes.guess_type(target.name)
    content_type = content_type or "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{target.name}"',
    }

    if r is None:
        return StreamingResponse(_file_stream(target, 0, size - 1), media_type=content_type, headers=headers)

    start, end = r
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        _file_stream(target, start, end),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


@app.get("/api/download/dir")
async def download_dir(path: str) -> Response:
    root = storage_root()
    target = resolve_under_root(root, path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    filename = f"{target.name}.tar"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        tar_directory_stream(target, arcname=target.name),
        media_type="application/x-tar",
        headers=headers,
    )

