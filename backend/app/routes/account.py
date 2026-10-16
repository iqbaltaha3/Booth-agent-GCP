"""Return the signed-in profile and its available constituencies."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends

from app.auth import get_current_user, user_may_access
from app.config import get_settings

router = APIRouter(tags=["account"])


def allowed_constituencies(user: dict) -> list:
    """Read the configured manifest and filter it by the user's permissions."""
    path = Path(get_settings().constituencies_manifest_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    with path.open(encoding="utf-8") as file:
        manifest = json.load(file)
    items = manifest if isinstance(manifest, list) else manifest["constituencies"]
    return [item for item in items if user_may_access(user, item["slug"])]


@router.get("/auth/me")
@router.post("/auth/bootstrap")
def profile(user: dict = Depends(get_current_user)) -> dict:
    """Supply the profile and seat selector after login."""
    return {**user, "constituencies": allowed_constituencies(user)}


@router.get("/constituencies")
def constituencies(user: dict = Depends(get_current_user)) -> list:
    """List only the constituencies this user can open."""
    return allowed_constituencies(user)
