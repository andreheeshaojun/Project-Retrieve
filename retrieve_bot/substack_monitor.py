"""Monitor tracked Substack newsletters for new posts."""

import logging
import sys
from pathlib import Path
from time import sleep
from typing import Any, Dict, List

import requests

from retrieve_bot.config import is_post_seen

SUBSTACK_API_DIR = str(Path(__file__).parent.parent / "substack_api")
if SUBSTACK_API_DIR not in sys.path:
    sys.path.insert(0, SUBSTACK_API_DIR)

logger = logging.getLogger(__name__)

# #region agent log
import json as _json, time as _time
_DBG_LOG = Path(__file__).parent.parent / "debug-f972e5.log"
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
        "Chrome/91.0.4472.77 Safari/537.36"
    )
}


def normalize_substack_url(username_or_url: str) -> str:
    """Accept 'username', 'username.substack.com', or full URL."""
    s = username_or_url.strip().rstrip("/")
    if s.startswith("http"):
        return s
    if ".substack.com" in s:
        return f"https://{s}"
    return f"https://{s}.substack.com"


def fetch_recent_posts_raw(newsletter_url: str, limit: int = 15) -> list:
    """Hit the archive endpoint directly – one request per newsletter."""
    endpoint = f"{newsletter_url}/api/v1/archive?sort=new&offset=0&limit={limit}"
    resp = requests.get(endpoint, headers=HEADERS, timeout=30)
    sleep(2)
    resp.raise_for_status()
    return resp.json()


def check_substack_for_new_posts(usernames: List[str]) -> List[Dict[str, Any]]:
    """Return metadata dicts for all unseen posts across tracked newsletters."""
    new_posts: List[Dict[str, Any]] = []

    for username in usernames:
        try:
            url = normalize_substack_url(username)
            raw_posts = fetch_recent_posts_raw(url, limit=15)
            # #region agent log
            _unseen = sum(1 for p in raw_posts if not is_post_seen(f"substack_{p.get('id', p.get('slug', ''))}"))
            _dbg("substack source", {"username": username, "fetched": len(raw_posts), "unseen": _unseen}, hyp="H8,H9", loc="substack_monitor.py:check")
            # #endregion

            for post_data in raw_posts:
                post_id_val = post_data.get("id", post_data.get("slug", ""))
                post_id = f"substack_{post_id_val}"

                if is_post_seen(post_id):
                    continue

                canonical_url = post_data.get("canonical_url", "")
                if not canonical_url:
                    slug = post_data.get("slug", "")
                    canonical_url = f"{url}/p/{slug}" if slug else url

                new_posts.append(
                    {
                        "id": post_id,
                        "platform": "substack",
                        "source": username,
                        "title": post_data.get("title", "Untitled"),
                        "subtitle": post_data.get("subtitle", ""),
                        "url": canonical_url,
                        "date": post_data.get("post_date", ""),
                    }
                )

        except Exception as exc:
            # #region agent log
            _dbg("substack source EXCEPTION", {"username": username, "error": str(exc)}, hyp="H8", loc="substack_monitor.py:check")
            # #endregion
            logger.warning("Substack check failed for %s: %s", username, exc)

    return new_posts


def get_post_html_content(post_url: str) -> str:
    """Fetch the full HTML body of a single Substack post."""
    try:
        from substack_api import Post

        post = Post(post_url)
        return post.get_content() or ""
    except Exception as exc:
        logger.warning("Content fetch failed for %s: %s", post_url, exc)
        return ""
