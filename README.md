# Feishu LCSC Bot

Feishu bot for 1:1 chat.  
User sends an LCSC link or ID (for example `C2040`), bot replies with the `.step` file.

## What This Bot Does

- Listens to Feishu IM messages over websocket.
- Accepts plain text and rich-text (`post`) messages.
- Extracts LCSC ID from:
  - direct ID (`C12345`)
  - LCSC links containing that ID
- Downloads STEP model via shared code in `lcsc_step_downloader/core.py` (used by both bot and downloader web app).
- Uploads the file to Feishu IM and sends it back in chat.
- Rejects group chats (only `p2p`).

## Files Added For Server Hosting

- `app.py`: bot runtime.
- `requirements.txt`: python dependencies.
- `.env.example`: required env variables.
- `scripts/deploy.sh`: installs venv, dependencies, systemd unit, restarts service.
- `deploy/systemd/feishu-lcsc-bot.service`: systemd template.

## Feishu App Setup

Recommended: create a **separate Feishu app** for this bot.  
If you reuse the same app as `feishu-expense-bot`, both services can receive overlapping events.

### Event Subscription

Enable:
- `p2_im_message_receive_v1`

### Required OpenAPI Access

The bot uses these endpoints:
- `/auth/v3/tenant_access_token/internal/`
- `/im/v1/messages`
- `/im/v1/files`

After changing permissions/events, publish a new app version.

## Environment Variables

Required:
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

Recommended:
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`

Optional:
- `LOG_LEVEL` (default `INFO`)
- `LOG_PATH` (default `logs/bot.log`)
- `DEDUP_CAPACITY` (default `3000`)

## Reusing Existing Credentials From `feishu-expense-bot`

If you want to reuse the same credentials temporarily, copy these keys from:
- `/home/santilopez10/Feishu Bot/feishu-expense-bot/.env`

into:
- `/home/santilopez10/Feishu Bot/Feishu LCSC bot/.env`

Keys to copy:
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`

## Local Run

```bash
cd "/home/santilopez10/Feishu Bot/Feishu LCSC bot"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python app.py
```

## Deploy To Server (Systemd)

```bash
cd "/home/santilopez10/Feishu Bot/Feishu LCSC bot"
cp .env.example .env
# edit .env first
bash scripts/deploy.sh
```

Health checks:

```bash
sudo systemctl status feishu-lcsc-bot --no-pager
journalctl -u feishu-lcsc-bot -f
tail -f "/home/santilopez10/Feishu Bot/Feishu LCSC bot/logs/bot.log"
```

## Smoke Test

1. Open 1:1 chat with the bot in Feishu.
2. Send `C2040` (or any valid LCSC component ID with STEP model).
3. Confirm bot sends back a `.step` file.
