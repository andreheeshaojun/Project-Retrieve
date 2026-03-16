"""Persistent configuration and state management for tracked sources."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "substack": [],
    "youtube": [],
    "spotify": [],
    "last_check": None,
    "seen_posts": {},
    "pending_items": [],
    "telegram_chat_id": None,
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
