import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
import aiosqlite
from database.db import db
from utils.embeds import success_embed, info_embed

class Starboard(commands.Cog, name="Starboard (Sapphire)"):
    """Starboard system for highlighting top community messages"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="starboard", description="Configure server Starboard")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    async def starboard_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            settings = await db.get_guild_settings(ctx.guild.id)
            ch = ctx.guild.get_channel(settings.get("starboard_channel_id"))
            desc = (
                f"• **Status:** {'✅ Enabled' if settings.get('starboard_enabled') else '❌ Disabled'}\n"
                f"• **Channel:** {ch.mention if ch else '*Not configured*'}\n"
                f"• **Threshold:** {settings.get('starboard_threshold', 3)} ⭐"
            )
            await ctx.send(embed=info_embed("Starboard Settings", desc))

    @starboard_group.command(name="set", description="Set starboard channel and star threshold")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel where starred messages will be posted", threshold="Number of ⭐ reactions required (default 3)")
    async def starboard_set(self, ctx: commands.Context, channel: discord.TextChannel, threshold: int = 3):
        await db.update_guild_setting(ctx.guild.id, "starboard_channel_id", channel.id)
        await db.update_guild_setting(ctx.guild.id, "starboard_threshold", max(1, threshold))
        await db.update_guild_setting(ctx.guild.id, "starboard_enabled", 1)

        await ctx.send(embed=success_embed(
            "Starboard Configured",
            f"Starred messages will be posted to {channel.mention} once they receive **{threshold}** ⭐ reactions."
        ))

    @starboard_group.command(name="disable", description="Disable Starboard")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    async def starboard_disable(self, ctx: commands.Context):
        await db.update_guild_setting(ctx.guild.id, "starboard_enabled", 0)
        await ctx.send(embed=success_embed("Starboard Disabled", "Starboard has been turned off."))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != "⭐" or not payload.guild_id:
            return

        settings = await db.get_guild_settings(payload.guild_id)
        if not settings.get("starboard_enabled"):
            return

        starboard_ch_id = settings.get("starboard_channel_id")
        if not starboard_ch_id:
            return

        threshold = settings.get("starboard_threshold", 3)
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        starboard_channel = guild.get_channel(starboard_ch_id)
        if not starboard_channel:
            return

        source_channel = guild.get_channel(payload.channel_id)
        if not source_channel:
            return

        # Prevent starring starboard channel itself
        if source_channel.id == starboard_channel.id:
            return

        try:
            msg = await source_channel.fetch_message(payload.message_id)
        except Exception:
            return

        # Find count of ⭐
        star_reaction = next((r for r in msg.reactions if str(r.emoji) == "⭐"), None)
        star_count = star_reaction.count if star_reaction else 0

        if star_count < threshold:
            return

        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT starboard_message_id FROM starboard_messages WHERE original_message_id = ?", (msg.id,))
            record = await cursor.fetchone()

            embed = discord.Embed(
                description=msg.content or "*[Embed or Attachment]*",
                color=0xF1C40F,
                timestamp=msg.created_at
            )
            embed.set_author(name=msg.author.display_name, icon_url=msg.author.avatar.url if msg.author.avatar else None)
            embed.add_field(name="Source", value=f"[Jump to Message]({msg.jump_url}) in {source_channel.mention}")

            if msg.attachments:
                embed.set_image(url=msg.attachments[0].url)

            content = f"⭐ **{star_count}** | {source_channel.mention}"

            if record:
                # Update existing starboard message
                try:
                    sb_msg = await starboard_channel.fetch_message(record["starboard_message_id"])
                    await sb_msg.edit(content=content, embed=embed)
                    await conn.execute("UPDATE starboard_messages SET stars = ? WHERE original_message_id = ?", (star_count, msg.id))
                    await conn.commit()
                except Exception:
                    pass
            else:
                # Post new starboard message
                sb_msg = await starboard_channel.send(content=content, embed=embed)
                await conn.execute("""
                INSERT INTO starboard_messages (original_message_id, starboard_message_id, channel_id, guild_id, stars)
                VALUES (?, ?, ?, ?, ?)
                """, (msg.id, sb_msg.id, source_channel.id, guild.id, star_count))
                await conn.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
