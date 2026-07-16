#!/usr/bin/env python3
"""Incrementally archive one WeChat article and optionally publish it to GitHub."""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ACCOUNT = "百味鸡OB Pluto"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148 MicroMessenger/8.0.56"
)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK_TAGS = {"article", "aside", "blockquote", "div", "figure", "figcaption", "footer", "header", "h1", "h2", "h3", "h4", "h5", "h6", "p", "section", "table", "tr"}
SKIP_TAGS = {"script", "style", "noscript", "svg"}
IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}
SHANGHAI = timezone(timedelta(hours=8))


class SyncError(RuntimeError):
    """A user-actionable synchronization failure."""


@dataclass
class Article:
    title: str
    account: str
    author: str
    published_at: str
    canonical_url: str
    markdown: str
    image_urls: list[str]
    content_type: str


def normalize_wechat_url(value: str) -> str:
    value = html.unescape(value.strip())
    if value.startswith("//"):
        value = "https:" + value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != "mp.weixin.qq.com":
        raise SyncError("只接受 mp.weixin.qq.com 的公众号文章链接。")
    path = re.sub(r"/+", "/", parts.path or "/")
    slug_match = re.fullmatch(r"/s/([^/?#]+)", path.rstrip("/"))
    if slug_match:
        return f"https://mp.weixin.qq.com/s/{slug_match.group(1)}"
    if path.rstrip("/") == "/s":
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        identity_keys = ["__biz", "mid", "idx", "sn"]
        if not all(query.get(key) for key in identity_keys[:3]):
            raise SyncError("这不是可识别的公众号文章链接；请使用文章右上角“复制链接”得到的地址。")
        pairs = [(key, query[key][0]) for key in identity_keys if query.get(key)]
        return "https://mp.weixin.qq.com/s?" + urllib.parse.urlencode(pairs)
    raise SyncError("这不是可识别的公众号文章链接；请使用文章右上角“复制链接”得到的地址。")


def normalize_image_url(value: str) -> str:
    value = html.unescape(value.strip()).replace("&amp;", "&")
    if value.startswith("//"):
        value = "https:" + value
    return value


def canonical_key(value: str) -> str:
    normalized = normalize_wechat_url(value)
    parts = urllib.parse.urlsplit(normalized)
    if parts.path.startswith("/s/"):
        return "slug:" + parts.path.split("/s/", 1)[1]
    query = urllib.parse.parse_qs(parts.query)
    return "triple:" + ":".join((query.get("__biz", [""])[0], query.get("mid", [""])[0], query.get("idx", [""])[0]))


class WeChatPageParser(HTMLParser):
    """Extract visible Markdown and basic metadata from a normal article page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._meta_capture = ""
        self._meta_depth = 0
        self._meta_parts: list[str] = []
        self._capture = False
        self._depth = 0
        self._skip_depth = 0
        self._out: list[str] = []
        self._link_stack: list[str] = []
        self.images: list[str] = []

    def _append(self, value: str) -> None:
        if value:
            self._out.append(value)

    def _newline(self, count: int = 1) -> None:
        self._append("\n" * count)

    def _image(self, value: str) -> None:
        url = normalize_image_url(value)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return
        token = f"{{{{WECHAT_IMAGE_{len(self.images):04d}}}}}"
        self.images.append(url)
        self._append(f"\n\n{token}\n\n")

    def handle_starttag(self, tag: str, attrs_raw: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = {key.lower(): (value or "") for key, value in attrs_raw}
        prop = attrs.get("property", "")
        if tag == "meta" and prop.startswith("og:"):
            self.meta[prop] = attrs.get("content", "").strip()

        element_id = attrs.get("id", "")
        if element_id in {"activity-name", "js_name", "js_author_name"} and not self._meta_capture:
            self._meta_capture = element_id
            self._meta_depth = 1
            self._meta_parts = []
        elif self._meta_capture and tag not in VOID_TAGS:
            self._meta_depth += 1

        if not self._capture:
            if element_id == "js_content":
                self._capture = True
                self._depth = 1
            return

        if tag not in VOID_TAGS:
            self._depth += 1
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag == "br":
            self._newline()
        elif tag == "hr":
            self._append("\n\n---\n\n")
        elif tag == "img":
            value = attrs.get("data-src") or attrs.get("data-original") or attrs.get("src")
            if value:
                self._image(value)
        elif tag == "a":
            href = normalize_image_url(attrs.get("href", ""))
            if href.startswith(("http://", "https://")):
                self._link_stack.append(href)
                self._append("[")
            else:
                self._link_stack.append("")
        elif tag in {"strong", "b"}:
            self._append("**")
        elif tag in {"em", "i"}:
            self._append("*")
        elif tag == "code":
            self._append("`")
        elif tag == "li":
            self._append("\n- ")
        elif tag == "blockquote":
            self._append("\n\n> ")
        elif tag in BLOCK_TAGS:
            self._newline(2)

        style = attrs.get("style", "")
        for match in re.finditer(r"(?:background(?:-image)?)\s*:[^;]*url\((['\"]?)(https?://[^)'\"]+)\1\)", style, flags=re.I):
            self._image(match.group(2))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._meta_capture:
            self._meta_depth -= 1
            if self._meta_depth == 0:
                self.meta[self._meta_capture] = "".join(self._meta_parts).strip()
                self._meta_capture = ""
                self._meta_parts = []

        if not self._capture:
            return
        if self._skip_depth:
            self._skip_depth -= 1
        else:
            if tag == "a" and self._link_stack:
                href = self._link_stack.pop()
                if href:
                    self._append(f"](<{href}>)")
            elif tag in {"strong", "b"}:
                self._append("**")
            elif tag in {"em", "i"}:
                self._append("*")
            elif tag == "code":
                self._append("`")
            elif tag in BLOCK_TAGS or tag == "li":
                self._newline(2)
        self._depth -= 1
        if self._depth <= 0:
            self._capture = False
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._meta_capture:
            self._meta_parts.append(data)
        if self._capture and not self._skip_depth:
            self._append(data)

    def markdown(self) -> str:
        text = html.unescape("".join(self._out))
        text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def decode_js_string(raw: str) -> str:
    try:
        decoded = ast.literal_eval("'" + raw + "'")
    except (SyntaxError, ValueError):
        decoded = bytes(raw, "utf-8").decode("unicode_escape", errors="replace")
    return html.unescape(decoded)


def js_field(segment: str, name: str) -> str:
    for match in re.finditer(rf"\b{re.escape(name)}\s*:\s*'((?:\\.|[^'\\])*)'", segment):
        value = decode_js_string(match.group(1)).strip()
        if value:
            return value
    return ""


def plain_short_content(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|div|section|li)\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\u00a0", " ").replace("\u200b", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def timestamp_from_page(page: str) -> str:
    patterns = [r'\bvar\s+ct\s*=\s*["\'](\d{9,12})["\']', r"\bori_create_time\s*:\s*['\"](\d{9,12})['\"]"]
    for pattern in patterns:
        match = re.search(pattern, page)
        if match:
            return datetime.fromtimestamp(int(match.group(1)), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M")
    return ""


def fetch_page(url: str) -> tuple[str, str]:
    request_url = url + ("&" if "?" in url else "?") + "scene=1"
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*", "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                page = response.read().decode("utf-8", errors="replace")
                final_url = response.geturl()
            if len(page) < 1000:
                raise SyncError("微信返回的页面内容异常短。")
            return page, final_url
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, SyncError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(attempt + 1)
    raise SyncError(f"连续 3 次读取微信文章失败：{last_error}")


def extract_article(page: str, requested_url: str) -> Article:
    parser = WeChatPageParser()
    parser.feed(page)
    body = parser.markdown()
    title = (parser.meta.get("activity-name") or parser.meta.get("og:title") or "").strip()
    account = parser.meta.get("js_name", "").strip()
    author = parser.meta.get("js_author_name", "").strip()
    published_at = timestamp_from_page(page)
    content_type = "article"
    images = parser.images

    if not title or (not body and not images):
        marker = page.find("window.cgiDataNew")
        if marker >= 0:
            segment = page[marker : marker + 120000]
            title = js_field(segment, "title") or title
            body = plain_short_content(js_field(segment, "content_noencode") or js_field(segment, "desc"))
            image_url = js_field(segment, "cdn_url")
            if image_url:
                image_url = normalize_image_url(image_url)
                images = [image_url]
                body = (body + "\n\n{{WECHAT_IMAGE_0000}}").strip()
            account = js_field(segment, "nick_name") or account
            author = js_field(segment, "author") or author
            raw_time = js_field(segment, "create_time")
            if raw_time.isdigit():
                published_at = datetime.fromtimestamp(int(raw_time), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M")
            content_type = "short-post"

    if not title:
        raise SyncError("没有从微信页面识别到文章标题，页面可能触发了访问验证。")
    if not body and not images:
        raise SyncError("没有从微信页面识别到正文或图片，未写入仓库。")
    if account != EXPECTED_ACCOUNT:
        shown = account or "未识别"
        raise SyncError(f"文章所属公众号为“{shown}”，不是“{EXPECTED_ACCOUNT}”，已停止以避免误收录。")

    candidate_url = parser.meta.get("og:url") or requested_url
    canonical_url = normalize_wechat_url(candidate_url)
    unique_images: list[str] = []
    for value in images:
        if value not in unique_images:
            unique_images.append(value)
    return Article(title, account, author, published_at, canonical_url, body, unique_images, content_type)


def image_extension(url: str, content_type: str = "") -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    fmt = (query.get("wx_fmt") or [""])[0].lower()
    aliases = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "svg": "svg", "bmp": "bmp"}
    if fmt in aliases:
        return aliases[fmt]
    content_type = content_type.split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(content_type) or ""
    return {".jpe": "jpg", ".jpeg": "jpg"}.get(guessed, guessed.lstrip(".")) or "bin"


def asset_stem(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def download_image(url: str, temp_dir: Path) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = response.read(50 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "")
            if not payload:
                raise ValueError("empty response")
            if len(payload) > 50 * 1024 * 1024:
                raise ValueError("image exceeds 50 MB")
            ext = image_extension(url, content_type)
            relative = Path("assets") / f"{asset_stem(url)}.{ext}"
            temp_path = temp_dir / relative.name
            temp_path.write_bytes(payload)
            return {
                "url": url,
                "local_path": relative.as_posix(),
                "bytes": len(payload),
                "content_type": content_type,
                "status": "downloaded",
                "error": "",
                "temp_path": str(temp_path),
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(attempt + 1)
    return {"url": url, "local_path": "", "bytes": 0, "content_type": "", "status": "failed", "error": last_error, "temp_path": ""}


def safe_filename(order: int, title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    cleaned = cleaned[:80].rstrip()
    return f"{order:03d} - {cleaned or f'文章 {order:03d}'}.md"


def markdown_link(path: str) -> str:
    return urllib.parse.quote(path, safe="/().-_~")


def article_markdown(article: Article, order: int, image_paths: dict[str, str]) -> str:
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    lines = [
        "---",
        f"title: {json.dumps(article.title, ensure_ascii=False)}",
        f"account: {json.dumps(article.account, ensure_ascii=False)}",
        f"author: {json.dumps(article.author, ensure_ascii=False)}",
        f"source: {json.dumps(article.canonical_url, ensure_ascii=False)}",
        f"published_at: {json.dumps(article.published_at, ensure_ascii=False)}",
        f"exported_at: {json.dumps(exported_at, ensure_ascii=False)}",
        f"order: {order}",
        'fetch_error: ""',
        'sync_method: "link-incremental"',
    ]
    if article.content_type == "short-post":
        lines.append('recovery_method: "wechat-cgiDataNew"')
    lines.extend(["tags:", "  - wechat", "  - 百味鸡ob-pluto"])
    if article.content_type == "short-post":
        lines.append("  - short-post")
    lines.extend(["---", ""])
    body = article.markdown
    for index, url in enumerate(article.image_urls):
        token = f"{{{{WECHAT_IMAGE_{index:04d}}}}}"
        body = body.replace(token, f"![](../{image_paths[url]})")
    if "{{WECHAT_IMAGE_" in body:
        raise SyncError("正文中仍有未处理的图片占位符，未写入仓库。")
    lines.extend([body.strip(), ""])
    return "\n".join(lines)


def build_readme(manifest: list[dict], assets: list[dict]) -> str:
    complete = [item for item in manifest if not item.get("fetch_error")]
    recovered = [item for item in manifest if item.get("recovery_method") == "wechat-cgiDataNew"]
    incremental = [item for item in manifest if item.get("sync_method") == "link-incremental"]
    downloaded = [item for item in assets if item.get("status") == "downloaded"]
    failed = [item for item in assets if item.get("status") != "downloaded"]
    lines = [
        "# 百味鸡 OB Pluto 微信公众号文章归档",
        "",
        "本仓库归档微信公众号“百味鸡 OB Pluto”的文章，正文由原始微信文章导出为 Markdown。",
        "",
        "## 归档状态",
        "",
        f"- 文章清单：{len(manifest)} 篇",
        f"- 正文完整：{len(complete)} 篇",
        f"- 其中由微信短内容页面恢复：{len(recovered)} 篇",
        f"- 通过一键同步新增：{len(incremental)} 篇",
        f"- 待补抓正文：{len(manifest) - len(complete)} 篇",
        f"- 已本地化图片：{len(downloaded)} 张",
        f"- 图片下载失败：{len(failed)} 张",
        "",
        "> 说明：普通图文和微信短内容使用不同页面结构；本归档已分别提取并统一为 Markdown。首次迁移详见 [MIGRATION_REPORT.md](MIGRATION_REPORT.md)。",
        "",
        "## 一键同步新文章",
        "",
        "在 Mac 上复制新文章链接，然后双击仓库中的 `一键同步.command`。程序会读取文章、下载图片、更新清单、校验并推送到 GitHub。详见 [SYNC_GUIDE.md](SYNC_GUIDE.md)。",
        "",
        "## 目录",
        "",
        "- `articles/`：文章 Markdown 文件",
        "- `assets/`：本地化图片",
        "- `data/manifest.json`：最终文章清单和微信原文链接",
        "- `data/source_manifest.json`：首次迁移前的原始导出清单",
        "- `data/assets.json`：图片下载与映射记录",
        "- `tools/`：增量同步与完整性校验脚本",
        "",
        "## 文章索引",
        "",
        "新同步文章优先显示；序号是稳定归档编号，不会因后续更新而改变。",
        "",
        "| 序号 | 标题 | 状态 | 微信原文 |",
        "| ---: | --- | --- | --- |",
    ]
    incremental_orders = {item["order"] for item in incremental}
    display_items = sorted(incremental, key=lambda item: item["order"], reverse=True)
    display_items.extend(sorted((item for item in manifest if item["order"] not in incremental_orders), key=lambda item: item["order"]))
    for item in display_items:
        article_path = markdown_link(f"articles/{item['file']}")
        title = str(item["title"]).replace("|", "\\|").replace("\n", " ")
        status = "完整" if not item.get("fetch_error") else "待补抓"
        lines.append(f"| {item['order']} | [{title}]({article_path}) | {status} | [原文]({item['url']}) |")
    lines.extend(["", "## 版权", "", "文章版权归原作者所有。除非作者另行授权，本仓库内容不附加开放内容许可。", ""])
    return "\n".join(lines)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(f"Git 操作失败（git {' '.join(args)}）：{detail}")
    return result


def ensure_publish_ready() -> None:
    branch = git("branch", "--show-current").stdout.strip()
    if branch != "main":
        raise SyncError(f"当前分支是 {branch}；一键发布只允许在 main 分支运行。")
    if git("status", "--porcelain").stdout.strip():
        raise SyncError("仓库中已有未提交改动。为避免覆盖你的内容，本次同步已停止。")
    git("pull", "--ff-only", "origin", "main")


def validate_archive() -> None:
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "validate_archive.py")], cwd=ROOT, text=True, capture_output=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        raise SyncError("归档完整性校验失败，改动没有发布。")


def duplicate_item(manifest: list[dict], url: str) -> dict | None:
    key = canonical_key(url)
    for item in manifest:
        try:
            if canonical_key(item["url"]) == key:
                return item
        except SyncError:
            continue
    return None


def sync(url: str, publish: bool, check_only: bool) -> int:
    requested_url = normalize_wechat_url(url)
    if publish:
        ensure_publish_ready()

    print("正在读取微信文章……")
    page, _ = fetch_page(requested_url)
    article = extract_article(page, requested_url)
    print(f"已识别：{article.title}")
    print(f"公众号：{article.account}；类型：{article.content_type}；图片：{len(article.image_urls)} 张")
    if check_only:
        print("链接检查通过；未修改任何文件。")
        return 0

    manifest_path = ROOT / "data" / "manifest.json"
    assets_path = ROOT / "data" / "assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    duplicate = duplicate_item(manifest, article.canonical_url)
    if duplicate:
        print(f"这篇文章已归档：#{duplicate['order']} {duplicate['title']}")
        if publish:
            ahead = git("rev-list", "--count", "origin/main..HEAD").stdout.strip()
            if ahead and int(ahead) > 0:
                print("发现此前尚未推送的本地提交，正在再次推送……")
                git("push", "origin", "main")
        print("无需重复写入。")
        return 0

    order = max((int(item["order"]) for item in manifest), default=0) + 1
    filename = safe_filename(order, article.title)
    article_path = ROOT / "articles" / filename
    existing_assets = {item["url"]: item for item in assets if item.get("status") == "downloaded"}
    image_paths: dict[str, str] = {}
    new_records: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="wechat-sync-", dir=ROOT / ".sync") as temp_name:
        temp_dir = Path(temp_name)
        targets = [url for url in article.image_urls if url not in existing_assets]
        results: dict[str, dict] = {}
        if targets:
            print(f"正在下载 {len(targets)} 张新图片……")
            with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
                futures = {pool.submit(download_image, image_url, temp_dir): image_url for image_url in targets}
                for future in as_completed(futures):
                    record = future.result()
                    results[record["url"]] = record
        failures = [record for record in results.values() if record["status"] != "downloaded"]
        if failures:
            details = "; ".join(f"{item['url']}: {item['error']}" for item in failures[:3])
            raise SyncError(f"有图片下载失败，未写入仓库：{details}")

        for image_url in article.image_urls:
            if image_url in existing_assets:
                record = existing_assets[image_url]
                if not (ROOT / record["local_path"]).exists():
                    raise SyncError(f"已有图片记录对应的文件不存在：{record['local_path']}")
                image_paths[image_url] = record["local_path"]
            else:
                record = results[image_url]
                image_paths[image_url] = record["local_path"]
                new_records.append({key: value for key, value in record.items() if key != "temp_path"})

        rendered = article_markdown(article, order, image_paths)
        temp_article = temp_dir / filename
        temp_article.write_text(rendered, encoding="utf-8")
        if len(re.sub(r"\s+", "", rendered.split("---", 2)[-1])) < 8 and not article.image_urls:
            raise SyncError("转换后的正文过短，未写入仓库。")

        item = {
            "order": order,
            "title": article.title,
            "url": article.canonical_url,
            "file": filename,
            "fetch_error": "",
            "published_at": article.published_at,
            "added_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "sync_method": "link-incremental",
            "content_type": article.content_type,
        }
        if article.content_type == "short-post":
            item["recovery_method"] = "wechat-cgiDataNew"
        new_manifest = [*manifest, item]
        new_assets = [*assets, *new_records]
        temp_manifest = temp_dir / "manifest.json"
        temp_assets = temp_dir / "assets.json"
        temp_readme = temp_dir / "README.md"
        temp_manifest.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_assets.write_text(json.dumps(new_assets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_readme.write_text(build_readme(new_manifest, new_assets), encoding="utf-8")

        backups = {path: path.read_bytes() for path in (manifest_path, assets_path, ROOT / "README.md")}
        created: list[Path] = []
        try:
            shutil.copy2(temp_article, article_path)
            created.append(article_path)
            for record in new_records:
                source = Path(results[record["url"]]["temp_path"])
                destination = ROOT / record["local_path"]
                if destination.exists() and destination.read_bytes() != source.read_bytes():
                    raise SyncError(f"图片文件名碰撞：{destination.name}")
                if not destination.exists():
                    shutil.copy2(source, destination)
                    created.append(destination)
            shutil.copy2(temp_manifest, manifest_path)
            shutil.copy2(temp_assets, assets_path)
            shutil.copy2(temp_readme, ROOT / "README.md")
            validate_archive()
        except Exception:
            for path, payload in backups.items():
                path.write_bytes(payload)
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise

    print(f"归档已更新：#{order} {article.title}")
    if publish:
        paths = [str(article_path.relative_to(ROOT)), "data/manifest.json", "data/assets.json", "README.md"]
        paths.extend(record["local_path"] for record in new_records)
        git("add", "--", *paths)
        git("commit", "-m", f"Archive WeChat article {order:03d}")
        print("正在推送到 GitHub……")
        git("push", "origin", "main")
        print("同步完成：GitHub 已更新。")
    else:
        print("本地归档已更新；未提交或推送。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="一键增量归档“百味鸡 OB Pluto”公众号文章")
    parser.add_argument("url", help="微信公众号文章链接")
    parser.add_argument("--publish", action="store_true", help="校验后提交并推送到 origin/main")
    parser.add_argument("--check", action="store_true", help="只检查链接和解析结果，不写文件")
    args = parser.parse_args()
    (ROOT / ".sync").mkdir(exist_ok=True)
    try:
        return sync(args.url, publish=args.publish, check_only=args.check)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except SyncError as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Preserve an actionable local log without exposing internals publicly.
        print(f"同步失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
