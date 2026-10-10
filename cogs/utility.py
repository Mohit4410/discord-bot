import discord
from discord.ext import commands, tasks
from discord import app_commands
import time
import asyncio
from typing import Optional
import aiosqlite
from database.db import db
from utils.embeds import info_embed, success_embed, error_embed
from utils.helpers import parse_duration, format_duration

class Utility(commands.Cog, name="Utility & Info (Sapphire)"):
    """Server info, user info, avatar, polls, reminders, and bot stats"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    # Loop to dispatch pending reminders
    @tasks.loop(seconds=10)
    async def check_reminders(self):
        now = time.time()
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM reminders WHERE remind_at <= ?", (now,))
            expired = await cursor.fetchall()

            for r in expired:
                user = self.bot.get_user(r["user_id"])
                channel = self.bot.get_channel(r["channel_id"])
                msg = f"⏰ **Reminder:** {r['reminder_text']}"
                if channel:
                    try:
                        await channel.send(f"<@{r['user_id']}> {msg}")
                    except Exception:
                        if user:
                            try:
                                await user.send(msg)
                            except Exception:
                                pass
                elif user:
                    try:
                        await user.send(msg)
                    except Exception:
                        pass

                await conn.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
            if expired:
                await conn.commit()

    @check_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

    # 1. PING
    @commands.hybrid_command(name="ping", description="Check bot latency and API response time")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! Latency: `{latency_ms}ms`")

    # 2. BOT INFO
    @commands.hybrid_command(name="botinfo", description="Display bot system information")
    async def botinfo(self, ctx: commands.Context):
        guild_count = len(self.bot.guilds)
        user_count = sum(g.member_count or 0 for g in self.bot.guilds)

        embed = discord.Embed(
            title="🤖 Moveer & Sapphire All-in-One Bot",
            description="A unified Discord bot combining the full power of Moveer voice suite & Sapphire modular server management.",
            color=0x5865F2
        )
        embed.add_field(name="Servers", value=f"{guild_count:,}", inline=True)
        embed.add_field(name="Members", value=f"{user_count:,}", inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Prefix", value="`/` or `!`", inline=True)
        embed.set_footer(text="Created with discord.py & aiosqlite")
        await ctx.send(embed=embed)

    # 3. SERVER INFO
    @commands.hybrid_command(name="serverinfo", description="Get comprehensive server statistics")
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        owner = g.owner or await self.bot.fetch_user(g.owner_id)

        text_chans = len(g.text_channels)
        voice_chans = len(g.voice_channels)
        categories = len(g.categories)
        roles_count = len(g.roles)

        embed = discord.Embed(title=f"🏰 Server Info — {g.name}", color=0x3498DB)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)

        embed.add_field(name="Owner", value=f"{owner.mention} (`{owner.id}`)", inline=True)
        embed.add_field(name="Server ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="Created At", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Members", value=f"{g.member_count:,}", inline=True)
        embed.add_field(name="Roles", value=f"{roles_count}", inline=True)
        embed.add_field(name="Channels", value=f"💬 {text_chans} | 🔊 {voice_chans} | 📁 {categories}", inline=True)
        embed.add_field(name="Boost Level", value=f"Tier {g.premium_tier} ({g.premium_subscription_count} boosts)", inline=True)

        await ctx.send(embed=embed)

    # 4. USER INFO
    @commands.hybrid_command(name="userinfo", description="Get information about a member")
    @app_commands.describe(user="The member to view (defaults to you)")
    async def userinfo(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        roles = [r.mention for r in reversed(target.roles) if r.name != "@everyone"]

        embed = discord.Embed(title=f"👤 User Info — {target.display_name}", color=target.color or 0x3498DB)
        if target.avatar:
            embed.set_thumbnail(url=target.avatar.url)

        embed.add_field(name="Username", value=f"{target} (`{target.id}`)", inline=False)
        embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:R>" if target.joined_at else "Unknown", inline=True)
        embed.add_field(name="Top Role", value=target.top_role.mention, inline=True)
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:20]) if roles else "None", inline=False)

        await ctx.send(embed=embed)

    # 5. AVATAR
    @commands.hybrid_command(name="avatar", description="Display a user's avatar in full resolution")
    @app_commands.describe(user="The user whose avatar you want to view")
    async def avatar(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        avatar_url = target.display_avatar.url

        embed = discord.Embed(title=f"🖼️ Avatar of {target.display_name}", color=0x3498DB)
        embed.set_image(url=avatar_url)
        await ctx.send(embed=embed)

    # 6. POLL
    @commands.hybrid_command(name="poll", description="Create an interactive poll")
    @app_commands.describe(
        question="Poll question",
        options="Comma-separated options (up to 10 options, e.g. Yes, No, Maybe)"
    )
    async def poll(self, ctx: commands.Context, question: str, options: str):
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2 or len(opts) > 10:
            return await ctx.send(embed=error_embed("Invalid Options", "Provide between 2 and 10 comma-separated options."), ephemeral=True)

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        desc = []
        for i, opt in enumerate(opts):
            desc.append(f"{emojis[i]} **{opt}**")

        embed = discord.Embed(
            title=f"📊 {question}",
            description="\n\n".join(desc),
            color=0x3498DB
        )
        embed.set_footer(text=f"Poll created by {ctx.author.display_name}")

        msg = await ctx.send(embed=embed)
        for i in range(len(opts)):
            await msg.add_reaction(emojis[i])

    # 7. REMIND
    @commands.hybrid_command(name="remind", aliases=["reminder"], description="Set a timer reminder")
    @app_commands.describe(duration="Time delay (e.g. 10m, 1h, 2d)", text="What to remind you about")
    async def remind(self, ctx: commands.Context, duration: str, *, text: str):
        seconds = parse_duration(duration)
        if not seconds or seconds < 10:
            return await ctx.send(embed=error_embed("Invalid Duration", "Provide a time format like `10m`, `2h`, `1d` (min 10s)."), ephemeral=True)

        remind_at = time.time() + seconds
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
            INSERT INTO reminders (user_id, channel_id, reminder_text, remind_at)
            VALUES (?, ?, ?, ?)
            """, (ctx.author.id, ctx.channel.id, text, remind_at))
            await conn.commit()

        await ctx.send(embed=success_embed(
            "Reminder Set",
            f"I will remind you about: **{text}** in **{format_duration(seconds)}**."
        ))

async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
