#!/usr/bin/env python3
"""Validate the self-contained WeChat archive without changing it."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_IMAGE_RE = re.compile(r"(?:!\[[^\]]*\]\((?:<)?(\.\./assets/[^)>]+)(?:>)?\)|src=['\"](\.\./assets/[^'\"]+)['\"])")
WECHAT_IMAGE_RE = re.compile(r"https?://(?:mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)/", flags=re.I)


def main() -> int:
    errors: list[str] = []
    manifest_path = ROOT / "data" / "manifest.json"
    assets_path = ROOT / "data" / "assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    article_files = sorted((ROOT / "articles").glob("*.md"))
    asset_files = sorted(path for path in (ROOT / "assets").iterdir() if path.is_file())

    orders = [item.get("order") for item in manifest]
    urls = [item.get("url") for item in manifest]
    files = [item.get("file") for item in manifest]
    if len(orders) != len(set(orders)):
        errors.append("manifest contains duplicate order values")
    if len(urls) != len(set(urls)):
        errors.append("manifest contains duplicate URLs")
    if len(files) != len(set(files)):
        errors.append("manifest contains duplicate filenames")
    if {path.name for path in article_files} != set(files):
        errors.append("article files do not exactly match manifest filenames")

    for item in manifest:
        article = ROOT / "articles" / str(item.get("file", ""))
        if not article.exists():
            errors.append(f"missing article: {item.get('file')}")
            continue
        text = article.read_text(encoding="utf-8")
        if not item.get("fetch_error") and WECHAT_IMAGE_RE.search(text):
            errors.append(f"complete article still has WeChat image links: {item['file']}")
        source_match = re.search(r'^source:\s*"([^"]+)"', text, flags=re.MULTILINE)
        order_match = re.search(r"^order:\s*(\d+)", text, flags=re.MULTILINE)
        if not source_match or source_match.group(1) != item.get("url"):
            errors.append(f"front matter source mismatch: {item['file']}")
        if not order_match or int(order_match.group(1)) != item.get("order"):
            errors.append(f"front matter order mismatch: {item['file']}")
        for match in LOCAL_IMAGE_RE.finditer(text):
            relative = match.group(1) or match.group(2)
            target = (article.parent / relative).resolve()
            if ROOT not in target.parents or not target.exists():
                errors.append(f"broken image link in {item['file']}: {relative}")

    asset_urls = [item.get("url") for item in assets]
    asset_paths = [item.get("local_path") for item in assets]
    if len(asset_urls) != len(set(asset_urls)):
        errors.append("assets.json contains duplicate URLs")
    if len(asset_paths) != len(set(asset_paths)):
        errors.append("assets.json contains duplicate local paths")
    if any(item.get("status") != "downloaded" for item in assets):
        errors.append("assets.json contains failed downloads")
    for item in assets:
        path = ROOT / str(item.get("local_path", ""))
        if path.parent != ROOT / "assets" or not path.exists():
            errors.append(f"missing or unsafe asset path: {item.get('local_path')}")
    if {path.relative_to(ROOT).as_posix() for path in asset_files} != set(asset_paths):
        errors.append("asset files do not exactly match assets.json")

    report = {
        "manifest_items": len(manifest),
        "article_files": len(article_files),
        "complete_articles": sum(not item.get("fetch_error") for item in manifest),
        "incremental_articles": sum(item.get("sync_method") == "link-incremental" for item in manifest),
        "asset_files": len(asset_files),
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
