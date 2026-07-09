"""
Watcher Cog — Monitors messages from other bots and alerts the owner
about noteworthy events (rare spawns, daily rewards, etc.).

Strictly read-only: never interacts with other bots' commands or buttons.
Detection and friendly reminders only.

Rules are loaded from bot_rules.json for easy configuration without
code changes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

import config

log = logging.getLogger("nixi.watcher")


class WatcherCog(commands.Cog):
    """Watches other bots' messages and sends owner alerts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.rules: dict[int, list[dict[str, Any]]] = {}
        self.alert_via_dm: bool = True
        self.alert_channel_id: int | None = None
        # Cooldown tracking: {(bot_id, rule_index): last_alert_timestamp}
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._load_rules()

    def _load_rules(self) -> None:
        """Load bot-watching rules from JSON config."""
        rules_path = config.BOT_RULES_PATH
        if not rules_path.exists():
            log.info("No bot_rules.json found — watcher disabled.")
            return

        try:
            data = json.loads(rules_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Failed to load bot_rules.json")
            return

        self.alert_via_dm = data.get("alert_via_dm", True)
        self.alert_channel_id = data.get("alert_channel_id")

        for bot_config in data.get("watched_bots", []):
            bot_id = bot_config.get("bot_id")
            if bot_id:
                self.rules[int(bot_id)] = bot_config.get("rules", [])
                log.info(
                    "Watching bot %s (ID: %d) with %d rules",
                    bot_config.get("name", "Unknown"),
                    bot_id,
                    len(self.rules[int(bot_id)]),
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Only care about bot messages
        if not message.author.bot:
            return

        # Ignore our own messages
        if message.author.id == self.bot.user.id:  # type: ignore[union-attr]
            return

        # Check if this bot is being watched
        rules = self.rules.get(message.author.id)
        if not rules:
            return

        for idx, rule in enumerate(rules):
            if self._matches_rule(message, rule):
                if self._check_cooldown(message.author.id, idx, rule):
                    await self._send_alert(message, rule)

    def _matches_rule(
        self, message: discord.Message, rule: dict[str, Any]
    ) -> bool:
        """Check if a message matches a single watching rule."""
        rule_type = rule.get("type", "")
        pattern = rule.get("pattern", "")

        if rule_type == "content_contains":
            content = (message.content or "").lower()
            if re.search(pattern, content, re.IGNORECASE):
                # Check rarity filter if present
                rarity_keywords = rule.get("rarity_keywords")
                if rarity_keywords:
                    return any(kw.lower() in content for kw in rarity_keywords)
                return True

        elif rule_type == "embed_title_contains":
            for embed in message.embeds:
                title = (embed.title or "").lower()
                description = (embed.description or "").lower()
                combined = f"{title} {description}"
                if re.search(pattern, combined, re.IGNORECASE):
                    rarity_keywords = rule.get("rarity_keywords")
                    if rarity_keywords:
                        return any(
                            kw.lower() in combined for kw in rarity_keywords
                        )
                    return True

        elif rule_type == "embed_field_contains":
            for embed in message.embeds:
                for field in embed.fields:
                    field_text = f"{field.name} {field.value}".lower()
                    if re.search(pattern, field_text, re.IGNORECASE):
                        rarity_keywords = rule.get("rarity_keywords")
                        if rarity_keywords:
                            return any(
                                kw.lower() in field_text
                                for kw in rarity_keywords
                            )
                        return True

        return False

    def _check_cooldown(
        self, bot_id: int, rule_idx: int, rule: dict[str, Any]
    ) -> bool:
        """Returns True if the alert is not on cooldown."""
        cooldown = rule.get("cooldown_seconds", 0)
        if cooldown <= 0:
            return True

        key = (bot_id, rule_idx)
        now = time.time()
        last = self._cooldowns.get(key, 0)
        if now - last < cooldown:
            return False

        self._cooldowns[key] = now
        return True

    async def _send_alert(
        self, message: discord.Message, rule: dict[str, Any]
    ) -> None:
        """Send a butler-style alert to the owner."""
        alert_template = rule.get(
            "alert", "Something noteworthy has occurred, Young Master."
        )

        # Try to extract some details from the message for the template
        details = message.content or ""
        if not details and message.embeds:
            embed = message.embeds[0]
            details = embed.title or embed.description or ""

        alert_text = alert_template.replace("{details}", details[:200])
        channel_info = ""
        if message.guild and hasattr(message.channel, "name"):
            channel_info = (
                f"\n-# *Observed in #{message.channel.name}, "  # type: ignore[union-attr]
                f"{message.guild.name}*"
            )

        full_alert = f"{alert_text}{channel_info}"

        try:
            if self.alert_via_dm:
                owner = await self.bot.fetch_user(config.OWNER_ID)
                await owner.send(full_alert)
                log.info("Alert sent via DM: %.80s", alert_text)
            elif self.alert_channel_id:
                channel = self.bot.get_channel(self.alert_channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    owner_mention = f"<@{config.OWNER_ID}>"
                    await channel.send(f"{owner_mention} {full_alert}")
                    log.info("Alert sent to channel: %.80s", alert_text)
        except discord.Forbidden:
            log.warning("Cannot send alert — missing permissions")
        except Exception:
            log.exception("Failed to send alert")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WatcherCog(bot))
