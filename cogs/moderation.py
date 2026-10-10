import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from database.db import db
from utils.embeds import success_embed, error_embed, info_embed, log_embed, COLOR_WARNING
from utils.transcript import generate_html_transcript
from utils.media_manager import download_attachment, create_discord_files_from_paths, PURGE_DIR

PURGE_CMD_PATTERN = re.compile(
    r"^[\/=!?.$\-~]?\s*(purge|clear|userpurge|purgeuser|puser|restore|cleanbot)\b",
    re.IGNORECASE
)

def is_moderation_command_message(content: str) -> bool:
    """Check if message content was a purge or restore command invocation."""
    if not content:
        return False
    return bool(PURGE_CMD_PATTERN.match(content.strip()))

class Restore(commands.Cog, name="Restore & Purge"):
    """Chat purge and restoration suite"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- PURGE & RESTORE SUITE ---
    # 9. PURGE / CLEAR MESSAGES
    async def do_purge(self, ctx: commands.Context, amount: int, user: Optional[discord.Member] = None):
        if amount < 1 or amount > 100:
            return await ctx.send(embed=error_embed("Invalid Amount", "Please specify an amount between 1 and 100."), ephemeral=True)

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)

        # Delete original invoking message if prefix command
        if ctx.interaction is None and ctx.message:
            try:
                # Add to ignored IDs so it doesn't get logged as deleted message
                if hasattr(self.bot, "ignored_message_ids"):
                    self.bot.ignored_message_ids.add(ctx.message.id)
                await ctx.message.delete()
            except Exception:
                pass

        deleted_count = 0
        def check(m):
            nonlocal deleted_count
            if deleted_count >= amount:
                return False
            if user is None or m.author.id == user.id:
                deleted_count += 1
                return True
            return False

        # When searching for a specific user, scan deeper to find up to `amount` messages
        search_limit = amount if user is None else min(1000, max(amount * 10, 100))
        deleted = await ctx.channel.purge(limit=search_limit, check=check)

        # Ignore purged messages from individual on_message_delete logs
        if hasattr(self.bot, "ignored_message_ids"):
            for m in deleted:
                self.bot.ignored_message_ids.add(m.id)

        # Download and preserve all media attachments locally for restore and HTML preview
        media_map = {}
        for m in deleted:
            if m.attachments:
                m_media = []
                for att in m.attachments:
                    item = await download_attachment(att, PURGE_DIR, prefix=f"purge_{m.id}")
                    if item:
                        m_media.append(item)
                if m_media:
                    media_map[m.id] = m_media

        # Save purged messages so they can be restored via /restore
        messages_to_save = []
        for m in reversed(deleted):
            # Never save the purge command itself or any moderation command messages
            if ctx.message and m.id == ctx.message.id:
                continue
            if is_moderation_command_message(m.content):
                continue

            att_urls = ",".join([a.url for a in m.attachments])
            avatar_url = m.author.display_avatar.url if hasattr(m.author, "display_avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
            saved_paths = ",".join([item["file_path"] for item in media_map.get(m.id, [])])

            # Serialize embeds for webhook or bot messages
            embeds_json = ""
            if m.embeds:
                try:
                    embeds_json = json.dumps([e.to_dict() for e in m.embeds])
                except Exception:
                    pass

            is_webhook = 1 if m.webhook_id is not None else 0

            messages_to_save.append({
                "guild_id": ctx.guild.id,
                "channel_id": ctx.channel.id,
                "author_id": m.author.id,
                "author_name": m.author.display_name,
                "author_avatar_url": avatar_url,
                "content": m.content or "",
                "attachments": att_urls,
                "media_files": saved_paths,
                "embeds": embeds_json,
                "is_webhook": is_webhook,
                "timestamp": m.created_at.timestamp()
            })

        if messages_to_save:
            await db.save_purged_messages(messages_to_save)

        # Generate HTML transcript of purged messages with embedded images
        html_bytes = None
        ts_filename = f"transcript-purge-{ctx.channel.name}-{int(datetime.now(timezone.utc).timestamp())}.html"
        if deleted:
            try:
                html_text = generate_html_transcript(
                    guild=ctx.guild,
                    channel=ctx.channel,
                    moderator=ctx.author,
                    messages=deleted,
                    reason=f"Purge command executed by {ctx.author}" + (f" for {user}" if user else ""),
                    media_map=media_map
                )
                html_bytes = html_text.encode("utf-8")
            except Exception:
                pass

        # Send audit log to Message Logs channel with HTML transcript attached
        log_channel_id = await db.get_log_channel(ctx.guild.id, "message")
        if not log_channel_id:
            log_channel_id = await db.get_log_channel(ctx.guild.id, "mod")
        if not log_channel_id:
            log_channel_id = await db.get_log_channel(ctx.guild.id, "all")

        sent_to_logs = False
        if log_channel_id:
            log_ch = ctx.guild.get_channel(log_channel_id)
            if log_ch and html_bytes:
                log_file = discord.File(io.BytesIO(html_bytes), filename=ts_filename)
                pfp = ctx.author.display_avatar.url
                log_embed_obj = log_embed(
                    "🧹 Messages Purged",
                    f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                    f"**Channel:** {ctx.channel.mention} (`{ctx.channel.id}`)\n"
                    f"**Amount Purged:** `{len(deleted)}` message(s)\n"
                    f"**Target User:** {user.mention if user else '*All Users*'}\n\n"
                    f"📎 *An interactive HTML transcript of all purged messages is attached below.*",
                    color=COLOR_WARNING,
                    thumbnail=pfp,
                    author_name=f"Purged by {ctx.author.display_name}",
                    author_icon=pfp
                )
                try:
                    await log_ch.send(embed=log_embed_obj, file=log_file)
                    sent_to_logs = True
                except Exception:
                    pass

        # Response to the moderator
        confirm_desc = f"Deleted **{len(deleted)}** message(s)" + (f" from {user.mention}." if user else ".") + "\n"
        if sent_to_logs:
            confirm_desc += f"• **Audit Log:** Saved with HTML transcript in <#{log_channel_id}>\n"
        else:
            confirm_desc += "• **Audit Log:** *Run `/logs setup` to create dedicated log channels for transcripts.*\n"
        confirm_desc += "• **Restore:** Restore these messages anytime with `/restore`."

        # If not sent to logs, attach the file directly to the user response
        if not sent_to_logs and html_bytes:
            user_file = discord.File(io.BytesIO(html_bytes), filename=ts_filename)
            await ctx.send(embed=success_embed("Messages Purged", confirm_desc), file=user_file, ephemeral=True)
        else:
            await ctx.send(embed=success_embed("Messages Purged", confirm_desc), ephemeral=True)

    # Standard Purge Command
    @commands.hybrid_command(name="purge", aliases=["clear"], description="Purge recent messages from the channel")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Optional user to filter messages by"
    )
    async def purge(self, ctx: commands.Context, amount: int, user: Optional[discord.Member] = None):
        """Standard purge: /purge <amount> [user] or =purge 50"""
        await self.do_purge(ctx, amount=amount, user=user)

    @purge.error
    async def purge_error(self, ctx: commands.Context, error: Exception):
        raw_error = getattr(error, 'original', error)
        # Handle flexible orders like `=purge @user 50` or `=purge @user`
        if ctx.interaction is None and isinstance(raw_error, (commands.BadArgument, commands.MissingRequiredArgument)):
            content = ctx.message.content
            target_user = None
            if ctx.message.mentions:
                target_user = ctx.message.mentions[0]
            else:
                id_match = re.search(r"\b(\d{17,20})\b", content)
                if id_match:
                    target_user = ctx.guild.get_member(int(id_match.group(1)))

            tokens = content.split()
            found_amount = 10
            for token in tokens[1:]:
                if token.isdigit() and len(token) <= 3:
                    found_amount = int(token)
                    break

            if target_user:
                return await self.do_purge(ctx, amount=found_amount, user=target_user)
            elif found_amount != 10:
                return await self.do_purge(ctx, amount=found_amount, user=None)

        if isinstance(raw_error, commands.MissingPermissions):
            await ctx.send(embed=error_embed("Permission Required", "You need the **Manage Messages** permission on one of your roles to purge messages."), ephemeral=True)
        elif isinstance(raw_error, commands.BotMissingPermissions):
            await ctx.send(embed=error_embed("Bot Missing Permission", "Bot requires **Manage Messages** and **Read Message History** permissions to purge."), ephemeral=True)
        else:
            await ctx.send(embed=error_embed("Purge Error", f"{raw_error}"), ephemeral=True)

    # Dedicated User Purge Command
    @commands.hybrid_command(name="purgeuser", aliases=["userpurge", "clearuser", "puser"], description="Purge messages from a specific user")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(
        user="The member whose messages will be purged",
        amount="Number of messages to delete (1-100, default 10)"
    )
    async def purgeuser(self, ctx: commands.Context, user: discord.Member, amount: int = 10):
        """Dedicated user purge: /purgeuser @user [amount]"""
        await self.do_purge(ctx, amount=amount, user=user)

    @purgeuser.error
    async def purgeuser_error(self, ctx: commands.Context, error: Exception):
        raw_error = getattr(error, 'original', error)
        if ctx.interaction is None and isinstance(raw_error, (commands.BadArgument, commands.MissingRequiredArgument)):
            content = ctx.message.content
            target_user = None
            if ctx.message.mentions:
                target_user = ctx.message.mentions[0]
            else:
                id_match = re.search(r"\b(\d{17,20})\b", content)
                if id_match:
                    target_user = ctx.guild.get_member(int(id_match.group(1)))

            tokens = content.split()
            found_amount = 10
            for token in tokens[1:]:
                if token.isdigit() and len(token) <= 3:
                    found_amount = int(token)
                    break

            if target_user:
                return await self.do_purge(ctx, amount=found_amount, user=target_user)

        if isinstance(raw_error, commands.MissingPermissions):
            await ctx.send(embed=error_embed("Permission Required", "You need the **Manage Messages** permission on one of your roles to purge messages."), ephemeral=True)
        elif isinstance(raw_error, commands.BotMissingPermissions):
            await ctx.send(embed=error_embed("Bot Missing Permission", "Bot requires **Manage Messages** and **Read Message History** permissions to purge."), ephemeral=True)
        else:
            await ctx.send(embed=error_embed("Purge Error", f"{raw_error}"), ephemeral=True)

    # RESTORE - Restore purged messages with original names & avatars (NQN / Tupperbox style)
    @commands.hybrid_command(name="restore", description="Restore purged messages using webhooks with original names and avatars (NQN style)")
    @commands.bot_has_permissions(manage_webhooks=True)
    @app_commands.describe(amount="Number of purged messages to restore (1-100, default 50)")
    async def restore(self, ctx: commands.Context, amount: int = 50):
        # Role & Permission Check
        restore_role_id = await db.get_restore_role_id(ctx.guild.id)
        is_admin = ctx.author.guild_permissions.administrator or ctx.author.id == ctx.guild.owner_id
        if restore_role_id:
            restore_role = ctx.guild.get_role(restore_role_id)
            if restore_role:
                has_role = any(r.id == restore_role_id for r in ctx.author.roles)
                if not (has_role or is_admin):
                    return await ctx.send(embed=error_embed(
                        "Permission Required",
                        f"You need the {restore_role.mention} role on your profile to use the `/restore` command."
                    ), ephemeral=True)
        else:
            if not (ctx.author.guild_permissions.manage_messages or is_admin):
                return await ctx.send(embed=error_embed(
                    "Permission Required",
                    "You need the **Manage Messages** permission (or the configured restore role) to use `/restore`."
                ), ephemeral=True)

        if amount < 1 or amount > 100:
            return await ctx.send(embed=error_embed("Invalid Amount", "Please specify an amount between 1 and 100."), ephemeral=True)

        await ctx.defer(ephemeral=True)
        channel = ctx.channel

        # Retrieve stored purged messages in chronological order
        purged = await db.get_purged_messages_to_restore(channel.id, limit=amount)
        if not purged:
            return await ctx.send(embed=error_embed("No Messages Found", "No recent purged messages were found for this channel."), ephemeral=True)

        # Find or create a webhook for restoration
        webhook = None
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user and wh.user.id == self.bot.user.id:
                    webhook = wh
                    break
            if not webhook:
                webhook = await channel.create_webhook(name="Chat Restorer", reason="Webhook for restoring purged messages")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing Permission", "Bot requires `Manage Webhooks` permission to restore chat."), ephemeral=True)
        except Exception as e:
            return await ctx.send(embed=error_embed("Webhook Error", f"Could not initialize webhook: {e}"), ephemeral=True)

        restored_count = 0
        restored_ids = []

        for msg in purged:
            content = msg["content"] or ""
            # Never restore purge or moderation command messages
            if is_moderation_command_message(content):
                restored_ids.append(msg["id"])
                continue

            media_files_str = msg.get("media_files") or ""
            local_paths = [p.strip() for p in media_files_str.split(",") if p.strip()]
            files_to_upload = create_discord_files_from_paths(local_paths)

            # Reconstruct original embeds if present (crucial for webhooks & rich messages)
            restored_embeds = []
            if msg.get("embeds"):
                try:
                    embed_dicts = json.loads(msg["embeds"])
                    for ed in embed_dicts:
                        restored_embeds.append(discord.Embed.from_dict(ed))
                except Exception:
                    pass

            # Fallback to URLs if files could not be loaded
            if not files_to_upload:
                attachments = [att.strip() for att in msg["attachments"].split(",") if att.strip()] if msg.get("attachments") else []
                if not content and attachments and not restored_embeds:
                    content = "\n".join(attachments)
                elif attachments and not restored_embeds:
                    content += "\n" + "\n".join(attachments)

            if not content and not files_to_upload and not restored_embeds:
                content = "*[Attachment, Embed or Media]*"

            try:
                await webhook.send(
                    content=content or None,
                    username=msg["author_name"],
                    avatar_url=msg["author_avatar_url"],
                    embeds=restored_embeds if restored_embeds else discord.utils.MISSING,
                    files=files_to_upload if files_to_upload else []
                )
                restored_count += 1
                restored_ids.append(msg["id"])
                # Rate limit safety delay
                await asyncio.sleep(0.4)
            except Exception:
                pass

        # Clean up restored messages from database queue so they aren't restored twice
        if restored_ids:
            await db.delete_restored_messages(restored_ids)

        await ctx.send(embed=success_embed(
            "Chat Restored",
            f"Successfully restored **{restored_count}** message(s) (including webhook messages, embeds & media) with original names & avatars!"
        ), ephemeral=True)

    # 14. WEBHOOK MANAGEMENT & RESTORATION
    @commands.hybrid_group(name="webhook", description="Manage, backup, and restore channel webhooks")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Webhook System",
                "• `/webhook backup` — Save a backup of all webhooks in the server\n"
                "• `/webhook list` — View all backed up webhooks\n"
                "• `/webhook restore <name>` — Recreate and restore a deleted webhook"
            ))

    @webhook_group.command(name="backup", description="Back up all webhooks across the server")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_backup(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        backed_up = 0
        for channel in ctx.guild.text_channels:
            try:
                whs = await channel.webhooks()
                for wh in whs:
                    avatar_url = wh.avatar.url if wh.avatar else ""
                    await db.save_webhook_backup(wh.id, ctx.guild.id, channel.id, wh.name, avatar_url)
                    backed_up += 1
            except Exception:
                pass
        await ctx.send(embed=success_embed(
            "Webhooks Backed Up",
            f"Successfully scanned and backed up **{backed_up}** webhook(s) across **{ctx.guild.name}**."
        ), ephemeral=True)

    @webhook_group.command(name="list", description="List backed up webhooks that can be restored")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_list(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        target_ch_id = channel.id if channel else None
        backups = await db.get_backed_up_webhooks(ctx.guild.id, target_ch_id)
        if not backups:
            return await ctx.send(embed=info_embed("Webhook Backups", "No backed up webhooks found for this server. Run `/webhook backup` first."))

        lines = []
        for b in backups[:20]:
            ch = ctx.guild.get_channel(b["channel_id"])
            ch_str = ch.mention if ch else f"`#{b['channel_id']}`"
            lines.append(f"• **{b['name']}** in {ch_str} (ID: `{b['webhook_id']}`)")

        await ctx.send(embed=info_embed(
            "Backed Up Webhooks",
            "\n".join(lines) + "\n\n*To restore a deleted webhook: `/webhook restore <name>`*"
        ))

    @webhook_group.command(name="restore", description="Restore or recreate a deleted webhook")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    @app_commands.describe(name="Name of the webhook to restore", channel="Optional channel to recreate webhook in")
    async def webhook_restore(self, ctx: commands.Context, name: str, channel: Optional[discord.TextChannel] = None):
        await ctx.defer(ephemeral=True)
        backups = await db.get_backed_up_webhooks(ctx.guild.id)
        target = None
        for b in backups:
            if b["name"].lower() == name.lower():
                target = b
                break

        if not target:
            return await ctx.send(embed=error_embed("Webhook Not Found", f"No backed up webhook with name `{name}` was found."), ephemeral=True)

        target_ch = channel or ctx.guild.get_channel(target["channel_id"]) or ctx.channel
        try:
            new_wh = await target_ch.create_webhook(
                name=target["name"],
                reason=f"Restored webhook from backup by {ctx.author}"
            )
            avatar_url = new_wh.avatar.url if new_wh.avatar else ""
            await db.save_webhook_backup(new_wh.id, ctx.guild.id, target_ch.id, new_wh.name, avatar_url)
            await ctx.send(embed=success_embed(
                "Webhook Restored",
                f"Successfully recreated webhook **{new_wh.name}** in {target_ch.mention}!\n"
                f"• **Webhook URL:** ||`{new_wh.url}`|| *(Keep this URL private)*"
            ), ephemeral=True)
        except Exception as e:
            await ctx.send(embed=error_embed("Restore Failed", f"Failed to recreate webhook: {e}"), ephemeral=True)

    # 15. RESTORE ROLE CONFIGURATION (ADMIN ONLY)
    @commands.hybrid_group(name="restorerole", description="Configure which role has permission to restore chat", fallback="show")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def restorerole_group(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """View current restore role or set a new one directly (e.g. /restorerole @Role)"""
        if role is not None:
            await db.set_restore_role_id(ctx.guild.id, role.id)
            return await ctx.send(embed=success_embed(
                "Restore Role Updated",
                f"The `/restore` command is now tied to {role.mention}.\n"
                f"Only members with this role (and server admins) will be able to restore chat."
            ))

        current_id = await db.get_restore_role_id(ctx.guild.id)
        if current_id:
            role_obj = ctx.guild.get_role(current_id)
            role_str = role_obj.mention if role_obj else f"*Deleted Role (`{current_id}`)*"
            desc = (
                f"The `/restore` command is currently restricted to: {role_str}\n\n"
                f"• **Change role:** `/restorerole set @Role`\n"
                f"• **Remove restriction:** `/restorerole remove`"
            )
        else:
            desc = (
                "No specific role is currently tied to `/restore`.\n"
                "Members with **Manage Messages** permission can restore chat.\n\n"
                "• **Tie to a role:** `/restorerole set @Role` (e.g. `/restorerole @Moderator`)"
            )
        await ctx.send(embed=info_embed("Restore Command Role", desc))

    @restorerole_group.command(name="set", description="Tie the restore command to a specific role")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(role="The role that will be allowed to use /restore")
    async def restorerole_set(self, ctx: commands.Context, role: discord.Role):
        await db.set_restore_role_id(ctx.guild.id, role.id)
        await ctx.send(embed=success_embed(
            "Restore Role Set",
            f"The `/restore` command is now restricted to {role.mention}.\n"
            f"Members without this role cannot run `/restore`."
        ))

    @restorerole_group.command(name="remove", aliases=["clear", "reset"], description="Remove role restriction from /restore")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def restorerole_remove(self, ctx: commands.Context):
        await db.set_restore_role_id(ctx.guild.id, None)
        await ctx.send(embed=success_embed(
            "Restore Role Cleared",
            "Role restriction cleared.\n"
            "The `/restore` command will now use standard **Manage Messages** permissions."
        ))

async def setup(bot: commands.Bot):
    await bot.add_cog(Restore(bot))
