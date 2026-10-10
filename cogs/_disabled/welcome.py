import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
from database.db import db
from utils.embeds import success_embed, info_embed
from utils.helpers import replace_placeholders

class Welcome(commands.Cog, name="Welcome & Leave (Sapphire)"):
    """Customizable welcome and goodbye messages with placeholders and embeds"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- WELCOME GROUP ---
    @commands.hybrid_group(name="welcome", description="Configure welcome announcements")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def welcome_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            settings = await db.get_guild_settings(ctx.guild.id)
            ch = ctx.guild.get_channel(settings.get("welcome_channel_id"))
            desc = (
                f"• **Status:** {'✅ Enabled' if settings.get('welcome_enabled') else '❌ Disabled'}\n"
                f"• **Channel:** {ch.mention if ch else '*Not configured*'}\n"
                f"• **Message:**\n> {settings.get('welcome_message')}"
            )
            await ctx.send(embed=info_embed("Welcome Configuration", desc))

    @welcome_group.command(name="set", description="Set welcome channel and optional custom message")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to send welcome message",
        message="Custom message (Placeholders: {user}, {user_mention}, {server}, {member_count})"
    )
    async def welcome_set(self, ctx: commands.Context, channel: discord.TextChannel, *, message: Optional[str] = None):
        await db.update_guild_setting(ctx.guild.id, "welcome_channel_id", channel.id)
        await db.update_guild_setting(ctx.guild.id, "welcome_enabled", 1)
        if message:
            await db.update_guild_setting(ctx.guild.id, "welcome_message", message)

        await ctx.send(embed=success_embed(
            "Welcome Enabled",
            f"Welcome messages will be sent to {channel.mention}.\n"
            f"**Message Template:**\n{message or 'Default template'}"
        ))

    @welcome_group.command(name="disable", description="Disable welcome messages")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def welcome_disable(self, ctx: commands.Context):
        await db.update_guild_setting(ctx.guild.id, "welcome_enabled", 0)
        await ctx.send(embed=success_embed("Welcome Disabled", "Welcome messages have been turned off."))

    @welcome_group.command(name="test", description="Send a test welcome card to verify formatting")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx: commands.Context):
        await self.send_welcome_message(ctx.author)
        await ctx.send("Test welcome message sent!", ephemeral=True)

    # --- LEAVE GROUP ---
    @commands.hybrid_group(name="leave", description="Configure member leave / goodbye messages")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def leave_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            settings = await db.get_guild_settings(ctx.guild.id)
            ch = ctx.guild.get_channel(settings.get("leave_channel_id"))
            desc = (
                f"• **Status:** {'✅ Enabled' if settings.get('leave_enabled') else '❌ Disabled'}\n"
                f"• **Channel:** {ch.mention if ch else '*Not configured*'}\n"
                f"• **Message:**\n> {settings.get('leave_message')}"
            )
            await ctx.send(embed=info_embed("Leave Message Configuration", desc))

    @leave_group.command(name="set", description="Set leave channel and optional custom message")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to send leave message",
        message="Custom message (Placeholders: {user}, {server}, {member_count})"
    )
    async def leave_set(self, ctx: commands.Context, channel: discord.TextChannel, *, message: Optional[str] = None):
        await db.update_guild_setting(ctx.guild.id, "leave_channel_id", channel.id)
        await db.update_guild_setting(ctx.guild.id, "leave_enabled", 1)
        if message:
            await db.update_guild_setting(ctx.guild.id, "leave_message", message)

        await ctx.send(embed=success_embed(
            "Leave Messages Enabled",
            f"Leave messages will be sent to {channel.mention}."
        ))

    @leave_group.command(name="disable", description="Disable leave messages")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def leave_disable(self, ctx: commands.Context):
        await db.update_guild_setting(ctx.guild.id, "leave_enabled", 0)
        await ctx.send(embed=success_embed("Leave Disabled", "Leave messages have been turned off."))

    # Helper method
    async def send_welcome_message(self, member: discord.Member):
        settings = await db.get_guild_settings(member.guild.id)
        if not settings.get("welcome_enabled"):
            return

        ch_id = settings.get("welcome_channel_id")
        channel = member.guild.get_channel(ch_id) if ch_id else None
        if not channel:
            return

        template = settings.get("welcome_message") or "Welcome {user_mention} to **{server}**! Member #{member_count}"
        rendered = replace_placeholders(template, member, member.guild)

        embed = discord.Embed(
            title=f"👋 Welcome to {member.guild.name}!",
            description=rendered,
            color=0x3498DB
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count}")

        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.send_welcome_message(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        settings = await db.get_guild_settings(member.guild.id)
        if not settings.get("leave_enabled"):
            return

        ch_id = settings.get("leave_channel_id")
        channel = member.guild.get_channel(ch_id) if ch_id else None
        if not channel:
            return

        template = settings.get("leave_message") or "**{user}** has left the server."
        rendered = replace_placeholders(template, member, member.guild)

        embed = discord.Embed(
            description=rendered,
            color=0xE74C3C
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
