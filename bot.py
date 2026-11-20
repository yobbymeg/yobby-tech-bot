"""
Yobby Tech Bot — Telegram config decryption service for Reese Tunnel .RT files.

Flow:
  /start -> channel join gate (@fridahtech) -> "I have joined" check
         -> welcome -> services menu
         -> Reese Tunnel: send .RT file -> password -> decrypted config
         -> Dark Tunnel / HTTP Injector: coming soon + contact admin

Environment:
  BOT_TOKEN      (required) bot token from @BotFather
  JOIN_CHANNEL   (optional) channel to gate, default @fridahtech
  ADMIN_IDS      (optional) comma separated telegram user ids for /broadcast
  WA_LINK        (optional) admin whatsapp link, default https://wa.me/254795314221
"""

import asyncio
import html
import json
import logging
import os
import time

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ehi
import rse5

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
JOIN_CHANNEL = os.environ.get("JOIN_CHANNEL", "@fridahtech")
CHANNEL_URL = "https://t.me/" + JOIN_CHANNEL.lstrip("@")
WA_LINK = os.environ.get("WA_LINK", "https://wa.me/254795314221")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
MAX_FILE_BYTES = 20 * 1024 * 1024

# Admin-only weak-password auto-recovery (see README: "Password recovery").
# This is password RECOVERY for weak/forgotten passwords — not a bypass.
# Strong passwords are cryptographically safe (0.8 s per guess, 2^256 keyspace).
RECOVERY_WORDLIST_PATH = os.environ.get("RECOVERY_WORDLIST_PATH", "")
RECOVERY_MAX_CANDIDATES = int(os.environ.get("RECOVERY_MAX_CANDIDATES", "150"))
RECOVERY_TIME_BUDGET = float(os.environ.get("RECOVERY_TIME_BUDGET", "600"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("yobby")

# --------------------------------------------------------------------------- #
# Lightweight persistence (stats + known users)                               #
# --------------------------------------------------------------------------- #
STATS_PATH = os.environ.get("STATS_PATH", "/tmp/yobby_stats.json")


def _load_stats():
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("files", 0)
            d.setdefault("ok", 0)
            d.setdefault("fail", 0)
            d.setdefault("users", [])
            return d
    except Exception:
        return {"files": 0, "ok": 0, "fail": 0, "users": []}


def _save_stats(stats):
    try:
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(stats, f)
    except Exception:
        pass


STATS = _load_stats()


def remember_user(update: Update):
    u = update.effective_user
    if u and u.id not in STATS["users"]:
        STATS["users"].append(u.id)
        _save_stats(STATS)


# --------------------------------------------------------------------------- #
# Session state (per chat, in memory)                                         #
# --------------------------------------------------------------------------- #
# stage values: "await_file", "await_hwid", "await_password"
SESSIONS = {}


def stage_of(chat_id):
    return SESSIONS.get(chat_id, {})


def set_stage(chat_id, stage, **data):
    SESSIONS[chat_id] = {"stage": stage, "ts": time.time(), **data}


def clear_stage(chat_id):
    SESSIONS.pop(chat_id, None)


# --------------------------------------------------------------------------- #
# Texts and keyboards                                                         #
# --------------------------------------------------------------------------- #
JOIN_TEXT = (
    "🔐 <b>ACCESS LOCKED</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Welcome to <b>Yobby Tech</b> 🤖\n\n"
    "To use this bot you must first join our official channel:\n"
    f"📢 {html.escape(JOIN_CHANNEL)}\n\n"
    "1️⃣ Tap <b>Join Channel</b> below\n"
    "2️⃣ Come back and tap <b>✅ I Have Joined</b>"
)
JOIN_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ I Have Joined", callback_data="join_check")],
    ]
)

WELCOME_TEXT = (
    "👑 <b>WELCOME TO YOBBY TECH</b> 👑\n"
    "<i>— The King of Tech —</i>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "✅ <b>Membership verified!</b>\n\n"
    "Hello <b>{name}</b> 👋\n\n"
    "🧰 <b>Available services</b> — pick one below 👇\n\n"
    "🚇 <b>Reese Tunnel</b> — unlock .RT config files\n"
    "💉 <b>HTTP Injector</b> — unlock .ehi config files\n"
    "🌑 <b>Dark Tunnel</b> — coming soon\n\n"
    "⚡ Fast • Secure • Trusted\n"
    "👇 <i>Use the buttons below</i>"
)
MENU_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🚇 Reese Tunnel", callback_data="svc_reese")],
        [
            InlineKeyboardButton("🌑 Dark Tunnel", callback_data="svc_dark"),
            InlineKeyboardButton("💉 HTTP Injector", callback_data="svc_http"),
        ],
        [
            InlineKeyboardButton("📞 Contact Admin", url=WA_LINK),
            InlineKeyboardButton("ℹ️ About", callback_data="about"),
        ],
        [InlineKeyboardButton("🆘 Help", callback_data="help")],
    ]
)

REESE_TEXT = (
    "🚇 <b>REESE TUNNEL — FILE UNLOCKER</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📤 <b>Send me your <code>.RT</code> file now.</b>\n\n"
    "How it works:\n"
    "1️⃣ Attach the <code>.RT</code> file here (as a document 📎)\n"
    "2️⃣ I will ask for the file <b>password</b> (if it has one)\n"
    "3️⃣ I unlock it and show you the full config 🔓\n\n"
    "⚠️ <i>Device-locked files also need the HWID they were made for.</i>"
)

HTTP_TEXT = (
    "💉 <b>HTTP INJECTOR — FILE UNLOCKER</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📤 <b>Send me your <code>.ehi</code> file now.</b>\n\n"
    "How it works:\n"
    "1️⃣ Attach the <code>.ehi</code> file here (as a document 📎)\n"
    "2️⃣ I decrypt it <b>instantly — no password needed</b> ⚡\n"
    "3️⃣ You get the full config + JSON file 🔓\n\n"
    "✅ Supports HTTP Injector v5/v6 encrypted configs\n"
    "🔒 <i>Locked files show lock/expiry info automatically.</i>"
)

SOON_TEXT = (
    "🚧 <b>COMING SOON</b> 🚧\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "{svc} support is not available yet.\n\n"
    "Our engineers are working on it ⚙️\n"
    "Stay tuned on the channel 📢\n\n"
    "📞 Need it urgently? Contact the admin 👇"
)
SOON_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")],
    ]
)

ABOUT_TEXT = (
    "ℹ️ <b>ABOUT YOBBY TECH</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "👑 <i>The King of Tech</i>\n\n"
    "🤖 Official bot for config services\n"
    "🔓 Reese Tunnel <code>.RT</code> decryption\n"
    "🌍 Tunnels: SSH • SlowDNS • UDP • V2Ray\n"
    "📢 Channel: {chan}\n"
    "📞 Admin: WhatsApp below\n\n"
    "⚡ <i>Fast. Secure. Trusted.</i>"
)

HELP_TEXT = (
    "🆘 <b>HOW TO USE THIS BOT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🚇 <b>Reese Tunnel</b>\n"
    "   1. Tap 🚇 Reese Tunnel\n"
    "   2. Send your <code>.RT</code> file 📎\n"
    "   3. Reply with the file password 🔑\n"
    "   4. Get the unlocked config 🔓\n\n"
    "💉 <b>HTTP Injector</b>\n"
    "   1. Tap 💉 HTTP Injector\n"
    "   2. Send your <code>.ehi</code> file 📎\n"
    "   3. Decrypted instantly — no password needed ⚡\n\n"
    "📜 <b>Commands</b>\n"
    "/menu — main menu\n"
    "/help — this guide\n"
    "/about — about Yobby Tech\n"
    "/contact — talk to the admin\n"
    "/cancel — cancel current action\n"
    "/stats — bot statistics\n"
    "/version — check which build is live\n\n"
    "⚠️ <b>Notes</b>\n"
    "• Device-locked (HWID) files need that device's HWID\n"
    "• Wrong password = decryption fails\n"
    "• Files are processed and never stored 🔒"
)

PROCESSING_TEXT = "⏳ <b>Decrypting…</b> this takes a few seconds.\nPlease wait 🔒"

# Bump this when features change — send /version to the live bot to check it.
BOT_VERSION = "2.0.0-ehi"
VERSION_TEXT = (
    "🏷 <b>BOT VERSION</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    f"Version: <code>{BOT_VERSION}</code>\n\n"
    "🚇 Reese Tunnel <code>.RT</code> — ✅ active\n"
    "💉 HTTP Injector <code>.ehi</code> — ✅ active (instant, no password)\n"
    "🌑 Dark Tunnel — 🚧 coming soon\n\n"
    "<i>If this message shows 2.0.0-ehi, the live bot is the latest build.\n"
    "If Telegram replies “unknown command”, the host is still running the old build — trigger a redeploy.</i>"
)

NOT_MEMBER_TEXT = (
    "⛔ <b>NOT VERIFIED YET</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "I checked and you have <b>not joined</b> the channel yet 🤖\n\n"
    "👉 Join {chan} first, then tap <b>✅ I Have Joined</b> again."
)
SERVICE_DOWN_TEXT = (
    "⚠️ <b>Verification service is not ready.</b>\n"
    "The admin needs to add this bot to {chan}.\n"
    "Please try again later or contact support."
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
async def check_membership(bot, user_id):
    """True = joined, False = not joined, None = check unavailable."""
    try:
        member = await bot.get_chat_member(JOIN_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except BadRequest as e:
        msg = str(e).lower()
        if "member not found" in msg or "user not found" in msg:
            return False
        log.error("join check failed (%s): %s", JOIN_CHANNEL, e)
        return None
    except TelegramError as e:
        log.error("join check error: %s", e)
        return None


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    clear_stage(update.effective_chat.id)
    await update.effective_message.reply_text(
        WELCOME_TEXT.format(name=html.escape(user.first_name)),
        reply_markup=MENU_KB,
        parse_mode=ParseMode.HTML,
    )


def fail_closed(update: Update):
    update.effective_message.reply_text(
        SERVICE_DOWN_TEXT.format(chan=html.escape(JOIN_CHANNEL)),
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    joined = await check_membership(context.bot, update.effective_user.id)
    if joined is None:
        fail_closed(update)
        return
    if joined:
        await send_welcome(update, context)
    else:
        await update.effective_message.reply_text(
            JOIN_TEXT, reply_markup=JOIN_KB, parse_mode=ParseMode.HTML
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    await send_welcome(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")],
        ]
    )
    await update.effective_message.reply_text(
        ABOUT_TEXT.format(chan=html.escape(JOIN_CHANNEL)),
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 WhatsApp Admin", url=WA_LINK)]])
    await update.effective_message.reply_text(
        "📞 <b>CONTACT ADMIN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Tap the button below to chat with us on WhatsApp 👇",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_stage(update.effective_chat.id)
    await update.effective_message.reply_text(
        "❌ <b>Cancelled.</b> Back to the main menu 👇",
        reply_markup=MENU_KB,
        parse_mode=ParseMode.HTML,
    )


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(VERSION_TEXT, parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        s = STATS
        await update.effective_message.reply_text(
            f"📊 <b>BOT STATS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 Files received: <b>{s['files']}</b>\n"
            f"🔓 Unlocked: <b>{s['ok']}</b>\n"
            f"⛔ Failed: <b>{s['fail']}</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    uptime_h = (time.time() - _BOOT) / 3600
    await update.effective_message.reply_text(
        f"📊 <b>ADMIN STATS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users seen: <b>{len(STATS['users'])}</b>\n"
        f"📥 Files received: <b>{STATS['files']}</b>\n"
        f"🔓 Unlocked: <b>{STATS['ok']}</b>\n"
        f"⛔ Failed: <b>{STATS['fail']}</b>\n"
        f"⏱ Uptime: <b>{uptime_h:.1f} h</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.effective_message.reply_text(
            "Usage: /broadcast your message here")
        return
    sent = 0
    for uid in list(STATS["users"]):
        try:
            await context.bot.send_message(uid, f"📢 <b>YOBBY TECH</b>\n\n{html.escape(text)}",
                                           parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except (Forbidden, BadRequest):
            pass
        except TelegramError:
            pass
    await update.effective_message.reply_text(f"✅ Broadcast delivered to {sent} users.")


_BOOT = time.time()

# --------------------------------------------------------------------------- #
# Callback buttons                                                            #
# --------------------------------------------------------------------------- #
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    remember_user(update)
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    if data == "join_check":
        joined = await check_membership(context.bot, update.effective_user.id)
        if joined is None:
            await query.edit_message_text(
                SERVICE_DOWN_TEXT.format(chan=html.escape(JOIN_CHANNEL)),
                parse_mode=ParseMode.HTML,
            )
            return
        if joined:
            # replace the join screen with the welcome card
            user = update.effective_user
            clear_stage(chat_id)
            await query.edit_message_text(
                WELCOME_TEXT.format(name=html.escape(user.first_name)),
                reply_markup=MENU_KB,
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.answer(
                "⛔ You have NOT joined yet — join first, then tap again!",
                show_alert=True,
            )

    elif data == "svc_reese":
        set_stage(chat_id, "await_file")
        await query.edit_message_text(
            REESE_TEXT,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "svc_http":
        set_stage(chat_id, "await_file")
        await query.edit_message_text(
            HTTP_TEXT,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "svc_dark":
        await query.edit_message_text(
            SOON_TEXT.format(svc="🌑 Dark Tunnel"),
            reply_markup=SOON_KB,
            parse_mode=ParseMode.HTML,
        )

    elif data == "about":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
                [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")],
            ]
        )
        await query.edit_message_text(
            ABOUT_TEXT.format(chan=html.escape(JOIN_CHANNEL)),
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    elif data == "help":
        await query.edit_message_text(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "recover_run":
        st = stage_of(chat_id)
        if update.effective_user.id not in ADMIN_IDS:
            await query.answer("⛔ Admin only.", show_alert=True)
            return
        if st.get("stage") != "await_password" or not st.get("data"):
            await query.answer("No pending file — send one first.", show_alert=True)
            return
        if st.get("recovering"):
            await query.answer("Recovery is already running…", show_alert=True)
            return
        st["recovering"] = True
        candidates = load_wordlist(st.get("fname", ""))
        await query.edit_message_text(
            f"🧠 <b>AUTO-RECOVERY RUNNING</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Testing <b>{len(candidates)}</b> candidate passwords…\n"
            f"⏳ ~1-2 s per candidate — can take several minutes.\n\n"
            f"🔓 <i>Recovers only weak/common passwords. "
            f"Strong passwords cannot be bypassed.</i>",
            parse_mode=ParseMode.HTML,
        )
        deadline = time.time() + RECOVERY_TIME_BUDGET
        found, tried, elapsed = await asyncio.to_thread(
            try_recover, st["data"], candidates, st.get("hwid", b""), deadline
        )
        st["recovering"] = False
        if found:
            await update.effective_message.reply_text(
                f"🔓 <b>PASSWORD RECOVERED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔑 Password: <code>{html.escape(found)}</code>\n"
                f"✅ {tried} candidates tried in {elapsed:.0f} s\n\n"
                f"Unlocking now…",
                parse_mode=ParseMode.HTML,
            )
            await run_decrypt(update, context, st["data"], found.encode(),
                              st.get("hwid", b""))
        else:
            set_stage(chat_id, "await_password", data=st["data"],
                      hwid=st.get("hwid", b""), fname=st.get("fname", ""))
            await update.effective_message.reply_text(
                f"❌ <b>NO MATCH FOUND</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Tested {tried} candidates in {elapsed:.0f} s — the password is "
                f"not weak enough to appear in the list.\n\n"
                f"🔐 Strong passwords cannot be bypassed — ask the file owner "
                f"or contact the admin 👇",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔁 Try Again", callback_data="recover_run")],
                        [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
                        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )

    elif data == "back_menu":
        user = update.effective_user
        clear_stage(chat_id)
        await query.edit_message_text(
            WELCOME_TEXT.format(name=html.escape(user.first_name)),
            reply_markup=MENU_KB,
            parse_mode=ParseMode.HTML,
        )


# --------------------------------------------------------------------------- #
# Reese Tunnel file flow                                                      #
# --------------------------------------------------------------------------- #
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    chat_id = update.effective_chat.id
    st = stage_of(chat_id)

    if st.get("stage") != "await_file":
        await update.effective_message.reply_text(
            "🤖 To unlock a config file, open the menu and tap "
            "<b>🚇 Reese Tunnel</b> or <b>💉 HTTP Injector</b> first 👇",
            reply_markup=MENU_KB,
            parse_mode=ParseMode.HTML,
        )
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await update.effective_message.reply_text("⛔ File too large (max 20 MB).")
        return

    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except TelegramError as e:
        log.error("download failed: %s", e)
        await update.effective_message.reply_text("⚠️ Could not download the file. Try again.")
        return

    # auto-detect the format: HTTP Injector (.ehi) or Reese Tunnel (.RT)
    try:
        ehi.parse_header(data)
        return await run_decrypt_ehi(update, context, data)
    except ValueError:
        pass

    try:
        flags, salt, nonce, ct, tag = rse5.parse_header(data)
    except ValueError as e:
        STATS["files"] += 1
        STATS["fail"] += 1
        _save_stats(STATS)
        await update.effective_message.reply_text(
            f"❌ <b>Not a valid config file.</b>\n\n"
            f"I unlock <code>.RT</code> (Reese Tunnel) 🚇 and <code>.ehi</code> "
            f"(HTTP Injector) 💉 files — send one of those 📎\n\n"
            f"<code>{html.escape(str(e))}</code>",
            reply_markup=MENU_KB,
            parse_mode=ParseMode.HTML,
        )
        clear_stage(chat_id)
        return

    STATS["files"] += 1
    _save_stats(STATS)

    if flags & 1 and flags & 2:
        set_stage(chat_id, "await_hwid", data=data)
        await update.effective_message.reply_text(
            "🔐 This file is <b>device-locked AND password-protected</b>.\n\n"
            "1️⃣ Send the <b>HWID</b> of the device it was made for\n"
            "2️⃣ Then send the <b>password</b>",
            parse_mode=ParseMode.HTML,
        )
    elif flags & 1:
        set_stage(chat_id, "await_hwid", data=data)
        await update.effective_message.reply_text(
            "🔐 This file is <b>locked to a device (HWID)</b>.\n\n"
            "Send the <b>HWID</b> of the device it was made for 👇",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.HTML,
        )
    elif flags & 2:
        set_stage(chat_id, "await_password", data=data, hwid=b"", fname=doc.file_name or "")
        if update.effective_user.id in ADMIN_IDS:
            await update.effective_message.reply_text(
                "🔑 This file is <b>password-protected</b>.\n\n"
                "Send the <b>password</b> now 👇\n\n"
                "🧠 <i>Admin tip:</i> or tap Auto-recovery to try common/weak passwords.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(
                            "🧠 Auto-recovery (admin)", callback_data="recover_run")],
                        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                "🔑 This file is <b>password-protected</b>.\n\n"
                "Send the <b>password</b> now 👇",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.HTML,
            )
    else:
        set_stage(chat_id, "await_password", data=data, hwid=b"")
        await run_decrypt(update, context, data, b"", b"")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    chat_id = update.effective_chat.id
    st = stage_of(chat_id)
    text = (update.message.text or "").strip()

    if st.get("stage") == "await_hwid":
        st["hwid"] = text.encode()
        if st.get("data") and st["hwid"]:
            # check whether password is also needed
            flags, *_ = rse5.parse_header(st["data"])
            if flags & 2:
                st["stage"] = "await_password"
                await update.effective_message.reply_text(
                    "✅ HWID saved.\n🔑 Now send the <b>password</b> 👇",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.HTML,
                )
                return
            await run_decrypt(update, context, st["data"], b"", st["hwid"])
        return

    if st.get("stage") == "await_password":
        await run_decrypt(update, context, st.get("data", b""), text.encode(),
                          st.get("hwid", b""))
        return

    # any other text
    await update.effective_message.reply_text(
        "🤖 I didn't understand that.\nUse the menu below 👇 or /help",
        reply_markup=MENU_KB,
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# Admin-only weak-password auto-recovery                                      #
# --------------------------------------------------------------------------- #
# Tries common passwords + PINs + filename-derived guesses, cheapest first.
# ~0.8 s per candidate (600k HMAC chain) -> the full default list takes ~2 min.
RECOVERY_PIN_GUESSES = [
    "12345", "1234", "123456", "0000", "00000", "1111", "1212", "123123",
    "112233", "654321", "000000", "2580", "1590",
]

DEFAULT_WORDLIST = [
    "vip", "vip2", "vip123", "free", "free123", "fre3", "admin", "admin123",
    "adm1n", "password", "password123", "passw0rd", "pass", "pass123",
    "unlock", "unlocked", "config", "configs", "tunnel", "tunnels", "reese",
    "reese123", "reesevpn", "dark", "darktunnel", "http", "httpinjector",
    "injector", "ssh", "slowdns", "v2ray", "udp", "payload", "sni", "bughost",
    "bug", "host", "proxy", "server", "data", "bundle", "bundles", "unlimited",
    "hack", "crack", "premium", "trial", "test", "test123", "demo", "client",
    "shared", "public", "locked", "key", "secret", "master", "root",
    "2020", "2021", "2022", "2023", "2024", "2025", "2026",
    "kenya", "nairobi", "kenyatech", "tech", "king", "kingoftech", "yobby",
    "yobbytech", "yobby123", "fridah", "fridahtech", "fridah123", "telkom",
    "telkomkenya", "faiba", "safaricom", "saf", "airtel", "equitel", "mtn",
    "glo", "airteltigo", "vodacom", "vodafone", "jazz", "etisalat", "9mobile",
    "mtnug", "vodacomtz", "orange", "wap", "netify", "netifyvpn", "starks",
    "starkvpn", "anony", "anonytun", "turbo", "turbovpn", "super", "mega",
    "magic", "speednet", "fastnet", "netfree", "internet", "chapa", "haraka",
    "freemium", "freebies", "datafree", "offer", "promo", "bonus", "money",
    "cash", "like", "share", "subscribe", "join", "channel", "welcome",
]


def filename_candidates(filename: str):
    """Sellers often name the file after the password or ISP."""
    if not filename:
        return []
    stem = filename.rsplit(".", 1)[0].strip()
    if not stem:
        return []
    return [
        stem, stem.lower(), stem.replace(" ", ""), stem.replace(" ", "").lower(),
        stem.replace(" ", "_"), stem + "123", stem.replace(" ", "") + "123",
        stem.replace(" ", "") + "2024",
    ]


def load_wordlist(filename: str = ""):
    """Custom list (RECOVERY_WORDLIST_PATH) -> fallback default list, PINs +
    filename-derived candidates first (highest hit probability)."""
    words = []
    if RECOVERY_WORDLIST_PATH:
        try:
            with open(RECOVERY_WORDLIST_PATH, "r", encoding="utf-8",
                      errors="ignore") as f:
                words = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            log.warning("RECOVERY_WORDLIST_PATH unreadable: %s", RECOVERY_WORDLIST_PATH)
    if not words:
        words = DEFAULT_WORDLIST
    candidates = RECOVERY_PIN_GUESSES + filename_candidates(filename) + words
    seen, out = set(), []
    for w in candidates:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:RECOVERY_MAX_CANDIDATES]


def try_recover(data: bytes, candidates, hwid: bytes = b"", deadline=None):
    """Sequential dictionary attack. Returns (password|None, tried, seconds)."""
    flags, salt, nonce, ct, tag = rse5.parse_header(data)
    started = time.time()
    tried = 0
    for cand in candidates:
        pw = cand.encode("utf-8", "ignore")
        key = rse5.kdf(rse5.build_material(flags, hwid, pw), salt)
        tried += 1
        if rse5.compute_tag(key, nonce, data[:34], ct) == tag:
            return cand, tried, time.time() - started
        if deadline and time.time() > deadline:
            break
    return None, tried, time.time() - started


# --------------------------------------------------------------------------- #
# Decryption                                                                  #
# --------------------------------------------------------------------------- #
def _fmt_bool(v):
    return "✅ Yes" if v else "❌ No"


def _summarize(envelope: dict) -> str:
    """Premium unlock card, arranged exactly:
    Host server -> Port -> Username -> Password -> Payload -> Proxy -> SNI
    -> more settings, then the JSON file is sent LAST.
    Uses the REAL Reese Tunnel envelope schema (verified on production files):
    meta.{name,author,createdAt,expiresAt,locked,lockAll,hwid,...} +
    config.{profileName,sshHost,sshPort,username,password,payload,sni,...}."""
    meta = envelope.get("meta") if isinstance(envelope, dict) else {}
    cfg = envelope.get("config") if isinstance(envelope, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    NA = "—"

    def val(key, cap=480):
        v = cfg.get(key)
        if v in (None, "", False):
            return NA
        s = str(v)
        if len(s) > cap:
            s = s[:cap] + f"… (+{len(s) - cap} chars — full in JSON file)"
        return html.escape(s)

    lines = [
        "╔═══════════════════════════════╗",
        "║   🔓 <b>CONFIG UNLOCKED — PREMIUM</b>   ║",
        "╚═══════════════════════════════╝",
        "",
    ]

    name = meta.get("name") or cfg.get("profileName")
    if name:
        lines.append(f"📛 <b>{html.escape(str(name)[:60])}</b>")
    author = meta.get("author")
    if author:
        lines.append(f"👑 Author: {html.escape(str(author))}")
    lines.append("")

    # ---- main settings, in the exact order requested ----
    lines += [
        "🌐 <b>HOST SERVER</b>",
        f"<code>{val('sshHost')}</code>",
        "",
        "🔌 <b>PORT</b>",
        f"<code>{val('sshPort')}</code>",
        "",
        "👤 <b>USERNAME</b>",
        f"<code>{val('username')}</code>",
        "",
        "🔑 <b>PASSWORD</b>",
        f"<code>{val('password')}</code>",
        "",
        "📦 <b>PAYLOAD</b>",
        f"<code>{val('payload')}</code>",
        "",
        "🛡 <b>PROXY</b>",
    ]
    if cfg.get("proxyHost") and cfg.get("proxyPort"):
        lines.append(
            f"<code>{html.escape(str(cfg['proxyHost']))}:{html.escape(str(cfg['proxyPort']))}</code>"
        )
    else:
        lines.append(f"<code>{val('proxyHost') if cfg.get('proxyHost') else val('proxyPort')}</code>")
    lines += [
        "",
        "🎯 <b>SNI</b>",
        f"<code>{val('sni')}</code>",
    ]

    # ---- "and more" ----
    lines += ["", "━━━━━━ ⚙️ <b>MORE SETTINGS</b> ━━━━━━"]
    more = []
    if cfg.get("tunnelMode"):
        more.append(f"🧭 Tunnel mode · <code>{html.escape(str(cfg['tunnelMode']))}</code>")
    if cfg.get("sshUdpGatewayPort"):
        more.append(f"🚪 UDP gateway · <code>{html.escape(str(cfg['sshUdpGatewayPort']))}</code>")
    dns = " → ".join(str(d) for d in (cfg.get("dnsPrimary"), cfg.get("dnsSecondary")) if d)
    if dns:
        more.append(f"🧬 DNS · <code>{html.escape(dns)}</code>")
    if cfg.get("udpCustomHost"):
        more.append(f"🔌 UDP host · <code>{html.escape(str(cfg['udpCustomHost']))}</code>")
    for k, lbl in (
        ("v2rayMode", "🛰 V2Ray mode"),
        ("slowDns", "🐢 SlowDNS tunnel"),
        ("udpCustom", "🔌 UDP custom mode"),
        ("enhanced", "⚡ Enhanced mode"),
        ("useSni", "🎯 SNI enabled"),
        ("usePayload", "📦 Payload enabled"),
        ("useRemoteProxy", "🛡 Remote proxy"),
        ("autoReconnect", "🔄 Auto-reconnect"),
    ):
        if cfg.get(k):
            more.append(lbl)
    lines += more if more else [NA]

    # ---- security / licence ----
    lines += ["", "🛠 <b>SECURITY &amp; LICENCE</b>"]
    if meta.get("locked"):
        locks = []
        if meta.get("lockAll"):
            locks.append("all settings")
        if meta.get("lockPayloadUnlockServer"):
            locks.append("payload/server")
        if meta.get("unlockUserPass"):
            locks.append("user/pass")
        if meta.get("lockRemoteProxy"):
            locks.append("proxy")
        if meta.get("blockRoot"):
            locks.append("root blocked")
        lines.append(f"🔒 Locks · {html.escape(', '.join(locks) or 'enabled')}")
    hw = meta.get("hwid")
    lines.append(
        f"📱 HWID · <code>{html.escape(str(hw))}</code>"
        if hw else "📱 HWID · not locked"
    )
    exp = meta.get("expiresAt")
    if isinstance(exp, (int, float)) and exp > 0:
        try:
            lines.append(f"⏳ Expiry · {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp / 1000))}")
        except (OSError, OverflowError, ValueError):
            lines.append("⏳ Expiry · " + html.escape(str(exp)))
    else:
        lines.append("⏳ Expiry · ♾ no expiry (unlimited)")
    created = meta.get("createdAt")
    if isinstance(created, (int, float)) and created > 0:
        try:
            lines.append(f"🗓 Created · {time.strftime('%Y-%m-%d', time.localtime(created / 1000))}")
        except (OSError, OverflowError, ValueError):
            pass

    lines += [
        "",
        "📄 <b>Full JSON config</b> is attached below 👇",
        "╭──────────────────────────────╮",
        "⚡ <b>YOBBY TECH — THE KING OF TECH</b> 👑",
        "╰──────────────────────────────╯",
    ]
    return "\n".join(lines)


async def run_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      data: bytes, password: bytes, hwid: bytes):
    chat_id = update.effective_chat.id
    clear_stage(chat_id)
    notice = await update.effective_message.reply_text(PROCESSING_TEXT, parse_mode=ParseMode.HTML)

    try:
        result = await asyncio.to_thread(rse5.decrypt, data, password, hwid)
    except ValueError as e:
        STATS["fail"] += 1
        _save_stats(STATS)
        reason = str(e)
        msg = (
            "❌ <b>WRONG PASSWORD!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "The decryption failed 🔐\n\n"
            "👉 Check the password and <b>send it again</b>, or tap 🚇 Reese Tunnel to start over."
            if reason == "wrong password"
            else "❌ <b>UNLOCK FAILED</b>\n<code>" + html.escape(reason) + "</code>\n\n"
            "If this file is device-locked, it only opens with the right HWID."
        )
        await notice.edit_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Try Again", callback_data="svc_reese")],
                    [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="back_menu")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:  # unexpected
        STATS["fail"] += 1
        _save_stats(STATS)
        log.exception("decrypt error")
        await notice.edit_text(
            "❌ <b>Unexpected error.</b> Please contact the admin.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    STATS["ok"] += 1
    _save_stats(STATS)

    envelope = result["envelope"]
    await notice.edit_text(_summarize(envelope), parse_mode=ParseMode.HTML)

    # send the full JSON as a file
    payload = result["json"].encode("utf-8")
    name = "config"
    if isinstance(envelope, dict):
        meta = envelope.get("meta") or {}
        if isinstance(meta, dict) and meta.get("name"):
            name = str(meta["name"])[:40]
    fname = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ").strip() or "config"
    await update.effective_message.reply_document(
        document=payload,
        filename=f"{fname}.json",
        caption="📄 <b>Full decrypted config</b> — keep it private 🔒",
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        "✅ <b>Done!</b> Unlock another? 👇",
        reply_markup=MENU_KB,
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# HTTP Injector (.ehi) flow                                                   #
# --------------------------------------------------------------------------- #
def _summarize_ehi(res: dict) -> str:
    """Premium unlock card for .ehi configs — same layout as the .RT card:
    Host server -> Port -> Username -> Password -> Payload -> Proxy -> SNI
    -> more settings, then the JSON file is sent LAST."""
    n = res["normalized"]
    header = res["header"]
    NA = "—"

    def val(key, cap=480):
        v = n.get(key) or ""
        if not v:
            return NA
        s = str(v)
        if len(s) > cap:
            s = s[:cap] + f"… (+{len(s) - cap} chars — full in JSON file)"
        return html.escape(s)

    lines = [
        "╔═══════════════════════════════╗",
        "║  💉 <b>EHI UNLOCKED — PREMIUM</b>  ║",
        "╚═══════════════════════════════╝",
        "",
    ]
    if n.get("configMessage"):
        lines.append(f"📝 <b>Note:</b> {html.escape(str(n['configMessage'])[:120])}")
    app_ver = header.get("app_version")
    if app_ver:
        lines.append(f"⚙️ Made with HTTP Injector <code>{html.escape(str(app_ver))}</code>")
    lines.append(f"🧬 Scheme: <code>{html.escape(str(res['scheme']))}</code>")
    lines.append("")

    # ---- main settings, same order as the .RT card ----
    lines += [
        "🌐 <b>HOST SERVER</b>",
        f"<code>{val('sshServer')}</code>",
        "",
        "🔌 <b>PORT</b>",
        f"<code>{val('sshPort')}</code>",
        "",
        "👤 <b>USERNAME</b>",
        f"<code>{val('sshUser')}</code>",
        "",
        "🔑 <b>PASSWORD</b>",
        f"<code>{val('sshPassword')}</code>",
        "",
        "📦 <b>PAYLOAD</b>",
        f"<code>{val('payload')}</code>",
        "",
        "🛡 <b>PROXY</b>",
    ]
    proxy = val("remoteProxy")
    if proxy == NA:
        proxy = val("proxyURL")
    if proxy != NA and n.get("remoteProxyPort") and ":" not in str(n.get("remoteProxy") or ""):
        proxy = f"{proxy}:{html.escape(str(n['remoteProxyPort']))}"
    lines.append(f"<code>{proxy}</code>")
    lines += [
        "",
        "🎯 <b>SNI</b>",
        f"<code>{val('sniHostname')}</code>",
    ]

    # ---- "and more" ----
    lines += ["", "━━━━━━ ⚙️ <b>MORE SETTINGS</b> ━━━━━━"]
    more = []
    if n.get("connectionType"):
        more.append(f"🧭 Connection · <code>{html.escape(str(n['connectionType']))}</code>")
    if n.get("ssl"):
        more.append("🔐 SSL/TLS enabled")
    dns = " → ".join(str(d) for d in (n.get("customDns1"), n.get("customDns2")) if d)
    if dns:
        more.append(f"🧬 DNS · <code>{html.escape(dns)}</code>")
    v2r = n.get("v2ray")
    if isinstance(v2r, dict) and v2r:
        addr = v2r.get("address") or v2r.get("add") or NA
        port = v2r.get("port") or NA
        more.append(f"🛰 V2Ray · <code>{html.escape(str(addr))}:{html.escape(str(port))}</code>")
    if isinstance(n.get("overwriteServerData"), dict) and n["overwriteServerData"]:
        osd = n["overwriteServerData"]
        more.append(f"🌐 Server override · <code>{html.escape(str(osd.get('sshServer') or osd.get('host') or 'present'))}</code>")
    lines += more if more else [NA]

    # ---- security / licence ----
    lines += ["", "🛠 <b>SECURITY &amp; LICENCE</b>"]
    locks = []
    raw_locks = n.get("lockModes") or ""
    if raw_locks:
        try:
            parsed = json.loads(raw_locks)
            locks = [str(x) for x in parsed] if isinstance(parsed, list) else [raw_locks]
        except (json.JSONDecodeError, TypeError):
            locks = [raw_locks]
    if locks and locks != ["none"]:
        lines.append(f"🔒 Locks · {html.escape(', '.join(locks))}")
    else:
        lines.append("🔓 Locks · none")
    hw = n.get("configHwid")
    lines.append(
        f"📱 HWID · <code>{html.escape(str(hw))}</code>"
        if hw else "📱 HWID · not locked"
    )
    op = n.get("configLockMobileOperatorId")
    if op:
        lines.append(f"📶 Operator lock · <code>{html.escape(str(op))}</code>")
    exp = n.get("configExpiryTimestamp")
    if isinstance(exp, (int, float)) and exp > 0:
        try:
            lines.append(f"⏳ Expiry · {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp / 1000))}")
        except (OSError, OverflowError, ValueError):
            lines.append(f"⏳ Expiry · {html.escape(str(exp))}")
    else:
        lines.append("⏳ Expiry · ♾ no expiry (unlimited)")

    lines += [
        "",
        "📄 <b>Full JSON config</b> is attached below 👇",
        "╭──────────────────────────────╮",
        "⚡ <b>YOBBY TECH — THE KING OF TECH</b> 👑",
        "╰──────────────────────────────╯",
    ]
    return "\n".join(lines)


async def run_decrypt_ehi(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          data: bytes):
    """HTTP Injector files need no password — decrypt instantly."""
    chat_id = update.effective_chat.id
    clear_stage(chat_id)
    notice = await update.effective_message.reply_text(PROCESSING_TEXT, parse_mode=ParseMode.HTML)

    try:
        res = await asyncio.to_thread(ehi.decrypt, data)
    except ValueError as e:
        STATS["fail"] += 1
        _save_stats(STATS)
        await notice.edit_text(
            "❌ <b>EHI UNLOCK FAILED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            "This may be an unsupported HTTP Injector variant — contact the admin 👇",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Try Again", callback_data="svc_http")],
                    [InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="back_menu")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception:
        STATS["fail"] += 1
        _save_stats(STATS)
        log.exception("ehi decrypt error")
        await notice.edit_text(
            "❌ <b>Unexpected error.</b> Please contact the admin.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📞 Contact Admin", url=WA_LINK)]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    STATS["ok"] += 1
    _save_stats(STATS)

    await notice.edit_text(_summarize_ehi(res), parse_mode=ParseMode.HTML)

    # send the full JSON as a file
    payload = res["json"].encode("utf-8")
    name = (res["normalized"].get("sniHostname")
            or res["normalized"].get("connectionType") or "ehi-config")
    fname = "".join(ch for ch in str(name)[:40] if ch.isalnum() or ch in "-_ ").strip() or "ehi-config"
    await update.effective_message.reply_document(
        document=payload,
        filename=f"{fname}.json",
        caption="📄 <b>Full decrypted .ehi config</b> — keep it private 🔒",
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        "✅ <b>Done!</b> Unlock another? 👇",
        reply_markup=MENU_KB,
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is missing")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Yobby Tech bot starting (channel gate: %s)", JOIN_CHANNEL)
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
