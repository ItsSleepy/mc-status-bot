# Minecraft Status Bot

A Discord bot for a self-hosted Minecraft server. It keeps one live status message up to date, and when the server's modpack changes it posts a changelog of what was added, removed and updated.

## Features

- **Live status message:** online or offline, players `x / max`, version, modpack version and who's on. The bot edits the **same** message every few minutes instead of posting new ones, `/mcstatus` refreshes it on demand, and `/modpack` shows the pack version.
- **Presence:** the bot's own Discord status shows the player count, checked every 30 seconds.
- **Modpack changelog:** it fingerprints the server's `mods/` folder by reading every jar's `neoforge.mods.toml` / `mods.toml` and manifest. When the set changes, it diffs against the last snapshot and posts what was added, removed and changed (with old → new versions), plus an optional download link for the pack.
- **`modpack.py` CLI:** the same scanner works on its own to snapshot or diff two mod folders.

If the server restarts, the message just shows offline until it's back. If the message is deleted, the next update posts a fresh one.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env        # bot token + channel ID
venv/bin/python bot.py
```

`mc-status-bot.service` is the systemd unit I run it with.

For the full player list, not just the capped sample, enable query in `server.properties`:

```
enable-query=true
query.port=25565
```

## Settings

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | required | |
| `CHANNEL_ID` | required | where the status message lives |
| `MC_HOST` / `MC_PORT` | `localhost` / `25565` | |
| `SERVER_LABEL` | `EUGH Server` | title of the status message |
| `UPDATE_INTERVAL_MINUTES` | `5` | |
| `SERVER_DIR` / `MODS_DIR` | | where the server and its mods live |
| `MODPACK_CHANNEL_ID` | `CHANNEL_ID` | where changelogs are posted |
| `PACK_DOWNLOAD_URL` | empty | link added to changelogs |
| `MODPACK_CHECK_MINUTES` | `30` | |

Built with Python, discord.py and mcstatus.
