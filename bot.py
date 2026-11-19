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
    "🌑 <b>Dark Tunnel</b> — coming soon\n"
    "💉 <b>HTTP Injector</b> — coming soon\n\n"
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
    "📜 <b>Commands</b>\n"
    "/menu — main menu\n"
    "/help — this guide\n"
    "/about — about Yobby Tech\n"
    "/contact — talk to the admin\n"
    "/cancel — cancel current action\n"
    "/stats — bot statistics\n\n"
    "⚠️ <b>Notes</b>\n"
    "• Device-locked (HWID) files need that device's HWID\n"
    "• Wrong password = decryption fails\n"
    "• Files are processed and never stored 🔒"
)

PROCESSING_TEXT = "⏳ <b>Decrypting…</b> this takes a few seconds.\nPlease wait 🔒"

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

    elif data in ("svc_dark", "svc_http"):
        name = "🌑 Dark Tunnel" if data == "svc_dark" else "💉 HTTP Injector"
        await query.edit_message_text(
            SOON_TEXT.format(svc=name), reply_markup=SOON_KB, parse_mode=ParseMode.HTML
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
            "<b>🚇 Reese Tunnel</b> first 👇",
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

    try:
        flags, salt, nonce, ct, tag = rse5.parse_header(data)
    except ValueError as e:
        STATS["files"] += 1
        STATS["fail"] += 1
        _save_stats(STATS)
        await update.effective_message.reply_text(
            f"❌ <b>Not a valid Reese Tunnel file.</b>\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            "Send a proper <code>.RT</code> file 📎 or tap ⬅️ Menu.",
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
        set_stage(chat_id, "await_password", data=data, hwid=b"")
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
# Decryption                                                                  #
# --------------------------------------------------------------------------- #
def _fmt_bool(v):
    return "✅ Yes" if v else "❌ No"


def _summarize(envelope: dict) -> str:
    """Best-effort human summary of the decrypted envelope."""
    meta = envelope.get("meta") if isinstance(envelope, dict) else {}
    cfg = envelope.get("config") if isinstance(envelope, dict) else {}
    lines = ["🔓 <b>FILE UNLOCKED SUCCESSFULLY!</b>", "━━━━━━━━━━━━━━━━━━━━"]

    if isinstance(meta, dict):
        name = meta.get("name") or meta.get("configName")
        if name:
            lines.append(f"📛 <b>Name:</b> {html.escape(str(name))}")
        if meta.get("creator") or meta.get("creatorName"):
            lines.append(f"👤 <b>Creator:</b> {html.escape(str(meta.get('creator') or meta.get('creatorName')))}")
        exp = meta.get("expiry") or meta.get("expiryMillis")
        if exp:
            try:
                exp_v = int(exp)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_v / 1000)) \
                    if exp_v > 10**12 else time.strftime("%Y-%m-%d", time.localtime(exp_v))
                lines.append(f"⏳ <b>Expiry:</b> {ts}")
            except (TypeError, ValueError):
                lines.append(f"⏳ <b>Expiry:</b> {html.escape(str(exp))}")
        hw = meta.get("hwid") or meta.get("lockedHwid")
        if hw:
            lines.append(f"📱 <b>HWID lock:</b> <code>{html.escape(str(hw))}</code>")

    if isinstance(cfg, dict):
        for key, label in (
            ("mode", "🧭 Mode"), ("protocol", "🧭 Protocol"), ("host", "🌐 Host"),
            ("server", "🌐 Server"), ("port", "🔌 Port"), ("payload", "📦 Payload"),
            ("sni", "🎯 SNI"), ("proxy", "🛡 Proxy"), ("username", "👤 User"),
            ("uuid", "🆔 UUID"),
        ):
            v = cfg.get(key)
            if v not in (None, ""):
                s = html.escape(str(v))
                if len(s) > 300:
                    s = s[:300] + "…"
                lines.append(f"{label}: <code>{s}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📄 Full config JSON is attached below 👇")
    lines.append("⚡ <i>Decrypted by Yobby Tech — The King of Tech 👑</i>")
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
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Yobby Tech bot starting (channel gate: %s)", JOIN_CHANNEL)
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
