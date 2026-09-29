"""Run the console headless against the real API, in this process.

    with serve(world) as port:                 # the FastAPI app on a private port (never the user's)
        result = run_console(port, cfg)         # node app/sim/js/run-set.js: the console's own scripts

`serve` mounts nothing sim-specific except one thing: a request header (x-sim-time) that the
console's virtual clock stamps on every call, which the sim world uses as "now" for the events it
logs. Everything else is the production app, with the world's edges installed (world.py).
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from app.sim.pool import REPO_ROOT

RUN_SET_JS = Path(__file__).with_name("js") / "run-set.js"


@contextlib.contextmanager
def serve(world):
    import uvicorn

    from app.ui import server

    @server.app.middleware("http")
    async def _sim_time(request, call_next):           # the console's virtual clock, as the sim world's clock
        t = request.headers.get("x-sim-time")
        if t is not None:
            try:
                world.set_time(float(t))
            except ValueError:
                pass
        world.begin_request()
        response = await call_next(request)
        lat = world.end_request()
        if lat is not None:                            # the model's own latency (recorded / measured), not a table figure
            response.headers["x-sim-latency"] = f"{lat:.3f}"
        return response

    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", access_log=False)
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, name="sim-api", daemon=True)
    thread.start()
    t0 = time.monotonic()
    while not srv.started:
        if not thread.is_alive() or time.monotonic() - t0 > 60:
            raise RuntimeError("the API server did not start")
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def run_console(port: int, cfg: dict, timeout_s: float = 1800.0) -> dict:
    """Spawn the node harness; return what it recorded (console log, events, net log, errors)."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is required: the console is JavaScript")
    with tempfile.TemporaryDirectory(prefix="sim-js-") as d:
        out = Path(d) / "out.json"
        cfg = {**cfg, "port": port, "out": str(out)}
        cfg_path = Path(d) / "cfg.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        p = subprocess.run([node, str(RUN_SET_JS), str(cfg_path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_s)
        if p.returncode != 0 or not out.exists():
            raise RuntimeError(f"headless console failed (rc {p.returncode}): {p.stderr.strip()[-1200:]}")
        return json.loads(out.read_text(encoding="utf-8"))
