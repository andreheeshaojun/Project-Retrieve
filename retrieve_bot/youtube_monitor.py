"""Monitor tracked YouTube channels for new videos and retrieve transcripts."""

import json
import logging
import re
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import feedparser
import requests
from bs4 import BeautifulSoup

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


_COOKIES_PATH = Path(__file__).parent.parent / "data" / "youtube_cookies.txt"


# ---- transcript fallback chain ----


def get_transcript(video_id: str) -> Optional[str]:
    """Retrieve transcript via a three-level fallback chain.

    1. youtube_transcript_api + cookies
    2. Direct YouTube page scraping (extract captionTracks → fetch XML)
    3. youtubetotranscript.com (best-effort HTTP scrape)

    Returns the transcript as plain text, or None if all methods fail.
    """
    text = _transcript_via_api(video_id)
    if text:
        logger.info("[YOUTUBE] Transcript OK via API for %s (%d chars)", video_id, len(text))
        return text

    text = _transcript_via_page_scrape(video_id)
    if text:
        logger.info("[YOUTUBE] Transcript OK via page scrape for %s (%d chars)", video_id, len(text))
        return text

    text = _transcript_via_web_fallback(video_id)
    if text:
        logger.info("[YOUTUBE] Transcript OK via web fallback for %s (%d chars)", video_id, len(text))
        return text

    logger.warning("[YOUTUBE] No transcript available for %s", video_id)
    return None


def _transcript_via_api(video_id: str) -> Optional[str]:
    """Method 1: youtube_transcript_api with optional cookie authentication.

    Supports both v1.x (instance-based) and v0.x (class-method) APIs.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        cookie_path = str(_COOKIES_PATH) if _COOKIES_PATH.exists() else None

        # v1.x API: cookies in constructor, instance .fetch()
        try:
            api = YouTubeTranscriptApi(cookies=cookie_path) if cookie_path else YouTubeTranscriptApi()
            transcript = api.fetch(video_id)
            lines = [snippet.text for snippet in transcript]
            text = "\n".join(lines)
            return text if text.strip() else None
        except TypeError:
            pass

        # v0.x API: class method, cookies as keyword arg
        kwargs = {"cookies": cookie_path} if cookie_path else {}
        transcript = YouTubeTranscriptApi.get_transcript(video_id, **kwargs)
        lines = [entry["text"] for entry in transcript]
        text = "\n".join(lines)
        return text if text.strip() else None
    except Exception as exc:
        logger.info("[YOUTUBE] API method failed for %s: %s", video_id, exc)
        return None


def _transcript_via_page_scrape(video_id: str) -> Optional[str]:
    """Method 2: fetch YouTube video page, extract caption track URL, fetch XML."""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=20)
        if resp.status_code != 200:
            logger.info("[YOUTUBE] Page scrape: HTTP %d for %s", resp.status_code, video_id)
            return None

        html = resp.text

        # Strategy A: parse ytInitialPlayerResponse JSON blob
        base_url = _extract_caption_url_from_player_response(html, video_id)

        # Strategy B: regex for captionTracks directly in raw HTML
        if not base_url:
            base_url = _extract_caption_url_from_raw_html(html, video_id)

        if not base_url:
            has_player = "ytInitialPlayerResponse" in html
            has_consent = "consent.youtube.com" in html
            logger.info(
                "[YOUTUBE] Page scrape: no caption URL for %s "
                "(has_player=%s, consent_page=%s, page_len=%d)",
                video_id, has_player, has_consent, len(html),
            )
            return None

        cap_resp = requests.get(base_url, headers=HEADERS, timeout=15)
        if cap_resp.status_code != 200:
            logger.info("[YOUTUBE] Page scrape: caption fetch HTTP %d for %s", cap_resp.status_code, video_id)
            return None

        root = ElementTree.fromstring(cap_resp.text)
        lines = [unescape(elem.text) for elem in root.iter("text") if elem.text]
        return "\n".join(lines) if lines else None
    except Exception as exc:
        logger.info("[YOUTUBE] Page scrape failed for %s: %s", video_id, exc)
        return None


def _extract_caption_url_from_player_response(html: str, video_id: str) -> Optional[str]:
    """Parse ytInitialPlayerResponse JSON to find an English caption track URL."""
    match = re.search(r'ytInitialPlayerResponse\s*=\s*', html)
    if not match:
        return None
    try:
        decoder = json.JSONDecoder()
        player_resp, _ = decoder.raw_decode(html, match.end())
    except (json.JSONDecodeError, ValueError):
        return None

    tracks = (
        player_resp
        .get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    if not tracks:
        status = player_resp.get("playabilityStatus", {}).get("status", "?")
        logger.info("[YOUTUBE] Page scrape: playerResponse has no captions (status=%s) for %s", status, video_id)
        return None

    track = next(
        (t for t in tracks if t.get("languageCode", "").startswith("en")),
        tracks[0],
    )
    return track.get("baseUrl")


def _extract_caption_url_from_raw_html(html: str, video_id: str) -> Optional[str]:
    """Regex fallback: find a timedtext baseUrl directly in the page HTML."""
    match = re.search(
        r'"baseUrl"\s*:\s*"(https://www\.youtube\.com/api/timedtext[^"]+)"',
        html,
    )
    if not match:
        return None
    raw_url = match.group(1).replace("\\u0026", "&")
    if "lang=en" in raw_url or "lang=a.en" in raw_url:
        return raw_url
    # Accept any language if English not found
    return raw_url


def _transcript_via_web_fallback(video_id: str) -> Optional[str]:
    """Method 3: best-effort scrape of youtubetotranscript.com.

    The site is primarily JS-rendered, so this only works if the server
    includes transcript data in the initial HTML (e.g. via SSR/Next.js
    __NEXT_DATA__).  Fails gracefully if the content is client-rendered.

    Constraints: no documented rate limits for the public site, but heavy
    automated use may be blocked. This is a last-resort fallback.
    """
    _BROWSER_HEADERS = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://youtubetotranscript.com/",
        "Connection": "keep-alive",
    }
    try:
        url = f"https://youtubetotranscript.com/transcript?v={video_id}&current_language_code=en"
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.info("[YOUTUBE] Web fallback: HTTP %d for %s", resp.status_code, video_id)
            return None

        # Try __NEXT_DATA__ JSON blob (Next.js SSR)
        nd_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if nd_match:
            nd = json.loads(nd_match.group(1))
            props = nd.get("props", {}).get("pageProps", {})
            segments = props.get("transcript") or props.get("segments") or []
            if isinstance(segments, list) and segments:
                lines = [
                    seg.get("text", "") if isinstance(seg, dict) else str(seg)
                    for seg in segments
                ]
                text = "\n".join(ln for ln in lines if ln.strip())
                if text:
                    return text
            body_text = props.get("body") or props.get("text") or ""
            if len(body_text) > 100:
                return body_text

        # Fallback: try parsing visible HTML for transcript content
        soup = BeautifulSoup(resp.text, "lxml")
        for sel in ("#demo", ".transcript", "[data-transcript]"):
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 100:
                    return text

        logger.info("[YOUTUBE] Web fallback: no transcript in HTML for %s", video_id)
        return None
    except Exception as exc:
        logger.info("[YOUTUBE] Web fallback failed for %s: %s", video_id, exc)
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
