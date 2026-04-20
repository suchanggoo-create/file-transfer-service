from __future__ import annotations

import asyncio
import queue
import tarfile
import threading
import traceback
from pathlib import Path
from typing import AsyncIterator, Optional


class _QueueWriter:
    def __init__(self, q: "queue.Queue[bytes | None]"):
        self._q = q

    def write(self, b: bytes) -> int:
        if not b:
            return 0
        self._q.put(bytes(b))
        return len(b)

    def flush(self) -> None:
        return None


async def tar_directory_stream(dir_path: Path, arcname: Optional[str] = None) -> AsyncIterator[bytes]:
    """
    Stream a tar archive of dir_path without creating a temp file.
    """
    dir_path = dir_path.resolve()
    if not dir_path.is_dir():
        raise ValueError("dir_path must be a directory")

    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)

    def producer() -> None:
        try:
            writer = _QueueWriter(q)
            with tarfile.open(fileobj=writer, mode="w|") as tf:
                tf.add(dir_path, arcname=arcname or dir_path.name, recursive=True)
        except Exception:
            traceback.print_exc()
        finally:
            q.put(None)

    threading.Thread(target=producer, daemon=True).start()

    while True:
        chunk = await asyncio.to_thread(q.get)
        if chunk is None:
            break
        yield chunk

