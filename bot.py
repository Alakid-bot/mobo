#!/usr/bin/env python3
"""
1812 — Discord chatbot powered by OpenAI
"""

import os
import json
import discord
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are 1812, a helpful and friendly Discord chatbot.")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
openai = AsyncOpenAI(api_key=OPENAI_API_KEY)

HISTORY_FILE = Path("history.json")


def load_history() -> dict[int, list[dict]]:
    if HISTORY_FILE.exists():
        raw = json.loads(HISTORY_FILE.read_text())
        return {int(k): v for k, v in raw.items()}
    return {}


def save_history(history: dict[int, list[dict]]):
    HISTORY_FILE.write_text(json.dumps({str(k): v for k, v in history.items()}, indent=2))


# Per-channel conversation history (persisted to history.json)
history: dict[int, list[dict]] = load_history()


@bot.event
async def on_ready():
    print(f"1812 online as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    print(f"[MSG] {message.author}: {message.content!r}")

    if message.author.bot:
        return

    # Only respond when mentioned (user or role) or in DMs
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions
    bot_role_ids = {r.id for r in message.guild.me.roles} if message.guild else set()
    is_role_mentioned = any(r.id in bot_role_ids for r in message.role_mentions)
    print(f"[MSG] is_dm={is_dm} is_mentioned={is_mentioned} is_role_mentioned={is_role_mentioned}")

    if not is_dm and not is_mentioned and not is_role_mentioned:
        await bot.process_commands(message)
        return

    # Strip the mention from the message content
    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    print(f"[MSG] content after strip: {content!r}")
    if not content:
        await message.reply("Yeah?")
        return

    channel_id = message.channel.id
    if channel_id not in history:
        history[channel_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    history[channel_id].append({"role": "user", "content": content})

    async with message.channel.typing():
        try:
            response = await openai.chat.completions.create(
                model=MODEL,
                messages=history[channel_id],
                max_tokens=1024,
            )
            reply = response.choices[0].message.content
            history[channel_id].append({"role": "assistant", "content": reply})

            # Keep history to last 20 messages to avoid token bloat
            if len(history[channel_id]) > 21:  # 1 system + 20 messages
                history[channel_id] = [history[channel_id][0]] + history[channel_id][-20:]

            save_history(history)
            await message.reply(reply)
        except Exception as e:
            await message.reply(f"Something went wrong: {type(e).__name__}")
            raise


@bot.command(name="clear")
async def clear_history(ctx: commands.Context):
    """Clear conversation history for this channel."""
    history.pop(ctx.channel.id, None)
    save_history(history)
    await ctx.send("Conversation history cleared.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
