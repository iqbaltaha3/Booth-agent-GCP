"""
Shared FastAPI dependencies.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import Depends, HTTPException, status

from app.middleware.firebase_auth import user_may_access, verify_token


async def get_current_user(
    user: Dict[str, Any] = Depends(verify_token),
) -> Dict[str, Any]:
    return user


def require_constituency(slug: str, user: Dict[str, Any]) -> str:
    """
    Validate that the authenticated user is allowed to access `slug`.
    Returns the normalised slug.  Raises 403 otherwise.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="constituency_id is required",
        )
    if not user_may_access(user, slug):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not authorised for constituency '{slug}'",
        )
    return slug
