# EUGH Status Bot

Posts a Discord embed showing whether the Minecraft server is online/offline
and who's connected. It edits the SAME message on a timer (default every 5
minutes) instead of spamming new messages, and also adds a `/mcstatus`
slash command for an on-demand refresh.

## Files already in place (deployed via Claude)

- `bot.py`, `requirements.txt`, `.env.example`, `mc-status-bot.service` are
  all here in `~/mc-status-bot`.
- `.env.example` already has your CHANNEL_ID filled in — just needs the
  bot token.

## What's left to do

1. **Rotate the bot token** at
   https://discord.com/developers/applications → your app → Bot → Reset
   Token (needed since the original was pasted in chat).
2. Copy `.env.example` to `.env` and paste the new token in:
   ```bash
   cd ~/mc-status-bot
   cp .env.example .env
   nano .env   # fill in DISCORD_TOKEN
   ```
3. Create the venv and install dependencies (if not already done):
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```
4. Install and start the service:
   ```bash
   sudo cp mc-status-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mc-status-bot
   sudo systemctl status mc-status-bot
   ```

Logs: `journalctl -u mc-status-bot -f`

## Optional — full player list instead of a capped sample

Add to `server.properties` and restart `eugh`:
```
enable-query=true
query.port=25565
```

## Notes

- If `eugh` restarts, the bot just shows offline until it's back — no
  special handling needed.
- Tracked message ID lives in `status_message.json` next to the bot. If
  you delete the message in Discord, it just posts a fresh one next update.
