from pathlib import Path
import importlib.util
import json
import unittest
from unittest.mock import patch

try:
    from yt_dlp.extractor.ted import TedTalkIE
except ImportError:
    TedTalkIE = None


@unittest.skipIf(TedTalkIE is None, "Run with the installed yt-dlp interpreter to test its plugin contract")
class TedSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts/blackboard/ytdlp-plugins/ted-schema/yt_dlp_plugins/extractor/ted_schema.py"
        spec = importlib.util.spec_from_file_location("blackboard_ted_schema_test", path)
        cls.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.plugin)

    def bridge(self, video):
        data = {"props": {"pageProps": {"videoData": video}}}
        with patch.object(TedTalkIE, "_search_nextjs_data", return_value=data):
            return self.plugin.TedTalkSchemaIE()._search_nextjs_data("fixture", "855")["props"]["pageProps"]["videoData"]

    def test_current_player_object_is_adapted_for_old_extractor(self):
        player = {"resources": {"h264": []}, "languages": [{"languageCode": "en"}]}
        result = self.bridge({"id": "855", "videoPlayerData": player})
        self.assertEqual(json.loads(result["playerData"]), player)
        self.assertEqual(result["videoPlayerData"], player)

    def test_existing_legacy_player_data_remains_unchanged(self):
        old = '{"resources": {"h264": []}}'
        self.assertEqual(self.bridge({"playerData": old, "videoPlayerData": {"different": True}})["playerData"], old)

    def test_missing_or_invalid_current_player_is_not_fabricated(self):
        for current in (None, [], "not-a-player"):
            with self.subTest(current=current):
                result = self.bridge({"videoPlayerData": current})
                self.assertNotIn("playerData", result)
