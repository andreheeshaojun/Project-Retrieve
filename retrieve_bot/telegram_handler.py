"""Telegram bot interface for Retrieve Bot.

Provides commands to manage tracked sources, sends consolidated poll-based
notifications for new content, and orchestrates PDF generation + OneDrive upload
once the user confirms their selections.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
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
from retrieve_bot.pdf_generator import generate_substack_pdf, generate_youtube_pdf
from retrieve_bot.substack_monitor import (
    check_substack_for_new_posts,
    get_post_html_content,
)
from retrieve_bot.youtube_monitor import (
    check_youtube_for_new_videos,
    get_transcript,
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
        "I monitor your Substack and YouTube sources every 24 hours "
        "and save approved content as PDFs to OneDrive\\.\n\n"
        "*Source management*\n"
        "/add\\_substack `<username>` \\- track a publisher\n"
        "/remove\\_substack `<username>` \\- stop tracking\n"
        "/add\\_youtube `<handle>` \\- track a channel\n"
        "/remove\\_youtube `<handle>` \\- stop tracking\n"
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


# ---- list / status ----

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Tracked Sources\n"]
    for platform in ("substack", "youtube", "spotify"):
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
    n_sp = len(config.get_sources("spotify"))
    last = config.get_last_check()
    last_str = last.strftime("%Y-%m-%d %H:%M UTC") if last else "Never"
    n_pending = len(pending_items)

    await update.message.reply_text(
        f"Retrieve Bot Status\n"
        f"-------------------\n"
        f"Substack sources: {n_sub}\n"
        f"YouTube sources:  {n_yt}\n"
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
    await _run_content_check(context)


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

    config.update_last_check()

    if not all_items:
        await context.bot.send_message(chat_id, "No new content found.")
        return

    # Store pending items
    pending_items.clear()
    selected_items.clear()
    poll_to_item_ids.clear()

    for item in all_items:
        pending_items[item["id"]] = item
    config.save_pending_items(list(pending_items.values()))

    # Build summary message
    summary_lines = [f"New Content Found ({len(all_items)} items)", "=" * 35, ""]
    for i, item in enumerate(all_items, 1):
        icon = "\U0001f4dd" if item["platform"] == "substack" else "\U0001f3ac"
        summary_lines.append(f"{i}. {icon} [{item['source']}] {item['title']}")
        if item.get("subtitle"):
            summary_lines.append(f"   {item['subtitle'][:80]}")
        summary_lines.append(f"   {item['url']}")
        summary_lines.append("")
    await context.bot.send_message(chat_id, "\n".join(summary_lines))

    # Send poll(s) – Telegram allows max 10 options per poll
    options: List[str] = []
    item_ids: List[str] = []
    for item in all_items:
        icon = "\U0001f4dd" if item["platform"] == "substack" else "\U0001f3ac"
        truncated = item["title"][:90]
        options.append(f"{icon} {truncated}")
        item_ids.append(item["id"])

    BATCH = 10
    for start in range(0, len(options), BATCH):
        batch_opts = options[start : start + BATCH]
        batch_ids = item_ids[start : start + BATCH]

        question = "Select content to save:"
        if len(options) > BATCH:
            batch_num = start // BATCH + 1
            total_batches = (len(options) + BATCH - 1) // BATCH
            question = f"Select content to save ({batch_num}/{total_batches}):"

        poll_msg = await context.bot.send_poll(
            chat_id,
            question=question,
            options=batch_opts,
            allows_multiple_answers=True,
            is_anonymous=False,
        )
        poll_to_item_ids[poll_msg.poll.id] = batch_ids

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

            local_path.unlink(missing_ok=True)
            config.mark_post_seen(item["id"])
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
# Scheduled 24-hour check (called by JobQueue)
# ====================================================================

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled 24-hour content check...")
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

    # 24-hour recurring check (first run 60 s after startup)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(
            scheduled_check,
            interval=86400,
            first=1800,
        )

    return app
