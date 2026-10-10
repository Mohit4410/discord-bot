import discord
from datetime import datetime, timezone
from typing import Optional

# Theme colors inspired by Sapphire and modern Discord UI
COLOR_SUCCESS = 0x2ECC71   # Emerald Green
COLOR_ERROR = 0xE74C3C     # Alizarin Red
COLOR_WARNING = 0xF39C12   # Sun Yellow / Orange
COLOR_INFO = 0x3498DB      # Sapphire Blue
COLOR_VOICE = 0x9B59B6     # Moveer Purple
COLOR_LOG = 0x5865F2       # Discord Blurple

def success_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=COLOR_SUCCESS,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    return embed

def error_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=COLOR_ERROR,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    return embed

def warning_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ {title}",
        description=description,
        color=COLOR_WARNING,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    return embed

def info_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"ℹ️ {title}",
        description=description,
        color=COLOR_INFO,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    return embed

def voice_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔊 {title}",
        description=description,
        color=COLOR_VOICE,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    return embed

def log_embed(
    title: str,
    description: str = "",
    color: int = COLOR_LOG,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    author_name: Optional[str] = None,
    author_icon: Optional[str] = None
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon or discord.utils.MISSING)
    return embed
