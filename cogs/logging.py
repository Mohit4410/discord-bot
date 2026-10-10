import io
import os
import re
import json
import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Literal
from database.db import db
from utils.embeds import log_embed, success_embed, info_embed, COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING, COLOR_INFO, COLOR_VOICE
from utils.transcript import generate_html_transcript
from utils.media_manager import cache_message_media, get_cached_media

LOG_CATEGORIES = ["message", "voice", "member", "mod", "server"]

PURGE_CMD_PATTERN = re.compile(
    r"^[\/=!?.$\-~;+]\s*(purge|clear|userpurge|purgeuser|puser|restore|cleanbot|clean|prune|wipe)\b",
    re.IGNORECASE
)

def is_moderation_command_message(content: str) -> bool:
    if not content:
        return False
    return bool(PURGE_CMD_PATTERN.match(content.strip()))

class CachedAvatarWrapper:
    def __init__(self, url: str):
        self.url = url

class CachedAuthorWrapper:
    def __init__(self, data: dict):
        self.id = data.get("author_id", 0)
        self.display_name = data.get("author_name", "Unknown")
        self.name = data.get("author_username", data.get("author_name", "Unknown"))
        self.bot = data.get("is_bot", False)
        self.mention = f"<@{self.id}>"
        avatar_url = data.get("author_avatar_url") or "https://cdn.discordapp.com/embed/avatars/0.png"
        self.display_avatar = CachedAvatarWrapper(avatar_url)

    def __str__(self):
        return self.name

class CachedAttachmentWrapper:
    def __init__(self, data):
        if isinstance(data, dict):
            self.filename = data.get("filename", "file")
            self.url = data.get("url", "")
            self.size = data.get("size", 0)
        else:
            self.url = str(data)
            self.filename = self.url.split("/")[-1].split("?")[0]
            self.size = 0

class CachedMessageWrapper:
    def __init__(self, data: dict):
        self.id = data["id"]
        self.content = data.get("content", "")
        self.created_at = data.get("created_at") or datetime.now(timezone.utc)
        self.author = CachedAuthorWrapper(data)
        self.attachments = [CachedAttachmentWrapper(a) for a in data.get("attachments", [])]
        self.embeds = []
        for e in data.get("embeds", []):
            try:
                self.embeds.append(discord.Embed.from_dict(e))
            except Exception:
                pass
        self.webhook_id = data.get("webhook_id")

class Logging(commands.Cog, name="Audit Logging (Sapphire)"):
    """Comprehensive event logging: messages, voice activity, member events, roles & channels"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_channel_for_log(self, guild: discord.Guild, log_type: str) -> Optional[discord.TextChannel]:
        channel_id = await db.get_log_channel(guild.id, log_type)
        if channel_id:
            return guild.get_channel(channel_id)
        # Fallback to general 'server' or 'all' if configured
        fallback_id = await db.get_log_channel(guild.id, "all")
        if fallback_id:
            return guild.get_channel(fallback_id)
        return None

    @commands.hybrid_group(name="logs", description="Configure audit and event logging channels")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def logs_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self.status(ctx)

    @logs_group.command(name="setup", description="Automatically create a private log category and channels for all log types")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    @app_commands.describe(category_name="Optional name for the log category (default: '📋 LOGS')")
    async def setup_logs(self, ctx: commands.Context, category_name: str = "📋 LOGS"):
        """Creates a private category and creates + links all log channels automatically."""
        await ctx.defer()
        guild = ctx.guild

        # Private category overwrites: hidden from @everyone, full access for bot
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True)
        }

        try:
            category = await guild.create_category(
                name=category_name,
                overwrites=overwrites,
                reason=f"Logs auto-setup requested by {ctx.author}"
            )
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Permission Error", "Bot lacks `Manage Channels` permission to create a category."), ephemeral=True)
        except Exception as e:
            return await ctx.send(embed=error_embed("Error", f"Failed to create category: {e}"), ephemeral=True)

        channels_info = [
            ("message", "💬┃message-logs", "Audit logs for deleted, edited, and purged messages"),
            ("voice", "🔊┃voice-logs", "Audit logs for voice joins, leaves, moves, and mutes/deafens"),
            ("member", "👤┃member-logs", "Audit logs for member joins, leaves, role updates, and nicknames"),
            ("mod", "🛡️┃mod-logs", "Audit logs for warnings, timeouts, kicks, and bans"),
            ("server", "⚙️┃server-logs", "Audit logs for channels and roles created or deleted"),
        ]

        created_channels = []
        for log_type, ch_name, topic in channels_info:
            try:
                ch = await guild.create_text_channel(
                    name=ch_name,
                    category=category,
                    topic=topic,
                    reason=f"Auto logs setup ({log_type})"
                )
                await db.set_log_channel(guild.id, log_type, ch.id)
                created_channels.append((log_type, ch))

                # Send an initial welcome/status embed into each created channel
                intro_embed = log_embed(
                    f"📌 #{ch.name} Configured",
                    f"All **{log_type.upper()}** events in **{guild.name}** will be automatically tracked and logged here.\n\n"
                    f"• **Category:** {category.name}\n"
                    f"• **Channel ID:** `{ch.id}`\n"
                    f"• **Setup by:** {ctx.author.mention}",
                    color=COLOR_INFO
                )
                await ch.send(embed=intro_embed)
            except Exception:
                pass

        desc = (
            f"Successfully created category **{category.name}** and configured **{len(created_channels)}** logging channels:\n\n"
            + "\n".join([f"• **{log_type.capitalize()} Logs:** {ch.mention}" for log_type, ch in created_channels])
            + "\n\n🔒 *The category permissions are automatically set to private, preventing regular members from reading logs.*"
        )
        await ctx.send(embed=success_embed("Logs Setup Complete", desc))

    @logs_group.command(name="set", description="Set log channel for an event category")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        category="Category: message, voice, member, mod, server, or all",
        channel="Text channel to receive the logs"
    )
    async def set_log(self, ctx: commands.Context, category: Literal["message", "voice", "member", "mod", "server", "all"], channel: discord.TextChannel):
        await db.set_log_channel(ctx.guild.id, category, channel.id)
        await ctx.send(embed=success_embed("Log Channel Configured", f"Logs for **{category}** will now be sent to {channel.mention}."))

    @logs_group.command(name="status", description="View all configured log channels")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        lines = []
        for cat in ["all", "message", "voice", "member", "mod", "server"]:
            ch_id = await db.get_log_channel(ctx.guild.id, cat)
            ch = ctx.guild.get_channel(ch_id) if ch_id else None
            status = ch.mention if ch else "*Not Configured*"
            lines.append(f"• **{cat.capitalize()} Logs:** {status}")

        await ctx.send(embed=info_embed("Audit Logging Channels", "\n".join(lines)))

    # --- MESSAGE LOGS ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or (self.bot.user and message.author.id == self.bot.user.id):
            return

        # Store in rolling message cache so uncached bulk-deletes from any bot can be recovered
        if hasattr(self.bot, "store_cached_message"):
            self.bot.store_cached_message(message)

        # Cache attachments from all users and other bots
        if message.attachments:
            asyncio.create_task(cache_message_media(message))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Suppress log if message was part of a purge or bot invocation
        ignored = getattr(self.bot, "ignored_message_ids", set())
        if payload.message_id in ignored:
            return

        ch = await self.get_channel_for_log(guild, "message")
        if not ch:
            return

        msg = payload.cached_message
        if not msg:
            custom_cache = getattr(self.bot, "message_cache", {})
            if payload.message_id in custom_cache:
                msg = CachedMessageWrapper(custom_cache[payload.message_id])

        channel_obj = guild.get_channel(payload.channel_id)
        ch_mention = channel_obj.mention if channel_obj else f"<#{payload.channel_id}>"

        if msg:
            # Never log our own bot's internal messages
            if self.bot.user and msg.author.id == self.bot.user.id:
                return

            avatar_url = msg.author.display_avatar.url if hasattr(msg.author, "display_avatar") else ""
            author_tag = f"{msg.author.mention} 🤖 (`{msg.author.id}`)" if msg.author.bot else f"{msg.author.mention} (`{msg.author.id}`)"
            title = "🤖 Bot Message Deleted" if msg.author.bot else "🗑️ Message Deleted"

            embed = log_embed(
                title,
                f"**Author:** {author_tag}\n"
                f"**Channel:** {ch_mention}\n\n"
                f"**Content:**\n{msg.content or '*[Empty or Embed/Attachment]*'}",
                color=COLOR_ERROR,
                thumbnail=avatar_url,
                author_name=f"{msg.author.display_name} ({msg.author})",
                author_icon=avatar_url
            )

            # Retrieve cached media files (images, videos, attachments)
            cached_media = get_cached_media(payload.message_id)
            files = []
            if cached_media:
                for item in cached_media:
                    fp = item["file_path"]
                    if os.path.exists(fp):
                        display_name = item["filename"]
                        file_obj = discord.File(fp, filename=display_name)
                        files.append(file_obj)
                        if item.get("is_image") and not embed.image.url:
                            embed.set_image(url=f"attachment://{display_name}")
                embed.add_field(name="Media Saved", value=f"✅ Preserved **{len(files)}** attachment file(s) below.")
            elif msg.attachments:
                embed.add_field(name="Attachments", value="\n".join([a.url for a in msg.attachments]))

            # Save to purged_messages so /restore can restore even single deleted messages
            media_paths = ",".join([item["file_path"] for item in cached_media]) if cached_media else ""
            att_urls = ",".join([a.url for a in msg.attachments]) if msg.attachments else ""
            embeds_json = ""
            if msg.embeds:
                try:
                    embeds_json = json.dumps([e.to_dict() for e in msg.embeds])
                except Exception:
                    pass

            is_wh = 1 if getattr(msg, "webhook_id", None) is not None else (1 if msg.author.bot else 0)
            await db.save_purged_messages([{
                "guild_id": guild.id,
                "channel_id": payload.channel_id,
                "author_id": msg.author.id,
                "author_name": msg.author.display_name,
                "author_avatar_url": avatar_url,
                "content": msg.content or "",
                "attachments": att_urls,
                "media_files": media_paths,
                "embeds": embeds_json,
                "is_webhook": is_wh,
                "timestamp": msg.created_at.timestamp()
            }])

            try:
                if files:
                    await ch.send(embed=embed, files=files)
                else:
                    await ch.send(embed=embed)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Check if already processed by bot's own purge command
        ignored = getattr(self.bot, "ignored_message_ids", set())
        unhandled_ids = [mid for mid in payload.message_ids if mid not in ignored]
        if not unhandled_ids:
            return

        ch = await self.get_channel_for_log(guild, "message")
        if not ch:
            return

        channel_obj = guild.get_channel(payload.channel_id)
        ch_mention = channel_obj.mention if channel_obj else f"<#{payload.channel_id}>"

        # 1. Discover who performed the purge via Discord Audit Logs
        purger = None
        try:
            await asyncio.sleep(0.8)
            if guild.me.guild_permissions.view_audit_log or guild.me.guild_permissions.administrator:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.message_bulk_delete, limit=10):
                    target_id = getattr(entry.target, "id", None) or getattr(entry, "_target_id", None)
                    if target_id == payload.channel_id:
                        diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                        if diff < 45:
                            purger = entry.user
                            break
        except Exception:
            pass

        # 2. Recover deleted messages from discord.py cache + internal rolling cache
        found_messages = {}
        for m in payload.cached_messages:
            if m.id in unhandled_ids:
                found_messages[m.id] = m

        custom_cache = getattr(self.bot, "message_cache", {})
        for mid in unhandled_ids:
            if mid not in found_messages and mid in custom_cache:
                found_messages[mid] = CachedMessageWrapper(custom_cache[mid])

        all_msgs = list(found_messages.values())
        all_msgs.sort(key=lambda x: x.created_at)

        # 3. Discover the person who commanded the purge / used the bot
        invoker = None
        command_used = None

        # Check inside the purged messages (newest to oldest)
        for m in reversed(all_msgs):
            if not m.author.bot and (not purger or m.author.id != purger.id):
                content = (m.content or "").strip()
                if is_moderation_command_message(content) or any(content.lower().startswith(p) for p in ["!", "?", "-", ";", ".", "/", "=", "+", "~", "$"]):
                    invoker = m.author
                    command_used = content
                    break

        # If not found inside purged messages, check recent rolling cache in this channel
        if not invoker:
            channel_msgs = [
                c for c in custom_cache.values()
                if c.get("channel_id") == payload.channel_id and not c.get("is_bot")
            ]
            for c in reversed(channel_msgs):
                diff = abs((datetime.now(timezone.utc) - c["created_at"]).total_seconds())
                if diff < 60:
                    content = (c.get("content") or "").strip()
                    if is_moderation_command_message(content) or any(content.lower().startswith(p) for p in ["!", "?", "-", ";", ".", "/", "=", "+", "~", "$"]):
                        invoker = CachedAuthorWrapper(c)
                        command_used = content
                        break

            if not invoker and channel_msgs:
                latest = channel_msgs[-1]
                if abs((datetime.now(timezone.utc) - latest["created_at"]).total_seconds()) < 45:
                    invoker = CachedAuthorWrapper(latest)
                    command_used = latest.get("content") or "*Command invocation / interaction*"

        # Save all purged messages (from users, bots & webhooks) to database so /restore can restore them
        messages_to_save = []
        media_map = {}
        for m in all_msgs:
            cached_m = get_cached_media(m.id)
            if cached_m:
                media_map[m.id] = cached_m
                saved_paths = ",".join([item["file_path"] for item in cached_m])
            else:
                saved_paths = ""

            att_urls = ",".join([a.url for a in m.attachments]) if m.attachments else ""
            embeds_json = ""
            if m.embeds:
                try:
                    embeds_json = json.dumps([e.to_dict() for e in m.embeds])
                except Exception:
                    pass

            is_wh = 1 if getattr(m, "webhook_id", None) is not None else (1 if m.author.bot else 0)
            avatar_url = m.author.display_avatar.url if hasattr(m.author, "display_avatar") else ""

            messages_to_save.append({
                "guild_id": guild.id,
                "channel_id": payload.channel_id,
                "author_id": m.author.id,
                "author_name": m.author.display_name,
                "author_avatar_url": avatar_url,
                "content": m.content or "",
                "attachments": att_urls,
                "media_files": saved_paths,
                "embeds": embeds_json,
                "is_webhook": is_wh,
                "timestamp": m.created_at.timestamp()
            })

        if messages_to_save:
            await db.save_purged_messages(messages_to_save)

        # Generate HTML transcript of purged messages
        transcript_file = None
        if all_msgs:
            try:
                mod_for_ts = invoker or purger or guild.me
                html_text = generate_html_transcript(
                    guild=guild,
                    channel=channel_obj or ch,
                    moderator=mod_for_ts,
                    messages=all_msgs,
                    reason=f"Purged by {purger.name if purger else 'external bot'}" + (f" (Commanded by {invoker.display_name})" if invoker else ""),
                    media_map=media_map
                )
                ts_filename = f"transcript-purge-{payload.channel_id}-{int(datetime.now(timezone.utc).timestamp())}.html"
                transcript_file = discord.File(io.BytesIO(html_text.encode("utf-8")), filename=ts_filename)
            except Exception:
                pass

        # Build embed description, bot info, invoker info, and avatar
        if purger and purger.bot:
            bot_tag = f"{purger.mention} 🤖 (`{purger.name}`)"
            if invoker:
                user_pfp = invoker.display_avatar.url
                author_name = f"Purged via {purger.name}"
                author_icon = purger.display_avatar.url
                thumbnail = user_pfp
                desc_lines = [
                    f"**Channel:** {ch_mention}",
                    f"**Bot Used:** {bot_tag}",
                    f"**Commanded By:** {invoker.mention} (`{invoker.display_name}`)",
                ]
                if command_used:
                    desc_lines.append(f"**Command Used:** `{command_used}`")
            else:
                author_name = f"Purged by {purger.name}"
                author_icon = purger.display_avatar.url
                thumbnail = purger.display_avatar.url
                desc_lines = [
                    f"**Channel:** {ch_mention}",
                    f"**Purged By:** {bot_tag}",
                ]
        elif purger and not purger.bot:
            user_pfp = purger.display_avatar.url
            author_name = f"Purged by {purger.display_name}"
            author_icon = user_pfp
            thumbnail = user_pfp
            desc_lines = [
                f"**Channel:** {ch_mention}",
                f"**Moderator:** {purger.mention} (`{purger.name}`)",
            ]
        else:
            if invoker:
                user_pfp = invoker.display_avatar.url
                author_name = f"Purge Triggered by {invoker.display_name}"
                author_icon = user_pfp
                thumbnail = user_pfp
                desc_lines = [
                    f"**Channel:** {ch_mention}",
                    f"**Executed By:** {invoker.mention} (`{invoker.display_name}`)",
                ]
                if command_used:
                    desc_lines.append(f"**Command Used:** `{command_used}`")
            else:
                author_name = "Messages Purged"
                author_icon = None
                thumbnail = None
                desc_lines = [
                    f"**Channel:** {ch_mention}",
                    f"**Purged By:** *External Bot / Moderator*",
                ]

        desc_lines.extend([
            f"**Total Deleted:** `{len(payload.message_ids)}` message(s)",
            f"**Recovered & Archived:** `{len(all_msgs)}` message(s)"
        ])

        if transcript_file:
            desc_lines.append("\n📎 *Interactive HTML transcript of all purged messages (including bot & webhook messages) is attached below.*")
        desc_lines.append(f"\n💡 *You can restore these messages anytime by running `/restore` in {ch_mention}.*")

        embed = log_embed(
            "🧹 Messages Purged (Bulk Deletion)",
            "\n".join(desc_lines),
            color=COLOR_WARNING,
            thumbnail=thumbnail,
            author_name=author_name,
            author_icon=author_icon
        )
        try:
            if transcript_file:
                await ch.send(embed=embed, file=transcript_file)
            else:
                await ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or (self.bot.user and before.author.id == self.bot.user.id):
            return
        if before.content == after.content and len(before.embeds) == len(after.embeds):
            return

        ch = await self.get_channel_for_log(before.guild, "message")
        if not ch:
            return

        avatar_url = before.author.display_avatar.url if hasattr(before.author, "display_avatar") else ""
        author_tag = f"{before.author.mention} 🤖 (`{before.author.id}`)" if before.author.bot else f"{before.author.mention} (`{before.author.id}`)"
        title = "🤖 Bot Message Edited" if before.author.bot else "✏️ Message Edited"

        embed = log_embed(
            title,
            f"**Author:** {author_tag}\n"
            f"**Channel:** {before.channel.mention} [Jump to Message]({after.jump_url})\n\n"
            f"**Before:**\n{before.content or '*[None or Embed]*'}\n\n"
            f"**After:**\n{after.content or '*[None or Embed]*'}",
            color=COLOR_WARNING,
            thumbnail=avatar_url,
            author_name=f"{before.author.display_name} ({before.author})",
            author_icon=avatar_url
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    # --- VOICE LOGS ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        ch = await self.get_channel_for_log(member.guild, "voice")
        if not ch:
            return

        pfp = member.display_avatar.url

        # User Joined Voice
        if before.channel is None and after.channel is not None:
            embed = log_embed(
                "📥 Member Joined Voice",
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** 🔊 {after.channel.name}",
                color=COLOR_SUCCESS,
                thumbnail=pfp,
                author_name=f"{member.display_name} ({member})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

        # User Left Voice
        elif before.channel is not None and after.channel is None:
            embed = log_embed(
                "📤 Member Left Voice",
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** 🔊 {before.channel.name}",
                color=COLOR_ERROR,
                thumbnail=pfp,
                author_name=f"{member.display_name} ({member})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

        # User Switched Voice Channels
        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            embed = log_embed(
                "🔄 Member Switched Voice Channel",
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**From:** 🔊 {before.channel.name}\n"
                f"**To:** 🔊 {after.channel.name}",
                color=COLOR_VOICE,
                thumbnail=pfp,
                author_name=f"{member.display_name} ({member})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

        # Server Mute / Deafen toggle
        elif before.mute != after.mute or before.deaf != after.deaf:
            changes = []
            if before.mute != after.mute:
                changes.append(f"Server Mute: `{'Muted' if after.mute else 'Unmuted'}`")
            if before.deaf != after.deaf:
                changes.append(f"Server Deaf: `{'Deafened' if after.deaf else 'Undeafened'}`")

            embed = log_embed(
                "🎙️ Member Voice State Changed",
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**Changes:**\n" + "\n".join(changes),
                color=COLOR_INFO,
                thumbnail=pfp,
                author_name=f"{member.display_name} ({member})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

    # --- MEMBER LOGS ---
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ch = await self.get_channel_for_log(member.guild, "member")
        if not ch:
            return

        pfp = member.display_avatar.url
        embed = log_embed(
            "👤 Member Joined",
            f"**Member:** {member.mention} (`{member.name}`)\n"
            f"**ID:** `{member.id}`\n"
            f"**Account Created:** <t:{int(member.created_at.timestamp())}:R>\n"
            f"**Member Count:** {member.guild.member_count}",
            color=COLOR_SUCCESS,
            thumbnail=pfp,
            author_name=f"{member.display_name} ({member})",
            author_icon=pfp
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ch = await self.get_channel_for_log(member.guild, "member")
        if not ch:
            return

        pfp = member.display_avatar.url

        # Check if member was kicked by moderator
        kicker = None
        kick_reason = None
        try:
            if member.guild.me.guild_permissions.view_audit_log:
                async for entry in member.guild.audit_logs(limit=3, action=discord.AuditLogAction.kick):
                    if entry.target.id == member.id:
                        diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                        if diff < 15:
                            kicker = entry.user
                            kick_reason = entry.reason
                            break
        except Exception:
            pass

        roles = [r.name for r in member.roles if r.name != "@everyone"]
        if kicker:
            title = "👢 Member Kicked"
            desc = (
                f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n"
                f"**Kicked By:** {kicker.mention} (`{kicker.id}`)\n"
                f"**Reason:** {kick_reason or '*No reason provided*'}\n"
                f"**Member Count:** {member.guild.member_count}"
            )
        else:
            title = "👤 Member Left"
            desc = (
                f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n"
                f"**Roles:** {', '.join(roles) if roles else '*None*'}\n"
                f"**Member Count:** {member.guild.member_count}"
            )

        embed = log_embed(
            title,
            desc,
            color=COLOR_ERROR,
            thumbnail=pfp,
            author_name=f"{member.display_name} ({member})",
            author_icon=pfp
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        ch = await self.get_channel_for_log(before.guild, "member")
        if not ch:
            return

        pfp = after.display_avatar.url

        if before.nick != after.nick:
            embed = log_embed(
                "🏷️ Nickname Changed",
                f"**Member:** {after.mention}\n"
                f"**Before:** {before.nick or before.name}\n"
                f"**After:** {after.nick or after.name}",
                color=COLOR_INFO,
                thumbnail=pfp,
                author_name=f"{after.display_name} ({after})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

        elif before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            lines = [f"**Member:** {after.mention}"]
            if added:
                lines.append(f"**Added Roles:** {', '.join(added)}")
            if removed:
                lines.append(f"**Removed Roles:** {', '.join(removed)}")

            embed = log_embed(
                "🛡️ Member Roles Updated",
                "\n".join(lines),
                color=COLOR_INFO,
                thumbnail=pfp,
                author_name=f"{after.display_name} ({after})",
                author_icon=pfp
            )
            await ch.send(embed=embed)

    # --- CHANNEL LOGS ---
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        ch = await self.get_channel_for_log(channel.guild, "server")
        if not ch:
            return
        embed = log_embed("📁 Channel Created", f"**Channel:** #{channel.name} (`{channel.id}`)", color=COLOR_SUCCESS)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ch = await self.get_channel_for_log(channel.guild, "server")
        if not ch:
            return
        embed = log_embed("📁 Channel Deleted", f"**Channel:** #{channel.name} (`{channel.id}`)", color=COLOR_ERROR)
        await ch.send(embed=embed)

    # --- ROLE LOGS ---
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        ch = await self.get_channel_for_log(role.guild, "server")
        if not ch:
            return
        embed = log_embed("🎭 Role Created", f"**Role:** {role.name} (`{role.id}`)", color=COLOR_SUCCESS)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        ch = await self.get_channel_for_log(role.guild, "server")
        if not ch:
            return
        embed = log_embed("🎭 Role Deleted", f"**Role:** {role.name} (`{role.id}`)", color=COLOR_ERROR)
        await ch.send(embed=embed)

    # --- MODERATION LOGS (BAN / UNBAN) ---
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        ch = await self.get_channel_for_log(guild, "mod")
        if not ch:
            return

        moderator_str = "Unknown Moderator"
        reason_str = "No reason provided"

        try:
            if guild.me.guild_permissions.view_audit_log:
                async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.ban):
                    if entry.target.id == user.id:
                        moderator_str = f"{entry.user.mention} (`{entry.user.id}`)"
                        reason_str = entry.reason or "No reason provided"
                        break
        except Exception:
            pass

        pfp = user.display_avatar.url
        embed = log_embed(
            "🔨 Member Banned",
            f"**User:** {user.mention} (`{user}` - `{user.id}`)\n"
            f"**Moderator:** {moderator_str}\n"
            f"**Reason:** {reason_str}",
            color=COLOR_ERROR,
            thumbnail=pfp,
            author_name=f"{user.display_name} ({user})",
            author_icon=pfp
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        ch = await self.get_channel_for_log(guild, "mod")
        if not ch:
            return

        moderator_str = "Unknown Moderator"
        reason_str = "No reason provided"

        try:
            if guild.me.guild_permissions.view_audit_log:
                async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.unban):
                    if entry.target.id == user.id:
                        moderator_str = f"{entry.user.mention} (`{entry.user.id}`)"
                        reason_str = entry.reason or "No reason provided"
                        break
        except Exception:
            pass

        pfp = user.display_avatar.url
        embed = log_embed(
            "🔓 Member Unbanned",
            f"**User:** {user.mention} (`{user}` - `{user.id}`)\n"
            f"**Moderator:** {moderator_str}\n"
            f"**Reason:** {reason_str}",
            color=COLOR_SUCCESS,
            thumbnail=pfp,
            author_name=f"{user.display_name} ({user})",
            author_icon=pfp
        )
        try:
            await ch.send(embed=embed)
        except Exception:
            pass
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    # --- WEBHOOK LOGS & AUTO-BACKUP ---
    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.TextChannel):
        if not channel.guild:
            return

        try:
            current_webhooks = await channel.webhooks()
            current_ids = {wh.id for wh in current_webhooks}

            # Update backups for all existing webhooks
            for wh in current_webhooks:
                avatar_url = wh.avatar.url if wh.avatar else ""
                await db.save_webhook_backup(wh.id, channel.guild.id, channel.id, wh.name, avatar_url)

            # Check if any backed up webhook in this channel was deleted
            previous_backups = await db.get_backed_up_webhooks(channel.guild.id, channel.id)
            for prev in previous_backups:
                if prev["webhook_id"] not in current_ids:
                    # Webhook was deleted! Log it and offer instant restoration
                    ch_log = await self.get_channel_for_log(channel.guild, "server")
                    if not ch_log:
                        ch_log = await self.get_channel_for_log(channel.guild, "mod")
                    if ch_log:
                        embed = log_embed(
                            "🗑️ Webhook Deleted",
                            f"**Webhook Name:** `{prev['name']}`\n"
                            f"**Channel:** {channel.mention}\n"
                            f"**Previous ID:** `{prev['webhook_id']}`\n\n"
                            f"💡 *Staff can restore this webhook anytime using:* `/webhook restore {prev['name']}`",
                            color=COLOR_ERROR
                        )
                        await ch_log.send(embed=embed)
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
