"""Persistent configuration and state management for tracked sources."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "substack": [],
    "youtube": [],
    "spotify": [],
    "websites": [],
    "last_check": None,
    "seen_posts": {},
    "pending_items": [],
    "telegram_chat_id": None,
    # FIX-1: Hour/minute (UTC) for the daily scheduled check
    "daily_check_hour": 9,
    "daily_check_minute": 0,
    # FIX-2: Strike counts for the 3-strike discard rule.
    # {post_id: {"count": N, "title": "..."}}
    "strike_counts": {},
}


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_data_dir()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        for key, default in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = (
                    type(default)() if isinstance(default, (list, dict)) else default
                )
        return config
    save_config(DEFAULT_CONFIG.copy())
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    _ensure_data_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, default=str)


# --------------- source management ---------------

def add_source(platform: str, username: str) -> bool:
    config = load_config()
    if platform not in config:
        config[platform] = []
    if username not in config[platform]:
        config[platform].append(username)
        save_config(config)
        return True
    return False


def remove_source(platform: str, username: str) -> bool:
    config = load_config()
    if platform in config and username in config[platform]:
        config[platform].remove(username)
        save_config(config)
        return True
    return False


def get_sources(platform: str) -> list:
    return load_config().get(platform, [])


# --------------- check timestamps ---------------

def update_last_check():
    config = load_config()
    config["last_check"] = datetime.now(timezone.utc).isoformat()
    save_config(config)


def get_last_check() -> Optional[datetime]:
    lc = load_config().get("last_check")
    if lc:
        return datetime.fromisoformat(lc)
    return None


# --------------- seen-post tracking ---------------

def mark_post_seen(post_id: str):
    config = load_config()
    config["seen_posts"][post_id] = datetime.now(timezone.utc).isoformat()
    save_config(config)


def is_post_seen(post_id: str) -> bool:
    return post_id in load_config().get("seen_posts", {})


# --------------- telegram chat id ---------------

def set_chat_id(chat_id: int):
    config = load_config()
    config["telegram_chat_id"] = chat_id
    save_config(config)


def get_chat_id() -> Optional[int]:
    return load_config().get("telegram_chat_id")


# --------------- pending items (survive restarts) ---------------

def save_pending_items(items: list):
    config = load_config()
    config["pending_items"] = items
    save_config(config)


def get_pending_items() -> list:
    return load_config().get("pending_items", [])


def clear_pending_items():
    config = load_config()
    config["pending_items"] = []
    save_config(config)


# --------------- FIX-1: daily check schedule ---------------

def get_daily_check_time() -> tuple[int, int]:
    """Return (hour, minute) in UTC for the daily scheduled check."""
    cfg = load_config()
    return cfg.get("daily_check_hour", 9), cfg.get("daily_check_minute", 0)


# --------------- FIX-2: 3-strike discard tracking ---------------

def get_strike_count(post_id: str) -> int:
    """Return how many cycles this item has been shown without selection."""
    counts = load_config().get("strike_counts", {})
    entry = counts.get(post_id)
    return entry["count"] if isinstance(entry, dict) else 0


def increment_strike(post_id: str, title: str) -> int:
    """Increment the strike counter; returns the new count."""
    config = load_config()
    strikes = config.setdefault("strike_counts", {})
    entry = strikes.get(post_id)
    if isinstance(entry, dict):
        entry["count"] += 1
    else:
        entry = {"count": 1, "title": title}
    strikes[post_id] = entry
    save_config(config)
    return entry["count"]


def clear_strike(post_id: str):
    """Remove an item's strike record (e.g. after user saves it)."""
    config = load_config()
    config.get("strike_counts", {}).pop(post_id, None)
    save_config(config)


def discard_item(post_id: str, title: str):
    """Permanently discard an item: mark seen and remove strike record."""
    logger.info('[DISCARDED] "%s" - shown 3 times without selection', title)
    mark_post_seen(post_id)
    config = load_config()
    config.get("strike_counts", {}).pop(post_id, None)
    save_config(config)
