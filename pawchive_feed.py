#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Pawchive Patreon creators.

Post discovery and post bodies come from Pawchive's documented v1 API.  Chapter
HTML is read from the same API field used by WebToEpub's PawchiveParser. Explicit
previous/next navigation and attachments are omitted; author notes remain.

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.
"""
from __future__ import annotations

import datetime as dt
import html
from html.parser import HTMLParser
import json
import os
import re
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests


BASE = "https://pawchive.pw"
API_BASE = BASE + "/api/v1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.7922.76 Safari/537.36"
)
UTC = dt.timezone.utc

FEEDS = [
    {
        "key": "cerim",
        "creator_id": "31891971",
        "fallback_name": "cerim",
    },
    {
        "key": "void-herald",
        "creator_id": "16493499",
        "fallback_name": "Void Herald",
    },
    {
        "key": "scyzim",
        "creator_id": "76415047",
        "fallback_name": "Scyzim",
    },
]

ITEM_LIMIT = max(1, int(os.environ.get("PAWCHIVE_MAX_ITEMS", "10")))
TIMEOUT = max(1, int(os.environ.get("PAWCHIVE_TIMEOUT", "30")))
RETRIES = max(0, int(os.environ.get("PAWCHIVE_RETRIES", "2")))
OUT_DIR = Path(os.environ.get("PAWCHIVE_OUT_DIR", "public"))
SITE_BASE_URL = os.environ.get("PAWCHIVE_SITE_BASE_URL", "").strip().rstrip("/")

DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template"}
VOID_TAGS = {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
KNOWN_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br",
    "caption", "cite", "code", "col", "colgroup", "data", "dd", "del", "details", "dfn",
    "div", "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav",
    "ol", "p", "picture", "pre", "q", "s", "samp", "section", "small", "source", "span",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "time", "tr", "u", "ul", "var", "wbr",
}
ALLOWED_ATTRS = {
    "alt", "class", "colspan", "datetime", "height", "href", "lang", "loading", "rel",
    "rowspan", "src", "srcset", "title", "width",
}
URL_ATTRS = {"href", "src"}

NAVIGATION_BLOCK_RE = re.compile(
    r"(?:"
    r"<blockquote\b[^>]*>\s*<p\b[^>]*>\s*"
    r"|<p\b[^>]*>\s*"
    r")"
    r"<a\b[^>]*>\s*(?:previous|next)\s+chapter\s*</a>\s*"
    r"</p>\s*(?:</blockquote>)?",
    re.I,
)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return session


def fetch_json(session: requests.Session, url: str) -> object:
    last_error = "unknown error"
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = str(exc)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_existing(session: requests.Session, url: str) -> bytes | None:
    """Fetch a published feed; only a real 404 is treated as first-run absence."""
    last_error = "unknown error"
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(f"failed to load published feed {url}: {last_error}")


def safe_url(value: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, value.strip())
    return absolute if urlparse(absolute).scheme.lower() in {"http", "https", "mailto"} else None


class ChapterSanitizer(HTMLParser):
    """Small allow-list sanitizer that escapes unknown tags as visible text."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.parts: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth:
            return
        if tag not in KNOWN_TAGS:
            self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}>"))
            return
        clean_attrs = []
        for name, value in attrs:
            name = name.lower()
            if name not in ALLOWED_ATTRS or value is None:
                continue
            if name in URL_ATTRS:
                value = safe_url(value, self.base_url)
                if value is None:
                    continue
            clean_attrs.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(clean_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            if self.drop_depth:
                self.drop_depth -= 1
            return
        if self.drop_depth:
            return
        if tag not in KNOWN_TAGS:
            self.parts.append(html.escape(f"</{tag}>"))
        elif tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self.parts)


def remove_chapter_fluff(source_html: str) -> str:
    """Remove previous/next wrappers while leaving chapter markup intact."""
    return NAVIGATION_BLOCK_RE.sub("", source_html).strip()


def build_chapter(post: dict) -> str:
    post_url = post_permalink(post)
    sanitizer = ChapterSanitizer(post_url)
    sanitizer.feed(remove_chapter_fluff(str(post.get("content") or "")))
    sanitizer.close()
    # CDATA cannot contain this delimiter. Splitting it preserves the text in XML.
    body = sanitizer.get_html()
    return body.replace("]]>", "]]]]><![CDATA[>")


def parse_date(value: object) -> dt.datetime:
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    return dt.datetime.now(UTC)


def post_permalink(post: dict) -> str:
    return f"{BASE}/{post.get('service', 'patreon')}/user/{post.get('user', '')}/post/{post.get('id', '')}"


def plain_summary(body: str, limit: int = 500) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def render_item(post: dict) -> str:
    title = str(post.get("title") or "Untitled").strip()
    link = post_permalink(post)
    published = parse_date(post.get("published") or post.get("added"))
    body = build_chapter(post)
    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(published)}</pubDate>\n"
        f"      <description>{escape(plain_summary(body))}</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_RE = re.compile(r"<guid[^>]*>(.*?)</guid>", re.S)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


def parse_rss_date(value: str) -> dt.datetime | None:
    try:
        parsed = parsedate_to_datetime(html.unescape(value).strip())
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def parse_existing_items(document: str) -> dict[str, tuple[dt.datetime, str]]:
    """Read retained item XML without rebuilding its already-rendered body."""
    items: dict[str, tuple[dt.datetime, str]] = {}
    for match in ITEM_RE.finditer(document):
        block = match.group(0).strip()
        guid_match = GUID_RE.search(block)
        link_match = LINK_RE.search(block)
        date_match = PUBDATE_RE.search(block)
        identity_match = guid_match or link_match
        if identity_match is None or date_match is None:
            continue
        published = parse_rss_date(date_match.group(1))
        if published is None:
            continue
        identity = html.unescape(identity_match.group(1)).strip()
        if identity:
            items[identity] = (published, block)
    return items


def load_existing(session: requests.Session, feed: dict) -> dict[str, tuple[dt.datetime, str]]:
    documents: list[str] = []
    local_path = OUT_DIR / feed["key"] / "feed.xml"
    if local_path.is_file():
        documents.append(local_path.read_text(encoding="utf-8"))
    if SITE_BASE_URL:
        remote = fetch_existing(session, f"{SITE_BASE_URL}/{feed['key']}/feed.xml")
        if remote is not None:
            documents.append(remote.decode("utf-8-sig"))

    items: dict[str, tuple[dt.datetime, str]] = {}
    for document in documents:
        items.update(parse_existing_items(document))
    return items


def collect_posts(session: requests.Session, creator_id: str) -> list[dict]:
    # Pawchive enforces 50-result API pages. Request only the first page, then
    # retain its ten newest posts so each build makes one request per creator.
    url = f"{API_BASE}/patreon/user/{creator_id}?o=0"
    payload = fetch_json(session, url)
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected creator-post response from {url}")
    posts_by_link = {
        post_permalink(post): post
        for post in payload
        if isinstance(post, dict) and post.get("id")
    }
    posts = list(posts_by_link.values())
    posts.sort(key=lambda post: parse_date(post.get("published") or post.get("added")), reverse=True)
    return posts[:ITEM_LIMIT]


def merge_new_posts(
    existing: dict[str, tuple[dt.datetime, str]],
    posts: list[dict],
    renderer=render_item,
) -> tuple[dict[str, tuple[dt.datetime, str]], int, int]:
    """Merge strictly newer posts, deduplicate, then evict oldest items only."""
    merged = dict(existing)
    newest_existing = max((value[0] for value in existing.values()), default=None)
    rendered = 0
    for post in posts:
        identity = post_permalink(post)
        published = parse_date(post.get("published") or post.get("added"))
        if identity in merged:
            continue
        if newest_existing is not None and published <= newest_existing:
            continue
        merged[identity] = (published, renderer(post).strip())
        rendered += 1

    ordered = sorted(merged.items(), key=lambda value: (value[1][0], value[0]), reverse=True)
    evicted = max(0, len(ordered) - ITEM_LIMIT)
    return dict(ordered[:ITEM_LIMIT]), rendered, evicted


def build_feed_xml(feed: dict, name: str, items: dict[str, tuple[dt.datetime, str]]) -> str:
    ordered = sorted(items.values(), key=lambda value: value[0], reverse=True)
    creator_url = f"{BASE}/patreon/user/{feed['creator_id']}"
    title = f"{name} — Pawchive"
    description = f"Unofficial full-text feed of {name}'s Patreon posts archived by Pawchive."
    self_link = (
        f'    <atom:link href="{escape(SITE_BASE_URL + "/" + feed["key"] + "/feed.xml")}" '
        'rel="self" type="application/rss+xml" />\n'
        if SITE_BASE_URL
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(creator_url)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{format_datetime(dt.datetime.now(UTC))}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(item for _, item in ordered)
        + "\n  </channel>\n</rss>\n"
    )


def write_feed(feed: dict, name: str, xml: str, count: int) -> None:
    directory = OUT_DIR / feed["key"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "feed.xml").write_text(xml, encoding="utf-8")
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(name)} — Pawchive RSS</title>"
        f"<h1>{html.escape(name)} — Pawchive RSS</h1>"
        "<p>Unofficial full-text feed generated from the Pawchive API.</p>"
        "<p><a href='feed.xml'>Subscribe to feed.xml</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")


def run_feed(session: requests.Session, feed: dict) -> int:
    name = feed["fallback_name"]
    print(f"[{feed['key']}] {name}")
    existing = load_existing(session, feed)
    posts = collect_posts(session, feed["creator_id"])
    items, rendered, evicted = merge_new_posts(existing, posts)
    count = len(items)
    write_feed(feed, name, build_feed_xml(feed, name, items), count)
    print(
        f"  listing items: {len(posts)}; retained: {len(existing)}; "
        f"newly rendered: {rendered}; evicted oldest: {evicted}; feed items: {count}"
    )
    return count


def main() -> int:
    session = make_session()
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    links = "".join(
        f"<li><a href='{html.escape(feed['key'])}/feed.xml'>{html.escape(feed['fallback_name'])}</a></li>"
        for feed in FEEDS
    )
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Pawchive RSS feeds</title>"
        f"<h1>Pawchive RSS feeds</h1><ul>{links}</ul>",
        encoding="utf-8",
    )
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
