import discord
from discord.ext import commands
from discord import app_commands
import re
import time
from typing import Optional, Literal
from collections import defaultdict
from database.db import db
from utils.embeds import success_embed, error_embed, info_embed, warning_embed

INVITE_REGEX = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com/invite)/[a-zA-Z0-9]+", re.IGNORECASE)
URL_REGEX = re.compile(r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+", re.IGNORECASE)

class AutoMod(commands.Cog, name="Auto-Moderation (Sapphire)"):
    """Automated filters: anti-invite, anti-link, anti-spam, caps, and bad words"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Track user message timestamps for anti-spam: {guild_id: {user_id: [timestamps]}}
        self.spam_tracker = defaultdict(lambda: defaultdict(list))

    @commands.hybrid_group(name="automod", description="Configure Sapphire Auto-Mod features")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def automod_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self.status(ctx)

    @automod_group.command(name="status", description="View current AutoMod configuration")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        cfg = await db.get_automod_config(ctx.guild.id)
        bad_words_list = [w for w in cfg.get("bad_words", "").split(",") if w]

        desc = (
            f"• **Anti-Invite:** {'✅ Enabled' if cfg.get('anti_invite') else '❌ Disabled'}\n"
            f"• **Anti-Link:** {'✅ Enabled' if cfg.get('anti_link') else '❌ Disabled'}\n"
            f"• **Anti-Spam:** {'✅ Enabled' if cfg.get('anti_spam') else '❌ Disabled'}\n"
            f"• **Anti-Caps:** {'✅ Enabled' if cfg.get('anti_caps') else '❌ Disabled'} (Threshold: {cfg.get('caps_threshold', 70)}%)\n"
            f"• **Punishment Action:** `{cfg.get('punishment', 'delete').upper()}`\n"
            f"• **Blacklisted Words:** {len(bad_words_list)} words configured"
        )
        await ctx.send(embed=info_embed("Auto-Moderation Settings", desc))

    @automod_group.command(name="antiinvite", description="Enable or disable anti-invite filter")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn on or off")
    async def antiinvite(self, ctx: commands.Context, enabled: bool):
        await db.update_automod_config(ctx.guild.id, "anti_invite", 1 if enabled else 0)
        await ctx.send(embed=success_embed("AutoMod Updated", f"Anti-Invite is now **{'Enabled' if enabled else 'Disabled'}**."))

    @automod_group.command(name="antilink", description="Enable or disable general links filter")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn on or off")
    async def antilink(self, ctx: commands.Context, enabled: bool):
        await db.update_automod_config(ctx.guild.id, "anti_link", 1 if enabled else 0)
        await ctx.send(embed=success_embed("AutoMod Updated", f"Anti-Link is now **{'Enabled' if enabled else 'Disabled'}**."))

    @automod_group.command(name="antispam", description="Enable or disable fast message spam filter")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn on or off")
    async def antispam(self, ctx: commands.Context, enabled: bool):
        await db.update_automod_config(ctx.guild.id, "anti_spam", 1 if enabled else 0)
        await ctx.send(embed=success_embed("AutoMod Updated", f"Anti-Spam is now **{'Enabled' if enabled else 'Disabled'}**."))

    @automod_group.command(name="anticaps", description="Enable or disable excessive caps filter")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn on or off", threshold="Caps percentage threshold (50-100, default 70)")
    async def anticaps(self, ctx: commands.Context, enabled: bool, threshold: int = 70):
        thresh = min(100, max(50, threshold))
        await db.update_automod_config(ctx.guild.id, "anti_caps", 1 if enabled else 0)
        await db.update_automod_config(ctx.guild.id, "caps_threshold", thresh)
        await ctx.send(embed=success_embed("AutoMod Updated", f"Anti-Caps is now **{'Enabled' if enabled else 'Disabled'}** at **{thresh}%** threshold."))

    @automod_group.command(name="action", description="Set the punishment action when a violation occurs")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(punishment="Action to take")
    async def set_action(self, ctx: commands.Context, punishment: Literal["delete", "warn", "timeout"]):
        await db.update_automod_config(ctx.guild.id, "punishment", punishment)
        await ctx.send(embed=success_embed("AutoMod Updated", f"Punishment action set to **{punishment.upper()}**."))

    @automod_group.command(name="badwords", description="Manage blacklisted words")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(action="Add, remove, or list words", word="Word to add or remove")
    async def badwords(self, ctx: commands.Context, action: Literal["add", "remove", "list"], word: Optional[str] = None):
        cfg = await db.get_automod_config(ctx.guild.id)
        current = [w.strip().lower() for w in cfg.get("bad_words", "").split(",") if w.strip()]

        if action == "list":
            if not current:
                return await ctx.send(embed=info_embed("Bad Words Filter", "No blacklisted words set."))
            return await ctx.send(embed=info_embed("Blacklisted Words", f"Words: `{', '.join(current)}`"))

        if not word:
            return await ctx.send(embed=error_embed("Missing Argument", "Please provide a word to add or remove."), ephemeral=True)

        target = word.strip().lower()
        if action == "add":
            if target in current:
                return await ctx.send(embed=warning_embed("Already Exists", f"`{target}` is already on the blacklist."), ephemeral=True)
            current.append(target)
            await db.update_automod_config(ctx.guild.id, "bad_words", ",".join(current))
            await ctx.send(embed=success_embed("Word Added", f"Added `{target}` to blacklisted words."))
        elif action == "remove":
            if target not in current:
                return await ctx.send(embed=warning_embed("Not Found", f"`{target}` was not in the blacklist."), ephemeral=True)
            current.remove(target)
            await db.update_automod_config(ctx.guild.id, "bad_words", ",".join(current))
            await ctx.send(embed=success_embed("Word Removed", f"Removed `{target}` from blacklisted words."))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        # Bypass for server administrators and members with Manage Messages
        if message.author.guild_permissions.manage_messages:
            return

        cfg = await db.get_automod_config(message.guild.id)
        content = message.content
        violation_reason = None

        # 1. Anti-Invite
        if cfg.get("anti_invite") and INVITE_REGEX.search(content):
            violation_reason = "Posting Discord Invite links is not allowed."

        # 2. Anti-Link
        elif cfg.get("anti_link") and URL_REGEX.search(content):
            violation_reason = "Posting links is disabled in this server."

        # 3. Anti-Caps
        elif cfg.get("anti_caps") and len(content) > 10:
            letters = [c for c in content if c.isalpha()]
            if letters:
                caps = [c for c in letters if c.isupper()]
                ratio = (len(caps) / len(letters)) * 100
                if ratio >= cfg.get("caps_threshold", 70):
                    violation_reason = f"Excessive uppercase letters ({int(ratio)}%)."

        # 4. Bad Words Filter
        elif cfg.get("bad_words"):
            bad_words = [w.strip().lower() for w in cfg.get("bad_words", "").split(",") if w.strip()]
            content_lower = content.lower()
            for bw in bad_words:
                if re.search(r"\b" + re.escape(bw) + r"\b", content_lower):
                    violation_reason = "Message contained prohibited words."
                    break

        # 5. Anti-Spam (more than 5 messages in 3 seconds)
        if not violation_reason and cfg.get("anti_spam"):
            now = time.time()
            user_stamps = self.spam_tracker[message.guild.id][message.author.id]
            # Prune timestamps older than 3 seconds
            self.spam_tracker[message.guild.id][message.author.id] = [t for t in user_stamps if now - t < 3.0]
            self.spam_tracker[message.guild.id][message.author.id].append(now)

            if len(self.spam_tracker[message.guild.id][message.author.id]) >= 5:
                violation_reason = "Excessive rapid message spam."
                self.spam_tracker[message.guild.id][message.author.id] = []

        if violation_reason:
            punishment = cfg.get("punishment", "delete")
            try:
                await message.delete()
            except Exception:
                pass

            # Punish user based on setting
            if punishment == "warn":
                await db.add_warning(message.guild.id, message.author.id, self.bot.user.id, f"AutoMod: {violation_reason}")
                await message.channel.send(
                    f"⚠️ {message.author.mention} was warned by **AutoMod**! Reason: {violation_reason}",
                    delete_after=6
                )
            elif punishment == "timeout":
                try:
                    from datetime import timedelta
                    await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=5), reason=f"AutoMod: {violation_reason}")
                    await message.channel.send(
                        f"🔇 {message.author.mention} was timed out for 5 minutes by **AutoMod**! Reason: {violation_reason}",
                        delete_after=6
                    )
                except Exception:
                    pass
            else: # "delete"
                await message.channel.send(
                    f"⚠️ {message.author.mention}, your message was removed by **AutoMod** ({violation_reason}).",
                    delete_after=5
                )

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
