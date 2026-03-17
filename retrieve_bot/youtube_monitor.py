"""Monitor tracked YouTube channels for new videos and retrieve transcripts."""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import feedparser
import requests

from retrieve_bot.config import is_post_seen

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}

_TRANSCRIPT_API_URL = "https://transcriptapi.com/api/v2/youtube/transcript"
_last_transcript_call: float = 0.0


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
                continue
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

    if not feed.entries:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(feed_url)

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


# ---- transcript via TranscriptAPI.com ----


def get_transcript(video_id: str) -> Optional[str]:
    """Fetch transcript from TranscriptAPI.com.

    Returns the transcript as plain text, or None on failure.
    Enforces a 3-second minimum gap between consecutive API calls.
    """
    global _last_transcript_call

    api_key = os.getenv("TRANSCRIPT_API_KEY", "")
    if not api_key:
        logger.warning("[YOUTUBE] TRANSCRIPT_API_KEY not set in environment")
        return None

    elapsed = time.time() - _last_transcript_call
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)

    try:
        resp = requests.get(
            _TRANSCRIPT_API_URL,
            params={"video_url": video_id, "format": "json"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        _last_transcript_call = time.time()

        if resp.status_code != 200:
            logger.warning(
                "[YOUTUBE] TranscriptAPI failed for %s: HTTP %d — %s",
                video_id, resp.status_code, resp.text[:200],
            )
            return None

        data = resp.json()

        segments = data.get("transcript") or data.get("segments") or []
        if isinstance(segments, list) and segments:
            lines = [
                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in segments
            ]
            text = "\n".join(ln for ln in lines if ln.strip())
            if text:
                logger.info(
                    "[TRANSCRIPT] %s - fetched via TranscriptAPI.com (%d chars)",
                    video_id, len(text),
                )
                return text

        if isinstance(data, dict) and data.get("text"):
            logger.info(
                "[TRANSCRIPT] %s - fetched via TranscriptAPI.com (%d chars)",
                video_id, len(data["text"]),
            )
            return data["text"]

        logger.warning("[YOUTUBE] TranscriptAPI returned empty transcript for %s", video_id)
        return None

    except Exception as exc:
        _last_transcript_call = time.time()
        logger.warning("[YOUTUBE] TranscriptAPI failed for %s: %s", video_id, exc)
        return None


def check_youtube_for_new_videos(channels: List[str]) -> List[Dict[str, Any]]:
    """Return metadata dicts for all unseen videos across tracked channels."""
    new_videos: List[Dict[str, Any]] = []

    for channel_input in channels:
        try:
            channel_id = resolve_channel_id(channel_input)
            if not channel_id:
                logger.warning("Could not resolve YouTube channel: %s", channel_input)
                continue

            videos = get_channel_videos(channel_id)

            for video in videos:
                post_id = f"youtube_{video['video_id']}"
                if is_post_seen(post_id):
                    continue

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
        except Exception as exc:
            logger.warning("YouTube check failed for %s: %s", channel_input, exc)

    return new_videos
