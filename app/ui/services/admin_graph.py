"""Admin graph view: /admin/graph and the read-only JSON API under /api/admin/graph (G11).

    GET /admin/graph                                   the page (static/admin-graph.js draws it with cytoscape)
    GET /api/admin/graph/stats                         {rev, seq, built_at, nodes, edges, node_kinds, edge_kinds, langs}
    GET /api/admin/graph/node?q=<name or yt id>        {results: [node]}   (q empty: 422)
    GET /api/admin/graph/node?id=<n>                   {node, edges: {kind: {out, in, top}}}
    GET /api/admin/graph/neighbours?id=&k=&kinds=a,b&min_weight=&lang=      {center, nodes, edges}

graph.db is read through atlas_bridge (graph_store in the private dj-atlas); nothing of it is in this repo. Without a
graph.db (or an atlas) every answer is empty: stats {"available": false}, search [], neighbours 404. All routes are
admin-gated like the rest of /admin; the page script comes from the same origin (CSP script-src 'self').
"""
from __future__ import annotations

from fastapi import Depends
from fastapi.responses import JSONResponse

from app.music_brain import atlas_bridge

_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'")
_HEAD = '<script src="/vendor/cytoscape.min.js" defer></script><script src="/admin-graph.js" defer></script>'

_BODY = """
<div id="gstats" class="muted">loading</div>
<form id="gsearch" class="gbar"><input id="gq" type="search" placeholder="artist, title or YouTube id" autocomplete="off"
  aria-label="search">
<button type="submit">Search</button></form>
<div id="gresults"></div>
<details class="gbar"><summary>Filters</summary>
 <div id="gkinds"></div>
 <label>Min weight <input id="gminw" type="number" min="0" max="1" step="0.05" value="0"></label>
 <label>Language <select id="glang"><option value="">any</option></select></label>
 <label>Neighbours per kind <input id="gk" type="number" min="1" max="50" value="12"></label>
</details>
<div id="glegend"></div>
<div id="gwrap"><div id="gcy" role="img" aria-label="song graph"></div>
<aside id="gpanel" hidden></aside></div>
<p class="muted">Tap a song to open its details and load its neighbours. Pinch or scroll to zoom, drag to pan.</p>
"""

_EXTRA_CSS = """<style>
.gbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}.gbar input[type=search]{flex:1;min-width:0}
#gwrap{position:relative}#gcy{height:60vh;min-height:320px;background:#0b0b0b;border:1px solid #333;border-radius:8px;touch-action:none}
#gpanel{background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;margin-top:8px}
#gresults button{display:block;width:100%;text-align:left;margin:4px 0}
#gkinds label,#glegend span{display:inline-flex;align-items:center;gap:6px;min-height:44px;margin-right:12px}
#glegend i{display:inline-block;width:22px;height:5px;border-radius:2px}
@media (min-width:900px){#gwrap{display:flex;gap:12px}#gcy{flex:1}#gpanel{width:320px;margin-top:0;max-height:60vh;overflow:auto}}
</style>"""


def _unavailable():
    return JSONResponse({"detail": "no graph", "code": "no_graph"}, status_code=404)


def register(router, require_admin, page) -> None:
    @router.get("/admin/graph")
    def graph_page(email: str = Depends(require_admin)):
        return page("Graph", _BODY, email, refresh=0, head=_EXTRA_CSS + _HEAD, csp=_CSP)

    @router.get("/api/admin/graph/stats")
    def graph_stats(email: str = Depends(require_admin)):
        st = atlas_bridge.graph_view("stats")
        return st if st is not None else {"available": False}

    @router.get("/api/admin/graph/node")
    def graph_node(q: str = "", id: int = 0, email: str = Depends(require_admin)):
        if id:
            d = atlas_bridge.graph_view("node_detail", id)
            return d if d is not None else _unavailable()
        if not q.strip():
            return JSONResponse({"detail": "q or id required", "code": "bad_query"}, status_code=422)
        return {"results": atlas_bridge.graph_view("find", q) or []}

    @router.get("/api/admin/graph/neighbours")
    def graph_neighbours(id: int, k: int = 12, kinds: str = "", min_weight: float = 0.0, lang: str = "",
                         email: str = Depends(require_admin)):
        wanted = [x for x in kinds.split(",") if x.strip()]
        out = atlas_bridge.graph_view("neighbours", id, k, [w.strip() for w in wanted], min_weight, lang)
        return out if out is not None else _unavailable()
