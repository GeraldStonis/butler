"""Button-driven games: owner or Nixi starts, everyone can participate."""

import asyncio
import logging
import time

import discord
from discord.ext import commands

import config
from services import activities, database as db, memory

log = logging.getLogger("nixi.activities")


class GameView(discord.ui.View):
    def __init__(self, cog, channel, kind, data):
        super().__init__(timeout=None)
        self.cog, self.channel, self.kind, self.data = cog, channel, kind, data
        self.votes = {}
        self.round = 0
        self.message = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.deadline = time.monotonic() + 60
        self.timer = None
        self.add_buttons()

    def arm_timer(self):
        self.deadline = time.monotonic() + 60
        self.timer = asyncio.create_task(self._finish_after_delay())

    async def _finish_after_delay(self):
        await asyncio.sleep(60)
        await self.on_timeout()

    def stop(self):
        super().stop()
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()

    def add_buttons(self):
        self.clear_items()
        labels = ["A", "B", "C", "D"] if self.kind == "trivia" else ["Higher", "Lower"]
        for index, label in enumerate(labels):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            async def callback(interaction, choice=index):
                await self.vote(interaction, choice)
            button.callback = callback
            self.add_item(button)

    async def vote(self, interaction, choice):
        async with self.lock:
            if self.closed or time.monotonic() >= self.deadline:
                await interaction.response.send_message("This round is closed.", ephemeral=True)
                return
            if interaction.user.id in self.votes:
                await interaction.response.send_message("Your answer is already locked in.", ephemeral=True)
                return
            self.votes[interaction.user.id] = choice
            # Participation counts as human activity even though it isn't a message.
            await interaction.response.send_message("Answer locked in.", ephemeral=True)
            await db.record_user_activity(self.channel.guild.id, self.channel.id, interaction.user.id, interaction.user.display_name)

    def embed(self):
        if self.kind == "trivia":
            embed = discord.Embed(title=self.data["category"], description=self.data["question"])
            for i, answer in enumerate(self.data["answers"]):
                embed.add_field(name="ABCD"[i], value=answer, inline=False)
            embed.set_footer(text="Open Trivia DB - CC BY-SA 4.0 | 60 seconds | +1 for a correct answer")
        else:
            card = self.data[self.round]
            embed = discord.Embed(title=f"Higher Or Lower - Round {self.round + 1}/5",
                                  description=f"{card['value']} of {card['suit']}\nAce high. Equal rank: no points.")
            embed.set_image(url=card["image"])
            embed.set_footer(text="Deck of Cards API | 60 seconds | +1 for a correct guess")
        return embed

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                if self.kind == "trivia":
                    correct = self.data["correct"]
                    answer = self.data["answers"][correct]
                    result = f"Answer: {answer}"
                else:
                    before, after = activities.card_rank(self.data[self.round]), activities.card_rank(self.data[self.round + 1])
                    correct = 0 if after > before else 1 if after < before else -1
                    card = self.data[self.round + 1]
                    result = f"Next card: {card['value']} of {card['suit']}"
                winners = [uid for uid, vote in self.votes.items() if vote == correct]
                for uid in winners:
                    await db.add_activity_score(self.channel.guild.id, uid, self.kind)
                embed = self.embed()
                embed.add_field(name="Result", value=f"{result}\n{len(winners)} correct / {len(self.votes)} players", inline=False)
                for button in self.children:
                    button.disabled = True
                if self.message:
                    await self.message.edit(embed=embed, view=self)
                if not self.votes:
                    return
                try:
                    line = await activities.commentary(f"{self.kind} round ended: {len(winners)} correct out of {len(self.votes)} participants.")
                except Exception:
                    line = ""
                if line:
                    sent = await self.channel.send(line, allowed_mentions=discord.AllowedMentions.none())
                    await memory.log_delivery(sent)
                if self.kind == "cards" and self.round < 4:
                    next_view = GameView(self.cog, self.channel, self.kind, self.data)
                    next_view.round = self.round + 1
                    next_view.message = await self.channel.send(embed=next_view.embed(), view=next_view)
                    await memory.log_delivery(next_view.message)
                    next_view.arm_timer()
                    self.cog.active[self.channel.id] = next_view
            except Exception:
                log.exception("Could not settle game in channel %s", self.channel.id)
            finally:
                if self.cog.active.get(self.channel.id) is self:
                    self.cog.active.pop(self.channel.id, None)


class ActivitiesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active = {}
        self._start_lock = asyncio.Lock()

    def cog_unload(self):
        for view in self.active.values():
            view.stop()
        self.active.clear()

    async def start_game(self, channel, kind, *, requester_id=None, autonomous=False):
        if not config.ACTIVITIES_ENABLED or not getattr(channel, "guild", None):
            return False
        if not autonomous and requester_id != config.OWNER_ID:
            return False
        async with self._start_lock:
            if channel.id in self.active:
                return False
            data = await activities.trivia_question() if kind == "trivia" else await activities.draw_cards()
            if not data:
                return False
            view = GameView(self, channel, kind, data)
            self.active[channel.id] = view
            try:
                try:
                    line = await activities.commentary(f"Invite the server to a new {kind} game.")
                except Exception:
                    line = ""
                view.message = await channel.send(line or None, embed=view.embed(), view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())
                view.arm_timer()
                await memory.log_delivery(view.message)
                await db.record_bot_activity(channel.guild.id, channel.id)
                return True
            except Exception:
                self.active.pop(channel.id, None)
                view.stop()
                raise

    async def show_leaderboard(self, channel):
        if not getattr(channel, "guild", None):
            return
        rows = await db.activity_leaderboard(channel.guild.id)
        lines = []
        for i, row in enumerate(rows, 1):
            member = channel.guild.get_member(row["user_id"])
            name = discord.utils.escape_markdown(member.display_name) if member else "Departed member"
            lines.append(f"{i}. {name}: {row['score']}")
        await channel.send(embed=discord.Embed(title="Estate Leaderboard", description="\n".join(lines) or "No scores yet."),
                           allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(ActivitiesCog(bot))
