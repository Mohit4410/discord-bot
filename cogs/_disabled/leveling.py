import discord
from discord.ext import commands
from discord import app_commands
import time
import random
from typing import Optional
from database.db import db
from utils.embeds import success_embed, info_embed

def render_progress_bar(current: int, total: int, length: int = 15) -> str:
    """Render a clean text progress bar like [████░░░░░░]"""
    if total <= 0:
        percent = 1.0
    else:
        percent = min(1.0, max(0.0, current / total))
    filled = int(round(length * percent))
    bar = "█" * filled + "░" * (length - filled)
    return f"`[{bar}]` {int(percent * 100)}%"

class Leveling(commands.Cog, name="Leveling & XP (Sapphire)"):
    """XP progression, rank cards, and server leaderboards"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.xp_cooldown = {} # (guild_id, user_id) -> timestamp

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        # Simple command bypass
        if message.content.startswith(("!", "/", "?", ".")):
            return

        settings = await db.get_guild_settings(message.guild.id)
        if not settings.get("leveling_enabled", 1):
            return

        now = time.time()
        key = (message.guild.id, message.author.id)
        last_time = self.xp_cooldown.get(key, 0)

        # 60s cooldown between XP gains
        if now - last_time < 60:
            return

        self.xp_cooldown[key] = now
        xp_gain = random.randint(15, 25)
        new_level, current_xp, leveled_up = await db.add_xp(message.guild.id, message.author.id, xp_gain, now)

        if leveled_up:
            target_ch_id = settings.get("level_channel_id", 0)
            target_ch = message.guild.get_channel(target_ch_id) if target_ch_id else message.channel
            if target_ch:
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=f"Congratulations {message.author.mention}, you reached **Level {new_level}**!",
                    color=0xF1C40F
                )
                if message.author.avatar:
                    embed.set_thumbnail(url=message.author.avatar.url)
                try:
                    await target_ch.send(embed=embed)
                except Exception:
                    pass

    @commands.hybrid_command(name="rank", description="Check your or another member's rank and XP")
    @app_commands.describe(user="The member to inspect (defaults to you)")
    async def rank(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        data = await db.get_user_level(ctx.guild.id, target.id)
        rank_num = await db.get_user_rank(ctx.guild.id, target.id)

        level = data["level"]
        xp = data["xp"]
        msgs = data["messages_count"]

        # Calculate XP needed for next level:
        # level = int((xp / 100) ** 0.6) => xp = 100 * (level ** (1 / 0.6))
        next_level_xp = int(100 * ((level + 1) ** (1 / 0.6)))
        curr_level_xp = int(100 * (level ** (1 / 0.6)))
        progress_in_level = max(0, xp - curr_level_xp)
        needed_for_level = max(1, next_level_xp - curr_level_xp)

        embed = discord.Embed(
            title=f"📊 Rank Card — {target.display_name}",
            color=0x3498DB
        )
        if target.avatar:
            embed.set_thumbnail(url=target.avatar.url)

        embed.add_field(name="Server Rank", value=f"#{rank_num}", inline=True)
        embed.add_field(name="Level", value=f"{level}", inline=True)
        embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
        embed.add_field(name="Messages Sent", value=f"{msgs:,}", inline=True)
        embed.add_field(
            name="Progress to Level " + str(level + 1),
            value=f"{render_progress_bar(progress_in_level, needed_for_level)}\n({progress_in_level:,} / {needed_for_level:,} XP)",
            inline=False
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"], description="Display the server XP leaderboard")
    async def leaderboard(self, ctx: commands.Context):
        top_users = await db.get_leaderboard(ctx.guild.id, limit=10)
        if not top_users:
            return await ctx.send(embed=info_embed("Leaderboard", "No XP recorded yet for this server."))

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for idx, row in enumerate(top_users):
            badge = medals[idx] if idx < 3 else f"`#{idx + 1}`"
            member = ctx.guild.get_member(row["user_id"])
            name = member.mention if member else f"`User ID: {row['user_id']}`"
            lines.append(f"{badge} {name} — **Level {row['level']}** (`{row['xp']:,}` XP)")

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} — XP Leaderboard",
            description="\n".join(lines),
            color=0xF1C40F
        )
        await ctx.send(embed=embed)

    @commands.hybrid_group(name="leveling", description="Configure leveling system")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def leveling_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `/leveling toggle` or `/leveling channel`.", ephemeral=True)

    @leveling_group.command(name="toggle", description="Enable or disable the XP and leveling system")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn on or off")
    async def leveling_toggle(self, ctx: commands.Context, enabled: bool):
        await db.update_guild_setting(ctx.guild.id, "leveling_enabled", 1 if enabled else 0)
        await ctx.send(embed=success_embed("Leveling Updated", f"Leveling system is now **{'Enabled' if enabled else 'Disabled'}**."))

    @leveling_group.command(name="channel", description="Set a specific channel for level-up announcements (or 0 for current channel)")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel for announcements (leave blank for where message is typed)")
    async def leveling_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        ch_id = channel.id if channel else 0
        await db.update_guild_setting(ctx.guild.id, "level_channel_id", ch_id)
        msg = f"Level-up announcements will be sent to {channel.mention}." if channel else "Level-up announcements will be sent in the active channel where user leveled up."
        await ctx.send(embed=success_embed("Level Channel Updated", msg))

async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
