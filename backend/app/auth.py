"""Verify Firebase login and restrict access to assigned constituencies."""

from functools import lru_cache

import firebase_admin
from fastapi import Header, HTTPException
from firebase_admin import auth, credentials

from app.config import get_settings


@lru_cache
def firebase_app():
    """Initialize Firebase once, using a certificate or cloud credentials."""
    settings = get_settings()
    try:
        return firebase_admin.get_app()
    except ValueError:
        credential = (
            credentials.Certificate(settings.firebase_credentials_path)
            if settings.firebase_credentials_path
            else None
        )
        return firebase_admin.initialize_app(credential)


def get_current_user(authorization: str | None = Header(None)) -> dict:
    """Read the Bearer token; allow header-free access only in local mode."""
    if get_settings().agent_local_mode and not authorization:
        return {"uid": "local-dev", "email": "dev@localhost", "role": "admin", "constituency_slugs": ["*"]}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    try:
        firebase_app()
    except Exception as error:
        raise HTTPException(503, "Firebase Auth is not configured") from error
    try:
        decoded = auth.verify_id_token(authorization[7:].strip())
    except Exception as error:
        raise HTTPException(401, "Invalid or expired token") from error
    claims = decoded.get("claims", {})
    allowed = decoded.get("constituency_slugs") or claims.get("constituency_slugs") or []
    return {
        "uid": decoded.get("uid", ""),
        "email": decoded.get("email", ""),
        "role": decoded.get("role") or claims.get("role") or "campaign_worker",
        "constituency_slugs": [allowed] if isinstance(allowed, str) else allowed,
    }


def user_may_access(user: dict, slug: str) -> bool:
    """Check the user's assigned seats, including the admin wildcard."""
    allowed = [item.lower() for item in user.get("constituency_slugs", [])]
    return "*" in allowed or slug.lower() in allowed


def require_constituency(slug: str, user: dict) -> str:
    """Normalize a seat id and reject invalid or unauthorized requests."""
    slug = slug.strip().lower()
    if not slug or any(character in slug for character in ("..", "/", "\\")):
        raise HTTPException(400, "Invalid constituency_id")
    if not user_may_access(user, slug):
        raise HTTPException(403, "Not authorised for this constituency")
    return slug
