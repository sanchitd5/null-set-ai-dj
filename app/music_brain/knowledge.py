"""Compat shim, remove after 1.1: this module moved to app.music_brain.matching.knowledge."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.matching.knowledge", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.matching.knowledge import *  # noqa: F401,F403
    from app.music_brain.matching.knowledge import (  # noqa: F401
        _SHARD_ID, _ABS, _EMAIL, _SEEN, _cache, _read, _dump, _write_bytes, _gz_json, _export_atlas,
        _Resolver, _stamp,
    )
