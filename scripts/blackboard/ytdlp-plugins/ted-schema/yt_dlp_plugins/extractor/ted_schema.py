"""Scoped bridge for TED's playerData -> videoPlayerData schema transition.

Keep yt-dlp's extraction, HLS subtitle handling and validation. Remove this
bridge when the installed extractor uses videoPlayerData directly.
"""
import json
from yt_dlp.extractor.ted import TedTalkIE


class TedTalkSchemaIE(TedTalkIE, plugin_name="blackboard_ted_schema"):
    def _search_nextjs_data(self, *args, **kwargs):
        data = super()._search_nextjs_data(*args, **kwargs)
        video = ((data.get("props") or {}).get("pageProps") or {}).get("videoData")
        if (isinstance(video, dict) and video.get("playerData") is None
                and isinstance(video.get("videoPlayerData"), dict)):
            video["playerData"] = json.dumps(video["videoPlayerData"])
        return data
