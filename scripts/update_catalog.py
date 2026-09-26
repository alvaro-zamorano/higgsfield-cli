"""Refresh src/hfcli/catalog.json from Higgsfield's public model docs (llms.txt per endpoint).

Usage:
  python scripts/update_catalog.py                 # refresh every endpoint already in the catalog
  python scripts/update_catalog.py <endpoint> ...  # refresh or add specific endpoints
  python scripts/update_catalog.py --check         # report differences without writing

Only public documentation is fetched; no API key is used and nothing is billed.
New endpoints get no alias automatically: add one to "aliases" by hand.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import httpx

CATALOG = Path(__file__).resolve().parents[1] / "src" / "hfcli" / "catalog.json"
DOCS = "https://dash.higgsfield.ai/models/{endpoint}/llms.txt"


def fetch(endpoint: str, http: httpx.Client) -> dict:
    resp = http.get(DOCS.format(endpoint=endpoint))
    resp.raise_for_status()
    text = resp.text
    schema = None
    for block in re.findall(r"```json\s*\n(.*?)```", text, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "object" and "properties" in data:
            schema = data
            break
    if schema is None:
        raise ValueError("no input JSON schema found in the docs")
    title = re.search(r"^# (.+)$", text, re.M)
    category = re.search(r"\*\*Category\*\*:\s*(\S+)", text)
    pricing = re.search(r"## Pricing\s*\n(.*?)(?=\n## )", text, re.S)
    return {
        "title": title.group(1).strip() if title else endpoint,
        "operation": category.group(1) if category else "",
        "pricing": re.sub(r"\s+", " ", pricing.group(1)).strip() if pricing else "",
        "schema": schema,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoints", nargs="*")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    catalog = json.loads(CATALOG.read_text("utf-8"))
    targets = args.endpoints or list(catalog["endpoints"])
    changed, failed = 0, 0
    with httpx.Client(timeout=30.0, follow_redirects=True) as http:
        for endpoint in targets:
            try:
                fresh = fetch(endpoint, http)
            except (httpx.HTTPError, ValueError) as exc:
                failed += 1
                print(f"FAIL  {endpoint}: {exc}")
                continue
            current = catalog["endpoints"].get(endpoint)
            if current is None:
                output = "video" if "video" in fresh["operation"] or "video" in endpoint else "image"
                catalog["endpoints"][endpoint] = {"title": fresh["title"], "output": output,
                                                  "operation": fresh["operation"], "schema": fresh["schema"],
                                                  "pricing": fresh["pricing"]}
                changed += 1
                print(f"NEW   {endpoint} ({output}) — add an alias by hand if useful")
                continue
            notes = []
            if current["schema"] != fresh["schema"]:
                before, after = set(current["schema"].get("properties", {})), set(fresh["schema"].get("properties", {}))
                notes.append(f"schema changed (+{sorted(after - before)} -{sorted(before - after)})")
                current["schema"] = fresh["schema"]
            if current.get("pricing") != fresh["pricing"]:
                notes.append("pricing updated")
                current["pricing"] = fresh["pricing"]
            if notes:
                changed += 1
                print(f"UPD   {endpoint}: {'; '.join(notes)}")
            else:
                print(f"same  {endpoint}")
    if changed and not args.check:
        catalog["_meta"]["retrieved"] = date.today().isoformat()
        catalog["_meta"]["endpoints"] = len(catalog["endpoints"])
        catalog["endpoints"] = dict(sorted(catalog["endpoints"].items()))
        CATALOG.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"\n{changed} changed, {failed} failed{' (check only, nothing written)' if args.check else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
