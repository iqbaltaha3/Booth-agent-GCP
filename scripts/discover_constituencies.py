#!/usr/bin/env python3
"""
Discover every constituencies/<slug>/constituency.yaml, optionally validate
that a matching folder exists in the GCS data bucket, and write manifest.json.

Usage:
  python scripts/discover_constituencies.py --out constituencies/manifest.json
  python scripts/discover_constituencies.py --env dev --bucket booth-agent-data-dev --out ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="constituencies", help="Path to constituencies/ folder")
    p.add_argument("--out", default="constituencies/manifest.json")
    p.add_argument("--env", default="")
    p.add_argument("--bucket", default="")
    p.add_argument("--skip-gcs-check", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"ERROR: {root} does not exist", file=sys.stderr)
        return 1

    items = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        yml = d / "constituency.yaml"
        if not yml.exists():
            print(f"WARNING: {d.name} has no constituency.yaml — skipped")
            continue
        meta = {"slug": d.name}
        if yaml:
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
                meta.update(data)
            except Exception as e:
                print(f"WARNING: failed to parse {yml}: {e}")
        else:
            # minimal fallback without PyYAML
            for line in yml.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip().strip("\"'")

        slug = meta.get("slug") or d.name
        meta["slug"] = slug

        # Optional GCS validation
        if args.bucket and not args.skip_gcs_check:
            try:
                from google.cloud import storage

                client = storage.Client()
                bucket = client.bucket(args.bucket)
                # Require at least voters.db
                blob = bucket.blob(f"{slug}/voters.db")
                if not blob.exists():
                    print(
                        f"ERROR: gs://{args.bucket}/{slug}/voters.db not found. "
                        f"Upload data before merging this YAML.",
                        file=sys.stderr,
                    )
                    return 2
                print(f"OK  gs://{args.bucket}/{slug}/")
            except Exception as e:
                print(f"WARNING: GCS check failed for {slug}: {e}")

        items.append(meta)
        print(f"discovered: {slug} ({meta.get('name', '')})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(items)} constituencies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
