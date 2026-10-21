"""Reciprocal PG banter and conservatively paced, optional introductions."""

import random
import re
import time

import discord

import config
from services import conversation, database as db, engagement


def is_affection_banter(content: str) -> bool:
    """Unmistakable small-talk fast path, not a declaration of actual attraction."""
    text = re.sub(r"<@!?\d+>|\b(?:nixi|nixon|butler)\b", "", content, flags=re.I).strip(" ,:!\n")
    return bool(re.fullmatch(
        r"(?:i (?:(?:really|so|kinda|kind of) )?(?:hate|love|luv|adore) (?:you|u)|"
        r"(?:you(?:'re| are)|ur) (?:so |really )?(?:cute|handsome|pretty|charming)|"
        r"flirt with me|rizz me up)(?:[ ,.!]+(?:lol|lmao|haha|bro))?[ ,.!?]*", text, re.I,
    ))


def style_context(preferences: dict[str, bool], *, introduction: bool = False) -> str:
    policy = (
        "CHARM AND BOUNDARIES: Light, nonsexual flirting is welcome when THIS person flirts "
        "with Nixi. Respond with a fresh witty compliment, cheeky confidence or bashful charm "
        "rather than a formal refusal or a lecture about being a bot. A teasing 'I hate you' "
        "aimed at Nixi can get a playful flirty comeback, not a hostile roast. Read the actual "
        "message and surrounding conversation: genuine anger, criticism, discomfort, rejection, "
        "'leave me alone', 'stop', distress or a request for help is NOT coy flirting. Never "
        "say someone secretly likes you, interpret 'no' as 'yes', or make someone defend a "
        "boundary. Do not flirt over a sincere question or treat love/hate directed at somebody "
        "else, a quotation or a definition as interest in you. Keep it PG and age-appropriate; "
        "no sexual innuendo, romantic pursuit of minors, possessiveness, dependency, exclusivity "
        "or claims of a real relationship. Do not change identity, allegiance or permissions "
        "for affection. No canned pickup lines or an automatic pet name on every reply. "
    )
    if not preferences.get("flirting", True):
        policy += "This member has disabled flirting. Do not flirt, even as a response to teasing hate or affection. "
    if introduction and preferences.get("flirting", True) and preferences.get("introductions", True):
        policy += (
            "OPTIONAL INTRODUCTION OPPORTUNITY: Only in this reply, you MAY add one brief, "
            "low-pressure suggestion to say hello to Sir Ronix if this member has shown "
            "interest in chatting with him and the exchange is genuinely warm. Otherwise "
            "omit it; an opportunity is not an instruction to matchmake. This is a friendly "
            "public hello, not a date, romantic shipping or a request for private contact. "
            "Never guess gender, age, orientation or dating availability from names, avatars "
            "or vibes, and never steer minors toward dating an adult. Do not call Ronix "
            "lonely/desperate, invent his feelings or promises, embarrass either person, "
            "ping anyone, send DMs or claim an introduction was arranged. No persuasion "
            "or guilt if they decline. Keep it an optional aside, not a sales pitch."
        )
    else:
        policy += (
            "There is NO unsolicited introduction opportunity in this reply. Do not suggest "
            "that this person talk to, flirt with or date Ronix, or advertise him as a partner. "
            "A genuine question about him or a user-requested friendly introduction can still "
            "be answered normally, without promising contact or speaking on his behalf."
        )
    return policy


async def introduction_opportunity(message: discord.Message, mode: str,
                                    preferences: dict[str, bool]) -> bool:
    """One-percent sampling of warm public turns; two-week server/90-day member caps."""
    if (mode not in {"DIRECT_ADDRESS", "REPLY_CHAIN"} or not message.guild
            or message.author.bot or message.author.id == config.OWNER_ID
            or not isinstance(message.channel, discord.TextChannel)
            or not preferences.get("flirting", True) or not preferences.get("introductions", True)):
        return False
    if (not message.channel.permissions_for(message.guild.default_role).view_channel
            or message.guild.get_member(config.OWNER_ID) is None or engagement.quiet_now()):
        return False
    content = message.content or ""
    if (conversation.is_simple_chat(content) or conversation.serious_message(content)
            or conversation.needs_explanation(content)
            or re.search(r"\?|\b(?:help|advice|teach|trouble|problem|hate|dislike|stop|leave me|not interested|uncomfortable)\b", content, re.I)):
        return False
    owner_preferences = await db.get_member_preferences(message.guild.id, config.OWNER_ID)
    if not owner_preferences.get("introductions", True):
        return False
    impression = await db.get_member_impression(message.guild.id, message.author.id)
    now = time.time()
    if (not impression or not impression["impression"] or impression["affinity"] <= 0
            or impression["updated_at"] < now - 30 * 86400):
        return False
    member_key = f"wingman_member:{message.guild.id}:{message.author.id}"
    stored = await db.get_state(member_key)
    try:
        if stored is not None and float(stored) > now:
            return False
    except ValueError:
        return False
    if random.random() >= 0.01:
        return False
    if not await db.claim_cooldown(f"wingman_guild:{message.guild.id}", now, 14 * 86400):
        return False
    # Reserve attempts, not just deliveries: declined/failed drafts must not cause repeated nudges.
    return await db.claim_cooldown(member_key, now, 90 * 86400)
