"""TestClient that works with the pinned starlette 0.27 and httpx >= 0.28.

starlette 0.27's TestClient.__init__ calls httpx.Client.__init__(app=...); httpx
0.28 removed that argument ("Client.__init__() got an unexpected keyword argument
'app'"). The app is already served by starlette's own sync transport, so the
argument is redundant: _DropApp sits between the two classes in the MRO and
drops it. With a starlette that no longer passes `app`, it does nothing.

Tests import TestClient from here instead of fastapi.testclient.
"""
import httpx
from starlette.testclient import TestClient as _StarletteTestClient


class _DropApp(httpx.Client):
    def __init__(self, *args, app=None, **kwargs):  # noqa: ARG002 - app is served by the transport
        super().__init__(*args, **kwargs)


class TestClient(_StarletteTestClient, _DropApp):
    """starlette.testclient.TestClient; MRO: TestClient -> starlette's -> _DropApp -> httpx.Client."""
    __test__ = False
