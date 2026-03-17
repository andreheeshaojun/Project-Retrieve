"""Telegram bot interface for Retrieve Bot.

Provides commands to manage tracked sources, sends consolidated poll-based
notifications for new content, and orchestrates PDF generation + OneDrive upload
once the user confirms their selections.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Set

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
    TypeHandler,
)

from retrieve_bot import config
from retrieve_bot.onedrive_client import OneDriveClient
from retrieve_bot.pdf_generator import (
    generate_substack_pdf,
    generate_youtube_pdf,
    generate_website_pdf,
)
from retrieve_bot.substack_monitor import (
    check_substack_for_new_posts,
    get_post_html_content,
)
from retrieve_bot.youtube_monitor import (
    check_youtube_for_new_videos,
    get_transcript,
)
from retrieve_bot.website_monitor import (
    check_websites_for_new_content,
    scrape_article_content,
    derive_source_label,
)

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = 1625301518

# ---- in-memory state for poll-based approval flow ----
pending_items: Dict[str, dict] = {}
poll_to_item_ids: Dict[str, List[str]] = {}   # poll_id -> ordered item ids
selected_items: Set[str] = set()
onedrive_client = OneDriveClient()

TEMP_DIR = Path(__file__).parent.parent / "data" / "temp"


# ====================================================================
# Security gate – blocks every update from non-owner users
# ====================================================================

async def _security_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != ALLOWED_USER_ID:
        raise ApplicationHandlerStop()


# ====================================================================
# Command handlers
# ====================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.set_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "Welcome to *Retrieve Bot*\\!\n\n"
        "I monitor your Substack, YouTube, and website sources every "
        "24 hours and save approved content as PDFs to OneDrive\\.\n\n"
        "*Source management*\n"
        "/add\\_substack `<username>` \\- track a publisher\n"
        "/remove\\_substack `<username>` \\- stop tracking\n"
        "/add\\_youtube `<handle>` \\- track a channel\n"
        "/remove\\_youtube `<handle>` \\- stop tracking\n"
        "/add\\_website `<url>` \\- track any website\n"
        "/remove\\_website `<url>` \\- stop tracking\n"
        "/add\\_spotify `<name>` \\- track \\(coming soon\\)\n"
        "/remove\\_spotify `<name>` \\- stop tracking\n\n"
        "*Actions*\n"
        "/list \\- show tracked sources\n"
        "/check \\- check for new content now\n"
        "/auth\\_onedrive \\- authenticate OneDrive\n"
        "/status \\- bot status\n"
        "/help \\- show this message",
        parse_mode="MarkdownV2",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ---- add / remove sources ----

async def _add_source(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      platform: str, label: str):
    if not context.args:
        await update.message.reply_text(f"Usage: /add_{platform} <{label}>")
        return
    name = context.args[0].strip()
    if config.add_source(platform, name):
        note = ""
        if platform == "spotify":
            note = "\n(Spotify integration coming soon)"
        await update.message.reply_text(f"Now tracking {platform.title()}: {name}{note}")
    else:
        await update.message.reply_text(f"Already tracking: {name}")


async def _remove_source(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         platform: str, label: str):
    if not context.args:
        await update.message.reply_text(f"Usage: /remove_{platform} <{label}>")
        return
    name = context.args[0].strip()
    if config.remove_source(platform, name):
        await update.message.reply_text(f"Stopped tracking {platform.title()}: {name}")
    else:
        await update.message.reply_text(f"Not currently tracking: {name}")


async def cmd_add_substack(update, context):
    await _add_source(update, context, "substack", "username")

async def cmd_remove_substack(update, context):
    await _remove_source(update, context, "substack", "username")

async def cmd_add_youtube(update, context):
    await _add_source(update, context, "youtube", "handle")

async def cmd_remove_youtube(update, context):
    await _remove_source(update, context, "youtube", "handle")

async def cmd_add_spotify(update, context):
    await _add_source(update, context, "spotify", "name")

async def cmd_remove_spotify(update, context):
    await _remove_source(update, context, "spotify", "name")

async def cmd_add_website(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_website <url>")
        return
    url = context.args[0].strip()
    if not url.startswith("http"):
        url = f"https://{url}"
    if config.add_source("websites", url):
        label = derive_source_label(url)
        await update.message.reply_text(
            f"Now tracking website: {url}\nLabel: {label}"
        )
    else:
        await update.message.reply_text(f"Already tracking: {url}")

async def cmd_remove_website(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_website <url>")
        return
    url = context.args[0].strip()
    if not url.startswith("http"):
        url = f"https://{url}"
    if config.remove_source("websites", url):
        await update.message.reply_text(f"Stopped tracking website: {url}")
    else:
        await update.message.reply_text(f"Not currently tracking: {url}")


# ---- list / status ----

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Tracked Sources\n"]
    for platform in ("substack", "youtube", "websites", "spotify"):
        sources = config.get_sources(platform)
        lines.append(f"{platform.title()}:")
        if sources:
            lines.extend(f"  - {s}" for s in sources)
        else:
            lines.append("  (none)")
        lines.append("")

    last = config.get_last_check()
    if last:
        lines.append(f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC')}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    od_status = "Authenticated" if onedrive_client.is_authenticated() else "Not authenticated"
    n_sub = len(config.get_sources("substack"))
    n_yt = len(config.get_sources("youtube"))
    n_web = len(config.get_sources("websites"))
    n_sp = len(config.get_sources("spotify"))
    last = config.get_last_check()
    last_str = last.strftime("%Y-%m-%d %H:%M UTC") if last else "Never"
    n_pending = len(pending_items)

    await update.message.reply_text(
        f"Retrieve Bot Status\n"
        f"-------------------\n"
        f"Substack sources: {n_sub}\n"
        f"YouTube sources:  {n_yt}\n"
        f"Website sources:  {n_web}\n"
        f"Spotify sources:  {n_sp}\n"
        f"Last check:       {last_str}\n"
        f"Pending items:    {n_pending}\n"
        f"OneDrive:         {od_status}"
    )


# ---- OneDrive auth ----

async def cmd_auth_onedrive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if onedrive_client.is_authenticated():
        await update.message.reply_text("OneDrive is already authenticated.")
        return

    try:
        flow = onedrive_client.get_device_flow()
        await update.message.reply_text(
            f"Please authenticate with OneDrive:\n\n{flow.get('message', '')}"
        )
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None, onedrive_client.complete_device_flow, flow
        )
        if success:
            await update.message.reply_text("OneDrive authenticated successfully!")
        else:
            await update.message.reply_text(
                "Authentication failed. Please try /auth_onedrive again."
            )
    except Exception as exc:
        await update.message.reply_text(f"OneDrive auth error: {exc}")


# ====================================================================
# Content checking & poll flow
# ====================================================================

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.set_chat_id(update.effective_chat.id)
    await update.message.reply_text("Checking for new content...")
    try:
        await _run_content_check(context)
    except Exception as _exc:
        await update.message.reply_text(f"Check failed: {_exc}")


async def _run_content_check(context: ContextTypes.DEFAULT_TYPE):
    """Core check logic shared by manual /check and the 24-hour scheduler."""
    global pending_items, selected_items, poll_to_item_ids

    chat_id = config.get_chat_id()
    if not chat_id:
        logger.warning("No chat_id stored – user must /start the bot first.")
        return

    all_items: List[dict] = []

    # --- Substack ---
    substacks = config.get_sources("substack")
    if substacks:
        try:
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(
                None, check_substack_for_new_posts, substacks
            )
            all_items.extend(items)
        except Exception as exc:
            await context.bot.send_message(chat_id, f"Substack error: {exc}")

    # --- YouTube ---
    youtubes = config.get_sources("youtube")
    if youtubes:
        try:
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(
                None, check_youtube_for_new_videos, youtubes
            )
            all_items.extend(items)
        except Exception as exc:
            await context.bot.send_message(chat_id, f"YouTube error: {exc}")

    # --- Websites ---
    websites = config.get_sources("websites")
    if websites:
        try:
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(
                None, check_websites_for_new_content, websites
            )
            all_items.extend(items)
        except Exception as exc:
            await context.bot.send_message(chat_id, f"Website error: {exc}")

    config.update_last_check()

    # FIX-2: 3-strike discard rule — remove items shown 3 times without selection
    filtered_items: List[dict] = []
    for item in all_items:
        count = config.get_strike_count(item["id"])
        if count >= 3:
            config.discard_item(item["id"], item["title"])
            continue
        filtered_items.append(item)

    # FIX-3: Strict top-15 cap — never backfill older content
    filtered_items = filtered_items[:15]

    # FIX-2: Increment strike counter for every item we are about to show
    for item in filtered_items:
        config.increment_strike(item["id"], item["title"])

    all_items = filtered_items

    if not all_items:
        # FIX-3: Explicit "nothing today" message instead of silence
        await context.bot.send_message(chat_id, "No new content to show today.")
        return

    # Store pending items
    pending_items.clear()
    selected_items.clear()
    poll_to_item_ids.clear()

    for item in all_items:
        pending_items[item["id"]] = item
    config.save_pending_items(list(pending_items.values()))

    # Build summary message
    _PLATFORM_ICONS = {
        "substack": "\U0001f4dd",
        "youtube": "\U0001f3ac",
        "website": "\U0001f4c4",
    }
    summary_lines = [f"New Content Found ({len(all_items)} items)", "=" * 35, ""]
    for i, item in enumerate(all_items, 1):
        icon = _PLATFORM_ICONS.get(item["platform"], "\U0001f4c4")
        summary_lines.append(f"{i}. {icon} [{item['source']}] {item['title']}")
        if item.get("subtitle"):
            summary_lines.append(f"   {item['subtitle'][:80]}")
        summary_lines.append(f"   {item['url']}")
        summary_lines.append("")
    _summary_text = "\n".join(summary_lines)

    _MSG_LIMIT = 4096
    if len(_summary_text) <= _MSG_LIMIT:
        await context.bot.send_message(chat_id, _summary_text)
    else:
        _header = f"New Content Found ({len(all_items)} items)\n{'=' * 35}\n\n"
        _chunk = _header
        for i, item in enumerate(all_items, 1):
            _icon = _PLATFORM_ICONS.get(item["platform"], "\U0001f4c4")
            _entry = f"{i}. {_icon} [{item['source']}] {item['title']}\n"
            if item.get("subtitle"):
                _entry += f"   {item['subtitle'][:80]}\n"
            _entry += f"   {item['url']}\n\n"
            if len(_chunk) + len(_entry) > _MSG_LIMIT:
                await context.bot.send_message(chat_id, _chunk)
                _chunk = _entry
            else:
                _chunk += _entry
        if _chunk:
            await context.bot.send_message(chat_id, _chunk)

    # Send poll(s) – Telegram allows max 10 options per poll
    options: List[str] = []
    item_ids: List[str] = []
    _POLL_OPT_LIMIT = 100
    for item in all_items:
        icon = _PLATFORM_ICONS.get(item["platform"], "\U0001f4c4")
        src = item["source"]
        prefix = f"{icon} [{src}] "
        max_title = _POLL_OPT_LIMIT - len(prefix)
        title = item["title"][:max_title]
        options.append(f"{prefix}{title}")
        item_ids.append(item["id"])

    BATCH = 10
    n_opts = len(options)
    batch_ranges: List[tuple] = []
    _i = 0
    while _i < n_opts:
        size = min(BATCH, n_opts - _i)
        if n_opts - _i - size == 1:
            size = BATCH - 1
        batch_ranges.append((_i, size))
        _i += size

    total_batches = len(batch_ranges)
    for batch_num, (start, size) in enumerate(batch_ranges, 1):
        batch_opts = options[start : start + size]
        batch_ids = item_ids[start : start + size]

        question = "Select content to save:"
        if total_batches > 1:
            question = f"Select content to save ({batch_num}/{total_batches}):"

        try:
            poll_msg = await context.bot.send_poll(
                chat_id,
                question=question,
                options=batch_opts,
                allows_multiple_answers=True,
                is_anonymous=False,
            )
            poll_to_item_ids[poll_msg.poll.id] = batch_ids
        except Exception as _exc:
            raise

    # Confirm button
    keyboard = [
        [InlineKeyboardButton(
            "\U0001f4e5 Confirm & Save Selected", callback_data="confirm_save"
        )]
    ]
    await context.bot.send_message(
        chat_id,
        "After selecting your content above, press the button below to save:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---- poll answer tracking ----

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id

    if poll_id not in poll_to_item_ids:
        return

    batch_ids = poll_to_item_ids[poll_id]

    # Clear previous selections for this specific poll
    for item_id in batch_ids:
        selected_items.discard(item_id)

    # Record current selections
    for idx in answer.option_ids:
        if idx < len(batch_ids):
            selected_items.add(batch_ids[idx])


# ---- confirm & save ----

async def handle_confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not selected_items:
        await query.edit_message_text("No items selected. Nothing to save.")
        return

    if not onedrive_client.is_authenticated():
        await query.edit_message_text(
            "OneDrive not authenticated.\n"
            "Use /auth_onedrive first, then /check again."
        )
        return

    items_to_save = [
        pending_items[iid]
        for iid in selected_items
        if iid in pending_items
    ]
    await query.edit_message_text(f"Processing {len(items_to_save)} items...")
    chat_id = update.effective_chat.id
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0

    for item in items_to_save:
        try:
            safe_title = re.sub(r'[^\w\s-]', '', item["title"])[:60].strip()
            date_prefix = (item.get("date") or "")[:10] or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%d")
            filename = f"{date_prefix}_{safe_title}.pdf"
            local_path = TEMP_DIR / filename

            if item["platform"] == "substack":
                html = await asyncio.get_event_loop().run_in_executor(
                    None, get_post_html_content, item["url"]
                )
                generate_substack_pdf(
                    title=item["title"],
                    subtitle=item.get("subtitle", ""),
                    author=item.get("source", ""),
                    date=item.get("date", ""),
                    html_content=html,
                    url=item["url"],
                    output_path=local_path,
                )
                remote = f"Substack/{item['source']}/{filename}"
                onedrive_client.ensure_folder(f"Substack/{item['source']}")
                with open(local_path, "rb") as f:
                    onedrive_client.upload_file(remote, f.read())

            elif item["platform"] == "youtube":
                transcript = await asyncio.get_event_loop().run_in_executor(
                    None, get_transcript, item.get("video_id", "")
                )
                generate_youtube_pdf(
                    title=item["title"],
                    channel=item.get("source", ""),
                    date=item.get("date", ""),
                    transcript=transcript or "Transcript not available.",
                    url=item["url"],
                    output_path=local_path,
                )
                remote = f"Youtube/{item['source']}/{filename}"
                onedrive_client.ensure_folder(f"Youtube/{item['source']}")
                with open(local_path, "rb") as f:
                    onedrive_client.upload_file(remote, f.read())

            elif item["platform"] == "website":
                content = await asyncio.get_event_loop().run_in_executor(
                    None, scrape_article_content, item["url"]
                )
                source_label = item.get("source", derive_source_label(
                    item.get("listing_url", item["url"])
                ))
                generate_website_pdf(
                    title=content.get("title") or item["title"],
                    author=content.get("author", ""),
                    date=content.get("date") or item.get("date", ""),
                    text_content=content.get("text", ""),
                    url=item["url"],
                    source=source_label,
                    output_path=local_path,
                )
                remote = f"Websites/{source_label}/{filename}"
                onedrive_client.ensure_folder(f"Websites/{source_label}")
                with open(local_path, "rb") as f:
                    onedrive_client.upload_file(remote, f.read())

            local_path.unlink(missing_ok=True)
            config.mark_post_seen(item["id"])
            # FIX-2: Clear strike record for saved items
            config.clear_strike(item["id"])
            saved += 1

        except Exception as exc:
            logger.error("Failed to save '%s': %s", item["title"], exc)
            await context.bot.send_message(
                chat_id, f"Error saving '{item['title']}': {exc}"
            )

    # Cleanup
    pending_items.clear()
    selected_items.clear()
    poll_to_item_ids.clear()
    config.clear_pending_items()

    await context.bot.send_message(
        chat_id,
        f"Done! Saved {saved}/{len(items_to_save)} items to OneDrive.",
    )


# ====================================================================
# FIX-1: Scheduled daily check at a fixed wall-clock time (JobQueue)
# ====================================================================

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    hour, minute = config.get_daily_check_time()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    # FIX-1: Log each cycle with precise timestamps for auditing
    logger.info(
        "[CHECK CYCLE] Started at %s, next scheduled at %s",
        now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        next_run.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )
    await _run_content_check(context)


# ====================================================================
# Application factory
# ====================================================================

def create_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Security: reject all updates from non-owner users before anything else
    app.add_handler(TypeHandler(Update, _security_gate), group=-1)

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add_substack", cmd_add_substack))
    app.add_handler(CommandHandler("remove_substack", cmd_remove_substack))
    app.add_handler(CommandHandler("add_youtube", cmd_add_youtube))
    app.add_handler(CommandHandler("remove_youtube", cmd_remove_youtube))
    app.add_handler(CommandHandler("add_website", cmd_add_website))
    app.add_handler(CommandHandler("remove_website", cmd_remove_website))
    app.add_handler(CommandHandler("add_spotify", cmd_add_spotify))
    app.add_handler(CommandHandler("remove_spotify", cmd_remove_spotify))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("auth_onedrive", cmd_auth_onedrive))

    # Poll answer tracking
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    # Confirm-save button
    app.add_handler(
        CallbackQueryHandler(handle_confirm_save, pattern="^confirm_save$")
    )

    # FIX-1: Daily check at a fixed UTC time instead of relative-to-startup.
    # Eliminates irregular timing caused by bot restarts stacking jobs.
    job_queue = app.job_queue
    if job_queue:
        hour, minute = config.get_daily_check_time()
        job_queue.run_daily(
            scheduled_check,
            time=dt_time(hour=hour, minute=minute, tzinfo=timezone.utc),
        )

    return app
