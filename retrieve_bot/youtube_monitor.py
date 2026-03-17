"""Monitor tracked YouTube channels for new videos and retrieve transcripts."""

import logging
import re
from typing import Any, Dict, List, Optional

import feedparser
import requests

from retrieve_bot.config import is_post_seen

logger = logging.getLogger(__name__)

# #region agent log
import json as _json, time as _time
from pathlib import Path as _Path
_DBG_LOG = _Path(__file__).parent.parent / "debug-f972e5.log"
def _dbg(msg, data=None, hyp="", loc=""):
    try:
        with open(_DBG_LOG, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId":"f972e5","timestamp":int(_time.time()*1000),"location":loc,"message":msg,"data":data or {},"hypothesisId":hyp}) + "\n")
    except Exception:
        pass
# #endregion

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}


def _is_short(video_id: str) -> bool:
    """Return True if *video_id* is a YouTube Short (not a regular video)."""
    try:
        resp = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            headers=HEADERS, cookies=COOKIES,
            timeout=8, allow_redirects=True,
        )
        return resp.ok and "/shorts/" in resp.url
    except Exception:
        return False


def resolve_channel_id(channel_input: str) -> Optional[str]:
    """Resolve a YouTube @handle / username / URL to a channel ID (UCxxxx)."""
    channel_input = channel_input.strip()

    if channel_input.startswith("UC") and len(channel_input) == 24:
        return channel_input

    handle = channel_input.lstrip("@").strip("/")
    url_patterns = [
        f"https://www.youtube.com/@{handle}",
        f"https://www.youtube.com/c/{handle}",
        f"https://www.youtube.com/user/{handle}",
    ]

    _uc_patterns = [
        ("rss", r'<link[^>]+type="application/rss\+xml"[^>]+href="[^"]*channel_id=(UC[a-zA-Z0-9_-]{22})'),
        ("externalId", r'"externalId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"'),
        ("meta_itemprop", r'<meta\s+itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{22})"'),
        ("canonical", r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"'),
        ("channelId", r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"'),
        ("browseId", r'"browseId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"'),
        ("channel_url", r'/channel/(UC[a-zA-Z0-9_-]{22})'),
    ]

    for url in url_patterns:
        try:
            resp = requests.get(
                url, headers=HEADERS, cookies=COOKIES,
                timeout=15, allow_redirects=True,
            )
            if resp.status_code != 200:
                # #region agent log
                _dbg("yt resolve attempt", {"url": url, "status": resp.status_code, "resp_len": len(resp.text)}, hyp="H10", loc="youtube_monitor.py:resolve")
                # #endregion
                continue
            # #region agent log
            all_matches = {}
            for name, pat in _uc_patterns:
                m = re.search(pat, resp.text)
                if m:
                    all_matches[name] = m.group(1)
            _dbg("yt resolve all_matches", {"url": url, "matches": all_matches}, hyp="H10,H11,H12,H13", loc="youtube_monitor.py:resolve")
            # #endregion
            for name, pat in _uc_patterns:
                match = re.search(pat, resp.text)
                if match:
                    return match.group(1)
        except Exception:
            continue

    return None


def get_channel_videos(channel_id: str) -> List[Dict[str, str]]:
    """Parse the YouTube RSS feed for a channel's recent long-form uploads.

    Uses the UULF (long-form) playlist so Shorts are excluded at the source,
    giving a full 15 real videos instead of a mix.  Falls back to the regular
    channel feed + per-video Shorts check if the UULF feed is empty.
    """
    suffix = channel_id[2:]
    uulf_playlist = f"UULF{suffix}"
    feed_url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={uulf_playlist}"
    feed = feedparser.parse(feed_url)

    # #region agent log
    feed_source = "UULF"
    # #endregion

    if not feed.entries:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(feed_url)
        # #region agent log
        feed_source = "channel_id_fallback"
        # #endregion

    # #region agent log
    _dbg("yt feed_source", {"channel_id": channel_id, "feed_source": feed_source, "playlist_id": uulf_playlist, "entry_count": len(feed.entries)}, hyp="H17", loc="youtube_monitor.py:get_channel_videos")
    # #endregion

    videos: List[Dict[str, str]] = []
    for entry in feed.entries:
        video_id = entry.get("yt_videoid", "")
        if not video_id:
            link = entry.get("link", "")
            m = re.search(r"v=([a-zA-Z0-9_-]{11})", link)
            video_id = m.group(1) if m else ""
        if video_id:
            videos.append(
                {
                    "video_id": video_id,
                    "title": entry.get("title", "Untitled"),
                    "link": entry.get(
                        "link",
                        f"https://www.youtube.com/watch?v={video_id}",
                    ),
                    "published": entry.get("published", ""),
                    "author": entry.get("author", ""),
                }
            )
    return videos


def get_transcript(video_id: str) -> Optional[str]:
    """Retrieve the transcript for a YouTube video (handles API v0.x and v1.x)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        try:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id)
            lines = [snippet.text for snippet in transcript]
        except (TypeError, AttributeError):
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            lines = [entry["text"] for entry in transcript]

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Transcript unavailable for %s: %s", video_id, exc)
        return None


def check_youtube_for_new_videos(channels: List[str]) -> List[Dict[str, Any]]:
    """Return metadata dicts for all unseen videos across tracked channels."""
    new_videos: List[Dict[str, Any]] = []

    for channel_input in channels:
        try:
            channel_id = resolve_channel_id(channel_input)
            # #region agent log
            _dbg("yt resolve_channel_id", {"channel_input": channel_input, "channel_id": channel_id}, hyp="H6", loc="youtube_monitor.py:check")
            # #endregion
            if not channel_id:
                logger.warning("Could not resolve YouTube channel: %s", channel_input)
                continue

            videos = get_channel_videos(channel_id)
            # #region agent log
            _seen_count = sum(1 for v in videos if is_post_seen(f"youtube_{v['video_id']}"))
            _dbg("yt channel videos", {"channel": channel_input, "total_videos": len(videos), "already_seen": _seen_count}, hyp="H6,H7", loc="youtube_monitor.py:check")
            # #endregion

            # #region agent log
            kept_videos = []
            # #endregion
            for video in videos:
                post_id = f"youtube_{video['video_id']}"
                if is_post_seen(post_id):
                    continue

                # #region agent log
                kept_videos.append({"id": video["video_id"], "title": video["title"]})
                # #endregion
                new_videos.append(
                    {
                        "id": post_id,
                        "platform": "youtube",
                        "source": channel_input,
                        "title": video["title"],
                        "subtitle": "",
                        "url": video["link"],
                        "date": video["published"],
                        "video_id": video["video_id"],
                    }
                )
            # #region agent log
            _dbg("yt result", {"channel": channel_input, "channel_id": channel_id, "kept_count": len(kept_videos), "kept_videos": kept_videos}, hyp="H17", loc="youtube_monitor.py:check")
            # #endregion
        except Exception as exc:
            # #region agent log
            _dbg("yt channel EXCEPTION", {"channel": channel_input, "error": str(exc)}, hyp="H6", loc="youtube_monitor.py:check")
            # #endregion
            logger.warning("YouTube check failed for %s: %s", channel_input, exc)

    return new_videos
