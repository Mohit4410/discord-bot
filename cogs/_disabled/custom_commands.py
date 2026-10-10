import discord
from discord.ext import commands
from discord import app_commands
from typing import Literal
import aiosqlite
from database.db import db
from utils.embeds import success_embed, error_embed, info_embed
from utils.helpers import replace_placeholders

class CustomCommands(commands.Cog, name="Custom Commands & Triggers (Sapphire)"):
    """Automated triggers, custom commands and auto-responders"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="customcommand", aliases=["cc", "autoresponder"], description="Manage custom triggers and auto responses")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def cc_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self.list_commands(ctx)

    @cc_group.command(name="add", description="Add an auto-response trigger")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        trigger="Phrase or word that activates response",
        response="Message to reply with (supports {user_mention}, {server}, etc.)",
        match_type="How to match the trigger"
    )
    async def add_command(
        self,
        ctx: commands.Context,
        trigger: str,
        response: str,
        match_type: Literal["exact", "contains", "startswith"] = "exact"
    ):
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
            INSERT INTO custom_commands (guild_id, trigger, response, match_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, trigger) DO UPDATE SET
                response = excluded.response,
                match_type = excluded.match_type
            """, (ctx.guild.id, trigger.lower().strip(), response, match_type))
            await conn.commit()

        await ctx.send(embed=success_embed(
            "Trigger Saved",
            f"Trigger: `{trigger.lower().strip()}` ({match_type})\n"
            f"Response: {response}"
        ))

    @cc_group.command(name="remove", description="Remove an auto-response trigger")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(trigger="Trigger to remove")
    async def remove_command(self, ctx: commands.Context, trigger: str):
        async with aiosqlite.connect(db.db_path) as conn:
            cursor = await conn.execute(
                "DELETE FROM custom_commands WHERE guild_id = ? AND trigger = ?",
                (ctx.guild.id, trigger.lower().strip())
            )
            await conn.commit()
            count = cursor.rowcount

        if count > 0:
            await ctx.send(embed=success_embed("Trigger Removed", f"Deleted trigger `{trigger}`."))
        else:
            await ctx.send(embed=error_embed("Not Found", f"No trigger found matching `{trigger}`."), ephemeral=True)

    @cc_group.command(name="list", description="List all custom commands in this server")
    async def list_commands(self, ctx: commands.Context):
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT trigger, response, match_type FROM custom_commands WHERE guild_id = ?", (ctx.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            return await ctx.send(embed=info_embed("Custom Commands", "No custom triggers configured."))

        lines = [f"• `{r['trigger']}` ({r['match_type']}) → {r['response'][:40]}..." for r in rows[:20]]
        await ctx.send(embed=info_embed("Custom Commands", "\n".join(lines)))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        content_lower = message.content.lower().strip()
        if not content_lower:
            return

        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT trigger, response, match_type FROM custom_commands WHERE guild_id = ?", (message.guild.id,))
            commands_list = await cursor.fetchall()

        for cmd in commands_list:
            trig = cmd["trigger"]
            match_type = cmd["match_type"]
            matched = False

            if match_type == "exact" and content_lower == trig:
                matched = True
            elif match_type == "startswith" and content_lower.startswith(trig):
                matched = True
            elif match_type == "contains" and trig in content_lower:
                matched = True

            if matched:
                rendered = replace_placeholders(cmd["response"], message.author, message.guild)
                try:
                    await message.channel.send(rendered)
                except Exception:
                    pass
                break

async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
