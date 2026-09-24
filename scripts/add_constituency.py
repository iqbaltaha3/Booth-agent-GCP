#!/usr/bin/env python3
"""
Add or update a constituency in the repo-local layout.

The script expects an artifact folder containing:
  voters.db
  history.db
  boothlist.csv
  booth_analysis/*.json

The folder name is used as the constituency slug unless --slug is passed.

It creates:
  legacy-data/<slug>/...
  constituencies/<slug>/constituency.yaml
  constituencies/manifest.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_FILES = ("voters.db", "history.db", "boothlist.csv")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def title_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def copy_artifacts(src: Path, dest: Path) -> int:
    if not src.exists() or not src.is_dir():
        die(f"artifact folder not found: {src}")

    for filename in REQUIRED_FILES:
        if not (src / filename).is_file():
            die(f"missing required file: {src / filename}")

    src_booth = src / "booth_analysis"
    if not src_booth.is_dir():
        die(f"missing required folder: {src_booth}")

    json_files = sorted(src_booth.glob("*.json"))
    if not json_files:
        die(f"no JSON files found in {src_booth}")

    if src.resolve() == dest.resolve():
        return len(json_files)

    dest.mkdir(parents=True, exist_ok=True)
    for filename in REQUIRED_FILES:
        shutil.copy2(src / filename, dest / filename)

    dest_booth = dest / "booth_analysis"
    if dest_booth.exists():
        shutil.rmtree(dest_booth)
    dest_booth.mkdir(parents=True, exist_ok=True)
    for json_file in json_files:
        shutil.copy2(json_file, dest_booth / json_file.name)

    return len(json_files)


def write_constituency_yaml(
    path: Path,
    slug: str,
    name: str,
    seat_type: str,
    district: str,
    state: str,
    product_name: str,
    product_tagline: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            f"slug: {slug}",
            f"name: {name}",
            f"seat_type: {seat_type}",
            f"district: {district}",
            f"state: {state}",
            f"product_name: {product_name}",
            f"product_tagline: {yaml_quote(product_tagline)}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def load_existing_yaml(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("\"'")
    return data


def refresh_manifest(repo_root: Path) -> None:
    discover = repo_root / "scripts" / "discover_constituencies.py"
    subprocess.run(
        [
            sys.executable,
            str(discover),
            "--out",
            "constituencies/manifest.json",
            "--skip-gcs-check",
        ],
        cwd=repo_root,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add or update a constituency from a folder named like the slug."
    )
    parser.add_argument(
        "artifacts",
        help="Folder named by slug and containing voters.db, history.db, boothlist.csv, and booth_analysis/",
    )
    parser.add_argument("--slug", help="Optional override. Default: artifact folder name.")
    parser.add_argument("--name", help="Display name, e.g. Gyanpur. Default: title-cased slug.")
    parser.add_argument("--district", help="District name. Default: existing YAML value or blank.")
    parser.add_argument("--state", help="State name. Default: existing YAML value or Uttar Pradesh.")
    parser.add_argument("--seat-type", help="Default: existing YAML value or Vidhan Sabha (Assembly).")
    parser.add_argument("--product-name", help="Default: existing YAML value or Arjun.")
    parser.add_argument("--product-tagline", default="")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    artifact_root = Path(args.artifacts).expanduser().resolve()
    slug = (args.slug or artifact_root.name).strip().lower()
    if not SLUG_RE.match(slug):
        die("slug must use lowercase letters, numbers, and hyphens only")

    local_dest = repo_root / "legacy-data" / slug
    yaml_path = repo_root / "constituencies" / slug / "constituency.yaml"
    existing = load_existing_yaml(yaml_path)

    name = (args.name or existing.get("name") or title_from_slug(slug)).strip()
    district = (args.district or existing.get("district") or "").strip()
    state = (args.state or existing.get("state") or "Uttar Pradesh").strip()
    seat_type = (args.seat_type or existing.get("seat_type") or "Vidhan Sabha (Assembly)").strip()
    product_name = (args.product_name or existing.get("product_name") or "Arjun").strip()
    product_tagline = (args.product_tagline or existing.get("product_tagline") or "").strip()
    if not product_tagline:
        product_tagline = f"Booth-level electoral intelligence for {name}"

    count = copy_artifacts(artifact_root, local_dest)
    write_constituency_yaml(
        yaml_path,
        slug,
        name,
        seat_type,
        district,
        state,
        product_name,
        product_tagline,
    )
    refresh_manifest(repo_root)

    summary = {
        "slug": slug,
        "local_data": str(local_dest.relative_to(repo_root)),
        "config": str(yaml_path.relative_to(repo_root)),
        "portfolio_json_files": count,
        "action": "updated" if existing else "added",
        "gcs_upload_command": f"gsutil -m rsync -r {local_dest}/ gs://booth-agent-data-dev/{slug}/",
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
