# Yobby Tech Bot 👑

Telegram bot for **Yobby Tech — The King of Tech**. Channel-gated services bot with
Reese Tunnel `.RT` config decryption built in.

## Features

- 🔐 **Channel join gate** — users must join [@fridahtech](https://t.me/fridahtech) first
- 👑 Welcome card with services menu
- 🚇 **Reese Tunnel** — send a `.RT` file + password, get the full decrypted config
- 🌑 **Dark Tunnel** / 💉 **HTTP Injector** — "coming soon" pages with back button
- 📞 Contact admin (WhatsApp) button everywhere
- 🆘 `/help`, `/menu`, `/about`, `/contact`, `/cancel`, `/stats`
- 📢 `/broadcast` for admins
- 📊 Stats (files received / unlocked / failed / users)
- 🔓 Real RSE5 decryption (verified byte-for-byte against the app's native code)

## Deploy on Railway

1. Push this repo (already on GitHub) — on Railway click **New Project → Deploy from GitHub repo** and pick this repo.
2. Railway auto-detects Python (Nixpacks) and uses the `Procfile` (worker process — no domain needed, it uses long polling).
3. In the service → **Variables**, add:

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | your bot token from @BotFather |
   | `JOIN_CHANNEL` | `@fridahtech` (optional — this is the default) |
   | `ADMIN_IDS` | your Telegram numeric ID (optional, enables admin stats/broadcast) |

4. **Deploy**. Check the deploy logs for `Yobby Tech bot starting`.
5. Open the bot in Telegram and send `/start`.

## ⚠️ Required one-time setup: add the bot to the channel

The join check reads membership from the channel, so the bot must be a member of
`@fridahtech` (bots in channels are admins by definition):

1. Open `@fridahtech` → **Channel info → Administrators → Add Admin**
2. Add your bot, grant **no extra rights** needed (default is fine)

Without this step the bot replies "Verification service is not ready" (fail-closed on purpose).

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
