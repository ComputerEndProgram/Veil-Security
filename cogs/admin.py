"""Admin commands for STFC bot (guild-only)."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

# Import from bot module to avoid repeated imports during command execution
from bot import GUILD_ID, ADMIN_ROLE_ID, VERIFY_ROLE_ID, OPS71_ROLE_ID, LOG_CHANNEL_ID, store, StartWizardView

log = logging.getLogger("veil_bot")


class AdminCog(commands.Cog):
    """Handles admin commands (guild-only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.GUILD_ID = GUILD_ID
        self.ADMIN_ROLE_ID = ADMIN_ROLE_ID
        self.VERIFY_ROLE_ID = VERIFY_ROLE_ID
        self.OPS71_ROLE_ID = OPS71_ROLE_ID
        self.LOG_CHANNEL_ID = LOG_CHANNEL_ID
        self.store = store

    @app_commands.command(
        name="admin_recall_verification",
        description="[ADMIN] Recall a user's verification, remove all roles, and alert them",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="The user to recall verification for",
        reason="Reason for the recall",
    )
    async def admin_recall_verification_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "No reason provided",
    ):
        """Recall a user's verification entry and remove assigned roles."""
        # Check admin permission
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "❌ Could not verify your permissions.",
                ephemeral=True,
            )

        admin_role = interaction.guild.get_role(self.ADMIN_ROLE_ID) if interaction.guild and self.ADMIN_ROLE_ID else None
        if not admin_role or admin_role not in interaction.user.roles:
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True)

        # Check if user is even verified first
        player_data = self.store.get_player_data(user.id)
        if not player_data:
            return await interaction.followup.send(
                f"❌ **{user.mention}** has not verified their STFC account yet.",
                ephemeral=True,
            )

        guild = self.bot.get_guild(self.GUILD_ID)
        if not guild:
            return await interaction.followup.send("❌ Could not access the server.", ephemeral=True)

        member = guild.get_member(user.id)
        if not member:
            return await interaction.followup.send(
                "❌ User is not in the server or could not be found.",
                ephemeral=True,
            )

        # Get player data before deletion
        player_data = self.store.get_player_data(user.id)

        # Remove roles
        try:
            roles_to_remove = []

            # Remove OPS 71+ role
            if self.OPS71_ROLE_ID:
                ops_role = guild.get_role(self.OPS71_ROLE_ID)
                if ops_role and ops_role in member.roles:
                    roles_to_remove.append(ops_role)

            # Remove verify role
            if self.VERIFY_ROLE_ID:
                verify_role = guild.get_role(self.VERIFY_ROLE_ID)
                if verify_role and verify_role in member.roles:
                    roles_to_remove.append(verify_role)

            # Remove server role if it exists
            if player_data:
                server_role = discord.utils.find(
                    lambda r: r.name == str(player_data[2]),  # player_data[2] is server
                    guild.roles,
                )
                if server_role and server_role in member.roles:
                    roles_to_remove.append(server_role)

            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"[RECALL] {reason}")
                log.info(f"[RECALL] Removed {len(roles_to_remove)} roles from {member.id}")
        except Exception as e:
            log.warning(f"[RECALL] Error removing roles from {member.id}: {e}")

        # Delete from database
        self.store.delete_verification(user.id)
        self.store.log_verification_action(user.id, "recalled", interaction.user.id, reason)

        # Alert user via DM and offer to re-verify
        try:
            embed = discord.Embed(
                title="⚠️ Verification Recalled",
                description=f"Your STFC verification has been recalled by an administrator.",
                colour=discord.Colour.red(),
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(
                name="What this means",
                value="All roles assigned during verification have been removed. "
                      "You can now re-verify whenever you're ready.",
                inline=False,
            )
            embed.set_footer(text="Click the button below to start verification again")

            await member.send(embed=embed, view=StartWizardView())
            log.info(f"[RECALL] Sent recall notification to {member.id}")
        except discord.Forbidden:
            log.warning(f"[RECALL] Could not send DM to {member.id} (DMs disabled)")
        except Exception as e:
            log.warning(f"[RECALL] Error sending DM to {member.id}: {e}")

        await interaction.followup.send(
            f"✅ Verification for {member.mention} has been recalled.\n"
            f"All roles removed. User has been notified via DM.",
            ephemeral=True,
        )

        # Log to log channel
        if self.LOG_CHANNEL_ID:
            embed = discord.Embed(
                title="🗑️ Verification Recalled",
                description=f"**{member.mention}** verification recalled by {interaction.user.mention}",
                colour=discord.Colour.red(),
                timestamp=interaction.created_at,
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            if player_data:
                embed.add_field(name="Player", value=f"{player_data[0]}", inline=True)
                embed.add_field(name="OPS Level", value=f"{player_data[1]}", inline=True)

            try:
                await self.bot.post_to_log_channel(embed)
                log.info("[RECALL] Logged recall to log channel")
            except Exception as e:
                log.warning(f"[RECALL] Could not log to channel: {e}")

        log.info(
            f"[RECALL] user={member.id} admin={interaction.user.id} reason={reason}"
        )


async def setup(bot: commands.Bot):
    """Load the admin cog."""
    await bot.add_cog(AdminCog(bot))
