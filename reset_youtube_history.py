"""One-time script to reset YouTube history only.

Clears YouTube entries from seen_posts, strike_counts, and pending_items
while leaving Substack and website data completely untouched.

Usage:
    python3 reset_youtube_history.py
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "data" / "config.json"


def main():
    if not CONFIG_FILE.exists():
        print(f"Config file not found: {CONFIG_FILE}")
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    # --- Count before ---
    seen = config.get("seen_posts", {})
    strikes = config.get("strike_counts", {})
    pending = config.get("pending_items", [])

    yt_seen = {k: v for k, v in seen.items() if k.startswith("youtube_")}
    sub_seen = {k: v for k, v in seen.items() if k.startswith("substack_")}
    web_seen = {k: v for k, v in seen.items() if k.startswith("website_")}
    other_seen = {k: v for k, v in seen.items()
                  if not k.startswith(("youtube_", "substack_", "website_"))}

    yt_strikes = {k: v for k, v in strikes.items() if k.startswith("youtube_")}
    non_yt_strikes = {k: v for k, v in strikes.items() if not k.startswith("youtube_")}

    yt_pending = [i for i in pending if i.get("platform") == "youtube"]
    non_yt_pending = [i for i in pending if i.get("platform") != "youtube"]

    print("=== YouTube History Reset ===\n")
    print(f"Seen posts total:    {len(seen)}")
    print(f"  YouTube:           {len(yt_seen)}  ← WILL BE REMOVED")
    print(f"  Substack:          {len(sub_seen)}  — not modified")
    print(f"  Website:           {len(web_seen)}  — not modified")
    if other_seen:
        print(f"  Other:             {len(other_seen)}  — not modified")
    print()
    print(f"Strike counts total: {len(strikes)}")
    print(f"  YouTube:           {len(yt_strikes)}  ← WILL BE REMOVED")
    print(f"  Non-YouTube:       {len(non_yt_strikes)}  — not modified")
    print()
    print(f"Pending items total: {len(pending)}")
    print(f"  YouTube:           {len(yt_pending)}  ← WILL BE REMOVED")
    print(f"  Non-YouTube:       {len(non_yt_pending)}  — not modified")
    print()

    # --- Remove YouTube entries only ---
    for k in yt_seen:
        del config["seen_posts"][k]

    for k in yt_strikes:
        del config["strike_counts"][k]

    config["pending_items"] = non_yt_pending

    # --- Save ---
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, default=str)

    # --- Verify ---
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        verify = json.load(f)

    yt_remaining = sum(1 for k in verify.get("seen_posts", {}) if k.startswith("youtube_"))
    sub_remaining = sum(1 for k in verify.get("seen_posts", {}) if k.startswith("substack_"))
    web_remaining = sum(1 for k in verify.get("seen_posts", {}) if k.startswith("website_"))

    print("=== Done ===\n")
    print(f"Removed {len(yt_seen)} YouTube seen entries")
    print(f"Removed {len(yt_strikes)} YouTube strike entries")
    print(f"Removed {len(yt_pending)} YouTube pending items")
    print()
    print(f"Verification — remaining seen_posts:")
    print(f"  YouTube:   {yt_remaining}  (should be 0)")
    print(f"  Substack:  {sub_remaining}  (should be {len(sub_seen)})")
    print(f"  Website:   {web_remaining}  (should be {len(web_seen)})")


if __name__ == "__main__":
    main()
