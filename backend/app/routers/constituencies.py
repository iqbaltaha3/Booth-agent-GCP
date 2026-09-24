"""
List constituencies the current user may access.
Populates the frontend login / dropdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.deps import get_current_user
from app.middleware.firebase_auth import user_may_access

router = APIRouter(tags=["constituencies"])


def _load_manifest() -> List[Dict[str, Any]]:
    settings = get_settings()
    path = Path(settings.constituencies_manifest_path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("constituencies", [])

    # Fallback: scan constituencies/ folders for YAML
    root = Path(__file__).resolve().parents[3] / "constituencies"
    if not root.exists():
        root = Path.cwd() / "constituencies"
    items = []
    if root.exists():
        for d in sorted(root.iterdir()):
            yaml_path = d / "constituency.yaml"
            if d.is_dir() and yaml_path.exists():
                try:
                    import yaml

                    with open(yaml_path, encoding="utf-8") as f:
                        meta = yaml.safe_load(f) or {}
                    items.append(
                        {
                            "slug": meta.get("slug") or d.name,
                            "name": meta.get("name") or d.name,
                            "district": meta.get("district", ""),
                            "state": meta.get("state", ""),
                            "seat_type": meta.get("seat_type", ""),
                            "product_name": meta.get("product_name", "Arjun"),
                            "product_tagline": meta.get("product_tagline", ""),
                        }
                    )
                except Exception:
                    items.append({"slug": d.name, "name": d.name})
    return items


@router.get("/constituencies")
def list_constituencies(
    user: Dict[str, Any] = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    all_items = _load_manifest()
    return [c for c in all_items if user_may_access(user, c.get("slug", ""))]
