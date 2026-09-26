#!/usr/bin/env python3
"""
Convert Hindi booth-list CSVs to English using GCP Vertex AI Gemini.

Before running:
  gcloud auth application-default login
  gcloud config set project YOUR_GCP_PROJECT_ID

Run all hardcoded CSVs:
  python gemini_boothlist_to_english.py

Run only a small test:
  python gemini_boothlist_to_english.py --only gyanpur --limit 30

The script prints converted rows as each batch returns and saves progress after
every successful batch. Use --resume to continue from an existing partial CSV.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests


BASE_DIR = Path("/Users/iqbaltaha3/Documents/PROJECTS/POLITICAL_PROJECTS")

JOBS = {
    "alld-south": {
        "input": BASE_DIR / "alld-south" / "alld-south-boothlist-hindi.csv",
        "output": BASE_DIR / "alld-south" / "alld-south-boothlist-english.csv",
    },
    "alld-west": {
        "input": BASE_DIR / "alld-west" / "alld-west-boothlist-hindi.csv",
        "output": BASE_DIR / "alld-west" / "alld-west-boothlist-english.csv",
    },
    "phulpur": {
        "input": BASE_DIR / "phulpur" / "phulpur-boothlist-hindi.csv",
        "output": BASE_DIR / "phulpur" / "phulpur-boothlist-english.csv",
    },
}

DEFAULT_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_BATCH_SIZE = 25
DEFAULT_TIMEOUT = 180
DEFAULT_RETRIES = 3


SYSTEM_PROMPT = """
You convert Uttar Pradesh Hindi/Devanagari polling booth list rows into simple English.

Return only valid JSON in this exact shape:
{"rows":[{"part_no":"Part_1","pooling_station":"...","region":"...","total_voters":858.0}]}

Rules:
- Preserve part_no exactly.
- Preserve total_voters exactly.
- Convert only pooling_station and region.
- Use lowercase English.
- Use practical election-booth English, not academic transliteration.
- Translate institution words consistently:
  उच्च प्राथमिक विद्यालय = upper primary school
  प्राथमिक विद्यालय = primary school
  कम्पोजिट विद्यालय = composite school
  पूर्व माध्यमिक विद्यालय = junior high school
  कन्या जूनियर हाईस्कूल = girls junior high school
  राजकीय = government
  बालिका = girls
  इन्टर कालेज / इंटर कॉलेज / इन्टरकालेज = inter college
  कक्ष सं. / कमरा नं. / कमरा नं = room no
  भाग = part
  उत्तरी = north
  दक्षिणी = south
  पूर्वी = east
  पश्चिमी = west
  मध्य = middle
- Transliterate place names naturally and consistently:
  खेवखर = khevkhar
  फाफामऊ = phaphamau
  तेलियरगंज = teliyarganj
  बंधवा = bandhawa
  मेजा = meja
- Expand abbreviations when clear:
  उ0 भाग = north part
  द0 भाग = south part
- Do not invent, omit, reorder, or merge rows.
- Do not add explanations.
""".strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Hindi booth-list CSVs to English using Vertex AI Gemini.")
    parser.add_argument("--only", choices=sorted(JOBS), default=None, help="Run only one hardcoded CSV")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="GCP project ID")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help=f"Vertex location, default: {DEFAULT_LOCATION}")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model, default: {DEFAULT_MODEL}")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"Rows per Gemini call, default: {DEFAULT_BATCH_SIZE}")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N rows for testing")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout seconds, default: {DEFAULT_TIMEOUT}")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"Retries per batch, default: {DEFAULT_RETRIES}")
    parser.add_argument("--resume", action="store_true", help="Skip rows already present in the output CSV")
    return parser.parse_args()


def get_gcp_project(cli_project: str | None) -> str:
    if cli_project:
        return cli_project
    try:
        project = subprocess.check_output(["gcloud", "config", "get-value", "project"], text=True).strip()
    except Exception:
        project = ""
    if not project:
        raise RuntimeError("No GCP project found. Pass --project or run: gcloud config set project YOUR_PROJECT_ID")
    return project


def get_access_token() -> str:
    try:
        return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    except Exception as exc:
        raise RuntimeError("Could not get GCP access token. Run: gcloud auth login") from exc


def clean_json_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last >= first:
        text = text[first : last + 1]
    return text


def call_gemini(
    rows: list[dict[str, Any]],
    project: str,
    location: str,
    model: str,
    timeout: int,
) -> list[dict[str, Any]]:
    token = get_access_token()
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
    )
    prompt = {
        "task": "Convert these booth-list rows to English.",
        "required_columns": ["part_no", "pooling_station", "region", "total_voters"],
        "rows": rows,
    }
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": SYSTEM_PROMPT + "\n\nInput JSON:\n" + json.dumps(prompt, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(clean_json_text(text))
    if isinstance(parsed, dict):
        parsed = parsed.get("rows", parsed.get("data", []))
    if not isinstance(parsed, list):
        raise ValueError(f"Gemini returned JSON but no row list: {parsed!r}")
    return parsed


def normalize_output_rows(source_rows: list[dict[str, Any]], model_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_part_no = {str(row.get("part_no")): row for row in model_rows}
    normalized = []
    for source in source_rows:
        part_no = str(source["part_no"])
        model_row = by_part_no.get(part_no, {})
        normalized.append(
            {
                "part_no": part_no,
                "pooling_station": str(model_row.get("pooling_station", source["pooling_station"])).strip(),
                "region": str(model_row.get("region", source["region"])).strip(),
                "total_voters": source["total_voters"],
            }
        )
    return normalized


def print_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(f"{row['part_no']},{row['pooling_station']},{row['region']},{row['total_voters']}", flush=True)


def convert_csv(
    input_path: Path,
    output_path: Path,
    project: str,
    location: str,
    model: str,
    batch_size: int,
    limit: int | None,
    timeout: int,
    retries: int,
    resume: bool,
) -> None:
    required = ["part_no", "pooling_station", "region", "total_voters"]
    df = pd.read_csv(input_path)
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{input_path} is missing columns: {missing}")
    df = df[required]
    if limit is not None:
        df = df.head(limit)

    converted_records: list[dict[str, Any]] = []
    if resume and output_path.exists():
        existing = pd.read_csv(output_path)
        if set(required).issubset(existing.columns):
            converted_records = existing[required].to_dict(orient="records")
            done = {str(row["part_no"]) for row in converted_records}
            df = df[~df["part_no"].astype(str).isin(done)]
            print(f"Resuming: {len(done)} rows already completed in {output_path}")

    source_records = df.to_dict(orient="records")
    print(f"\nInput:  {input_path}")
    print(f"Output: {output_path}")

    for start in range(0, len(source_records), batch_size):
        batch = source_records[start : start + batch_size]
        print(f"Converting rows {start + 1}-{start + len(batch)} of {len(source_records)}...", flush=True)
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                model_rows = call_gemini(batch, project, location, model, timeout)
                normalized_rows = normalize_output_rows(batch, model_rows)
                converted_records.extend(normalized_rows)
                print_rows(normalized_rows)

                out_df = pd.DataFrame(converted_records, columns=required)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                out_df.to_csv(output_path, index=False)
                print(f"  Saved {len(out_df)} rows -> {output_path}", flush=True)
                break
            except Exception as exc:
                last_error = exc
                print(f"  Attempt {attempt}/{retries} failed: {exc}", flush=True)
                if attempt < retries:
                    time.sleep(5 * attempt)
        else:
            parts = ", ".join(str(row["part_no"]) for row in batch)
            raise RuntimeError(f"Failed batch after {retries} attempts: {parts}. Last error: {last_error}")


def main() -> int:
    args = parse_args()
    try:
        project = get_gcp_project(args.project)
        selected = {args.only: JOBS[args.only]} if args.only else JOBS
        for name, job in selected.items():
            print(f"=== {name} ===")
            convert_csv(
                input_path=job["input"],
                output_path=job["output"],
                project=project,
                location=args.location,
                model=args.model,
                batch_size=args.batch_size,
                limit=args.limit,
                timeout=args.timeout,
                retries=args.retries,
                resume=args.resume,
            )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())