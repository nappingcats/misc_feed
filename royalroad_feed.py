#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Royal Road fiction.

Royal Road's syndication feeds provide the latest chapter metadata, but their
bodies end in ``(...)``.  This builder uses those feeds for discovery, fetches
only missing chapter pages, and extracts the complete ``.chapter-content`` HTML.

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup


BASE = "https://www.royalroad.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.7922.76 Safari/537.36"
)
UTC = dt.timezone.utc
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

FEEDS = [
    {
        "key": "zenith-of-sorcery",
        "fiction_id": "71045",
        "slug": "zenith-of-sorcery",
        "title": "Zenith of Sorcery",
        "author": "nobody103",
    },
]

ITEM_LIMIT = max(1, int(os.environ.get("ROYALROAD_MAX_ITEMS", "10")))
TIMEOUT = max(1, int(os.environ.get("ROYALROAD_TIMEOUT", "30")))
RETRIES = max(0, int(os.environ.get("ROYALROAD_RETRIES", "2")))
REQUEST_DELAY = max(0.0, float(os.environ.get("ROYALROAD_REQUEST_DELAY", "1")))
OUT_DIR = Path(os.environ.get("ROYALROAD_OUT_DIR", "public"))
SITE_BASE_URL = os.environ.get("ROYALROAD_SITE_BASE_URL", "").strip().rstrip("/")

DROP_TAGS = {"script", "style", "iframe", "object", "embed", "template", "form"}
ALLOWED_ATTRS = {
    "alt", "colspan", "datetime", "height", "href", "lang", "loading", "rel",
    "rowspan", "src", "title", "width",
}
URL_ATTRS = {"href", "src"}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return session


def fetch(session: requests.Session, url: str, *, optional: bool = False) -> requests.Response | None:
    last_error = "unknown error"
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            if optional and response.status_code == 404:
                return None
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fiction_url(feed: dict) -> str:
    return f"{BASE}/fiction/{feed['fiction_id']}/{feed['slug']}"


def syndication_url(feed: dict) -> str:
    return f"{BASE}/fiction/syndication/{feed['fiction_id']}"


def parse_date(value: str | None) -> dt.datetime:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError):
            pass
    return dt.datetime.now(UTC)


def parse_syndication(document: bytes, feed: dict) -> list[dict]:
    root = ET.fromstring(document.lstrip(b"\xef\xbb\xbf"))
    chapters = []
    title_prefix = feed["title"] + " - "
    for item in root.findall("./channel/item")[:ITEM_LIMIT]:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        title = (item.findtext("title") or "Untitled").strip()
        if title.startswith(title_prefix):
            title = title[len(title_prefix):]
        chapters.append(
            {
                "title": title,
                "link": link,
                "guid": (item.findtext("guid") or link).strip(),
                "date": parse_date(item.findtext("pubDate")),
            }
        )
    return chapters


def safe_url(value: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, value.strip())
    return absolute if urlparse(absolute).scheme.lower() in {"http", "https", "mailto"} else None


def extract_chapter(page: str, chapter_url: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    chapter_inner = soup.select_one("div.chapter-inner")
    if chapter_inner is None:
        raise RuntimeError(f"chapter content not found at {chapter_url}")

    # Royal Road inserts anti-scraping watermarks as ordinary elements whose
    # generated class is hidden by a page-local CSS rule.  WebToEpub discovers
    # those display:none selectors and removes the matching elements.
    hidden_classes: set[str] = set()
    for style in soup.find_all("style"):
        css = style.get_text(" ", strip=False)
        hidden_classes.update(
            re.findall(r"\.([A-Za-z_][\w-]*)\s*\{[^}]*display\s*:\s*none\b", css, re.I | re.S)
        )

    wrapper = BeautifulSoup("<div></div>", "html.parser").div
    assert wrapper is not None
    wrapper.append(chapter_inner.extract())

    for note in wrapper.select("div.author-note-portlet, div.author-note"):
        note.decompose()
    for class_name in hidden_classes:
        for watermark in wrapper.select(f".{class_name}"):
            watermark.decompose()
    for tag in wrapper.find_all(DROP_TAGS):
        tag.decompose()
    for image in wrapper.find_all("img"):
        if not image.get("src"):
            image.decompose()
    for link in wrapper.find_all("a"):
        label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).lower()
        href = str(link.get("href") or "").lower()
        if "www.royalroadl.com" in href or label in {"previous chapter", "next chapter", "previous", "next"}:
            link.decompose()
            continue
    for text_node in list(wrapper.find_all(string=True)):
        if text_node.strip() == "<-->":
            text_node.extract()

    for tag in wrapper.find_all(True):
        for attr in list(tag.attrs):
            name = attr.lower()
            if name.startswith("on") or name not in ALLOWED_ATTRS:
                del tag.attrs[attr]
                continue
            value = tag.attrs.get(attr)
            if name in URL_ATTRS and isinstance(value, str):
                cleaned = safe_url(value, chapter_url)
                if cleaned is None:
                    del tag.attrs[attr]
                else:
                    tag.attrs[attr] = cleaned

    body = wrapper.decode_contents(formatter="html").strip()
    if not re.sub(r"<[^>]+>", "", body).strip():
        raise RuntimeError(f"empty chapter content at {chapter_url}")
    return body


def plain_summary(body: str, limit: int = 500) -> str:
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def render_item(chapter: dict) -> str:
    body = chapter["body_html"]
    cdata_body = body.replace("]]>", "]]]]><![CDATA[>")
    return (
        "    <item>\n"
        f"      <title>{escape(chapter['title'])}</title>\n"
        f"      <link>{escape(chapter['link'])}</link>\n"
        f"      <guid isPermaLink=\"false\">{escape(chapter['guid'])}</guid>\n"
        f"      <pubDate>{format_datetime(chapter['date'])}</pubDate>\n"
        f"      <description>{escape(plain_summary(body))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata_body}]]></content:encoded>\n"
        "    </item>"
    )


def load_existing(session: requests.Session, feed: dict) -> dict[str, dict]:
    documents: list[bytes] = []
    local_path = OUT_DIR / feed["key"] / "feed.xml"
    if local_path.is_file():
        documents.append(local_path.read_bytes())
    if SITE_BASE_URL:
        response = fetch(session, f"{SITE_BASE_URL}/{feed['key']}/feed.xml", optional=True)
        if response is not None:
            documents.append(response.content)

    chapters: dict[str, dict] = {}
    for document in documents:
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            continue
        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or "").strip()
            body = item.findtext(f"{{{CONTENT_NS}}}encoded") or ""
            date = parse_date(item.findtext("pubDate"))
            if link and body:
                chapters[link] = {
                    "title": (item.findtext("title") or "Untitled").strip(),
                    "link": link,
                    "guid": (item.findtext("guid") or link).strip(),
                    "date": date,
                    "body_html": body,
                }
    return chapters


def merge_new_chapters(
    existing: dict[str, dict],
    listing: list[dict],
    body_loader,
) -> tuple[list[dict], int, int]:
    """Merge strictly newer chapters and evict only the oldest at the cap."""
    merged = dict(existing)
    known_guids = {chapter["guid"] for chapter in existing.values()}
    newest_existing = max((chapter["date"] for chapter in existing.values()), default=None)
    fetched = 0
    for chapter in listing:
        if chapter["link"] in merged or chapter["guid"] in known_guids:
            continue
        if newest_existing is not None and chapter["date"] <= newest_existing:
            continue
        new_chapter = dict(chapter)
        new_chapter["body_html"] = body_loader(new_chapter)
        merged[new_chapter["link"]] = new_chapter
        known_guids.add(new_chapter["guid"])
        fetched += 1

    ordered = sorted(
        merged.values(),
        key=lambda chapter: (chapter["date"], chapter["guid"]),
        reverse=True,
    )
    evicted = max(0, len(ordered) - ITEM_LIMIT)
    return ordered[:ITEM_LIMIT], fetched, evicted


def build_feed_xml(feed: dict, chapters: list[dict]) -> str:
    now = format_datetime(dt.datetime.now(UTC))
    self_link = (
        f'    <atom:link href="{escape(SITE_BASE_URL + "/" + feed["key"] + "/feed.xml")}" '
        'rel="self" type="application/rss+xml" />\n'
        if SITE_BASE_URL
        else ""
    )
    description = f"Unofficial full-text feed of {feed['title']} by {feed['author']} on Royal Road."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(feed['title'])} — Royal Road</title>\n"
        f"    <link>{escape(fiction_url(feed))}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(render_item(chapter) for chapter in chapters)
        + "\n  </channel>\n</rss>\n"
    )


def write_feed(feed: dict, xml: str, count: int) -> None:
    directory = OUT_DIR / feed["key"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "feed.xml").write_text(xml, encoding="utf-8")
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(feed['title'])} — Royal Road RSS</title>"
        f"<h1>{html.escape(feed['title'])} — Royal Road RSS</h1>"
        "<p>Unofficial full-text feed generated from Royal Road.</p>"
        "<p><a href='feed.xml'>Subscribe to feed.xml</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")


def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}] {feed['title']}")
    existing = load_existing(session, feed)
    listing = fetch(session, syndication_url(feed))
    assert listing is not None
    chapters = parse_syndication(listing.content, feed)
    if not chapters:
        raise RuntimeError(f"no chapters found for {feed['title']}")

    detail_fetches = 0

    def load_body(chapter: dict) -> str:
        nonlocal detail_fetches
        if detail_fetches:
            time.sleep(REQUEST_DELAY)
        response = fetch(session, chapter["link"])
        assert response is not None
        detail_fetches += 1
        return extract_chapter(response.text, chapter["link"])

    merged, fetched, evicted = merge_new_chapters(existing, chapters, load_body)
    write_feed(feed, build_feed_xml(feed, merged), len(merged))
    print(
        f"  listing items: {len(chapters)}; retained: {len(existing)}; "
        f"newly fetched/rendered: {fetched}; evicted oldest: {evicted}; feed items: {len(merged)}"
    )
    return len(merged)


def main() -> int:
    session = make_session()
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
