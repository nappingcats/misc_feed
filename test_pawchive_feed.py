import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import pawchive_feed as feed


class PawchiveFeedTests(unittest.TestCase):
    @staticmethod
    def make_post(post_id, day):
        return {
            "id": str(post_id),
            "user": "34",
            "service": "patreon",
            "title": f"Chapter {post_id}",
            "content": f"<p>Body {post_id}</p>",
            "published": f"2026-08-{day:02d}T10:00:00",
            "attachments": [],
        }

    def test_feed_definitions_are_unique(self):
        self.assertEqual(3, len(feed.FEEDS))
        self.assertEqual(3, len({item["key"] for item in feed.FEEDS}))
        self.assertEqual(3, len({item["creator_id"] for item in feed.FEEDS}))

    def test_build_chapter_keeps_only_chapter_body(self):
        post = {
            "id": "12",
            "user": "34",
            "service": "patreon",
            "content": (
                '<blockquote><p><a href="https://example.com/previous">Previous Chapter</a></p></blockquote>'
                '<p>Hello <em>world</em>. <a href="/map">Map</a></p>'
                '<script>bad()</script><foo>x</foo>'
                '<p>***</p><p><em>Author Note: buy the book.</em></p>'
                '<p><a href="https://example.com/next">Next chapter</a></p>'
            ),
            "attachments": [
                {"name": "cover.JPG", "path": "/aa/cover.jpg"},
                {"name": "chapter.epub", "path": "bb/chapter.epub"},
            ],
        }
        body = feed.build_chapter(post)
        self.assertIn("<p>Hello <em>world</em>.", body)
        self.assertNotIn("bad()", body)
        self.assertIn("&lt;foo&gt;x&lt;/foo&gt;", body)
        self.assertIn('href="https://pawchive.pw/map"', body)
        self.assertNotIn("Previous Chapter", body)
        self.assertNotIn("Next chapter", body)
        self.assertIn("Author Note: buy the book.", body)
        self.assertIn("***", body)
        self.assertNotIn("Attachments", body)
        self.assertNotIn("cover.jpg", body)
        self.assertNotIn("chapter.epub", body)

    def test_feed_contains_full_body(self):
        post = {
            "id": "12",
            "user": "34",
            "service": "patreon",
            "title": "Chapter 12",
            "content": "<p>The complete chapter.</p>",
            "published": "2026-08-01T10:30:00",
            "attachments": [],
        }
        rendered = feed.render_item(post)
        items = {feed.post_permalink(post): (dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), rendered)}
        xml = feed.build_feed_xml(feed.FEEDS[0], "Author", items)
        root = ET.fromstring(xml)
        encoded = root.find("./channel/item/{http://purl.org/rss/1.0/modules/content/}encoded")
        self.assertIsNotNone(encoded)
        self.assertEqual("<p>The complete chapter.</p>", encoded.text)

    def test_noop_merge_does_not_render_existing_posts(self):
        posts = [self.make_post(i, i) for i in range(1, 4)]
        existing = {
            feed.post_permalink(post): (
                feed.parse_date(post["published"]),
                feed.render_item(post),
            )
            for post in posts
        }
        renderer = Mock(side_effect=feed.render_item)
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, rendered, evicted = feed.merge_new_posts(existing, list(reversed(posts)), renderer)
        self.assertEqual(existing, merged)
        self.assertEqual((0, 0), (rendered, evicted))
        renderer.assert_not_called()

    def test_one_new_post_evicts_only_oldest_and_deduplicates(self):
        old_posts = [self.make_post(i, i) for i in range(1, 4)]
        new_post = self.make_post(4, 4)
        stale_post = self.make_post(99, 1)
        existing = {
            feed.post_permalink(post): (
                feed.parse_date(post["published"]),
                feed.render_item(post),
            )
            for post in old_posts
        }
        renderer = Mock(side_effect=feed.render_item)
        listing = [new_post, new_post, old_posts[-1], stale_post]
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, rendered, evicted = feed.merge_new_posts(existing, listing, renderer)
        self.assertEqual((1, 1), (rendered, evicted))
        self.assertEqual(
            {feed.post_permalink(post) for post in (old_posts[1], old_posts[2], new_post)},
            set(merged),
        )
        renderer.assert_called_once_with(new_post)

    def test_existing_item_xml_round_trips_without_duplication(self):
        post = self.make_post(7, 7)
        identity = feed.post_permalink(post)
        original = feed.render_item(post)
        document = f"<rss><channel>{original}</channel></rss>"
        parsed = feed.parse_existing_items(document)
        self.assertEqual([identity], list(parsed))
        self.assertEqual(original.strip(), parsed[identity][1])


if __name__ == "__main__":
    unittest.main()
