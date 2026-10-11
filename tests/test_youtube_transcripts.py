from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from blackboard.sync_models import Attachment, Page
from blackboard import archive_io
from blackboard import youtube_transcripts as yt
from blackboard import url_policy

class YouTubeUrlTests(unittest.TestCase):
    def test_video_transcript_host_allowlist(self) -> None:
        accept = (
            "www.youtube.com",
            "youtube.com",
            "youtu.be",
            "www.youtube-nocookie.com",
            "www.ted.com",
        )
        for host in accept:
            self.assertTrue(url_policy.is_video_transcript_url(f"https://{host}/x"), host)
        reject = (
            "https://example.com/watch?v=x",
            "https://learn.example.edu/",
            "ftp://www.youtube.com/watch?v=x",
            "not a url",
        )
        for url in reject:
            self.assertFalse(url_policy.is_video_transcript_url(url), url)

    def test_canonical_viewer_url_raises_for_non_youtube(self) -> None:
        for bad in ("https://example.com/x", "https://learn.example.edu/outline", ""):
            with self.assertRaises(ValueError):
                yt.canonical_viewer_url(bad)


class CaptionProjectionTests(unittest.TestCase):
    SAMPLE_VTT = (
        "WEBVTT\n\n"
        "NOTE auto-generated\n\n"
        "00:00:00.000 --> 00:00:02.560\n"
        "hi there <c.bg_green>everyone</c>\n\n"
        "00:00:02.560 --> 00:00:05.000\n"
        "welcome &amp; &#39;again&#39;\n\n"
        "00:00:06.000 --> 00:00:08.000\n"
        "second cue\n\n"
    )


    def test_safe_caption_items_drops_malformed_rows(self) -> None:
        raw = [
            {"Caption": "ok", "Time": 1, "CaptionDuration": 2},
            {"Caption": "", "Time": 3, "CaptionDuration": 1},          # dropped: empty caption
            {"Caption": "bad", "Time": "x", "CaptionDuration": 1},     # dropped: non-numeric Time
            "not a dict",                                                # dropped
            {"Caption": "extra", "Time": 4, "CaptionDuration": 1, "x": 9},  # kept, x stripped
        ]
        safe = yt.safe_caption_items(raw)
        self.assertEqual(safe, [
            {"Caption": "ok", "Time": 1.0, "CaptionDuration": 2.0},
            {"Caption": "extra", "Time": 4.0, "CaptionDuration": 1.0},
        ])


class LinkDiscoveryTests(unittest.TestCase):
    def _build_course(self, tmp: Path):
        course_root = tmp / "TEAC7116"
        pages = [
            Page('_9002_1', 'Video Lecture', None, None, 0, ['Video Lecture'], '', '',
                 attachments=[Attachment('Not a video', 'https://example.com/file.pdf'),
                              Attachment('Short video', 'https://www.youtube.com/shorts/qMmIVwyMrWE')],
                 child_links=[{'href': 'https://www.youtube.com/watch?v=qMmIVwyMrWF&t=42s&feature=share'}],
                 external_links=[{'text': '', 'href': 'https://www.youtube-nocookie.com/embed/qMmIVwyMrWA'},
                  {'text': 'Milgram', 'href': 'https://www.youtube.com/watch?v=O1VOZhwRvWo'},
                  {'href': 'https://www.youtube.com/watch?v=qMmIVwyMrWA'},
                  {'href': 'https://www.youtube-nocookie.com/embed/qMmIVwyMrWB'},
                  {'href': 'https://youtu.be/qMmIVwyMrWC'},
                  {'href': 'https://www.youtube.com/embed/qMmIVwyMrWD'},
                  {'href': 'https://www.ted.com/talks/dan_meyer_math_class_needs_a_makeover'},
                  {'href': 'https://www.ted.com/talks/dan_meyer_math_class_needs_a_makeover?utm_source=twitter'},
                  {'href': 'https://www.ted.com/talks/dan_meyer_math_class_needs_a_makeover_alt?utm_source=twitter'},
                  {'href': 'https://www.youtube.com/'},
                  {'href': 'https://www.youtube.com/channel/UC123'},
                  {'href': 'https://www.ted.com/'},
                  {'href': 'https://www.ted.com/playlists/42'},
                  {'href': 'https://example.com/watch?v=x'}]),
            Page('_9004_1', 'Video Lecture', None, None, 0, ['Video Lecture'], '', '',
                 external_links=[{'text': 'Lecture', 'href': 'https://youtu.be/qMmIVwyMrWA'}]),
        ]
        return course_root, pages

    def test_iter_youtube_links_dedupes_and_skips_non_youtube(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            course_root, pages = self._build_course(Path(tmpdir))
            links = yt.iter_youtube_links(course_root, pages=pages)
            expected = {
                video_id: f"https://www.youtube.com/watch?v={video_id}"
                for video_id in ("O1VOZhwRvWo", "qMmIVwyMrWA", "qMmIVwyMrWB", "qMmIVwyMrWC", "qMmIVwyMrWD", "qMmIVwyMrWE", "qMmIVwyMrWF")
            }
            expected.update({
                slug: f"https://www.ted.com/talks/{slug}"
                for slug in ("dan_meyer_math_class_needs_a_makeover", "dan_meyer_math_class_needs_a_makeover_alt")
            })
            self.assertEqual(len(links), len(expected))
            self.assertEqual({link.url for link in links}, set(expected.values()))
            self.assertEqual({link.video_id: link.url for link in links}, expected)
            # The duplicated nocookie/youtu.be forms collapsed onto one link.
            qmm = [link for link in links if link.video_id == "qMmIVwyMrWA"][0]
            self.assertEqual(qmm.url, "https://www.youtube.com/watch?v=qMmIVwyMrWA")


class CookieFallbackTests(unittest.TestCase):
    def test_public_caption_is_tried_before_explicit_browser_cookies(self):
        vtt = CaptionProjectionTests.SAMPLE_VTT
        meta = {
            "title": "Lecture 1", "uploader": "U", "uploader_id": "UC1",
            "duration": 3071, "upload_date": "20260101", "view_count": 5,
            "like_count": 2, "evil": "drop", "subtitles": {"en": []},
            "automatic_captions": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            link = yt.YouTubeLink(
                "https://www.youtube.com/watch?v=abc123", course_root=Path(tmpdir)
            )
            with patch.object(
                yt, "fetch_transcript", side_effect=[None, ("vtt", vtt)]
            ) as fetch:
                result = yt.save_transcripts(
                    link, meta, languages=["en"], cookies_browser="firefox"
                )
            out = Path(result["out_dir"])
            captions = json.loads((out / "captions_en.json").read_text(encoding="utf-8"))
            self.assertEqual(len(captions), 3)
            self.assertEqual(captions[0], {"Caption": "hi there everyone", "Time": 0.0, "CaptionDuration": 2.56})
            self.assertEqual(captions[1]["Caption"], "welcome & 'again'")
            self.assertEqual(captions[2]["Caption"], "second cue")
            for caption in captions:
                self.assertEqual(set(caption), {"Caption", "Time", "CaptionDuration"})
            srt = (out / "captions_en.srt").read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:02,560", srt)
            self.assertIn("hi there everyone", srt)
            md = (out / "transcript_en.md").read_text(encoding="utf-8")
            self.assertIn("# Lecture 1", md)
            self.assertIn("`00:00:00.000` hi there everyone", md)
            summary = json.loads((out / "video.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["kind"], "youtube-video-summary")
            self.assertNotIn("evil", summary["video"])
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(fetch.call_args_list[0].kwargs["cookies_browser"], None)
        self.assertEqual(fetch.call_args_list[1].kwargs["cookies_browser"], "firefox")


class TrackSelectionTests(unittest.TestCase):
    VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"

    def capture(self, meta, fetched=True, languages=None):
        with tempfile.TemporaryDirectory() as tmp:
            link = yt.YouTubeLink("https://www.youtube.com/watch?v=fixture", Path(tmp))
            with patch.object(yt, "fetch_transcript", return_value=("vtt", self.VTT) if fetched else None) as fetch:
                result = yt.save_transcripts(link, meta, languages=languages or ["en"], cookies_browser=None)
                summary = archive_io.load_json_object(Path(result["out_dir"]) / "video.json", required=True)
            return result, summary, [call.args[1] for call in fetch.call_args_list]

    def test_manual_english_variant_preferred_over_automatic(self):
        result, summary, calls = self.capture({"subtitles": {"en-GB": []}, "automatic_captions": {"en": []}})
        self.assertEqual(calls, ["en-GB"])
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(summary["caption_tracks"], [{"language": "en-GB", "source": "manual"}])

    def test_original_automatic_english_is_not_absent(self):
        result, summary, calls = self.capture({"automatic_captions": {"en-orig": [], "en": []}})
        self.assertEqual(calls, ["en-orig"])
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(summary["caption_tracks"], [{"language": "en-orig", "source": "automatic"}])

    def test_other_language_does_not_mean_video_has_no_captions(self):
        result, summary, calls = self.capture({"subtitles": {"de-DE": []}})
        self.assertEqual(result["status"], "no-captions")
        self.assertEqual(summary["caption_probe"]["reason"], "no-requested-language")
        self.assertEqual(summary["caption_probe"]["available_manual_languages"], ["de-DE"])
        self.assertEqual(calls, [])

    def test_genuinely_no_tracks_is_cached_with_probe_time(self):
        result, summary, calls = self.capture({})
        self.assertEqual(result["status"], "no-captions")
        self.assertEqual(summary["caption_probe"]["reason"], "no-tracks")
        self.assertTrue(summary["caption_probe"]["checked_at"])
        self.assertEqual(calls, [])

    def test_available_track_download_failure_is_not_absence(self):
        result, summary, calls = self.capture({"subtitles": {"en-US": []}}, fetched=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(summary["caption_probe"]["reason"], "capture-failed")
        self.assertIn("en-US", result["error"])
        self.assertEqual(calls, ["en-US"])

    def test_explicit_regional_language_stays_exact(self):
        result, summary, calls = self.capture({"subtitles": {"en-GB": [], "en-US": []}}, languages=["en-US"])
        self.assertEqual(calls, ["en-US"])
        self.assertEqual(result["status"], "downloaded")


if __name__ == "__main__":
    unittest.main()
