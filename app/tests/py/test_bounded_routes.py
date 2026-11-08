"""@_bounded routes keep their real parameter kinds: a pydantic body stays a body (it was read as a query param,
so POST /api/riff/plan and /api/prerender answered 422 to every valid request)."""
from pydantic import BaseModel

from app.ui import server
from app.ui.services import load_shed


def _bounded_routes():
    for r in server.app.routes:
        fn = getattr(r, "endpoint", None)
        if fn is not None and getattr(fn, "__wrapped__", None) is not None and fn.__module__ == server.__name__:
            yield r


def test_every_bounded_route_keeps_its_body_model_as_the_body():
    seen = 0
    for r in _bounded_routes():
        for p in r.dependant.query_params:
            ann = p.field_info.annotation if hasattr(p, "field_info") else None
            assert not (isinstance(ann, type) and issubclass(ann, BaseModel)), f"{r.path}: body model {p.name} read as a query param"
            assert not isinstance(ann, str), f"{r.path}: {p.name} annotation left unresolved"
        seen += 1
    assert seen >= 5   # riff/plan, prerender, separate, stems, audio, ...


def test_riff_plan_and_prerender_take_a_json_body():
    paths = {r.path: r for r in server.app.routes if getattr(r, "methods", None) and "POST" in r.methods}
    for path in ("/api/riff/plan", "/api/prerender"):
        dep = paths[path].dependant
        assert [p.name for p in dep.body_params] == ["req"], path
        assert dep.query_params == [], path


def test_a_wrapped_handler_resolves_annotations_from_its_own_module():
    class Body(BaseModel):
        x: int

    def handler(req: "Body"):   # a string annotation, like server.py's (from __future__ import annotations)
        return req

    handler.__globals__["Body"] = Body
    try:
        w = load_shed.bounded(handler)
        assert w.__signature__.parameters["req"].annotation is Body
    finally:
        del handler.__globals__["Body"]
