import os
import logging
import asyncio
import psutil
from datetime import datetime, timedelta
from pyrogram import Client, filters, errors, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import json
from collections import defaultdict

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================

BOT_TOKEN = "8572583556:AAFeadR0wqigQKKTi3oLeWXyuNF6BxgsosI"
API_ID = "27352735"
API_HASH = "8c4512c1052a60e05b05522a2ea12e5e"
DB_URI = "mongodb+srv://50duddubot518:50duddubot518@cluster0.momby1w.mongodb.net/?appName=Cluster0"
OWNER_USERNAME = "NeonGhost"
OWNER_ID = None
ADMIN_IDS = []  # Add admin user IDs here like [123456789, 987654321]
WELCOME_IMAGE = "https://te.legra.ph/file/c5b07f2679e49c58bfb1b.jpg"

# Create directories
os.makedirs("sessions", exist_ok=True)
os.makedirs("data", exist_ok=True)

# ==================== MONGODB SETUP ====================

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
    bot_users_col = db['bot_users']  # Track bot users for broadcast
    
    # Create indexes for better performance
    async def create_indexes():
        await users_col.create_index("user_id", unique=True)
        await channels_col.create_index([("user_id", 1), ("chat_id", 1)])
        await sessions_col.create_index("user_id", unique=True)
        await bot_users_col.create_index("user_id", unique=True)
        logger.info("✅ Database indexes created")
        
except Exception as e:
    logger.error(f"❌ MongoDB setup error: {e}")

# ==================== GLOBAL VARIABLES ====================

scheduler = AsyncIOScheduler()
bot = Client("auto_approve_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH, workdir="data")

# Storage
user_clients = {}
user_states = {}
active_tasks = {}
live_handlers = {}  # Track live handlers per channel
stats_cache = {}  # Cache for statistics
cache_timestamp = {}  # Cache expiry tracking

# Performance tracking
start_time = datetime.now()
approval_counter = defaultdict(int)  # Track approvals per hour

# ==================== RATE LIMITS ====================

RATE_LIMITS = {
    "free": {
        "pending_delay": 1.0,  # 1 req/sec
        "pending_batch": 50,
        "daily_limit": 1000,
        "max_channels": 3
    },
    "premium": {
        "pending_delay": 0.1,  # 10 req/sec (SAFE MAX)
        "pending_batch": 100,
        "daily_limit": None,  # Unlimited
        "max_channels": None  # Unlimited
    }
}

# ==================== HELPER FUNCTIONS ====================

async def is_owner(user_id):
    """Check if user is owner"""
    global OWNER_ID
    if OWNER_ID is None:
        try:
            owner = await bot.get_users(OWNER_USERNAME)
            OWNER_ID = owner.id
        except:
            pass
    return user_id == OWNER_ID

async def is_admin(user_id):
    """Check if user is admin or owner"""
    return await is_owner(user_id) or user_id in ADMIN_IDS

async def ensure_admin_premium(user_id):
    """Auto-premium for owner and admins"""
    if await is_admin(user_id):
        await users_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "is_premium": True,
                "premium_expires": None  # Lifetime
            }},
            upsert=True
        )
        logger.info(f"✅ Auto-premium granted to admin {user_id}")

async def track_bot_user(user_id, username, first_name):
    """Track users who start the bot"""
    try:
        await bot_users_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_interaction": datetime.now(),
                "started_bot": True
            }},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error tracking bot user: {e}")

async def get_user(user_id):
    """Get user from database"""
    try:
        return await users_col.find_one({"user_id": user_id})
    except:
        return None

async def create_user(user_id, username):
    """Create new user"""
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
        
        # Auto-premium for admins
        await ensure_admin_premium(user_id)
        
        return user_data
    except:
        return None

async def get_user_channels(user_id):
    """Get user's channels"""
    try:
        return await channels_col.find({"user_id": user_id}).to_list(None)
    except:
        return []

async def can_add_channel(user_id):
    """Check if user can add more channels"""
    try:
        user = await get_user(user_id)
        if user and (user["is_premium"] or await is_admin(user_id)):
            return True, None
        
        count = await channels_col.count_documents({"user_id": user_id})
        max_channels = RATE_LIMITS["free"]["max_channels"]
        
        if count >= max_channels:
            return False, f"❌ Free users can add max {max_channels} channels. Upgrade to Premium!"
        return True, None
    except:
        return False, "❌ Error checking channel limit"

async def check_request_limit(user_id):
    """Check if user can make more requests"""
    try:
        user = await get_user(user_id)
        if not user:
            return False, 0
        
        # Reset if new day
        if (datetime.now() - user["last_reset"]).days >= 1:
            await users_col.update_one(
                {"user_id": user_id},
                {"$set": {"daily_requests": 0, "last_reset": datetime.now()}}
            )
            user["daily_requests"] = 0
        
        # Premium or admin = unlimited
        if user["is_premium"] or await is_admin(user_id):
            return True, user["daily_requests"]
        
        # Free user limit
        limit = RATE_LIMITS["free"]["daily_limit"]
        if user["daily_requests"] >= limit:
            return False, user["daily_requests"]
        
        return True, user["daily_requests"]
    except:
        return False, 0
    
# ==================== SESSION MANAGEMENT ====================

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
                "updated_at": datetime.now()
            }},
            upsert=True
        )
        logger.info(f"✅ Session saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving session: {e}")
        return False

async def load_session(user_id):
    """Load session from database"""
    try:
        return await sessions_col.find_one({"user_id": user_id})
    except Exception as e:
        logger.error(f"Error loading session: {e}")
        return None

async def delete_user_session(user_id):
    """Delete user session"""
    try:
        # Stop all tasks for this user
        tasks_to_remove = [key for key in active_tasks.keys() if key.startswith(f"{user_id}_")]
        for task_key in tasks_to_remove:
            active_tasks[task_key].cancel()
            del active_tasks[task_key]
        
        # Remove live handlers
        handlers_to_remove = [key for key in live_handlers.keys() if key.startswith(f"{user_id}_")]
        for handler_key in handlers_to_remove:
            del live_handlers[handler_key]
        
        # Disconnect client
        if user_id in user_clients:
            try:
                await user_clients[user_id].stop()
            except:
                pass
            del user_clients[user_id]
        
        # Delete from database
        await sessions_col.delete_one({"user_id": user_id})
        
        logger.info(f"✅ Session deleted for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting session: {e}")
        return False

async def initialize_user_client(user_id):
    """Initialize user client from saved session"""
    try:
        # Check if client already exists and is connected
        if user_id in user_clients:
            try:
                await user_clients[user_id].get_me()
                return user_clients[user_id]
            except:
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
        logger.info(f"✅ Client initialized for user {user_id}")
        return user_client
        
    except Exception as e:
        logger.error(f"❌ Error initializing client for {user_id}: {e}")
        return None
    
# ==================== STATISTICS FUNCTIONS ====================

async def get_system_stats():
    """Get system resource usage"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        ram_percent = ram.percent
        uptime = datetime.now() - start_time
        
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        seconds = int(uptime.total_seconds() % 60)
        
        return {
            "cpu": cpu_percent,
            "ram": ram_percent,
            "uptime": f"{hours}h {minutes}m {seconds}s"
        }
    except:
        return {"cpu": 0, "ram": 0, "uptime": "0h 0m 0s"}

async def get_database_stats():
    """Get database statistics"""
    try:
        stats = await db.command("dbStats")
        return {
            "size": round(stats.get("dataSize", 0) / (1024 * 1024), 2),  # MB
            "collections": len(await db.list_collection_names()),
            "documents": sum([
                await users_col.count_documents({}),
                await channels_col.count_documents({}),
                await sessions_col.count_documents({}),
                await bot_users_col.count_documents({})
            ])
        }
    except Exception as e:
        logger.error(f"Error getting DB stats: {e}")
        return {"size": 0, "collections": 0, "documents": 0}

async def get_cached_stats(force_refresh=False):
    """Get statistics with caching (5 min cache)"""
    cache_key = "admin_stats"
    
    # Check cache
    if not force_refresh and cache_key in stats_cache:
        if cache_key in cache_timestamp:
            age = (datetime.now() - cache_timestamp[cache_key]).seconds
            if age < 300:  # 5 minutes
                return stats_cache[cache_key]
    
    # Fetch fresh stats
    try:
        total_users = await users_col.count_documents({})
        premium_users = await users_col.count_documents({"is_premium": True})
        total_channels = await channels_col.count_documents({})
        active_channels = await channels_col.count_documents({"is_active": True})
        live_enabled = await channels_col.count_documents({"live_mode": True})
        bot_users = await bot_users_col.count_documents({})
        
        # Active users (last 24h)
        yesterday = datetime.now() - timedelta(days=1)
        active_users = await bot_users_col.count_documents({
            "last_interaction": {"$gte": yesterday}
        })
        
        # Total approvals
        pipeline = [
            {"$group": {"_id": None, "total": {"$sum": "$total_approved"}}}
        ]
        approval_result = await channels_col.aggregate(pipeline).to_list(1)
        total_approvals = approval_result[0]["total"] if approval_result else 0
        
        # Today's approvals
        today_pipeline = [
            {"$group": {"_id": None, "total": {"$sum": "$daily_requests"}}}
        ]
        today_result = await users_col.aggregate(today_pipeline).to_list(1)
        today_approvals = today_result[0]["total"] if today_result else 0
        
        # Database stats
        db_stats = await get_database_stats()
        
        # System stats
        sys_stats = await get_system_stats()
        
        stats = {
            "total_users": total_users,
            "active_users": active_users,
            "premium_users": premium_users,
            "premium_percentage": round((premium_users / total_users * 100), 1) if total_users > 0 else 0,
            "bot_users": bot_users,
            "total_channels": total_channels,
            "active_channels": active_channels,
            "live_enabled": live_enabled,
            "total_approvals": total_approvals,
            "today_approvals": today_approvals,
            "db_size": db_stats["size"],
            "db_collections": db_stats["collections"],
            "db_documents": db_stats["documents"],
            "cpu": sys_stats["cpu"],
            "ram": sys_stats["ram"],
            "uptime": sys_stats["uptime"],
            "active_tasks": len(active_tasks),
            "active_sessions": len(user_clients)
        }
        
        # Cache it
        stats_cache[cache_key] = stats
        cache_timestamp[cache_key] = datetime.now()
        
        return stats
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return None

async def format_admin_stats(stats):
    """Format statistics in beautiful UI"""
    if not stats:
        return "❌ Error loading statistics"
    
    text = (
        f"╭────[ 📊 ʟɪᴠᴇ sᴛᴀᴛɪsᴛɪᴄs ] ────⍟\n"
        f"│\n"
        f"├──[ 👥 ᴜsᴇʀ ᴍᴇᴛʀɪᴄs ]\n"
        f"│   ├⋟ ᴛᴏᴛᴀʟ ᴜsᴇʀs ⋟ {stats['total_users']:,}\n"
        f"│   ├⋟ ʙᴏᴛ ᴜsᴇʀs ⋟ {stats['bot_users']:,}\n"
        f"│   ├⋟ ᴀᴄᴛɪᴠᴇ (24ʜ) ⋟ {stats['active_users']:,}\n"
        f"│   ├⋟ ᴘʀᴇᴍɪᴜᴍ ⋟ {stats['premium_users']:,} ({stats['premium_percentage']}%)\n"
        f"│\n"
        f"├──[ 📢 ᴄʜᴀɴɴᴇʟ ᴍᴇᴛʀɪᴄs ]\n"
        f"│   ├⋟ ᴛᴏᴛᴀʟ ᴄʜᴀɴɴᴇʟs ⋟ {stats['total_channels']:,}\n"
        f"│   ├⋟ ᴀᴄᴛɪᴠᴇ ⋟ {stats['active_channels']:,}\n"
        f"│   ├⋟ ʟɪᴠᴇ ᴍᴏᴅᴇ ᴏɴ ⋟ {stats['live_enabled']:,}\n"
        f"│   └⋟ ᴀᴠɢ/ᴜsᴇʀ ⋟ {round(stats['total_channels'] / stats['total_users'], 1) if stats['total_users'] > 0 else 0}\n"
        f"│\n"
        f"├──[ ✅ ᴀᴘᴘʀᴏᴠᴀʟ ᴍᴇᴛʀɪᴄs ]\n"
        f"│   ├⋟ ᴛᴏᴛᴀʟ ᴀᴘᴘʀᴏᴠᴇᴅ ⋟ {stats['total_approvals']:,}\n"
        f"│   ├⋟ ᴛᴏᴅᴀʏ ⋟ {stats['today_approvals']:,}\n"
        f"│   └⋟ ᴀᴠɢ/ᴅᴀʏ ⋟ {stats['today_approvals']:,}\n"
        f"│\n"
        f"├──[ 🗄️ ᴅᴀᴛᴀʙᴀsᴇ ɪɴꜰᴏ ]\n"
        f"│   ├⋟ ᴅʙ ꜱɪᴢᴇ ⋟ {stats['db_size']:.2f} MB\n"
        f"│   ├⋟ ᴄᴏʟʟᴇᴄᴛɪᴏɴꜱ ⋟ {stats['db_collections']}\n"
        f"│   └⋟ ᴅᴏᴄᴜᴍᴇɴᴛꜱ ⋟ {stats['db_documents']:,}\n"
        f"│\n"
        f"├──[ 🤖 ʙᴏᴛ ʜᴇᴀʟᴛʜ ]\n"
        f"│   ├⋟ ᴜᴘᴛɪᴍᴇ ⋟ {stats['uptime']}\n"
        f"│   ├⋟ ʀᴀᴍ ᴜsᴀɢᴇ ⋟ {stats['ram']:.1f}%\n"
        f"│   ├⋟ ᴄᴘᴜ ʟᴏᴀᴅ ⋟ {stats['cpu']:.1f}%\n"
        f"│   ├⋟ ᴀᴄᴛɪᴠᴇ ᴛᴀsᴋs ⋟ {stats['active_tasks']}\n"
        f"│   └⋟ sᴇssɪᴏɴs ⋟ {stats['active_sessions']}\n"
        f"│\n"
        f"╰─────────────────────⍟"
    )
    
    return text
# ==================== SCHEDULED TASKS ====================

async def check_premium_expiry():
    """Check and expire premium users"""
    try:
        expired_users = await users_col.find({
            "is_premium": True,
            "premium_expires": {"$lt": datetime.now(), "$ne": None}
        }).to_list(None)
        
        for user in expired_users:
            # Skip admins
            if await is_admin(user["user_id"]):
                continue
            
            await users_col.update_one(
                {"user_id": user["user_id"]},
                {"$set": {"is_premium": False, "premium_expires": None}}
            )
            
            try:
                await bot.send_message(
                    user["user_id"],
                    "❌ **Premium Expired!**\n\n"
                    f"Contact @{OWNER_USERNAME} to renew!"
                )
            except:
                pass
                
        if expired_users:
            logger.info(f"✅ Expired {len(expired_users)} premium subscriptions")
            
    except Exception as e:
        logger.error(f"Error checking premium expiry: {e}")

async def send_premium_reminders():
    """Send reminders 3 days before expiry"""
    try:
        reminder_date = datetime.now() + timedelta(days=3)
        
        users = await users_col.find({
            "is_premium": True,
            "premium_expires": {
                "$gte": datetime.now(),
                "$lte": reminder_date,
                "$ne": None
            }
        }).to_list(None)
        
        for user in users:
            days_left = (user["premium_expires"] - datetime.now()).days
            try:
                await bot.send_message(
                    user["user_id"],
                    f"⚠️ **Premium Expiring Soon!**\n\n"
                    f"📅 {days_left} days left\n"
                    f"Contact @{OWNER_USERNAME} to renew!"
                )
            except:
                pass
                
    except Exception as e:
        logger.error(f"Error sending reminders: {e}")

async def reset_daily_limits():
    """Reset daily request limits"""
    try:
        await users_col.update_many(
            {},
            {"$set": {"daily_requests": 0, "last_reset": datetime.now()}}
        )
        logger.info("✅ Daily limits reset")
    except Exception as e:
        logger.error(f"Error resetting limits: {e}")

async def cleanup_inactive_sessions():
    """Cleanup sessions inactive for 30+ days"""
    try:
        cutoff = datetime.now() - timedelta(days=30)
        
        inactive = await sessions_col.find({
            "updated_at": {"$lt": cutoff}
        }).to_list(None)
        
        for session in inactive:
            await delete_user_session(session["user_id"])
            
        if inactive:
            logger.info(f"✅ Cleaned up {len(inactive)} inactive sessions")
            
    except Exception as e:
        logger.error(f"Error cleaning up sessions: {e}")

async def memory_cleanup():
    """Cleanup memory periodically"""
    try:
        # Remove disconnected clients
        to_remove = []
        for user_id, client in list(user_clients.items()):
            try:
                await client.get_me()
            except:
                to_remove.append(user_id)
        
        for user_id in to_remove:
            del user_clients[user_id]
            logger.info(f"✅ Removed disconnected client: {user_id}")
        
        # Clear old cache
        for key in list(cache_timestamp.keys()):
            age = (datetime.now() - cache_timestamp[key]).seconds
            if age > 600:  # 10 minutes
                if key in stats_cache:
                    del stats_cache[key]
                del cache_timestamp[key]
        
    except Exception as e:
        logger.error(f"Error in memory cleanup: {e}")
# ==================== START COMMAND ====================
# ==================== START COMMAND ====================

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    first_name = message.from_user.first_name
    
    # Track bot user
    await track_bot_user(user_id, username, first_name)
    
    # Get or create user
    user = await get_user(user_id)
    if not user:
        await create_user(user_id, username)
        user = await get_user(user_id)
    
    # Check if admin
    is_admin_user = await is_admin(user_id)
    
    welcome_text = (
        f"👋 **Welcome {first_name}!**\n\n"
        f"🤖 **Auto-Approve Bot**\n"
        f"Automatically approve join requests!\n\n"
        f"✨ **Features:**\n"
        f"├ 🚀 Auto-approve (Pending + Live)\n"
        f"├ 📊 Real-time stats\n"
        f"├ ⚡ Live mode toggle\n"
        f"├ 📈 Multi-channel support\n"
        f"└ 🔒 Safe & secure\n\n"
    )
    
    if is_admin_user:
        welcome_text += "👑 **You are Admin!** (Full Premium)"
    elif user:
        welcome_text += "💎 **Plan:** " + ("Premium ✨" if user["is_premium"] else "Free 🆓")
    
    keyboard = [
        [InlineKeyboardButton("📱 Connect Account", callback_data="connect_account")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
        [InlineKeyboardButton("📚 My Channels", callback_data="my_channels")],
        [InlineKeyboardButton("🔄 Session Manager", callback_data="session_manager")],
    ]
    
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    
    keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help")])
    
    try:
        await message.reply_photo(
            photo=WELCOME_IMAGE,
            caption=welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except:
        await message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ==================== DASHBOARD ====================

@bot.on_callback_query(filters.regex("^dashboard$"))
async def dashboard_callback(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback_query.answer("❌ /start again", show_alert=True)
        return
    
    channels_count = await channels_col.count_documents({"user_id": user_id})
    active_count = await channels_col.count_documents({"user_id": user_id, "is_active": True})
    premium_status = "✅ Active" if user["is_premium"] else "❌ Inactive"
    
    if user["is_premium"] and user.get("premium_expires"):
        days_left = (user["premium_expires"] - datetime.now()).days
        premium_status += f"\n📅 {days_left} days left"
    
    is_admin_user = await is_admin(user_id)
    if is_admin_user:
        premium_status = "👑 Admin (Lifetime)"
    
    text = (
        f"📊 **Dashboard**\n\n"
        f"👤 {callback_query.from_user.first_name}\n"
        f"🆔 `{user_id}`\n"
        f"💎 Premium: {premium_status}\n\n"
        f"📈 **Stats:**\n"
        f"├ 📁 Channels: {channels_count}\n"
        f"├ 🟢 Active: {active_count}\n"
        f"├ 📝 Today: {user['daily_requests']:,}/{'∞' if user['is_premium'] or is_admin_user else '1,000'}\n"
        f"├ 📊 Total: {user['total_requests']:,}\n"
        f"└ 📅 Since: {user['created_at'].strftime('%d %b %Y')}\n\n"
    )
    
    if not user["is_premium"] and not is_admin_user:
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

@bot.on_callback_query(filters.regex("^main_menu$"))
async def main_menu_callback(client, callback_query):
    await callback_query.message.delete()
    # Create a fake message object to reuse start_command
    await start_command(client, callback_query.message)
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
        original_input = channel_input
        
        # Clean input
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
                "live_mode": False,  # NEW: Live mode toggle
                "welcome_message": None,
                "total_approved": 0,
                "today_approved": 0,
                "total_declined": 0,
                "last_activity": "Never",
                "added_at": datetime.now()
            }},
            upsert=True
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
            live_icon = "⚡" if ch.get("live_mode") else ""
            text += f"{idx}. {status}{live_icon} {ch['title']}\n"
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
    live_status = "✅ ON" if ch.get("live_mode") else "❌ OFF"
    
    text = (
        f"╭────[ 📢 ᴄʜᴀɴɴᴇʟ ᴅᴀsʜʙᴏᴀʀᴅ ] ────⍟\n"
        f"│\n"
        f"│  📺 {ch['title']}\n"
        f"│  🆔 `{chat_id}`\n"
        f"│\n"
        f"├──[ ⚙️ sᴇᴛᴛɪɴɢs ]\n"
        f"│   ├⋟ sᴛᴀᴛᴜs ⋟ {status}\n"
        f"│   ├⋟ ᴀᴜᴛᴏ-ᴀᴘᴘʀᴏᴠᴇ ⋟ {'✅ ᴇɴᴀʙʟᴇᴅ' if ch.get('auto_approve_enabled') else '❌ ᴅɪsᴀʙʟᴇᴅ'}\n"
        f"│   └⋟ ʟɪᴠᴇ ᴍᴏᴅᴇ ⋟ {live_status}\n"
        f"│\n"
        f"├──[ 📊 sᴛᴀᴛɪsᴛɪᴄs ]\n"
        f"│   ├⋟ ᴛᴏᴛᴀʟ ᴀᴘᴘʀᴏᴠᴇᴅ ⋟ {ch.get('total_approved', 0):,}\n"
        f"│   ├⋟ ᴛᴏᴅᴀʏ ⋟ {ch.get('today_approved', 0):,}\n"
        f"│   └⋟ ʟᴀsᴛ ᴀᴄᴛɪᴠɪᴛʏ ⋟ {ch.get('last_activity', 'N/A')}\n"
        f"│\n"
        f"├──[ 🔗 ʟɪɴᴋ ]\n"
        f"│   └⋟ {ch['invite_link']}\n"
        f"│\n"
        f"╰─────────────────────⍟\n\n"
        f"⚠️ **Safe Mode:** No welcome messages"
    )
    
    # Dynamic button text
    live_button_text = "🔴 Live: OFF" if ch.get("live_mode") else "🟢 Live: ON"
    
    keyboard = [
        [InlineKeyboardButton("▶️ Start", callback_data=f"start_approve_{chat_id}"), InlineKeyboardButton("⏸️ Stop", callback_data=f"stop_approve_{chat_id}")],
        [InlineKeyboardButton(live_button_text, callback_data=f"toggle_live_{chat_id}")],
        [InlineKeyboardButton("🗑️ Remove", callback_data=f"remove_channel_{chat_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="my_channels")]
    ]
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()

# ==================== TOGGLE LIVE MODE ====================

@bot.on_callback_query(filters.regex("^toggle_live_"))
async def toggle_live_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
    if not ch:
        await callback_query.answer("❌ Channel not found!", show_alert=True)
        return
    
    # Toggle live mode
    new_live_mode = not ch.get("live_mode", False)
    
    await channels_col.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"live_mode": new_live_mode}}
    )
    
    if new_live_mode:
        await callback_query.answer("✅ Live Mode Enabled! Requests will be approved instantly.", show_alert=True)
    else:
        await callback_query.answer("❌ Live Mode Disabled!", show_alert=True)
    
    # Refresh channel info
    await channel_info_callback(client, callback_query)

# ==================== REMOVE CHANNEL ====================

@bot.on_callback_query(filters.regex("^remove_channel_"))
async def remove_channel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    chat_id = int(callback_query.data.split("_")[2])
    
    # Stop task if running
    task_key = f"{user_id}_{chat_id}"
    if task_key in active_tasks:
        active_tasks[task_key].cancel()
        del active_tasks[task_key]
    
    # Remove live handler
    if task_key in live_handlers:
        del live_handlers[task_key]
    
    # Remove channel
    await channels_col.delete_one({"user_id": user_id, "chat_id": chat_id})
    
    await callback_query.answer("✅ Channel removed!")
    await callback_query.message.edit_text(
        "✅ **Channel Removed!**",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 My Channels", callback_data="my_channels")]])
    )
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
        f"✅ **Started!**\n\n📢 {ch['title']}\n🔄 Auto-approving...",
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
    
    if task_key in live_handlers:
        del live_handlers[task_key]
    
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
    """Main auto-approve task"""
    try:
        user = await get_user(user_id)
        user_client = await initialize_user_client(user_id)
        
        if not user_client:
            return
        
        ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
        if not ch:
            return
        
        try:
            chat = await user_client.get_chat(chat_id)
        except Exception as e:
            logger.error(f"Cannot load chat {chat_id}: {e}")
            return
        
        is_premium_user = user["is_premium"] or await is_admin(user_id)
        rate_config = RATE_LIMITS["premium"] if is_premium_user else RATE_LIMITS["free"]
        delay = rate_config["pending_delay"]
        batch_size = rate_config["pending_batch"]
        
        logger.info(f"Started auto-approve for {user_id}_{chat_id} [Speed: {1/delay} req/s]")
        
        # LIVE HANDLER
        task_key = f"{user_id}_{chat_id}"
        if task_key not in live_handlers:
            @user_client.on_chat_join_request(filters.chat(chat_id))
            async def handle_live_request(client, join_request):
                try:
                    ch_check = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
                    if not ch_check or not ch_check.get("live_mode", False):
                        return
                    
                    await client.approve_chat_join_request(chat_id, join_request.from_user.id)
                    asyncio.create_task(channels_col.update_one(
                        {"user_id": user_id, "chat_id": chat_id},
                        {"$inc": {"total_approved": 1, "today_approved": 1}, "$set": {"last_activity": datetime.now().strftime("%H:%M")}}
                    ))
                    logger.info(f"✅ Live approved: {join_request.from_user.id}")
                except errors.FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logger.error(f"Live approval error: {e}")
            
            live_handlers[task_key] = handle_live_request
        
        # PENDING LOOP
        while True:
            ch = await channels_col.find_one({"user_id": user_id, "chat_id": chat_id})
            if not ch or not ch.get("is_active"):
                break
            
            can_approve, _ = await check_request_limit(user_id)
            if not can_approve:
                await bot.send_message(user_id, f"⚠️ **Daily Limit Reached!**\n\n📢 {ch['title']}\n\n💎 Upgrade Premium!")
                break
            
            try:
                pending_count = 0
                async for req in user_client.get_chat_join_requests(chat_id, limit=batch_size):
                    try:
                        await user_client.approve_chat_join_request(chat_id, req.user.id)
                        pending_count += 1
                        
                        if pending_count % 10 == 0:
                            await users_col.update_one({"user_id": user_id}, {"$inc": {"daily_requests": 10, "total_requests": 10}})
                            await channels_col.update_one({"user_id": user_id, "chat_id": chat_id}, {"$inc": {"total_approved": 10, "today_approved": 10}, "$set": {"last_activity": datetime.now().strftime("%H:%M")}})
                        
                        await asyncio.sleep(delay)
                    except errors.FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        logger.error(f"Approval error: {e}")
                
                if pending_count > 0:
                    remaining = pending_count % 10
                    if remaining > 0:
                        await users_col.update_one({"user_id": user_id}, {"$inc": {"daily_requests": remaining, "total_requests": remaining}})
                        await channels_col.update_one({"user_id": user_id, "chat_id": chat_id}, {"$inc": {"total_approved": remaining, "today_approved": remaining}})
                    logger.info(f"✅ Approved {pending_count} pending in {chat_id}")
                
                await asyncio.sleep(10)
            except errors.ChatAdminRequired:
                await bot.send_message(user_id, f"❌ **Admin Rights Lost!**\n\n📢 {ch['title']}")
                break
            except Exception as e:
                logger.error(f"Error in approval loop: {e}")
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        logger.info(f"Task cancelled for {user_id}_{chat_id}")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
# ==================== ADMIN PANEL ====================

@bot.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Admin only!", show_alert=True)
        return
    
    stats = await get_cached_stats()
    if not stats:
        await callback_query.answer("❌ Error loading stats", show_alert=True)
        return
    
    text = await format_admin_stats(stats)
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add_premium"), InlineKeyboardButton("➖ Remove Premium", callback_data="admin_remove_premium")],
        [InlineKeyboardButton("📋 Premium List", callback_data="admin_premium_list"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_refresh")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    
    try:
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except errors.MessageNotModified:
        pass
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^admin_refresh$"))
async def admin_refresh_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
        return
    stats = await get_cached_stats(force_refresh=True)
    text = await format_admin_stats(stats)
    keyboard = [
        [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add_premium"), InlineKeyboardButton("➖ Remove Premium", callback_data="admin_remove_premium")],
        [InlineKeyboardButton("📋 Premium List", callback_data="admin_premium_list"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_refresh")],
        [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]
    ]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer("✅ Refreshed!")

@bot.on_callback_query(filters.regex("^admin_add_premium$"))
async def admin_add_premium_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
        return
    user_states[callback_query.from_user.id] = "admin_waiting_user_id"
    text = "➕ **Add Premium**\n\nSend User ID:\n\n**Example:** `123456789`"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

async def admin_add_premium_handler(message: Message):
    user_id = message.from_user.id
    try:
        target = int(message.text.strip())
        tuser = await get_user(target)
        if not tuser:
            await message.reply_text("❌ User not found!")
            del user_states[user_id]
            return
        user_states[user_id] = f"admin_duration_{target}"
        keyboard = [
            [InlineKeyboardButton("7 Days", callback_data=f"premium_duration_7_{target}"), InlineKeyboardButton("30 Days", callback_data=f"premium_duration_30_{target}")],
            [InlineKeyboardButton("90 Days", callback_data=f"premium_duration_90_{target}"), InlineKeyboardButton("♾️ Lifetime", callback_data=f"premium_duration_lifetime_{target}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]
        ]
        await message.reply_text(f"👤 User: `{target}`\n📛 {tuser.get('username', 'N/A')}\n\n⏱️ Select Duration:", reply_markup=InlineKeyboardMarkup(keyboard))
    except ValueError:
        await message.reply_text("❌ Invalid User ID!")
        del user_states[user_id]

@bot.on_callback_query(filters.regex("^premium_duration_"))
async def premium_duration_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
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
    await users_col.update_one({"user_id": target}, {"$set": {"is_premium": True, "premium_expires": expiry}})
    try:
        await bot.send_message(target, f"🎉 **Premium Activated!**\n\n⏱️ **Duration:** {dur_text}\n\n✨ Enjoy unlimited features!")
    except:
        pass
    if callback_query.from_user.id in user_states:
        del user_states[callback_query.from_user.id]
    await callback_query.message.edit_text(f"✅ **Premium Added!**\n\n👤 User: `{target}`\n⏱️ Duration: {dur_text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]]))
    await callback_query.answer("✅ Done!")

@bot.on_callback_query(filters.regex("^admin_premium_list$"))
async def admin_premium_list_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
        return
    pusers = await users_col.find({"is_premium": True}).to_list(None)
    if not pusers:
        text = "📋 **Premium Users**\n\n❌ No premium users"
    else:
        text = f"📋 **Premium Users ({len(pusers)})**\n\n"
        for idx, u in enumerate(pusers[:20], 1):
            exp = u.get("premium_expires")
            exp_text = f"{(exp - datetime.now()).days}d left" if exp else "Lifetime"
            text += f"{idx}. `{u['user_id']}` - {exp_text}\n"
        if len(pusers) > 20:
            text += f"\n...and {len(pusers) - 20} more"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

@bot.on_callback_query(filters.regex("^admin_broadcast$"))
async def admin_broadcast_callback(client, callback_query):
    if not await is_admin(callback_query.from_user.id):
        return
    user_states[callback_query.from_user.id] = "admin_broadcast_message"
    bot_users = await bot_users_col.count_documents({})
    text = f"📢 **Broadcast Message**\n\n👥 Bot Users: {bot_users:,}\n\nSend your message to broadcast:"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

async def admin_broadcast_handler(message: Message):
    user_id = message.from_user.id
    text = message.text
    status = await message.reply_text("📢 **Broadcasting...**\n\n0 sent...")
    ausers = await bot_users_col.find({}).to_list(None)
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
    await status.edit_text(f"✅ **Broadcast Complete!**\n\n✅ Sent: {success}\n❌ Failed: {failed}\n📊 Total: {len(ausers)}")
    del user_states[user_id]
# ==================== TEXT HANDLER ====================

@bot.on_message(filters.text & filters.private & ~filters.command(["start", "connect", "code", "password"]))
async def handle_text(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return
    if isinstance(user_states.get(user_id), dict):
        return
    state = user_states[user_id]
    if state == "waiting_channel":
        await add_channel_process(client, message)
    elif state == "admin_waiting_user_id":
        await admin_add_premium_handler(message)
    elif state == "admin_broadcast_message":
        await admin_broadcast_handler(message)

# ==================== HELP ====================

@bot.on_callback_query(filters.regex("^help$"))
async def help_callback(client, callback_query):
    text = (
        "❓ **Help & Guide**\n\n"
        "**📱 Setup:**\n"
        "1. Get API from https://my.telegram.org\n"
        "2. Use `/connect API_ID API_HASH PHONE`\n"
        "3. Enter OTP with `/code`\n"
        "4. Add channels\n"
        "5. Toggle Live Mode for instant approval\n\n"
        "**⚡ Live Mode:**\n"
        "├ Unlimited instant approvals\n"
        "├ No daily limit\n"
        "└ Toggle per channel\n\n"
        "**💎 Premium:**\n"
        "├ 10 req/sec (pending)\n"
        "├ Unlimited requests\n"
        "└ Unlimited channels\n\n"
        "📞 **Support:** @NeonGhost"
    )
    keyboard = [[InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await callback_query.answer()

# ==================== STARTUP & MAIN ====================

async def restore_sessions():
    try:
        sessions = await sessions_col.find({}).to_list(None)
        logger.info(f"Found {len(sessions)} saved sessions")
        for session in sessions:
            try:
                user_id = session["user_id"]
                user_client = Client(f"user_{user_id}", api_id=session["api_id"], api_hash=session["api_hash"], session_string=session["session_string"], in_memory=True)
                await user_client.start()
                user_clients[user_id] = user_client
                logger.info(f"Restored session for {user_id}")
            except Exception as e:
                logger.error(f"Failed to restore session for {session['user_id']}: {e}")
        logger.info(f"✅ Restored {len(user_clients)} sessions")
    except Exception as e:
        logger.error(f"Error restoring sessions: {e}")

async def restore_active_tasks():
    try:
        active_channels = await channels_col.find({"is_active": True}).to_list(None)
        logger.info(f"Found {len(active_channels)} active channels")
        for ch in active_channels:
            user_id = ch["user_id"]
            chat_id = ch["chat_id"]
            if user_id in user_clients:
                task_key = f"{user_id}_{chat_id}"
                task = asyncio.create_task(auto_approve_task(user_id, chat_id))
                active_tasks[task_key] = task
                logger.info(f"Restored task for {task_key}")
        logger.info(f"✅ Restored {len(active_tasks)} active tasks")
    except Exception as e:
        logger.error(f"Error restoring tasks: {e}")

scheduler.add_job(check_premium_expiry, 'interval', hours=1)
scheduler.add_job(send_premium_reminders, 'interval', hours=6)
scheduler.add_job(reset_daily_limits, 'cron', hour=0, minute=0)
scheduler.add_job(cleanup_inactive_sessions, 'cron', hour=3, minute=0)
scheduler.add_job(memory_cleanup, 'interval', hours=2)

async def main():
    try:
        await mongo_client.admin.command('ping')
        logger.info("✅ MongoDB connected!")
        await create_indexes()
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return
    scheduler.start()
    logger.info("✅ Scheduler started")
    await bot.start()
    me = await bot.get_me()
    logger.info(f"✅ Bot started: @{me.username}")
    await restore_sessions()
    await restore_active_tasks()
    logger.info("🚀 Bot is fully operational!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        bot.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
