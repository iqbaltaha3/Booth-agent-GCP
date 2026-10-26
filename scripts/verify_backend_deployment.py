"""Fail a deployment whose live backend does not expose the voter search API."""

import argparse
import json
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend_url")
    args = parser.parse_args()
    with urlopen(args.backend_url.rstrip("/") + "/openapi.json", timeout=30) as response:
        schema = json.load(response)
    paths = schema.get("paths", {})
    required = {"/voter/search-options": "get", "/voter/search": "post"}
    missing = [route for route, method in required.items() if method not in paths.get(route, {})]
    if missing:
        raise SystemExit("Live backend is missing required routes: " + ", ".join(missing))
    print("Live voter search API verified.")


if __name__ == "__main__":
    main()
