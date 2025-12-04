import os
import logging
import asyncio
from datetime import datetime, timedelta
from pyrogram import Client, filters, errors, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import shutil

# Logging setup
logging.basicConfig(level=logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = "7318148378:AAFqQbfhIFeOwVTv6a6jmwSowH7dmMbI7IU"
API_ID = "27352735"
API_HASH = "8c4512c1052a60e05b05522a2ea12e5e"
DB_URI = "mongodb+srv://50duddubot518:50duddubot518@cluster0.momby1w.mongodb.net/?appName=Cluster0"
OWNER_USERNAME = "NeonGhost"
OWNER_ID = None
WELCOME_IMAGE = "https://te.legra.ph/file/c5b07f2679e49c58bfb1b.jpg"

# Create directories
os.makedirs("sessions", exist_ok=True)
os.makedirs("data", exist_ok=True)

# MongoDB Setup with connection pooling
try:
    mongo_client = AsyncIOMotorClient(
        DB_URI,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        maxPoolSize=50,
        minPoolSize=10,
        tls=True,
        tlsAllowInvalidCertificates=True
    )
    db = mongo_client['auto_approve_bot']
    users_col = db['users']
    channels_col = db['channels']
    sessions_col = db['sessions']
    stats_col = db['stats']
    approved_users_col = db['approved_users']  # NEW: Track approved users for broadcast
except Exception as e:
    logger.error(f"MongoDB setup error: {e}")

# Scheduler & Bot
scheduler = AsyncIOScheduler()
bot = Client("auto_approve_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH, workdir="data")

# Storage
user_clients = {}
user_states = {}
active_tasks = {}
broadcast_states = {}  # NEW: For broadcast handling
# ==================== HELPER FUNCTIONS ====================
# ==================== STATS HELPER FUNCTIONS ====================

async def initialize_channel_stats(user_id, chat_id):
    """Initialize stats for a channel if not exists"""
    try:
        ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        if ch and "dm_stats" not in ch:
            await channels_col.update_one(
                {"user_id": user_id, "chat_id": chat_id},
                {"$set": {
                    "dm_stats": {
                        "total_dm_sent": 0,
                        "today_dm_sent": 0,
                        "total_dm_failed": 0,
                        "last_reset_date": datetime.now(),
                        "success_rate": 0.0
                    }
                }}
            )
    except Exception as e:
        logger.error(f"Error initializing stats: {e}")

async def update_dm_stats(user_id, chat_id, success=True):
    """Update DM statistics"""
    try:
        ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        if not ch:
            return
        
        # Initialize if not exists
        if "dm_stats" not in ch:
            await initialize_channel_stats(user_id, chat_id)
            ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        
        stats = ch.get("dm_stats", {})
        
        # Check if we need to reset today's count
        last_reset = stats.get("last_reset_date", datetime.now())
        if (datetime.now() - last_reset).days >= 1:
            stats["today_dm_sent"] = 0
            stats["last_reset_date"] = datetime.now()
        
        # Update stats
        if success:
            stats["total_dm_sent"] = stats.get("total_dm_sent", 0) + 1
            stats["today_dm_sent"] = stats.get("today_dm_sent", 0) + 1
        else:
            stats["total_dm_failed"] = stats.get("total_dm_failed", 0) + 1
        
        # Calculate success rate
        total_attempts = stats.get("total_dm_sent", 0) + stats.get("total_dm_failed", 0)
        if total_attempts > 0:
            stats["success_rate"] = round((stats.get("total_dm_sent", 0) / total_attempts) * 100, 1)
        
        # Save to database
        await channels_col.update_one(
            {"user_id": user_id, "chat_id": chat_id},
            {"$set": {"dm_stats": stats}}
        )
        
    except Exception as e:
        logger.error(f"Error updating stats: {e}")

async def get_channel_stats(user_id, chat_id):
    """Get formatted channel statistics"""
    try:
        ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        if not ch or "dm_stats" not in ch:
            return None
        
        stats = ch["dm_stats"]
        last_reset = stats.get("last_reset_date", datetime.now())
        hours_ago = int((datetime.now() - last_reset).seconds / 3600)
        
        return {
            "total": stats.get("total_dm_sent", 0),
            "today": stats.get("today_dm_sent", 0),
            "failed": stats.get("total_dm_failed", 0),
            "rate": stats.get("success_rate", 0.0),
            "reset_hours": hours_ago
        }
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return None
async def is_owner(user_id):
    global OWNER_ID
    if OWNER_ID is None:
        try:
            owner = await bot.get_users(OWNER_USERNAME)
            OWNER_ID = owner.id
        except:
            pass
    return user_id == OWNER_ID

async def get_user(user_id):
    try:
        return await users_col.find_one({"user_id": user_id})
    except:
        return None

async def create_user(user_id, username):
    try:
        user_data = {
            "user_id": user_id,
            "username": username,
            "is_premium": False,
            "premium_expires": None,
            "daily_requests": 0,
            "total_requests": 0,
            "last_reset": datetime.now(),
            "created_at": datetime.now()
        }
        await users_col.insert_one(user_data)
        return user_data
    except:
        return None

async def get_user_channels(user_id):
    try:
        return await channels_col.find({"user_id": user_id}).to_list(None)
    except:
        return []

async def can_add_channel(user_id):
    try:
        user = await get_user(user_id)
        if user and user["is_premium"]:
            return True, None
        count = await channels_col.count_documents({"user_id": user_id})
        if count >= 3:
            return False, "❌ Free users can add max 3 channels. Upgrade to Premium!"
        return True, None
    except:
        return False, "❌ Error checking channel limit"

async def check_request_limit(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return False, 0
        if (datetime.now() - user["last_reset"]).days >= 1:
            await users_col.update_one({"user_id": user_id}, {"$set": {"daily_requests": 0, "last_reset": datetime.now()}})
            user["daily_requests"] = 0
        if user["is_premium"]:
            return True, user["daily_requests"]
        if user["daily_requests"] >= 1000:
            return False, user["daily_requests"]
        return True, user["daily_requests"]
    except:
        return False, 0

# NEW: Save approved user data
async def save_approved_user(user_id, chat_id, approved_user_id, username, first_name):
    try:
        await approved_users_col.update_one(
            {"user_id": user_id, "chat_id": chat_id, "approved_user_id": approved_user_id},
            {"$set": {
                "user_id": user_id,
                "chat_id": chat_id,
                "approved_user_id": approved_user_id,
                "username": username,
                "first_name": first_name,
                "approved_at": datetime.now()
            }},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving approved user: {e}")

# NEW: Get all approved users for broadcast
async def get_approved_users(user_id, chat_id=None):
    try:
        query = {"user_id": user_id}
        if chat_id:
            query["chat_id"] = chat_id
        return await approved_users_col.find(query).to_list(None)
    except:
        return []

async def check_premium_expiry():
    try:
        expired_users = await users_col.find({"is_premium": True, "premium_expires": {"$lt": datetime.now(), "$ne": None}}).to_list(None)
        for user in expired_users:
            await users_col.update_one({"user_id": user["user_id"]}, {"$set": {"is_premium": False, "premium_expires": None}})
            try:
                await bot.send_message(user["user_id"], "❌ **Premium Expired!**\n\nContact @NeonGhost to renew!")
            except:
                pass
    except:
        pass

async def send_premium_reminders():
    try:
        reminder_date = datetime.now() + timedelta(days=3)
        users = await users_col.find({"is_premium": True, "premium_expires": {"$gte": datetime.now(), "$lte": reminder_date, "$ne": None}}).to_list(None)
        for user in users:
            days_left = (user["premium_expires"] - datetime.now()).days
            try:
                await bot.send_message(user["user_id"], f"⚠️ Premium expires in {days_left} days!\nContact @NeonGhost")
            except:
                pass
    except:
        pass

async def reset_daily_limits():
    try:
        await users_col.update_many({}, {"$set": {"daily_requests": 0, "last_reset": datetime.now()}})
    except:
        pass

# NEW: Session management functions

async def save_session(user_id, session_string, api_id, api_hash, phone):
    """Save session to database"""
    try:
        await sessions_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "session_string": session_string,
                "api_id": api_id,
                "api_hash": api_hash,
                "phone": phone,
                "connected_at": datetime.now(),
                "updated_at": datetime.now()  # FIX: Added this
            }},
            upsert=True
        )
        logger.info(f"Session saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving session: {e}")
        return False

async def load_session(user_id):
    """Load session from database"""
    try:
        session_data = await sessions_col.find_one({"user_id": user_id})
        return session_data
    except Exception as e:
        logger.error(f"Error loading session: {e}")
        return None

async def delete_user_session(user_id):
    """Delete user session from database and files"""
    try:
        # Delete from database
        await sessions_col.delete_one({"user_id": user_id})
        
        # Disconnect client if active
        if user_id in user_clients:
            try:
                await user_clients[user_id].stop()
            except:
                pass
            del user_clients[user_id]
        
        # Delete session files
        session_file = f"sessions/user_{user_id}.session"
        if os.path.exists(session_file):
            os.remove(session_file)
        
        logger.info(f"Session deleted for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting session: {e}")
        return False

async def initialize_user_client(user_id):
    """Initialize user client from saved session"""
    try:
        if user_id in user_clients:
            # Check if client is still connected
            try:
                await user_clients[user_id].get_me()
                return user_clients[user_id]
            except:
                # Client disconnected, remove it
                del user_clients[user_id]
        
        session_data = await load_session(user_id)
        if not session_data:
            return None
        
        # Create client with session string
        user_client = Client(
            f"user_{user_id}",
            api_id=session_data["api_id"],
            api_hash=session_data["api_hash"],
            session_string=session_data["session_string"],
            in_memory=True
        )
        
        await user_client.start()
        user_clients[user_id] = user_client
        logger.info(f"Client initialized for user {user_id}")
        return user_client
        
    except Exception as e:
        logger.error(f"Error initializing client for {user_id}: {e}")
        return None
# ==================== START COMMAND ====================

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    user = await get_user(user_id)
    if not user:
        await create_user(user_id, username)
        user = await get_user(user_id)
    
    is_owner_user = await is_owner(user_id)
    
    welcome_text = (
        f"👋 **Welcome {message.from_user.first_name}!**\n\n"
        f"🤖 **Auto-Approve Bot**\n"
        f"Automatically approve join requests!\n\n"
        f"✨ **Features:**\n"
        f"├ 🚀 Auto-approve (Pending + Live)\n"
        f"├ 📊 Real-time stats\n"
        f"├ 💬 Custom welcomes\n"
        f"├ 📈 Multi-channel\n"
        f"└ 📢 Broadcast to approved users\n\n"
    )
    
    if is_owner_user:
        welcome_text += "👑 **You are the Owner!**"
    elif user:
        welcome_text += "💎 **Plan:** " + ("Premium ✨" if user["is_premium"] else "Free 🆓")
    
    keyboard = [
        [InlineKeyboardButton("📱 Connect Account", callback_data="connect_account")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
        [InlineKeyboardButton("📚 My Channels", callback_data="my_channels")],
        [InlineKeyboardButton("🔄 Session Manager", callback_data="session_manager")],
    ]
    
    if is_owner_user:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    
    keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help")])
    
    try:
        await message.reply_photo(photo=WELCOME_IMAGE, caption=welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        await message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))

# ==================== DASHBOARD ====================

@bot.on_callback_query(filters.regex("^dashboard$"))
async def dashboard_callback(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback_query.answer("❌ /start again", show_alert=True)
        return
    
    channels_count = await channels_col.count_documents({"user_id": user_id})
    approved_count = await approved_users_col.count_documents({"user_id": user_id})
    premium_status = "✅ Active" if user["is_premium"] else "❌ Inactive"
    
    if user["is_premium"] and user.get("premium_expires"):
        days_left = (user["premium_expires"] - datetime.now()).days
        premium_status += f"\n📅 {days_left} days left"
    
    text = (
        f"📊 **Dashboard**\n\n"
        f"👤 {callback_query.from_user.first_name}\n"
        f"🆔 `{user_id}`\n"
        f"💎 Premium: {premium_status}\n\n"
        f"📈 **Stats:**\n"
        f"├ 📁 Channels: {channels_count}\n"
        f"├ ✅ Approved Users: {approved_count}\n"
        f"├ 📝 Today: {user['daily_requests']}/{'∞' if user['is_premium'] else '1000'}\n"
        f"├ 📊 Total: {user['total_requests']}\n"
        f"└ 📅 Since: {user['created_at'].strftime('%d %b %Y')}\n\n"
    )
    
    if not user["is_premium"]:
        text += "⚠️ **Free Limits:**\n├ 🐢 1 req/sec\n├ 📊 1000/day\n└ 📁 3 channels\n\n💎 Upgrade Premium!"
    
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="dashboard")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()
# ==================== SESSION MANAGER ====================

@bot.on_callback_query(filters.regex("^session_manager$"))
async def session_manager_callback(client, callback_query):
    user_id = callback_query.from_user.id
    session_data = await load_session(user_id)
    
    if not session_data:
        text = "🔌 **Session Manager**\n\n❌ No active session\n\nConnect your account first!"
        keyboard = [
            [InlineKeyboardButton("📱 Connect Account", callback_data="connect_account")],
            [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
        ]
    else:
        # FIX: Check if updated_at exists
        updated_date = session_data.get('updated_at') or session_data.get('connected_at') or datetime.now()
        
        text = (
            f"🔌 **Session Manager**\n\n"
            f"✅ **Connected**\n"
            f"📱 Phone: `{session_data['phone']}`\n"
            f"🆔 API ID: `{session_data['api_id']}`\n"
            f"📅 Updated: {updated_date.strftime('%d %b %Y')}\n\n"
            f"⚠️ Delete session will stop all auto-approve tasks!"
        )
        keyboard = [
            [InlineKeyboardButton("🔄 Reconnect", callback_data="connect_account")],
            [InlineKeyboardButton("🗑️ Delete Session", callback_data="delete_session_confirm")],
            [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
        ]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^delete_session_confirm$"))
async def delete_session_confirm_callback(client, callback_query):
    text = "⚠️ **Confirm Delete?**\n\nThis will:\n├ Stop all auto-approve tasks\n├ Remove session data\n└ Disconnect your account\n\n**Are you sure?**"
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data="delete_session_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="session_manager")]
    ]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^delete_session_yes$"))
async def delete_session_yes_callback(client, callback_query):
    user_id = callback_query.from_user.id
    
    # Stop all active tasks
    tasks_to_remove = [key for key in active_tasks.keys() if key.startswith(f"{user_id}_")]
    for task_key in tasks_to_remove:
        active_tasks[task_key].cancel()
        del active_tasks[task_key]
    
    # Delete session
    success = await delete_user_session(user_id)
    
    if success:
        text = "✅ **Session Deleted!**\n\nAll tasks stopped.\nYou can reconnect anytime."
    else:
        text = "❌ **Error!**\n\nFailed to delete session."
    
    keyboard = [
        [InlineKeyboardButton("📱 Reconnect", callback_data="connect_account")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

# ==================== CONNECT ACCOUNT ====================

@bot.on_callback_query(filters.regex("^connect_account$"))
async def connect_account_callback(client, callback_query):
    text = (
        "📱 **Connect Your Account**\n\n"
        "⚠️ **Important:**\n"
        "├ Credentials stored securely\n"
        "├ Needed for auto-approve\n"
        "└ Session saved for reuse\n\n"
        "📝 **Steps:**\n"
        "1. Get API from https://my.telegram.org\n"
        "2. Send: `/connect API_ID API_HASH PHONE`\n\n"
        "**Example:**\n"
        "`/connect 12345678 abcd1234 +919876543210`\n\n"
        "⚠️ Include country code!"
    )
    keyboard = [
        [InlineKeyboardButton("🔗 Get API", url="https://my.telegram.org")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_message(filters.command("connect") & filters.private)
async def connect_command(client, message: Message):
    user_id = message.from_user.id
    
    try:
        parts = message.text.split()
        if len(parts) != 4:
            await message.reply_text(
                "❌ **Invalid!**\n\n"
                "Usage: `/connect API_ID API_HASH PHONE`\n"
                "Example: `/connect 12345678 abcd1234 +919876543210`"
            )
            return
        
        api_id, api_hash, phone = parts[1], parts[2], parts[3]
        
        if not phone.startswith("+"):
            await message.reply_text("❌ Phone must start with +\nExample: +919876543210")
            return
        
        await message.delete()
        status_msg = await message.reply_text("⏳ Connecting...")
        
        # Use in-memory session
        user_client = Client(
            f"user_{user_id}",
            api_id=int(api_id),
            api_hash=api_hash,
            phone_number=phone,
            in_memory=True
        )
        
        await user_client.connect()
        
        try:
            sent_code = await user_client.send_code(phone)
            await status_msg.edit_text("📱 **Code Sent!**\n\nCheck Telegram.\nSend: `/code 12345`\n\n⚠️ 3 minutes")
            
            user_states[user_id] = {
                "state": "waiting_code",
                "client": user_client,
                "phone": phone,
                "phone_code_hash": sent_code.phone_code_hash,
                "api_id": int(api_id),
                "api_hash": api_hash
            }
        except errors.PhoneNumberInvalid:
            await status_msg.edit_text("❌ Invalid phone! Check format")
            await user_client.disconnect()
        except errors.PhoneNumberBanned:
            await status_msg.edit_text("❌ Phone banned!")
            await user_client.disconnect()
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
            await user_client.disconnect()
    except ValueError:
        await message.reply_text("❌ Invalid API_ID!")
    except Exception as e:
        await message.reply_text(f"❌ Failed: {str(e)}")

@bot.on_message(filters.command("code") & filters.private)
async def code_command(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_states or not isinstance(user_states[user_id], dict) or user_states[user_id].get("state") != "waiting_code":
        await message.reply_text("❌ No pending verification! Use /connect")
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply_text("❌ Invalid! Send: `/code 12345`")
            return
        
        code = parts[1].replace("-", "").strip()
        await message.delete()
        status_msg = await message.reply_text("⏳ Verifying...")
        
        user_data = user_states[user_id]
        user_client = user_data["client"]
        
        try:
            await user_client.sign_in(user_data["phone"], user_data["phone_code_hash"], code)
            me = await user_client.get_me()
            
            # Export and save session string
            session_string = await user_client.export_session_string()
            await save_session(user_id, session_string, user_data["api_id"], user_data["api_hash"], user_data["phone"])
            
            # Save to database
            await sessions_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "api_id": user_data["api_id"],
                    "api_hash": user_data["api_hash"],
                    "phone": user_data["phone"],
                    "session_string": session_string,
                    "connected_at": datetime.now(),
                    "account_name": me.first_name,
                    "account_username": me.username
                }},
                upsert=True
            )
            
            user_clients[user_id] = user_client
            del user_states[user_id]
            
            await status_msg.edit_text(
                f"✅ **Connected!**\n\n👤 {me.first_name}\n🆔 {me.id}\n📱 {me.phone_number}\n\n💾 Session saved!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 Add Channel", callback_data="add_channel_guide")]])
            )
        except errors.SessionPasswordNeeded:
            user_states[user_id]["state"] = "waiting_2fa"
            await status_msg.edit_text("🔐 **2FA Required**\n\nSend: `/password YOUR_PASSWORD`")
        except errors.PhoneCodeInvalid:
            await status_msg.edit_text("❌ Invalid code! Try: `/code 12345`")
        except errors.PhoneCodeExpired:
            await status_msg.edit_text("❌ Code expired! Use /connect again")
            await user_states[user_id]["client"].disconnect()
            del user_states[user_id]
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
            if user_id in user_states:
                await user_states[user_id]["client"].disconnect()
                del user_states[user_id]
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")

@bot.on_message(filters.command("password") & filters.private)
async def password_command(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_states or not isinstance(user_states[user_id], dict) or user_states[user_id].get("state") != "waiting_2fa":
        await message.reply_text("❌ No pending 2FA!")
        return
    
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) != 2:
            await message.reply_text("❌ Invalid! Send: `/password YOUR_PASSWORD`")
            return
        
        password = parts[1]
        await message.delete()
        status_msg = await message.reply_text("⏳ Verifying...")
        
        user_data = user_states[user_id]
        user_client = user_data["client"]
        
        try:
            await user_client.check_password(password)
            me = await user_client.get_me()
            
            # Export and save session string
            session_string = await user_client.export_session_string()
            await save_session(user_id, session_string, user_data["api_id"], user_data["api_hash"], user_data["phone"])
            
            await sessions_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "api_id": user_data["api_id"],
                    "api_hash": user_data["api_hash"],
                    "phone": user_data["phone"],
                    "session_string": session_string,
                    "connected_at": datetime.now(),
                    "account_name": me.first_name,
                    "account_username": me.username
                }},
                upsert=True
            )
            
            user_clients[user_id] = user_client
            del user_states[user_id]
            
            await status_msg.edit_text(
                f"✅ **Connected!**\n\n👤 {me.first_name}\n🆔 {me.id}\n📱 {me.phone_number}\n\n💾 Session saved!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 Add Channel", callback_data="add_channel_guide")]])
            )
        except errors.PasswordHashInvalid:
            await status_msg.edit_text("❌ Invalid password! Try: `/password YOUR_PASSWORD`")
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
            if user_id in user_states:
                await user_data["client"].disconnect()
                del user_states[user_id]
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")
# ==================== ADD CHANNEL ====================

@bot.on_callback_query(filters.regex("^add_channel_guide$"))
async def add_channel_guide_callback(client, callback_query):
    user_id = callback_query.from_user.id
    session = await load_session(user_id)
    
    if not session:
        await callback_query.answer("❌ Connect account first!", show_alert=True)
        await callback_query.message.edit_text(
            "⚠️ **Not Connected**\n\nConnect your account first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Connect", callback_data="connect_account")]])
        )
        return
    
    can_add, error = await can_add_channel(user_id)
    if not can_add:
        await callback_query.answer(error, show_alert=True)
        return
    
    user_states[user_id] = "waiting_channel"
    text = "📢 **Add Channel**\n\nSend username or link:\n@channel or https://t.me/channel\n\n⚠️ Must be admin!"
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="my_channels")]]))
    await callback_query.answer()


async def add_channel_process(client, message: Message):
    user_id = message.from_user.id
    channel_input = message.text.strip()
    status_msg = await message.reply_text("⏳ Adding...")

    try:
        # Initialize client session
        user_client = await initialize_user_client(user_id)
        if not user_client:
            await status_msg.edit_text("❌ Session lost! Reconnect.")
            if user_id in user_states:
                del user_states[user_id]
            return

        chat = None

        # Clean input
        original_input = channel_input
        if "t.me/" in channel_input:
            channel_input = channel_input.split("t.me/")[-1].strip()
        if channel_input.startswith("@"):
            channel_input = channel_input[1:].strip()

        # Step 1: Sync dialogs
        await status_msg.edit_text("⏳ Syncing channels...")
        try:
            async for dialog in user_client.get_dialogs(limit=100):
                pass
            await asyncio.sleep(0.5)
        except:
            pass

        # Step 2: Access channel
        await status_msg.edit_text("⏳ Accessing channel...")

        # Method 1 – numeric ID
        if channel_input.lstrip('-').isdigit():
            chat_id = int(channel_input)

            # Check dialogs
            try:
                async for dialog in user_client.get_dialogs(limit=200):
                    if dialog.chat.id == chat_id:
                        chat = dialog.chat
                        logger.info(f"Found chat in dialogs: {chat.title}")
                        break
            except Exception as e:
                logger.error(f"Error iterating dialogs: {e}")

            # Direct get
            if not chat:
                try:
                    chat = await user_client.get_chat(chat_id)
                    logger.info(f"Got chat directly: {chat.title}")
                except Exception as e:
                    logger.error(f"Cannot get chat by ID: {e}")
                    await status_msg.edit_text(
                        "❌ **Cannot access this channel!**\n\n"
                        "Please provide username or link instead."
                    )
                    if user_id in user_states:
                        del user_states[user_id]
                    return

        # Method 2 – username / link
        else:
            tried = []

            # Attempt 1
            try:
                chat = await user_client.get_chat(channel_input)
            except Exception as e1:
                tried.append(f"Username: {str(e1)[:50]}")

                # Attempt 2
                try:
                    chat = await user_client.get_chat(f"@{channel_input}")
                except Exception as e2:
                    tried.append(f"@Username: {str(e2)[:50]}")

                    # Attempt 3 – Join
                    try:
                        chat = await user_client.join_chat(original_input)
                        await asyncio.sleep(1)
                    except Exception as e3:
                        tried.append(f"Join: {str(e3)[:50]}")

                        # Attempt 4 – Search in dialogs
                        try:
                            async for dialog in user_client.get_dialogs(limit=300):
                                if dialog.chat.username and dialog.chat.username.lower() == channel_input.lower():
                                    chat = dialog.chat
                                    break
                        except Exception as e4:
                            tried.append(f"Search: {str(e4)[:50]}")

            if not chat:
                error_msg = "❌ **Cannot access channel!**\n\n**Tried:**\n"
                for idx, t in enumerate(tried[:3], 1):
                    error_msg += f"{idx}. {t}\n"

                error_msg += (
                    "\n**Solutions:**\n"
                    "1. Join the channel\n"
                    "2. Use public username `@channel`\n"
                    "3. Use invite link `t.me/+xxxx`\n"
                )

                await status_msg.edit_text(error_msg)
                if user_id in user_states:
                    del user_states[user_id]
                return

        if not chat:
            await status_msg.edit_text("❌ Could not access the channel!")
            if user_id in user_states:
                del user_states[user_id]
            return

        # Step 3: verify type
        if chat.type not in [enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP]:
            await status_msg.edit_text(
                "❌ **Not a channel/group!**\nChannels & supergroups only."
            )
            if user_id in user_states:
                del user_states[user_id]
            return

        # Step 4: Verify admin
        await status_msg.edit_text("⏳ Checking permissions...")
        try:
            member = await user_client.get_chat_member(chat.id, "me")

            if member.status not in [enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR]:
                await status_msg.edit_text(
                    "❌ **You are not an admin in this channel!**"
                )
                if user_id in user_states:
                    del user_states[user_id]
                return

        except Exception as e:
            logger.error(f"Admin verify error: {e}")
            await status_msg.edit_text("⚠️ Cannot verify admin. Continuing...")

        # Step 5: Get invite link
        await status_msg.edit_text("⏳ Getting invite link...")
        try:
            invite_link = await user_client.export_chat_invite_link(chat.id)
        except Exception as e:
            logger.error(f"Cannot export link: {e}")
            invite_link = f"https://t.me/{chat.username}" if chat.username else "N/A"

        # Step 6: Save to DB
        await status_msg.edit_text("⏳ Saving...")
        await channels_col.update_one(
            {"user_id": user_id, "chat_id": chat.id},
            {"$set": {
                "user_id": user_id,
                "chat_id": chat.id,
                "title": chat.title,
                "username": chat.username,
                "invite_link": invite_link,
                "is_active": False,
                "auto_approve_enabled": False,
                "welcome_message": None,
                "total_approved": 0,
                "total_declined": 0,
                "added_at": datetime.now()
            }},
            upsert=True
        )

        # 👉 Your requested line (correct position)
        await channels_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"forward_type": "normal"}}
        )

        # Cleanup state
        if user_id in user_states:
            del user_states[user_id]

        # Keyboard
        keyboard = [
            [InlineKeyboardButton("▶️ Start Auto-Approve", callback_data=f"start_approve_{chat.id}")],
            [InlineKeyboardButton("⚙️ Settings", callback_data=f"channel_info_{chat.id}")],
            [InlineKeyboardButton("📁 My Channels", callback_data="my_channels")]
        ]

        # Done
        await status_msg.edit_text(
            f"✅ **Channel Added Successfully!**\n\n"
            f"📢 **{chat.title}**\n"
            f"🆔 `{chat.id}`\n"
            f"🔗 {invite_link}\n\n"
            f"Now you can start auto-approve! 🚀",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        logger.info(f"Channel added: {chat.id} by user {user_id}")

    except Exception as e:
        logger.error(f"Error in add_channel_process: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ **Failed to add channel!**\n\nError: `{str(e)}`"
        )
        if user_id in user_states:
            del user_states[user_id]


# ==================== MY CHANNELS ====================

@bot.on_callback_query(filters.regex("^my_channels$"))
async def my_channels_callback(client, callback_query):
    user_id = callback_query.from_user.id
    channels = await get_user_channels(user_id)
    
    if not channels:
        text = "📁 **No Channels**\n\nAdd your first channel!"
        keyboard = [
            [InlineKeyboardButton("➕ Add", callback_data="add_channel_guide")],
            [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
        ]
    else:
        text = f"📁 **Your Channels ({len(channels)})**\n\n"
        keyboard = []
        for idx, ch in enumerate(channels, 1):
            status = "🟢" if ch.get("is_active") else "🔴"
            text += f"{idx}. {status} {ch['title']}\n"
            keyboard.append([InlineKeyboardButton(f"📢 {ch['title'][:20]}", callback_data=f"channel_info_{ch['chat_id']}")])
        keyboard.append([InlineKeyboardButton("➕ Add", callback_data="add_channel_guide")])
        keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="main_menu")])
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()

# ==================== CHANNEL INFO ====================

@bot.on_callback_query(filters.regex("^channel_info_"))
async def channel_info_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
    
    if not ch:
        await callback_query.answer("❌ Not found!", show_alert=True)
        return
    
    status = "🟢 Active" if ch.get("is_active") else "🔴 Inactive"
    approved_count = await approved_users_col.count_documents({"user_id": user_id, "chat_id": chat_id})
    
    # Simple formatted text
    text = (
        f"╔══════════════════════════════╗\n"
        f"║   📢 {ch['title'][:22]}\n"
        f"╠══════════════════════════════╣\n"
        f"║                              ║\n"
        f"║  🆔 ID: `{chat_id}`\n"
        f"║  📊 Status: {status}\n"
        f"║                              ║\n"
        f"╠══════════════════════════════╣\n"
        f"║  📈 Approval Stats           ║\n"
        f"║  ━━━━━━━━━━━━━━━━━━━━━━━    ║\n"
        f"║  ✅ Total Approved: {ch.get('total_approved', 0):<8}║\n"
        f"║  👥 Saved Users: {approved_count:<11}║\n"
        f"║                              ║\n"
        f"╚══════════════════════════════╝\n\n"
        f"🔗 {ch['invite_link']}\n\n"
        f"⚠️ **Safe Mode:** No welcome messages\n"
        f"(Prevents account freeze)"
    )
    
    keyboard = [
        [InlineKeyboardButton("▶️ Start", callback_data=f"start_approve_{chat_id}"), InlineKeyboardButton("⏸️ Stop", callback_data=f"stop_approve_{chat_id}")],
        [InlineKeyboardButton("📢 Broadcast", callback_data=f"broadcast_channel_{chat_id}"), InlineKeyboardButton("🗑️ Remove", callback_data=f"remove_channel_{chat_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="my_channels")]
    ]
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()
# ==================== AUTO-APPROVE SYSTEM ====================

@bot.on_callback_query(filters.regex("^start_approve_"))
async def start_approve_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    can_approve, _ = await check_request_limit(user_id)
    if not can_approve:
        await callback_query.answer("❌ Limit reached! Upgrade Premium", show_alert=True)
        return
    
    ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
    if not ch:
        await callback_query.answer("❌ Not found!", show_alert=True)
        return
    
    await channels_col.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"is_active": True, "auto_approve_enabled": True}}
    )
    
    task_key = f"{user_id}_{chat_id}"
    if task_key not in active_tasks:
        task = asyncio.create_task(auto_approve_task(user_id, chat_id))
        active_tasks[task_key] = task
    
    await callback_query.message.edit_text(
        f"✅ **Started!**\n\n📢 {ch['title']}\n🔄 Auto-approving pending + live requests...",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏸️ Stop", callback_data=f"stop_approve_{chat_id}"), InlineKeyboardButton("🔙 Back", callback_data=f"channel_info_{chat_id}")]])
    )
    await callback_query.answer("✅ Started!")

@bot.on_callback_query(filters.regex("^stop_approve_"))
async def stop_approve_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    task_key = f"{user_id}_{chat_id}"
    if task_key in active_tasks:
        active_tasks[task_key].cancel()
        del active_tasks[task_key]
    
    await channels_col.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"is_active": False, "auto_approve_enabled": False}}
    )
    
    await callback_query.message.edit_text(
        "⏸️ **Stopped**\n\nAuto-approve disabled.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start", callback_data=f"start_approve_{chat_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"channel_info_{chat_id}")]
        ])
    )
    await callback_query.answer("⏸️ Stopped!")

async def auto_approve_task(user_id, chat_id):
    """Main auto-approve task - NO WELCOME MESSAGES (Safe from spam)"""
    try:
        user = await get_user(user_id)
        user_client = await initialize_user_client(user_id)
        
        if not user_client:
            logger.error(f"Failed to initialize client for user {user_id}")
            return
        
        ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        if not ch:
            return
        
        # Ensure chat is in cache
        try:
            chat = await user_client.get_chat(chat_id)
            logger.info(f"Loaded chat into cache: {chat.title}")
        except Exception as e:
            logger.error(f"Cannot load chat {chat_id}: {e}")
            return
        
        # Initialize stats
        await initialize_channel_stats(user_id, chat_id)
        
        delay = 1.0 if not user["is_premium"] else 0.2
        
        logger.info(f"Started auto-approve for user {user_id}, channel {chat_id}")
        
        # Register handler for LIVE join requests
        @user_client.on_chat_join_request(filters.chat(chat_id))
        async def handle_live_request(client, join_request):
            """Handle live join requests - APPROVE ONLY"""
            try:
                ch_check = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
                if not ch_check or not ch_check.get("is_active"):
                    return
                
                can_approve, _ = await check_request_limit(user_id)
                if not can_approve:
                    return
                
                # ONLY APPROVE - NO WELCOME MESSAGE
                await client.approve_chat_join_request(chat_id, join_request.from_user.id)
                
                # Update counters
                await users_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"daily_requests": 1, "total_requests": 1}}
                )
                await channels_col.update_one(
                    {"user_id": user_id, "chat_id": chat_id},
                    {"$inc": {"total_approved": 1}}
                )
                
                # Save user data (for broadcast)
                await save_approved_user(
                    user_id, chat_id,
                    join_request.from_user.id,
                    join_request.from_user.username,
                    join_request.from_user.first_name
                )
                
                logger.info(f"✅ Live approved: {join_request.from_user.id}")
                
            except errors.FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Error in live approval: {e}")
        
        # Process PENDING requests
        while True:
            ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
            if not ch or not ch.get("is_active"):
                logger.info(f"Stopping task for {user_id}_{chat_id}")
                break
            
            can_approve, _ = await check_request_limit(user_id)
            if not can_approve:
                await bot.send_message(
                    user_id,
                    f"⚠️ **Daily Limit Reached!**\n\n📢 {ch['title']}\n\n💎 Upgrade Premium!"
                )
                break
            
            try:
                pending_count = 0
                async for req in user_client.get_chat_join_requests(chat_id, limit=50):
                    try:
                        # ONLY APPROVE - NO WELCOME MESSAGE
                        await user_client.approve_chat_join_request(chat_id, req.user.id)
                        pending_count += 1
                        
                        # Update counters
                        await users_col.update_one(
                            {"user_id": user_id},
                            {"$inc": {"daily_requests": 1, "total_requests": 1}}
                        )
                        await channels_col.update_one(
                            {"user_id": user_id, "chat_id": chat_id},
                            {"$inc": {"total_approved": 1}}
                        )
                        
                        # Save user data (for broadcast)
                        await save_approved_user(
                            user_id, chat_id,
                            req.user.id,
                            req.user.username,
                            req.user.first_name
                        )
                        
                        await asyncio.sleep(delay)
                        
                    except errors.FloodWait as e:
                        logger.warning(f"FloodWait: {e.value}s")
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        logger.error(f"Error approving request: {e}")
                        continue
                
                if pending_count > 0:
                    logger.info(f"✅ Approved {pending_count} pending requests in {chat_id}")
                
                await asyncio.sleep(10)
                
            except errors.ChatAdminRequired:
                await bot.send_message(
                    user_id,
                    f"❌ **Admin Rights Lost!**\n\n📢 {ch['title']}\n\nMake sure you have admin rights."
                )
                break
            except errors.PeerIdInvalid:
                logger.error(f"PeerIdInvalid for {chat_id}, reloading...")
                try:
                    await user_client.get_chat(chat_id)
                    await asyncio.sleep(5)
                except:
                    break
            except Exception as e:
                logger.error(f"Error in approval loop: {e}")
                await asyncio.sleep(10)
                
    except asyncio.CancelledError:
        logger.info(f"Task cancelled for {user_id}_{chat_id}")
    except Exception as e:
        logger.error(f"Fatal error in auto_approve_task: {e}", exc_info=True)
# ==================== WELCOME MESSAGE ====================

@bot.on_callback_query(filters.regex("^remove_welcome_"))
async def remove_welcome_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    await channels_col.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"welcome_message": None}}
    )
    
    if user_id in user_states:
        del user_states[user_id]
    
    await callback_query.answer("✅ Welcome message removed!")
    await callback_query.message.edit_text(
        "✅ **Welcome Message Removed!**",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"channel_info_{chat_id}")]])
    )

@bot.on_callback_query(filters.regex("^remove_channel_"))
async def remove_channel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    # Stop task if running
    task_key = f"{user_id}_{chat_id}"
    if task_key in active_tasks:
        active_tasks[task_key].cancel()
        del active_tasks[task_key]
    
    # Remove channel
    await channels_col.delete_one({"user_id": user_id, "chat_id": chat_id})
    
    await callback_query.answer("✅ Channel removed!")
    await callback_query.message.edit_text(
        "✅ **Channel Removed!**",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 My Channels", callback_data="my_channels")]])
    )
# ==================== BROADCAST SYSTEM ====================

@bot.on_callback_query(filters.regex("^broadcast_channel_"))
async def broadcast_channel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    # Get approved users count
    approved_count = await approved_users_col.count_documents({"user_id": user_id, "chat_id": chat_id})
    
    if approved_count == 0:
        await callback_query.answer("❌ No approved users!", show_alert=True)
        return
    
    text = (
        f"📢 **Broadcast to Channel**\n\n"
        f"👥 **Approved Users:** {approved_count}\n\n"
        f"**How to broadcast:**\n"
        f"1. Send or forward any message/media\n"
        f"2. Reply to it with `/broadcast`\n"
        f"3. Message will be posted **IN THE CHANNEL**\n\n"
        f"⚠️ **Safe Mode:**\n"
        f"├ No private DMs (prevents ban)\n"
        f"├ Posted publicly in channel\n"
        f"└ All members can see it\n\n"
        f"💡 **Tip:** Use channel posts for announcements!"
    )
    
    broadcast_states[user_id] = chat_id
    
    keyboard = [
        [InlineKeyboardButton("❌ Cancel", callback_data=f"channel_info_{chat_id}")]
    ]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_message(filters.command("broadcast") & filters.private)
async def broadcast_command(client, message: Message):
    user_id = message.from_user.id
    
    # Check if user is in broadcast state
    if user_id not in broadcast_states:
        await message.reply_text("❌ **Not in broadcast mode!**\n\nGo to channel info → Broadcast")
        return
    
    # Check if replying to a message
    if not message.reply_to_message:
        await message.reply_text("❌ **Reply to a message to broadcast it!**")
        return
    
    chat_id = broadcast_states[user_id]
    ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
    
    if not ch:
        await message.reply_text("❌ Channel not found!")
        del broadcast_states[user_id]
        return
    
    # Initialize user client
    user_client = await initialize_user_client(user_id)
    if not user_client:
        await message.reply_text("❌ Session lost! Reconnect your account.")
        del broadcast_states[user_id]
        return
    
    status_msg = await message.reply_text(f"📢 **Broadcasting to channel...**")
    
    try:
        # Get the message to broadcast
        broadcast_msg = message.reply_to_message
        
        # Post message IN THE CHANNEL (not private DMs)
        if broadcast_msg.text:
            sent = await user_client.send_message(
                chat_id,
                broadcast_msg.text
            )
        elif broadcast_msg.photo:
            sent = await user_client.send_photo(
                chat_id,
                broadcast_msg.photo.file_id,
                caption=broadcast_msg.caption
            )
        elif broadcast_msg.video:
            sent = await user_client.send_video(
                chat_id,
                broadcast_msg.video.file_id,
                caption=broadcast_msg.caption
            )
        elif broadcast_msg.document:
            sent = await user_client.send_document(
                chat_id,
                broadcast_msg.document.file_id,
                caption=broadcast_msg.caption
            )
        elif broadcast_msg.audio:
            sent = await user_client.send_audio(
                chat_id,
                broadcast_msg.audio.file_id,
                caption=broadcast_msg.caption
            )
        elif broadcast_msg.voice:
            sent = await user_client.send_voice(
                chat_id,
                broadcast_msg.voice.file_id,
                caption=broadcast_msg.caption
            )
        elif broadcast_msg.animation:
            sent = await user_client.send_animation(
                chat_id,
                broadcast_msg.animation.file_id,
                caption=broadcast_msg.caption
            )
        else:
            # Try to copy message as is
            sent = await user_client.copy_message(
                chat_id,
                message.chat.id,
                broadcast_msg.id
            )
        
        # Success
        await status_msg.edit_text(
            f"✅ **Broadcast Complete!**\n\n"
            f"📢 Posted in: {ch['title']}\n"
            f"🔗 Message Link: https://t.me/c/{str(chat_id)[4:]}/{sent.id}\n\n"
            f"✨ **Safe Mode:** No private DMs sent"
        )
        
        logger.info(f"Broadcast posted in channel {chat_id}")
        
    except errors.ChatWriteForbidden:
        await status_msg.edit_text("❌ **No permission to post in channel!**\n\nMake sure you have send messages permission.")
    except Exception as e:
        await status_msg.edit_text(f"❌ **Broadcast Failed!**\n\nError: {str(e)}")
        logger.error(f"Broadcast error: {e}")
    
    del broadcast_states[user_id]
# ==================== ADMIN PANEL ====================

@bot.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        await callback_query.answer("❌ Owner only!", show_alert=True)
        return
    
    total = await users_col.count_documents({})
    premium = await users_col.count_documents({"is_premium": True})
    channels = await channels_col.count_documents({})
    active_channels = await channels_col.count_documents({"is_active": True})
    total_approved = await approved_users_col.count_documents({})
    
    text = (
        f"👑 **Admin Panel**\n\n"
        f"📊 **Statistics:**\n"
        f"├ 👥 Total Users: {total}\n"
        f"├ 💎 Premium: {premium}\n"
        f"├ 📁 Channels: {channels}\n"
        f"├ 🟢 Active: {active_channels}\n"
        f"└ ✅ Approved Users: {total_approved}\n\n"
        f"🔧 **Quick Actions:**"
    )
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add_premium"), InlineKeyboardButton("➖ Remove Premium", callback_data="admin_remove_premium")],
        [InlineKeyboardButton("📋 Premium List", callback_data="admin_premium_list"), InlineKeyboardButton("👥 All Users", callback_data="admin_all_users")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^admin_stats$"))
async def admin_stats_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    # Aggregate stats
    total_requests = await users_col.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$total_requests"}}}
    ]).to_list(1)
    
    total_req = total_requests[0]["total"] if total_requests else 0
    
    # Get top users
    top_users = await users_col.find({}).sort("total_requests", -1).limit(5).to_list(5)
    
    text = f"📊 **Detailed Stats**\n\n"
    text += f"📈 **Total Requests:** {total_req}\n\n"
    text += f"🏆 **Top Users:**\n"
    
    for idx, u in enumerate(top_users, 1):
        status = "💎" if u["is_premium"] else "🆓"
        text += f"{idx}. {status} `{u['user_id']}` - {u['total_requests']} reqs\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^admin_add_premium$"))
async def admin_add_premium_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    user_states[callback_query.from_user.id] = "admin_waiting_user_id"
    
    text = (
        "➕ **Add Premium**\n\n"
        "Send the User ID of the user you want to make premium.\n\n"
        "**Example:** `123456789`"
    )
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

async def admin_add_premium_handler(message: Message):
    user_id = message.from_user.id
    
    try:
        target = int(message.text.strip())
        tuser = await get_user(target)
        
        if not tuser:
            await message.reply_text("❌ User not found! They must start the bot first.")
            del user_states[user_id]
            return
        
        user_states[user_id] = f"admin_duration_{target}"
        
        keyboard = [
            [InlineKeyboardButton("7 Days", callback_data=f"premium_duration_7_{target}"), InlineKeyboardButton("30 Days", callback_data=f"premium_duration_30_{target}")],
            [InlineKeyboardButton("90 Days", callback_data=f"premium_duration_90_{target}"), InlineKeyboardButton("365 Days", callback_data=f"premium_duration_365_{target}")],
            [InlineKeyboardButton("♾️ Lifetime", callback_data=f"premium_duration_lifetime_{target}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]
        ]
        
        await message.reply_text(
            f"👤 **User:** `{target}`\n"
            f"📛 **Name:** {tuser.get('username', 'N/A')}\n\n"
            f"⏱️ **Select Duration:**",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except ValueError:
        await message.reply_text("❌ Invalid User ID! Send numbers only.")
        del user_states[user_id]
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")
        del user_states[user_id]

@bot.on_callback_query(filters.regex("^premium_duration_"))
async def premium_duration_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    parts = callback_query.data.split("_")
    duration = parts[2]
    target = int(parts[3])
    
    if duration == "lifetime":
        expiry = None
        dur_text = "Lifetime"
    else:
        days = int(duration)
        expiry = datetime.now() + timedelta(days=days)
        dur_text = f"{duration} days"
    
    await users_col.update_one(
        {"user_id": target},
        {"$set": {"is_premium": True, "premium_expires": expiry}}
    )
    
    try:
        await bot.send_message(
            target,
            f"🎉 **Premium Activated!**\n\n"
            f"⏱️ **Duration:** {dur_text}\n\n"
            f"✨ **Benefits:**\n"
            f"├ 🚀 5x faster approval\n"
            f"├ ♾️ Unlimited requests\n"
            f"└ 📁 Unlimited channels\n\n"
            f"Enjoy your premium features!"
        )
    except:
        pass
    
    if callback_query.from_user.id in user_states:
        del user_states[callback_query.from_user.id]
    
    await callback_query.message.edit_text(
        f"✅ **Premium Added!**\n\n"
        f"👤 **User:** `{target}`\n"
        f"⏱️ **Duration:** {dur_text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]])
    )
    await callback_query.answer("✅ Premium added!")

@bot.on_callback_query(filters.regex("^admin_remove_premium$"))
async def admin_remove_premium_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    user_states[callback_query.from_user.id] = "admin_remove_premium_id"
    
    text = (
        "➖ **Remove Premium**\n\n"
        "Send the User ID to remove premium.\n\n"
        "**Example:** `123456789`"
    )
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

async def admin_remove_premium_handler(message: Message):
    user_id = message.from_user.id
    
    try:
        target = int(message.text.strip())
        
        await users_col.update_one(
            {"user_id": target},
            {"$set": {"is_premium": False, "premium_expires": None}}
        )
        
        try:
            await bot.send_message(
                target,
                "❌ **Premium Removed**\n\n"
                "Your premium subscription has been removed.\n"
                "Contact @NeonGhost for more info."
            )
        except:
            pass
        
        await message.reply_text(
            f"✅ **Premium Removed!**\n\n👤 User: `{target}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]])
        )
        
        del user_states[user_id]
        
    except ValueError:
        await message.reply_text("❌ Invalid User ID!")
        del user_states[user_id]
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")
        del user_states[user_id]

@bot.on_callback_query(filters.regex("^admin_premium_list$"))
async def admin_premium_list_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    pusers = await users_col.find({"is_premium": True}).to_list(None)
    
    if not pusers:
        text = "📋 **Premium Users**\n\n❌ No premium users"
    else:
        text = f"📋 **Premium Users ({len(pusers)})**\n\n"
        for idx, u in enumerate(pusers, 1):
            exp = u.get("premium_expires")
            if exp:
                days_left = (exp - datetime.now()).days
                exp_text = f"{days_left}d left"
            else:
                exp_text = "Lifetime"
            text += f"{idx}. `{u['user_id']}` - {exp_text}\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^admin_all_users$"))
async def admin_all_users_callback(client, callback_query):
    if not await is_owner(callback_query.from_user.id):
        return
    
    ausers = await users_col.find({}).sort("created_at", -1).limit(30).to_list(None)
    
    text = f"👥 **Recent Users (30)**\n\n"
    for idx, u in enumerate(ausers, 1):
        status = "💎" if u["is_premium"] else "🆓"
        text += f"{idx}. {status} `{u['user_id']}` - {u['total_requests']} reqs\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

async def admin_broadcast_handler(message: Message):
    user_id = message.from_user.id
    text = message.text
    
    status = await message.reply_text("📢 **Broadcasting...**\n\n0 sent...")
    
    ausers = await users_col.find({}).to_list(None)
    success = 0
    failed = 0
    
    for idx, u in enumerate(ausers, 1):
        try:
            await bot.send_message(u["user_id"], text)
            success += 1
            
            if success % 20 == 0:
                await status.edit_text(f"📢 **Broadcasting...**\n\n✅ {success} sent...")
            
            await asyncio.sleep(0.05)
        except:
            failed += 1
    
    await status.edit_text(
        f"✅ **Broadcast Complete!**\n\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Total: {len(ausers)}"
    )
    
    del user_states[user_id]
# ==================== TEXT HANDLER ====================

@bot.on_message(filters.text & filters.private & ~filters.command(["start", "connect", "code", "password", "broadcast"]))
async def handle_text(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_states:
        return
    
    # Handle dict states (waiting_code, waiting_2fa)
    if isinstance(user_states.get(user_id), dict):
        state_value = user_states[user_id].get("state")
        if state_value in ["waiting_code", "waiting_2fa"]:
            return
    
    state = user_states[user_id]
    
    # Handle string states
    if isinstance(state, str):
        if state == "waiting_channel":
            await add_channel_process(client, message)
        elif state == "admin_waiting_user_id":
            await admin_add_premium_handler(message)
        elif state == "admin_remove_premium_id":
            await admin_remove_premium_handler(message)
        elif state == "admin_broadcast_message":
            await admin_broadcast_handler(message)

# ==================== HELP & MENU ====================

@bot.on_callback_query(filters.regex("^main_menu$"))
async def main_menu_callback(client, callback_query):
    await callback_query.message.delete()
    await start_command(client, callback_query.message)

@bot.on_callback_query(filters.regex("^help$"))
async def help_callback(client, callback_query):
    text = (
        "❓ **Help & Guide**\n\n"
        "**📱 Setup:**\n"
        "1. Get API credentials from https://my.telegram.org\n"
        "2. Use `/connect API_ID API_HASH PHONE`\n"
        "3. Enter OTP code with `/code`\n"
        "4. Add your channels\n"
        "5. Start auto-approve\n\n"
        "**🔧 Features:**\n"
        "├ Auto-approve pending requests\n"
        "├ Auto-approve live requests\n"
        "├ Custom welcome messages\n"
        "├ Broadcast to approved users\n"
        "└ Session management\n\n"
        "**💬 Broadcast:**\n"
        "1. Go to channel info\n"
        "2. Click Broadcast\n"
        "3. Reply to any message with `/broadcast`\n\n"
        "**💎 Premium Benefits:**\n"
        "├ 5x faster approval\n"
        "├ Unlimited requests\n"
        "├ Unlimited channels\n"
        "└ Priority support\n\n"
        "📞 **Support:** @NeonGhost"
    )
    
    keyboard = [[InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

# ==================== STARTUP & MAIN ====================

async def restore_sessions():
    """Restore user sessions on startup"""
    try:
        sessions = await sessions_col.find({}).to_list(None)
        logger.info(f"Found {len(sessions)} saved sessions")
        
        for session in sessions:
            try:
                user_id = session["user_id"]
                user_client = Client(
                    f"user_{user_id}",
                    api_id=session["api_id"],
                    api_hash=session["api_hash"],
                    session_string=session["session_string"],
                    in_memory=True
                )
                await user_client.start()
                user_clients[user_id] = user_client
                logger.info(f"Restored session for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to restore session for {session['user_id']}: {e}")
        
        logger.info(f"✅ Restored {len(user_clients)} sessions")
    except Exception as e:
        logger.error(f"Error restoring sessions: {e}")

async def restore_active_tasks():
    """Restore active auto-approve tasks on startup"""
    try:
        active_channels = await channels_col.find({"is_active": True}).to_list(None)
        logger.info(f"Found {len(active_channels)} active channels")
        
        for ch in active_channels:
            user_id = ch["user_id"]
            chat_id = ch["chat_id"]
            
            # Check if user client exists
            if user_id in user_clients:
                task_key = f"{user_id}_{chat_id}"
                task = asyncio.create_task(auto_approve_task(user_id, chat_id))
                active_tasks[task_key] = task
                logger.info(f"Restored task for {task_key}")
        
        logger.info(f"✅ Restored {len(active_tasks)} active tasks")
    except Exception as e:
        logger.error(f"Error restoring tasks: {e}")

# Schedule jobs
scheduler.add_job(check_premium_expiry, 'interval', hours=1)
scheduler.add_job(send_premium_reminders, 'interval', hours=6)
scheduler.add_job(reset_daily_limits, 'cron', hour=0, minute=0)

async def main():
    """Main function"""
    try:
        # Test MongoDB connection
        await mongo_client.admin.command('ping')
        logger.info("✅ MongoDB connected!")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return
    
    # Start scheduler
    scheduler.start()
    logger.info("✅ Scheduler started")
    
    # Start bot
    await bot.start()
    me = await bot.get_me()
    logger.info(f"✅ Bot started: @{me.username}")
    
    # Restore sessions and tasks
    await restore_sessions()
    await restore_active_tasks()
    
    logger.info("🚀 Bot is fully operational!")
    
    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        bot.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
