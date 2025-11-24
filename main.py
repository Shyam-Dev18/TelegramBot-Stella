import os
import signal
import logging
import asyncio
import secrets
import string
from datetime import datetime

# Network and Web Server
from aiohttp import web

# Database
import motor.motor_asyncio

# Telegram Bot Library
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message, 
    CallbackQuery
)
from pyrogram.errors import (
    UserNotParticipant, 
    ChatAdminRequired, 
    ChannelPrivate,
    FloodWait,
    MessageNotModified
)

# Environment Variables
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# CONFIGURATION & SETUP
# --------------------------------------------------------------------------

# Load .env file
load_dotenv()

# Logging setup (helps debug issues on Render logs)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Get variables from environment
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
PORT = int(os.getenv("PORT", 10000))

# --------------------------------------------------------------------------
# DATABASE CONNECTION (MongoDB)
# --------------------------------------------------------------------------

# Connect to Mongo
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo_client[MONGO_DB_NAME]

# Collections
must_join_channels_col = db["must_join_channels"]   # Stores channels for FSub (user must join)
post_channels_col = db["post_channels"]             # Stores channels where bot posts content
fileshares_col = db["fileshares"]                   # Stores file links

# --------------------------------------------------------------------------
# BOT INITIALIZATION
# --------------------------------------------------------------------------

# Initialize Pyrogram Client
app = Client(
    "my_render_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True  # Good for ephemeral filesystems like Render
)

# --------------------------------------------------------------------------
# STATE MANAGEMENT (MEMORY)
# --------------------------------------------------------------------------

# Since this is a simple bot, we store wizard states in Python dictionaries.
# If the bot restarts, these current edit sessions are lost (persistent data is in Mongo).

# user_states stores what step the admin is on (e.g., "WAITING_FOR_CHANNEL_INPUT")
user_states = {} 

# post_cache stores the post the admin is currently building (Text, Media, Buttons)
post_cache = {}

# --------------------------------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------------------------------

def generate_random_token(length=8):
    """Generates a random string for deep linking."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

async def is_user_member(user_id, channel_id):
    """Checks if a user is a member of a specific channel."""
    try:
        member = await app.get_chat_member(channel_id, user_id)
        if member.status in [enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT]:
            return False
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        logger.error(f"Error checking membership: {e}")
        # If bot can't check (e.g. kicked), assume False to be safe
        return False

def get_cancel_button(callback_data="admin_cancel_action"):
    """Returns a standardized Cancel button."""
    return InlineKeyboardButton("❌ Cancel", callback_data=callback_data)

def get_back_button(callback_data="admin_back_main"):
    """Returns a standardized Back button."""
    return InlineKeyboardButton("🔙 Back", callback_data=callback_data)

def format_button_markup(buttons):
    """Format buttons: 2 per row, single button takes full width."""
    if not buttons:
        return None
    
    button_rows = []
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            # Two buttons side by side
            button_rows.append([
                InlineKeyboardButton(buttons[i][0], url=buttons[i][1]),
                InlineKeyboardButton(buttons[i+1][0], url=buttons[i+1][1])
            ])
        else:
            # Single button, full width
            button_rows.append([InlineKeyboardButton(buttons[i][0], url=buttons[i][1])])
    
    return InlineKeyboardMarkup(button_rows)

# --------------------------------------------------------------------------
# WEB SERVER (KEEP-ALIVE)
# --------------------------------------------------------------------------

async def health_check(request):
    """Simple route to keep Render happy."""
    return web.Response(text="Bot is Running OK")

async def start_web_server():
    """Starts the aiohttp web server."""
    server = web.Application()
    server.router.add_get("/health", health_check)
    #server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    # Bind to 0.0.0.0 so outside world can access (required by Render)
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")

### Removed duplicated early admin handlers block to avoid double registration

# --------------------------------------------------------------------------
# ADMIN HANDLERS
# --------------------------------------------------------------------------

# 1. START COMMAND (ADMIN VIEW)
@app.on_message(filters.command("start") & filters.user(ADMIN_USER_ID) & filters.private)
async def admin_start(client, message):
    # Check if there is a deep link payload (e.g. /start code_123) even for admin
    command_parts = message.command
    if command_parts and len(command_parts) > 1:
        await user_start_handler(client, message)
        return

    # Admin Main Menu
    buttons = [
        [InlineKeyboardButton("📝 New Post", callback_data="admin_new_post")],
        [InlineKeyboardButton("📋 Manage Must Join Channels", callback_data="admin_manage_join_channels")],
        [InlineKeyboardButton("📢 Manage Post Channels", callback_data="admin_manage_post_channels")],
        [InlineKeyboardButton("📜 View All Channels", callback_data="admin_view_all_channels")]
    ]

    intro = (
        "**👮‍♂️ Admin Panel**\n\n"
        "Welcome to your FileShare Bot control center.\n"
        "You can create broadcast posts, add or remove forced-subscription channels, and view the list of channels.\n\n"
        "Use the buttons below to proceed. Sending any message will re-show this panel if you're not in a wizard." 
    )

    await message.reply_text(intro, reply_markup=InlineKeyboardMarkup(buttons))

# 2. MANAGE MUST JOIN CHANNELS
@app.on_callback_query(filters.regex("admin_manage_join_channels") & filters.user(ADMIN_USER_ID))
async def manage_join_channels_menu(client, callback_query):
    buttons = [
        [InlineKeyboardButton("➕ Add Join Channel", callback_data="add_join_channel")],
        [InlineKeyboardButton("➖ Remove Join Channel", callback_data="remove_join_channel")],
        [InlineKeyboardButton("📜 View Join Channels", callback_data="view_join_channels")],
        [get_back_button("admin_back_main")]
    ]
    
    await callback_query.message.edit_text(
        "**📋 Manage Must Join Channels**\n\n"
        "These channels users MUST join before downloading files.\n"
        "Choose an option below:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# 3. MANAGE POST CHANNELS  
@app.on_callback_query(filters.regex("admin_manage_post_channels") & filters.user(ADMIN_USER_ID))
async def manage_post_channels_menu(client, callback_query):
    buttons = [
        [InlineKeyboardButton("➕ Add Post Channel", callback_data="add_post_channel")],
        [InlineKeyboardButton("➖ Remove Post Channel", callback_data="remove_post_channel")],
        [InlineKeyboardButton("📜 View Post Channels", callback_data="view_post_channels")],
        [get_back_button("admin_back_main")]
    ]
    
    await callback_query.message.edit_text(
        "**📢 Manage Post Channels**\n\n"
        "These channels are where the bot will post your content.\n"
        "Choose an option below:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ADD JOIN CHANNEL
@app.on_callback_query(filters.regex("add_join_channel") & filters.user(ADMIN_USER_ID))
async def add_join_channel(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_JOIN_CHANNEL_INPUT"
    
    buttons = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_manage_join_channels")]]
    
    await callback_query.message.edit_text(
        "**➕ Add Must Join Channel**\n\n"
        "Please **Forward a message** from the channel to here, or send the **@username**.\n\n"
        "⚠️ *Note: Users will be required to join this channel before downloading files!*",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ADD POST CHANNEL
@app.on_callback_query(filters.regex("add_post_channel") & filters.user(ADMIN_USER_ID))
async def add_post_channel(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_POST_CHANNEL_INPUT"
    
    buttons = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_manage_post_channels")]]
    
    await callback_query.message.edit_text(
        "**➕ Add Post Channel**\n\n"
        "Please **Forward a message** from the channel to here, or send the **@username**.\n\n"
        "⚠️ *Note: Make sure I am an Admin in that channel first!*",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_message(filters.user(ADMIN_USER_ID) & filters.private)
async def handle_admin_inputs(client, message):
    state = user_states.get(message.from_user.id)

    # If no active wizard state, show intro panel (requirement #1)
    if not state:
        # Ensure stale state cleared
        user_states.pop(message.from_user.id, None)
        await admin_start(client, message)
        return

    # --- HANDLE ADD JOIN CHANNEL INPUT ---
    if state == "WAITING_JOIN_CHANNEL_INPUT":
        chat_id = None
        chat_title = None
        chat_username = None

        # Try to get chat details from Forward
        if message.forward_from_chat:
            chat_id = message.forward_from_chat.id
            chat_title = message.forward_from_chat.title
            chat_username = message.forward_from_chat.username
        # Try to get chat details from Text (@username)
        elif message.text:
            try:
                chat = await client.get_chat(message.text)
                chat_id = chat.id
                chat_title = chat.title
                chat_username = chat.username
            except Exception:
                await message.reply("❌ Could not find that channel. Ensure the username is correct.")
                return

        if chat_id:
            # Save to Must Join Channels DB
            try:
                existing = await must_join_channels_col.find_one({"channel_id": chat_id})
                if existing:
                    await message.reply("ℹ️ This channel is already in the must join list.")
                else:
                    await must_join_channels_col.insert_one({
                        "channel_id": chat_id,
                        "title": chat_title,
                        "username": chat_username,
                        "added_at": datetime.utcnow()
                    })
                    await message.reply(f"✅ **Successfully Added to Must Join List:** {chat_title}")
            except Exception as e:
                logger.error(f"DB Error adding join channel: {e}")
                await message.reply("❌ Database error occurred.")
        
        # Reset State and go back to join channels menu
        user_states.pop(message.from_user.id, None)
        # Send a new message with the join channels menu
        buttons = [
            [InlineKeyboardButton("➕ Add Join Channel", callback_data="add_join_channel")],
            [InlineKeyboardButton("➖ Remove Join Channel", callback_data="remove_join_channel")],
            [InlineKeyboardButton("📜 View Join Channels", callback_data="view_join_channels")],
            [get_back_button("admin_back_main")]
        ]
        await message.reply(
            "**📋 Manage Must Join Channels**\n\n"
            "These channels users MUST join before downloading files.\n"
            "Choose an option below:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # --- HANDLE ADD POST CHANNEL INPUT ---
    if state == "WAITING_POST_CHANNEL_INPUT":
        chat_id = None
        chat_title = None
        chat_username = None

        # Try to get chat details from Forward
        if message.forward_from_chat:
            chat_id = message.forward_from_chat.id
            chat_title = message.forward_from_chat.title
            chat_username = message.forward_from_chat.username
        # Try to get chat details from Text (@username)
        elif message.text:
            try:
                chat = await client.get_chat(message.text)
                chat_id = chat.id
                chat_title = chat.title
                chat_username = chat.username
            except Exception:
                await message.reply("❌ Could not find that channel. Ensure I am admin there or check the username.")
                return

        if chat_id:
            # Verify Admin Status for post channels
            try:
                member = await client.get_chat_member(chat_id, "me")
                if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
                    await message.reply("⚠️ I am not an Admin in that channel. Please promote me and try again.")
                    return
            except Exception as e:
                await message.reply(f"❌ Error accessing channel: {e}")
                return

            # Save to Post Channels DB
            try:
                existing = await post_channels_col.find_one({"channel_id": chat_id})
                if existing:
                    await message.reply("ℹ️ This channel is already in the post channels list.")
                else:
                    await post_channels_col.insert_one({
                        "channel_id": chat_id,
                        "title": chat_title,
                        "username": chat_username,
                        "added_at": datetime.utcnow()
                    })
                    await message.reply(f"✅ **Successfully Added to Post Channels:** {chat_title}")
            except Exception as e:
                logger.error(f"DB Error adding post channel: {e}")
                await message.reply("❌ Database error occurred.")
        
        # Reset State and go back to post channels menu
        user_states.pop(message.from_user.id, None)
        # Send a new message with the post channels menu
        buttons = [
            [InlineKeyboardButton("➕ Add Post Channel", callback_data="add_post_channel")],
            [InlineKeyboardButton("➖ Remove Post Channel", callback_data="remove_post_channel")],
            [InlineKeyboardButton("📜 View Post Channels", callback_data="view_post_channels")],
            [get_back_button("admin_back_main")]
        ]
        await message.reply(
            "**📢 Manage Post Channels**\n\n"
            "These channels are where the bot will post your content.\n"
            "Choose an option below:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # --- HANDLE TEXT EDITING ---
    if state == "WAITING_TEXT_EDIT":
        if not message.text and not message.caption:
            await message.reply("❌ Please send text content.")
            return
            
        # Update text and entities in cache
        new_text = message.text or message.caption or ""
        new_entities = message.entities or message.caption_entities or []
        
        if message.from_user.id in post_cache:
            post_cache[message.from_user.id]['text'] = new_text
            post_cache[message.from_user.id]['entities'] = new_entities
            await message.reply("✅ **Text Updated Successfully!**")
        else:
            await message.reply("❌ Session expired. Please start creating a new post.")
            
        user_states[message.from_user.id] = "BUILDING_POST"
        await show_post_builder_menu(client, message.chat.id)
        return

    # --- HANDLE NEW POST WIZARD: CONTENT ---
    if state == "WAITING_POST_CONTENT":
        # Store basic content info with entities for styled text
        cache = {
            "type": "text",
            "text": message.text or message.caption or "",
            "entities": message.entities or message.caption_entities or [],
            "file_id": None,
            "buttons": [], # List of [text, url]
            "attached_file_token": None
        }

        if message.photo:
            cache["type"] = "photo"
            cache["file_id"] = message.photo.file_id
        elif message.video:
            cache["type"] = "video"
            cache["file_id"] = message.video.file_id
        elif message.document: # Fallback if they send document as content
            cache["type"] = "document"
            cache["file_id"] = message.document.file_id
        elif message.audio:
            cache["type"] = "audio"
            cache["file_id"] = message.audio.file_id

        post_cache[message.from_user.id] = cache
        user_states[message.from_user.id] = "BUILDING_POST"
        
        # Show the Builder Menu
        await show_post_builder_menu(client, message.chat.id)
        return

    # --- HANDLE NEW POST WIZARD: URL BUTTONS ---
    if state == "WAITING_URL_BUTTONS":
        lines = message.text.split('\n')
        buttons_added = 0
        for line in lines:
            if '-' in line:
                parts = line.split('-', 1)
                text = parts[0].strip()
                url = parts[1].strip()
                post_cache[message.from_user.id]['buttons'].append([text, url])
                buttons_added += 1
        
        user_states[message.from_user.id] = "BUILDING_POST"
        await message.reply(f"✅ Added {buttons_added} buttons.")
        await show_post_builder_menu(client, message.chat.id)
        return

    # --- HANDLE NEW POST WIZARD: FILE ATTACHMENT ---
    if state == "WAITING_FILE_ATTACH":
        if not message.media:
            await message.reply("❌ Please send a file (Photo, Video, or Document).")
            return

        # Get File ID and info
        file_id = None
        file_name = "File"
        file_type = "document"
        
        # Determine type and ID
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or "Document"
            file_type = "document"
        elif message.video:
            file_id = message.video.file_id
            file_name = "Video"
            file_type = "video"
        elif message.photo:
            file_id = message.photo.file_id
            file_name = "Photo"
            file_type = "photo"
        elif message.audio:
            file_id = message.audio.file_id
            file_name = "Audio"
            file_type = "audio"

        # Store file info temporarily and ask for button title
        post_cache[message.from_user.id]['temp_file'] = {
            "file_id": file_id,
            "file_type": file_type,
            "file_name": file_name,
            "caption": message.caption or ""
        }
        
        user_states[message.from_user.id] = "WAITING_BUTTON_TITLE"
        buttons = [
            [InlineKeyboardButton("🔙 Back", callback_data="wiz_back_attach_file")],
            [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")]
        ]
        
        await message.reply(
            f"📎 **File Received: {file_name}**\n\n"
            "Now please send the **button title** you want users to see.\n"
            "For example: `📥 Download Movie` or `🎵 Get Audio`",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # --- HANDLE BUTTON TITLE INPUT ---
    if state == "WAITING_BUTTON_TITLE":
        if not message.text:
            await message.reply("❌ Please send text for the button title.")
            return
            
        button_title = message.text.strip()
        temp_file = post_cache[message.from_user.id].get('temp_file')
        
        if not temp_file:
            await message.reply("❌ Session expired. Please attach the file again.")
            user_states[message.from_user.id] = "BUILDING_POST"
            await show_post_builder_menu(client, message.chat.id)
            return

        # Generate Token and Save to DB
        try:
            token = generate_random_token()
            bot_username = (await client.get_me()).username
            
            await fileshares_col.insert_one({
                "token": token,
                "file_id": temp_file["file_id"],
                "file_type": temp_file["file_type"],
                "file_name": temp_file["file_name"],
                "caption": temp_file["caption"],
                "button_title": button_title,
                "created_at": datetime.utcnow()
            })

            # Add custom button to the post
            deep_link = f"https://t.me/{bot_username}?start={token}"
            post_cache[message.from_user.id]['buttons'].append([button_title, deep_link])
            post_cache[message.from_user.id]['attached_file_token'] = token
            
            # Clear temp file data
            post_cache[message.from_user.id].pop('temp_file', None)

            user_states[message.from_user.id] = "BUILDING_POST"
            await message.reply(f"✅ File Attached with Button: **{button_title}**")
            await show_post_builder_menu(client, message.chat.id)
        except Exception as e:
            logger.error(f"Error saving file share: {e}")
            await message.reply("❌ Error saving file info to database.")
        return

# REMOVE JOIN CHANNEL HANDLER
@app.on_callback_query(filters.regex("remove_join_channel") & filters.user(ADMIN_USER_ID))
async def remove_join_channel_list(client, callback_query):
    try:
        channels = must_join_channels_col.find({})
        buttons = []
        async for ch in channels:
            btn_text = f"{ch.get('title', 'Unknown')} (ID: {ch['channel_id']})"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"rm_join_ch_{ch['channel_id']}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_join_channels")])
        
        if not buttons:
            await callback_query.answer("No join channels found!", show_alert=True)
            return

        await callback_query.message.edit_text(
            "🗑 **Tap a Must Join channel to remove it:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error listing join channels: {e}")
        await callback_query.answer("Error loading channels.", show_alert=True)

@app.on_callback_query(filters.regex(r"^rm_join_ch_") & filters.user(ADMIN_USER_ID))
async def confirm_remove_join_channel(client, callback_query):
    try:
        channel_id = int(callback_query.data.split("_")[3])
        await must_join_channels_col.delete_one({"channel_id": channel_id})
        await callback_query.answer("✅ Join Channel Removed", show_alert=True)
        # Refresh list
        await remove_join_channel_list(client, callback_query)
    except Exception as e:
        logger.error(e)
        await callback_query.answer("Error removing channel.", show_alert=True)

# REMOVE POST CHANNEL HANDLER
@app.on_callback_query(filters.regex("remove_post_channel") & filters.user(ADMIN_USER_ID))
async def remove_post_channel_list(client, callback_query):
    try:
        channels = post_channels_col.find({})
        buttons = []
        async for ch in channels:
            btn_text = f"{ch.get('title', 'Unknown')} (ID: {ch['channel_id']})"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"rm_post_ch_{ch['channel_id']}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_post_channels")])
        
        if not buttons:
            await callback_query.answer("No post channels found!", show_alert=True)
            return

        await callback_query.message.edit_text(
            "🗑 **Tap a Post channel to remove it:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error listing post channels: {e}")
        await callback_query.answer("Error loading channels.", show_alert=True)

@app.on_callback_query(filters.regex(r"^rm_post_ch_") & filters.user(ADMIN_USER_ID))
async def confirm_remove_post_channel(client, callback_query):
    try:
        channel_id = int(callback_query.data.split("_")[3])
        await post_channels_col.delete_one({"channel_id": channel_id})
        await callback_query.answer("✅ Post Channel Removed", show_alert=True)
        # Refresh list
        await remove_post_channel_list(client, callback_query)
    except Exception as e:
        logger.error(e)
        await callback_query.answer("Error removing channel.", show_alert=True)

@app.on_callback_query(filters.regex("admin_back_main") & filters.user(ADMIN_USER_ID))
async def back_to_main(client, callback_query):
    # Just call the start command logic visually
    buttons = [
        [InlineKeyboardButton("📝 New Post", callback_data="admin_new_post")],
        [InlineKeyboardButton("📋 Manage Must Join Channels", callback_data="admin_manage_join_channels")],
        [InlineKeyboardButton("📢 Manage Post Channels", callback_data="admin_manage_post_channels")],
        [InlineKeyboardButton("📜 View All Channels", callback_data="admin_view_all_channels")]
    ]
    await callback_query.message.edit_text(
        "**👮‍♂️ Admin Panel**\nSend any message to return here.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# VIEW JOIN CHANNELS
@app.on_callback_query(filters.regex("view_join_channels") & filters.user(ADMIN_USER_ID))
async def view_join_channels(client, callback_query):
    try:
        channels_cursor = must_join_channels_col.find({})
        buttons = []
        async for ch in channels_cursor:
            title = ch.get('title', 'Channel')
            username = ch.get('username')
            if username:
                buttons.append([InlineKeyboardButton(title, url=f"https://t.me/{username}")])
            else:
                buttons.append([InlineKeyboardButton(f"{title} (No public link)", callback_data="noop")])

        if not buttons:
            buttons.append([InlineKeyboardButton("No must join channels added yet", callback_data="noop")])

        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_join_channels")])
        await callback_query.message.edit_text(
            "**📋 Must Join Channels List**\nUsers must join these to download files:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error showing join channels list: {e}")
        await callback_query.answer("Error loading list", show_alert=True)

# VIEW POST CHANNELS
@app.on_callback_query(filters.regex("view_post_channels") & filters.user(ADMIN_USER_ID))
async def view_post_channels(client, callback_query):
    try:
        channels_cursor = post_channels_col.find({})
        buttons = []
        async for ch in channels_cursor:
            title = ch.get('title', 'Channel')
            username = ch.get('username')
            if username:
                buttons.append([InlineKeyboardButton(title, url=f"https://t.me/{username}")])
            else:
                buttons.append([InlineKeyboardButton(f"{title} (No public link)", callback_data="noop")])

        if not buttons:
            buttons.append([InlineKeyboardButton("No post channels added yet", callback_data="noop")])

        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_post_channels")])
        await callback_query.message.edit_text(
            "**📢 Post Channels List**\nBot will post content to these channels:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error showing post channels list: {e}")
        await callback_query.answer("Error loading list", show_alert=True)

# VIEW ALL CHANNELS
@app.on_callback_query(filters.regex("admin_view_all_channels") & filters.user(ADMIN_USER_ID))
async def admin_view_all_channels(client, callback_query):
    try:
        # Get both types of channels
        join_channels = []
        post_channels = []
        
        async for ch in must_join_channels_col.find({}):
            join_channels.append(ch)
            
        async for ch in post_channels_col.find({}):
            post_channels.append(ch)

        buttons = []
        
        if join_channels:
            buttons.append([InlineKeyboardButton("📋 Must Join Channels:", callback_data="noop")])
            for ch in join_channels:
                title = ch.get('title', 'Channel')
                username = ch.get('username')
                if username:
                    buttons.append([InlineKeyboardButton(f"  └ {title}", url=f"https://t.me/{username}")])
                else:
                    buttons.append([InlineKeyboardButton(f"  └ {title} (private)", callback_data="noop")])
        
        if post_channels:
            if join_channels:  # Add separator if we have join channels too
                buttons.append([InlineKeyboardButton("━━━━━━━━━━━━━━━━━━━━", callback_data="noop")])
            buttons.append([InlineKeyboardButton("📢 Post Channels:", callback_data="noop")])
            for ch in post_channels:
                title = ch.get('title', 'Channel')
                username = ch.get('username')
                if username:
                    buttons.append([InlineKeyboardButton(f"  └ {title}", url=f"https://t.me/{username}")])
                else:
                    buttons.append([InlineKeyboardButton(f"  └ {title} (private)", callback_data="noop")])

        if not join_channels and not post_channels:
            buttons.append([InlineKeyboardButton("No channels added yet", callback_data="noop")])

        buttons.append([get_back_button("admin_back_main")])
        await callback_query.message.edit_text(
            "**📜 All Channels Overview**\nTap a channel to open (if public):",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error showing all channels list: {e}")
        await callback_query.answer("Error loading list", show_alert=True)

@app.on_callback_query(filters.regex("^noop$") & filters.user(ADMIN_USER_ID))
async def noop_handler(client, callback_query):
    await callback_query.answer("No public link available.", show_alert=True)



# GENERIC CANCEL HANDLER
@app.on_callback_query(filters.regex("admin_cancel_action") & filters.user(ADMIN_USER_ID))
async def admin_cancel_action(client, callback_query):
    user_states.pop(callback_query.from_user.id, None)
    await callback_query.answer("Action Cancelled")
    await back_to_main(client, callback_query)

# --------------------------------------------------------------------------
# NEW POST WIZARD (STATE MACHINE)
# --------------------------------------------------------------------------

@app.on_callback_query(filters.regex("admin_new_post") & filters.user(ADMIN_USER_ID))
async def start_new_post(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_POST_CONTENT"
    # Clear old cache
    post_cache.pop(callback_query.from_user.id, None)
    
    buttons = [[get_cancel_button()]]

    await callback_query.message.edit_text(
        "**📝 Create New Post**\n\n"
        "Send me the content now:\n"
        "- Text Message\n"
        "- Photo (with caption)\n"
        "- Video (with caption)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_post_builder_menu(client, chat_id):
    buttons = [
        [InlineKeyboardButton("✏️ Edit Text", callback_data="wiz_edit_text")],
        [InlineKeyboardButton("➕ Add URL Buttons", callback_data="wiz_add_btn"),
         InlineKeyboardButton("🗑️ Delete Buttons", callback_data="wiz_delete_buttons")],
        [InlineKeyboardButton("📎 Attach FileShare File", callback_data="wiz_attach_file")],
        [InlineKeyboardButton("▶️ Continue", callback_data="wiz_preview")],
        [get_cancel_button("wiz_cancel")]
    ]
    await client.send_message(
        chat_id,
        "**⚙️ Post Builder Menu**\n\nWhat would you like to add next?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("wiz_add_btn") & filters.user(ADMIN_USER_ID))
async def wiz_add_btn(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_URL_BUTTONS"
    buttons = [
        [InlineKeyboardButton("🔙 Back", callback_data="wiz_back_to_builder")],
        [get_cancel_button("wiz_cancel")]
    ]
    await callback_query.message.edit_text(
        "**Add URL Buttons**\n\n"
        "Send buttons in this format (one per line):\n"
        "`Button Text - https://link.com`\n"
        "`Join Us - https://t.me/example`",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("wiz_attach_file") & filters.user(ADMIN_USER_ID))
async def wiz_attach_file(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_FILE_ATTACH"
    buttons = [
        [InlineKeyboardButton("🔙 Back", callback_data="wiz_back_to_builder")],
        [get_cancel_button("wiz_cancel")]
    ]
    await callback_query.message.edit_text(
        "**📎 Attach File for FileShare**\n\n"
        "Forward or upload the file you want users to download.\n"
        "I will auto-generate a secured link and add a 'Download' button to this post.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("wiz_preview") & filters.user(ADMIN_USER_ID))
async def wiz_preview(client, callback_query):
    user_id = callback_query.from_user.id
    data = post_cache.get(user_id)
    
    if not data:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    # Build Markup from buttons with proper formatting
    markup = format_button_markup(data['buttons'])
    
    # Send Preview Header
    await callback_query.message.edit_text("⬇️ **PREVIEW BELOW** ⬇️")
    
    try:
        # Send preview with entities (styled text) preserved
        if data['type'] == "text":
            await client.send_message(
                user_id, 
                data['text'], 
                entities=data.get('entities', []),
                reply_markup=markup
            )
        elif data['type'] == "photo":
            await client.send_photo(
                user_id, 
                data['file_id'], 
                caption=data['text'],
                caption_entities=data.get('entities', []),
                reply_markup=markup
            )
        elif data['type'] == "video":
            await client.send_video(
                user_id, 
                data['file_id'], 
                caption=data['text'],
                caption_entities=data.get('entities', []),
                reply_markup=markup
            )
        elif data['type'] == "document":
            await client.send_document(
                user_id, 
                data['file_id'], 
                caption=data['text'],
                caption_entities=data.get('entities', []),
                reply_markup=markup
            )
        elif data['type'] == "audio":
            await client.send_audio(
                user_id, 
                data['file_id'], 
                caption=data['text'],
                caption_entities=data.get('entities', []),
                reply_markup=markup
            )
    except Exception as e:
        await client.send_message(user_id, f"❌ Error generating preview: {e}")
        return

    # Send Controls - Send and Back on top row, Cancel below
    control_buttons = [
        [InlineKeyboardButton("📤 Send", callback_data="wiz_send_menu"),
         InlineKeyboardButton("🔙 Back", callback_data="wiz_back_to_builder")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")]
    ]
    await client.send_message(
        user_id, 
        "**Is this preview correct?**", 
        reply_markup=InlineKeyboardMarkup(control_buttons)
    )

@app.on_callback_query(filters.regex("wiz_edit_text") & filters.user(ADMIN_USER_ID))
async def wiz_edit_text(client, callback_query):
    user_states[callback_query.from_user.id] = "WAITING_TEXT_EDIT"
    data = post_cache.get(callback_query.from_user.id, {})
    current_text = data.get('text', '')
    
    buttons = [
        [InlineKeyboardButton("🔙 Back", callback_data="wiz_back_to_builder")],
        [get_cancel_button("wiz_cancel")]
    ]
    
    message_text = "**✏️ Edit Text**\n\nSend new text to replace current content.\n\n"
    if current_text:
        message_text += f"**Current Text:**\n{current_text[:200]}{'...' if len(current_text) > 200 else ''}"
    else:
        message_text += "**No text content yet.** Send text to add content."
    
    await callback_query.message.edit_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("wiz_delete_buttons") & filters.user(ADMIN_USER_ID))
async def wiz_delete_buttons(client, callback_query):
    data = post_cache.get(callback_query.from_user.id, {})
    buttons_list = data.get('buttons', [])
    
    if not buttons_list:
        await callback_query.answer("No buttons to delete!", show_alert=True)
        return
    
    # Create buttons for deletion
    delete_buttons = []
    for i, btn in enumerate(buttons_list):
        delete_buttons.append([InlineKeyboardButton(f"🗑️ {btn[0]}", callback_data=f"del_btn_{i}")])
    
    delete_buttons.append([InlineKeyboardButton("✅ Done", callback_data="wiz_back_to_builder")])
    delete_buttons.append([get_cancel_button("wiz_cancel")])
    
    await callback_query.message.edit_text(
        "**🗑️ Delete Buttons**\n\nTap a button to delete it:",
        reply_markup=InlineKeyboardMarkup(delete_buttons)
    )

@app.on_callback_query(filters.regex(r"^del_btn_\d+$") & filters.user(ADMIN_USER_ID))
async def delete_button_handler(client, callback_query):
    try:
        button_index = int(callback_query.data.split("_")[2])
        data = post_cache.get(callback_query.from_user.id, {})
        buttons_list = data.get('buttons', [])
        
        if 0 <= button_index < len(buttons_list):
            deleted_button = buttons_list.pop(button_index)
            post_cache[callback_query.from_user.id]['buttons'] = buttons_list
            await callback_query.answer(f"✅ Deleted: {deleted_button[0]}", show_alert=True)
            
            # Refresh the delete menu
            await wiz_delete_buttons(client, callback_query)
        else:
            await callback_query.answer("❌ Button not found", show_alert=True)
    except Exception as e:
        await callback_query.answer("❌ Error deleting button", show_alert=True)

@app.on_callback_query(filters.regex("wiz_back_attach_file") & filters.user(ADMIN_USER_ID))
async def wiz_back_attach_file(client, callback_query):
    # Clear temp file data and return to attach file step
    if callback_query.from_user.id in post_cache:
        post_cache[callback_query.from_user.id].pop('temp_file', None)
    
    user_states[callback_query.from_user.id] = "WAITING_FILE_ATTACH"
    buttons = [
        [InlineKeyboardButton("🔙 Back", callback_data="wiz_back_to_builder")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")]
    ]
    await callback_query.message.edit_text(
        "**📎 Attach File for FileShare**\n\n"
        "Forward or upload the file you want users to download.\n"
        "I will auto-generate a secured link and add a 'Download' button to this post.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("wiz_back_to_builder") & filters.user(ADMIN_USER_ID))
async def wiz_back_to_builder(client, callback_query):
    user_states[callback_query.from_user.id] = "BUILDING_POST"
    await callback_query.message.edit_text("🔙 **Returned to Post Builder**")
    await show_post_builder_menu(client, callback_query.from_user.id)

@app.on_callback_query(filters.regex("wiz_cancel") & filters.user(ADMIN_USER_ID))
async def wiz_cancel(client, callback_query):
    post_cache.pop(callback_query.from_user.id, None)
    user_states.pop(callback_query.from_user.id, None)
    await callback_query.message.edit_text("❌ Post creation cancelled.")
    await back_to_main(client, callback_query)

# --------------------------------------------------------------------------
# BROADCAST LOGIC (SELECTIVE)
# --------------------------------------------------------------------------

@app.on_callback_query(filters.regex("wiz_send_menu") & filters.user(ADMIN_USER_ID))
async def wiz_send_menu(client, callback_query):
    try:
        # Fetch post channels
        channels = post_channels_col.find({})
        buttons = []
        async for ch in channels:
            btn_text = f"{ch.get('title', 'Channel')} 📢"
            # Callback format: send_target_CHANNELID
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"send_target_{ch['channel_id']}")])
        
        if not buttons:
            await callback_query.answer("No post channels added! Please add post channels first.", show_alert=True)
            return

        # Add "Send to ALL" option
        buttons.append([InlineKeyboardButton("📢 Post to All Post Channels", callback_data="send_target_ALL")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")])

        await callback_query.message.edit_text(
            "🚀 **Select Destination**\n\nWhere do you want to post this?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error in send menu: {e}")
        await callback_query.answer("Error loading channels.", show_alert=True)

@app.on_callback_query(filters.regex(r"^send_target_") & filters.user(ADMIN_USER_ID))
async def execute_broadcast(client, callback_query):
    target = callback_query.data.split("_")[2]
    user_id = callback_query.from_user.id
    data = post_cache.get(user_id)

    if not data:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    await callback_query.message.edit_text("⏳ Sending...")

    # Determine targets
    target_ids = []
    try:
        if target == "ALL":
            async for ch in post_channels_col.find({}):
                target_ids.append(ch['channel_id'])
        else:
            target_ids.append(int(target))
    except Exception as e:
        logger.error(f"Error fetching targets: {e}")
        await callback_query.message.edit_text("❌ Error fetching targets.")
        return

    # Build Markup with proper button formatting
    markup = format_button_markup(data['buttons'])

    success = 0
    failed = 0

    for chat_id in target_ids:
        try:
            # Send with styled text entities preserved
            if data['type'] == "text":
                await client.send_message(
                    chat_id, 
                    data['text'], 
                    entities=data.get('entities', []),
                    reply_markup=markup
                )
            elif data['type'] == "photo":
                await client.send_photo(
                    chat_id, 
                    data['file_id'], 
                    caption=data['text'],
                    caption_entities=data.get('entities', []),
                    reply_markup=markup
                )
            elif data['type'] == "video":
                await client.send_video(
                    chat_id, 
                    data['file_id'], 
                    caption=data['text'],
                    caption_entities=data.get('entities', []),
                    reply_markup=markup
                )
            elif data['type'] == "document":
                await client.send_document(
                    chat_id, 
                    data['file_id'], 
                    caption=data['text'],
                    caption_entities=data.get('entities', []),
                    reply_markup=markup
                )
            elif data['type'] == "audio":
                await client.send_audio(
                    chat_id, 
                    data['file_id'], 
                    caption=data['text'],
                    caption_entities=data.get('entities', []),
                    reply_markup=markup
                )
            success += 1
            await asyncio.sleep(0.5) # Avoid hitting flood limits
        except FloodWait as e:
            await asyncio.sleep(e.value)
            # Retry once (simple logic)
            try:
                if data['type'] == "text":
                    await client.send_message(
                        chat_id, 
                        data['text'], 
                        entities=data.get('entities', []),
                        reply_markup=markup
                    )
                elif data['type'] == "photo":
                    await client.send_photo(
                        chat_id, 
                        data['file_id'], 
                        caption=data['text'],
                        caption_entities=data.get('entities', []),
                        reply_markup=markup
                    )
                elif data['type'] == "video":
                    await client.send_video(
                        chat_id, 
                        data['file_id'], 
                        caption=data['text'],
                        caption_entities=data.get('entities', []),
                        reply_markup=markup
                    )
                elif data['type'] == "document":
                    await client.send_document(
                        chat_id, 
                        data['file_id'], 
                        caption=data['text'],
                        caption_entities=data.get('entities', []),
                        reply_markup=markup
                    )
                elif data['type'] == "audio":
                    await client.send_audio(
                        chat_id, 
                        data['file_id'], 
                        caption=data['text'],
                        caption_entities=data.get('entities', []),
                        reply_markup=markup
                    )
                success += 1
            except:
                failed += 1
        except Exception as e:
            logger.error(f"Failed to send to {chat_id}: {e}")
            failed += 1

    # Clear Cache
    post_cache.pop(user_id, None)
    user_states.pop(user_id, None)

    result_text = (
        f"✅ **Broadcasting Complete**\n\n"
        f"Successful: {success}\n"
        f"Failed: {failed}"
    )
    await client.send_message(user_id, result_text)
    # Show admin menu again
    await admin_start(client, callback_query.message)

# --------------------------------------------------------------------------
# USER SIDE & FORCE SUB LOGIC
# --------------------------------------------------------------------------

@app.on_message(filters.command("start"))
async def user_start_handler(client, message):
    # If it's a plain /start
    if len(message.command) == 1:
        buttons = [
            [InlineKeyboardButton("ℹ️ About", callback_data="user_about"),
             InlineKeyboardButton("🆘 Help", callback_data="user_help")],
            [InlineKeyboardButton("❌ Close", callback_data="user_close")]
        ]
        await message.reply(
            "**👋 Welcome to FileShare Bot!**\n\n"
            "I can help you store and share files with forced subscription protection.\n"
            "Use the buttons below to learn more.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # If it's /start token
    token = message.command[1]
    
    # 1. Fetch File Info
    try:
        file_data = await fileshares_col.find_one({"token": token})
        if not file_data:
            await message.reply("❌ **Invalid or expired link.**")
            return
    except Exception as e:
        logger.error(f"DB Error fetching file: {e}")
        await message.reply("❌ System error. Please try again later.")
        return

    # 2. Check Forced Subscription (FSub)
    try:
        channels = must_join_channels_col.find({})
        not_joined_channels = []

        async for ch in channels:
            is_member = await is_user_member(message.from_user.id, ch['channel_id'])
            if not is_member:
                # If username exists use that, else try to make a link
                invite_link = f"https://t.me/{ch['username']}" if ch.get('username') else None
                # If we don't have username, we can't link easily without export_invite_link which requires rights
                # For this simple bot, we assume admin added public channels or we have usernames
                if invite_link:
                    not_joined_channels.append((ch['title'], invite_link))

        if not_joined_channels:
            # Build Join Buttons
            buttons = []
            for title, link in not_joined_channels:
                buttons.append([InlineKeyboardButton(f"Join {title}", url=link)])
            
            # Add "Try Again" button which re-triggers the same start command
            # Deep linking format: https://t.me/bot?start=token
            bot_username = (await client.get_me()).username
            url_retry = f"https://t.me/{bot_username}?start={token}"
            buttons.append([InlineKeyboardButton("✅ I Joined", url=url_retry)])

            await message.reply(
                "⚠️ **You must join our channels to access this file:**",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return
    except Exception as e:
        logger.error(f"Error in FSub check: {e}")
        # Fail open or closed? Let's fail closed for security but notify user
        await message.reply("❌ Error verifying subscription status.")
        return

    # 3. Send File (If subscribed)
    try:
        caption = file_data.get("caption", "")
        f_type = file_data.get("file_type")
        f_id = file_data.get("file_id")

        if f_type == "document":
            await client.send_document(message.chat.id, f_id, caption=caption, protect_content=True)
        elif f_type == "video":
            await client.send_video(message.chat.id, f_id, caption=caption, protect_content=True)
        elif f_type == "photo":
            await client.send_photo(message.chat.id, f_id, caption=caption, protect_content=True)
        elif f_type == "audio":
            await client.send_audio(message.chat.id, f_id, caption=caption, protect_content=True)
        else:
            await message.reply("❌ Unknown file type.")

    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await message.reply("❌ Error sending file. It might have been deleted from Telegram servers.")

# USER CALLBACKS
@app.on_callback_query(filters.regex("user_about"))
async def user_about(client, callback_query):
    await callback_query.answer("Made with ❤️ by Antigravity", show_alert=True)

@app.on_callback_query(filters.regex("user_help"))
async def user_help(client, callback_query):
    await callback_query.answer("Contact the admin for support.", show_alert=True)

@app.on_callback_query(filters.regex("user_close"))
async def user_close(client, callback_query):
    await callback_query.message.delete()

# --------------------------------------------------------------------------
# MAIN EXECUTION
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# MAIN EXECUTION
# --------------------------------------------------------------------------

# We need to make the web server runner and site accessible to gracefully stop it.
# We'll modify start_web_server to return the runner and site objects.

# Update the definition of start_web_server
async def start_web_server():
    """Starts the aiohttp web server and returns runner and site."""
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    # Bind to 0.0.0.0 so outside world can access (required by Render)
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")
    return runner, site # Return the runner and site

# Add a function to handle graceful shutdown
async def shutdown(loop, runner, site):
    """Gracefully shuts down the bot, web server, and cancels tasks."""
    logger.info("Shutdown initiated...")
    
    # 1. Stop Telegram Bot (Pyrogram)
    if app.is_running:
        logger.info("Stopping Pyrogram Client...")
        await app.stop()
        
    # 2. Stop Aiohttp Web Server
    if site and runner:
        logger.info("Stopping aiohttp Web Server...")
        await site.stop()
        await runner.cleanup()
        
    # 3. Cancel outstanding tasks (excluding the main loop task)
    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task(loop=loop)]
    for task in tasks:
        task.cancel()
    
    logger.info("Shutdown complete.")


async def main():
    # Start Web Server (Keep Alive) and get runner/site objects
    runner, site = await start_web_server()
    
    # Start Bot
    logger.info("Starting Bot...")
    await app.start()
    
    # Keep running until cancelled (e.g., SIGTERM on Render)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        # This is expected when an external signal triggers shutdown
        pass
    except KeyboardInterrupt:
        # For local testing
        pass
    finally:
        # Execute the shutdown procedure
        await shutdown(asyncio.get_event_loop(), runner, site)

if __name__ == "__main__":
    # Run the async main loop
    loop = asyncio.get_event_loop()
    
    # Add a signal handler for graceful termination (e.g. SIGINT/SIGTERM)
    # This is especially crucial for deployment on platforms like Render.
    try:
        # SIGINT is typically Ctrl+C; SIGTERM is used by docker/orchestrators
        loop.add_signal_handler(
            signal.SIGINT, 
            lambda: loop.stop()
        )
        loop.add_signal_handler(
            signal.SIGTERM, 
            lambda: loop.stop()
        )
    except NotImplementedError:
        # Windows doesn't support signal handlers in this way
        logger.warning("Signal handlers not supported on this platform.")

    #import signal # Import the signal module
    
    try:
        # Start the main coroutine
        loop.run_until_complete(main())
    finally:
        # After the main coroutine finishes (due to cancellation/stop),
        # close the loop and clean up.
        logger.info("Closing loop...")
        loop.close()

# Note: You will need to add `import signal` at the top of your file