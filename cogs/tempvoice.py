import discord
from discord.ext import commands
from discord import app_commands, ui
from typing import Optional
from database.db import db
from utils.embeds import voice_embed, success_embed, error_embed

class RenameModal(ui.Modal, title="Rename Voice Channel"):
    channel_name = ui.TextInput(label="New Channel Name", placeholder="e.g. Chill Zone", max_length=50, required=True)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await self.channel.edit(name=self.channel_name.value)
        await interaction.response.send_message(f"Channel renamed to **{self.channel_name.value}**!", ephemeral=True)

class LimitModal(ui.Modal, title="Set User Limit"):
    limit = ui.TextInput(label="User Limit (0 for unlimited, max 99)", placeholder="e.g. 5", max_length=2, required=True)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        val = self.limit.value.strip()
        if not val.isdigit() or int(val) < 0 or int(val) > 99:
            return await interaction.response.send_message("Please provide a valid number between 0 and 99.", ephemeral=True)
        limit_val = int(val)
        await self.channel.edit(user_limit=limit_val)
        await interaction.response.send_message(f"Channel limit set to **{limit_val if limit_val > 0 else 'Unlimited'}**!", ephemeral=True)

class VoiceControlView(ui.View):
    def __init__(self, bot: commands.Bot, channel_id: int, owner_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.channel_id = channel_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only the channel owner or an administrator can use these controls!", ephemeral=True)
            return False
        return True

    @ui.button(label="Lock / Unlock", style=discord.ButtonStyle.primary, emoji="🔒", custom_id="vc_toggle_lock")
    async def toggle_lock(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.response.send_message("Channel not found!", ephemeral=True)

        overwrite = channel.overwrites_for(interaction.guild.default_role)
        currently_locked = overwrite.connect is False

        overwrite.connect = None if currently_locked else False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)

        status_text = "unlocked (everyone can join)" if currently_locked else "locked (no one can join)"
        button.emoji = "🔒" if currently_locked else "🔓"
        await interaction.response.send_message(f"Voice channel is now **{status_text}**.", ephemeral=True)

    @ui.button(label="Rename", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id="vc_rename")
    async def rename(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.response.send_message("Channel not found!", ephemeral=True)
        await interaction.response.send_modal(RenameModal(channel))

    @ui.button(label="Set Limit", style=discord.ButtonStyle.secondary, emoji="👥", custom_id="vc_limit")
    async def set_limit(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.response.send_message("Channel not found!", ephemeral=True)
        await interaction.response.send_modal(LimitModal(channel))

class TempVoice(commands.Cog, name="Dynamic Voice (Temp Voice)"):
    """Join-to-Create temporary voice channel management"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="tempvoice", description="Manage Join-to-Create temporary voice channels")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    async def tempvoice_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `/tempvoice setup` or `/tempvoice disable`.", ephemeral=True)

    @tempvoice_group.command(name="setup", description="Setup Join-to-Create voice system")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        category="Category to create temp channels in (optional)",
        channel_name="Name of the Join to Create trigger channel"
    )
    async def setup_tempvoice(self, ctx: commands.Context, category: Optional[discord.CategoryChannel] = None, channel_name: str = "➕ Join to Create"):
        guild = ctx.guild
        target_category = category or (ctx.channel.category if isinstance(ctx.channel, discord.TextChannel) else None)

        # Create the trigger channel
        trigger_ch = await guild.create_voice_channel(
            name=channel_name,
            category=target_category,
            reason="Temp Voice Trigger Channel Setup"
        )

        cat_id = target_category.id if target_category else None
        await db.update_guild_setting(guild.id, "tempvoice_creator_id", trigger_ch.id)
        await db.update_guild_setting(guild.id, "tempvoice_category_id", cat_id)
        await db.update_guild_setting(guild.id, "tempvoice_enabled", 1)

        await ctx.send(embed=success_embed(
            "Temp Voice Configured",
            f"Join-to-Create channel created: {trigger_ch.mention}\n"
            f"Category: **{target_category.name if target_category else 'None'}**\n\n"
            f"When users join {trigger_ch.name}, a private room will be created for them with control buttons!"
        ))

    @tempvoice_group.command(name="disable", description="Disable Join-to-Create system")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    async def disable_tempvoice(self, ctx: commands.Context):
        await db.update_guild_setting(ctx.guild.id, "tempvoice_enabled", 0)
        await ctx.send(embed=success_embed("Temp Voice Disabled", "Join-to-Create voice channels have been disabled."))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        guild = member.guild
        settings = await db.get_guild_settings(guild.id)
        if not settings.get("tempvoice_enabled"):
            return

        creator_id = settings.get("tempvoice_creator_id")
        category_id = settings.get("tempvoice_category_id")
        category = guild.get_channel(category_id) if category_id else None

        # 1. User joins the Creator/Trigger channel
        if after.channel and after.channel.id == creator_id:
            ch_name = f"🔊 {member.display_name}'s Room"
            temp_ch = await guild.create_voice_channel(
                name=ch_name,
                category=category or after.channel.category,
                reason=f"Temp voice created for {member}"
            )
            # Move user into their newly created channel
            await member.move_to(temp_ch)
            await db.add_temp_channel(temp_ch.id, guild.id, member.id)

            # Send control panel inside the voice text channel
            try:
                view = VoiceControlView(self.bot, temp_ch.id, member.id)
                embed = voice_embed(
                    "Voice Channel Controls",
                    f"Welcome to your temporary voice channel {member.mention}!\n"
                    f"Use the buttons below to lock, rename, or limit your room."
                )
                await temp_ch.send(content=member.mention, embed=embed, view=view)
            except Exception:
                pass

        # 2. Check if a temp channel was vacated
        if before.channel and before.channel.id != creator_id:
            owner_id = await db.get_temp_channel_owner(before.channel.id)
            if owner_id:
                # If channel is empty, delete it
                if len(before.channel.members) == 0:
                    try:
                        await before.channel.delete(reason="Temp voice channel vacated")
                    except Exception:
                        pass
                    await db.remove_temp_channel(before.channel.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
