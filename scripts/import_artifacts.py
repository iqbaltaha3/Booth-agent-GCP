#!/usr/bin/env python3
"""Normalize constituency snapshots without changing the source artifacts.

Default is a read-only audit. --apply imports snapshots into legacy-data and
refreshes constituency configuration. Empty rolls retain the real roll schema.
"""
import argparse
from contextlib import closing
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {"ALLD-NORTH": "allahabad-north", "ALLD-SOUTH": "allahabad-south",
           "ALLD-WEST": "allahabad-west", "GYANPUR(BHADOHI)": "gyanpur"}
DECLARED = {"phulpur": (115, 457), "handia": (391, 451)}
COMPLETE = {"allahabad-north", "allahabad-south", "allahabad-west", "gyanpur", "meja"}


def one(paths):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError(f"Expected exactly one artifact, found: {paths}")
    return paths[0]


def audit(folder):
    slug = ALIASES.get(folder.name, folder.name.lower())
    history = one(folder.glob('*history.db'))
    boothlist = one(folder.glob('*boothlist*.csv'))
    rolls = list(folder.glob('*voters.db'))
    if len(rolls) > 1:
        raise ValueError(f"Ambiguous voter databases: {folder}")
    reports = list(folder.glob('*/**/*.json'))
    if len({p.name for p in reports}) != len(reports):
        raise ValueError(f"Duplicate report filenames: {folder}")
    with boothlist.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    for db in [history] + rolls:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as c:
            if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError(f"Invalid SQLite artifact: {db}")
    stations = 0
    if rolls:
        with closing(sqlite3.connect(rolls[0].as_uri() + '?mode=ro', uri=True)) as c:
            stations = c.execute('SELECT COUNT(DISTINCT polling_station) FROM voters').fetchone()[0]
    parts = set()
    for report in reports:
        obj = json.loads(report.read_text(encoding='utf-8'))
        part = (obj.get('booth_metadata') or {}).get('booth_name')
        if part:
            parts.add(str(part))
    done, total = DECLARED.get(slug, (None, None))
    # A later source snapshot may have grown; do not reuse an obsolete declaration.
    if done is not None and len(reports) != done:
        done = None
    return slug, history, boothlist, rolls, reports, {
        'status': 'history_only' if not rolls else 'partial' if slug in DECLARED else 'complete_declared' if slug in COMPLETE else 'unverified',
        'history_available': True, 'voter_available': bool(rolls and stations),
        'portfolio_available': bool(reports), 'completed_booths': done,
        'total_booths': total, 'count_basis': 'owner_declared' if slug in DECLARED else 'not_reconciled',
        'csv_rows': len(rows), 'portfolio_files': len(reports),
        'portfolio_booth_ids': sorted(parts), 'voter_station_values': stations,
        'note': 'Station names and file counts are not verified booth counts. Coverage totals require reconciliation.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('--slug', help='Import only this normalized constituency slug')
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    source = args.source.expanduser().resolve()
    # Validate all selected seats before changing any local artifacts.
    seats = [audit(d) for d in sorted(source.iterdir()) if d.is_dir()
             and (not args.slug or ALIASES.get(d.name, d.name.lower()) == args.slug)]
    if not seats:
        p.error('No matching constituency folders')
    template = one(next(d for d in source.iterdir() if d.name == 'PHULPUR').glob('*voters.db'))
    with closing(sqlite3.connect(template.as_uri() + '?mode=ro', uri=True)) as c:
        schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='voters'").fetchone()[0]
    for slug, history, boothlist, rolls, reports, coverage in seats:
        print(json.dumps({'slug': slug, **{k: v for k, v in coverage.items() if k != 'portfolio_booth_ids'}}))
        if not args.apply:
            continue
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp) / slug
            stage.mkdir()
            shutil.copy2(history, stage / 'history.db')
            shutil.copy2(boothlist, stage / 'boothlist.csv')
            if rolls:
                shutil.copy2(rolls[0], stage / 'voters.db')
            else:
                with closing(sqlite3.connect(stage / 'voters.db')) as c, c:
                    c.execute(schema)
            (stage / 'booth_analysis').mkdir()
            for report in reports:
                shutil.copy2(report, stage / 'booth_analysis' / report.name)
            (stage / 'coverage.json').write_text(json.dumps(coverage, indent=2) + '\n')
            subprocess.run([sys.executable, str(ROOT / 'scripts/add_constituency.py'), str(stage),
                            '--slug', slug, '--district', 'Bhadohi' if slug == 'gyanpur' else 'Prayagraj'], check=True)


if __name__ == '__main__':
    main()
