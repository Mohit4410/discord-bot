import discord
from discord.ext import commands
from discord import app_commands, ui
from typing import Optional, Literal
import aiosqlite
from database.db import db
from utils.embeds import success_embed, error_embed, info_embed

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger
}

class RoleButton(ui.Button):
    def __init__(self, role_id: int, label: str, emoji: Optional[str] = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"selfrole_{role_id}"
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("This role no longer exists on the server.", ephemeral=True)

        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role, reason="Self-role toggled off via button")
            await interaction.response.send_message(f"Removed the {role.mention} role from you.", ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason="Self-role toggled on via button")
                await interaction.response.send_message(f"Gave you the {role.mention} role!", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("Bot does not have permission to grant this role (check role hierarchy).", ephemeral=True)

class PersistentRoleView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

class Roles(commands.Cog, name="Roles & Self-Roles (Sapphire)"):
    """Button roles, interactive self-roles, and autoroles"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Restore all button role listeners from database on cog load."""
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT DISTINCT role_id, label, emoji, button_style FROM button_roles")
            rows = await cursor.fetchall()
            
            view = PersistentRoleView()
            for row in rows:
                btn_style = BUTTON_STYLES.get(row['button_style'], discord.ButtonStyle.secondary)
                view.add_item(RoleButton(
                    role_id=row['role_id'],
                    label=row['label'] or "Get Role",
                    emoji=row['emoji'],
                    style=btn_style
                ))
            if rows:
                self.bot.add_view(view)

    # --- AUTOROLE COMMANDS ---
    @commands.hybrid_group(name="autorole", description="Manage auto-assigned roles for new members")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    async def autorole_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self.autorole_list(ctx)

    @autorole_group.command(name="add", description="Add an autorole to give on join")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(role="Role to automatically grant")
    async def autorole_add(self, ctx: commands.Context, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed("Hierarchy Error", "Role is higher than or equal to bot's highest role."), ephemeral=True)

        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO autoroles (guild_id, role_id) VALUES (?, ?)", (ctx.guild.id, role.id))
            await conn.commit()

        await ctx.send(embed=success_embed("Autorole Added", f"{role.mention} will now be automatically given to new members."))

    @autorole_group.command(name="remove", description="Remove an autorole")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(role="Role to remove from autorole list")
    async def autorole_remove(self, ctx: commands.Context, role: discord.Role):
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("DELETE FROM autoroles WHERE guild_id = ? AND role_id = ?", (ctx.guild.id, role.id))
            await conn.commit()

        await ctx.send(embed=success_embed("Autorole Removed", f"{role.mention} has been removed from autoroles."))

    @autorole_group.command(name="list", description="List all configured autoroles")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    async def autorole_list(self, ctx: commands.Context):
        async with aiosqlite.connect(db.db_path) as conn:
            cursor = await conn.execute("SELECT role_id FROM autoroles WHERE guild_id = ?", (ctx.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            return await ctx.send(embed=info_embed("Autoroles", "No autoroles currently configured."))

        roles = [ctx.guild.get_role(r[0]) for r in rows if ctx.guild.get_role(r[0])]
        desc = "\n".join([f"• {r.mention}" for r in roles])
        await ctx.send(embed=info_embed("Configured Autoroles", desc))

    # --- BUTTON ROLE COMMANDS ---
    @commands.hybrid_command(name="buttonrole", description="Create an interactive self-role button message")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(
        role="Role to give/take with button",
        label="Text shown on the button",
        channel="Channel to post button in (defaults to current)",
        style="Button color style",
        emoji="Optional emoji for the button"
    )
    async def create_button_role(
        self,
        ctx: commands.Context,
        role: discord.Role,
        label: str,
        channel: Optional[discord.TextChannel] = None,
        style: Literal["primary", "secondary", "success", "danger"] = "secondary",
        emoji: Optional[str] = None
    ):
        target_ch = channel or ctx.channel
        btn_style = BUTTON_STYLES.get(style, discord.ButtonStyle.secondary)

        view = ui.View(timeout=None)
        view.add_item(RoleButton(role_id=role.id, label=label, emoji=emoji, style=btn_style))

        embed = discord.Embed(
            title="🎭 Role Selection",
            description=f"Click the button below to toggle the {role.mention} role!",
            color=0x5865F2
        )

        sent_msg = await target_ch.send(embed=embed, view=view)

        # Record in database
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
            INSERT INTO button_roles (guild_id, channel_id, message_id, role_id, emoji, label, button_style)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ctx.guild.id, target_ch.id, sent_msg.id, role.id, emoji, label, style))
            await conn.commit()

        await ctx.send(embed=success_embed("Button Role Created", f"Sent button role message to {target_ch.mention}."), ephemeral=True)

    # Member join listener for autoroles
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        async with aiosqlite.connect(db.db_path) as conn:
            cursor = await conn.execute("SELECT role_id FROM autoroles WHERE guild_id = ?", (member.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            return

        roles_to_add = [member.guild.get_role(r[0]) for r in rows if member.guild.get_role(r[0])]
        roles_to_add = [r for r in roles_to_add if r < member.guild.me.top_role]

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Sapphire Autorole on join")
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
