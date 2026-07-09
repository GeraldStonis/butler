"""
Commands Cog — Slash commands for Nixi.

/ask        — Factual question answering (in character but accuracy-first)
/info-yourself — Brief butler self-introduction
/info-boss  — Vague, privacy-protective info about the employer
/instruct   — Owner-only standing instruction (stored persistently)
!sync       — Owner-only command tree sync (prefix command)
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import llm, memory, emoji_filter, database as db

log = logging.getLogger("nixi.commands")


class CommandsCog(commands.Cog):
    """Slash commands and owner utilities."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /ask ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="ask",
        description="Ask Nixi any question — he'll answer factually and in character.",
    )
    @app_commands.describe(question="What would you like to know?")
    async def ask(
        self, interaction: discord.Interaction, question: str
    ) -> None:
        await interaction.response.defer(thinking=True)

        guild_id = interaction.guild.id if interaction.guild else None
        context = await memory.build_context(
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            current_message=question,
            current_author=interaction.user.display_name,
            mode="ASK_COMMAND",
            guild_id=guild_id,
            guild=interaction.guild,
        )

        reply = await llm.generate_response(
            context, temperature=0.3  # Lower temp for factual accuracy
        )

        # Filter emojis
        guild = interaction.guild
        reply = await emoji_filter.filter_response(reply, guild)

        if len(reply) > 2000:
            reply = reply[:1997] + "..."

        await interaction.followup.send(reply)

        # Log both sides
        await memory.log_message(
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            guild_id=guild_id,
            author_id=interaction.user.id,
            author_name=interaction.user.display_name,
            content=f"[/ask] {question}",
        )
        await memory.log_message(
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            guild_id=guild_id,
            author_id=self.bot.user.id,  # type: ignore[union-attr]
            author_name="Nixi",
            content=reply,
            is_bot=True,
        )

    # ── /info-yourself ───────────────────────────────────────────────────

    @app_commands.command(
        name="info-yourself",
        description="Nixi introduces himself.",
    )
    async def info_yourself(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        persona = config.load_persona()
        context = [
            {"role": "system", "content": persona},
            {
                "role": "system",
                "content": (
                    "The user has asked you to introduce yourself. Give a brief, "
                    "charming, in-character introduction of yourself as Nixi the "
                    "butler. 2-3 sentences. Be witty and warm."
                ),
            },
            {
                "role": "user",
                "content": f"[{interaction.user.display_name}]: Tell me about yourself, Nixi.",
            },
        ]

        reply = await llm.generate_response(context, temperature=0.8)
        guild = interaction.guild
        reply = await emoji_filter.filter_response(reply, guild)

        if len(reply) > 2000:
            reply = reply[:1997] + "..."

        await interaction.followup.send(reply)

    # ── /info-boss ───────────────────────────────────────────────────────

    @app_commands.command(
        name="info-boss",
        description="Ask Nixi about his employer (he'll be discreet).",
    )
    async def info_boss(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        persona = config.load_persona()
        context = [
            {"role": "system", "content": persona},
            {
                "role": "system",
                "content": (
                    "The user is asking about your employer, Icosahedronixon. "
                    "Give a deliberately vague, privacy-protective, in-character "
                    "description. You may mention he is a gentleman of refined "
                    "tastes and eclectic interests. NEVER mention his profession, "
                    "technical skills, or frame his shyness as a weakness. "
                    "Keep it to 2-3 charming sentences."
                ),
            },
            {
                "role": "user",
                "content": f"[{interaction.user.display_name}]: Tell me about your boss.",
            },
        ]

        reply = await llm.generate_response(context, temperature=0.8)
        guild = interaction.guild
        reply = await emoji_filter.filter_response(reply, guild)

        if len(reply) > 2000:
            reply = reply[:1997] + "..."

        await interaction.followup.send(reply)

    # ── /instruct (owner-only) ───────────────────────────────────────────

    @app_commands.command(
        name="instruct",
        description="[Owner only] Give Nixi a standing instruction to remember.",
    )
    @app_commands.describe(
        instruction="What should Nixi remember and follow going forward?"
    )
    async def instruct(
        self, interaction: discord.Interaction, instruction: str
    ) -> None:
        # Owner check
        if interaction.user.id != config.OWNER_ID:
            await interaction.response.send_message(
                "I beg your pardon, but I take instructions from the "
                "Young Master alone. Do forgive the impertinence of refusing.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        instr_id = await db.save_instruction(instruction)
        log.info("Owner instruction #%d stored: %.100s", instr_id, instruction)

        await interaction.followup.send(
            f"Very good, M'Lord. Instruction noted and shall be observed "
            f"henceforth. (Instruction #{instr_id})",
            ephemeral=True,
        )

    # ── !sync (prefix, owner-only) ───────────────────────────────────────

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx: commands.Context) -> None:
        """Manually sync the slash command tree with Discord."""
        async with ctx.typing():
            synced = await self.bot.tree.sync()
        await ctx.send(
            f"Command tree synchronised. {len(synced)} commands registered."
        )

    @sync_commands.error
    async def sync_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.NotOwner):
            await ctx.send(
                "I'm afraid that privilege is reserved for the master of the house."
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandsCog(bot))
