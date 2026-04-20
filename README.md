# Feishu LCSC Bot

Feishu bot for 1:1 chat.  
User sends an LCSC link or ID (for example `C2040`), bot replies with a KiCad component library `.zip` file (symbol, footprint, 3D model).

## What This Bot Does

- Listens to Feishu IM messages over websocket.
- Accepts plain text and rich-text (`post`) messages.
- Extracts LCSC ID from:
  - direct ID (`C12345`)
  - LCSC links containing that ID
- Generates KiCad symbol/footprint/3D model by invoking local `JLC2KiCad_lib-master`.
- Packages generated files as a `.zip`.
- Uploads the archive to Feishu IM and sends it back in chat.
- Supports `/info` to return live part info (stock, pricing tiers, package, lifecycle).
- Supports `/compare` to compare multiple parts side-by-side.
- Supports `/bom` via pasted text and CSV/XLSX uploads.
- Supports `/chat` natural-language component search assistant.
  - Use `/chat reset` to clear context.
  - Plain follow-up text (without `/chat`) continues active chat context.
- Supports optional STEP-only mode with `/step Cxxxx`.
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
- `/im/v1/messages/{message_id}/resources/{file_key}` (BOM file download)

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
- `KICAD_LIBRARY_MODELS` (default `STEP`, accepted values: `STEP`, `WRL`, or both like `STEP WRL`)
- `STEP_BACKEND_ORDER` (default `easyeda2kicad,jlc2kicad`, only used for `/step` mode)
- `NODE_BIN` (optional absolute path to Node.js, useful under systemd when Node is installed via `nvm`)
- `COMPARE_MAX_PARTS` (default `5`)
- `BOM_MAX_PARTS` (default `500`)
- `BOM_DETAIL_LIMIT` (default `25`, limits how many skipped/unmatched details are sent in chat)
- `BOM_GENERATE_LIBS` (default `0`)
- `BOM_MAX_LIBS` (default `5`)
- `ANTHROPIC_API_KEY` (optional, enables AI ranking/suggestions for `/chat`)
- `ANTHROPIC_MODEL` (default `claude-sonnet`)
- `CHAT_SEARCH_POOL` (default `20`, max candidates fetched from LCSC search endpoint)
- `CHAT_AI_POOL` (default `12`, candidates passed to Claude for ranking)
- `CHAT_MAX_RESULTS` (default `5`, items returned to user)
- `CHAT_SESSION_TTL_SEC` (default `7200`, how long chat context is kept per 1:1 chat)
- `CHAT_SESSION_MAX_TURNS` (default `8`, user/assistant turn pairs retained for context)
- `CHAT_SESSION_MAX_CHATS` (default `500`, max active chat contexts in memory)

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
2. Send `C2040` (or any valid LCSC component ID / product link).
3. Confirm bot sends back a `.zip` with KiCad library files.
4. Send `/info C2040` and confirm part info (stock/price tiers).
5. Send `/compare C2040 C8596` and confirm compare summary.
6. Send `/bom C2040,10` or upload a BOM CSV/XLSX file in the chat and confirm report generation.
7. Optional: send `/step C2040` to request STEP-only output.
8. Optional: send `/chat low iq 3.3V LDO in SOT-23` and confirm shortlist response.
9. Optional: reply with plain text (for example `max current 150mA`) and confirm it continues the same `/chat` context.
10. Optional: send `/chat reset` and confirm context is cleared.
