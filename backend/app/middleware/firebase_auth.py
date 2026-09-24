"""
Firebase ID-token verification.

In production every route (except /healthz) requires a valid Bearer token.
Custom claims carry the list of constituency_slugs the user may access.

Local / test mode: set AGENT_LOCAL_MODE=true and omit the Authorization
header to use a synthetic admin user that can see every constituency.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Header, HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)

_firebase_ready = False


def _init_firebase() -> None:
    global _firebase_ready
    if _firebase_ready:
        return
    settings = get_settings()
    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            if settings.firebase_credentials_path:
                cred = credentials.Certificate(settings.firebase_credentials_path)
                firebase_admin.initialize_app(cred)
            else:
                # Application Default Credentials (Cloud Run / Workload Identity)
                firebase_admin.initialize_app()
        _firebase_ready = True
    except Exception as e:
        logger.warning("Firebase Admin init failed: %s", e)
        _firebase_ready = False


async def verify_token(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Returns a dict shaped like:
      {
        "uid": "...",
        "email": "...",
        "constituency_slugs": ["gyanpur", ...],
        "role": "campaign_worker" | "admin",
      }
    """
    settings = get_settings()

    # Local-dev bypass
    if settings.agent_local_mode and not authorization:
        return {
            "uid": "local-dev",
            "email": "dev@localhost",
            "constituency_slugs": ["*"],  # all
            "role": "admin",
        }

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = authorization[7:].strip()
    _init_firebase()

    if not _firebase_ready:
        if settings.agent_local_mode:
            # Still allow local mode with a dummy token
            return {
                "uid": "local-dev",
                "email": "dev@localhost",
                "constituency_slugs": ["*"],
                "role": "admin",
            }
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase Auth is not configured",
        )

    try:
        from firebase_admin import auth as firebase_auth

        decoded = firebase_auth.verify_id_token(token)
    except Exception as e:
        logger.info("Token verification failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    claims = decoded.get("constituency_slugs") or decoded.get("claims", {}).get(
        "constituency_slugs"
    ) or []
    if isinstance(claims, str):
        claims = [claims]

    return {
        "uid": decoded.get("uid", ""),
        "email": decoded.get("email", ""),
        "constituency_slugs": list(claims),
        "role": decoded.get("role") or decoded.get("claims", {}).get("role") or "campaign_worker",
    }


def user_may_access(user: Dict[str, Any], slug: str) -> bool:
    allowed: List[str] = user.get("constituency_slugs") or []
    if "*" in allowed:
        return True
    return slug.strip().lower() in [s.lower() for s in allowed]
