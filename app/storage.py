from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Optional, Tuple


_PATH_SEP_RE = re.compile(r"[\\/]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def storage_root() -> Path:
    root = os.environ.get("STORAGE_ROOT") or str(Path.cwd() / "data")
    p = Path(root).expanduser().resolve()
    ensure_dir(p)
    ensure_dir(p / ".uploads")
    return p


def _split_relative_path(path: str) -> list[str]:
    path = (path or "").strip()
    if not path:
        return []
    if "\x00" in path:
        raise ValueError("invalid path")
    parts = [p for p in _PATH_SEP_RE.split(path) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path traversal is not allowed")
    return parts


def resolve_under_root(root: Path, rel: str) -> Path:
    parts = _split_relative_path(rel)
    p = root
    for part in parts:
        p = p / part
    p = p.resolve()
    # make sure resolved path stays under root
    if root != p and root not in p.parents:
        raise ValueError("path is outside storage root")
    return p


def new_upload_id() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class UploadMeta:
    upload_id: str
    rel_path: str
    total_size: int
    created_at: str

    @property
    def part_path(self) -> Path:
        root = storage_root()
        return (root / ".uploads" / f"{self.upload_id}.part").resolve()

    @property
    def meta_path(self) -> Path:
        root = storage_root()
        return (root / ".uploads" / f"{self.upload_id}.json").resolve()

    @property
    def final_path(self) -> Path:
        root = storage_root()
        return resolve_under_root(root, self.rel_path)


def write_meta(meta: UploadMeta) -> None:
    import json

    ensure_dir(storage_root() / ".uploads")
    meta.meta_path.write_text(
        json.dumps(
            {
                "upload_id": meta.upload_id,
                "rel_path": meta.rel_path,
                "total_size": meta.total_size,
                "created_at": meta.created_at,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def read_meta(upload_id: str) -> UploadMeta:
    import json

    root = storage_root()
    meta_path = (root / ".uploads" / f"{upload_id}.json").resolve()
    if not meta_path.exists():
        raise FileNotFoundError("upload not found")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    return UploadMeta(
        upload_id=str(data["upload_id"]),
        rel_path=str(data["rel_path"]),
        total_size=int(data["total_size"]),
        created_at=str(data.get("created_at") or ""),
    )


def current_received_bytes(meta: UploadMeta) -> int:
    try:
        return meta.part_path.stat().st_size
    except FileNotFoundError:
        return 0


def open_part_for_append(meta: UploadMeta) -> BinaryIO:
    ensure_dir(meta.part_path.parent)
    # open in read/write so we can seek, but we enforce sequential offset in API
    return open(meta.part_path, "a+b", buffering=0)


def atomic_move(src: Path, dst: Path) -> None:
    ensure_dir(dst.parent)
    src.replace(dst)


def complete_upload(meta: UploadMeta) -> None:
    received = current_received_bytes(meta)
    if received != meta.total_size:
        raise ValueError(f"upload incomplete: received={received}, total={meta.total_size}")
    atomic_move(meta.part_path, meta.final_path)
    # best-effort cleanup meta
    try:
        meta.meta_path.unlink(missing_ok=True)  # py3.8+ on mac supports missing_ok
    except TypeError:
        if meta.meta_path.exists():
            meta.meta_path.unlink()


def human_safe_name(p: Path) -> str:
    try:
        return str(p.relative_to(storage_root()))
    except Exception:
        return str(p)


def parse_range_header(range_header: Optional[str], file_size: int) -> Optional[Tuple[int, int]]:
    """
    Returns (start, end_inclusive) or None if header missing.
    Only supports a single bytes range.
    """
    if not range_header:
        return None
    m = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None

    if a == "":
        # suffix bytes: last b bytes
        suffix = int(b)
        if suffix <= 0:
            return None
        start = max(0, file_size - suffix)
        end = file_size - 1
        return (start, end)

    start = int(a)
    end = int(b) if b != "" else file_size - 1
    if start < 0 or end < start:
        return None
    if start >= file_size:
        return None
    end = min(end, file_size - 1)
    return (start, end)

