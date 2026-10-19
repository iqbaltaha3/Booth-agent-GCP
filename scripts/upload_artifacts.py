#!/usr/bin/env python3
"""Upload normalized constituencies separately; preview unless --execute is set."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bucket', default='booth-agent-data-dev')
    p.add_argument('--slug')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--replace', action='store_true',
                   help='Remove destination-only objects within each selected constituency')
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    seats = json.loads((root / 'constituencies/manifest.json').read_text())
    selected = [s['slug'] for s in seats if not args.slug or s['slug'] == args.slug]
    if not selected:
        p.error('No matching constituency')
    for slug in selected:
        source = root / 'legacy-data' / slug
        if not (source / 'coverage.json').is_file():
            p.error(f'Import and validate {slug} before uploading')
    for slug in selected:
        log = Path('/tmp') / f'booth-upload-{slug}.log'
        cmd = ['gcloud', 'storage', 'rsync', str(root / 'legacy-data' / slug),
               f'gs://{args.bucket}/{slug}', '--recursive', '--checksums-only',
               '--exclude=.*\\.DS_Store$']
        if not args.execute:
            cmd.append('--dry-run')
        if args.replace:
            cmd.append('--delete-unmatched-destination-objects')
        with log.open('w') as output:
            result = subprocess.run(cmd, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise SystemExit(f'{slug}: upload failed; inspect {log}')
        print(f'{slug}: {"uploaded" if args.execute else "preview complete"}; {log}', flush=True)
    print(('Destination-only objects removed within selected constituencies.' if args.replace and args.execute
           else 'Replacement preview only.' if args.replace
           else 'Existing destination-only objects are preserved.')
          + ' Redeploy/restart the backend after updates to clear cached data.')


if __name__ == '__main__':
    main()
