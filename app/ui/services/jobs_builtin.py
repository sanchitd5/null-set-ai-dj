"""The jobs that exist today, registered with the job runner (job_registry.py) at server startup.

    sync_compact         periodic, 24 h: folds old op-log entries away (DJ_SYNC_KEEP_DAYS)
    quarantine_cleanup   periodic, 24 h, also at start (it replaced its own thread)
    lang_id              external: lang_service keeps its own worker; the page shows status and can pause / resume it
    knowledge_publish    external, observe only: the daily dj-atlas publish timer
    graph_rebuild        manual: rebuilds graph.db from the atlas (run now from /admin/jobs); never automatic
    (private jobs)       whatever the builder registers through atlas_bridge.register_jobs
"""
from __future__ import annotations

from typing import Callable

from app.ui.services.job_registry import JobContext, JobSpec, Registry


def _quarantine(ctx: JobContext) -> dict:
    from app.ui.services import quarantine_cleanup
    r = quarantine_cleanup.cleanup()
    if r["audio"] or r["notes"]:
        ctx.log(f"removed {r['audio']} audio, {r['notes']} notes, {r['bytes']} bytes")
    return r


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
    from app.ui.services import lang_service, quarantine_cleanup

    reg.register(JobSpec("quarantine_cleanup", "Quarantine cleanup", fn=_quarantine, kind="periodic",
                         interval_s=quarantine_cleanup.INTERVAL_S, run_at_start=True))
    reg.register(JobSpec("sync_compact", "Op-log compaction", fn=_sync_compact, kind="periodic", interval_s=86400))
    reg.register(JobSpec("lang_id", "Language id backfill", kind="external", status_fn=lang_service.status,
                         pause_fn=lambda: lang_service.set_paused(True), resume_fn=lambda: lang_service.set_paused(False)))
    reg.register(JobSpec("knowledge_publish", "Knowledge publish timer", kind="external", status_fn=_publish_status))
    if atlas_bridge.AVAILABLE:
        reg.register(JobSpec("graph_rebuild", "Graph rebuild", fn=_graph_rebuild, kind="manual"))
    atlas_bridge.register_jobs(reg.register)
