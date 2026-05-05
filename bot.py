"""
Veil Security Bot - STFC Verification & Updates

Features:
  - Verify STFC players via stfc.pro/stfc.wtf data
  - Assign server roles (matched by server ID)
  - Assign OPS 71+ role only if player level >= 71
  - Periodic updates to check for OPS level changes
  - Screenshot logging for verification records
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks, commands
from dotenv import load_dotenv

from stfc_scraper import STFCProScraper, PlayerData, format_player_info

# ---------------------------------------------------------------------------
# Environment & Logging
# ---------------------------------------------------------------------------
load_dotenv()

DEBUG = os.getenv("DEBUG", "0") not in ("0", "", "false", "False", "no", "No")
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("veil_bot")


# ---------------------------------------------------------------------------
# Config Helpers
# ---------------------------------------------------------------------------
def _env_int(name: str, required: bool = True, default: Optional[int] = None) -> Optional[int]:
    """Load integer from environment variable."""
    v = os.getenv(name)
    if v is None or v == "":
        if required:
            raise SystemExit(f"Missing required env var: {name}")
        return default
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"Env var {name} must be an integer.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GUILD_ID = _env_int("GUILD_ID")
OPS71_ROLE_ID = _env_int("OPS71_ROLE_ID")
VERIFY_ROLE_ID = _env_int("VERIFY_ROLE_ID", required=False, default=0)
ADMIN_ROLE_ID = _env_int("ADMIN_ROLE_ID", required=False, default=0)

VERIFY_CHANNEL_ID = _env_int("VERIFY_CHANNEL_ID")
LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", required=False, default=0)

MIN_OPS_LEVEL = int(os.getenv("MIN_OPS_LEVEL", "71"))
UPDATE_CHECK_HOURS = int(os.getenv("UPDATE_CHECK_HOURS", "24"))
DB_PATH = os.getenv("DB_PATH", "stfc_players.db")

log.info(
    "Config: GUILD=%s OPS71_ROLE=%s VERIFY_ROLE=%s ADMIN_ROLE=%s "
    "VERIFY_CH=%s LOG_CH=%s MIN_OPS_LEVEL=%s UPDATE_CHECK=%sh",
    GUILD_ID, OPS71_ROLE_ID, VERIFY_ROLE_ID, ADMIN_ROLE_ID,
    VERIFY_CHANNEL_ID, LOG_CHANNEL_ID, MIN_OPS_LEVEL, UPDATE_CHECK_HOURS,
)


# ---------------------------------------------------------------------------
# SQLite Store for STFC Player Links
# ---------------------------------------------------------------------------
class Store:
    """SQLite wrapper to store STFC player links for periodic updates."""

    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self):
        """Initialize database tables."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stfc_players (
                    user_id INTEGER PRIMARY KEY,
                    stfc_link TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    username TEXT,
                    level INTEGER,
                    server INTEGER,
                    alliance_tag TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def store_stfc_player(
        self,
        user_id: int,
        stfc_link: str,
        player_data: PlayerData,
    ):
        """Store or update a player's STFC link and data."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO stfc_players
                (user_id, stfc_link, player_id, username, level, server, alliance_tag, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                user_id,
                stfc_link,
                player_data.player_id,
                player_data.username,
                player_data.level,
                player_data.server,
                player_data.alliance_tag,
            ))
            conn.commit()

    def get_all_players(self) -> list[tuple]:
        """Get all stored player links for update checks."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                "SELECT user_id, stfc_link, player_id FROM stfc_players"
            )
            return cursor.fetchall()

    def get_player_data(self, user_id: int) -> Optional[tuple]:
        """Get stored player data for a user."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                "SELECT username, level, server, alliance_tag FROM stfc_players WHERE user_id = ?",
                (user_id,),
            )
            return cursor.fetchone()


store = Store(DB_PATH)


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
class VeilBot(commands.Bot):
    """Veil Security Bot for STFC verification and updates."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """Register commands and start background tasks."""
        await self.tree.sync(guild=discord.Object(GUILD_ID))
        log.info("[SETUP] Commands synced")

        self.update_stfc_players.start()
        log.info("[SETUP] Background tasks started")

    async def on_ready(self):
        """Bot ready."""
        log.info(f"[READY] Logged in as {self.user}")

    async def post_to_log_channel(self, embed: discord.Embed, file: discord.File = None):
        """Post an embed to the log channel."""
        if not LOG_CHANNEL_ID:
            return

        guild = self.get_guild(GUILD_ID)
        if not guild:
            return

        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if not log_ch:
            log.warning(f"[LOG] Log channel {LOG_CHANNEL_ID} not found")
            return

        try:
            await log_ch.send(embed=embed, file=file)
        except Exception as e:
            log.warning(f"[LOG] Could not send to log channel: {e}")

    async def post_admin_notification(self, message: str):
        """Post a message to admins (via admin role if available)."""
        if not ADMIN_ROLE_ID:
            return

        guild = self.get_guild(GUILD_ID)
        if not guild:
            return

        admin_role = guild.get_role(ADMIN_ROLE_ID)
        if not admin_role:
            log.warning(f"[ADMIN] Admin role {ADMIN_ROLE_ID} not found")
            return

        for channel in guild.text_channels:
            try:
                await channel.send(f"{admin_role.mention} {message}")
                return
            except discord.Forbidden:
                continue

        log.warning("[ADMIN] Could not find a channel to send notification")

    @tasks.loop(hours=1)
    async def update_stfc_players(self):
        """Periodically update player data from stfc.pro."""
        guild = self.get_guild(GUILD_ID)
        if not guild:
            log.warning("[UPDATE] Guild not found")
            return

        log.info("[UPDATE] Starting periodic player update check")

        players = store.get_all_players()
        log.info(f"[UPDATE] Found {len(players)} players to check")

        for user_id, stfc_link, player_id in players:
            member = guild.get_member(user_id)
            if not member:
                log.debug(f"[UPDATE] Member {user_id} no longer in guild")
                continue

            try:
                player_data = STFCProScraper.fetch_player_data(player_id)
                if not player_data:
                    log.warning(f"[UPDATE] Could not fetch data for player {player_id}")
                    continue

                old_data = store.get_player_data(user_id)
                old_level = old_data[1] if old_data else None

                # Update nickname if changed
                new_nick = self._build_nickname(player_data)
                if member.nick != new_nick:
                    try:
                        await member.edit(nick=new_nick)
                        log.info(
                            f"[UPDATE] Updated nickname for {member.id} ({member.name}): {new_nick}"
                        )
                    except discord.Forbidden:
                        log.debug(f"[UPDATE] Could not update nickname for {member.id} (Forbidden)")
                    except Exception as e:
                        log.warning(f"[UPDATE] Error updating nickname for {member.id}: {e}")

                # Check for OPS level changes
                if old_level != player_data.level:
                    log.info(
                        f"[UPDATE] {member.id} ({member.name}) level changed: "
                        f"{old_level} → {player_data.level}"
                    )

                    # Assign OPS role if they reached level 71+
                    has_ops_role = any(r.id == OPS71_ROLE_ID for r in member.roles)
                    if player_data.level >= MIN_OPS_LEVEL and not has_ops_role and OPS71_ROLE_ID:
                        ops_role = guild.get_role(OPS71_ROLE_ID)
                        if ops_role:
                            try:
                                await member.add_roles(
                                    ops_role,
                                    reason=f"Auto-promoted to OPS 71+ (level {player_data.level})"
                                )
                                log.info(
                                    f"[UPDATE] Promoted {member.id} ({member.name}) to OPS 71+ "
                                    f"(level {player_data.level})"
                                )
                            except Exception as e:
                                log.warning(
                                    f"[UPDATE] Could not assign OPS role to {member.id}: {e}"
                                )

                # Store updated player data
                store.store_stfc_player(user_id, stfc_link, player_data)

            except Exception as e:
                log.error(f"[UPDATE] Error updating player {player_id}: {e}")

        log.info("[UPDATE] Periodic player update check completed")

    @update_stfc_players.before_loop
    async def before_update_stfc(self):
        """Wait for bot to be ready before starting update loop."""
        await self.wait_until_ready()

    def _build_nickname(self, player_data: PlayerData) -> str:
        """Build nickname from player data."""
        if player_data.alliance_tag:
            nick = f"[{player_data.server}] {player_data.alliance_tag} - {player_data.username}"
        else:
            nick = f"[{player_data.server}] {player_data.username}"

        if len(nick) > 32:
            nick = nick[:32]

        return nick


bot = VeilBot()


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="verify_stfc",
    description="Verify your STFC account via stfc.pro or stfc.wtf",
    guild=discord.Object(GUILD_ID),
)
@app_commands.describe(
    player_link="stfc.pro/stfc.wtf URL or player ID (e.g., https://stfc.pro/players/2659122580)",
    screenshot="Screenshot of your STFC player profile",
)
async def verify_stfc_cmd(
    interaction: discord.Interaction,
    player_link: str,
    screenshot: discord.Attachment,
):
    """Verify STFC account using stfc.pro or stfc.wtf data.

    Args:
        player_link: stfc.pro or stfc.wtf player URL or ID
        screenshot: Player profile screenshot (for logging)
    """
    await interaction.response.defer(thinking=True)

    member = (
        interaction.user
        if isinstance(interaction.user, discord.Member)
        else (interaction.guild.get_member(interaction.user.id) if interaction.guild else None)
    )
    if not member:
        return await interaction.followup.send(
            "❌ Could not identify you in the guild.", ephemeral=True
        )

    # Extract and validate player ID
    player_id = STFCProScraper.extract_player_id_from_url(player_link)
    if not player_id:
        return await interaction.followup.send(
            "❌ Invalid player link. Use format:\n"
            "`https://stfc.pro/players/2659122580` or `https://stfc.wtf/players/2659122580` or just `2659122580`",
            ephemeral=True,
        )

    # Fetch player data from stfc.pro
    player_data = STFCProScraper.fetch_player_data(player_id)
    if not player_data:
        return await interaction.followup.send(
            f"❌ Could not fetch player data from stfc.pro. Check the ID: `{player_id}`",
            ephemeral=True,
        )

    # Validate screenshot
    if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
        return await interaction.followup.send(
            "❌ Please upload an image file (PNG, JPG, etc.)",
            ephemeral=True,
        )

    # Read screenshot
    try:
        img_bytes = await screenshot.read()
    except Exception as e:
        log.error(f"[VERIFY] Failed to read screenshot: {e}")
        return await interaction.followup.send(
            "❌ Could not read screenshot. Please try again.",
            ephemeral=True,
        )

    # Build response
    feedback = [f"📋 **Verification Successful**\n"]
    feedback.append(f"✅ **stfc.pro Data:**")
    feedback.append(f"  {format_player_info(player_data)}\n")

    # Set nickname
    new_nick = bot._build_nickname(player_data)
    try:
        await member.edit(nick=new_nick)
        feedback.append(f"✅ Nickname set to: `{new_nick}`")
        log.info(f"[VERIFY] Set nickname for {member.id}: {new_nick}")
    except discord.Forbidden:
        feedback.append(f"⚠️ Could not set nickname (missing permissions)")
        log.warning(f"[VERIFY] Could not set nickname for {member.id} (Forbidden)")
    except Exception as e:
        feedback.append(f"⚠️ Error setting nickname: {e}")
        log.error(f"[VERIFY] Error setting nickname for {member.id}: {e}")

    # Assign server role (matched by server ID name)
    if interaction.guild:
        server_role = discord.utils.find(
            lambda r: r.name == str(player_data.server),
            interaction.guild.roles,
        )

        if server_role:
            try:
                await member.add_roles(server_role, reason=f"Verified via stfc.pro (server {player_data.server})")
                feedback.append(f"✅ Server role assigned: `{server_role.name}`")
                log.info(f"[VERIFY] Assigned server role {server_role.name} to {member.id}")
            except Exception as e:
                feedback.append(f"⚠️ Error assigning server role: {e}")
                log.warning(f"[VERIFY] Error assigning server role to {member.id}: {e}")
        else:
            feedback.append(f"⚠️ Server role `{player_data.server}` not found")
            await bot.post_admin_notification(
                f"❌ Missing server role `{player_data.server}` for user {member.mention}"
            )
            log.error(f"[VERIFY] Server role {player_data.server} not found")

    # Assign verify role if configured
    if VERIFY_ROLE_ID and interaction.guild:
        verify_role = interaction.guild.get_role(VERIFY_ROLE_ID)
        if verify_role:
            try:
                await member.add_roles(verify_role, reason="Verified via stfc.pro")
                feedback.append(f"✅ Verification role assigned")
                log.info(f"[VERIFY] Assigned verify role to {member.id}")
            except Exception as e:
                feedback.append(f"⚠️ Error assigning verify role: {e}")
                log.warning(f"[VERIFY] Error assigning verify role to {member.id}: {e}")

    # Assign OPS 71+ role ONLY if level >= MIN_OPS_LEVEL
    if player_data.level >= MIN_OPS_LEVEL and OPS71_ROLE_ID and interaction.guild:
        ops_role = interaction.guild.get_role(OPS71_ROLE_ID)
        if ops_role:
            try:
                await member.add_roles(
                    ops_role,
                    reason=f"Verified via stfc.pro - Level {player_data.level} >= {MIN_OPS_LEVEL}"
                )
                feedback.append(f"✅ OPS 71+ role assigned")
                log.info(f"[VERIFY] Assigned OPS 71+ role to {member.id} (level {player_data.level})")
            except Exception as e:
                feedback.append(f"⚠️ Error assigning OPS role: {e}")
                log.warning(f"[VERIFY] Error assigning OPS role to {member.id}: {e}")
    else:
        if player_data.level < MIN_OPS_LEVEL:
            feedback.append(
                f"⚠️ OPS level {player_data.level} < {MIN_OPS_LEVEL} - OPS role not assigned"
            )
            log.info(
                f"[VERIFY] Skipped OPS role for {member.id} (level {player_data.level} < {MIN_OPS_LEVEL})"
            )

    # Store player link and data for periodic updates
    store.store_stfc_player(member.id, player_link, player_data)
    feedback.append(f"💾 Player data stored for periodic updates")

    feedback_text = "\n".join(feedback)
    await interaction.followup.send(feedback_text, ephemeral=True)

    # Log to log channel with screenshot
    if LOG_CHANNEL_ID and interaction.guild:
        embed = discord.Embed(
            title="✅ Verification Successful",
            description=f"**{member.mention}** verified as **{player_data.username}**",
            colour=discord.Colour.green(),
            timestamp=interaction.created_at,
        )
        embed.add_field(name="Player", value=f"{player_data.username}", inline=True)
        embed.add_field(name="OPS Level", value=f"{player_data.level}", inline=True)
        embed.add_field(name="Server", value=f"{player_data.server}", inline=True)
        embed.add_field(
            name="Alliance",
            value=player_data.alliance_tag or "None",
            inline=True,
        )
        embed.set_image(url=f"attachment://{screenshot.filename}")

        try:
            file = await screenshot.to_file()
            await bot.post_to_log_channel(embed, file)
            log.info("[VERIFY] Logged verification to log channel")
        except Exception as e:
            log.warning(f"[VERIFY] Could not log to channel: {e}")

    log.info(
        f"[VERIFY] user={member.id} player_id={player_id} name={player_data.username} "
        f"server={player_data.server} alliance={player_data.alliance_tag} "
        f"level={player_data.level} ops_eligible={player_data.level >= MIN_OPS_LEVEL}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in .env")

    bot.run(token)
