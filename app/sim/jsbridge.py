"""Python client of bridge.js: one persistent node process, JSON lines in and out.

    with JsBridge() as js:
        js.call("autopilot.energyStepOk", 5, 6, {"songs": 3})

Every call is synchronous and deterministic (the node side is pure). A node crash or an
answer of ok=false raises JsError with the node stack, never a silent default.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

BRIDGE_JS = Path(__file__).with_name("bridge.js")


class JsError(RuntimeError):
    pass


class JsBridge:
    def __init__(self, node: Optional[str] = None):
        exe = node or shutil.which("node")
        if not exe:
            raise JsError("node is required for the virtual set (the console logic is JavaScript)")
        self._proc = subprocess.Popen([exe, str(BRIDGE_JS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1)
        self._n = 0
        self.calls = 0

    def call(self, fn: str, *args: Any) -> Any:
        if self._proc.poll() is not None:
            raise JsError(f"node bridge exited: {self._proc.stderr.read()[-800:]}")
        self._n += 1
        self.calls += 1
        self._proc.stdin.write(json.dumps({"id": self._n, "fn": fn, "args": list(args)}, allow_nan=False) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise JsError(f"node bridge closed: {self._proc.stderr.read()[-800:]}")
        res = json.loads(line)
        if not res.get("ok"):
            raise JsError(f"{fn}: {res.get('error')}")
        return res["result"]

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    def __enter__(self) -> "JsBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
