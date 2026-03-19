"""One-time script to undo the last 2 /check cycles.

- Decrements all strike counts by 2 (removes entries that drop to 0 or below)
- Removes seen_posts entries added in the last 2 checks (by timestamp)
- Clears pending_items

This lets you re-select content from those checks as if they never happened.

Usage:
    python3 reset_last_2_checks.py
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "data" / "config.json"


def main():
    if not CONFIG_FILE.exists():
        print(f"Config file not found: {CONFIG_FILE}")
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    seen = config.get("seen_posts", {})
    strikes = config.get("strike_counts", {})
    pending = config.get("pending_items", [])

    # --- Determine cutoff: remove seen_posts added in the last ~24 hours ---
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=26)

    recent_seen = {}
    kept_seen = {}
    for post_id, ts in seen.items():
        try:
            entry_time = datetime.fromisoformat(ts)
            if entry_time >= cutoff:
                recent_seen[post_id] = ts
            else:
                kept_seen[post_id] = ts
        except (ValueError, TypeError):
            kept_seen[post_id] = ts

    # --- Decrement strikes by 2 ---
    reduced_strikes = {}
    removed_strikes = 0
    for post_id, entry in strikes.items():
        if isinstance(entry, dict):
            new_count = entry["count"] - 2
            if new_count > 0:
                reduced_strikes[post_id] = {"count": new_count, "title": entry.get("title", "")}
            else:
                removed_strikes += 1
        else:
            removed_strikes += 1

    print("=== Undo Last 2 /check Cycles ===\n")
    print(f"Seen posts total:       {len(seen)}")
    print(f"  Added in last ~26h:   {len(recent_seen)}  ← WILL BE REMOVED")
    print(f"  Older (kept):         {len(kept_seen)}  — not modified")
    print()

    by_platform = {}
    for pid in recent_seen:
        plat = pid.split("_")[0] if "_" in pid else "unknown"
        by_platform[plat] = by_platform.get(plat, 0) + 1
    if by_platform:
        print(f"  Removed breakdown:    {by_platform}")
        print()

    print(f"Strike counts total:    {len(strikes)}")
    print(f"  Decremented by 2:     {len(reduced_strikes)} entries remain")
    print(f"  Fully cleared:        {removed_strikes} entries removed")
    print()
    print(f"Pending items:          {len(pending)}  ← WILL BE CLEARED")
    print()

    # --- Apply ---
    config["seen_posts"] = kept_seen
    config["strike_counts"] = reduced_strikes
    config["pending_items"] = []

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, default=str)

    # --- Verify ---
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        verify = json.load(f)

    print("=== Done ===\n")
    print(f"Seen posts remaining:   {len(verify.get('seen_posts', {}))}")
    print(f"Strike counts remaining:{len(verify.get('strike_counts', {}))}")
    print(f"Pending items:          {len(verify.get('pending_items', []))}")
    print()
    print("Restart the bot and run /check to get fresh polls.")


if __name__ == "__main__":
    main()
