#!/usr/bin/env python3
"""List the exact split-tar assets and sizes for one adsb.lol release tag."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def api_url(tag: str) -> str:
    m = re.fullmatch(r"v(\d{4})\.\d{2}\.\d{2}-planes-readsb-prod-0", tag)
    if not m:
        raise ValueError(f"invalid dump tag: {tag}")
    repo = f"adsblol/globe_history_{m.group(1)}"
    return f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"


def fetch_release(tag: str) -> dict:
    fixture = os.environ.get("ADSB_ASSET_API_JSON")
    if fixture:
        return json.loads(Path(fixture).read_text())
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "co2gap-release-asset-validator/1"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api_url(tag), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(44) from exc
        raise RuntimeError(f"GitHub release API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub release API failed: {exc}") from exc


def split_assets(release: dict, tag: str) -> list[tuple[str, int, str]]:
    prefix = f"{tag}.tar."
    found = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if not re.fullmatch(re.escape(prefix) + r"[a-z]{2}", name):
            continue
        size = asset.get("size")
        url = asset.get("browser_download_url")
        if not isinstance(size, int) or size <= 0 or not isinstance(url, str):
            raise ValueError(f"asset {name!r} lacks a positive size or download URL")
        found.append((name, size, url))
    found.sort()
    if not found:
        raise ValueError(f"release {tag} has no split-tar assets")
    expected_suffixes = [
        chr(ord("a") + i // 26) + chr(ord("a") + i % 26)
        for i in range(len(found))
    ]
    actual_suffixes = [name[-2:] for name, _, _ in found]
    if actual_suffixes != expected_suffixes:
        raise ValueError(
            f"release {tag} split assets are not contiguous from aa: {actual_suffixes}")
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    args = ap.parse_args()
    try:
        release = fetch_release(args.tag)
        assets = split_assets(release, args.tag)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"asset manifest error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    for name, size, url in assets:
        print(f"{name}\t{size}\t{url}")


if __name__ == "__main__":
    main()
