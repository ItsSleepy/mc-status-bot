"""
EUGH server status bot.

Posts a Discord embed showing whether the Minecraft server is online/offline
and who's currently connected, then keeps that SAME message updated on a
timer instead of spamming new messages every check.

Also watches the server's mods folder and posts a changelog embed whenever
the modpack changes. Because a pack change always requires a server restart,
the check is triggered on the offline -> online edge, so the changelog lands
within seconds of the server coming back up. The timed poll is a backstop.

Commands:
  /mcstatus   force-refresh the status embed
  /modpack    what the server is running right now
  /changelog  diff between two pack snapshots

Setup: see README.md in this folder.
"""

import asyncio
import json
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from mcstatus import JavaServer

import modpack

# ---------- config (from environment) ----------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
MC_HOST = os.environ.get("MC_HOST", "localhost")
MC_PORT = int(os.environ.get("MC_PORT", "25565"))
UPDATE_INTERVAL_MINUTES = float(os.environ.get("UPDATE_INTERVAL_MINUTES", "5"))
SERVER_LABEL = os.environ.get("SERVER_LABEL", "EUGH Server")

# ---------- modpack watcher config ----------
SERVER_DIR = Path(os.environ.get("SERVER_DIR", "/home/sleepy/eugh"))
MODS_DIR = Path(os.environ.get("MODS_DIR", SERVER_DIR / "mods"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", Path(__file__).parent / "versions"))
PACK_VERSION_FILE = Path(os.environ.get("PACK_VERSION_FILE", SERVER_DIR / "pack_version.txt"))
PACK_DOWNLOAD_URL = os.environ.get("PACK_DOWNLOAD_URL", "").strip()
MODPACK_CHECK_MINUTES = float(os.environ.get("MODPACK_CHECK_MINUTES", "30"))
PRESENCE_POLL_SECONDS = float(os.environ.get("PRESENCE_POLL_SECONDS", "30"))
MODPACK_CHANNEL_ID = int(os.environ.get("MODPACK_CHANNEL_ID", CHANNEL_ID))

STATE_FILE = Path(__file__).parent / "status_message.json"
MODS_STATE_FILE = Path(__file__).parent / "mods_state.json"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ================= status embed (unchanged behaviour) =================

def load_message_id() -> int | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("message_id")
        except Exception:
            return None
    return None


def save_message_id(message_id: int) -> None:
    STATE_FILE.write_text(json.dumps({"message_id": message_id}))


def build_embed() -> discord.Embed:
    """Query the MC server and build a status embed. Never raises — falls
    back to an 'offline / unreachable' embed on any error."""
    try:
        server = JavaServer.lookup(f"{MC_HOST}:{MC_PORT}")

        # Try query first (needs enable-query=true in server.properties) —
        # gives the FULL player list, not just a sample.
        player_names = None
        try:
            q = server.query()
            player_names = list(q.players.names)
            player_count = q.players.online
            max_players = q.players.max
            version = q.software.version
        except Exception:
            # fall back to basic status ping (always available, but player
            # list is a sample capped around 12 names by the server)
            status = server.status()
            player_count = status.players.online
            max_players = status.players.max
            version = status.version.name
            if status.players.sample:
                player_names = [p.name for p in status.players.sample]

        embed = discord.Embed(
            title=f"🟢 {SERVER_LABEL} — Online",
            color=discord.Color.green(),
        )
        embed.add_field(name="Players", value=f"{player_count} / {max_players}", inline=True)
        embed.add_field(name="Version", value=version or "unknown", inline=True)

        pack = current_pack_label()
        if pack != "unversioned":
            embed.add_field(name="Modpack", value=f"EUGH {pack}", inline=True)

        if player_count and player_names:
            embed.add_field(name="Who's on", value="\n".join(f"• {n}" for n in player_names), inline=False)
        elif player_count and not player_names:
            embed.add_field(name="Who's on", value="_(names unavailable — enable query in server.properties for full list)_", inline=False)
        else:
            embed.add_field(name="Who's on", value="_nobody right now_", inline=False)

        embed.set_footer(text="Updates automatically")
        embed.timestamp = discord.utils.utcnow()
        return embed

    except Exception:
        embed = discord.Embed(
            title=f"🔴 {SERVER_LABEL} — Offline",
            description="Couldn't reach the server. It may be down or restarting.",
            color=discord.Color.red(),
        )
        embed.set_footer(text="Updates automatically")
        embed.timestamp = discord.utils.utcnow()
        return embed


async def refresh_status_message() -> str:
    """Edits the tracked status message (or posts a new one if none exists
    yet / the old one was deleted). Returns a short result string."""
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    # build_embed() does blocking socket I/O (mcstatus is synchronous), so
    # run it in a thread — otherwise it stalls the whole event loop,
    # including Discord's 3-second window to ack slash commands.
    embed = await asyncio.to_thread(build_embed)

    message_id = load_message_id()
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed)
            return "updated existing message"
        except discord.NotFound:
            pass  # message was deleted — fall through to posting a new one

    msg = await channel.send(embed=embed)
    save_message_id(msg.id)
    return "posted new message"


# ===================== modpack watcher =====================

def current_pack_label() -> str:
    """Pack version comes from a one-line text file next to the server, so
    it can be bumped without touching the bot or the systemd unit."""
    try:
        return PACK_VERSION_FILE.read_text().strip() or "unversioned"
    except OSError:
        return "unversioned"


def load_mods_state() -> dict | None:
    if MODS_STATE_FILE.exists():
        try:
            return json.loads(MODS_STATE_FILE.read_text())
        except Exception:
            return None
    return None


def save_mods_state(fp: str, label: str, mods: dict) -> None:
    MODS_STATE_FILE.write_text(json.dumps(
        {"fingerprint": fp, "label": label, "mods": mods}, indent=2
    ))


def _field(items, fmt, limit: int = 1000) -> str:
    """Join lines, truncating before Discord's 1024-char field ceiling."""
    lines, used = [], 0
    for n, item in enumerate(items):
        line = fmt(item)
        if used + len(line) + 1 > limit:
            lines.append(f"…and {len(items) - n} more")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else "_none_"


def build_changelog_embed(d: dict, old_label: str, new_label: str, total: int) -> discord.Embed:
    title = (f"📦 EUGH {old_label} → {new_label}"
             if old_label != new_label else f"📦 EUGH {new_label} — mods changed")
    embed = discord.Embed(
        title=title,
        description=f"{modpack.summarise(d)} · {total} mods installed",
        color=discord.Color.blurple(),
    )
    if d["updated"]:
        embed.add_field(
            name=f"Updated ({len(d['updated'])})",
            value=_field(d["updated"], lambda m: f"• {m['name']} `{m['old']}` → `{m['new']}`"),
            inline=False)
    if d["added"]:
        embed.add_field(
            name=f"Added ({len(d['added'])})",
            value=_field(d["added"], lambda m: f"• {m['name']} `{m['version']}`"),
            inline=False)
    if d["removed"]:
        embed.add_field(
            name=f"Removed ({len(d['removed'])})",
            value=_field(d["removed"], lambda m: f"• {m['name']} `{m['version']}`"),
            inline=False)
    if PACK_DOWNLOAD_URL:
        embed.add_field(name="Download", value=PACK_DOWNLOAD_URL, inline=False)
    embed.set_footer(text="Update your client pack before joining")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def check_modpack(announce: bool = True) -> dict | None:
    """Scan the mods folder; announce a changelog if anything moved.

    Returns the diff, or None if nothing changed (or this was the first run,
    which records a silent baseline rather than announcing 148 'additions')."""
    fp = await asyncio.to_thread(modpack.fingerprint, MODS_DIR)
    state = load_mods_state()

    if state and state.get("fingerprint") == fp:
        return None  # cheap path: no jar added, removed, or touched

    mods = await asyncio.to_thread(modpack.scan_mods, MODS_DIR)
    label = current_pack_label()
    loader = modpack.detect_loader(SERVER_DIR)

    if not state:
        save_mods_state(fp, label, mods)
        modpack.save_snapshot(SNAPSHOT_DIR, label, mods, loader)
        return None

    d = modpack.diff(state.get("mods", {}), mods)
    old_label = state.get("label", "?")
    save_mods_state(fp, label, mods)

    if modpack.is_empty(d):
        return None  # mtime changed but contents identical (rsync, re-copy)

    modpack.save_snapshot(SNAPSHOT_DIR, label, mods, loader)

    if announce:
        channel = client.get_channel(MODPACK_CHANNEL_ID) or await client.fetch_channel(MODPACK_CHANNEL_ID)
        await channel.send(embed=build_changelog_embed(d, old_label, label, len(mods)))
    return d


# ===================== restart detection =====================

# None = not observed yet. Kept in memory only: on bot restart the first
# observation is treated as a baseline, never as a transition.
_last_online: bool | None = None


def server_reachable() -> bool:
    """One cheap status ping. Sub-millisecond on localhost."""
    try:
        JavaServer.lookup(f"{MC_HOST}:{MC_PORT}", timeout=3).status()
        return True
    except Exception:
        return False


@tasks.loop(seconds=PRESENCE_POLL_SECONDS)
async def presence_loop():
    """Watch for the server going down and coming back.

    A modpack change can only take effect through a restart, so the moment
    the server is reachable again is the moment to diff the mods folder."""
    global _last_online
    online = await asyncio.to_thread(server_reachable)
    if online == _last_online:
        return

    was, _last_online = _last_online, online
    if was is None:
        return  # first observation since bot start — not a real transition

    if online:
        print("server came back online — checking modpack")
        try:
            # do this BEFORE refreshing status so the embed's Modpack
            # field reflects whatever just got installed
            await check_modpack()
        except Exception as e:
            print(f"modpack check on restart failed: {e}")

    try:
        await refresh_status_message()
    except Exception as e:
        print(f"status refresh on transition failed: {e}")


# ===================== loops =====================

@tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
async def update_loop():
    await refresh_status_message()


@tasks.loop(minutes=MODPACK_CHECK_MINUTES)
async def modpack_loop():
    try:
        await check_modpack()
    except Exception as e:
        print(f"modpack check failed: {e}")


# ===================== commands =====================

@tree.command(name="mcstatus", description="Force-refresh the server status embed now")
async def mcstatus_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await refresh_status_message()
    await interaction.followup.send(f"Refreshed ({result}).", ephemeral=True)


@tree.command(name="modpack", description="What modpack version the server is running")
async def modpack_command(interaction: discord.Interaction):
    await interaction.response.defer()
    mods = await asyncio.to_thread(modpack.scan_mods, MODS_DIR)
    label = current_pack_label()

    embed = discord.Embed(
        title=f"📦 EUGH {label}",
        description="This is what the **server** is running. Your client pack must match.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Mods", value=str(len(mods)), inline=True)
    embed.add_field(name="Loader", value=modpack.detect_loader(SERVER_DIR) or "unknown", inline=True)
    snaps = modpack.list_snapshots(SNAPSHOT_DIR)
    if snaps:
        embed.add_field(name="Known versions", value=", ".join(snaps[-8:]), inline=False)
    if PACK_DOWNLOAD_URL:
        embed.add_field(name="Download", value=PACK_DOWNLOAD_URL, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await interaction.followup.send(embed=embed)


@tree.command(name="changelog", description="Show what changed between two modpack versions")
@app_commands.describe(
    old="Version to compare from (defaults to the previous snapshot)",
    new="Version to compare to (defaults to the latest snapshot)",
)
async def changelog_command(interaction: discord.Interaction, old: str | None = None, new: str | None = None):
    await interaction.response.defer()
    snaps = modpack.list_snapshots(SNAPSHOT_DIR)
    if len(snaps) < 2 and not (old and new):
        await interaction.followup.send(
            f"Only {len(snaps)} snapshot recorded so far — nothing to compare yet. "
            "The first changelog appears after the next pack update.")
        return

    old = old or snaps[-2]
    new = new or snaps[-1]
    a, b = modpack.load_snapshot(SNAPSHOT_DIR, old), modpack.load_snapshot(SNAPSHOT_DIR, new)
    if not a or not b:
        await interaction.followup.send(
            f"Couldn't find that version. Known: {', '.join(snaps) or 'none'}")
        return

    d = modpack.diff(a["mods"], b["mods"])
    if modpack.is_empty(d):
        await interaction.followup.send(f"No mod differences between {old} and {new}.")
        return
    await interaction.followup.send(embed=build_changelog_embed(d, old, new, b.get("mod_count", len(b["mods"]))))


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")
    if not update_loop.is_running():
        update_loop.start()
    if not modpack_loop.is_running():
        modpack_loop.start()
    if not presence_loop.is_running():
        presence_loop.start()


client.run(DISCORD_TOKEN)
