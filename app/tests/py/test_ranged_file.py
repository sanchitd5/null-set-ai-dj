"""RangedFileResponse (app/ui/services/ranged_file.py): Range/206 independent of the starlette version."""
import pytest
from fastapi import FastAPI

from app.tests.py.testclient_compat import TestClient
from app.ui.services.ranged_file import RangedFileResponse, parse_range


@pytest.mark.parametrize("header,size,want", [
    ("bytes=0-", 10, (0, 9)), ("bytes=3-5", 10, (3, 5)), ("bytes=3-99", 10, (3, 9)),
    ("bytes=-4", 10, (6, 9)), ("bytes=-99", 10, (0, 9)),
    ("bytes=10-", 10, (-1, -1)), ("bytes=-0", 10, (-1, -1)),
    ("bytes=5-2", 10, None), ("bytes=0-1,4-5", 10, None), ("items=0-1", 10, None), ("bytes=-", 10, None), ("", 10, None),
])
def test_parse_range(header, size, want):
    assert parse_range(header, size) == want


def test_responses(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(bytes(range(200)) * 1000)   # 200000 bytes: spans several 64 KiB chunks
    whole = f.read_bytes()
    app = FastAPI()

    @app.get("/f")
    def get_f():
        return RangedFileResponse(f, media_type="application/octet-stream")

    c = TestClient(app, auth=False)
    r = c.get("/f")
    assert r.status_code == 200 and r.content == whole and r.headers["accept-ranges"] == "bytes"
    r = c.get("/f", headers={"Range": "bytes=70000-"})
    assert r.status_code == 206 and r.content == whole[70000:]
    assert r.headers["content-range"] == f"bytes 70000-{len(whole) - 1}/{len(whole)}"
    assert r.headers["content-length"] == str(len(whole) - 70000)
    r = c.get("/f", headers={"Range": "bytes=5-9"})
    assert r.status_code == 206 and r.content == whole[5:10]
    r = c.get("/f", headers={"Range": "bytes=-3"})
    assert r.status_code == 206 and r.content == whole[-3:]
    r = c.get("/f", headers={"Range": f"bytes={len(whole)}-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(whole)}"
    r = c.get("/f", headers={"Range": "bytes=0-1,4-5"})   # several ranges: whole file, 200
    assert r.status_code == 200 and r.content == whole
