"""Monitor arbitrary websites for new content by discovering article links on listing pages."""

import logging
import re
from collections import Counter
from time import sleep
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from retrieve_bot.config import is_post_seen, mark_post_seen

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
        "Chrome/91.0.4472.77 Safari/537.36"
    )
}

_SKIP_PATH_TOKENS = frozenset([
    "/login", "/signup", "/register", "/contact", "/about",
    "/privacy", "/terms", "/search", "/tag/", "/category/",
    "/cart", "/checkout", "/account", "/settings", "/faq",
    "/subscribe", "/newsletter",
])


def derive_source_label(listing_url: str) -> str:
    """Create a short, filesystem-safe label from a listing URL.

    Examples:
        https://www.oaktreecapital.com/insights/memos  ->  oaktreecapital-memos
        https://colossus.com/series/business-breakdowns/ ->  colossus-business-breakdowns
    """
    parsed = urlparse(listing_url)
    domain = parsed.netloc.lower()
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split(":")[0]
    domain = domain.split(".")[0]

    path = parsed.path.strip("/")
    segments = [s for s in path.split("/") if s] if path else []
    tail = segments[-1] if segments else ""

    label = f"{domain}-{tail}" if tail else domain
    label = re.sub(r"[^\w-]", "", label)
    return label or "website"


def _url_pattern(url: str) -> str:
    """Extract the 'directory' portion of a URL path for pattern grouping.

    /insights/memo/sea-change  ->  /insights/memo/
    /episode/ge-aerospace      ->  /episode/
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if "/" in path:
        return path.rsplit("/", 1)[0] + "/"
    return "/"


def extract_article_links(listing_url: str, limit: int = 15) -> List[Dict[str, str]]:
    """Discover article links on a listing page using pattern-based grouping.

    Returns up to *limit* links in page order (typically most-recent first).
    """
    resp = requests.get(listing_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    parsed_base = urlparse(listing_url)
    base_domain = parsed_base.netloc.lower()
    listing_normalized = listing_url.rstrip("/")

    seen_urls: set = set()
    candidates: List[Dict[str, str]] = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()

        if href.startswith("javascript:") or href.startswith("#"):
            continue

        full_url = urljoin(listing_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc.lower() != base_domain:
            continue

        normalized = full_url.split("?")[0].split("#")[0].rstrip("/")
        if normalized == listing_normalized:
            continue

        if normalized in seen_urls:
            continue

        path_lower = parsed.path.lower()
        if any(tok in path_lower for tok in _SKIP_PATH_TOKENS):
            continue

        text = a_tag.get_text(strip=True)
        if not text or len(text) < 5:
            continue

        seen_urls.add(normalized)
        candidates.append({"url": normalized, "title": text})

    if not candidates:
        return []

    pattern_counts: Counter = Counter()
    pattern_links: Dict[str, List[Dict[str, str]]] = {}

    for link in candidates:
        pat = _url_pattern(link["url"])
        pattern_counts[pat] += 1
        pattern_links.setdefault(pat, []).append(link)

    dominant_pattern, _ = pattern_counts.most_common(1)[0]
    article_links = pattern_links[dominant_pattern]

    return article_links[:limit]


def scrape_article_content(article_url: str) -> Dict[str, str]:
    """Extract article content using trafilatura with BeautifulSoup fallback."""
    result = {
        "title": "",
        "author": "",
        "date": "",
        "text": "",
        "url": article_url,
    }

    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(article_url)
        # #region agent log
        _dbg("website scrape trafilatura", {"url": article_url, "downloaded_len": len(downloaded) if downloaded else 0, "has_login_gate": ("sign in" in (downloaded or "").lower() or "log in" in (downloaded or "").lower()), "has_transcript_link": ("access the full transcript" in (downloaded or "").lower())}, hyp="H23,H24,H25,H26", loc="website_monitor.py:scrape")
        # #endregion
        if downloaded:
            extracted = trafilatura.bare_extraction(
                downloaded, include_comments=False, include_tables=True
            )
            if extracted:
                result["title"] = extracted.get("title") or ""
                result["author"] = extracted.get("author") or ""
                result["date"] = extracted.get("date") or ""
                result["text"] = extracted.get("text") or ""
                # #region agent log
                _dbg("website scrape extracted", {"url": article_url, "title": result["title"][:80], "text_len": len(result["text"]), "text_preview": result["text"][:300], "text_tail": result["text"][-200:] if len(result["text"]) > 200 else ""}, hyp="H23,H26", loc="website_monitor.py:scrape")
                # #endregion

        if result["text"]:
            return result
    except Exception as exc:
        # #region agent log
        _dbg("website scrape trafilatura FAILED", {"url": article_url, "exc_type": type(exc).__name__, "exc_msg": str(exc)[:300]}, hyp="H23", loc="website_monitor.py:scrape")
        # #endregion
        logger.warning("trafilatura failed for %s: %s", article_url, exc)

    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for tag in soup(["script", "style", "nav", "header", "footer", "svg"]):
            tag.decompose()

        if not result["title"]:
            title_tag = soup.find("title")
            if title_tag:
                result["title"] = title_tag.get_text(strip=True)

        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main:
            result["text"] = main.get_text(separator="\n", strip=True)

    except Exception as exc:
        logger.warning("BeautifulSoup fallback failed for %s: %s", article_url, exc)

    if not result["text"]:
        result["text"] = "Content could not be extracted. Visit the URL to read."

    return result


def check_websites_for_new_content(
    website_urls: List[str],
) -> List[Dict[str, Any]]:
    """Return metadata dicts for all unseen articles across tracked websites.

    On a fresh source, only the most recent 15 articles are surfaced;
    any additional links found are silently marked as seen.
    """
    new_items: List[Dict[str, Any]] = []

    for listing_url in website_urls:
        try:
            source_label = derive_source_label(listing_url)
            all_links = extract_article_links(listing_url, limit=100)
            sleep(2)

            unseen: List[Dict[str, str]] = []
            overflow: List[Dict[str, str]] = []

            for link in all_links:
                post_id = f"website_{link['url']}"
                if is_post_seen(post_id):
                    continue
                if len(unseen) < 15:
                    unseen.append(link)
                else:
                    overflow.append(link)

            for link in overflow:
                mark_post_seen(f"website_{link['url']}")

            for link in unseen:
                new_items.append({
                    "id": f"website_{link['url']}",
                    "platform": "website",
                    "source": source_label,
                    "title": link["title"],
                    "subtitle": "",
                    "url": link["url"],
                    "date": "",
                    "listing_url": listing_url,
                })

        except Exception as exc:
            logger.warning("Website check failed for %s: %s", listing_url, exc)

    return new_items
