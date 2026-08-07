import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import royalroad_feed as feed


class RoyalRoadFeedTests(unittest.TestCase):
    def test_feed_definitions_are_unique(self):
        self.assertEqual(1, len(feed.FEEDS))
        self.assertEqual(1, len({item["key"] for item in feed.FEEDS}))
        self.assertEqual(1, len({item["fiction_id"] for item in feed.FEEDS}))

    @staticmethod
    def make_chapter(chapter_id, day, *, body=True, link_id=None):
        chapter = {
            "title": f"Chapter {chapter_id}",
            "link": f"https://www.royalroad.com/fiction/chapter/{link_id or chapter_id}",
            "guid": str(chapter_id),
            "date": dt.datetime(2026, 8, day, tzinfo=dt.timezone.utc),
        }
        if body:
            chapter["body_html"] = f"<p>Body {chapter_id}</p>"
        return chapter

    def test_parse_syndication_limits_and_strips_fiction_title(self):
        items = "".join(
            f"<item><title>Zenith of Sorcery - Chapter {i}</title>"
            f"<link>https://www.royalroad.com/fiction/chapter/{i}</link>"
            f"<guid>{i}</guid><pubDate>Tue, 04 Aug 2026 21:00:16 GMT</pubDate></item>"
            for i in range(12)
        )
        document = f"<rss><channel>{items}</channel></rss>".encode()
        chapters = feed.parse_syndication(document, feed.FEEDS[0])
        self.assertEqual(10, len(chapters))
        self.assertEqual("Chapter 0", chapters[0]["title"])

    def test_extract_chapter_uses_only_chapter_content(self):
        page = """
        <html><head><style>.watermark { display: none; }</style></head><body>
          <nav><a href='/previous'>Previous Chapter</a></nav>
          <div class='content-parent'>
            <div class='chapter-inner chapter-content'>
              <p class='random' onclick='bad()'>Full <em>chapter</em>.</p>
              <span class='watermark'>Read this only on Royal Road.</span>
              <p><a href='/map'>Map</a></p>
              <p><a href='/next'>Next Chapter</a></p>&lt;--&gt;
              <img alt='empty'>
              <div class='author-note'>Nested author note.</div>
              <script>bad()</script>
            </div>
            <div class='author-note-portlet'><div class='author-note'>Author note.</div></div>
          </div>
          <div class='comments'>Not chapter content</div>
        </body></html>
        """
        body = feed.extract_chapter(page, "https://www.royalroad.com/fiction/chapter/1")
        self.assertIn("Full <em>chapter</em>.", body)
        self.assertIn('href="https://www.royalroad.com/map"', body)
        self.assertNotIn("Previous Chapter", body)
        self.assertNotIn("Next Chapter", body)
        self.assertNotIn("&lt;--&gt;", body)
        self.assertNotIn("Read this only on Royal Road", body)
        self.assertNotIn("Not chapter content", body)
        self.assertNotIn("Author note.", body)
        self.assertNotIn("Nested author note.", body)
        self.assertNotIn("onclick", body)
        self.assertNotIn("bad()", body)
        self.assertNotIn("empty", body)

    def test_rendered_feed_contains_full_body(self):
        chapter = {
            "title": "Chapter 1",
            "link": "https://www.royalroad.com/fiction/chapter/1",
            "guid": "1",
            "date": feed.parse_date("Tue, 04 Aug 2026 21:00:16 GMT"),
            "body_html": "<p>Complete chapter.</p>",
        }
        xml = feed.build_feed_xml(feed.FEEDS[0], [chapter])
        root = ET.fromstring(xml)
        body = root.find(f"./channel/item/{{{feed.CONTENT_NS}}}encoded")
        self.assertEqual("<p>Complete chapter.</p>", body.text)

    def test_noop_merge_does_not_fetch_existing_chapters(self):
        chapters = [self.make_chapter(i, i) for i in range(1, 4)]
        existing = {chapter["link"]: chapter for chapter in chapters}
        loader = Mock()
        listing = [{key: value for key, value in chapter.items() if key != "body_html"} for chapter in chapters]
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, fetched, evicted = feed.merge_new_chapters(existing, listing, loader)
        self.assertEqual(chapters[::-1], merged)
        self.assertEqual((0, 0), (fetched, evicted))
        loader.assert_not_called()

    def test_one_new_chapter_evicts_only_oldest_and_deduplicates_guid(self):
        chapters = [self.make_chapter(i, i) for i in range(1, 4)]
        existing = {chapter["link"]: chapter for chapter in chapters}
        new_chapter = self.make_chapter(4, 4, body=False)
        duplicate_guid = self.make_chapter(3, 4, body=False, link_id=300)
        stale = self.make_chapter(99, 1, body=False)
        loader = Mock(return_value="<p>Body 4</p>")
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, fetched, evicted = feed.merge_new_chapters(
                existing,
                [new_chapter, new_chapter, duplicate_guid, stale],
                loader,
            )
        self.assertEqual((1, 1), (fetched, evicted))
        self.assertEqual(["4", "3", "2"], [chapter["guid"] for chapter in merged])
        loader.assert_called_once()
        self.assertEqual("4", loader.call_args.args[0]["guid"])


if __name__ == "__main__":
    unittest.main()
