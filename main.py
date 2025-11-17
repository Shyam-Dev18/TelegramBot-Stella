import os
import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatType
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import (
    ChatAdminRequired,
    ChannelPrivate,
    PeerIdInvalid,
    FloodWait,
    UserIsBlocked,
)
from aiohttp import web

# ----------------- CONFIG & LOGGING -----------------

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Please set API_ID, API_HASH and BOT_TOKEN in .env file")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
LOGGER = logging.getLogger(__name__)

app = Client(
    "controller_like_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ----------------- SIMPLE STORAGE -----------------

DATA_FILE = Path(__file__).parent / "channels.json"

# channels_data = { "user_id(str)": [ { "id": int, "title": str } ] }
channels_data: Dict[str, List[Dict[str, Any]]] = {}


def load_channels() -> None:
    """Load channels data from JSON file."""
    global channels_data
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                channels_data = json.load(f)
            LOGGER.info("Loaded %d user channel lists", len(channels_data))
        except json.JSONDecodeError as e:
            LOGGER.error("Invalid JSON in channels.json: %s", e)
            channels_data = {}
        except Exception as e:
            LOGGER.error("Error loading channels.json: %s", e)
            channels_data = {}
    else:
        channels_data = {}
        LOGGER.info("No existing channels.json found, starting fresh")


def save_channels() -> None:
    """Save channels data to JSON file."""
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(channels_data, f, indent=2, ensure_ascii=False)
        LOGGER.debug("Channels saved successfully")
    except Exception as e:
        LOGGER.error("Error saving channels.json: %s", e)


def get_user_channels(user_id: int) -> List[Dict[str, Any]]:
    """Get list of channels for a specific user."""
    return channels_data.get(str(user_id), [])


def add_user_channel(user_id: int, chat_id: int, title: str) -> bool:
    """Add a channel to user's list. Returns True if added, False if already exists."""
    uid = str(user_id)
    user_list = channels_data.setdefault(uid, [])
    
    # Check if channel already exists
    for c in user_list:
        if c["id"] == chat_id:
            LOGGER.info("Channel %s already exists for user %s", chat_id, user_id)
            return False
    
    user_list.append({"id": chat_id, "title": title})
    save_channels()
    LOGGER.info("Added channel %s (%s) for user %s", title, chat_id, user_id)
    return True


def remove_user_channel(user_id: int, chat_id: int) -> bool:
    """Remove a channel from user's list. Returns True if removed, False if not found."""
    uid = str(user_id)
    user_list = channels_data.get(uid, [])
    
    for i, c in enumerate(user_list):
        if c["id"] == chat_id:
            removed = user_list.pop(i)
            save_channels()
            LOGGER.info("Removed channel %s for user %s", chat_id, user_id)
            return True
    
    return False


# ----------------- SESSION STATE -----------------

# session[user_id] = { "state": str, "post": {...}, "tmp_button_text": str }
sessions: Dict[int, Dict[str, Any]] = {}


def get_session(user_id: int) -> Dict[str, Any]:
    """Get or create session for a user."""
    if user_id not in sessions:
        sessions[user_id] = {
            "state": "idle",
            "post": None,
            "tmp_button_text": None
        }
    return sessions[user_id]


def reset_session(user_id: int) -> None:
    """Reset user session to idle state."""
    sessions[user_id] = {
        "state": "idle",
        "post": None,
        "tmp_button_text": None
    }
    LOGGER.debug("Reset session for user %s", user_id)


# ----------------- KEYBOARDS -----------------

def main_menu() -> InlineKeyboardMarkup:
    """Main menu keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 New Post", callback_data="menu_new_post")],
        [InlineKeyboardButton("📡 Add Channel", callback_data="menu_add_channel")],
        [InlineKeyboardButton("🗑️ Remove Channel", callback_data="menu_remove_channel")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Simple cancel button keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")]
    ])


def add_button_or_skip_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for adding buttons or skipping."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Button", callback_data="post_add_button")],
        [InlineKeyboardButton("➡️ Skip Buttons", callback_data="post_skip_buttons")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")],
    ])


def done_more_buttons_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for adding more buttons or finishing."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Another Button", callback_data="post_add_button")],
        [InlineKeyboardButton("✅ Done", callback_data="post_done_buttons")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")],
    ])


def channel_select_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Keyboard for selecting target channel(s)."""
    user_channels = get_user_channels(user_id)
    rows: List[List[InlineKeyboardButton]] = []

    if not user_channels:
        rows.append([InlineKeyboardButton("⚠️ No channels added yet", callback_data="noop")])
    else:
        for ch in user_channels:
            rows.append([
                InlineKeyboardButton(
                    ch["title"],
                    callback_data=f"target_channel:{ch['id']}",
                )
            ])
        if len(user_channels) > 1:
            rows.append([InlineKeyboardButton("📤 Post to ALL Channels", callback_data="target_all")])

    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")])
    return InlineKeyboardMarkup(rows)


def channel_remove_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Keyboard for selecting channel to remove."""
    user_channels = get_user_channels(user_id)
    rows: List[List[InlineKeyboardButton]] = []

    if not user_channels:
        rows.append([InlineKeyboardButton("⚠️ No channels to remove", callback_data="noop")])
    else:
        for ch in user_channels:
            rows.append([
                InlineKeyboardButton(
                    f"🗑️ {ch['title']}",
                    callback_data=f"remove_channel:{ch['id']}",
                )
            ])

    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")])
    return InlineKeyboardMarkup(rows)


def build_buttons_markup(post: Dict[str, Any]) -> Optional[InlineKeyboardMarkup]:
    """Build inline keyboard from post buttons."""
    buttons = post.get("buttons") or []
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(text=b["text"], url=b["url"])] for b in buttons]
    return InlineKeyboardMarkup(rows)


# ----------------- COMMAND HANDLERS -----------------

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_: Client, message: Message):
    """Handle /start command."""
    reset_session(message.from_user.id)
    LOGGER.info("User %s started the bot", message.from_user.id)
    
    await message.reply_text(
        "👋 **Welcome to Channel Controller Bot!**\n\n"
        "This bot lets you:\n"
        "• Add channels where the bot is admin\n"
        "• Create posts (text/photo/video)\n"
        "• Add inline buttons to posts\n"
        "• Send to one or all channels\n\n"
        "Choose an option below to get started:",
        reply_markup=main_menu(),
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(_: Client, message: Message):
    """Handle /cancel command."""
    reset_session(message.from_user.id)
    LOGGER.info("User %s cancelled action", message.from_user.id)
    await message.reply_text("✅ Current action cancelled.", reply_markup=main_menu())


@app.on_message(filters.command("help") & filters.private)
async def cmd_help(_: Client, message: Message):
    """Handle /help command."""
    await message.reply_text(
        "**📖 How to use this bot:**\n\n"
        "**1️⃣ Add a Channel**\n"
        "   • Make sure the bot is admin in your channel\n"
        "   • Forward any message from that channel to the bot\n"
        "   • Or send the channel username (@username) or link\n\n"
        "**2️⃣ Create a Post**\n"
        "   • Choose 'New Post'\n"
        "   • Send text, photo, or video\n"
        "   • Optionally add inline buttons\n"
        "   • Select target channel(s)\n\n"
        "**3️⃣ Remove a Channel**\n"
        "   • Choose 'Remove Channel'\n"
        "   • Select the channel to remove\n\n"
        "**Commands:**\n"
        "/start - Show main menu\n"
        "/cancel - Cancel current action\n"
        "/help - Show this help message",
        reply_markup=main_menu(),
    )


# ----------------- PRIVATE MESSAGE HANDLER (STATES) -----------------

@app.on_message(filters.private & ~filters.command(["start", "cancel", "help"]))
async def private_messages(client: Client, message: Message):
    """Handle all private messages based on user state."""
    user_id = message.from_user.id
    session = get_session(user_id)
    state = session["state"]

    LOGGER.debug("User %s in state '%s' sent message", user_id, state)

    if state == "await_channel_forward":
        await handle_channel_forward(client, message, session)
    elif state == "await_post_content":
        await handle_post_content(client, message, session)
    elif state == "await_button_text":
        await handle_button_text(client, message, session)
    elif state == "await_button_url":
        await handle_button_url(client, message, session)
    else:
        # Idle or unknown state - show menu
        await message.reply_text("Choose an option:", reply_markup=main_menu())


# ----------------- STATE HANDLERS -----------------

async def handle_channel_forward(client: Client, message: Message, session: Dict[str, Any]):
    """Handle channel forward/identification."""
    user_id = message.from_user.id
    fwd_chat = message.forward_from_chat

    LOGGER.debug(
        "Processing channel forward: user=%s, forward_from_chat=%s",
        user_id,
        fwd_chat.id if fwd_chat else None
    )

    # If no forward info, try to interpret message text as channel reference
    if not fwd_chat:
        text = (message.text or "").strip()
        identifier = None

        if text.startswith("@"):
            identifier = text
        elif "t.me/" in text:
            try:
                part = text.split("t.me/")[-1].split("?")[0].strip().strip("/")
                if not part.startswith("joinchat/") and part:
                    identifier = part
            except Exception as e:
                LOGGER.error("Error parsing t.me link: %s", e)
                identifier = None
        elif text.lstrip("-").isdigit():
            try:
                identifier = int(text)
            except Exception as e:
                LOGGER.error("Error parsing chat ID: %s", e)
                identifier = None

        if identifier:
            try:
                chat = await client.get_chat(identifier)
                fwd_chat = chat
                LOGGER.info("Resolved chat from identifier: %s -> %s", identifier, chat.id)
            except PeerIdInvalid:
                await message.reply_text(
                    "⚠️ **Invalid channel identifier.**\n\n"
                    "Please ensure:\n"
                    "• The channel username is correct\n"
                    "• The channel is public\n"
                    "• You've provided a valid link",
                    reply_markup=cancel_keyboard(),
                )
                return
            except ChannelPrivate:
                await message.reply_text(
                    "⚠️ **This channel is private.**\n\n"
                    "For private channels, please forward a message from the channel instead.",
                    reply_markup=cancel_keyboard(),
                )
                return
            except Exception as e:
                LOGGER.error("Error resolving chat identifier %s: %s", identifier, e)
                await message.reply_text(
                    f"⚠️ **Error accessing channel:** {str(e)}\n\n"
                    "Please try again or use a different method.",
                    reply_markup=cancel_keyboard(),
                )
                return

    if not fwd_chat:
        await message.reply_text(
            "⚠️ **Invalid input.**\n\n"
            "Please provide one of the following:\n"
            "• Forward any message from the channel\n"
            "• Send the channel username (e.g., `@channelname`)\n"
            "• Send a public channel link (e.g., `https://t.me/channelname`)",
            reply_markup=cancel_keyboard(),
        )
        return

    # Verify it's a channel
    if fwd_chat.type != ChatType.CHANNEL:
        await message.reply_text(
            "⚠️ **This is not a channel.**\n\n"
            "Please provide a channel where the bot is admin.",
            reply_markup=cancel_keyboard(),
        )
        return

    # Verify bot is admin
    try:
        bot_member = await client.get_chat_member(fwd_chat.id, "me")
        if not bot_member.privileges or not bot_member.privileges.can_post_messages:
            await message.reply_text(
                f"⚠️ **Bot is not admin in {fwd_chat.title}**\n\n"
                "Please make the bot an admin with 'Post Messages' permission.",
                reply_markup=cancel_keyboard(),
            )
            return
    except ChatAdminRequired:
        await message.reply_text(
            f"⚠️ **Bot is not admin in {fwd_chat.title}**\n\n"
            "Please make the bot an admin with 'Post Messages' permission.",
            reply_markup=cancel_keyboard(),
        )
        return
    except Exception as e:
        LOGGER.error("Error checking admin status in %s: %s", fwd_chat.id, e)
        await message.reply_text(
            f"⚠️ **Error verifying bot permissions:** {str(e)}\n\n"
            "Please ensure the bot is admin in the channel.",
            reply_markup=cancel_keyboard(),
        )
        return

    # Add channel
    added = add_user_channel(user_id, fwd_chat.id, fwd_chat.title or str(fwd_chat.id))
    session["state"] = "idle"

    if added:
        await message.reply_text(
            f"✅ **Channel added successfully!**\n\n"
            f"**Channel:** {fwd_chat.title}\n"
            f"**ID:** `{fwd_chat.id}`\n\n"
            "You can now create posts for this channel.",
            reply_markup=main_menu(),
        )
    else:
        await message.reply_text(
            f"ℹ️ **Channel already exists!**\n\n"
            f"**Channel:** {fwd_chat.title}\n"
            f"**ID:** `{fwd_chat.id}`",
            reply_markup=main_menu(),
        )


async def handle_post_content(_: Client, message: Message, session: Dict[str, Any]):
    """Handle post content input."""
    user_id = message.from_user.id

    post: Dict[str, Any] = {
        "type": None,
        "text": None,
        "file_id": None,
        "caption": None,
        "buttons": [],
    }

    if message.photo:
        post["type"] = "photo"
        post["file_id"] = message.photo.file_id
        post["caption"] = message.caption or ""
        LOGGER.info("User %s created photo post", user_id)
    elif message.video:
        post["type"] = "video"
        post["file_id"] = message.video.file_id
        post["caption"] = message.caption or ""
        LOGGER.info("User %s created video post", user_id)
    elif message.text:
        post["type"] = "text"
        post["text"] = message.text
        LOGGER.info("User %s created text post", user_id)
    else:
        await message.reply_text(
            "⚠️ **Unsupported content type.**\n\n"
            "Please send:\n"
            "• Text message\n"
            "• Photo (with optional caption)\n"
            "• Video (with optional caption)",
            reply_markup=cancel_keyboard(),
        )
        return

    session["post"] = post
    session["state"] = "await_add_buttons"

    await message.reply_text(
        "✅ **Post content received!**\n\n"
        "Would you like to add inline buttons (links) to this post?",
        reply_markup=add_button_or_skip_keyboard(),
    )


async def handle_button_text(_: Client, message: Message, session: Dict[str, Any]):
    """Handle button text input."""
    text = message.text
    if not text:
        await message.reply_text(
            "⚠️ Please send the **button text** as a message.\n\n"
            "Example: `Visit Website`",
            reply_markup=cancel_keyboard(),
        )
        return

    if len(text) > 100:
        await message.reply_text(
            "⚠️ **Button text is too long** (max 100 characters).\n\n"
            "Please send a shorter text.",
            reply_markup=cancel_keyboard(),
        )
        return

    session["tmp_button_text"] = text
    session["state"] = "await_button_url"
    
    await message.reply_text(
        f"✅ Button text: **{text}**\n\n"
        "Now send the **URL** for this button.\n\n"
        "Example: `https://example.com`",
        reply_markup=cancel_keyboard(),
    )


async def handle_button_url(_: Client, message: Message, session: Dict[str, Any]):
    """Handle button URL input."""
    url = (message.text or "").strip()
    
    if not url:
        await message.reply_text(
            "⚠️ Please send a valid URL.",
            reply_markup=cancel_keyboard(),
        )
        return
    
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply_text(
            "⚠️ **Invalid URL format.**\n\n"
            "URL must start with `http://` or `https://`\n\n"
            "Example: `https://example.com`",
            reply_markup=cancel_keyboard(),
        )
        return

    if not session.get("post"):
        reset_session(message.from_user.id)
        await message.reply_text(
            "⚠️ **Session expired.**\n\n"
            "Please start a new post.",
            reply_markup=main_menu(),
        )
        return

    btn_text = session.get("tmp_button_text") or "Button"
    session["post"]["buttons"].append({"text": btn_text, "url": url})
    session["tmp_button_text"] = None
    session["state"] = "await_add_buttons"

    buttons_count = len(session["post"]["buttons"])
    
    await message.reply_text(
        f"✅ **Button added!**\n\n"
        f"**Text:** {btn_text}\n"
        f"**URL:** {url}\n\n"
        f"**Total buttons:** {buttons_count}",
        disable_web_page_preview=True,
        reply_markup=done_more_buttons_keyboard(),
    )


# ----------------- CALLBACK QUERIES -----------------

@app.on_callback_query()
async def callbacks(client: Client, query: CallbackQuery):
    """Handle all callback queries."""
    user_id = query.from_user.id
    data = query.data or ""
    session = get_session(user_id)

    LOGGER.debug("User %s pressed button: %s", user_id, data)

    # Ignore dummy callbacks
    if data == "noop":
        await query.answer("No action available", show_alert=False)
        return

    # Menu: New Post
    if data == "menu_new_post":
        channels = get_user_channels(user_id)
        if not channels:
            await query.answer("Please add a channel first!", show_alert=True)
            await client.send_message(
                user_id,
                "⚠️ **No channels available.**\n\n"
                "Please add a channel first using the 'Add Channel' button.",
                reply_markup=main_menu(),
            )
            return
        
        session["state"] = "await_post_content"
        session["post"] = None
        session["tmp_button_text"] = None
        await query.answer()
        await client.send_message(
            user_id,
            "📝 **Create New Post**\n\n"
            "Send me the post content:\n\n"
            "• **Text** - Just type your message\n"
            "• **Photo** - Send a photo with optional caption\n"
            "• **Video** - Send a video with optional caption",
            reply_markup=cancel_keyboard(),
        )
        return

    # Menu: Add Channel
    if data == "menu_add_channel":
        session["state"] = "await_channel_forward"
        await query.answer()
        await client.send_message(
            user_id,
            "📡 **Add a Channel**\n\n"
            "To add a channel, you can:\n\n"
            "**1.** Forward any message from the channel to me\n"
            "**2.** Send the channel username (e.g., `@channelname`)\n"
            "**3.** Send the channel link (e.g., `https://t.me/channelname`)\n\n"
            "⚠️ **Important:** The bot must be an admin in the channel!",
            reply_markup=cancel_keyboard(),
        )
        return

    # Menu: Remove Channel
    if data == "menu_remove_channel":
        channels = get_user_channels(user_id)
        if not channels:
            await query.answer("No channels to remove!", show_alert=True)
            return
        
        await query.answer()
        await client.send_message(
            user_id,
            "🗑️ **Remove a Channel**\n\n"
            "Select the channel you want to remove:",
            reply_markup=channel_remove_keyboard(user_id),
        )
        return

    # Menu: Cancel
    if data == "menu_cancel":
        reset_session(user_id)
        await query.answer("Cancelled")
        await client.send_message(
            user_id,
            "❌ **Action cancelled.**",
            reply_markup=main_menu()
        )
        return

    # Post: Add Button
    if data == "post_add_button":
        session["state"] = "await_button_text"
        await query.answer()
        await client.send_message(
            user_id,
            "➕ **Add Inline Button**\n\n"
            "Send the **button text** (what users will see).\n\n"
            "Example: `Visit Website` or `Join Channel`",
            reply_markup=cancel_keyboard(),
        )
        return

    # Post: Skip Buttons
    if data == "post_skip_buttons":
        session["state"] = "await_channel_select"
        await query.answer()
        await client.send_message(
            user_id,
            "📤 **Select Target Channel**\n\n"
            "Where do you want to post this?",
            reply_markup=channel_select_keyboard(user_id),
        )
        return

    # Post: Done with Buttons
    if data == "post_done_buttons":
        session["state"] = "await_channel_select"
        await query.answer()
        
        buttons_count = len(session.get("post", {}).get("buttons", []))
        await client.send_message(
            user_id,
            f"✅ **Buttons configured!**\n\n"
            f"Total buttons: {buttons_count}\n\n"
            "📤 **Select Target Channel**\n\n"
            "Where do you want to post this?",
            reply_markup=channel_select_keyboard(user_id),
        )
        return

    # Channel Selection or Remove
    if data.startswith("target_channel:") or data == "target_all":
        await query.answer()
        await handle_post_target(client, query, session)
        return

    if data.startswith("remove_channel:"):
        await query.answer()
        await handle_channel_remove(client, query, session)
        return

    # Unknown callback
    await query.answer("Unknown action", show_alert=False)


async def handle_channel_remove(client: Client, query: CallbackQuery, session: Dict[str, Any]):
    """Handle channel removal."""
    user_id = query.from_user.id
    
    try:
        ch_id = int(query.data.split(":", 1)[1])
    except Exception as e:
        LOGGER.error("Error parsing channel ID from callback: %s", e)
        await client.send_message(
            user_id,
            "⚠️ **Error processing request.**",
            reply_markup=main_menu(),
        )
        return
    
    # Find channel title before removing
    channels = get_user_channels(user_id)
    channel_title = next((c["title"] for c in channels if c["id"] == ch_id), "Unknown")
    
    removed = remove_user_channel(user_id, ch_id)
    
    if removed:
        await client.send_message(
            user_id,
            f"✅ **Channel removed successfully!**\n\n"
            f"**Channel:** {channel_title}\n"
            f"**ID:** `{ch_id}`",
            reply_markup=main_menu(),
        )
    else:
        await client.send_message(
            user_id,
            "⚠️ **Channel not found.**",
            reply_markup=main_menu(),
        )


async def handle_post_target(client: Client, query: CallbackQuery, session: Dict[str, Any]):
    """Handle post distribution to selected channel(s)."""
    user_id = query.from_user.id
    data = query.data

    post = session.get("post")
    if not post:
        reset_session(user_id)
        await client.send_message(
            user_id,
            "⚠️ **Session expired.**\n\n"
            "Please create the post again.",
            reply_markup=main_menu(),
        )
        return

    user_channels = get_user_channels(user_id)
    if not user_channels:
        reset_session(user_id)
        await client.send_message(
            user_id,
            "⚠️ **No channels available.**\n\n"
            "Please add a channel first.",
            reply_markup=main_menu(),
        )
        return

    # Determine target channel IDs
    target_ids: List[int] = []
    if data == "target_all":
        target_ids = [c["id"] for c in user_channels]
    else:
        try:
            ch_id = int(data.split(":", 1)[1])
            target_ids = [ch_id]
        except Exception as e:
            LOGGER.error("Error parsing target channel ID: %s", e)
            await client.send_message(
                user_id,
                "⚠️ **Invalid channel selection.**",
                reply_markup=main_menu(),
            )
            return

    markup = build_buttons_markup(post)
    
    sent_to = 0
    failed = 0
    error_details = []

    # Send to each target channel
    for cid in target_ids:
        channel_title = next((c["title"] for c in user_channels if c["id"] == cid), str(cid))
        
        try:
            if post["type"] == "text":
                await client.send_message(cid, post["text"], reply_markup=markup)
            elif post["type"] == "photo":
                await client.send_photo(
                    cid,
                    post["file_id"],
                    caption=post.get("caption") or "",
                    reply_markup=markup,
                )
            elif post["type"] == "video":
                await client.send_video(
                    cid,
                    post["file_id"],
                    caption=post.get("caption") or "",
                    reply_markup=markup,
                )
            
            sent_to += 1
            LOGGER.info("Successfully posted to channel %s (%s)", channel_title, cid)
            
        except ChatAdminRequired:
            failed += 1
            error_details.append(f"❌ {channel_title}: Bot is not admin")
            LOGGER.error("Bot is not admin in channel %s (%s)", channel_title, cid)
        except ChannelPrivate:
            failed += 1
            error_details.append(f"❌ {channel_title}: Channel is private")
            LOGGER.error("Channel %s (%s) is private or bot was removed", channel_title, cid)
        except FloodWait as e:
            failed += 1
            error_details.append(f"❌ {channel_title}: Rate limited ({e.value}s)")
            LOGGER.error("FloodWait error for channel %s: %s seconds", cid, e.value)
        except UserIsBlocked:
            failed += 1
            error_details.append(f"❌ {channel_title}: Bot is blocked")
            LOGGER.error("Bot is blocked by channel %s", cid)
        except Exception as e:
            failed += 1
            error_details.append(f"❌ {channel_title}: {str(e)[:50]}")
            LOGGER.error("Error sending to channel %s (%s): %s", channel_title, cid, e)

    reset_session(user_id)

    # Build result message
    result_msg = "📊 **Posting Complete!**\n\n"
    result_msg += f"✅ **Sent:** {sent_to}\n"
    result_msg += f"❌ **Failed:** {failed}\n"
    
    if error_details:
        result_msg += "\n**Error Details:**\n"
        result_msg += "\n".join(error_details[:5])  # Limit to first 5 errors
        if len(error_details) > 5:
            result_msg += f"\n...and {len(error_details) - 5} more"

    await client.send_message(
        user_id,
        result_msg,
        reply_markup=main_menu(),
    )


# ----------------- ERROR HANDLER -----------------

@app.on_message(filters.private)
async def error_handler(_: Client, message: Message):
    """Catch-all error handler for unexpected messages."""
    # This handler has lowest priority and catches anything not handled above
    pass


# ----------------- HEALTH CHECK SERVER FOR RENDER -----------------

async def health_check(request):
    """Health check endpoint for uptime monitoring."""
    return web.Response(text="Bot is running!", status=200)


async def run_health_server():
    """Run aiohttp web server for health checks (Render requirement)."""
    app_web = web.Application()
    app_web.router.add_get('/', health_check)
    app_web.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app_web)
    await runner.setup()
    
    # Render provides PORT env variable
    port = int(os.getenv('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    LOGGER.info("✅ Health check server started on 0.0.0.0:%d", port)
    LOGGER.info("✅ Health endpoints: / and /health")
    
    # Keep the server running indefinitely
    while True:
        await asyncio.sleep(3600)  # Sleep for 1 hour, repeat forever


# ----------------- MAIN -----------------

async def start_bot():
    """Start the bot and health check server."""
    load_channels()
    
    LOGGER.info("=" * 50)
    LOGGER.info("Bot starting...")
    LOGGER.info("API_ID: %s", API_ID)
    LOGGER.info("Channels loaded: %d users", len(channels_data))
    LOGGER.info("=" * 50)
    
    # Start the Pyrogram client
    await app.start()
    LOGGER.info("✅ Pyrogram client started!")
    
    # Run health server and idle concurrently
    await asyncio.gather(
        run_health_server(),  # Health check server
        idle()                # Keep bot running
    )


def main():
    """Main entry point."""
    try:
        app.run(start_bot())
        
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped by user")
    except Exception as e:
        LOGGER.critical("Fatal error: %s", e, exc_info=True)
        raise
    finally:
        LOGGER.info("Bot shutdown complete")


if __name__ == "__main__":
    main()