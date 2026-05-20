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
# Discord Views (Buttons & Forms)
# ---------------------------------------------------------------------------
class StartWizardView(discord.ui.View):
    """Initial welcome message with Start button."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Verification", style=discord.ButtonStyle.green)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        
        # Check if user is already verified
        if store.get_player_data(user_id):
            await interaction.response.send_message(
                "⚠️ You have already verified your STFC account!\n\n"
                "If you need to:\n"
                "- **Update your information or report an issue** → Open a support ticket in <#1432048848771223713>.\n\n"
                "Only admins (Operations) can modify or recall your verification.",
                ephemeral=True,
            )
            log.info(f"[WIZARD] User {user_id} attempted re-verification (already verified)")
            return
        
        # Initialize wizard session in database
        store.create_wizard_session(user_id)
        log.info(f"[WIZARD] Created session for user {user_id}")
        
        await interaction.response.defer()
        
        embed = discord.Embed(
            title="📋 Step 1: STFC Player Link",
            description="Please send your https://stfc.pro  player profile link.\n\n"
                        "**Valid formats:**\n"
                        "- `https://stfc.pro/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.wtf/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.live/players/XXXXXXXXXXXXX`\n"
                        "- Or just the player ID: `XXXXXXXXXXXXX`",
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text="Reply with your link in the next message")
        
        await interaction.followup.send(embed=embed)
        log.info(f"[WIZARD] User {user_id} started verification wizard")


class SkipStepsView(discord.ui.View):
    """View with Restart button during verification."""
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.danger)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your verification wizard.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        # Reset session
        store.update_wizard_session(self.user_id, step=1, stfc_link=None, screenshot_data=None)
        
        embed = discord.Embed(
            title="📋 Step 1: STFC Player Link",
            description="Please send your https://stfc.pro player profile link.\n\n"
                        "**Valid formats:**\n"
                        "- `https://stfc.pro/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.wtf/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.live/players/XXXXXXXXXXXXX`\n"
                        "- Or just the player ID: `XXXXXXXXXXXXX`",
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text="Reply with your link in the next message")
        
        await interaction.followup.send(embed=embed, view=SkipStepsView(self.user_id))
        log.info(f"[WIZARD] User {self.user_id} restarted verification wizard")


class SessionExpiredView(discord.ui.View):
    """View for expired session with Restart button."""
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="🔄 Restart Verification", style=discord.ButtonStyle.green)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your verification wizard.", ephemeral=True)
            return
        
        # Initialize new wizard session
        store.create_wizard_session(self.user_id)
        log.info(f"[WIZARD] Created new session for user {self.user_id} after timeout")
        
        await interaction.response.defer()
        
        embed = discord.Embed(
            title="📋 Step 1: STFC Player Link",
            description="Please send your https://stfc.pro player profile link.\n\n"
                        "**Valid formats:**\n"
                        "- `https://stfc.pro/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.wtf/players/XXXXXXXXXXXXX`\n"
                        "- Or just the player ID: `XXXXXXXXXXXXX`",
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text="Reply with your link in the next message (session expires in 10 minutes)")
        
        await interaction.followup.send(embed=embed, view=SkipStepsView(self.user_id))
        log.info(f"[WIZARD] Restarted wizard for user {self.user_id}")


class ConfirmVerificationView(discord.ui.View):
    """Final confirmation with Complete and Restart buttons."""
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="✅ Complete", style=discord.ButtonStyle.green)
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your verification wizard.", ephemeral=True)
            return
        
        session = store.get_wizard_session(self.user_id)
        if not session:
            await interaction.response.send_message("❌ Verification session expired.", ephemeral=True)
            return
        
        await interaction.response.defer(thinking=True)
        
        session = store.get_wizard_session(self.user_id)
        await _finalize_verification(interaction, session)
        
        # Clean up session
        store.delete_wizard_session(self.user_id)

    @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.danger)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your verification wizard.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        # Reset session
        store.update_wizard_session(self.user_id, step=1, stfc_link=None, screenshot_data=None)
        
        embed = discord.Embed(
            title="📋 Step 1: STFC Player Link",
            description="Please send your https://stfc.pro player profile link.\n\n"
                        "**Valid formats:**\n"
                        "- `https://stfc.pro/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.wtf/players/XXXXXXXXXXXXX`\n"
                        "- `https://stfc.live/players/XXXXXXXXXXXXX`\n"
                        "- Or just the player ID: `XXXXXXXXXXXXX`",
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text="Reply with your link in the next message")
        
        await interaction.followup.send(embed=embed, view=SkipStepsView(self.user_id))
        log.info(f"[WIZARD] User {self.user_id} restarted verification wizard")


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
                    verification_status TEXT DEFAULT 'verified',
                    verified_at TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verification_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    admin_id INTEGER,
                    reason TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES stfc_players(user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wizard_sessions (
                    user_id INTEGER PRIMARY KEY,
                    step INTEGER DEFAULT 1,
                    stfc_link TEXT,
                    screenshot_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            
            # Migration: Add missing columns to existing tables
            cursor = conn.execute("PRAGMA table_info(stfc_players)")
            columns = {row[1] for row in cursor.fetchall()}
            
            if "verification_status" not in columns:
                conn.execute(
                    "ALTER TABLE stfc_players ADD COLUMN verification_status TEXT DEFAULT 'verified'"
                )
                log.info("[DB] Migrated: Added verification_status column")
            
            if "verified_at" not in columns:
                conn.execute(
                    "ALTER TABLE stfc_players ADD COLUMN verified_at TIMESTAMP"
                )
                log.info("[DB] Migrated: Added verified_at column")
            
            # Add player_data_json column to wizard_sessions if missing
            cursor = conn.execute("PRAGMA table_info(wizard_sessions)")
            wizard_columns = {row[1] for row in cursor.fetchall()}
            
            if "player_data_json" not in wizard_columns:
                conn.execute(
                    "ALTER TABLE wizard_sessions ADD COLUMN player_data_json TEXT"
                )
                log.info("[DB] Migrated: Added player_data_json column to wizard_sessions")
            
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

    def get_verification_status(self, user_id: int) -> Optional[str]:
        """Get verification status for a user."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                "SELECT verification_status FROM stfc_players WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def is_user_verified(self, user_id: int) -> bool:
        """Check if user has already verified."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                "SELECT user_id FROM stfc_players WHERE user_id = ? AND verification_status = 'verified'",
                (user_id,),
            )
            return cursor.fetchone() is not None

    def mark_verified(self, user_id: int):
        """Mark user as verified."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE stfc_players SET verification_status = 'verified', verified_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()

    def delete_verification(self, user_id: int):
        """Delete a user's verification entry."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM stfc_players WHERE user_id = ?", (user_id,))
            conn.commit()

    def log_verification_action(self, user_id: int, action: str, admin_id: int = None, reason: str = None):
        """Log a verification action."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO verification_logs (user_id, action, admin_id, reason)
                   VALUES (?, ?, ?, ?)""",
                (user_id, action, admin_id, reason),
            )
            conn.commit()

    def create_wizard_session(self, user_id: int) -> None:
        """Create a new wizard session for a user."""
        from datetime import datetime, timedelta
        expires_at = (datetime.now() + timedelta(minutes=10)).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO wizard_sessions (user_id, step, created_at, expires_at)
                   VALUES (?, 1, CURRENT_TIMESTAMP, ?)""",
                (user_id, expires_at),
            )
            conn.commit()

    def get_wizard_session(self, user_id: int) -> Optional[dict]:
        """Get wizard session for a user, or None if expired/not found."""
        from datetime import datetime
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """SELECT user_id, step, stfc_link, screenshot_data, created_at, expires_at, player_data_json
                   FROM wizard_sessions WHERE user_id = ?""",
                (user_id,),
            )
            row = cursor.fetchone()
            
            if not row:
                return None
            
            # Check if expired
            expires_at = datetime.fromisoformat(row[5])
            if datetime.now() > expires_at:
                conn.execute("DELETE FROM wizard_sessions WHERE user_id = ?", (user_id,))
                conn.commit()
                return None
            
            return {
                "user_id": row[0],
                "step": row[1],
                "stfc_link": row[2],
                "screenshot_data": row[3],
                "created_at": row[4],
                "expires_at": row[5],
                "player_data_json": row[6],
            }

    def update_wizard_session(self, user_id: int, step: int = None, stfc_link: str = None, screenshot_data: str = None, player_data_json: str = None) -> None:
        """Update wizard session."""
        updates = []
        params = []
        if step is not None:
            updates.append("step = ?")
            params.append(step)
        if stfc_link is not None:
            updates.append("stfc_link = ?")
            params.append(stfc_link)
        if screenshot_data is not None:
            updates.append("screenshot_data = ?")
            params.append(screenshot_data)
        if player_data_json is not None:
            updates.append("player_data_json = ?")
            params.append(player_data_json)
        
        if not updates:
            return
        
        params.append(user_id)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                f"UPDATE wizard_sessions SET {', '.join(updates)} WHERE user_id = ?",
                params,
            )
            conn.commit()

    def get_wizard_player_data(self, user_id: int):
        """Get stored player data from wizard session."""
        import json
        from dataclasses import dataclass
        
        session = self.get_wizard_session(user_id)
        if not session or not session.get('player_data_json'):
            return None
        
        try:
            data = json.loads(session['player_data_json'])
            # Reconstruct PlayerData object
            @dataclass
            class PlayerData:
                player_id: str
                username: str
                level: int
                server: int
                alliance_tag: str = None
            
            return PlayerData(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def delete_wizard_session(self, user_id: int) -> None:
        """Delete wizard session for a user."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM wizard_sessions WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_user_stfc_link(self, user_id: int) -> Optional[str]:
        """Get stored STFC link for a user."""
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                "SELECT stfc_link FROM stfc_players WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None


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
        """Load cogs and start background tasks."""
        # Load all cogs from the cogs folder
        import pathlib
        cogs_dir = pathlib.Path(__file__).parent / "cogs"
        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue
            cog_name = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(cog_name)
                log.info(f"[SETUP] Loaded cog: {cog_name}")
            except Exception as e:
                log.error(f"[SETUP] Failed to load cog {cog_name}: {e}")

        # Sync commands globally
        await self.tree.sync()
        log.info("[SETUP] Commands synced globally")

        # Sync admin commands to guild only (removes them from other servers)
        try:
            guild = discord.Object(id=GUILD_ID)
            await self.tree.sync(guild=guild)
            log.info(f"[SETUP] Commands synced to guild {GUILD_ID}")
        except Exception as e:
            log.warning(f"[SETUP] Could not sync to guild: {e}")

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

    async def on_message(self, message: discord.Message):
        """Handle wizard flow messages in DMs."""
        log.debug(f"[WIZARD] on_message called for {message.author.id}")
        
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Only handle DMs
        if message.guild is not None:
            return
        
        user_id = message.author.id
        
        # Check if user has an active wizard session
        session = store.get_wizard_session(user_id)
        if not session:
            log.debug(f"[WIZARD] No active session for {user_id}, ignoring message")
            # Check if there's an expired session to notify user
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.execute(
                    "SELECT expires_at FROM wizard_sessions WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()
                if row:
                    # Session exists but expired - notify user with restart button
                    from datetime import datetime
                    expires_at = datetime.fromisoformat(row[0])
                    if datetime.now() > expires_at:
                        conn.execute("DELETE FROM wizard_sessions WHERE user_id = ?", (user_id,))
                        conn.commit()
                        
                        embed = discord.Embed(
                            title="⏰ Verification Session Expired",
                            description="Your verification session has expired (10 minute timeout).\n\nClick the button below to start a new verification session.",
                            colour=discord.Colour.orange(),
                        )
                        await message.author.send(embed=embed, view=SessionExpiredView(user_id))
                        log.info(f"[WIZARD] Notified user {user_id} of session expiration")
            return
        
        log.info(f"[WIZARD] Received DM from {user_id}: '{message.content[:50]}'... (step: {session['step']})")
        
        try:
            if session['step'] == 1:
                # Expecting STFC link
                player_link = message.content.strip()
                
                # Extract and validate player ID
                player_id = STFCProScraper.extract_player_id_from_url(player_link)
                if not player_id:
                    await message.reply(
                        "❌ Invalid player link. Please use format:\n"
                        "`https://stfc.pro/players/XXXXXXXXXXXXX` or `https://stfc.wtf/players/XXXXXXXXXXXXX` or just `XXXXXXXXXXXXX`",
                    )
                    return
                
                # Fetch player data
                player_data = STFCProScraper.fetch_player_data(player_id)
                if not player_data:
                    await message.reply(
                        f"❌ Could not fetch player data from stfc.pro. Check the ID: `{player_id}`"
                    )
                    return
                
                # Store link and player data as JSON
                import json
                player_data_json = json.dumps({
                    "player_id": player_data.player_id,
                    "username": player_data.username,
                    "level": player_data.level,
                    "server": player_data.server,
                    "alliance_tag": player_data.alliance_tag,
                })
                store.update_wizard_session(user_id, stfc_link=player_link, player_data_json=player_data_json)
                store.update_wizard_session(user_id, step=2)
                
                # Ask for screenshot
                embed = discord.Embed(
                    title="📷 Step 2: Player Profile Screenshot",
                    description="Please upload a screenshot of your STFC player profile.\n\n"
                                "This is used for logging and verification purposes.",
                    colour=discord.Colour.blue(),
                )
                embed.set_footer(text="Reply with a screenshot attachment in the next message")
                
                await message.reply(embed=embed, view=SkipStepsView(user_id))
                log.info(f"[WIZARD] User {user_id} provided STFC link, moving to step 2")
            
            elif session['step'] == 2:
                # Expecting screenshot
                if not message.attachments:
                    await message.reply("❌ Please attach a screenshot image.")
                    return
                
                screenshot = message.attachments[0]
                
                # Validate screenshot
                if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
                    await message.reply("❌ Please upload an image file (PNG, JPG, etc.)")
                    return
                
                # Read and store screenshot
                try:
                    img_bytes = await screenshot.read()
                    import base64
                    screenshot_b64 = base64.b64encode(img_bytes).decode('utf-8')
                    store.update_wizard_session(user_id, screenshot_data=screenshot_b64, step=3)
                except Exception as e:
                    log.error(f"[WIZARD] Failed to read screenshot: {e}")
                    await message.reply("❌ Could not read screenshot. Please try again.")
                    return
                
                # Get player data from session
                player_data = store.get_wizard_player_data(user_id)
                if not player_data:
                    await message.reply("❌ Session error: Could not retrieve player data. Please restart.")
                    store.delete_wizard_session(user_id)
                    return
                
                # Show summary
                embed = discord.Embed(
                    title="✅ Step 3: Verify Information",
                    description="Please review your information below. If everything looks correct, click **Complete**.",
                    colour=discord.Colour.gold(),
                )
                embed.add_field(name="Player Name", value=f"{player_data.username}", inline=True)
                embed.add_field(name="OPS Level", value=f"{player_data.level}", inline=True)
                embed.add_field(name="Server", value=f"{player_data.server}", inline=True)
                embed.add_field(name="Alliance", value=player_data.alliance_tag or "None", inline=True)
                
                ops_eligible = "✅ Yes" if player_data.level >= MIN_OPS_LEVEL else f"❌ No (Level {player_data.level} < {MIN_OPS_LEVEL})"
                embed.add_field(name="Eligible for OPS 71+ Role", value=ops_eligible, inline=False)
                
                embed.set_footer(text="Review the data above and click Complete or Restart")
                
                await message.reply(embed=embed, view=ConfirmVerificationView(user_id))
                log.info(f"[WIZARD] User {user_id} provided screenshot, showing summary")
        
        except Exception as e:
            log.error(f"[WIZARD] Error in wizard flow for user {user_id}: {e}", exc_info=True)
            await message.reply(f"❌ An error occurred: {e}")
            store.delete_wizard_session(user_id)


bot = VeilBot()


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------
# Verification Helpers
async def _finalize_verification(interaction: discord.Interaction, session: dict):
    """Finalize the verification after user clicks Complete."""
    user_id = session['user_id']
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await interaction.followup.send("❌ Could not access the server.", ephemeral=True)
        return

    member = guild.get_member(user_id)
    if not member:
        await interaction.followup.send("❌ Could not find your account.", ephemeral=True)
        return

    player_link = session['stfc_link']
    
    # Fetch player data from stfc link
    from stfc_scraper import STFCProScraper
    player_id = STFCProScraper.extract_player_id_from_url(player_link)
    if not player_id:
        await interaction.followup.send("❌ Invalid player link stored.", ephemeral=True)
        return
    
    player_data = STFCProScraper.fetch_player_data(player_id)
    if not player_data:
        await interaction.followup.send("❌ Could not fetch player data.", ephemeral=True)
        return

    # Build completion message
    feedback = ["📋 **Verification Complete**\n"]
    feedback.append("✅ **stfc.pro Data:**")
    feedback.append(f"  {format_player_info(player_data)}\n")

    # Set nickname
    new_nick = bot._build_nickname(player_data)
    try:
        await member.edit(nick=new_nick)
        feedback.append(f"✅ Nickname set to: `{new_nick}`")
        log.info(f"[WIZARD] Set nickname for {member.id}: {new_nick}")
    except discord.Forbidden:
        feedback.append(f"⚠️ Could not set nickname (missing permissions)")
        log.warning(f"[WIZARD] Could not set nickname for {member.id} (Forbidden)")
    except Exception as e:
        feedback.append(f"⚠️ Error setting nickname: {e}")
        log.error(f"[WIZARD] Error setting nickname for {member.id}: {e}")

    # Assign server role (matched by server ID name)
    server_role = discord.utils.find(
        lambda r: r.name == str(player_data.server),
        guild.roles,
    )

    if server_role:
        try:
            await member.add_roles(server_role, reason=f"Verified via stfc.pro (server {player_data.server})")
            feedback.append(f"✅ Server role assigned: `{server_role.name}`")
            log.info(f"[WIZARD] Assigned server role {server_role.name} to {member.id}")
        except Exception as e:
            feedback.append(f"⚠️ Error assigning server role: {e}")
            log.warning(f"[WIZARD] Error assigning server role to {member.id}: {e}")
    else:
        feedback.append(f"⚠️ Server role `{player_data.server}` not found")
        await bot.post_admin_notification(
            f"❌ Missing server role `{player_data.server}` for user {member.mention}"
        )
        log.error(f"[WIZARD] Server role {player_data.server} not found")

    # Assign verify role if configured
    if VERIFY_ROLE_ID:
        verify_role = guild.get_role(VERIFY_ROLE_ID)
        if verify_role:
            try:
                await member.add_roles(verify_role, reason="Verified via stfc.pro")
                feedback.append(f"✅ Verification role assigned")
                log.info(f"[WIZARD] Assigned verify role to {member.id}")
            except Exception as e:
                feedback.append(f"⚠️ Error assigning verify role: {e}")
                log.warning(f"[WIZARD] Error assigning verify role to {member.id}: {e}")

    # Assign OPS 71+ role ONLY if level >= MIN_OPS_LEVEL
    if player_data.level >= MIN_OPS_LEVEL and OPS71_ROLE_ID:
        ops_role = guild.get_role(OPS71_ROLE_ID)
        if ops_role:
            try:
                await member.add_roles(
                    ops_role,
                    reason=f"Verified via stfc.pro - Level {player_data.level} >= {MIN_OPS_LEVEL}"
                )
                feedback.append(f"✅ OPS 71+ role assigned")
                log.info(f"[WIZARD] Assigned OPS 71+ role to {member.id} (level {player_data.level})")
            except Exception as e:
                feedback.append(f"⚠️ Error assigning OPS role: {e}")
                log.warning(f"[WIZARD] Error assigning OPS role to {member.id}: {e}")
    else:
        if player_data.level < MIN_OPS_LEVEL:
            feedback.append(
                f"⚠️ OPS level {player_data.level} < {MIN_OPS_LEVEL} - OPS role not assigned"
            )
            log.info(
                f"[WIZARD] Skipped OPS role for {member.id} (level {player_data.level} < {MIN_OPS_LEVEL})"
            )

    # Store player link and data for periodic updates
    store.store_stfc_player(member.id, player_link, player_data)
    store.mark_verified(member.id)
    store.log_verification_action(member.id, "verified")
    feedback.append(f"💾 Player data stored for periodic updates")

    feedback_text = "\n".join(feedback)
    await interaction.followup.send(feedback_text, ephemeral=True)

     # Log to log channel with screenshot
    if LOG_CHANNEL_ID:
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
        
        # Get screenshot from session if available
        screenshot_file = None
        if session.get('screenshot_data'):
            try:
                import base64
                import io
                screenshot_bytes = base64.b64decode(session['screenshot_data'])
                screenshot_file = discord.File(io.BytesIO(screenshot_bytes), filename="verification.png")
                embed.set_image(url="attachment://verification.png")
            except Exception as e:
                log.warning(f"[WIZARD] Could not decode screenshot: {e}")
        
        try:
            if screenshot_file:
                await bot.post_to_log_channel(embed, screenshot_file)
            else:
                await bot.post_to_log_channel(embed)
            log.info("[WIZARD] Logged verification to log channel")
        except Exception as e:
            log.warning(f"[WIZARD] Could not log to channel: {e}")

    log.info(
        f"[WIZARD] user={member.id} name={player_data.username} "
        f"server={player_data.server} alliance={player_data.alliance_tag} "
        f"level={player_data.level} ops_eligible={player_data.level >= MIN_OPS_LEVEL}"
    )


# Slash commands are now in cogs/ folder for better organization


@bot.event
async def on_member_join(member: discord.Member):
    """Send verification wizard welcome when user joins."""
    # Only handle the main guild
    if member.guild.id != GUILD_ID:
        return
    
    # Ignore bots
    if member.bot:
        return
    
    try:
        embed = discord.Embed(
            title="👋 Welcome to the Server!",
            description="To access server features, you need to verify your STFC account.",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="What is this?",
            value="We use stfc.pro/stfc.wtf player data to verify your account and assign appropriate roles.\n"
                  "This ensures our community stays safe and organized.",
            inline=False,
        )
        embed.add_field(
            name="⚠️ Important Rules",
            value="• **One-time verification**: You can only verify once.\n"
                  "• **Link uniqueness**: Each player link can only be used once.\n"
                  "• **Admin recall**: If admins recall your verification, you can re-verify after authorization.",
            inline=False,
        )
        embed.add_field(
            name="🚀 Ready?",
            value="Click the button below to start the verification wizard.",
            inline=False,
        )
        embed.set_footer(text="This message contains important information about verification.")
        
        await member.send(embed=embed, view=StartWizardView())
        log.info(f"[WIZARD] Sent welcome message to new member {member.id}")
    except discord.Forbidden:
        log.warning(f"[WIZARD] Could not send DM to new member {member.id} (DMs disabled)")
    except Exception as e:
        log.error(f"[WIZARD] Error sending welcome to member {member.id}: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    """Clean up verification data when user leaves."""
    # Only handle the main guild
    if member.guild.id != GUILD_ID:
        return
    
    # Remove from database if verified
    if store.get_player_data(member.id):
        store.delete_verification(member.id)
        store.log_verification_action(member.id, "removed", None, "User left the server")
        log.info(f"[DB] Deleted verification for user {member.id} (left server)")
    
    # Clean up wizard session if exists
    store.delete_wizard_session(member.id)
    log.info(f"[WIZARD] Cleared wizard session for user {member.id}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in .env")

    bot.run(token)
