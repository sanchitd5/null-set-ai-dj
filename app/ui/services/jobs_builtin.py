"""The jobs that exist today, registered with the job runner (job_registry.py) at server startup.

    cache_audit          manual: flags library files that are probably not the song they are named after (HF9)
    sync_compact         cron "30 3 * * *": folds old op-log entries away (DJ_SYNC_KEEP_DAYS)
    quarantine_cleanup   cron "15 4 * * *" (server local time); a fire missed while down runs once at startup
    lang_id              external: lang_service keeps its own worker; the page shows status and can pause / resume it
    knowledge_publish    external, observe only: the daily dj-atlas publish timer
    graph_rebuild        manual: rebuilds graph.db from the atlas (run now from /admin/jobs); schedulable in the UI, off by default
    (private jobs)       whatever the builder registers through atlas_bridge.register_jobs
"""
from __future__ import annotations

import time
from typing import Callable

from app.ui.services.job_registry import JobContext, JobSpec, Registry


def _quarantine(ctx: JobContext) -> dict:
    from app.ui.services import quarantine_cleanup
    r = quarantine_cleanup.cleanup()
    if r["audio"] or r["notes"]:
        ctx.log(f"removed {r['audio']} audio, {r['notes']} notes, {r['bytes']} bytes")
    return r


def _cache_audit(ctx: JobContext) -> dict:
    from app.ui import server
    from app.music_brain.config import CACHE_DIR
    from app.ui.services import cache_audit
    return cache_audit.run(dict(server._tracks), dict(server._track_names), CACHE_DIR, time.time(), ctx.log)


def _graph_rebuild(ctx: JobContext) -> dict:
    from app.music_brain import atlas_bridge
    return atlas_bridge.build_graph(None, ctx.log)


def _sync_compact(ctx: JobContext) -> dict:
    from app.ui.services import sync_api
    return sync_api.compact_job(ctx)


def _publish_status() -> dict:
    from app.music_brain.matching import knowledge_publish as kp
    timer = getattr(kp, "_timer", None)
    return {"enabled": kp.autopublish_enabled(), "timer_alive": bool(timer is not None and timer.is_alive())}


def register_builtin(reg: Registry) -> None:
    from app.music_brain import atlas_bridge
    from app.ui.services import lang_service

    reg.register(JobSpec("quarantine_cleanup", "Quarantine cleanup", fn=_quarantine, kind="manual",
                         schedule="15 4 * * *"))
    reg.register(JobSpec("cache_audit", "Song cache audit (flags suspect files, deletes nothing)", fn=_cache_audit,
                         kind="manual"))
    reg.register(JobSpec("sync_compact", "Op-log compaction", fn=_sync_compact, kind="manual",
                         schedule="30 3 * * *"))
    reg.register(JobSpec("lang_id", "Language id backfill", kind="external", status_fn=lang_service.status,
                         pause_fn=lambda: lang_service.set_paused(True), resume_fn=lambda: lang_service.set_paused(False)))
    reg.register(JobSpec("knowledge_publish", "Knowledge publish timer", kind="external", status_fn=_publish_status))
    if atlas_bridge.AVAILABLE:
        reg.register(JobSpec("graph_rebuild", "Graph rebuild", fn=_graph_rebuild, kind="manual"))
    atlas_bridge.register_jobs(reg.register)
