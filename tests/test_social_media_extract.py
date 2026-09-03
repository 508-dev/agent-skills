from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills/social-media-extract"
SCRIPT = SKILL_DIR / "scripts/social_media_extract.py"
LAUNCHER = SKILL_DIR / "scripts/social-media-extract"
SKILL = SKILL_DIR / "SKILL.md"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("social_media_extract_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SocialMediaExtractPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.command = load_module()

    def test_skill_is_installable_and_uses_the_locked_launcher(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        launcher = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("name: social-media-extract", skill)
        self.assertIn("--scrape-only", skill)
        self.assertIn("--download-media", skill)
        self.assertIn("--locked", launcher)
        self.assertIn("runtime", launcher)
        self.assertTrue(LAUNCHER.stat().st_mode & 0o111)
        self.assertTrue((SKILL_DIR / "runtime/uv.lock").is_file())
        self.assertTrue((SKILL_DIR / "runtime/requirements.lock").is_file())

    def test_normalizes_supported_instagram_and_facebook_targets(self) -> None:
        instagram = self.command.normalize_social_url(
            "https://www.instagram.com/someone/reels/C0de_Test/?utm_source=ig"
        )
        facebook_reel = self.command.normalize_social_url(
            "https://m.facebook.com/reel/897621193261763?mibextid=abc"
        )
        facebook_post = self.command.normalize_social_url(
            "https://www.facebook.com/jwang815/posts/10118723421345343/?comment_id=1"
        )
        facebook_share = self.command.normalize_social_url("https://www.facebook.com/share/Abcd_1234/")

        self.assertEqual(instagram.platform, "instagram")
        self.assertEqual(instagram.canonical_url, "https://www.instagram.com/reel/C0de_Test/")
        self.assertEqual(facebook_reel.platform, "facebook")
        self.assertEqual(facebook_reel.canonical_url, "https://www.facebook.com/reel/897621193261763")
        self.assertEqual(facebook_post.kind, "post")
        self.assertEqual(
            facebook_post.canonical_url,
            "https://www.facebook.com/jwang815/posts/10118723421345343/",
        )
        self.assertEqual(facebook_share.kind, "share")
        self.assertEqual(facebook_share.canonical_url, "https://www.facebook.com/share/Abcd_1234/")

    def test_parses_public_facebook_caption_and_media(self) -> None:
        target = self.command.normalize_social_url("https://www.facebook.com/reel/897621193261763")
        video_url = "https://video-nrt1-2.xx.fbcdn.net/reel.mp4?token=signed"
        source = (
            '<meta property="og:title" content="87K views · 12K reactions | Ramen One&#10;Tokyo, Japan | Mr.Tokyo">'
            '<meta property="og:description" content="Ramen One...">'
            '<meta property="og:image" content="https://scontent.xx.fbcdn.net/cover.jpg">'
            f"<script>{json.dumps({'browser_native_hd_url': video_url})}</script>"
        )

        post = self.command.parse_social_html(source, target)

        self.assertEqual(post.caption, "Ramen One\nTokyo, Japan")
        self.assertEqual(post.username, "Mr.Tokyo")
        self.assertEqual(post.thumbnail_url, "https://scontent.xx.fbcdn.net/cover.jpg")
        self.assertEqual(post.video_url, video_url)

    def test_parses_public_facebook_post_description_and_image(self) -> None:
        target = self.command.normalize_social_url(
            "https://www.facebook.com/jwang815/posts/10118723421345343/"
        )
        source = (
            '<meta property="og:title" content="Jason Wang">'
            '<meta property="og:description" content="[Japan - Kyoto] Noodle Shop Rennosuke (麺屋 練之助).">'
            '<meta property="og:image" content="https://scontent.xx.fbcdn.net/cover.jpg">'
        )

        post = self.command.parse_social_html(source, target)

        self.assertEqual(post.username, "Jason Wang")
        self.assertEqual(post.caption, "[Japan - Kyoto] Noodle Shop Rennosuke (麺屋 練之助).")
        self.assertEqual(post.image_urls, ["https://scontent.xx.fbcdn.net/cover.jpg"])
        self.assertEqual(post.content_type, "image")

    def test_maps_urls_are_deterministic_search_links(self) -> None:
        post = self.command.ScrapedPost(
            source_url="https://www.instagram.com/p/C0de_Test/",
            shortcode="C0de_Test",
        )

        places = self.command.maps_places(
            {
                "overall_region": "Tokyo, Japan",
                "places": [
                    {"name": "Sushi Dai", "region": None, "evidence": "caption"},
                    {"name": "Sushi Dai", "region": "Tokyo, Japan", "evidence": "duplicate"},
                ],
            },
            post,
        )

        self.assertEqual(len(places), 1)
        self.assertEqual(
            places[0]["maps_url"],
            "https://www.google.com/maps/search/?api=1&query=Sushi%20Dai%2C%20Tokyo%2C%20Japan",
        )
        self.assertNotIn("places.googleapis.com", SCRIPT.read_text(encoding="utf-8"))

    def test_browser_manager_is_opt_in_and_cache_is_skill_specific(self) -> None:
        args = self.command.parse_args(["https://www.instagram.com/p/C0de_Test/"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.command.resolve_browser_manager_config(args))
            with self.assertRaises(self.command.InstagramToMapsError):
                self.command.resolve_browser_manager_config(args, require=True)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SOCIAL_MEDIA_EXTRACT_CACHE_DIR": directory}, clear=False):
                cache = self.command.media_cache_directory()
            self.assertTrue(cache.is_dir())
            self.assertEqual(cache.parent, Path(directory))

    def test_media_handoff_refers_to_the_bundled_launcher(self) -> None:
        post = self.command.ScrapedPost(
            source_url="https://www.instagram.com/p/C0de_Test/",
            shortcode="C0de_Test",
        )

        media = self.command.media_handoff(post, max_images=8)

        self.assertEqual(media["download_command"][0], str(LAUNCHER))
        self.assertIn("--download-media", media["download_command"])


if __name__ == "__main__":
    unittest.main()
