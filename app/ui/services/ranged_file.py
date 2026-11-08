"""FileResponse that answers a single-range `Range: bytes=...` request with 206.

starlette < 0.39 (what requirements-ci.txt pins: fastapi 0.103.2 -> starlette 0.27) ignores Range
and always sends 200 with the whole file; newer starlette does it itself. The phone resumes
interrupted audio downloads with Range, so the server must not depend on the starlette version.
Anything this does not understand (several ranges, a malformed header) is served as the whole
file with 200, which RFC 9110 allows; a range past the end is 416.
"""
from __future__ import annotations

import os
import re
import stat as stat_mod
from typing import Optional, Tuple

import anyio
from starlette.responses import FileResponse, Response

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """(first, last) inclusive for a single satisfiable range; None if absent/unsupported;
    (-1, -1) when the single range is syntactically valid but cannot be satisfied."""
    m = _RANGE.match(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":                       # suffix: the last N bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            return (-1, -1)
        return (max(size - n, 0), size - 1)
    first = int(m.group(1))
    if m.group(2) != "" and int(m.group(2)) < first:    # last < first: invalid, ignore the header
        return None
    if first >= size:
        return (-1, -1)
    last = size - 1 if m.group(2) == "" else min(int(m.group(2)), size - 1)
    return (first, last)


class RangedFileResponse(FileResponse):
    async def __call__(self, scope, receive, send) -> None:
        self.headers.setdefault("accept-ranges", "bytes")
        raw = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"range"), "")
        if not raw or self.send_header_only:
            return await super().__call__(scope, receive, send)
        try:
            st = self.stat_result or await anyio.to_thread.run_sync(os.stat, self.path)
        except FileNotFoundError:
            raise RuntimeError(f"File at path {self.path} does not exist.")
        if not stat_mod.S_ISREG(st.st_mode):
            raise RuntimeError(f"File at path {self.path} is not a file.")
        rng = parse_range(raw, st.st_size)
        if rng is None:
            return await super().__call__(scope, receive, send)
        if rng == (-1, -1):
            return await Response(status_code=416, headers={"content-range": f"bytes */{st.st_size}"})(
                scope, receive, send)
        first, last = rng
        self.set_stat_headers(st)
        self.headers["content-length"] = str(last - first + 1)
        self.headers["content-range"] = f"bytes {first}-{last}/{st.st_size}"
        await send({"type": "http.response.start", "status": 206, "headers": self.raw_headers})
        left = last - first + 1
        async with await anyio.open_file(self.path, mode="rb") as f:
            await f.seek(first)
            while left > 0:
                chunk = await f.read(min(self.chunk_size, left))
                if not chunk:
                    break
                left -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": left > 0})
        if left > 0:   # file shrank under us: close the body
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        if self.background is not None:
            await self.background()
