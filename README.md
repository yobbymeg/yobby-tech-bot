# Yobby Tech Bot 👑

Telegram bot for **Yobby Tech — The King of Tech**. Channel-gated services bot with
Reese Tunnel `.RT` config decryption built in.

## Features

- 🔐 **Channel join gate** — users must join [@fridahtech](https://t.me/fridahtech) first
- 👑 Welcome card with services menu
- 🚇 **Reese Tunnel** — send a `.RT` file + password, get the full decrypted config
- 🧠 **Auto-recovery (admin)** — dictionary/PIN recovery of *weak* forgotten passwords
- 🌑 **Dark Tunnel** / 💉 **HTTP Injector** — "coming soon" pages with back button
- 📞 Contact admin (WhatsApp) button everywhere
- 🆘 `/help`, `/menu`, `/about`, `/contact`, `/cancel`, `/stats`
- 📢 `/broadcast` for admins
- 📊 Stats (files received / unlocked / failed / users)
- 🔓 Real RSE5 decryption (verified byte-for-byte against the app's native code,
  and end-to-end against a real production file)

## Deploy on Railway

1. Push this repo (already on GitHub) — on Railway click **New Project → Deploy from GitHub repo** and pick this repo.
2. Railway auto-detects Python (Nixpacks) and uses the `Procfile` (worker process — no domain needed, it uses long polling).
3. In the service → **Variables**, add:

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | your bot token from @BotFather |
   | `JOIN_CHANNEL` | `@fridahtech` (optional — this is the default) |
   | `ADMIN_IDS` | your Telegram numeric ID (optional, enables admin stats/broadcast/recovery) |
   | `RECOVERY_WORDLIST_PATH` | optional file with one password candidate per line (replaces the built-in list) |
   | `RECOVERY_MAX_CANDIDATES` | max candidates per recovery run (default 150) |
   | `RECOVERY_TIME_BUDGET` | hard stop in seconds (default 600) |

4. **Deploy**. Check the deploy logs for `Yobby Tech bot starting`.
5. Open the bot in Telegram and send `/start`.

## ⚠️ Required one-time setup: add the bot to the channel

The join check reads membership from the channel, so the bot must be a member of
`@fridahtech` (bots in channels are admins by definition):

1. Open `@fridahtech` → **Channel info → Administrators → Add Admin**
2. Add your bot, grant **no extra rights** needed (default is fine)

Without this step the bot replies "Verification service is not ready" (fail-closed on purpose).

## 🧠 Password recovery (admin only)

When an admin (someone whose ID is in `ADMIN_IDS`) sends a password-protected file,
the bot offers an **Auto-recovery** button. It tries a list of common config passwords,
PINs, and filename-derived guesses against the file's authentication tag.

**What it can and cannot do — be realistic:**

- Each candidate costs a full 600,000-iteration HMAC chain (~0.8 s per guess on CPU).
- It recovers only **weak** passwords (PINs like `12345`, common words, filename-based
  guesses). Real production files regularly use exactly these.
- It **cannot** bypass a strong password — that is cryptographically infeasible:
  AES-256 with per-file random salt and nonce, and no master key exists in the
  algorithm (verified against native emulation).
- Order of tries: PINs → filename variants → built-in wordlist
  (or `RECOVERY_WORDLIST_PATH` if set).

Use this for your own files and files whose owner asked for recovery. Don't market
it as "we crack anything" — for strong passwords that claim would be false.

## Run locally (optional)

```bash
pip install -r requirements.txt
export BOT_TOKEN="123:abc..."
python3 bot.py
```

## File layout

```
bot.py           — bot logic, menus, flow, join gate
rse5.py          — verified RSE5/.RT decryptor (KDF + tag + CTR + envelope)
requirements.txt — python-telegram-bot, pycryptodome
Procfile         — Railway worker start command
```

## Security notes

- Never commit the bot token — keep it in Railway Variables only.
- Users' files are processed in memory and never stored.
