"""Verification commands for STFC bot."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

# These imports are now safe since bot.py doesn't have problematic re-imports
from bot import store, StartWizardView

log = logging.getLogger("veil_bot")


class VerificationCog(commands.Cog):
    """Handles STFC verification commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="verify_stfc",
        description="Start STFC verification wizard",
    )
    @app_commands.guild_only()
    async def verify_stfc_cmd(self, interaction: discord.Interaction):
        """Start the STFC verification wizard in DMs.

        This command is available in the server. When triggered, it sends the
        verification wizard to the user's DMs.
        """
        # Defer immediately to avoid timeout - we have 3 seconds to respond
        await interaction.response.defer(ephemeral=True)
        
        # Check if already verified
        if store.get_player_data(interaction.user.id):
            await interaction.followup.send(
                "⚠️ You have already verified your STFC account!\n\n"
                "If you need to:\n"
                "- **Update your information or report an issue** → Open a support ticket in <#1432048848771223713>.\n\n"
                "Only Admins (Operations) can modify or recall your verification.",
                ephemeral=True,
            )
            log.info(f"[WIZARD] User {interaction.user.id} attempted re-verification via command (already verified)")
            return

        embed = discord.Embed(
            title="👋 Welcome to STFC Verification!",
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
            value="- **One-time verification**: You can only verify once.\n"
                  "- **Link uniqueness**: Each player link can only be used once.\n"
                  "- **Admin recall**: If admins recall your verification, you can re-verify again.",
            inline=False,
        )
        embed.add_field(
            name="🚀 Ready?",
            value="Click the button below to start the verification wizard.",
            inline=False,
        )
        embed.set_footer(text="This message contains important information about verification.")

        try:
            await interaction.user.send(embed=embed, view=StartWizardView())
            await interaction.followup.send(
                "✅ Verification wizard sent to your DMs!",
                ephemeral=True,
            )
            log.info(f"[WIZARD] User {interaction.user.id} triggered verification via /verify_stfc command")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't send you a DM. Please enable DMs from server members and try again.",
                ephemeral=True,
            )
            log.warning(f"[WIZARD] Could not send DM to {interaction.user.id} (DMs disabled)")


async def setup(bot: commands.Bot):
    """Load the verification cog."""
    await bot.add_cog(VerificationCog(bot))
