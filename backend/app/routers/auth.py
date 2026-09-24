"""
Auth routes — Firebase token verify + bootstrap.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.routers.constituencies import _load_manifest
from app.middleware.firebase_auth import user_may_access

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/bootstrap")
def bootstrap(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Called by the frontend right after Firebase sign-in.
    Returns the user's profile and the constituencies they may access.
    """
    all_items = _load_manifest()
    allowed = [c for c in all_items if user_may_access(user, c.get("slug", ""))]
    return {
        "uid": user.get("uid"),
        "email": user.get("email"),
        "role": user.get("role"),
        "constituency_slugs": user.get("constituency_slugs"),
        "constituencies": allowed,
    }


@router.get("/me")
def me(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return bootstrap(user)
