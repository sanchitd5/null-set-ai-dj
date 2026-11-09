"""Who a sync request is, from the auth layer only (never the request body)."""
from __future__ import annotations

import re
from typing import Set

OWNER = "user:owner"
_SUB = re.compile(r"[A-Za-z0-9_.:@-]{1,120}")


def scope_for(request) -> str:
    """'user:<google sub>' for a Google browser session or device token, else 'user:owner' (password, CLI token,
    local machine)."""
    auth = request.scope.get("dj_auth") or {}
    sub = auth.get("sub")
    return f"user:{sub}" if isinstance(sub, str) and _SUB.fullmatch(sub) else OWNER


def visible_scopes(request) -> Set[str]:
    """The scopes this caller may read: everyone's `global` plus its own user scope."""
    return {"global", scope_for(request)}
