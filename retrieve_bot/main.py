"""Entry point for Retrieve Bot."""

import logging
import sys
from pathlib import Path

# Ensure the local substack_api package is importable
_SUBSTACK_DIR = str(Path(__file__).parent.parent / "substack_api")
if _SUBSTACK_DIR not in sys.path:
    sys.path.insert(0, _SUBSTACK_DIR)

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s  %(name)-25s  %(levelname)-7s  %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

from retrieve_bot.telegram_handler import create_application


def main():
    app = create_application()
    print(
        "Retrieve Bot is running.\n"
        "Open Telegram and message @Retrieve_bot to get started.\n"
        "Press Ctrl+C to stop."
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
