#!/usr/bin/env bash
# Prepare legacy-data/gyanpur/ for local development from the original MVP files.
# Run from the repo root after placing the original DBs next to this script or
# pointing SRC to the extracted Arjun-Booth-Agent folder.
set -euo pipefail

SRC="${1:-../arjun-src/Arjun-Booth-Agent}"
DEST="legacy-data/gyanpur"

mkdir -p "$DEST/booth_analysis"

if [[ -f "$SRC/gyanpur_voters.db" ]]; then
  cp -f "$SRC/gyanpur_voters.db" "$DEST/voters.db"
  echo "OK voters.db"
else
  echo "MISSING $SRC/gyanpur_voters.db — copy it manually"
fi

if [[ -f "$SRC/gyanpur_history.db" ]]; then
  cp -f "$SRC/gyanpur_history.db" "$DEST/history.db"
  echo "OK history.db"
else
  echo "MISSING $SRC/gyanpur_history.db — copy it manually"
fi

if [[ -f "$SRC/gyanpur-boothlist.csv" ]]; then
  cp -f "$SRC/gyanpur-boothlist.csv" "$DEST/boothlist.csv"
  echo "OK boothlist.csv"
fi

if [[ -d "$SRC/booth_analysis" ]]; then
  cp -f "$SRC/booth_analysis/"*.json "$DEST/booth_analysis/" 2>/dev/null || true
  echo "OK booth_analysis/ ($(ls "$DEST/booth_analysis" | wc -l) files)"
fi

echo "Local data ready under $DEST"
echo "Set LOCAL_DATA_ROOT=$(pwd)/legacy-data when starting the backend."
