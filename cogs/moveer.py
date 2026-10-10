import discord
from discord.ext import commands
from discord import app_commands
import random
from typing import Optional, List
from utils.embeds import voice_embed, error_embed, success_embed

class Moveer(commands.Cog, name="Voice Management (Moveer)"):
    """Moveer voice control commands: move, cmove, fmove, rmove, drag, spread, etc."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # 1. MOVE - Move single user
    @commands.hybrid_command(name="move", description="Move a user to a specific voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(user="The member to move", channel="Target voice channel")
    async def move(self, ctx: commands.Context, user: discord.Member, channel: discord.VoiceChannel):
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("Move Failed", f"{user.mention} is not in any voice channel!"), ephemeral=True)
        
        from_channel = user.voice.channel
        try:
            await user.move_to(channel)
            await ctx.send(embed=voice_embed(
                "User Moved",
                f"Successfully moved {user.mention} from **{from_channel.name}** to **{channel.name}**."
            ))
        except discord.Forbidden:
            await ctx.send(embed=error_embed("Permission Denied", "Bot lacks permission to move members to that channel."), ephemeral=True)
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    # 2. CMOVE - Move all users from channel A to channel B
    @commands.hybrid_command(name="cmove", description="Move all users from one voice channel to another")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(from_channel="Channel to move users from", to_channel="Channel to move users to")
    async def cmove(self, ctx: commands.Context, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel):
        if from_channel.id == to_channel.id:
            return await ctx.send(embed=error_embed("Invalid Operation", "Source and target channels cannot be identical!"), ephemeral=True)

        members = list(from_channel.members)
        if not members:
            return await ctx.send(embed=error_embed("Channel Empty", f"There is no one in **{from_channel.name}** to move."), ephemeral=True)

        await ctx.defer()
        moved = 0
        failed = 0
        for m in members:
            try:
                await m.move_to(to_channel)
                moved += 1
            except Exception:
                failed += 1

        await ctx.send(embed=voice_embed(
            "Channel Move Complete",
            f"Moved **{moved}** user(s) from **{from_channel.name}** to **{to_channel.name}**."
            + (f"\nFailed to move {failed} member(s)." if failed > 0 else "")
        ))

    # 3. FMOVE - Force move all users with a specific role across the server to a target voice channel
    @commands.hybrid_command(name="fmove", description="Move all users with a specific role in any voice channel to target channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(role="Role to filter by", channel="Target voice channel")
    async def fmove(self, ctx: commands.Context, role: discord.Role, channel: discord.VoiceChannel):
        await ctx.defer()
        targets = [m for m in role.members if m.voice and m.voice.channel and m.voice.channel.id != channel.id]
        
        if not targets:
            return await ctx.send(embed=error_embed("No Members Found", f"No members with role {role.mention} are currently connected to other voice channels."))

        moved = 0
        for m in targets:
            try:
                await m.move_to(channel)
                moved += 1
            except Exception:
                pass

        await ctx.send(embed=voice_embed(
            "Force Move Complete",
            f"Moved **{moved}** member(s) with role {role.mention} to **{channel.name}**."
        ))

    # 4. RMOVE - Move users with a role from your current channel to target channel
    @commands.hybrid_command(name="rmove", description="Move users with a role from your current channel to target channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(role="Role to move", channel="Target voice channel")
    async def rmove(self, ctx: commands.Context, role: discord.Role, channel: discord.VoiceChannel):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Voice Required", "You must be in a voice channel to use this command!"), ephemeral=True)

        current_channel = ctx.author.voice.channel
        targets = [m for m in current_channel.members if role in m.roles and m.voice and m.voice.channel]

        if not targets:
            return await ctx.send(embed=error_embed("No Role Members", f"No members with role {role.mention} found in **{current_channel.name}**."))

        await ctx.defer()
        moved = 0
        for m in targets:
            try:
                await m.move_to(channel)
                moved += 1
            except Exception:
                pass

        await ctx.send(embed=voice_embed(
            "Role Move Complete",
            f"Moved **{moved}** member(s) with role {role.mention} from **{current_channel.name}** to **{channel.name}**."
        ))

    # 5. DRAG - Drag member into your current voice channel
    @commands.hybrid_command(name="drag", description="Drag a member into your voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(user="User to drag into your channel")
    async def drag(self, ctx: commands.Context, user: discord.Member):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Voice Required", "You must be in a voice channel to drag users!"), ephemeral=True)
        
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("Target Not Connected", f"{user.mention} is not in any voice channel!"), ephemeral=True)

        target_channel = ctx.author.voice.channel
        if user.voice.channel.id == target_channel.id:
            return await ctx.send(embed=error_embed("Already Here", f"{user.mention} is already in your channel!"), ephemeral=True)

        try:
            await user.move_to(target_channel)
            await ctx.send(embed=voice_embed("User Dragged", f"Dragged {user.mention} into **{target_channel.name}**."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    # 6. MASSDRAG - Drag everyone from another channel to yours
    @commands.hybrid_command(name="massdrag", description="Drag everyone from another channel into your current channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(from_channel="Channel to drag members from")
    async def massdrag(self, ctx: commands.Context, from_channel: discord.VoiceChannel):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Voice Required", "You must be in a voice channel to use this command!"), ephemeral=True)

        target_channel = ctx.author.voice.channel
        if from_channel.id == target_channel.id:
            return await ctx.send(embed=error_embed("Invalid Operation", "Cannot drag from your own channel!"), ephemeral=True)

        members = list(from_channel.members)
        if not members:
            return await ctx.send(embed=error_embed("Channel Empty", f"No users found in **{from_channel.name}**."))

        await ctx.defer()
        moved = 0
        for m in members:
            try:
                await m.move_to(target_channel)
                moved += 1
            except Exception:
                pass

        await ctx.send(embed=voice_embed(
            "Mass Drag Complete",
            f"Dragged **{moved}** user(s) from **{from_channel.name}** into **{target_channel.name}**."
        ))

    # 7. SPREAD - Distribute users from your channel evenly across other channels
    @commands.hybrid_command(name="spread", description="Spread users from your channel across specified target channels (e.g. for teams)")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(channels="Comma-separated channel mentions or names (e.g. #team1, #team2)")
    async def spread(self, ctx: commands.Context, *, channels: str):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Voice Required", "You must be in a voice channel to spread users!"), ephemeral=True)

        # Parse voice channels
        target_channels = []
        raw_names = [ch.strip() for ch in channels.split(",") if ch.strip()]
        for name in raw_names:
            clean = name.replace("<#", "").replace(">", "").strip()
            ch = None
            if clean.isdigit():
                ch = ctx.guild.get_channel(int(clean))
            if not ch:
                ch = discord.utils.get(ctx.guild.voice_channels, name=clean)
            if ch and isinstance(ch, discord.VoiceChannel):
                target_channels.append(ch)

        if len(target_channels) < 2:
            return await ctx.send(embed=error_embed("Invalid Channels", "Please provide at least 2 valid voice channels separated by comma."), ephemeral=True)

        source_members = list(ctx.author.voice.channel.members)
        if not source_members:
            return await ctx.send(embed=error_embed("No Members", "No members in your current channel to spread."))

        random.shuffle(source_members)
        await ctx.defer()

        distribution = {ch.name: 0 for ch in target_channels}
        for idx, member in enumerate(source_members):
            target = target_channels[idx % len(target_channels)]
            try:
                await member.move_to(target)
                distribution[target.name] += 1
            except Exception:
                pass

        desc = "\n".join([f"• **{ch_name}**: {count} member(s)" for ch_name, count in distribution.items()])
        await ctx.send(embed=voice_embed("Users Spread", f"Distributed {len(source_members)} user(s):\n\n{desc}"))

    # 8. GMOVE - Move members playing a specific game
    @commands.hybrid_command(name="gmove", description="Move all members playing a specific game to a channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(game="Name of the game", channel="Target voice channel")
    async def gmove(self, ctx: commands.Context, game: str, channel: discord.VoiceChannel):
        await ctx.defer()
        targets = []
        game_lower = game.lower()

        for member in ctx.guild.members:
            if member.voice and member.voice.channel and member.voice.channel.id != channel.id:
                for act in member.activities:
                    if act.name and game_lower in act.name.lower():
                        targets.append(member)
                        break

        if not targets:
            return await ctx.send(embed=error_embed("No Gamers Found", f"No one in voice channels is currently playing **{game}**."))

        moved = 0
        for m in targets:
            try:
                await m.move_to(channel)
                moved += 1
            except Exception:
                pass

        await ctx.send(embed=voice_embed(
            "Game Move Complete",
            f"Moved **{moved}** player(s) of **{game}** to **{channel.name}**."
        ))

    # 9. RANDOMMOVE - Move a random user
    @commands.hybrid_command(name="randommove", description="Move a random user from author's channel to target channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(channel="Target voice channel")
    async def randommove(self, ctx: commands.Context, channel: discord.VoiceChannel):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Voice Required", "You must be in a voice channel!"), ephemeral=True)

        members = list(ctx.author.voice.channel.members)
        if not members:
            return await ctx.send(embed=error_embed("Empty", "No members in your channel."))

        chosen = random.choice(members)
        try:
            await chosen.move_to(channel)
            await ctx.send(embed=voice_embed("Random Move", f"🎲 Selected {chosen.mention} and moved them to **{channel.name}**!"))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)))

    # 10. VOICE KICK / DISCONNECT
    @commands.hybrid_command(name="vkick", description="Disconnect a user from voice")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(user="User to disconnect")
    async def vkick(self, ctx: commands.Context, user: discord.Member):
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not in a voice channel!"), ephemeral=True)

        try:
            await user.move_to(None)
            await ctx.send(embed=voice_embed("Voice Kick", f"Disconnected {user.mention} from voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    # 11. MASS VOICE KICK
    @commands.hybrid_command(name="massvkick", description="Disconnect all users from a voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(channel="Channel to clear")
    async def massvkick(self, ctx: commands.Context, channel: discord.VoiceChannel):
        members = list(channel.members)
        if not members:
            return await ctx.send(embed=error_embed("Empty Channel", f"No one is in **{channel.name}**."))

        await ctx.defer()
        disconnected = 0
        for m in members:
            try:
                await m.move_to(None)
                disconnected += 1
            except Exception:
                pass

        await ctx.send(embed=voice_embed("Mass Voice Kick", f"Disconnected **{disconnected}** user(s) from **{channel.name}**."))

    # 12. VOICE MUTE & UNMUTE
    @commands.hybrid_command(name="vmute", description="Server-mute a user in voice")
    @app_commands.default_permissions(mute_members=True)
    @commands.has_permissions(mute_members=True)
    @app_commands.describe(user="User to server mute")
    async def vmute(self, ctx: commands.Context, user: discord.Member):
        if not user.voice:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not connected to voice."), ephemeral=True)
        try:
            await user.edit(mute=True)
            await ctx.send(embed=voice_embed("Voice Muted", f"Server-muted {user.mention} in voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    @commands.hybrid_command(name="vunmute", description="Server-unmute a user in voice")
    @app_commands.default_permissions(mute_members=True)
    @commands.has_permissions(mute_members=True)
    @app_commands.describe(user="User to server unmute")
    async def vunmute(self, ctx: commands.Context, user: discord.Member):
        if not user.voice:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not connected to voice."), ephemeral=True)
        try:
            await user.edit(mute=False)
            await ctx.send(embed=voice_embed("Voice Unmuted", f"Server-unmuted {user.mention} in voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    # 13. VOICE DEAFEN & UNDEAFEN
    @commands.hybrid_command(name="vdeaf", description="Server-deafen a user in voice")
    @app_commands.default_permissions(deafen_members=True)
    @commands.has_permissions(deafen_members=True)
    @app_commands.describe(user="User to server deafen")
    async def vdeaf(self, ctx: commands.Context, user: discord.Member):
        if not user.voice:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not connected to voice."), ephemeral=True)
        try:
            await user.edit(deafen=True)
            await ctx.send(embed=voice_embed("Voice Deafened", f"Server-deafened {user.mention} in voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

    @commands.hybrid_command(name="vundeaf", description="Server-undeafen a user in voice")
    @app_commands.default_permissions(deafen_members=True)
    @commands.has_permissions(deafen_members=True)
    @app_commands.describe(user="User to server undeafen")
    async def vundeaf(self, ctx: commands.Context, user: discord.Member):
        if not user.voice:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not connected to voice."), ephemeral=True)
        try:
            await user.edit(deafen=False)
            await ctx.send(embed=voice_embed("Voice Undeafened", f"Server-undeafened {user.mention} in voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Error", str(e)), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Moveer(bot))
