"""Monitor tracked YouTube channels for new videos and retrieve transcripts."""

import logging
import re
from typing import Any, Dict, List, Optional

import feedparser
import requests

from retrieve_bot.config import is_post_seen

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.77 Safari/537.36"
    )
}


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

    for url in url_patterns:
        try:
            resp = requests.get(
                url, headers=HEADERS, timeout=15, allow_redirects=True
            )
            if resp.status_code != 200:
                continue
            match = re.search(
                r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"', resp.text
            )
            if match:
                return match.group(1)
            match = re.search(
                r'<meta\s+itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{22})"',
                resp.text,
            )
            if match:
                return match.group(1)
        except Exception:
            continue

    return None


def get_channel_videos(channel_id: str) -> List[Dict[str, str]]:
    """Parse the YouTube RSS feed for a channel's recent uploads."""
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
            if not channel_id:
                logger.warning("Could not resolve YouTube channel: %s", channel_input)
                continue

            for video in get_channel_videos(channel_id):
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
