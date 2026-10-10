import re
import discord
from typing import Optional
from datetime import datetime, timezone

TIME_REGEX = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)

TIME_MULTIPLIERS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800
}

def parse_duration(duration_str: str) -> Optional[int]:
    """Parse time string like 10m, 1h, 2d, 30s into total seconds."""
    if not duration_str:
        return None
    
    total_seconds = 0
    matches = TIME_REGEX.findall(duration_str)
    
    if not matches:
        # Check if plain digits provided, default to seconds or minutes
        if duration_str.isdigit():
            return int(duration_str)
        return None
        
    for amount, unit in matches:
        unit = unit.lower()
        if unit in TIME_MULTIPLIERS:
            total_seconds += int(amount) * TIME_MULTIPLIERS[unit]
            
    return total_seconds if total_seconds > 0 else None

def format_duration(seconds: int) -> str:
    """Format seconds into readable format like 1h 25m 10s."""
    if seconds <= 0:
        return "0s"
    parts = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")
        
    return " ".join(parts)

def replace_placeholders(template: str, member: discord.Member, guild: discord.Guild) -> str:
    """Replace placeholders in messages and embed descriptions."""
    created_at = member.created_at.strftime("%Y-%m-%d %H:%M:%S")
    joined_at = member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown"
    
    replacements = {
        "{user}": str(member),
        "{user_mention}": member.mention,
        "{username}": member.name,
        "{user_display}": member.display_name,
        "{user_id}": str(member.id),
        "{server}": guild.name,
        "{server_name}": guild.name,
        "{guild}": guild.name,
        "{member_count}": str(guild.member_count),
        "{created_at}": created_at,
        "{joined_at}": joined_at,
    }
    
    result = template
    for key, val in replacements.items():
        result = result.replace(key, val)
    return result

def can_moderate(moderator: discord.Member, target: discord.Member) -> bool:
    """Check if moderator has role hierarchy over target."""
    if moderator.guild.owner_id == moderator.id:
        return True
    if target.guild.owner_id == target.id:
        return False
    return moderator.top_role > target.top_role
