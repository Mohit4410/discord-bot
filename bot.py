import os
import sys
import re
import io
import time
import shlex
import json
import base64
import random
import asyncio
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Literal, Tuple, Union
from collections import OrderedDict

import aiohttp
import aiosqlite
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ==============================================================================
# 1. LOGGING & ENVIRONMENT CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Bot")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("BOT_PREFIX", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_database.sqlite")
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media_cache")
RECENT_DIR = os.path.join(MEDIA_DIR, "recent")
PURGE_DIR = os.path.join(MEDIA_DIR, "purge")

os.makedirs(RECENT_DIR, exist_ok=True)
os.makedirs(PURGE_DIR, exist_ok=True)

# ==============================================================================
# 2. COLOR PALETTE & EMBED FACTORIES
# ==============================================================================
COLOR_SUCCESS = 0x2ECC71   # Emerald Green
COLOR_ERROR = 0xE74C3C     # Crimson Red
COLOR_WARNING = 0xF39C12   # Sun Orange / Yellow
COLOR_INFO = 0x3498DB      # Sapphire Blue
COLOR_VOICE = 0x9B59B6     # Moveer Purple
COLOR_LOG = 0x5865F2       # Discord Blurple
COLOR_PRIMARY = COLOR_INFO   # Sapphire Blue (Primary)

RESTORE_MASTER_USER_ID = 711955153196155011

def success_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=f"✅ {title}", description=description, color=COLOR_SUCCESS, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
    return embed

def error_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=f"❌ {title}", description=description, color=COLOR_ERROR, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
    return embed

def warning_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=f"⚠️ {title}", description=description, color=COLOR_WARNING, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
    return embed

def info_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=f"ℹ️ {title}", description=description, color=COLOR_INFO, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
    return embed

def voice_embed(title: str, description: str = "", footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=f"🔊 {title}", description=description, color=COLOR_VOICE, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
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
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if footer: embed.set_footer(text=footer)
    if thumbnail: embed.set_thumbnail(url=thumbnail)
    if author_name: embed.set_author(name=author_name, icon_url=author_icon or discord.utils.MISSING)
    return embed

# ==============================================================================
# 3. GENERAL HELPERS & TIME PARSING
# ==============================================================================
TIME_REGEX = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
TIME_MULTIPLIERS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}

def parse_duration(duration_str: str) -> Optional[int]:
    if not duration_str: return None
    total_seconds = 0
    matches = TIME_REGEX.findall(duration_str)
    if not matches:
        if duration_str.isdigit(): return int(duration_str)
        return None
    for amount, unit in matches:
        unit = unit.lower()
        if unit in TIME_MULTIPLIERS:
            total_seconds += int(amount) * TIME_MULTIPLIERS[unit]
    return total_seconds if total_seconds > 0 else None

def format_duration(seconds: int) -> str:
    if seconds <= 0: return "0s"
    parts = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    if seconds > 0 or not parts: parts.append(f"{seconds}s")
    return " ".join(parts)

def can_moderate(moderator: discord.Member, target: discord.Member) -> bool:
    if moderator.guild.owner_id == moderator.id: return True
    if target.guild.owner_id == target.id: return False
    return moderator.top_role > target.top_role

PURGE_CMD_PATTERN = re.compile(
    r"^[\/=!?.$\-~;+]\s*(purge|clear|userpurge|purgeuser|puser|restore|cleanbot|clean|prune|wipe)\b",
    re.IGNORECASE
)

def is_moderation_command_message(content: str) -> bool:
    if not content: return False
    return bool(PURGE_CMD_PATTERN.match(content.strip()))

# ==============================================================================
# 4. MEDIA MANAGER & ATTACHMENT CACHING
# ==============================================================================
_RECENT_CACHE: OrderedDict[int, List[dict]] = OrderedDict()
MAX_RECENT_MESSAGES = 100
MAX_DOWNLOAD_SIZE = 5 * 1024 * 1024  # 5 MB (reduced to prevent disk saturation)

DEFAULT_STORAGE_GUILD_ID = 1556861768490426400
STORAGE_GUILD_ID = int(os.getenv("STORAGE_GUILD_ID", str(DEFAULT_STORAGE_GUILD_ID)))

def cleanup_local_cache(max_age_seconds: int = 180, force_all: bool = False) -> int:
    """
    Cleans up local temporary media files to strictly prevent disk quota exhaustion (<15MB local usage).
    Returns number of deleted files.
    """
    deleted = 0
    now = time.time()
    for d in (RECENT_DIR, PURGE_DIR):
        if not os.path.exists(d):
            continue
        try:
            for fname in os.listdir(d):
                fpath = os.path.join(d, fname)
                if os.path.isfile(fpath):
                    try:
                        if force_all or (now - os.path.getmtime(fpath)) > max_age_seconds:
                            os.remove(fpath)
                            deleted += 1
                    except Exception:
                        pass
        except Exception:
            pass

    # Safety cap: enforce directory size strictly under 15MB
    for d in (RECENT_DIR, PURGE_DIR):
        if not os.path.exists(d):
            continue
        try:
            files = []
            total_size = 0
            for fname in os.listdir(d):
                fpath = os.path.join(d, fname)
                if os.path.isfile(fpath):
                    sz = os.path.getsize(fpath)
                    files.append((fpath, os.path.getmtime(fpath), sz))
                    total_size += sz
            if total_size > (15 * 1024 * 1024):
                files.sort(key=lambda x: x[1])
                for fpath, _, sz in files:
                    try:
                        os.remove(fpath)
                        deleted += 1
                        total_size -= sz
                        if total_size <= (5 * 1024 * 1024):
                            break
                    except Exception:
                        pass
        except Exception:
            pass
    return deleted

def get_local_cache_size_mb() -> float:
    """Returns the total size of local media cache directories in Megabytes."""
    total_bytes = 0
    for d in (RECENT_DIR, PURGE_DIR):
        if os.path.exists(d):
            for dirpath, _, filenames in os.walk(d):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total_bytes += os.path.getsize(fp)
                    except Exception:
                        pass
    return round(total_bytes / (1024 * 1024), 2)

def normalize_asset_str(url_or_str: str) -> str:
    """Normalize URLs and asset strings (handling cdn.discordapp.com vs media.discordapp.net, query params, etc.)."""
    if not url_or_str:
        return ""
    s = str(url_or_str).strip().lower()
    s = s.split("?")[0].split("#")[0]
    s = re.sub(r"^https?://", "", s)
    s = s.replace("media.discordapp.net", "discordapp.net")
    s = s.replace("cdn.discordapp.com", "discordapp.net")
    s = s.replace("c.tenor.com", "tenor.com")
    return s.strip("/")

def _save_file_sync(file_path: str, data: bytes):
    with open(file_path, "wb") as f:
        f.write(data)

async def download_media_url(url: str, dest_dir: str, prefix: str = "", filename: Optional[str] = None) -> Optional[dict]:
    if not url or not url.startswith("http"):
        return None
    try:
        clean_url = url.split("?")[0]
        ext = os.path.splitext(clean_url)[1].lower()
        if not ext or len(ext) > 5 or ext not in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"]:
            ext = ".png"
        if not filename:
            filename = f"media_{int(time.time() * 1000)}{ext}"
        safe_filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
        unique_filename = f"{prefix}_{safe_filename}"
        file_path = os.path.join(dest_dir, unique_filename)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) > MAX_DOWNLOAD_SIZE:
                    return None
                await asyncio.to_thread(_save_file_sync, file_path, data)
                content_type = resp.headers.get("Content-Type", "image/png")
                is_image = content_type.startswith("image/") or any(unique_filename.lower().endswith(e) for e in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
                return {
                    "file_path": file_path,
                    "filename": safe_filename,
                    "content_type": content_type,
                    "is_image": is_image,
                    "size": len(data),
                    "url": url
                }
    except Exception as e:
        logger.debug(f"Failed to download media URL {url}: {e}")
        return None

async def download_attachment(attachment: Any, dest_dir: str, prefix: str = "") -> Optional[dict]:
    att_size = getattr(attachment, "size", 0)
    if att_size and att_size > MAX_DOWNLOAD_SIZE:
        return None
    att_filename = getattr(attachment, "filename", "file")
    att_id = getattr(attachment, "id", int(time.time() * 1000))
    safe_filename = "".join(c for c in att_filename if c.isalnum() or c in "._- ")
    unique_filename = f"{prefix}_{att_id}_{safe_filename}"
    file_path = os.path.join(dest_dir, unique_filename)

    if hasattr(attachment, "read") and callable(attachment.read):
        try:
            data = await attachment.read()
            if len(data) > MAX_DOWNLOAD_SIZE:
                return None
            await asyncio.to_thread(_save_file_sync, file_path, data)
            content_type = getattr(attachment, "content_type", None) or "application/octet-stream"
            is_image = content_type.startswith("image/") or any(safe_filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
            return {
                "file_path": file_path,
                "filename": safe_filename,
                "content_type": content_type,
                "is_image": is_image,
                "size": len(data),
                "url": getattr(attachment, "url", "")
            }
        except Exception:
            pass

    url = getattr(attachment, "url", None)
    if url:
        return await download_media_url(url, dest_dir, prefix=prefix, filename=safe_filename)
    return None

async def cache_message_media(message: discord.Message, bot: Optional[commands.Bot] = None):
    """Cache attachments and embed media from incoming messages and offload them to the Discord storage server."""
    if not message or not getattr(message, "guild", None):
        return
    saved_items = []

    # 1. Direct attachments
    if getattr(message, "attachments", None):
        for att in message.attachments:
            item = await download_attachment(att, RECENT_DIR, prefix=f"msg_{message.id}")
            if item:
                saved_items.append(item)

    # 2. Embed images & media (common for bots like NotSoBot, image generators, webhooks)
    if getattr(message, "embeds", None):
        for idx, em in enumerate(message.embeds):
            img_url = (em.image.url if em.image else None) or (em.thumbnail.url if em.thumbnail else None)
            if img_url:
                if not any(it.get("url") == img_url for it in saved_items):
                    item = await download_media_url(img_url, RECENT_DIR, prefix=f"msg_{message.id}_em{idx}")
                    if item:
                        saved_items.append(item)

    # 3. Media URLs in message content if no other media saved
    if not saved_items and getattr(message, "content", None):
        content_urls = re.findall(r"(https?://\S+)", message.content)
        for idx, url in enumerate(content_urls[:3]):
            clean_u = url.split("?")[0].lower()
            if any(clean_u.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4")) or any(h in url.lower() for h in ("cdn.discordapp.com", "media.discordapp.net", "tenor.com", "giphy.com", "notsobot")):
                item = await download_media_url(url, RECENT_DIR, prefix=f"msg_{message.id}_link{idx}")
                if item:
                    saved_items.append(item)

    if saved_items:
        _RECENT_CACHE[message.id] = saved_items
        while len(_RECENT_CACHE) > MAX_RECENT_MESSAGES:
            oldest_id, old_items = _RECENT_CACHE.popitem(last=False)
            for old_item in old_items:
                fp = old_item.get("file_path")
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

def get_cached_media(message_id: int) -> List[dict]:
    return _RECENT_CACHE.get(message_id, [])

def create_discord_files_from_paths(paths: List[str]) -> List[discord.File]:
    files = []
    for fp in paths:
        if os.path.exists(fp):
            try:
                basename = os.path.basename(fp)
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                files.append(discord.File(fp, filename=display_name))
            except Exception as e:
                logger.error(f"Failed to prepare file {fp}: {e}")
    return files

def get_base64_data_uri(file_path: str, content_type: Optional[str] = None) -> Optional[str]:
    if not os.path.exists(file_path): return None
    try:
        if os.path.getsize(file_path) > 3 * 1024 * 1024: return None
        with open(file_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ctype = content_type or "image/png"
        return f"data:{ctype};base64,{encoded}"
    except Exception:
        return None

# ==============================================================================
# 5. HTML TRANSCRIPT GENERATOR
# ==============================================================================
import html

MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
MARKDOWN_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
MARKDOWN_UNDERLINE = re.compile(r"__(.+?)__")
MARKDOWN_STRIKE = re.compile(r"~~(.+?)~~")
MARKDOWN_CODEBLOCK = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", re.DOTALL)
MARKDOWN_INLINE_CODE = re.compile(r"`([^`]+)`")
MARKDOWN_SPOILER = re.compile(r"\|\|(.+?)\|\|")
URL_REGEX = re.compile(r"(https?://[^\s<]+)")
USER_MENTION = re.compile(r"<@!?(\d+)>")
ROLE_MENTION = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION = re.compile(r"<#(\d+)>")

def format_content(content: str, guild: Optional[discord.Guild] = None) -> str:
    if not content: return ""
    text = html.escape(content)
    text = MARKDOWN_CODEBLOCK.sub(lambda m: f'<pre class="codeblock"><code class="language-{m.group(1)}">{m.group(2)}</code></pre>', text)
    text = MARKDOWN_INLINE_CODE.sub(r'<code class="inline-code">\1</code>', text)
    text = MARKDOWN_SPOILER.sub(r'<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">\1</span>', text)
    text = MARKDOWN_BOLD.sub(r"<strong>\1</strong>", text)
    text = MARKDOWN_UNDERLINE.sub(r"<u>\1</u>", text)
    text = MARKDOWN_STRIKE.sub(r"<s>\1</s>", text)
    text = MARKDOWN_ITALIC.sub(r"<em>\1</em>", text)

    def user_sub(m):
        uid = int(m.group(1))
        name = f"@{uid}"
        if guild:
            member = guild.get_member(uid)
            if member: name = f"@{member.display_name}"
        return f'<span class="mention">{html.escape(name)}</span>'

    def channel_sub(m):
        cid = int(m.group(1))
        cname = f"#{cid}"
        if guild:
            ch = guild.get_channel(cid)
            if ch: cname = f"#{ch.name}"
        return f'<span class="mention">{html.escape(cname)}</span>'

    def role_sub(m):
        rid = int(m.group(1))
        rname = f"@{rid}"
        if guild:
            role = guild.get_role(rid)
            if role: rname = f"@{role.name}"
        return f'<span class="mention">{html.escape(rname)}</span>'

    text = USER_MENTION.sub(user_sub, text)
    text = CHANNEL_MENTION.sub(channel_sub, text)
    text = ROLE_MENTION.sub(role_sub, text)
    text = URL_REGEX.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', text)
    text = text.replace("\n", "<br>")
    return text

def generate_html_transcript(
    guild: discord.Guild,
    channel: Any,
    moderator: Any,
    messages: List[Any],
    reason: Optional[str] = None,
    media_map: Optional[dict] = None
) -> str:
    sorted_messages = sorted(messages, key=lambda m: m.created_at)
    guild_name = html.escape(guild.name)
    channel_name = html.escape(getattr(channel, "name", "channel"))
    mod_name = html.escape(str(moderator))
    mod_id = getattr(moderator, "id", 0)
    guild_icon = guild.icon.url if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
    now_str = datetime.now(timezone.utc).strftime("%d-%b-%Y %H:%M:%S UTC")
    total_count = len(sorted_messages)

    message_blocks = []
    for msg in sorted_messages:
        author = msg.author
        author_name = html.escape(author.display_name)
        username = html.escape(str(author))
        author_id = author.id
        avatar_url = author.display_avatar.url if hasattr(author, "display_avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
        timestamp_str = msg.created_at.strftime("%d-%b-%Y %H:%M:%S")
        content_html = format_content(msg.content, guild)
        bot_badge = '<span class="bot-badge">BOT</span>' if getattr(author, "bot", False) else ""

        attachments_html = ""
        msg_saved_media = media_map.get(msg.id, []) if media_map else []
        saved_by_filename = {item["filename"]: item for item in msg_saved_media}

        if getattr(msg, "attachments", None):
            att_items = []
            for att in msg.attachments:
                filename = html.escape(att.filename)
                url = att.url
                saved_item = saved_by_filename.get(att.filename)
                base64_src = None
                if saved_item and saved_item.get("is_image"):
                    base64_src = get_base64_data_uri(saved_item["file_path"], saved_item.get("content_type"))

                img_src = base64_src or url
                is_image = any(filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
                if is_image:
                    att_items.append(f'''
                    <div class="attachment-image-card">
                        <a href="{url}" target="_blank">
                            <img src="{img_src}" alt="{filename}" loading="lazy" class="attachment-img" onerror="this.style.display='none'"/>
                        </a>
                        <div class="attachment-meta"><a href="{url}" target="_blank">{filename}</a></div>
                    </div>''')
                else:
                    att_items.append(f'''
                    <div class="attachment-file-card">
                        <span class="file-icon">📁</span>
                        <a href="{url}" target="_blank" class="file-link">{filename}</a>
                    </div>''')
            attachments_html = f'<div class="attachments-container">{"".join(att_items)}</div>'

        embeds_html = ""
        if getattr(msg, "embeds", None):
            embed_items = []
            for em in msg.embeds:
                em_color = f"#{em.color.value:06x}" if em.color else "#5865f2"
                em_title = f'<div class="embed-title">{html.escape(em.title)}</div>' if em.title else ""
                em_desc = f'<div class="embed-desc">{format_content(em.description or "", guild)}</div>' if em.description else ""
                fields_html = ""
                if em.fields:
                    f_list = []
                    for f in em.fields:
                        inline_cls = " embed-field-inline" if f.inline else ""
                        f_list.append(f'''
                        <div class="embed-field{inline_cls}">
                            <div class="embed-field-name">{html.escape(f.name)}</div>
                            <div class="embed-field-value">{format_content(f.value, guild)}</div>
                        </div>''')
                    fields_html = f'<div class="embed-fields">{"".join(f_list)}</div>'

                em_img = ""
                if getattr(em, "image", None) and getattr(em.image, "url", None):
                    img_src = em.image.url
                    if msg_saved_media:
                        for s_item in msg_saved_media:
                            if s_item.get("is_image") and (s_item.get("url") == em.image.url or not getattr(msg, "attachments", None)):
                                b64 = get_base64_data_uri(s_item["file_path"], s_item.get("content_type"))
                                if b64:
                                    img_src = b64
                                    break
                    em_img = f'<div class="embed-img-wrap"><img src="{img_src}" class="embed-image"/></div>'
                em_thumb = f'<img src="{em.thumbnail.url}" class="embed-thumbnail"/>' if getattr(em, "thumbnail", None) and getattr(em.thumbnail, "url", None) else ""

                embed_items.append(f'''
                <div class="embed-card" style="border-left-color: {em_color};">
                    <div class="embed-content">
                        {em_title}
                        {em_desc}
                        {fields_html}
                        {em_img}
                    </div>
                    {em_thumb}
                </div>''')
            embeds_html = f'<div class="embeds-container">{"".join(embed_items)}</div>'

        message_blocks.append(f'''
        <div class="chat-message" id="msg-{msg.id}">
            <img src="{avatar_url}" alt="{author_name}" class="avatar"/>
            <div class="message-body">
                <div class="message-header">
                    <span class="author-name">{author_name}</span>
                    {bot_badge}
                    <span class="author-username">({username})</span>
                    <span class="timestamp">{timestamp_str}</span>
                </div>
                <div class="message-content">{content_html}</div>
                {attachments_html}
                {embeds_html}
            </div>
        </div>''')

    reason_row = f'''<div class="meta-row"><span class="meta-label">Reason:</span><span class="meta-val">{html.escape(reason)}</span></div>''' if reason else ""

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Purge Transcript - #{channel_name} - {guild_name}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background-color: #313338; color: #dbdee1; font-family: "gg sans", "Noto Sans", Helvetica, Arial, sans-serif; font-size: 15px; line-height: 1.375; }}
.container {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
.header {{ background-color: #2b2d31; border-radius: 8px; padding: 20px; margin-bottom: 24px; border: 1px solid #1e1f22; display: flex; gap: 20px; align-items: center; }}
.header-icon {{ width: 68px; height: 68px; border-radius: 50%; object-fit: cover; border: 2px solid #5865f2; }}
.header-info {{ flex: 1; }}
.header-title {{ font-size: 20px; font-weight: 700; color: #f2f3f5; margin-bottom: 6px; }}
.meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin-top: 10px; }}
.meta-row {{ font-size: 13px; color: #949ba4; }}
.meta-label {{ font-weight: 600; color: #b5bac1; margin-right: 6px; }}
.meta-val {{ color: #f2f3f5; }}
.chat-container {{ background-color: #313338; border-radius: 8px; padding: 12px; }}
.chat-message {{ display: flex; gap: 16px; padding: 8px 12px; border-radius: 4px; transition: background-color 0.15s; margin-bottom: 4px; }}
.chat-message:hover {{ background-color: #2e3035; }}
.avatar {{ width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; object-fit: cover; margin-top: 2px; }}
.message-body {{ flex: 1; min-width: 0; }}
.message-header {{ display: flex; align-items: baseline; gap: 6px; margin-bottom: 4px; }}
.author-name {{ font-weight: 600; color: #f2f3f5; font-size: 15px; }}
.bot-badge {{ background-color: #5865f2; color: #ffffff; font-size: 10px; font-weight: 700; padding: 1px 4px; border-radius: 3px; }}
.author-username {{ font-size: 12px; color: #949ba4; }}
.timestamp {{ font-size: 11px; color: #949ba4; margin-left: 4px; }}
.message-content {{ color: #dbdee1; word-break: break-word; }}
.mention {{ background-color: rgba(88, 101, 242, 0.3); color: #c9cdfb; padding: 0 4px; border-radius: 3px; font-weight: 500; }}
.inline-code {{ background-color: #1e1f22; padding: 2px 4px; border-radius: 3px; font-family: monospace; font-size: 85%; color: #dbdee1; }}
.codeblock {{ background-color: #1e1f22; border-radius: 4px; padding: 10px; margin: 6px 0; overflow-x: auto; font-family: monospace; font-size: 13px; color: #dbdee1; }}
.spoiler {{ background-color: #1e1f22; color: transparent; border-radius: 3px; padding: 0 2px; cursor: pointer; user-select: none; }}
.spoiler.revealed {{ background-color: rgba(255,255,255,0.1); color: inherit; }}
.attachments-container {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px; }}
.attachment-image-card {{ max-width: 400px; border-radius: 6px; overflow: hidden; background-color: #2b2d31; border: 1px solid #1e1f22; }}
.attachment-img {{ max-width: 100%; max-height: 300px; display: block; object-fit: contain; }}
.attachment-meta {{ padding: 6px 10px; font-size: 12px; background-color: #1e1f22; }}
.attachment-meta a {{ color: #00a8fc; text-decoration: none; }}
.attachment-file-card {{ display: flex; align-items: center; gap: 8px; background-color: #2b2d31; padding: 10px 14px; border-radius: 6px; border: 1px solid #1e1f22; font-size: 13px; }}
.file-link {{ color: #00a8fc; text-decoration: none; word-break: break-all; }}
.embeds-container {{ margin-top: 8px; }}
.embed-card {{ display: flex; border-left: 4px solid #5865f2; background-color: #2b2d31; border-radius: 4px; padding: 12px 16px; max-width: 520px; margin-bottom: 6px; }}
.embed-content {{ flex: 1; min-width: 0; }}
.embed-title {{ font-weight: 700; color: #f2f3f5; margin-bottom: 6px; font-size: 15px; }}
.embed-desc {{ color: #dbdee1; font-size: 14px; margin-bottom: 8px; }}
.embed-fields {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; margin-top: 6px; }}
.embed-field-name {{ font-size: 13px; font-weight: 600; color: #f2f3f5; }}
.embed-field-value {{ font-size: 13px; color: #dbdee1; margin-top: 2px; }}
.embed-thumbnail {{ width: 80px; height: 80px; border-radius: 4px; object-fit: cover; margin-left: 16px; flex-shrink: 0; }}
.embed-image {{ max-width: 100%; border-radius: 4px; margin-top: 8px; }}
.footer {{ text-align: center; color: #949ba4; font-size: 12px; margin-top: 30px; padding: 16px; border-top: 1px solid #1e1f22; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <img src="{guild_icon}" alt="Guild Icon" class="header-icon"/>
        <div class="header-info">
            <div class="header-title">Purge Transcript — #{channel_name}</div>
            <div class="meta-grid">
                <div class="meta-row"><span class="meta-label">Server:</span><span class="meta-val">{guild_name}</span></div>
                <div class="meta-row"><span class="meta-label">Channel:</span><span class="meta-val">#{channel_name}</span></div>
                <div class="meta-row"><span class="meta-label">Moderator:</span><span class="meta-val">{mod_name}</span></div>
                <div class="meta-row"><span class="meta-label">Messages Purged:</span><span class="meta-val">{total_count}</span></div>
                <div class="meta-row"><span class="meta-label">Generated At:</span><span class="meta-val">{now_str}</span></div>
                {reason_row}
            </div>
        </div>
    </div>
    <div class="chat-container">
        {"".join(message_blocks) if message_blocks else '<div style="padding:20px;text-align:center;color:#949ba4;">No messages recorded.</div>'}
    </div>
    <div class="footer">Moveer & Sapphire All-in-One Bot • Self-Contained HTML Transcript</div>
</div>
</body>
</html>'''

# ==============================================================================
# 6. SQLITE DATABASE SCHEMA & MANAGER
# ==============================================================================
class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def init_tables(self):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT DEFAULT NULL,
                restore_role_id INTEGER DEFAULT NULL
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS logging_config (
                guild_id INTEGER,
                log_type TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, log_type)
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS temp_voice_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                reminder_text TEXT NOT NULL,
                remind_at REAL NOT NULL
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS purged_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                author_avatar_url TEXT NOT NULL,
                content TEXT,
                attachments TEXT,
                media_files TEXT DEFAULT '',
                embeds TEXT DEFAULT '',
                is_webhook INTEGER DEFAULT 0,
                timestamp REAL NOT NULL
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS backed_up_webhooks (
                webhook_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                timestamp REAL NOT NULL
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS afk_users (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                reason TEXT DEFAULT 'AFK',
                afk_time REAL NOT NULL,
                original_nick TEXT DEFAULT NULL,
                gif_url TEXT DEFAULT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
            """)
            try:
                await conn.execute("ALTER TABLE afk_users ADD COLUMN gif_url TEXT DEFAULT NULL")
            except Exception: pass
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS restore_allowed_users (
                target_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                target_type TEXT DEFAULT 'user',
                message_limit INTEGER NOT NULL DEFAULT 20,
                added_at REAL NOT NULL,
                PRIMARY KEY (target_id, guild_id)
            )
            """)
            try:
                await conn.execute("ALTER TABLE restore_allowed_users ADD COLUMN message_limit INTEGER NOT NULL DEFAULT 20")
            except Exception: pass
            try:
                await conn.execute("ALTER TABLE guild_settings ADD COLUMN restrict_exempt_staff INTEGER DEFAULT 1")
            except Exception: pass
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_responders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                trigger_text TEXT NOT NULL,
                response_text TEXT NOT NULL,
                match_type TEXT NOT NULL DEFAULT 'contains',
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS restricted_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                restrict_type TEXT NOT NULL,
                content TEXT DEFAULT NULL,
                channel_id INTEGER DEFAULT NULL,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_restricted_content_guild ON restricted_content (guild_id)")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS replay_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                suppress_original INTEGER NOT NULL DEFAULT 1,
                twitter_enabled INTEGER NOT NULL DEFAULT 1,
                instagram_enabled INTEGER NOT NULL DEFAULT 1,
                youtube_enabled INTEGER NOT NULL DEFAULT 1,
                tiktok_enabled INTEGER NOT NULL DEFAULT 1,
                reddit_enabled INTEGER NOT NULL DEFAULT 1,
                disabled_channels TEXT NOT NULL DEFAULT ''
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS module_settings (
                guild_id INTEGER NOT NULL,
                module_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, module_name)
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_storage_channels (
                guild_id INTEGER PRIMARY KEY,
                storage_guild_id INTEGER NOT NULL,
                category_id INTEGER,
                messages_channel_id INTEGER,
                media_channel_id INTEGER,
                purges_channel_id INTEGER,
                created_at REAL NOT NULL
            )
            """)
            await conn.commit()

    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
                await conn.commit()
                cursor = await conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
                row = await cursor.fetchone()
            return dict(row) if row else {}

    async def update_guild_setting(self, guild_id: int, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            await conn.execute(f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
            await conn.commit()

    async def get_restore_role_id(self, guild_id: int) -> Optional[int]:
        settings = await self.get_guild_settings(guild_id)
        return settings.get("restore_role_id")

    async def set_restore_role_id(self, guild_id: int, role_id: Optional[int]):
        await self.update_guild_setting(guild_id, "restore_role_id", role_id)

    async def get_restrict_exempt_staff(self, guild_id: int) -> bool:
        settings = await self.get_guild_settings(guild_id)
        return bool(settings.get("restrict_exempt_staff", 1))

    async def set_restrict_exempt_staff(self, guild_id: int, exempt: bool):
        val = 1 if exempt else 0
        await self.update_guild_setting(guild_id, "restrict_exempt_staff", val)

    async def add_restore_allowed(self, target_id: int, guild_id: int, added_by: int, target_type: str = "user", message_limit: int = 20):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO restore_allowed_users (target_id, guild_id, added_by, target_type, message_limit, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (target_id, guild_id, added_by, target_type, message_limit, time.time()))
            await conn.commit()

    async def remove_restore_allowed(self, target_id: int, guild_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            DELETE FROM restore_allowed_users WHERE target_id = ? AND (guild_id = ? OR guild_id = 0)
            """, (target_id, guild_id))
            await conn.commit()

    async def get_restore_allowed(self, guild_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM restore_allowed_users WHERE guild_id = ? OR guild_id = 0 ORDER BY added_at ASC
            """, (guild_id,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_restore_permission(self, user_id: int, guild_id: int, role_ids: Optional[List[int]] = None, is_owner: bool = False) -> Tuple[bool, Optional[int]]:
        if user_id == RESTORE_MASTER_USER_ID or is_owner:
            return (True, None)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            check_ids = [user_id]
            if role_ids:
                check_ids.extend(role_ids)
            placeholders = ",".join("?" for _ in check_ids)
            cursor = await conn.execute(f"""
            SELECT message_limit FROM restore_allowed_users 
            WHERE target_id IN ({placeholders}) AND (guild_id = ? OR guild_id = 0)
            ORDER BY message_limit DESC
            """, (*check_ids, guild_id))
            row = await cursor.fetchone()
            if row:
                return (True, row["message_limit"])
            return (False, 0)

    async def is_restore_allowed(self, user_id: int, guild_id: int, role_ids: Optional[List[int]] = None) -> bool:
        allowed, _ = await self.get_restore_permission(user_id, guild_id, role_ids)
        return allowed

    async def set_log_channel(self, guild_id: int, log_type: str, channel_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO logging_config (guild_id, log_type, channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, log_type) DO UPDATE SET channel_id = excluded.channel_id
            """, (guild_id, log_type, channel_id))
            await conn.commit()

    async def get_log_channel(self, guild_id: int, log_type: str) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT channel_id FROM logging_config WHERE guild_id = ? AND log_type = ?",
                (guild_id, log_type)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_all_logging_configs(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM logging_config")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_guild_logging_config(self, guild_id: int) -> Dict[str, int]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT log_type, channel_id FROM logging_config WHERE guild_id = ?", (guild_id,))
            rows = await cursor.fetchall()
            return {r["log_type"]: r["channel_id"] for r in rows}

    async def add_temp_channel(self, channel_id: int, guild_id: int, owner_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO temp_voice_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
                (channel_id, guild_id, owner_id)
            )
            await conn.commit()

    async def remove_temp_channel(self, channel_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
            await conn.commit()

    async def get_temp_channel_owner(self, channel_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT owner_id FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def save_purged_messages(self, messages_data: List[Dict[str, Any]]):
        if not messages_data: return
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executemany("""
            INSERT INTO purged_messages (guild_id, channel_id, author_id, author_name, author_avatar_url, content, attachments, media_files, embeds, is_webhook, timestamp)
            VALUES (:guild_id, :channel_id, :author_id, :author_name, :author_avatar_url, :content, :attachments, :media_files, :embeds, :is_webhook, :timestamp)
            """, messages_data)
            await conn.commit()

    async def get_purged_messages_to_restore(self, channel_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM purged_messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?
            """, (channel_id, limit))
            rows = await cursor.fetchall()
            msgs = [dict(r) for r in rows]
            # Chronological order of original chat (oldest message first -> newest message last)
            msgs.sort(key=lambda m: (m.get("timestamp") or 0, m.get("id") or 0))
            return msgs

    async def get_user_purged_messages(self, guild_id: int, author_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM purged_messages WHERE guild_id = ? AND author_id = ? ORDER BY id DESC LIMIT ?
            """, (guild_id, author_id, limit))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def delete_restored_messages(self, ids: List[int]):
        if not ids: return
        async with aiosqlite.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(f"DELETE FROM purged_messages WHERE id IN ({placeholders})", ids)
            await conn.commit()

    async def save_webhook_backup(self, webhook_id: int, guild_id: int, channel_id: int, name: str, avatar_url: str):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO backed_up_webhooks (webhook_id, guild_id, channel_id, name, avatar_url, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(webhook_id) DO UPDATE SET name = excluded.name, avatar_url = excluded.avatar_url
            """, (webhook_id, guild_id, channel_id, name, avatar_url, time.time()))
            await conn.commit()

    async def get_backed_up_webhooks(self, guild_id: int, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if channel_id:
                cursor = await conn.execute("SELECT * FROM backed_up_webhooks WHERE guild_id = ? AND channel_id = ? ORDER BY timestamp DESC", (guild_id, channel_id))
            else:
                cursor = await conn.execute("SELECT * FROM backed_up_webhooks WHERE guild_id = ? ORDER BY timestamp DESC", (guild_id,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def set_afk(self, user_id: int, guild_id: int, reason: str, original_nick: Optional[str] = None, gif_url: Optional[str] = None):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO afk_users (user_id, guild_id, reason, afk_time, original_nick, gif_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                reason = excluded.reason,
                afk_time = excluded.afk_time,
                original_nick = excluded.original_nick,
                gif_url = excluded.gif_url
            """, (user_id, guild_id, reason, time.time(), original_nick, gif_url))
            await conn.commit()

    async def get_afk(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM afk_users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def remove_afk(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM afk_users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
            row = await cursor.fetchone()
            if row:
                await conn.execute("DELETE FROM afk_users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                await conn.commit()
                return dict(row)
            return None

    async def add_auto_responder(self, guild_id: int, trigger_text: str, response_text: str, match_type: str = "contains", created_by: int = 0) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            INSERT INTO auto_responders (guild_id, trigger_text, response_text, match_type, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (guild_id, trigger_text.strip(), response_text.strip(), match_type.lower(), created_by, time.time()))
            await conn.commit()
            return cursor.lastrowid

    async def remove_auto_responder(self, guild_id: int, responder_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM auto_responders WHERE id = ? AND guild_id = ?
            """, (responder_id, guild_id))
            await conn.commit()
            return cursor.rowcount > 0

    async def remove_auto_responder_by_trigger(self, guild_id: int, trigger_text: str) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM auto_responders WHERE guild_id = ? AND LOWER(trigger_text) = LOWER(?)
            """, (guild_id, trigger_text.strip()))
            await conn.commit()
            return cursor.rowcount

    async def get_auto_responders(self, guild_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM auto_responders WHERE guild_id = ? ORDER BY id ASC
            """, (guild_id,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def clear_auto_responders(self, guild_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM auto_responders WHERE guild_id = ?
            """, (guild_id,))
            await conn.commit()
            return cursor.rowcount

    async def add_restriction(self, guild_id: int, restrict_type: str, content: Optional[str] = None, channel_id: Optional[int] = None, created_by: int = 0) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            INSERT INTO restricted_content (guild_id, restrict_type, content, channel_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (guild_id, restrict_type.lower(), content.strip() if content else None, channel_id, created_by, time.time()))
            await conn.commit()
            return cursor.lastrowid

    async def remove_restriction(self, guild_id: int, restriction_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM restricted_content WHERE id = ? AND guild_id = ?
            """, (restriction_id, guild_id))
            await conn.commit()
            return cursor.rowcount > 0

    async def remove_restriction_by_query(self, guild_id: int, query: str) -> int:
        clean = query.strip().lower()
        role_id_clean = re.sub(r"[<@&>]", "", query).strip()
        norm_q = normalize_asset_str(query)
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM restricted_content 
            WHERE guild_id = ? AND (LOWER(content) = ? OR LOWER(restrict_type) = ? OR (restrict_type = 'role' AND content = ?))
            """, (guild_id, clean, clean, role_id_clean))
            deleted = cursor.rowcount
            if not deleted and norm_q:
                cursor2 = await conn.execute("SELECT id, content FROM restricted_content WHERE guild_id = ?", (guild_id,))
                rows = await cursor2.fetchall()
                ids_to_del = [r[0] for r in rows if r[1] and normalize_asset_str(r[1]) == norm_q]
                if ids_to_del:
                    placeholders = ",".join("?" for _ in ids_to_del)
                    c3 = await conn.execute(f"DELETE FROM restricted_content WHERE id IN ({placeholders})", ids_to_del)
                    deleted = c3.rowcount
            await conn.commit()
            return deleted

    async def get_restrictions(self, guild_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM restricted_content WHERE guild_id = ? ORDER BY id ASC
            """, (guild_id,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def clear_restrictions(self, guild_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            DELETE FROM restricted_content WHERE guild_id = ?
            """, (guild_id,))
            await conn.commit()
            return cursor.rowcount

    async def get_replay_settings(self, guild_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM replay_settings WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.execute("""
                INSERT OR IGNORE INTO replay_settings (guild_id) VALUES (?)
                """, (guild_id,))
                await conn.commit()
                cursor = await conn.execute("SELECT * FROM replay_settings WHERE guild_id = ?", (guild_id,))
                row = await cursor.fetchone()
            if row:
                return dict(row)
            return {
                "guild_id": guild_id,
                "enabled": 1,
                "suppress_original": 1,
                "twitter_enabled": 1,
                "instagram_enabled": 1,
                "youtube_enabled": 1,
                "tiktok_enabled": 1,
                "reddit_enabled": 1,
                "disabled_channels": ""
            }

    async def update_replay_setting(self, guild_id: int, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO replay_settings (guild_id) VALUES (?)", (guild_id,))
            await conn.execute(f"UPDATE replay_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
            await conn.commit()

    async def toggle_replay_channel(self, guild_id: int, channel_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO replay_settings (guild_id) VALUES (?)", (guild_id,))
            cursor = await conn.execute("SELECT disabled_channels FROM replay_settings WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            ch_str = (row[0] if row and row[0] else "").strip()
            ch_list = [int(x) for x in ch_str.split(",") if x.strip().isdigit()]
            if channel_id in ch_list:
                ch_list.remove(channel_id)
                is_disabled = False
            else:
                ch_list.append(channel_id)
                is_disabled = True
            new_str = ",".join(str(x) for x in ch_list)
            await conn.execute("UPDATE replay_settings SET disabled_channels = ? WHERE guild_id = ?", (new_str, guild_id))
            await conn.commit()
            return is_disabled

    async def get_module_status(self, guild_id: int, module_name: str) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT enabled FROM module_settings WHERE guild_id = ? AND module_name = ?",
                (guild_id, module_name.lower())
            )
            row = await cursor.fetchone()
            if row is not None:
                return bool(row[0])
            if module_name.lower() == "replay":
                r_settings = await self.get_replay_settings(guild_id)
                return bool(r_settings.get("enabled", 1))
            return True

    async def set_module_status(self, guild_id: int, module_name: str, enabled: bool):
        val = 1 if enabled else 0
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO module_settings (guild_id, module_name, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, module_name) DO UPDATE SET enabled = excluded.enabled
            """, (guild_id, module_name.lower(), val))
            await conn.commit()
        if module_name.lower() == "replay":
            await self.update_replay_setting(guild_id, "enabled", val)

    async def get_all_module_statuses(self, guild_id: int) -> Dict[str, bool]:
        defaults = {
            "moveer": True,
            "tempvoice": True,
            "autoresponder": True,
            "restrict": True,
            "replay": True,
            "restore": True,
            "afk": True,
            "logging": True
        }
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT module_name, enabled FROM module_settings WHERE guild_id = ?",
                (guild_id,)
            )
            rows = await cursor.fetchall()
            for mod_name, enabled in rows:
                if mod_name.lower() in defaults:
                    defaults[mod_name.lower()] = bool(enabled)
            r_settings = await self.get_replay_settings(guild_id)
            defaults["replay"] = bool(r_settings.get("enabled", 1))
            return defaults

    async def get_storage_channels(self, guild_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM server_storage_channels WHERE guild_id = ?",
                (guild_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_storage_channels(
        self,
        guild_id: int,
        storage_guild_id: int,
        category_id: int,
        messages_channel_id: int,
        media_channel_id: int,
        purges_channel_id: int
    ):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO server_storage_channels (
                guild_id, storage_guild_id, category_id, messages_channel_id, media_channel_id, purges_channel_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, storage_guild_id, category_id, messages_channel_id, media_channel_id, purges_channel_id, time.time()))
            await conn.commit()

    async def clear_storage_channels(self, guild_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("DELETE FROM server_storage_channels WHERE guild_id = ?", (guild_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def prune_purged_messages(self, max_per_guild: int = 500) -> int:
        """Keeps sqlite lean by pruning old purge entries exceeding max_per_guild."""
        pruned = 0
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute("SELECT DISTINCT guild_id FROM purged_messages")
                guild_rows = await cursor.fetchall()
                for (gid,) in guild_rows:
                    del_cursor = await conn.execute("""
                    DELETE FROM purged_messages 
                    WHERE guild_id = ? AND id NOT IN (
                        SELECT id FROM purged_messages WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                    )
                    """, (gid, gid, max_per_guild))
                    pruned += del_cursor.rowcount
                await conn.commit()
        except Exception:
            pass
        return pruned

    async def vacuum(self):
        """Reclaims free disk space from SQLite database."""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("VACUUM")
        except Exception:
            pass

db = Database()

# ==============================================================================
# 6.5 DISCORD SERVER MEMORY MANAGER
# ==============================================================================
class DiscordMemoryManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_cache: Dict[int, Dict[str, Any]] = {}

    def get_storage_guild_id(self) -> int:
        return STORAGE_GUILD_ID

    def is_storage_configured(self, custom_guild_id: Optional[int] = None) -> bool:
        target_id = custom_guild_id or self.get_storage_guild_id()
        if not target_id:
            return False
        return self.bot.get_guild(target_id) is not None

    async def get_storage_guild(self, custom_guild_id: Optional[int] = None) -> Optional[discord.Guild]:
        target_id = custom_guild_id or self.get_storage_guild_id()
        if not target_id:
            return None
        return self.bot.get_guild(target_id)

    async def get_or_setup_channels(self, guild: discord.Guild, custom_storage_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Retrieves or creates Category and separate channels (messages, media-storage, purged-logs)
        in the designated Discord memory storage server for this guild.
        """
        target_gid = custom_storage_id or self.get_storage_guild_id()
        storage_guild = await self.get_storage_guild(target_gid)
        if not storage_guild:
            raise RuntimeError(
                f"Storage Server (ID: `{target_gid}`) was not found or the bot is not in it!\n"
                f"Please invite the bot to server `{target_gid}` with Administrator or Manage Channels permission."
            )

        # 1. Check in-memory cache
        cached = self._channel_cache.get(guild.id)
        if cached and cached.get("storage_guild_id") == target_gid:
            cat = storage_guild.get_channel(cached.get("category_id"))
            msg_ch = storage_guild.get_channel(cached.get("messages_channel_id"))
            med_ch = storage_guild.get_channel(cached.get("media_channel_id"))
            prg_ch = storage_guild.get_channel(cached.get("purges_channel_id"))
            if cat and msg_ch and med_ch and prg_ch:
                return {
                    "category": cat,
                    "messages_channel": msg_ch,
                    "media_channel": med_ch,
                    "purges_channel": prg_ch,
                    "storage_guild": storage_guild
                }

        # 2. Check Database
        stored = await db.get_storage_channels(guild.id)
        if stored and stored.get("storage_guild_id") == target_gid:
            cat = storage_guild.get_channel(stored["category_id"])
            msg_ch = storage_guild.get_channel(stored["messages_channel_id"])
            med_ch = storage_guild.get_channel(stored["media_channel_id"])
            prg_ch = storage_guild.get_channel(stored["purges_channel_id"])
            if cat and msg_ch and med_ch and prg_ch:
                res = {
                    "category": cat,
                    "messages_channel": msg_ch,
                    "media_channel": med_ch,
                    "purges_channel": prg_ch,
                    "storage_guild": storage_guild
                }
                self._channel_cache[guild.id] = {
                    "storage_guild_id": target_gid,
                    "category_id": cat.id,
                    "messages_channel_id": msg_ch.id,
                    "media_channel_id": med_ch.id,
                    "purges_channel_id": prg_ch.id
                }
                return res

        # 3. Create or find Category in storage guild
        safe_name = "".join(c for c in guild.name if c.isalnum() or c in " -_")[:20].strip() or "Server"
        cat_name = f"📁 {safe_name} ({str(guild.id)[-4:]})"

        category = discord.utils.get(storage_guild.categories, name=cat_name)
        if not category:
            for c in storage_guild.categories:
                if str(guild.id)[-4:] in c.name:
                    category = c
                    break

        if not category:
            category = await storage_guild.create_category(
                name=cat_name,
                reason=f"Discord Memory category for server {guild.name} ({guild.id})"
            )

        # 4. Create or find #messages channel (Keeps msg id with date as data)
        msg_ch = discord.utils.get(category.text_channels, name="messages")
        if not msg_ch:
            msg_ch = await category.create_text_channel(
                name="messages",
                topic=f"guild_id:{guild.id} | Server: {guild.name} | Messages with ID & Date",
                reason=f"Discord Memory messages channel for {guild.name}"
            )

        # 5. Create or find #media-storage channel
        med_ch = discord.utils.get(category.text_channels, name="media-storage")
        if not med_ch:
            med_ch = await category.create_text_channel(
                name="media-storage",
                topic=f"guild_id:{guild.id} | Server: {guild.name} | Preserved Media & Attachments",
                reason=f"Discord Memory media channel for {guild.name}"
            )

        # 6. Create or find #purged-logs channel
        prg_ch = discord.utils.get(category.text_channels, name="purged-logs")
        if not prg_ch:
            prg_ch = await category.create_text_channel(
                name="purged-logs",
                topic=f"guild_id:{guild.id} | Server: {guild.name} | Purged Messages & HTML Transcripts",
                reason=f"Discord Memory purges channel for {guild.name}"
            )

        # Save to DB & memory cache
        await db.set_storage_channels(
            guild_id=guild.id,
            storage_guild_id=storage_guild.id,
            category_id=category.id,
            messages_channel_id=msg_ch.id,
            media_channel_id=med_ch.id,
            purges_channel_id=prg_ch.id
        )
        self._channel_cache[guild.id] = {
            "storage_guild_id": target_gid,
            "category_id": category.id,
            "messages_channel_id": msg_ch.id,
            "media_channel_id": med_ch.id,
            "purges_channel_id": prg_ch.id
        }

        try:
            init_eb = discord.Embed(
                title=f"📁 Memory Initialized • {guild.name}",
                description=(
                    f"External Discord Memory is now active for **{guild.name}** (`{guild.id}`).\n\n"
                    f"• **Messages Channel:** {msg_ch.mention} — Logs message data with ID and Date\n"
                    f"• **Media Storage:** {med_ch.mention} — Hosts preserved media on Discord\n"
                    f"• **Purged Logs:** {prg_ch.mention} — Archives purged chat history & HTML transcripts\n\n"
                    f"🛡️ *Host VPS disk quota is protected from exceeding 1GB.*"
                ),
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            init_eb.set_footer(text=f"Server ID: {guild.id} • Category ID: {category.id}")
            await msg_ch.send(embed=init_eb)
        except Exception:
            pass

        return {
            "category": category,
            "messages_channel": msg_ch,
            "media_channel": med_ch,
            "purges_channel": prg_ch,
            "purged_channel": prg_ch,
            "purged-logs": prg_ch,
            "storage_guild": storage_guild
        }

    async def store_message_data(
        self,
        guild: discord.Guild,
        message_id: int,
        channel: Union[discord.TextChannel, discord.Thread, Any],
        author: Union[discord.Member, discord.User],
        content: str,
        created_at: datetime,
        attachments: Optional[List[Any]] = None,
        embeds: Optional[List[Any]] = None,
        media_files: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Stores message record (Message ID, Date, Author, Channel, Content) in the memory storage server.
        If media files exist, uploads them to #media-storage so Discord permanently hosts them.
        """
        if not self.is_storage_configured():
            return None

        try:
            channels = await self.get_or_setup_channels(guild)
        except Exception as e:
            logger.debug(f"Memory storage not available for guild {guild.id}: {e}")
            return None

        msg_ch: discord.TextChannel = channels["messages_channel"]
        med_ch: discord.TextChannel = channels["media_channel"]

        remote_media_urls = []
        files_to_upload = []

        if media_files:
            for fp in media_files:
                if os.path.exists(fp):
                    try:
                        files_to_upload.append(discord.File(fp, filename=os.path.basename(fp)))
                    except Exception:
                        pass

        if attachments:
            for a in attachments:
                if hasattr(a, "url") and a.url and not any(a.url in u for u in remote_media_urls):
                    remote_media_urls.append(a.url)

        if files_to_upload:
            try:
                ch_name = getattr(channel, "name", str(getattr(channel, "id", "chat")))
                header = (
                    f"📦 **Preserved Media** | Msg ID: `{message_id}` | "
                    f"Date: <t:{int(created_at.timestamp())}:f> | Channel: `#{ch_name}`"
                )
                sent_med = await med_ch.send(content=header, files=files_to_upload)
                for sent_att in sent_med.attachments:
                    remote_media_urls.append(sent_att.url)
            except Exception as e:
                logger.error(f"Failed to upload media to storage server: {e}")
            finally:
                for fp in media_files:
                    try:
                        if os.path.exists(fp):
                            os.remove(fp)
                    except Exception:
                        pass

        try:
            ts = int(created_at.timestamp())
            date_str = created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            ch_id = getattr(channel, "id", 0)
            ch_name = getattr(channel, "name", str(ch_id))
            eb = discord.Embed(
                title=f"📝 Message Record • #{ch_name}",
                description=content[:2000] if content else "*[No text content]*",
                color=COLOR_INFO,
                timestamp=created_at
            )
            eb.add_field(name="Message ID", value=f"`{message_id}`", inline=True)
            eb.add_field(name="Date / Timestamp", value=f"<t:{ts}:F>\n(`{date_str}`)", inline=True)
            eb.add_field(name="Author", value=f"{author.mention} (`{author.id}`)", inline=True)
            eb.add_field(name="Channel", value=f"<#{ch_id}> (`{ch_id}`)", inline=True)
            if remote_media_urls:
                eb.add_field(
                    name="Preserved Media URLs",
                    value="\n".join([f"• [Link]({u})" for u in remote_media_urls[:5]]),
                    inline=False
                )
            eb.set_footer(text=f"Server: {guild.name} ({guild.id}) • Msg ID: {message_id}")
            await msg_ch.send(embed=eb)
        except Exception as e:
            logger.error(f"Failed to post message record to storage: {e}")

        return {
            "remote_media_urls": remote_media_urls
        }

    async def store_purged_data(
        self,
        guild: discord.Guild,
        channel: Union[discord.TextChannel, discord.Thread, Any],
        moderator: Union[discord.Member, discord.User],
        messages: List[discord.Message],
        media_items: Optional[List[dict]] = None,
        transcript_bytes: Optional[bytes] = None,
        transcript_filename: Optional[str] = None
    ) -> Dict[int, List[str]]:
        """
        Stores purged message batches and media into the memory storage server.
        Returns a mapping of {message_id: [remote_attachment_urls]}.
        """
        media_url_map: Dict[int, List[str]] = {}
        if not self.is_storage_configured():
            return media_url_map

        try:
            channels = await self.get_or_setup_channels(guild)
        except Exception as e:
            logger.debug(f"Memory storage not available for purge: {e}")
            return media_url_map

        med_ch: Optional[discord.TextChannel] = channels.get("media_channel")
        prg_ch: Optional[discord.TextChannel] = channels.get("purged-logs") or channels.get("purges_channel")

        if not prg_ch or not med_ch:
            logger.warning(f"Memory storage channels not found for guild {guild.id}")
            return media_url_map

        now_ts = int(time.time())

        # 1. Upload preserved media to #media-storage and capture permanent Discord CDN URLs
        if media_items:
            files_by_msg: Dict[int, List[discord.File]] = {}
            for item in media_items:
                fp = item.get("file_path")
                m_id = item.get("message_id") or 0
                if fp and os.path.exists(fp):
                    if m_id not in files_by_msg:
                        files_by_msg[m_id] = []
                    try:
                        files_by_msg[m_id].append(discord.File(fp, filename=item.get("filename", os.path.basename(fp))))
                    except Exception:
                        pass

            for m_id, file_list in files_by_msg.items():
                try:
                    ch_name = getattr(channel, "name", str(getattr(channel, "id", "chat")))
                    cap = f"📦 **Purged Media** | Msg ID: `{m_id}` | Date: <t:{now_ts}:f> | Channel: `#{ch_name}`"
                    for chunk in [file_list[i:i+10] for i in range(0, len(file_list), 10)]:
                        sent = await med_ch.send(content=cap, files=chunk)
                        if m_id not in media_url_map:
                            media_url_map[m_id] = []
                        for sent_att in sent.attachments:
                            media_url_map[m_id].append(sent_att.url)
                except Exception as e:
                    logger.error(f"Error uploading purged media to storage: {e}")

            for item in media_items:
                fp = item.get("file_path")
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

        # 2. Upload transcript HTML file
        transcript_file = None
        if transcript_bytes:
            fn = transcript_filename or f"transcript-purge-{channel.id}-{now_ts}.html"
            transcript_file = discord.File(io.BytesIO(transcript_bytes), filename=fn)

        # 3. Post purge summary with Message IDs and Dates to #purged-logs
        try:
            ch_name = getattr(channel, "name", str(getattr(channel, "id", "chat")))
            ch_id = getattr(channel, "id", 0)
            eb = discord.Embed(
                title=f"🧹 Purge Archive • #{ch_name}",
                description=(
                    f"**{len(messages)}** message(s) purged by {moderator.mention} (`{moderator.id}`).\n"
                    f"**Channel:** <#{ch_id}> (`{ch_id}`)\n"
                    f"**Archive Date:** <t:{now_ts}:F> (`<t:{now_ts}:R>`)"
                ),
                color=COLOR_WARNING,
                timestamp=datetime.now(timezone.utc)
            )
            eb.set_footer(text=f"Server: {guild.name} ({guild.id}) • Total Messages: {len(messages)}")

            if transcript_file:
                await prg_ch.send(embed=eb, file=transcript_file)
            else:
                await prg_ch.send(embed=eb)

            for i in range(0, min(len(messages), 25), 5):
                batch = messages[i:i+5]
                batch_eb = discord.Embed(
                    title=f"📋 Purged Message Records ({i+1}-{i+len(batch)} of {len(messages)})",
                    color=COLOR_INFO
                )
                for m in batch:
                    m_ts = int(m.created_at.timestamp())
                    preview = (m.content[:200] + "...") if len(m.content) > 200 else (m.content or "*[Attachment/Embed]*")
                    urls = media_url_map.get(m.id, [])
                    url_str = f" • [Media ({len(urls)})]({urls[0]})" if urls else ""
                    batch_eb.add_field(
                        name=f"Msg ID: `{m.id}` | <t:{m_ts}:f>",
                        value=f"• **Author:** `{m.author.display_name}` (`{m.author.id}`)\n• **Content:** {preview}{url_str}",
                        inline=False
                    )
                await prg_ch.send(embed=batch_eb)

        except Exception as e:
            logger.error(f"Error posting purge records to storage server: {e}")

        cleanup_local_cache(max_age_seconds=60)
        return media_url_map

    async def prepare_media_for_restore(self, media_files_str: str) -> List[discord.File]:
        """
        Loads media files for webhook restoration.
        If the file is hosted on Discord CDN (from our storage server),
        it downloads directly into memory (BytesIO) with ZERO local disk write!
        """
        files = []
        if not media_files_str:
            return files

        paths_or_urls = [p.strip() for p in media_files_str.split(",") if p.strip()]
        for item in paths_or_urls:
            if item.startswith("http://") or item.startswith("https://"):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(item, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                clean_url = item.split("?")[0]
                                fname = os.path.basename(clean_url) or "restored_media.png"
                                files.append(discord.File(io.BytesIO(data), filename=fname))
                except Exception as e:
                    logger.debug(f"Failed to fetch remote media for restore: {e}")
            elif os.path.exists(item):
                try:
                    basename = os.path.basename(item)
                    parts = basename.split("_", 2)
                    display_name = parts[2] if len(parts) >= 3 else basename
                    files.append(discord.File(item, filename=display_name))
                except Exception:
                    pass
        return files

# ==============================================================================
# 7. CACHED MESSAGE WRAPPERS
# ==============================================================================
class CachedAvatarWrapper:
    def __init__(self, url: str): self.url = url

class CachedAuthorWrapper:
    def __init__(self, data: dict):
        self.id = data.get("author_id", 0)
        self.display_name = data.get("author_name", "Unknown")
        self.name = data.get("author_username", data.get("author_name", "Unknown"))
        self.bot = data.get("is_bot", False)
        self.mention = f"<@{self.id}>"
        avatar_url = data.get("author_avatar_url") or "https://cdn.discordapp.com/embed/avatars/0.png"
        self.display_avatar = CachedAvatarWrapper(avatar_url)

    def __str__(self): return self.name

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
            try: self.embeds.append(discord.Embed.from_dict(e))
            except Exception: pass
        self.webhook_id = data.get("webhook_id")

# ==============================================================================
# 8. COG: VOICE MANAGEMENT (MOVEER)
# ==============================================================================
class Moveer(commands.Cog, name="Voice Management (Moveer)"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild and not self.bot.is_feature_enabled(ctx.guild.id, "moveer"):
            await ctx.send(embed=error_embed("Moveer Disabled", "The Voice Moveer feature is currently **disabled** on this server.\nUse `/toggle feature: moveer` or `/toggle_moveer` to enable it."), ephemeral=True)
            return False
        return True

    def _resolve_voice_channel(self, guild: discord.Guild, token: str) -> Optional[discord.VoiceChannel]:
        if not token or not guild: return None
        token = token.strip()
        m = re.match(r"^<#(\d+)>$", token)
        if m:
            ch = guild.get_channel(int(m.group(1)))
            if isinstance(ch, discord.VoiceChannel): return ch
        if token.isdigit():
            ch = guild.get_channel(int(token))
            if isinstance(ch, discord.VoiceChannel): return ch
        clean = token.lower()
        for vc in guild.voice_channels:
            if vc.name.lower() == clean:
                return vc
        return None

    def _resolve_member(self, guild: discord.Guild, token: str) -> Optional[discord.Member]:
        if not token or not guild: return None
        token = token.strip()
        m = re.match(r"^<@!?(\d+)>$", token)
        if m: return guild.get_member(int(m.group(1)))
        if token.isdigit():
            mem = guild.get_member(int(token))
            if mem: return mem
        clean = token.lstrip("@").lower()
        for mem in guild.members:
            if mem.name.lower() == clean or mem.display_name.lower() == clean or str(mem).lower() == clean:
                return mem
        return None

    @commands.hybrid_command(name="move", description="Move a user to a specific voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(user="The member to move", channel="Target voice channel")
    async def move(self, ctx: commands.Context, user: discord.Member, channel: discord.VoiceChannel):
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("Move Failed", f"{user.mention} is not in any voice channel!"), ephemeral=True)
        from_channel = user.voice.channel
        try:
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, user.id, "move", ctx.author, channel.id, via=f"/{ctx.command.name}")
            await user.move_to(channel)
            await ctx.send(embed=voice_embed("User Moved", f"Successfully moved {user.mention} from **{from_channel.name}** to **{channel.name}**."))
        except Exception as e:
            await ctx.send(embed=error_embed("Move Error", str(e)), ephemeral=True)

    @commands.hybrid_command(name="cmove", description="Move specified user(s) into a target voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(
        to_channel="Destination voice channel",
        users="Member(s) to move (mention, username, or ID separated by spaces)"
    )
    async def cmove(self, ctx: commands.Context, to_channel: Optional[discord.VoiceChannel] = None, *, users: str = ""):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        target_channel = to_channel
        user_tokens = []

        if ctx.interaction:
            target_channel = to_channel
            try:
                user_tokens = shlex.split(users) if users else []
            except Exception:
                user_tokens = users.split() if users else []
        else:
            content_args = ctx.message.content.split()[1:] if ctx.message else []
            if not content_args and not target_channel:
                return await ctx.send(embed=info_embed(
                    "🔊 cmove Usage",
                    "**Move specific user(s) to a voice channel:**\n"
                    "`&cmove <to_channel> @user1 @user2...`\n"
                    "• *Mentions, User IDs, or Usernames are supported!*\n"
                    "• *Destination channel can be mentioned (<#channel>) or named!*"
                ), ephemeral=True)

            try:
                tokens = shlex.split(ctx.message.content)[1:]
            except Exception:
                tokens = content_args

            if not target_channel:
                for i, tok in enumerate(tokens):
                    vc = self._resolve_voice_channel(ctx.guild, tok)
                    if vc:
                        target_channel = vc
                        tokens.pop(i)
                        break

            user_tokens = tokens

        if not target_channel:
            return await ctx.send(embed=error_embed(
                "Missing Channel",
                "Please specify a valid destination voice channel.\n"
                "Example: `&cmove #Gaming @User1 @User2`"
            ), ephemeral=True)

        if not user_tokens:
            return await ctx.send(embed=error_embed(
                "Missing Users",
                "Please specify at least one user to move.\n"
                "Example: `&cmove #Gaming @User1 @User2`"
            ), ephemeral=True)

        moved = []
        already_in = []
        not_in_voice = []
        failed = []

        for tok in user_tokens:
            mem = self._resolve_member(ctx.guild, tok)
            if not mem:
                failed.append(f"`{tok}` (User not found)")
                continue

            if not mem.voice or not mem.voice.channel:
                not_in_voice.append(mem)
                continue

            if mem.voice.channel.id == target_channel.id:
                already_in.append(mem)
                continue

            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, mem.id, "move", ctx.author, target_channel.id, via=f"/{ctx.command.name}")
                await mem.move_to(target_channel)
                moved.append(mem)
            except Exception as e:
                failed.append(f"{mem.mention} ({e})")

        embed = discord.Embed(
            title=f"🔊 Voice Move: {target_channel.name}",
            color=COLOR_SUCCESS if moved else (COLOR_WARNING if not_in_voice or failed else COLOR_INFO),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
        embed.description = f"**Destination Channel:** {target_channel.mention} (`{target_channel.name}`)"

        if moved:
            embed.add_field(name=f"🟢 Moved ({len(moved)})", value=" ".join(m.mention for m in moved), inline=False)
        if already_in:
            embed.add_field(name=f"ℹ️ Already in Channel ({len(already_in)})", value=" ".join(m.mention for m in already_in), inline=False)
        if not_in_voice:
            embed.add_field(name=f"⚠️ Not in Voice ({len(not_in_voice)})", value=" ".join(m.mention for m in not_in_voice), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(f"• {f}" for f in failed), inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="fmove", description="Move all users from one voice channel to another")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(from_channel="Channel to move users from", to_channel="Channel to move users to")
    async def fmove(self, ctx: commands.Context, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if from_channel.id == to_channel.id:
            return await ctx.send(embed=error_embed("Invalid Channels", "Source and destination voice channels cannot be the same."), ephemeral=True)

        members = from_channel.members
        if not members:
            return await ctx.send(embed=error_embed("No Members", f"There is nobody in {from_channel.mention}."), ephemeral=True)

        moved = []
        failed = []

        for m in members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, to_channel.id, via=f"/{ctx.command.name}")
                await m.move_to(to_channel)
                moved.append(m)
            except Exception as e:
                failed.append(f"{m.mention} ({e})")

        embed = discord.Embed(
            title="🔄 Voice Channel Migration (fmove)",
            color=COLOR_SUCCESS if moved else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
        embed.description = f"**From:** {from_channel.mention} ➡️ **To:** {to_channel.mention}"

        if moved:
            embed.add_field(name=f"🟢 Moved ({len(moved)})", value=" ".join(m.mention for m in moved), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(f"• {f}" for f in failed), inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="tmove", description="Move all members with a specific role into a voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    @app_commands.describe(role="The role to target", to_channel="Destination voice channel")
    async def tmove(self, ctx: commands.Context, role: discord.Role, to_channel: discord.VoiceChannel):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        members_to_move = [m for m in role.members if m.voice and m.voice.channel and m.voice.channel.id != to_channel.id]
        if not members_to_move:
            return await ctx.send(embed=error_embed("No Members Found", f"No members with role {role.mention} are currently connected to other voice channels."), ephemeral=True)

        moved = []
        failed = []

        for m in members_to_move:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, to_channel.id, via=f"/{ctx.command.name}")
                await m.move_to(to_channel)
                moved.append(m)
            except Exception as e:
                failed.append(f"{m.mention} ({e})")

        embed = discord.Embed(
            title=f"🎭 Role Voice Move: {role.name} (tmove)",
            color=COLOR_SUCCESS if moved else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
        embed.description = f"**Role:** {role.mention} ➡️ **Destination:** {to_channel.mention}"

        if moved:
            embed.add_field(name=f"🟢 Moved ({len(moved)})", value=" ".join(m.mention for m in moved), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(f"• {f}" for f in failed), inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="rmove", description="Move all members from multiple voice channels into your current channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def rmove(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Not in Voice", "You must be in a voice channel to use `rmove`."), ephemeral=True)
        dest = ctx.author.voice.channel
        moved = 0
        for vc in ctx.guild.voice_channels:
            if vc != dest:
                for m in vc.members:
                    try:
                        if hasattr(self.bot, "track_voice_action"):
                            self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, dest.id, via=f"/{ctx.command.name}")
                        await m.move_to(dest)
                        moved += 1
                    except Exception: pass
        await ctx.send(embed=voice_embed("All Voice Gathered", f"Successfully pulled **{moved}** member(s) into {dest.mention}."))

    @commands.hybrid_command(name="drag", description="Drag a member into your current voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def drag(self, ctx: commands.Context, user: discord.Member):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Not in Voice", "You must be in a voice channel to drag someone to you."), ephemeral=True)
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("User Not Connected", f"{user.mention} is not in any voice channel!"), ephemeral=True)
        dest = ctx.author.voice.channel
        try:
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, user.id, "move", ctx.author, dest.id, via=f"/{ctx.command.name}")
            await user.move_to(dest)
            await ctx.send(embed=voice_embed("User Dragged", f"Dragged {user.mention} into {dest.mention}."))
        except Exception as e:
            await ctx.send(embed=error_embed("Drag Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="massdrag", description="Drag all members from a specific channel into your current channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def massdrag(self, ctx: commands.Context, from_channel: discord.VoiceChannel):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(embed=error_embed("Not in Voice", "You must be in a voice channel to mass drag members."), ephemeral=True)
        dest = ctx.author.voice.channel
        moved = 0
        for m in from_channel.members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, dest.id, via=f"/{ctx.command.name}")
                await m.move_to(dest)
                moved += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Mass Drag Completed", f"Dragged **{moved}** member(s) from **{from_channel.name}** into {dest.mention}."))

    @commands.hybrid_command(name="spread", description="Evenly distribute members from one channel across other voice channels")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def spread(self, ctx: commands.Context, source_channel: discord.VoiceChannel):
        targets = [vc for vc in ctx.guild.voice_channels if vc != source_channel]
        members = source_channel.members
        if not members or not targets:
            return await ctx.send(embed=error_embed("Spread Error", "Need members in source channel and at least one other voice channel."), ephemeral=True)
        distributed = 0
        for i, m in enumerate(members):
            dest = targets[i % len(targets)]
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, dest.id, via=f"/{ctx.command.name}")
                await m.move_to(dest)
                distributed += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Members Spread", f"Distributed **{distributed}** member(s) across **{len(targets)}** channels."))

    @commands.hybrid_command(name="gmove", description="Move all members with a game/activity to a channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def gmove(self, ctx: commands.Context, game_name: str, to_channel: discord.VoiceChannel):
        moved = 0
        for m in ctx.guild.members:
            if m.voice and m.voice.channel and m.voice.channel != to_channel:
                for act in m.activities:
                    if act.name and game_name.lower() in act.name.lower():
                        try:
                            if hasattr(self.bot, "track_voice_action"):
                                self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, to_channel.id, via=f"/{ctx.command.name}")
                            await m.move_to(to_channel)
                            moved += 1
                            break
                        except Exception: pass
        await ctx.send(embed=voice_embed("Gaming Move", f"Moved **{moved}** player(s) playing `{game_name}` to **{to_channel.name}**."))

    @commands.hybrid_command(name="randommove", description="Randomly move a specified number of users from a channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def randommove(self, ctx: commands.Context, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel, count: int = 1):
        members = from_channel.members
        if not members:
            return await ctx.send(embed=error_embed("Channel Empty", f"No members in {from_channel.mention}."), ephemeral=True)
        picked = random.sample(members, min(count, len(members)))
        moved = 0
        for m in picked:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "move", ctx.author, to_channel.id, via=f"/{ctx.command.name}")
                await m.move_to(to_channel)
                moved += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Random Move", f"Randomly moved **{moved}** member(s) to **{to_channel.name}**."))

    @commands.hybrid_command(name="vkick", description="Disconnect/kick a member from voice")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def vkick(self, ctx: commands.Context, user: discord.Member):
        if not user.voice or not user.voice.channel:
            return await ctx.send(embed=error_embed("Not in Voice", f"{user.mention} is not connected to voice."), ephemeral=True)
        try:
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, user.id, "disconnect", ctx.author, None, via=f"/{ctx.command.name}")
            await user.move_to(None)
            await ctx.send(embed=voice_embed("Voice Kick", f"Disconnected {user.mention} from voice."))
        except Exception as e:
            await ctx.send(embed=error_embed("Voice Kick Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="massvkick", description="Disconnect all users from a voice channel")
    @app_commands.default_permissions(move_members=True)
    @commands.has_permissions(move_members=True)
    async def massvkick(self, ctx: commands.Context, channel: discord.VoiceChannel):
        members = channel.members
        if not members:
            return await ctx.send(embed=error_embed("Empty Channel", f"No users in {channel.mention}."), ephemeral=True)
        kicked = 0
        for m in members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "disconnect", ctx.author, None, via=f"/{ctx.command.name}")
                await m.move_to(None)
                kicked += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Mass Voice Kick", f"Disconnected **{kicked}** user(s) from **{channel.name}**."))

    @commands.hybrid_command(name="vmute", description="Server mute a member or entire channel")
    @app_commands.default_permissions(mute_members=True)
    @commands.has_permissions(mute_members=True)
    async def vmute(self, ctx: commands.Context, target: Optional[discord.Member] = None, channel: Optional[discord.VoiceChannel] = None):
        if target:
            if not target.voice: return await ctx.send(embed=error_embed("Not in Voice", f"{target.mention} is not in voice."), ephemeral=True)
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, target.id, "mute", ctx.author, None, via=f"/{ctx.command.name}")
            await target.edit(mute=True)
            return await ctx.send(embed=voice_embed("Voice Muted", f"Muted {target.mention}."))
        ch = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not ch: return await ctx.send(embed=error_embed("No Channel", "Specify a member, channel, or be in a voice channel."), ephemeral=True)
        count = 0
        for m in ch.members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "mute", ctx.author, None, via=f"/{ctx.command.name}")
                await m.edit(mute=True)
                count += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Channel Muted", f"Server muted **{count}** member(s) in **{ch.name}**."))

    @commands.hybrid_command(name="vunmute", description="Unmute a member or entire channel")
    @app_commands.default_permissions(mute_members=True)
    @commands.has_permissions(mute_members=True)
    async def vunmute(self, ctx: commands.Context, target: Optional[discord.Member] = None, channel: Optional[discord.VoiceChannel] = None):
        if target:
            if not target.voice: return await ctx.send(embed=error_embed("Not in Voice", f"{target.mention} is not in voice."), ephemeral=True)
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, target.id, "mute", ctx.author, None, via=f"/{ctx.command.name}")
            await target.edit(mute=False)
            return await ctx.send(embed=voice_embed("Voice Unmuted", f"Unmuted {target.mention}."))
        ch = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not ch: return await ctx.send(embed=error_embed("No Channel", "Specify a member, channel, or be in a voice channel."), ephemeral=True)
        count = 0
        for m in ch.members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "mute", ctx.author, None, via=f"/{ctx.command.name}")
                await m.edit(mute=False)
                count += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Channel Unmuted", f"Server unmuted **{count}** member(s) in **{ch.name}**."))

    @commands.hybrid_command(name="vdeaf", description="Server deafen a member or entire channel")
    @app_commands.default_permissions(deafen_members=True)
    @commands.has_permissions(deafen_members=True)
    async def vdeaf(self, ctx: commands.Context, target: Optional[discord.Member] = None, channel: Optional[discord.VoiceChannel] = None):
        if target:
            if not target.voice: return await ctx.send(embed=error_embed("Not in Voice", f"{target.mention} is not in voice."), ephemeral=True)
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, target.id, "deafen", ctx.author, None, via=f"/{ctx.command.name}")
            await target.edit(deafen=True)
            return await ctx.send(embed=voice_embed("Voice Deafened", f"Deafened {target.mention}."))
        ch = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not ch: return await ctx.send(embed=error_embed("No Channel", "Specify a member, channel, or be in a voice channel."), ephemeral=True)
        count = 0
        for m in ch.members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "deafen", ctx.author, None, via=f"/{ctx.command.name}")
                await m.edit(deafen=True)
                count += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Channel Deafened", f"Server deafened **{count}** member(s) in **{ch.name}**."))

    @commands.hybrid_command(name="vundeaf", description="Undeafen a member or entire channel")
    @app_commands.default_permissions(deafen_members=True)
    @commands.has_permissions(deafen_members=True)
    async def vundeaf(self, ctx: commands.Context, target: Optional[discord.Member] = None, channel: Optional[discord.VoiceChannel] = None):
        if target:
            if not target.voice: return await ctx.send(embed=error_embed("Not in Voice", f"{target.mention} is not in voice."), ephemeral=True)
            if hasattr(self.bot, "track_voice_action"):
                self.bot.track_voice_action(ctx.guild.id, target.id, "deafen", ctx.author, None, via=f"/{ctx.command.name}")
            await target.edit(deafen=False)
            return await ctx.send(embed=voice_embed("Voice Undeafened", f"Undeafened {target.mention}."))
        ch = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not ch: return await ctx.send(embed=error_embed("No Channel", "Specify a member, channel, or be in a voice channel."), ephemeral=True)
        count = 0
        for m in ch.members:
            try:
                if hasattr(self.bot, "track_voice_action"):
                    self.bot.track_voice_action(ctx.guild.id, m.id, "deafen", ctx.author, None, via=f"/{ctx.command.name}")
                await m.edit(deafen=False)
                count += 1
            except Exception: pass
        await ctx.send(embed=voice_embed("Channel Undeafened", f"Server undeafened **{count}** member(s) in **{ch.name}**."))

# ==============================================================================
# 9. COG: DYNAMIC VOICE (TEMP VOICE)
# ==============================================================================
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
        await self.channel.edit(user_limit=int(val))
        await interaction.response.send_message(f"User limit updated to **{val}**!", ephemeral=True)

class TempVoiceControlView(ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        owner_id = await db.get_temp_channel_owner(self.channel_id)
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != owner_id and not interaction.user.guild_permissions.administrator and not is_server_owner:
            await interaction.response.send_message("Only the channel creator can customize this channel.", ephemeral=True)
            return False
        return True

    @ui.button(label="Rename", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="tempvoice_rename")
    async def rename_btn(self, interaction: discord.Interaction, button: ui.Button):
        ch = interaction.guild.get_channel(self.channel_id)
        if ch: await interaction.response.send_modal(RenameModal(ch))

    @ui.button(label="Limit", style=discord.ButtonStyle.secondary, emoji="👥", custom_id="tempvoice_limit")
    async def limit_btn(self, interaction: discord.Interaction, button: ui.Button):
        ch = interaction.guild.get_channel(self.channel_id)
        if ch: await interaction.response.send_modal(LimitModal(ch))

    @ui.button(label="Lock/Unlock", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="tempvoice_lock")
    async def lock_btn(self, interaction: discord.Interaction, button: ui.Button):
        ch = interaction.guild.get_channel(self.channel_id)
        if not ch: return
        overwrites = ch.overwrites_for(interaction.guild.default_role)
        is_locked = overwrites.connect is False
        overwrites.connect = None if is_locked else False
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrites)
        msg = "Channel unlocked!" if is_locked else "Channel locked!"
        await interaction.response.send_message(msg, ephemeral=True)

class TempVoice(commands.Cog, name="Dynamic Voice (Temp Voice)"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="tempvoice", description="Configure Join to Create dynamic voice channels", fallback="panel")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def tempvoice_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            settings = await db.get_guild_settings(ctx.guild.id)
            c_id = settings.get("tempvoice_creator_id")
            cat_id = settings.get("tempvoice_category_id")
            c_ch = ctx.guild.get_channel(c_id) if c_id else None
            cat_ch = ctx.guild.get_channel(cat_id) if cat_id else None
            desc = (
                f"• **Join to Create Channel:** {c_ch.mention if c_ch else '*Not Set*'}\n"
                f"• **Category:** {cat_ch.name if cat_ch else '*Not Set*'}\n\n"
                f"Run `/tempvoice setup` to automatically generate the voice hub."
            )
            await ctx.send(embed=info_embed("Temp Voice Hub", desc))

    @tempvoice_group.command(name="setup", description="Automatically create a Join to Create channel and category")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def setup_tempvoice(self, ctx: commands.Context):
        guild = ctx.guild
        cat = await guild.create_category(name="🔊 DYNAMIC VOICE")
        creator_ch = await guild.create_voice_channel(name="➕ Join to Create", category=cat)
        await db.update_guild_setting(guild.id, "tempvoice_creator_id", creator_ch.id)
        await db.update_guild_setting(guild.id, "tempvoice_category_id", cat.id)
        await ctx.send(embed=success_embed("Temp Voice Configured", f"Created {creator_ch.mention}! When members join, their own private channel is instantly generated."))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if not self.bot.is_feature_enabled(guild.id, "tempvoice"):
            return
        settings = await db.get_guild_settings(guild.id)
        creator_id = settings.get("tempvoice_creator_id")
        category_id = settings.get("tempvoice_category_id")

        if after.channel and creator_id and after.channel.id == creator_id:
            cat = guild.get_channel(category_id)
            name = f"🔊 {member.display_name}'s Channel"
            new_channel = await guild.create_voice_channel(name=name, category=cat, reason="Temp voice channel created")
            await db.add_temp_channel(new_channel.id, guild.id, member.id)
            try:
                await member.move_to(new_channel)
            except Exception: pass

            control_embed = voice_embed(
                "Voice Controls",
                f"Welcome {member.mention}! You own this private channel.\nUse the buttons below to customize it:"
            )
            try:
                await new_channel.send(embed=control_embed, view=TempVoiceControlView(new_channel.id))
            except Exception: pass

        if before.channel:
            owner_id = await db.get_temp_channel_owner(before.channel.id)
            if owner_id is not None and len(before.channel.members) == 0:
                await db.remove_temp_channel(before.channel.id)
                try:
                    await before.channel.delete(reason="Temp voice empty")
                except Exception: pass

# ==============================================================================
# 10. COG: UTILITY & STATS
# ==============================================================================
class Utility(commands.Cog, name="Utility & Info (Sapphire)"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    @tasks.loop(seconds=10)
    async def check_reminders(self):
        now = time.time()
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM reminders WHERE remind_at <= ?", (now,))
            expired = await cursor.fetchall()
            for r in expired:
                user = self.bot.get_user(r["user_id"])
                channel = self.bot.get_channel(r["channel_id"])
                msg = f"⏰ **Reminder:** {r['reminder_text']}"
                if channel:
                    try: await channel.send(f"<@{r['user_id']}> {msg}")
                    except Exception: pass
                elif user:
                    try: await user.send(msg)
                    except Exception: pass
                await conn.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
            if expired: await conn.commit()

    @check_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="ping", description="Check bot latency and API response time")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! Latency: `{latency_ms}ms`")

    @commands.hybrid_command(name="botinfo", description="Display bot system information")
    async def botinfo(self, ctx: commands.Context):
        guild_count = len(self.bot.guilds)
        user_count = sum(g.member_count or 0 for g in self.bot.guilds)
        embed = discord.Embed(
            title="🤖 Moveer & Sapphire All-in-One Bot",
            description="High-performance voice management, audit logs, and instant chat restoration.",
            color=0x5865F2
        )
        embed.add_field(name="Servers", value=f"{guild_count:,}", inline=True)
        embed.add_field(name="Members", value=f"{user_count:,}", inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Prefix", value="`/` or custom prefix", inline=True)
        embed.set_footer(text="Running completely self-contained with aiosqlite")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="serverinfo", description="Get comprehensive server statistics")
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        owner = g.owner or await self.bot.fetch_user(g.owner_id)
        embed = discord.Embed(title=f"🏰 Server Info — {g.name}", color=0x3498DB)
        if g.icon: embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=f"{owner.mention} (`{owner.id}`)", inline=True)
        embed.add_field(name="Server ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="Members", value=f"{g.member_count:,}", inline=True)
        embed.add_field(name="Channels", value=f"💬 {len(g.text_channels)} | 🔊 {len(g.voice_channels)}", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="userinfo", description="Get information about a member")
    @app_commands.describe(user="The member to view (defaults to you)")
    async def userinfo(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        roles = [r.mention for r in reversed(target.roles) if r.name != "@everyone"]
        embed = discord.Embed(title=f"👤 User Info — {target.display_name}", color=target.color or 0x3498DB)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Username", value=f"{target} (`{target.id}`)", inline=False)
        embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Top Role", value=target.top_role.mention, inline=True)
        embed.add_field(name="Roles", value=" ".join(roles[:15]) if roles else "None", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="av", aliases=["avatar", "pfp"], description="Display a user's avatar in full resolution")
    @app_commands.describe(user="The member whose avatar you want to view")
    async def av(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        embed = discord.Embed(title=f"🖼️ Avatar of {target.display_name}", color=0x3498DB)
        embed.set_image(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="poll", description="Create an interactive poll")
    @app_commands.describe(question="Poll question", options="Comma-separated options (up to 10)")
    async def poll(self, ctx: commands.Context, question: str, options: str):
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2 or len(opts) > 10:
            return await ctx.send(embed=error_embed("Invalid Options", "Provide between 2 and 10 comma-separated options."), ephemeral=True)
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        desc = [f"{emojis[i]} **{opt}**" for i, opt in enumerate(opts)]
        embed = discord.Embed(title=f"📊 {question}", description="\n\n".join(desc), color=0x3498DB)
        msg = await ctx.send(embed=embed)
        for i in range(len(opts)):
            await msg.add_reaction(emojis[i])

    @commands.hybrid_command(name="remind", description="Set a timer reminder")
    @app_commands.describe(duration="Time delay (e.g. 10m, 1h, 2d)", text="What to remind you about")
    async def remind(self, ctx: commands.Context, duration: str, *, text: str):
        seconds = parse_duration(duration)
        if not seconds or seconds < 10:
            return await ctx.send(embed=error_embed("Invalid Duration", "Provide a time format like `10m`, `2h`, `1d` (min 10s)."), ephemeral=True)
        remind_at = time.time() + seconds
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("INSERT INTO reminders (user_id, channel_id, reminder_text, remind_at) VALUES (?, ?, ?, ?)",
                               (ctx.author.id, ctx.channel.id, text, remind_at))
            await conn.commit()
        await ctx.send(embed=success_embed("Reminder Set", f"I will remind you about: **{text}** in **{format_duration(seconds)}**."))

# ==============================================================================
# 10.1 COG: ROLE MANAGEMENT SUITE
# ==============================================================================
class RoleManagement(commands.Cog, name="Role Management"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _resolve_member(self, guild: discord.Guild, token: str) -> Optional[discord.Member]:
        if not token or not guild: return None
        token = token.strip()
        m = re.match(r"^<@!?(\d+)>$", token)
        if m: return guild.get_member(int(m.group(1)))
        if token.isdigit():
            mem = guild.get_member(int(token))
            if mem: return mem
        clean = token.lstrip("@").lower()
        for mem in guild.members:
            if mem.name.lower() == clean or mem.display_name.lower() == clean or str(mem).lower() == clean:
                return mem
        return None

    def _resolve_role(self, guild: discord.Guild, token: str) -> Optional[discord.Role]:
        if not token or not guild: return None
        token = token.strip()
        m = re.match(r"^<@&(\d+)>$", token)
        if m: return guild.get_role(int(m.group(1)))
        if token.isdigit():
            r = guild.get_role(int(token))
            if r: return r
        clean = token.lstrip("@").lower()
        for r in guild.roles:
            if r.name.lower() == clean:
                return r
        return None

    def _parse_remaining_roles(self, guild: discord.Guild, tokens: list[str]) -> list[Any]:
        results = []
        i = 0
        while i < len(tokens):
            matched = False
            single = self._resolve_role(guild, tokens[i])
            if single:
                results.append(single)
                i += 1
                continue
            for j in range(len(tokens), i, -1):
                candidate = " ".join(tokens[i:j]).lstrip("@").lower()
                for r in guild.roles:
                    if r.name.lower() == candidate:
                        results.append(r)
                        i = j
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                results.append(tokens[i])
                i += 1
        return results

    def _has_mod_perms(self, role: discord.Role) -> bool:
        p = role.permissions
        return bool(p.administrator or p.kick_members or p.ban_members or p.moderate_members)

    def _is_management_role(self, role: discord.Role) -> bool:
        p = role.permissions
        return bool(
            p.administrator or p.manage_guild or p.manage_roles or
            p.manage_channels or p.manage_messages or p.manage_webhooks or
            p.manage_nicknames or p.manage_threads or p.manage_events or
            p.kick_members or p.ban_members or p.moderate_members
        )

    @commands.hybrid_command(
        name="role",
        description="Toggle multiple roles on a user, or add multiple users to a single role"
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(args="Format 1: @user @role1 @role2... | Format 2: @role @user1 @user2...")
    async def role(self, ctx: commands.Context, *, args: str = ""):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not args.strip():
            embed = info_embed(
                "🎭 Role Command Usage",
                "**1. Toggle multiple roles for a single user:**\n"
                "`&role @user @role1 @role2 @role3...`\n"
                "• *Adds the role if the user does NOT have it.*\n"
                "• *Removes the role if the user ALREADY has it.*\n\n"
                "**2. Add multiple users to a single role:**\n"
                "`&role @role @user1 @user2 @user3...`\n"
                "• *Adds the specified role to all listed users.*\n\n"
                "*(You can mention `@users` and `@roles`, or use IDs/names!)*"
            )
            return await ctx.send(embed=embed)

        if not ctx.guild.me.guild_permissions.manage_roles:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Roles** permission."), ephemeral=True)

        is_exempt = ctx.author.id in (ctx.guild.owner_id, RESTORE_MASTER_USER_ID)
        if not ctx.author.guild_permissions.manage_roles and not is_exempt:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need the **Manage Roles** permission to use this command."), ephemeral=True)

        try:
            tokens = shlex.split(args)
        except Exception:
            tokens = args.split()

        if len(tokens) < 2:
            return await ctx.send(embed=error_embed(
                "Incomplete Command",
                "Please specify at least one user and one role.\n"
                "• Toggle roles on user: `&role @user @role1 @role2...`\n"
                "• Add multiple users to role: `&role @role @user1 @user2...`"
            ), ephemeral=True)

        token0 = tokens[0]
        rem_tokens = tokens[1:]

        m0 = self._resolve_member(ctx.guild, token0)
        r0 = self._resolve_role(ctx.guild, token0)

        is_user_first = False
        is_role_first = False

        if m0 and not r0:
            is_user_first = True
        elif r0 and not m0:
            is_role_first = True
        elif m0 and r0:
            t1_r = self._resolve_role(ctx.guild, rem_tokens[0])
            t1_m = self._resolve_member(ctx.guild, rem_tokens[0])
            if t1_r and not t1_m:
                is_user_first = True
            elif t1_m and not t1_r:
                is_role_first = True
            else:
                is_user_first = True
        else:
            return await ctx.send(embed=error_embed(
                "Not Found",
                f"Could not find any user or role matching `{token0}`.\n"
                "Check the spelling or mention them directly."
            ), ephemeral=True)

        # -------------------------------------------------------------
        # FORMAT 1: &role @user @role1 @role2 @role3... (Toggle roles)
        # -------------------------------------------------------------
        if is_user_first:
            target_member = m0
            parsed_roles = self._parse_remaining_roles(ctx.guild, rem_tokens)
            added = []
            removed = []
            skipped = []

            for item in parsed_roles:
                if not isinstance(item, discord.Role):
                    skipped.append(f"`{item}` (Role not found)")
                    continue
                role_obj = item

                if role_obj.is_default():
                    skipped.append(f"{role_obj.name} (Cannot assign @everyone)")
                    continue
                if role_obj.managed:
                    skipped.append(f"{role_obj.name} (Managed by integration/bot)")
                    continue
                if role_obj >= ctx.guild.me.top_role:
                    skipped.append(f"{role_obj.name} (Equal to or higher than bot's role)")
                    continue
                if not is_exempt and role_obj >= ctx.author.top_role:
                    skipped.append(f"{role_obj.name} (Equal to or higher than your role)")
                    continue

                is_target_above = (not is_exempt and target_member.id != ctx.author.id and target_member.top_role >= ctx.author.top_role)

                try:
                    if role_obj in target_member.roles:
                        if is_target_above and self._is_management_role(role_obj):
                            skipped.append(f"{role_obj.name} (Cannot remove management role from member with equal/higher role)")
                            continue
                        await target_member.remove_roles(role_obj, reason=f"Role toggle by {ctx.author} ({ctx.author.id})")
                        removed.append(role_obj)
                    else:
                        if is_target_above and self._has_mod_perms(role_obj):
                            skipped.append(f"{role_obj.name} (Cannot grant Kick/Ban/Timeout role to member with equal/higher role)")
                            continue
                        await target_member.add_roles(role_obj, reason=f"Role toggle by {ctx.author} ({ctx.author.id})")
                        added.append(role_obj)
                except Exception as e:
                    skipped.append(f"{role_obj.name} ({e})")

            embed = discord.Embed(
                title=f"🎭 Roles Updated for {target_member.display_name}",
                color=COLOR_SUCCESS if (added or removed) else (COLOR_WARNING if skipped else COLOR_INFO),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=target_member.display_avatar.url)
            embed.description = f"**Member:** {target_member.mention} (`{target_member.name}` - `{target_member.id}`)"

            if added:
                embed.add_field(name=f"🟢 Added ({len(added)})", value=" ".join(r.mention for r in added), inline=False)
            if removed:
                embed.add_field(name=f"🔴 Removed ({len(removed)})", value=" ".join(r.mention for r in removed), inline=False)
            if skipped:
                embed.add_field(name=f"⚠️ Skipped / Failed ({len(skipped)})", value="\n".join(f"• {s}" for s in skipped), inline=False)
            if not added and not removed and not skipped:
                embed.add_field(name="ℹ️ Result", value="No role changes were performed.", inline=False)

            embed.set_footer(text=f"Member ID: {target_member.id} • Mod ID: {ctx.author.id}")
            return await ctx.send(embed=embed)

        # -------------------------------------------------------------
        # FORMAT 2: &role @role @user1 @user2 @user3... (Add multiple users to role)
        # -------------------------------------------------------------
        else:
            target_role = r0
            if target_role.is_default():
                return await ctx.send(embed=error_embed("Invalid Role", "Cannot assign the `@everyone` role."), ephemeral=True)
            if target_role.managed:
                return await ctx.send(embed=error_embed("Invalid Role", f"{target_role.mention} is managed by an integration/bot and cannot be assigned manually."), ephemeral=True)
            if target_role >= ctx.guild.me.top_role:
                return await ctx.send(embed=error_embed("Hierarchy Error", f"{target_role.mention} is equal to or higher than my highest role ({ctx.guild.me.top_role.mention})."), ephemeral=True)
            if not is_exempt and target_role >= ctx.author.top_role:
                return await ctx.send(embed=error_embed("Hierarchy Error", f"{target_role.mention} is equal to or higher than your highest role ({ctx.author.top_role.mention})."), ephemeral=True)

            added_users = []
            already_have = []
            skipped_users = []

            for token in rem_tokens:
                mem = self._resolve_member(ctx.guild, token)
                if not mem:
                    skipped_users.append(f"`{token}` (Member not found)")
                    continue

                if not is_exempt and mem.id != ctx.author.id and mem.top_role >= ctx.author.top_role:
                    if self._has_mod_perms(target_role):
                        skipped_users.append(f"{mem.mention} (Cannot grant Kick/Ban/Timeout role to equal/higher rank member)")
                        continue

                if target_role in mem.roles:
                    already_have.append(mem)
                    continue

                try:
                    await mem.add_roles(target_role, reason=f"Mass role add by {ctx.author} ({ctx.author.id})")
                    added_users.append(mem)
                except Exception as e:
                    skipped_users.append(f"{mem.mention} ({e})")

            embed = discord.Embed(
                title=f"🎭 Mass Role Assigned: {target_role.name}",
                color=COLOR_SUCCESS if added_users else (COLOR_WARNING if skipped_users else COLOR_INFO),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.description = f"**Role:** {target_role.mention} (`{target_role.id}`)"

            if added_users:
                embed.add_field(name=f"🟢 Added To ({len(added_users)})", value=" ".join(m.mention for m in added_users), inline=False)
            if already_have:
                embed.add_field(name=f"ℹ️ Already Had Role ({len(already_have)})", value=" ".join(m.mention for m in already_have), inline=False)
            if skipped_users:
                embed.add_field(name=f"⚠️ Skipped / Failed ({len(skipped_users)})", value="\n".join(f"• {s}" for s in skipped_users), inline=False)

            embed.set_footer(text=f"Role ID: {target_role.id} • Mod ID: {ctx.author.id}")
            return await ctx.send(embed=embed)

# ==============================================================================
# 10.2 COG: MODERATION SUITE (BAN, UNBAN, MUTE/TIMEOUT)
# ==============================================================================
class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_hierarchy(self, moderator: discord.Member, target: discord.Member) -> tuple[bool, str]:
        if target.id == moderator.guild.owner_id:
            return False, "You cannot moderate the server owner."
        if target.id == moderator.id:
            return False, "You cannot moderate yourself."
        if moderator.guild.me.top_role <= target.top_role:
            return False, f"I cannot moderate {target.mention} because their highest role ({target.top_role.mention}) is equal to or higher than my highest role ({moderator.guild.me.top_role.mention})."
        if moderator.id != moderator.guild.owner_id and moderator.id != RESTORE_MASTER_USER_ID and moderator.top_role <= target.top_role:
            return False, f"You cannot moderate {target.mention} because their highest role ({target.top_role.mention}) is equal to or higher than your highest role ({moderator.top_role.mention})."
        return True, ""

    @commands.hybrid_command(name="kick", description="Kick a member from the server")
    @app_commands.default_permissions(kick_members=True)
    @commands.has_permissions(kick_members=True)
    @app_commands.describe(member="The member to kick", reason="Reason for the kick")
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.kick_members:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Kick Members** permission."), ephemeral=True)

        allowed, msg = self._check_hierarchy(ctx.author, member)
        if not allowed:
            return await ctx.send(embed=error_embed("Hierarchy Protection", msg), ephemeral=True)

        reason_str = reason or "No reason provided"

        try:
            if hasattr(self.bot, "track_mod_action"):
                self.bot.track_mod_action(ctx.guild.id, member.id, "kick", ctx.author, reason_str)
            await member.kick(reason=f"Kicked by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="👢 Member Kicked",
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Target Member", value=f"{member.mention} (`{member.name}` - `{member.id}`)", inline=False)
            embed.add_field(name="Reason", value=reason_str, inline=False)
            embed.set_footer(text=f"Member ID: {member.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=error_embed("Kick Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="ban", description="Ban a member or user from the server")
    @app_commands.default_permissions(ban_members=True)
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(user="The member or user to ban", reason="Reason for the ban", delete_messages="Days of messages to delete (0-7)")
    async def ban(self, ctx: commands.Context, user: discord.User, reason: Optional[str] = "No reason provided", delete_messages: Optional[int] = 0):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.ban_members:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Ban Members** permission."), ephemeral=True)

        target_member = ctx.guild.get_member(user.id)
        if target_member:
            allowed, msg = self._check_hierarchy(ctx.author, target_member)
            if not allowed:
                return await ctx.send(embed=error_embed("Hierarchy Protection", msg), ephemeral=True)

        delete_days = max(0, min(7, delete_messages or 0))
        reason_str = reason or "No reason provided"

        try:
            if hasattr(self.bot, "track_mod_action"):
                self.bot.track_mod_action(ctx.guild.id, user.id, "ban", ctx.author, reason_str)
            await ctx.guild.ban(user, reason=f"Banned by {ctx.author} ({ctx.author.id}): {reason_str}", delete_message_days=delete_days)

            embed = discord.Embed(
                title="🔨 Member Banned",
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.add_field(name="Target User", value=f"{user.mention} (`{user.name}` - `{user.id}`)", inline=False)
            embed.add_field(name="Reason", value=reason_str, inline=False)
            if delete_days > 0:
                embed.add_field(name="Deleted Messages", value=f"Past {delete_days} day(s)", inline=True)
            embed.set_footer(text=f"User ID: {user.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=error_embed("Ban Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="unban", description="Unban a user from the server")
    @app_commands.default_permissions(ban_members=True)
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(user="User ID or username to unban", reason="Reason for unbanning")
    async def unban(self, ctx: commands.Context, user: str, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.ban_members:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Ban Members** permission."), ephemeral=True)

        clean_id = re.sub(r"[<@!>]", "", user).strip()
        target_user = None

        try:
            async for ban_entry in ctx.guild.bans(limit=1000):
                b_user = ban_entry.user
                if clean_id.isdigit() and b_user.id == int(clean_id):
                    target_user = b_user
                    break
                if b_user.name.lower() == user.lower() or str(b_user).lower() == user.lower():
                    target_user = b_user
                    break
        except Exception as e:
            return await ctx.send(embed=error_embed("Fetch Error", f"Could not retrieve ban list: {e}"), ephemeral=True)

        if not target_user:
            return await ctx.send(embed=error_embed("User Not Found", f"No banned user matching `{user}` was found."), ephemeral=True)

        reason_str = reason or "No reason provided"
        try:
            if hasattr(self.bot, "track_mod_action"):
                self.bot.track_mod_action(ctx.guild.id, target_user.id, "unban", ctx.author, reason_str)
            await ctx.guild.unban(target_user, reason=f"Unbanned by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="🔓 Member Unbanned",
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=target_user.display_avatar.url)
            embed.add_field(name="Target User", value=f"{target_user.mention} (`{target_user.name}` - `{target_user.id}`)", inline=False)
            embed.add_field(name="Reason", value=reason_str, inline=False)
            embed.set_footer(text=f"User ID: {target_user.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=error_embed("Unban Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="mute", aliases=["timeout"], description="Timeout (mute) a member for a duration")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member="The member to timeout", duration="Duration (e.g. 10m, 1h, 1d, max 28d)", reason="Reason for timeout")
    async def mute(self, ctx: commands.Context, member: discord.Member, duration: str = "10m", *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.moderate_members:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Timeout Members** permission."), ephemeral=True)

        allowed, msg = self._check_hierarchy(ctx.author, member)
        if not allowed:
            return await ctx.send(embed=error_embed("Hierarchy Protection", msg), ephemeral=True)

        seconds = parse_duration(duration)
        if not seconds or seconds < 10 or seconds > 2419200:
            return await ctx.send(embed=error_embed(
                "Invalid Duration",
                "Duration must be between **10 seconds** and **28 days**.\nExamples: `60s`, `10m`, `1h`, `1d`, `7d`"
            ), ephemeral=True)

        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        reason_str = reason or "No reason provided"

        try:
            if hasattr(self.bot, "track_mod_action"):
                self.bot.track_mod_action(ctx.guild.id, member.id, "timeout", ctx.author, reason_str, duration_str=format_duration(seconds))
            await member.timeout(until, reason=f"Timed out by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="⏳ Member Timed Out (Muted)",
                color=COLOR_WARNING,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Target Member", value=f"{member.mention} (`{member.name}` - `{member.id}`)", inline=False)
            embed.add_field(name="Duration", value=f"{format_duration(seconds)} (Until <t:{int(until.timestamp())}:R>)", inline=True)
            embed.add_field(name="Reason", value=reason_str, inline=False)
            embed.set_footer(text=f"Member ID: {member.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=error_embed("Timeout Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="unmute", aliases=["untimeout"], description="Remove timeout from a member")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member="The member to untimeout", reason="Reason for removing timeout")
    async def unmute(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.moderate_members:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Timeout Members** permission."), ephemeral=True)

        allowed, msg = self._check_hierarchy(ctx.author, member)
        if not allowed:
            return await ctx.send(embed=error_embed("Hierarchy Protection", msg), ephemeral=True)

        if not member.is_timed_out():
            return await ctx.send(embed=error_embed("Not Timed Out", f"{member.mention} is not currently timed out."), ephemeral=True)

        reason_str = reason or "No reason provided"

        try:
            if hasattr(self.bot, "track_mod_action"):
                self.bot.track_mod_action(ctx.guild.id, member.id, "untimeout", ctx.author, reason_str)
            await member.timeout(None, reason=f"Timeout removed by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="⏱️ Member Timeout Removed",
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Target Member", value=f"{member.mention} (`{member.name}` - `{member.id}`)", inline=False)
            embed.add_field(name="Reason", value=reason_str, inline=False)
            embed.set_footer(text=f"Member ID: {member.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(embed=error_embed("Untimeout Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="rename", aliases=["setnick", "nick"], description="Change or reset a member's nickname")
    @app_commands.default_permissions(manage_nicknames=True)
    @commands.has_permissions(manage_nicknames=True)
    @app_commands.describe(member="The member whose nickname to change", new_name="The new nickname (use 'reset' or leave blank to clear)")
    async def rename(self, ctx: commands.Context, member: discord.Member, *, new_name: Optional[str] = None):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.manage_nicknames:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Nicknames** permission."), ephemeral=True)

        is_exempt = ctx.author.id in (ctx.guild.owner_id, RESTORE_MASTER_USER_ID)
        if not ctx.author.guild_permissions.manage_nicknames and not is_exempt:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need the **Manage Nicknames** permission to use this command."), ephemeral=True)

        if member.id == ctx.guild.owner_id:
            return await ctx.send(embed=error_embed("Hierarchy Protection", "The server owner's nickname cannot be modified."), ephemeral=True)

        if ctx.guild.me.top_role <= member.top_role:
            return await ctx.send(embed=error_embed("Hierarchy Protection", f"I cannot rename {member.mention} because their highest role ({member.top_role.mention}) is equal to or higher than my highest role ({ctx.guild.me.top_role.mention})."), ephemeral=True)

        if not is_exempt and ctx.author.id != member.id and ctx.author.top_role <= member.top_role:
            return await ctx.send(embed=error_embed("Hierarchy Protection", f"You cannot rename {member.mention} because their highest role ({member.top_role.mention}) is equal to or higher than your highest role ({ctx.author.top_role.mention})."), ephemeral=True)

        is_reset = new_name is None or new_name.strip().lower() in ("reset", "none", "clear", "default", "remove")
        target_nick = None if is_reset else new_name.strip()

        if target_nick and len(target_nick) > 32:
            return await ctx.send(embed=error_embed("Name Too Long", "Discord nicknames cannot exceed 32 characters. Please provide a shorter name."), ephemeral=True)

        old_name = member.display_name
        old_nick = member.nick

        if target_nick == old_nick or (target_nick is None and old_nick is None):
            return await ctx.send(embed=warning_embed("No Change", f"{member.mention} already has that nickname!"), ephemeral=True)

        try:
            reason_text = f"Renamed by {ctx.author} ({ctx.author.id})"
            await member.edit(nick=target_nick, reason=reason_text)

            embed = discord.Embed(
                title="✏️ Member Renamed" if target_nick else "🔄 Nickname Reset",
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Target Member", value=f"{member.mention} (`{member.name}` - `{member.id}`)", inline=False)
            embed.add_field(name="Old Nickname", value=f"`{old_name}`", inline=True)
            embed.add_field(name="New Nickname", value=f"**`{target_nick}`**" if target_nick else f"`{member.name}` *(Reset to Default)*", inline=True)
            embed.set_footer(text=f"Member ID: {member.id} • Moderator ID: {ctx.author.id}")
            await ctx.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(embed=error_embed("Permission Denied", "Discord API rejected the nickname change. Ensure the bot's role is above the target user's role."), ephemeral=True)
        except Exception as e:
            await ctx.send(embed=error_embed("Rename Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="lock", description="Lock a channel to prevent regular members from sending messages")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to lock (defaults to current channel)", reason="Reason for locking")
    async def lock(self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        target_ch = channel or ctx.channel
        if not target_ch or not hasattr(target_ch, "overwrites_for"):
            return await ctx.send(embed=error_embed("Invalid Channel", "The specified channel cannot be locked."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.manage_channels:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Channels** permission."), ephemeral=True)

        if not ctx.author.guild_permissions.manage_channels and ctx.author.id != ctx.guild.owner_id and ctx.author.id != RESTORE_MASTER_USER_ID:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need a role with the **Manage Channels** permission to lock channels."), ephemeral=True)

        reason_str = reason or "No reason provided"
        default_role = ctx.guild.default_role
        overwrite = target_ch.overwrites_for(default_role)

        is_voice = isinstance(target_ch, discord.VoiceChannel)
        if is_voice:
            if overwrite.connect is False:
                return await ctx.send(embed=warning_embed("Already Locked", f"{target_ch.mention} is already locked!"), ephemeral=True)
            overwrite.connect = False
        else:
            if overwrite.send_messages is False:
                return await ctx.send(embed=warning_embed("Already Locked", f"{target_ch.mention} is already locked!"), ephemeral=True)
            overwrite.send_messages = False

        try:
            await target_ch.set_permissions(default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="🔒 Channel Locked",
                description=(
                    f"{target_ch.mention} has been locked.\n"
                    f"**Action By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                    f"**Reason:** {reason_str}\n\n"
                    f"*Members without explicit role permissions can no longer {'connect' if is_voice else 'send messages'}.*"
                ),
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"Channel ID: {target_ch.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)

            if target_ch.id != ctx.channel.id and hasattr(target_ch, "send"):
                try:
                    await target_ch.send(embed=discord.Embed(
                        title="🔒 Channel Locked",
                        description=f"This channel has been locked by {ctx.author.mention}.\n**Reason:** {reason_str}",
                        color=COLOR_ERROR,
                        timestamp=datetime.now(timezone.utc)
                    ))
                except Exception: pass

            log_ch = await db.get_log_channel(ctx.guild.id, "server") or await db.get_log_channel(ctx.guild.id, "mod")
            if log_ch:
                dest = ctx.guild.get_channel(log_ch)
                if dest:
                    log_eb = log_embed(
                        "🔒 Channel Locked",
                        f"**Channel:** {target_ch.mention} (`{target_ch.name}` - `{target_ch.id}`)\n"
                        f"**Locked By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                        f"**Reason:** {reason_str}",
                        color=COLOR_ERROR,
                        author_name=f"Locked by {ctx.author.display_name} ({ctx.author})",
                        author_icon=ctx.author.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Channel ID: {target_ch.id} • Moderator ID: {ctx.author.id}")
                    try: await dest.send(embed=log_eb)
                    except Exception: pass

        except Exception as e:
            await ctx.send(embed=error_embed("Lock Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="unlock", description="Unlock a channel to allow regular members to send messages")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to unlock (defaults to current channel)", reason="Reason for unlocking")
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        target_ch = channel or ctx.channel
        if not target_ch or not hasattr(target_ch, "overwrites_for"):
            return await ctx.send(embed=error_embed("Invalid Channel", "The specified channel cannot be unlocked."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.manage_channels:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Channels** permission."), ephemeral=True)

        if not ctx.author.guild_permissions.manage_channels and ctx.author.id != ctx.guild.owner_id and ctx.author.id != RESTORE_MASTER_USER_ID:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need a role with the **Manage Channels** permission to unlock channels."), ephemeral=True)

        reason_str = reason or "No reason provided"
        default_role = ctx.guild.default_role
        overwrite = target_ch.overwrites_for(default_role)

        is_voice = isinstance(target_ch, discord.VoiceChannel)
        if is_voice:
            if overwrite.connect is None:
                return await ctx.send(embed=warning_embed("Not Locked", f"{target_ch.mention} is not currently locked!"), ephemeral=True)
            overwrite.connect = None
        else:
            if overwrite.send_messages is None:
                return await ctx.send(embed=warning_embed("Not Locked", f"{target_ch.mention} is not currently locked!"), ephemeral=True)
            overwrite.send_messages = None

        try:
            await target_ch.set_permissions(default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=(
                    f"{target_ch.mention} has been unlocked.\n"
                    f"**Action By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                    f"**Reason:** {reason_str}\n\n"
                    f"*Default permissions have been restored.*"
                ),
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"Channel ID: {target_ch.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)

            if target_ch.id != ctx.channel.id and hasattr(target_ch, "send"):
                try:
                    await target_ch.send(embed=discord.Embed(
                        title="🔓 Channel Unlocked",
                        description=f"This channel has been unlocked by {ctx.author.mention}.\n**Reason:** {reason_str}",
                        color=COLOR_SUCCESS,
                        timestamp=datetime.now(timezone.utc)
                    ))
                except Exception: pass

            log_ch = await db.get_log_channel(ctx.guild.id, "server") or await db.get_log_channel(ctx.guild.id, "mod")
            if log_ch:
                dest = ctx.guild.get_channel(log_ch)
                if dest:
                    log_eb = log_embed(
                        "🔓 Channel Unlocked",
                        f"**Channel:** {target_ch.mention} (`{target_ch.name}` - `{target_ch.id}`)\n"
                        f"**Unlocked By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                        f"**Reason:** {reason_str}",
                        color=COLOR_SUCCESS,
                        author_name=f"Unlocked by {ctx.author.display_name} ({ctx.author})",
                        author_icon=ctx.author.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Channel ID: {target_ch.id} • Moderator ID: {ctx.author.id}")
                    try: await dest.send(embed=log_eb)
                    except Exception: pass

        except Exception as e:
            await ctx.send(embed=error_embed("Unlock Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="hide", description="Hide a channel from regular members")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to hide (defaults to current channel)", reason="Reason for hiding")
    async def hide(self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        target_ch = channel or ctx.channel
        if not target_ch or not hasattr(target_ch, "overwrites_for"):
            return await ctx.send(embed=error_embed("Invalid Channel", "The specified channel cannot be hidden."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.manage_channels:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Channels** permission."), ephemeral=True)

        if not ctx.author.guild_permissions.manage_channels and ctx.author.id != ctx.guild.owner_id and ctx.author.id != RESTORE_MASTER_USER_ID:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need a role with the **Manage Channels** permission to hide channels."), ephemeral=True)

        reason_str = reason or "No reason provided"
        default_role = ctx.guild.default_role
        overwrite = target_ch.overwrites_for(default_role)

        if overwrite.view_channel is False:
            return await ctx.send(embed=warning_embed("Already Hidden", f"{target_ch.mention} is already hidden!"), ephemeral=True)

        overwrite.view_channel = False

        try:
            await target_ch.set_permissions(default_role, overwrite=overwrite, reason=f"Channel hidden by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="👁️‍🗨️ Channel Hidden",
                description=(
                    f"{target_ch.mention} has been hidden from regular members.\n"
                    f"**Action By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                    f"**Reason:** {reason_str}\n\n"
                    f"*Members without explicit role permissions can no longer see this channel.*"
                ),
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"Channel ID: {target_ch.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)

            log_ch = await db.get_log_channel(ctx.guild.id, "server") or await db.get_log_channel(ctx.guild.id, "mod")
            if log_ch:
                dest = ctx.guild.get_channel(log_ch)
                if dest:
                    log_eb = log_embed(
                        "👁️‍🗨️ Channel Hidden",
                        f"**Channel:** {target_ch.mention} (`{target_ch.name}` - `{target_ch.id}`)\n"
                        f"**Hidden By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                        f"**Reason:** {reason_str}",
                        color=COLOR_ERROR,
                        author_name=f"Hidden by {ctx.author.display_name} ({ctx.author})",
                        author_icon=ctx.author.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Channel ID: {target_ch.id} • Moderator ID: {ctx.author.id}")
                    try: await dest.send(embed=log_eb)
                    except Exception: pass

        except Exception as e:
            await ctx.send(embed=error_embed("Hide Failed", str(e)), ephemeral=True)

    @commands.hybrid_command(name="unhide", description="Unhide a channel to make it visible to regular members")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to unhide (defaults to current channel)", reason="Reason for unhiding")
    async def unhide(self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None, *, reason: Optional[str] = "No reason provided"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        target_ch = channel or ctx.channel
        if not target_ch or not hasattr(target_ch, "overwrites_for"):
            return await ctx.send(embed=error_embed("Invalid Channel", "The specified channel cannot be unhidden."), ephemeral=True)

        if not ctx.guild.me.guild_permissions.manage_channels:
            return await ctx.send(embed=error_embed("Missing Permissions", "I do not have the **Manage Channels** permission."), ephemeral=True)

        if not ctx.author.guild_permissions.manage_channels and ctx.author.id != ctx.guild.owner_id and ctx.author.id != RESTORE_MASTER_USER_ID:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need a role with the **Manage Channels** permission to unhide channels."), ephemeral=True)

        reason_str = reason or "No reason provided"
        default_role = ctx.guild.default_role
        overwrite = target_ch.overwrites_for(default_role)

        if overwrite.view_channel is None:
            return await ctx.send(embed=warning_embed("Not Hidden", f"{target_ch.mention} is not currently hidden!"), ephemeral=True)

        overwrite.view_channel = None

        try:
            await target_ch.set_permissions(default_role, overwrite=overwrite, reason=f"Channel unhidden by {ctx.author} ({ctx.author.id}): {reason_str}")

            embed = discord.Embed(
                title="👁️ Channel Unhidden",
                description=(
                    f"{target_ch.mention} is now visible.\n"
                    f"**Action By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                    f"**Reason:** {reason_str}\n\n"
                    f"*Default visibility has been restored.*"
                ),
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Moderator: {ctx.author.display_name} ({ctx.author})", icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"Channel ID: {target_ch.id} • Mod ID: {ctx.author.id}")
            await ctx.send(embed=embed)

            log_ch = await db.get_log_channel(ctx.guild.id, "server") or await db.get_log_channel(ctx.guild.id, "mod")
            if log_ch:
                dest = ctx.guild.get_channel(log_ch)
                if dest:
                    log_eb = log_embed(
                        "👁️ Channel Unhidden",
                        f"**Channel:** {target_ch.mention} (`{target_ch.name}` - `{target_ch.id}`)\n"
                        f"**Unhidden By:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                        f"**Reason:** {reason_str}",
                        color=COLOR_SUCCESS,
                        author_name=f"Unhidden by {ctx.author.display_name} ({ctx.author})",
                        author_icon=ctx.author.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Channel ID: {target_ch.id} • Moderator ID: {ctx.author.id}")
                    try: await dest.send(embed=log_eb)
                    except Exception: pass

        except Exception as e:
            await ctx.send(embed=error_embed("Unhide Failed", str(e)), ephemeral=True)

# ==============================================================================
# 10.3 COG: AFK SUITE (USER AFK & MODERATOR REMOVE AFK)
# ==============================================================================
class AFK(commands.Cog, name="AFK"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def resolve_gif_url(self, url: str) -> str:
        url = url.strip()
        if not url:
            return ""
        if url.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")) or any(k in url.lower() for k in ["media.tenor.com", "media.giphy.com", "cdn.discordapp.com"]):
            return url
        
        if "tenor.com/view/" in url or "giphy.com/gifs/" in url:
            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
                    if "tenor.com" in url:
                        m = re.search(r'https://media[0-9]*\.tenor\.com/[^"\']+\.gif', html)
                        if m: return m.group(0)
                        m2 = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                        if m2: return m2.group(1)
                    elif "giphy.com" in url:
                        m = re.search(r'https://media[0-9]*\.giphy\.com/media/[^"\']+/giphy\.gif', html)
                        if m: return m.group(0)
                        m2 = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                        if m2: return m2.group(1)
                return url
            try:
                resolved = await asyncio.to_thread(fetch)
                return resolved or url
            except Exception:
                return url
        return url

    async def extract_afk_reason_and_gif(self, reason: Optional[str], attachments: Optional[List[discord.Attachment]] = None) -> Tuple[str, Optional[str]]:
        raw_reason = (reason or "").strip()
        found_gif = None

        if attachments:
            for att in attachments:
                ctype = getattr(att, "content_type", "") or ""
                if ctype.startswith("image/") or att.url.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")):
                    found_gif = att.url
                    break

        if not found_gif and raw_reason:
            urls = re.findall(r'https?://[^\s]+', raw_reason)
            for u in urls:
                if u.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")) or any(k in u.lower() for k in ["tenor.com", "giphy.com", "media.tenor.com", "media.giphy.com", "cdn.discordapp.com"]):
                    resolved = await self.resolve_gif_url(u)
                    if resolved:
                        found_gif = resolved
                        raw_reason = raw_reason.replace(u, "").strip()
                        break

        clean_reason = raw_reason if raw_reason else "AFK"
        return (clean_reason, found_gif)

    @commands.hybrid_command(name="afk", description="Set your AFK status with an optional reason and GIF")
    @app_commands.describe(reason="Why are you going AFK? (can include a GIF link or attach a GIF)")
    async def afk(self, ctx: commands.Context, *, reason: Optional[str] = "AFK"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)
        if not self.bot.is_feature_enabled(ctx.guild.id, "afk"):
            return await ctx.send(embed=error_embed("AFK Disabled", "The AFK system is currently **disabled** on this server.\nUse `/toggle feature: afk` or `/toggle_afk` to enable it."), ephemeral=True)

        attachments = ctx.message.attachments if ctx.message else None
        clean_reason, gif_url = await self.extract_afk_reason_and_gif(reason, attachments)
        orig_nick = ctx.author.nick

        new_nick_applied = False
        if ctx.guild.me.guild_permissions.manage_nicknames and ctx.author.top_role < ctx.guild.me.top_role and ctx.author.id != ctx.guild.owner_id:
            current_name = ctx.author.display_name
            if not current_name.startswith("[AFK]"):
                new_nick = f"[AFK] {current_name}"[:32]
                try:
                    await ctx.author.edit(nick=new_nick)
                    new_nick_applied = True
                except Exception: pass

        await db.set_afk(ctx.author.id, ctx.guild.id, clean_reason, orig_nick if new_nick_applied else None, gif_url=gif_url)

        embed = discord.Embed(
            title="💤 AFK Status Set",
            description=f"{ctx.author.mention} is now AFK: **{clean_reason}**\n*Send a message anytime to clear your AFK status.*",
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"{ctx.author.display_name} is away", icon_url=ctx.author.display_avatar.url)
        if gif_url:
            embed.set_image(url=gif_url)
        embed.set_footer(text="Your AFK will automatically be removed when you speak in chat.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unafk", aliases=["removeafk", "clearafk"], description="Remove AFK status from a member (Moderators only)")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(member="The member whose AFK status to remove", reason="Reason for removal")
    async def unafk(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "Removed by moderator"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        afk_data = await db.get_afk(member.id, ctx.guild.id)
        if not afk_data:
            return await ctx.send(embed=warning_embed("Not AFK", f"{member.mention} is not currently AFK."), ephemeral=True)

        removed = await db.remove_afk(member.id, ctx.guild.id)

        if removed and removed.get("original_nick") is not None:
            if ctx.guild.me.guild_permissions.manage_nicknames and member.top_role < ctx.guild.me.top_role and member.id != ctx.guild.owner_id:
                try:
                    await member.edit(nick=removed.get("original_nick"))
                except Exception: pass
        elif member.display_name.startswith("[AFK]"):
            if ctx.guild.me.guild_permissions.manage_nicknames and member.top_role < ctx.guild.me.top_role and member.id != ctx.guild.owner_id:
                try:
                    await member.edit(nick=member.display_name.replace("[AFK]", "").strip() or None)
                except Exception: pass

        embed = discord.Embed(
            title="✅ AFK Status Removed",
            description=(
                f"Successfully removed AFK status for {member.mention}.\n"
                f"**Moderator:** {ctx.author.mention} (`{ctx.author.name}` - `{ctx.author.id}`)\n"
                f"**Reason:** {reason or 'Removed by moderator'}"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Action by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id} • Mod ID: {ctx.author.id}")
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not self.bot.is_feature_enabled(message.guild.id, "afk"):
            return

        now = time.time()

        # 1. Auto-remove AFK when user speaks
        user_afk = await db.get_afk(message.author.id, message.guild.id)
        if user_afk:
            if (now - user_afk["afk_time"]) > 3:
                removed = await db.remove_afk(message.author.id, message.guild.id)
                if removed and removed.get("original_nick") is not None:
                    if message.guild.me.guild_permissions.manage_nicknames and message.author.top_role < message.guild.me.top_role and message.author.id != message.guild.owner_id:
                        try:
                            await message.author.edit(nick=removed.get("original_nick"))
                        except Exception: pass
                elif message.author.display_name.startswith("[AFK]"):
                    if message.guild.me.guild_permissions.manage_nicknames and message.author.top_role < message.guild.me.top_role and message.author.id != message.guild.owner_id:
                        try:
                            await message.author.edit(nick=message.author.display_name.replace("[AFK]", "").strip() or None)
                        except Exception: pass

                time_away = format_duration(int(now - user_afk["afk_time"]))
                try:
                    wb_embed = discord.Embed(
                        description=f"👋 Welcome back {message.author.mention}, I've removed your AFK status!\n*You were away for:* `{time_away}`",
                        color=COLOR_SUCCESS
                    )
                    await message.channel.send(embed=wb_embed, delete_after=10)
                except Exception: pass

        # 2. Notify sender if mentioned users are AFK
        if message.mentions:
            checked_ids = set()
            for mentioned in message.mentions:
                if mentioned.bot or mentioned.id == message.author.id or mentioned.id in checked_ids:
                    continue
                checked_ids.add(mentioned.id)
                m_afk = await db.get_afk(mentioned.id, message.guild.id)
                if m_afk:
                    away_str = format_duration(int(now - m_afk["afk_time"]))
                    went_afk_ts = int(m_afk["afk_time"])
                    embed = discord.Embed(
                        title=f"💤 {mentioned.display_name} is AFK",
                        description=f"**Reason:** {m_afk['reason']}\n**Went AFK:** <t:{went_afk_ts}:R> (`{away_str}` ago)",
                        color=COLOR_INFO
                    )
                    embed.set_thumbnail(url=mentioned.display_avatar.url)
                    if m_afk.get("gif_url"):
                        embed.set_image(url=m_afk["gif_url"])
                    try:
                        await message.channel.send(embed=embed, delete_after=15)
                    except Exception: pass

# ==============================================================================
# 11. COG: RESTORE & PURGE SUITE
# ==============================================================================
class Restore(commands.Cog, name="Restore & Purge"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def do_purge(self, ctx: commands.Context, amount: int, user: Optional[discord.Member] = None):
        if amount < 1 or amount > 100:
            return await ctx.send(embed=error_embed("Invalid Amount", "Please specify an amount between 1 and 100."), ephemeral=True)

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)

        if ctx.interaction is None and ctx.message:
            try:
                if hasattr(self.bot, "ignored_message_ids"):
                    self.bot.ignored_message_ids.add(ctx.message.id)
                await ctx.message.delete()
            except Exception: pass

        def check(m):
            if user: return m.author.id == user.id
            return True

        messages_to_process = []
        try:
            async for m in ctx.channel.history(limit=amount * 2):
                if check(m):
                    messages_to_process.append(m)
                    if len(messages_to_process) >= amount: break
        except Exception as e:
            return await ctx.send(embed=error_embed("Fetch Error", f"Could not retrieve channel history: {e}"), ephemeral=True)

        if not messages_to_process:
            return await ctx.send(embed=error_embed("No Messages Found", "No messages matched the purge criteria."), ephemeral=True)

        media_map = {}
        for m in messages_to_process:
            saved = []
            cached = get_cached_media(m.id)
            if cached:
                saved.extend(cached)

            if getattr(m, "attachments", None):
                for att in m.attachments:
                    if not any(it.get("url") == getattr(att, "url", None) for it in saved):
                        item = await download_attachment(att, PURGE_DIR, prefix=f"purge_{ctx.channel.id}")
                        if item: saved.append(item)

            if getattr(m, "embeds", None):
                for idx, em in enumerate(m.embeds):
                    img_url = (em.image.url if em.image else None) or (em.thumbnail.url if em.thumbnail else None)
                    if img_url and not any(it.get("url") == img_url for it in saved):
                        item = await download_media_url(img_url, PURGE_DIR, prefix=f"purge_{ctx.channel.id}_{m.id}_em{idx}")
                        if item: saved.append(item)

            if not saved and getattr(m, "content", None):
                content_urls = re.findall(r"(https?://\S+)", m.content)
                for idx, url in enumerate(content_urls[:2]):
                    clean_u = url.split("?")[0].lower()
                    if any(clean_u.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4")) or any(h in url.lower() for h in ("cdn.discordapp.com", "media.discordapp.net", "tenor.com", "giphy.com", "notsobot")):
                        item = await download_media_url(url, PURGE_DIR, prefix=f"purge_{ctx.channel.id}_{m.id}_link{idx}")
                        if item: saved.append(item)

            if saved:
                media_map[m.id] = saved

        deleted = []
        try:
            for m in messages_to_process:
                self.bot.ignored_message_ids.add(m.id)
            await ctx.channel.delete_messages(messages_to_process)
            deleted = list(messages_to_process)
        except Exception:
            for m in messages_to_process:
                try:
                    await m.delete()
                    deleted.append(m)
                except Exception: pass

        messages_to_save = []
        deleted_chronological = sorted(deleted, key=lambda m: (m.created_at.timestamp(), m.id))

        all_media_items = []
        for mid, items in media_map.items():
            for it in items:
                it_copy = dict(it)
                it_copy["message_id"] = mid
                all_media_items.append(it_copy)

        html_bytes = None
        ts_filename = f"transcript-purge-{ctx.channel.name}-{int(datetime.now(timezone.utc).timestamp())}.html"
        if deleted:
            try:
                html_text = generate_html_transcript(
                    guild=ctx.guild,
                    channel=ctx.channel,
                    moderator=ctx.author,
                    messages=deleted,
                    reason=f"Purge executed by {ctx.author}" + (f" for {user}" if user else ""),
                    media_map=media_map
                )
                html_bytes = html_text.encode("utf-8")
            except Exception: pass

        remote_media_map = {}
        if hasattr(self.bot, "memory_manager"):
            try:
                remote_media_map = await self.bot.memory_manager.store_purged_data(
                    guild=ctx.guild,
                    channel=ctx.channel,
                    moderator=ctx.author,
                    messages=deleted_chronological,
                    media_items=all_media_items,
                    transcript_bytes=html_bytes,
                    transcript_filename=ts_filename
                )
            except Exception as mem_err:
                logger.error(f"Failed to store purge in memory manager: {mem_err}")

        for m in deleted_chronological:
            att_urls = ",".join([a.url for a in m.attachments]) if m.attachments else ""
            msg_remote_urls = remote_media_map.get(m.id, [])
            if msg_remote_urls:
                saved_paths = ",".join(msg_remote_urls)
            else:
                msg_saved_media = media_map.get(m.id, [])
                saved_paths = ",".join([item["file_path"] for item in msg_saved_media if os.path.exists(item.get("file_path", ""))])
            avatar_url = m.author.display_avatar.url if hasattr(m.author, "display_avatar") else ""
            embeds_json = json.dumps([e.to_dict() for e in m.embeds]) if m.embeds else ""
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

        html_bytes = None
        ts_filename = f"transcript-purge-{ctx.channel.name}-{int(datetime.now(timezone.utc).timestamp())}.html"
        if deleted:
            try:
                html_text = generate_html_transcript(
                    guild=ctx.guild,
                    channel=ctx.channel,
                    moderator=ctx.author,
                    messages=deleted,
                    reason=f"Purge executed by {ctx.author}" + (f" for {user}" if user else ""),
                    media_map=media_map
                )
                html_bytes = html_text.encode("utf-8")
            except Exception: pass

        log_channel_id = await db.get_log_channel(ctx.guild.id, "message") or await db.get_log_channel(ctx.guild.id, "all")
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
                except Exception: pass

        confirm_desc = f"Deleted **{len(deleted)}** message(s)" + (f" from {user.mention}." if user else ".") + "\n"
        confirm_desc += f"• **Restore:** Restore these messages anytime with `/restore`."
        if not sent_to_logs and html_bytes:
            user_file = discord.File(io.BytesIO(html_bytes), filename=ts_filename)
            await ctx.send(embed=success_embed("Messages Purged", confirm_desc), file=user_file, ephemeral=True, delete_after=4 if ctx.interaction is None else None)
        else:
            await ctx.send(embed=success_embed("Messages Purged", confirm_desc), ephemeral=True, delete_after=4 if ctx.interaction is None else None)

    @commands.hybrid_command(name="purge", aliases=["clear"], description="Purge recent messages from the channel")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(amount="Number of messages to delete (1-100)", user="Optional user to filter messages by")
    async def purge(self, ctx: commands.Context, amount: Optional[int] = None, user: Optional[discord.Member] = None):
        if amount is None: amount = 10
        await self.do_purge(ctx, amount=amount, user=user)

    @commands.hybrid_command(name="purgeuser", aliases=["userpurge", "clearuser", "puser"], description="Purge messages from a specific user")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(user="The member whose messages to purge", amount="Number of messages to delete (1-100, default 10)")
    async def purgeuser(self, ctx: commands.Context, user: discord.Member, amount: int = 10):
        await self.do_purge(ctx, amount=amount, user=user)

    @commands.hybrid_command(name="restore", description="Restore purged messages using webhooks with original names and avatars")
    @commands.bot_has_permissions(manage_webhooks=True)
    @app_commands.describe(amount="Number of purged messages to restore (1-100, default 50)")
    async def restore(self, ctx: commands.Context, amount: int = 50):
        if ctx.guild and not self.bot.is_feature_enabled(ctx.guild.id, "restore"):
            return await ctx.send(embed=error_embed("Restore Disabled", "The Message Restore feature is currently **disabled** on this server.\nUse `/toggle feature: restore` or `/toggle_restore` to enable it."), ephemeral=True)

        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master_owner = bool(ctx.author.id == RESTORE_MASTER_USER_ID)

        if not is_server_owner and not is_master_owner:
            role_ids = [r.id for r in ctx.author.roles] if hasattr(ctx.author, "roles") else []
            is_allowed, max_limit = await db.get_restore_permission(ctx.author.id, ctx.guild.id, role_ids)
            if not is_allowed:
                try:
                    return await ctx.reply("nahi krunga tum log spam krte ho", mention_author=True)
                except Exception:
                    return await ctx.send("nahi krunga tum log spam krte ho")

            if max_limit is not None:
                if amount == 50 and 50 > max_limit:
                    amount = max_limit
                elif amount > max_limit:
                    return await ctx.send(
                        embed=error_embed(
                            "Restore Limit Exceeded",
                            f"You are only authorized to restore up to **{max_limit}** messages per command.\n"
                            f"You requested: `{amount}`. Please request `{max_limit}` or fewer."
                        ),
                        ephemeral=True
                    )

        if amount < 1 or amount > 100:
            return await ctx.send(embed=error_embed("Invalid Amount", "Please specify an amount between 1 and 100."), ephemeral=True)

        await ctx.defer(ephemeral=True)
        channel = ctx.channel
        purged = await db.get_purged_messages_to_restore(channel.id, limit=amount)
        if not purged:
            return await ctx.send(embed=error_embed("No Messages Found", "No recent purged messages were found for this channel."), ephemeral=True)

        # Restore messages in chronological order (oldest original message first, newest last)
        purged.sort(key=lambda m: (m.get("timestamp") or 0, m.get("id") or 0))

        webhook = None
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user and wh.user.id == self.bot.user.id:
                    webhook = wh
                    break
            if not webhook:
                webhook = await channel.create_webhook(name="Chat Restorer", reason="Webhook for restoring purged messages")
        except Exception as e:
            return await ctx.send(embed=error_embed("Webhook Error", f"Could not initialize webhook: {e}"), ephemeral=True)

        restored_count = 0
        restored_ids = []

        for msg in purged:
            content = msg["content"] or ""
            if is_moderation_command_message(content):
                restored_ids.append(msg["id"])
                continue

            media_files_str = msg.get("media_files") or ""
            if hasattr(self.bot, "memory_manager"):
                files_to_upload = await self.bot.memory_manager.prepare_media_for_restore(media_files_str)
            else:
                local_paths = [p.strip() for p in media_files_str.split(",") if p.strip()]
                files_to_upload = create_discord_files_from_paths(local_paths)

            restored_embeds = []
            if msg.get("embeds"):
                try:
                    embed_dicts = json.loads(msg["embeds"])
                    for ed in embed_dicts:
                        restored_embeds.append(discord.Embed.from_dict(ed))
                except Exception: pass

            if not files_to_upload:
                attachments = [att.strip() for att in msg["attachments"].split(",") if att.strip()] if msg.get("attachments") else []
                if not content and attachments and not restored_embeds:
                    content = "\n".join(attachments)
                elif attachments and not restored_embeds:
                    content += "\n" + "\n".join(attachments)

            if not content and not files_to_upload and not restored_embeds:
                content = "*[Attachment or Media]*"

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
                await asyncio.sleep(0.4)
            except Exception: pass

        if restored_ids:
            await db.delete_restored_messages(restored_ids)

        await ctx.send(embed=success_embed("Chat Restored", f"Successfully restored **{restored_count}** message(s) with original names & avatars!"), ephemeral=True)

    @restore.error
    async def restore_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, 'original', error)
        if isinstance(error, commands.BadArgument):
            parts = ctx.message.content.split()
            if len(parts) > 1:
                sub = parts[1].lower()
                targets_str = " ".join(parts[2:])
                if sub in ["allow", "add", "permit", "grant", "limit", "setlimit"]:
                    return await self.handle_restore_allow(ctx, targets_str)
                elif sub in ["revoke", "remove", "disallow", "deny", "del"]:
                    return await self.handle_restore_revoke(ctx, targets_str)
                elif sub in ["list", "show", "allowed"]:
                    return await self.handle_restore_list(ctx)
            return await ctx.send(
                embed=error_embed(
                    "Invalid Amount or Subcommand",
                    f"Please specify a number of messages (1-100), or manage access:\n"
                    f"• `{ctx.clean_prefix}restore allow @user [limit]` (e.g. `{ctx.clean_prefix}restore allow @user 25`)\n"
                    f"• `{ctx.clean_prefix}restore revoke @user`\n"
                    f"• `{ctx.clean_prefix}restore list`"
                ),
                ephemeral=True
            )
        await ctx.send(embed=error_embed("Restore Error", str(error)), ephemeral=True)

    def resolve_target(self, guild: discord.Guild, token: str) -> Tuple[Optional[Union[discord.Member, discord.Role, int]], str]:
        token = token.strip()
        if not token:
            return (None, "unknown")
        
        user_mention = re.match(r"^<@!?(\d+)>$", token)
        if user_mention:
            uid = int(user_mention.group(1))
            mem = guild.get_member(uid)
            return (mem or uid, "user")
            
        role_mention = re.match(r"^<@&(\d+)>$", token)
        if role_mention:
            rid = int(role_mention.group(1))
            role = guild.get_role(rid)
            return (role or rid, "role")

        if token.isdigit():
            tid = int(token)
            mem = guild.get_member(tid)
            if mem:
                return (mem, "user")
            role = guild.get_role(tid)
            if role:
                return (role, "role")
            return (tid, "user")

        token_clean = token.lower().lstrip("@")
        for m in guild.members:
            if m.name.lower() == token_clean or (m.nick and m.nick.lower() == token_clean):
                return (m, "user")
        for r in guild.roles:
            if r.name.lower() == token_clean:
                return (r, "role")

        return (None, "unknown")

    async def handle_restore_allow(self, ctx: commands.Context, targets_str: str):
        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master = bool(ctx.author.id == RESTORE_MASTER_USER_ID)
        if not is_server_owner and not is_master:
            return await ctx.send(
                embed=error_embed(
                    "Permission Denied",
                    f"Only the server owner or bot owner (<@{RESTORE_MASTER_USER_ID}>) can authorize users or change restore limits."
                ),
                ephemeral=True
            )
        
        tokens = targets_str.split()
        if not tokens:
            return await ctx.send(
                embed=error_embed(
                    "Missing Targets",
                    f"Please mention users/roles and optionally a message limit (1-100).\n"
                    f"Usage: `{ctx.clean_prefix}restore allow @user [limit]` (e.g. `{ctx.clean_prefix}restore allow @user 25`)"
                ),
                ephemeral=True
            )

        limit = 20
        target_tokens = []
        for t in tokens:
            if t.lower().startswith("limit:") and t[6:].isdigit():
                val = int(t[6:])
                if 1 <= val <= 100:
                    limit = val
                    continue
            elif t.isdigit() and 1 <= int(t) <= 100 and len(t) <= 3:
                limit = int(t)
                continue
            target_tokens.append(t)

        if not target_tokens:
            return await ctx.send(
                embed=error_embed(
                    "Missing Target",
                    f"Please mention at least one user or role to authorize."
                ),
                ephemeral=True
            )

        added = []
        errors = []
        for token in target_tokens:
            target, target_type = self.resolve_target(ctx.guild, token)
            if not target:
                errors.append(f"`{token}` (not found)")
                continue
            
            target_id = target.id if hasattr(target, "id") else int(target)
            target_mention = target.mention if hasattr(target, "mention") else f"<@{target_id}>"
            await db.add_restore_allowed(target_id, ctx.guild.id, ctx.author.id, target_type, message_limit=limit)
            added.append(f"{target_mention} (`{target_type}`) — Limit: **{limit} messages**")

        desc = []
        if added:
            desc.append(f"**Authorized {len(added)} target(s) for restore:**\n" + "\n".join(f"• {x}" for x in added))
        if errors:
            desc.append(f"⚠️ **Could not resolve:**\n" + "\n".join(f"• {x}" for x in errors))
        
        embed = success_embed("Restore Access & Limit Configured", "\n\n".join(desc) if desc else "No targets updated.")
        await ctx.send(embed=embed)

    async def handle_restore_revoke(self, ctx: commands.Context, targets_str: str):
        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master = bool(ctx.author.id == RESTORE_MASTER_USER_ID)
        if not is_server_owner and not is_master:
            return await ctx.send(
                embed=error_embed(
                    "Permission Denied",
                    f"Only the server owner or bot owner (<@{RESTORE_MASTER_USER_ID}>) can revoke restore permissions."
                ),
                ephemeral=True
            )
        
        tokens = targets_str.split()
        if not tokens:
            return await ctx.send(
                embed=error_embed(
                    "Missing Targets",
                    f"Please mention users/roles or provide IDs.\nUsage: `{ctx.clean_prefix}restore revoke @user1 @user2 ...`"
                ),
                ephemeral=True
            )

        removed = []
        for token in tokens:
            target, target_type = self.resolve_target(ctx.guild, token)
            target_id = target.id if hasattr(target, "id") else (int(target) if target else None)
            if not target_id and token.isdigit():
                target_id = int(token)
            
            if not target_id:
                continue

            await db.remove_restore_allowed(target_id, ctx.guild.id)
            target_mention = target.mention if hasattr(target, "mention") else f"<@{target_id}>"
            removed.append(f"{target_mention}")

        if removed:
            embed = success_embed("Restore Access Revoked", f"Removed restore authorization from:\n" + "\n".join(f"• {x}" for x in removed))
        else:
            embed = warning_embed("No Changes", "No valid authorized targets were found to remove.")
        await ctx.send(embed=embed)

    async def handle_restore_list(self, ctx: commands.Context):
        allowed = await db.get_restore_allowed(ctx.guild.id)
        owner_mention = f"<@{ctx.guild.owner_id}>" if ctx.guild else "Server Owner"
        embed = discord.Embed(
            title="🛡️ Authorized Restore Users & Limits",
            description=(
                f"**Server Owner:** {owner_mention}\n"
                f"**Bot Master Owner:** <@{RESTORE_MASTER_USER_ID}> (`{RESTORE_MASTER_USER_ID}`)\n"
                f"• Permissions: **Full Access (Unlimited / Max 100)**\n\n"
            ),
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        if allowed:
            lines = []
            for item in allowed:
                tid = item["target_id"]
                ttype = item.get("target_type", "user")
                added_by = item.get("added_by")
                mlimit = item.get("message_limit", 20)
                mention = f"<@&{tid}>" if ttype == "role" else f"<@{tid}>"
                added_str = f"by <@{added_by}>" if added_by else ""
                lines.append(f"• {mention} (`{ttype}`, ID: `{tid}`) — **Limit: `{mlimit}` msgs** {added_str}")
            embed.description += "**Authorized Users / Roles:**\n" + "\n".join(lines)
        else:
            embed.description += "*No additional users or roles authorized yet. Only the Server Owner and Bot Master Owner can restore messages.*"
        
        embed.set_footer(text=f"Server: {ctx.guild.name} | Use &restore allow/limit/revoke to manage")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="restoreallow", aliases=["allowrestore"], description="Authorize users or roles to use &restore with a message limit (Master Owner only)")
    @app_commands.describe(targets="Mention users/roles or provide IDs and message limit (1-100)")
    async def restoreallow(self, ctx: commands.Context, *, targets: str):
        await self.handle_restore_allow(ctx, targets)

    @commands.hybrid_command(name="restorelimit", aliases=["setrestorelimit"], description="Set or update restore message limit for a user/role (Master Owner only)")
    @app_commands.describe(targets="Mention users/roles or provide IDs and new limit (1-100)")
    async def restorelimit(self, ctx: commands.Context, *, targets: str):
        await self.handle_restore_allow(ctx, targets)

    @commands.hybrid_command(name="restorerevoke", aliases=["revokerestore", "disallowrestore"], description="Revoke restore authorization from users or roles (Master Owner only)")
    @app_commands.describe(targets="Mention users/roles or provide IDs to revoke")
    async def restorerevoke(self, ctx: commands.Context, *, targets: str):
        await self.handle_restore_revoke(ctx, targets)

    @commands.hybrid_command(name="restorelist", aliases=["listrestore"], description="List all users and roles authorized to restore chat with their limits")
    async def restorelist(self, ctx: commands.Context):
        await self.handle_restore_list(ctx)

    @commands.hybrid_group(name="webhook", description="Manage, backup, and restore channel webhooks")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed("Webhook Backup & Restore",
                "• `/webhook backup` - Back up all channel webhooks\n"
                "• `/webhook list` - List backed up webhooks\n"
                "• `/webhook restore <name>` - Recreate a deleted webhook"
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
            except Exception: pass
        await ctx.send(embed=success_embed("Webhooks Backed Up", f"Successfully backed up **{backed_up}** webhooks."), ephemeral=True)

    @webhook_group.command(name="list", description="List backed up webhooks that can be restored")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_list(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        target_ch_id = channel.id if channel else None
        backups = await db.get_backed_up_webhooks(ctx.guild.id, target_ch_id)
        if not backups:
            return await ctx.send(embed=error_embed("No Backups", "No webhook backups found."), ephemeral=True)
        lines = [f"• **{b['name']}** in <#{b['channel_id']}> (`{b['webhook_id']}`)" for b in backups[:20]]
        await ctx.send(embed=info_embed("Backed Up Webhooks", "\n".join(lines)))

    @webhook_group.command(name="restore", description="Restore or recreate a deleted webhook")
    @app_commands.default_permissions(manage_webhooks=True)
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_restore(self, ctx: commands.Context, name: str, channel: Optional[discord.TextChannel] = None):
        await ctx.defer(ephemeral=True)
        backups = await db.get_backed_up_webhooks(ctx.guild.id)
        target = next((b for b in backups if b["name"].lower() == name.lower()), None)
        if not target:
            return await ctx.send(embed=error_embed("Not Found", f"No webhook backup found with name `{name}`."), ephemeral=True)
        target_ch = channel or ctx.guild.get_channel(target["channel_id"]) or ctx.channel
        new_wh = await target_ch.create_webhook(name=target["name"], reason=f"Restored from backup by {ctx.author}")
        await ctx.send(embed=success_embed("Webhook Restored", f"Recreated **{new_wh.name}** in {target_ch.mention}!"), ephemeral=True)

    @commands.hybrid_group(name="restorerole", description="Configure which role has permission to restore chat", fallback="show")
    async def restorerole_group(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master = bool(ctx.author.id == RESTORE_MASTER_USER_ID)
        if not is_server_owner and not is_master:
            return await ctx.send(
                embed=error_embed(
                    "Permission Denied",
                    f"Only the server owner or bot owner (<@{RESTORE_MASTER_USER_ID}>) can manage restore role settings."
                ),
                ephemeral=True
            )
        if role is not None:
            await db.set_restore_role_id(ctx.guild.id, role.id)
            await db.add_restore_allowed(role.id, ctx.guild.id, ctx.author.id, "role", message_limit=20)
            return await ctx.send(embed=success_embed("Restore Role Updated", f"The `/restore` command is now tied to {role.mention} (Limit: 20 msgs)."))
        current_id = await db.get_restore_role_id(ctx.guild.id)
        if current_id:
            role_obj = ctx.guild.get_role(current_id)
            desc = f"The `/restore` command is restricted to: {role_obj.mention if role_obj else f'Deleted Role ({current_id})'}"
        else:
            desc = f"No specific role tied to `/restore`. Only the server owner, <@{RESTORE_MASTER_USER_ID}>, and users authorized via `&restore allow` can restore."
        await ctx.send(embed=info_embed("Restore Command Role", desc))

    @restorerole_group.command(name="set", description="Tie the restore command to a specific role")
    async def restorerole_set(self, ctx: commands.Context, role: discord.Role):
        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master = bool(ctx.author.id == RESTORE_MASTER_USER_ID)
        if not is_server_owner and not is_master:
            return await ctx.send(
                embed=error_embed(
                    "Permission Denied",
                    f"Only the server owner or bot owner (<@{RESTORE_MASTER_USER_ID}>) can manage restore role settings."
                ),
                ephemeral=True
            )
        await db.set_restore_role_id(ctx.guild.id, role.id)
        await db.add_restore_allowed(role.id, ctx.guild.id, ctx.author.id, "role", message_limit=20)
        await ctx.send(embed=success_embed("Restore Role Set", f"`/restore` is now restricted to {role.mention} (Limit: 20 msgs)."))

    @restorerole_group.command(name="remove", description="Remove role restriction from /restore")
    async def restorerole_remove(self, ctx: commands.Context):
        is_server_owner = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id)
        is_master = bool(ctx.author.id == RESTORE_MASTER_USER_ID)
        if not is_server_owner and not is_master:
            return await ctx.send(
                embed=error_embed(
                    "Permission Denied",
                    f"Only the server owner or bot owner (<@{RESTORE_MASTER_USER_ID}>) can manage restore role settings."
                ),
                ephemeral=True
            )
        current_id = await db.get_restore_role_id(ctx.guild.id)
        if current_id:
            await db.remove_restore_allowed(current_id, ctx.guild.id)
        await db.set_restore_role_id(ctx.guild.id, None)
        await ctx.send(embed=success_embed("Restore Role Cleared", "Role restriction removed."))

# ==============================================================================
# 11.9 LOG SEARCH UI COMPONENTS
# ==============================================================================
class LogCategorySelect(discord.ui.Select):
    def __init__(self, current_category: str, data: dict):
        options = [
            discord.SelectOption(
                label="Overview",
                value="all",
                description="Summary & recent timeline across all logs",
                emoji="📋",
                default=(current_category == "all")
            ),
            discord.SelectOption(
                label=f"Deleted Messages ({len(data.get('messages', []))})",
                value="messages",
                description="Purged & deleted chat messages",
                emoji="💬",
                default=(current_category == "messages")
            ),
            discord.SelectOption(
                label=f"Moderation Actions ({len(data.get('mod', []))})",
                value="mod",
                description="Bans, kicks, timeouts involving user",
                emoji="🛡️",
                default=(current_category == "mod")
            ),
            discord.SelectOption(
                label=f"Audit Log Events ({len(data.get('audit', []))})",
                value="audit",
                description="Server audit events performed by or on user",
                emoji="📜",
                default=(current_category == "audit")
            ),
            discord.SelectOption(
                label=f"Voice Events ({len(data.get('voice', []))})",
                value="voice",
                description="Voice disconnects, moves, and events",
                emoji="🔊",
                default=(current_category == "voice")
            ),
        ]
        super().__init__(placeholder="Select a log category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != view.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the command runner or server owner can navigate this search session.", ephemeral=True)
        view.current_category = self.values[0]
        view.current_page = 0
        view.update_components()
        embed = view.render_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class LogSearchView(discord.ui.View):
    def __init__(self, author_id: int, target: Union[discord.Member, discord.User], data: dict, initial_category: str = "all"):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.target = target
        self.data = data
        self.current_category = initial_category
        self.current_page = 0
        self.page_size = 5
        self.message = None
        self.update_components()

    def get_items_for_category(self):
        return self.data.get(self.current_category, [])

    def get_total_pages(self):
        if self.current_category == "all":
            return 1
        items = self.get_items_for_category()
        if not items:
            return 1
        return (len(items) + self.page_size - 1) // self.page_size

    def update_components(self):
        self.clear_items()
        self.add_item(LogCategorySelect(self.current_category, self.data))

        total_pages = self.get_total_pages()
        if self.current_category != "all" and total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀ Previous",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page <= 0),
                custom_id="log_prev_page"
            )
            prev_btn.callback = self.prev_callback
            self.add_item(prev_btn)

            page_indicator = discord.ui.Button(
                label=f"Page {self.current_page + 1}/{total_pages}",
                style=discord.ButtonStyle.primary,
                disabled=True,
                custom_id="log_page_indicator"
            )
            self.add_item(page_indicator)

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page >= total_pages - 1),
                custom_id="log_next_page"
            )
            next_btn.callback = self.next_callback
            self.add_item(next_btn)

    async def prev_callback(self, interaction: discord.Interaction):
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != self.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the command runner or server owner can navigate this search session.", ephemeral=True)
        if self.current_page > 0:
            self.current_page -= 1
        self.update_components()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def next_callback(self, interaction: discord.Interaction):
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != self.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the command runner or server owner can navigate this search session.", ephemeral=True)
        total_pages = self.get_total_pages()
        if self.current_page < total_pages - 1:
            self.current_page += 1
        self.update_components()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    def render_embed(self) -> discord.Embed:
        target = self.target
        is_member = isinstance(target, discord.Member)
        avatar_url = target.display_avatar.url if hasattr(target, "display_avatar") else ""

        embed = discord.Embed(
            title=f"🔍 Log Search: {target.display_name}",
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=avatar_url)

        created_ts = int(target.created_at.timestamp())
        info_lines = [
            f"**User:** {target.mention} (`{target.name}` - `{target.id}`)",
            f"**Account Created:** <t:{created_ts}:R> (<t:{created_ts}:d>)"
        ]
        if is_member:
            if target.joined_at:
                joined_ts = int(target.joined_at.timestamp())
                info_lines.append(f"**Joined Server:** <t:{joined_ts}:R> (<t:{joined_ts}:d>)")
            if target.top_role and not target.top_role.is_default():
                info_lines.append(f"**Highest Role:** {target.top_role.mention}")
            if target.is_timed_out():
                to_ts = int(target.timed_out_until.timestamp())
                info_lines.append(f"⚠️ **Currently Timed Out Until:** <t:{to_ts}:R>")

        embed.description = "\n".join(info_lines)

        items = self.get_items_for_category()
        total_pages = self.get_total_pages()

        if self.current_category == "all":
            msg_count = len(self.data.get("messages", []))
            mod_count = len(self.data.get("mod", []))
            audit_count = len(self.data.get("audit", []))
            voice_count = len(self.data.get("voice", []))

            stats_val = (
                f"• 💬 **Deleted/Purged Messages:** `{msg_count}`\n"
                f"• 🛡️ **Moderation Actions/Punishments:** `{mod_count}`\n"
                f"• 📜 **Server Audit Log Events:** `{audit_count}`\n"
                f"• 🔊 **Voice Events:** `{voice_count}`"
            )
            embed.add_field(name="📊 Logs Recorded", value=stats_val, inline=False)

            timeline = self.data.get("timeline", [])
            if timeline:
                recent_lines = []
                for entry in timeline[:8]:
                    recent_lines.append(f"• <t:{int(entry['timestamp'])}:R> {entry['text']}")
                embed.add_field(name="🕒 Recent Activity Highlights", value="\n".join(recent_lines), inline=False)
            else:
                embed.add_field(name="🕒 Recent Activity Highlights", value="*No recorded logs or audit events found for this user.*", inline=False)

            embed.set_footer(text=f"User ID: {target.id} • Use the dropdown menu below to view specific categories")
            return embed

        elif self.current_category == "messages":
            if not items:
                embed.add_field(name="💬 Deleted Messages", value="*No deleted or purged messages recorded for this user.*", inline=False)
            else:
                start = self.current_page * self.page_size
                chunk = items[start:start + self.page_size]
                lines = []
                for r in chunk:
                    ts = int(r["timestamp"])
                    ch_mention = f"<#{r['channel_id']}>"
                    content = (r["content"] or "").strip()
                    if len(content) > 120:
                        content = content[:117] + "..."
                    media_tag = " 📎 *[Media Saved]*" if r.get("has_media") else ""
                    lines.append(f"• <t:{ts}:R> in {ch_mention}:{media_tag}\n> {content or '*[No text preview]*'}")
                embed.add_field(name=f"💬 Deleted & Purged Messages ({len(items)})", value="\n".join(lines), inline=False)

            embed.set_footer(text=f"User ID: {target.id} • Page {self.current_page + 1}/{total_pages}")
            return embed

        elif self.current_category == "mod":
            if not items:
                embed.add_field(name="🛡️ Moderation Actions", value="*No moderation events (kicks, bans, timeouts) recorded for this user.*", inline=False)
            else:
                start = self.current_page * self.page_size
                chunk = items[start:start + self.page_size]
                lines = []
                for r in chunk:
                    ts = int(r["timestamp"])
                    action = r["action"]
                    mod = r.get("moderator", "Unknown")
                    reason = r.get("reason") or "No reason specified"
                    lines.append(f"• <t:{ts}:R> **{action}** by {mod}\n> **Reason:** `{reason}`")
                embed.add_field(name=f"🛡️ Moderation History ({len(items)})", value="\n".join(lines), inline=False)

            embed.set_footer(text=f"User ID: {target.id} • Page {self.current_page + 1}/{total_pages}")
            return embed

        elif self.current_category == "audit":
            if not items:
                embed.add_field(name="📜 Server Audit Log", value="*No server audit actions recorded involving this user.*", inline=False)
            else:
                start = self.current_page * self.page_size
                chunk = items[start:start + self.page_size]
                lines = []
                for r in chunk:
                    ts = int(r["timestamp"])
                    action = r["action"]
                    actor = r.get("actor", "")
                    reason = f" - *{r['reason']}*" if r.get("reason") else ""
                    lines.append(f"• <t:{ts}:R> **{action}** {actor}{reason}")
                embed.add_field(name=f"📜 Audit Log Actions ({len(items)})", value="\n".join(lines), inline=False)

            embed.set_footer(text=f"User ID: {target.id} • Page {self.current_page + 1}/{total_pages}")
            return embed

        elif self.current_category == "voice":
            if not items:
                embed.add_field(name="🔊 Voice Events", value="*No voice events recorded for this user.*", inline=False)
            else:
                start = self.current_page * self.page_size
                chunk = items[start:start + self.page_size]
                lines = []
                for r in chunk:
                    ts = int(r["timestamp"])
                    action = r["action"]
                    actor = f" ({r['actor']})" if r.get("actor") else ""
                    reason = f" - *{r['reason']}*" if r.get("reason") else ""
                    lines.append(f"• <t:{ts}:R> **{action}**{actor}{reason}")
                embed.add_field(name=f"🔊 Voice Log Events ({len(items)})", value="\n".join(lines), inline=False)

            embed.set_footer(text=f"User ID: {target.id} • Page {self.current_page + 1}/{total_pages}")
            return embed

        return embed


# ==============================================================================
# 11.5 COG: AUTO RESPONDER SUITE
# ==============================================================================
class AutoResponder(commands.Cog, name="Auto Responder"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[int, List[Dict[str, Any]]] = {}
        self._cooldowns: Dict[tuple, float] = {}

    def _is_authorized(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id or ctx.author.id == RESTORE_MASTER_USER_ID:
            return True
        return ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator

    async def _get_guild_responders(self, guild_id: int) -> List[Dict[str, Any]]:
        if guild_id in self._cache:
            return self._cache[guild_id]
        responders = await db.get_auto_responders(guild_id)
        self._cache[guild_id] = responders
        return responders

    def _invalidate_cache(self, guild_id: int):
        self._cache.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not self.bot.is_feature_enabled(message.guild.id, "autoresponder"):
            return
        content = message.content
        if not content:
            return

        # Don't trigger if message starts with server command prefix
        guild_settings = await db.get_guild_settings(message.guild.id)
        prefix = guild_settings.get("prefix")
        if prefix and content.startswith(prefix):
            return
        elif not prefix and any(content.startswith(p) for p in ("&", "!", "/", "?")):
            return

        responders = await self._get_guild_responders(message.guild.id)
        if not responders:
            return

        now = time.time()
        for resp in responders:
            trigger = resp["trigger_text"]
            match_type = resp.get("match_type", "contains").lower()
            matched = False

            if match_type == "exact":
                if content.strip().lower() == trigger.lower():
                    matched = True
            else:
                if re.match(r"^\w+$", trigger):
                    pattern = r"(?i)\b" + re.escape(trigger) + r"\b"
                    if re.search(pattern, content):
                        matched = True
                else:
                    if trigger.lower() in content.lower():
                        matched = True

            if matched:
                cd_key = (message.channel.id, resp["id"])
                if now - self._cooldowns.get(cd_key, 0) < 2.0:
                    continue
                self._cooldowns[cd_key] = now

                reply_text = resp["response_text"]
                reply_text = reply_text.replace("{user}", message.author.mention)
                reply_text = reply_text.replace("{mention}", message.author.mention)
                reply_text = reply_text.replace("{username}", message.author.display_name)
                reply_text = reply_text.replace("{user_name}", message.author.display_name)
                reply_text = reply_text.replace("{server}", message.guild.name)
                reply_text = reply_text.replace("{channel}", message.channel.mention)

                try:
                    sent_msg = await message.channel.send(reply_text, delete_after=10)
                    if sent_msg and hasattr(self.bot, "ignored_message_ids"):
                        self.bot.ignored_message_ids.add(sent_msg.id)
                except Exception:
                    pass
                break

    @commands.hybrid_group(
        name="autoresponder",
        aliases=["ar", "responder", "autoresponse"],
        description="Set automatic responses for specific words or messages",
        fallback="list"
    )
    async def autoresponder_group(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        responders = await self._get_guild_responders(ctx.guild.id)
        if not responders:
            embed = discord.Embed(
                title="🤖 Auto-Responders",
                description=(
                    "No auto-responders are configured for this server yet!\n\n"
                    "**How to Add an Auto-Responder:**\n"
                    "• `/autoresponder add trigger: hello response: Welcome to the server!`\n"
                    "• `&ar add hello | Welcome {user} to {server}!`\n\n"
                    "**Available Placeholders:**\n"
                    "`{user}` - Mentions the user\n"
                    "`{username}` - Shows user display name\n"
                    "`{server}` - Server name\n"
                    "`{channel}` - Channel mention"
                ),
                color=COLOR_INFO
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title=f"🤖 Auto-Responders for {ctx.guild.name} ({len(responders)})",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc)
        )
        for r in responders[:25]:
            mtype = r.get("match_type", "contains").title()
            trig = r["trigger_text"]
            resp = r["response_text"]
            resp_preview = resp if len(resp) <= 80 else resp[:77] + "..."
            embed.add_field(
                name=f"#{r['id']} | Trigger: `{trig}` ({mtype})",
                value=f"↳ Response: {resp_preview}",
                inline=False
            )
        embed.set_footer(text=f"Total: {len(responders)} • Remove using /autoresponder remove <id>")
        await ctx.send(embed=embed)

    @autoresponder_group.command(name="add", aliases=["set", "create"], description="Add a new auto-response trigger and answer")
    @app_commands.describe(
        trigger="The word or phrase to trigger the response",
        response="The answer message the bot will send",
        match_type="Match type: 'contains' (word in message) or 'exact' (entire message matches)"
    )
    async def ar_add(
        self,
        ctx: commands.Context,
        trigger: str,
        response: Optional[str] = None,
        match_type: Optional[Literal["contains", "exact"]] = "contains"
    ):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to configure auto-responders."), ephemeral=True)

        actual_trigger = trigger
        actual_response = response
        actual_match_type = (match_type or "contains").lower()

        if actual_response is None and ctx.message and "|" in ctx.message.content:
            raw = ctx.message.content
            parts = raw.split(None, 2)
            if len(parts) >= 3:
                args_str = parts[2]
                if "|" in args_str:
                    t_part, r_part = args_str.split("|", 1)
                    actual_trigger = t_part.strip()
                    actual_response = r_part.strip()

        if not actual_response:
            return await ctx.send(embed=error_embed(
                "Missing Response",
                "Please provide both a trigger and a response!\n"
                "• **Slash Command:** `/autoresponder add trigger: hello response: hi there!`\n"
                "• **Prefix Command:** `&ar add hello | hi there!` or `&ar add \"hello\" \"hi there!\"`"
            ), ephemeral=True)

        if len(actual_trigger) > 200:
            return await ctx.send(embed=error_embed("Trigger Too Long", "Trigger text cannot exceed 200 characters."), ephemeral=True)
        if len(actual_response) > 1800:
            return await ctx.send(embed=error_embed("Response Too Long", "Response text cannot exceed 1800 characters."), ephemeral=True)

        responder_id = await db.add_auto_responder(
            guild_id=ctx.guild.id,
            trigger_text=actual_trigger,
            response_text=actual_response,
            match_type=actual_match_type,
            created_by=ctx.author.id
        )
        self._invalidate_cache(ctx.guild.id)

        embed = discord.Embed(
            title="🤖 Auto-Responder Created",
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Trigger Word / Phrase", value=f"`{actual_trigger}`", inline=True)
        embed.add_field(name="Match Mode", value=f"`{actual_match_type.title()}`", inline=True)
        embed.add_field(name="ID", value=f"`#{responder_id}`", inline=True)
        embed.add_field(name="Bot Response", value=actual_response[:1024], inline=False)
        embed.set_footer(text=f"Created by {ctx.author.display_name} • Placeholders: {{user}}, {{server}}, {{channel}}")
        await ctx.send(embed=embed)

    @autoresponder_group.command(name="remove", aliases=["delete", "del", "rem"], description="Remove an auto-response by ID or trigger word")
    @app_commands.describe(query="The ID number (e.g. 1) or trigger word to remove")
    async def ar_remove(self, ctx: commands.Context, query: str):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to configure auto-responders."), ephemeral=True)

        q = query.strip()
        deleted = False
        if q.isdigit() or (q.startswith("#") and q[1:].isdigit()):
            rid = int(q.lstrip("#"))
            deleted = await db.remove_auto_responder(ctx.guild.id, rid)
        else:
            count = await db.remove_auto_responder_by_trigger(ctx.guild.id, q)
            deleted = count > 0

        if deleted:
            self._invalidate_cache(ctx.guild.id)
            await ctx.send(embed=success_embed("Auto-Responder Removed", f"Successfully removed auto-responder matching `{q}`."))
        else:
            await ctx.send(embed=error_embed("Not Found", f"No auto-responder found matching `{q}`.\nUse `/autoresponder list` to see all IDs and triggers."), ephemeral=True)

    @autoresponder_group.command(name="show", aliases=["view"], description="Show all auto-responders (alias for list)")
    async def ar_show(self, ctx: commands.Context):
        await self.autoresponder_group(ctx)

    @autoresponder_group.command(name="clear", aliases=["reset"], description="Delete all auto-responses for this server")
    async def ar_clear(self, ctx: commands.Context):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to clear auto-responders."), ephemeral=True)

        count = await db.clear_auto_responders(ctx.guild.id)
        self._invalidate_cache(ctx.guild.id)
        await ctx.send(embed=success_embed("Auto-Responders Cleared", f"Successfully cleared all **{count}** auto-responder(s) for this server."))


# ==============================================================================
# 11.6 COG: MESSAGE RESTRICTION (AUTOMOD)
# ==============================================================================
class MessageRestrict(commands.Cog, name="Message Restriction (AutoMod)"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[int, List[Dict[str, Any]]] = {}
        self._staff_exempt_cache: Dict[int, bool] = {}
        self._last_role_delete_log: Dict[Tuple[int, int], float] = {}

    def _is_authorized(self, target: Union[commands.Context, discord.Interaction, discord.Member]) -> bool:
        guild = getattr(target, "guild", None)
        if not guild:
            return False
        user = getattr(target, "author", getattr(target, "user", target))
        if user.id == guild.owner_id or user.id == RESTORE_MASTER_USER_ID:
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms:
            return bool(perms.manage_guild or perms.administrator or perms.manage_messages)
        return False

    async def _get_guild_restrictions(self, guild_id: int) -> List[Dict[str, Any]]:
        if guild_id in self._cache:
            return self._cache[guild_id]
        restrictions = await db.get_restrictions(guild_id)
        self._cache[guild_id] = restrictions
        return restrictions

    def _invalidate_cache(self, guild_id: int):
        self._cache.pop(guild_id, None)
        self._staff_exempt_cache.pop(guild_id, None)

    async def _send_restrict_log(self, guild: discord.Guild, embed: discord.Embed):
        """Send a restriction audit log embed to the configured or auto-detected log channel."""
        try:
            ch_id = (
                await db.get_log_channel(guild.id, "restrict")
                or await db.get_log_channel(guild.id, "mod")
                or await db.get_log_channel(guild.id, "server")
                or await db.get_log_channel(guild.id, "message")
                or await db.get_log_channel(guild.id, "all")
            )
            dest = guild.get_channel(ch_id) if ch_id else None
            if not dest:
                for ch in guild.text_channels:
                    cname = ch.name.lower().replace("┃", "-").replace(" ", "-")
                    if any(target in cname for target in ("mod-log", "automod", "server-log", "audit-log")):
                        dest = ch
                        break
            if dest:
                await dest.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send restrict log: {e}")

    async def _is_exempt(self, member: discord.Member) -> bool:
        if member.bot:
            return True
        if not member.guild:
            return True
        gid = member.guild.id
        if gid not in self._staff_exempt_cache:
            self._staff_exempt_cache[gid] = await db.get_restrict_exempt_staff(gid)
        if not self._staff_exempt_cache[gid]:
            return False
        if member.id == member.guild.owner_id or member.id == RESTORE_MASTER_USER_ID:
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_messages:
            return True
        return False

    def evaluate_restriction(
        self,
        restrictions: List[Dict[str, Any]],
        content: str,
        attachments: Optional[List[Any]] = None,
        embeds: Optional[List[Any]] = None,
        channel_id: Optional[int] = None,
        author_role_ids: Optional[List[int]] = None
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        if not restrictions:
            return None

        raw_content = content or ""
        content_lower = raw_content.lower()

        urls_in_content = re.findall(r"(?:https?://|discord\.gg/)\S+", raw_content, flags=re.IGNORECASE)

        asset_urls: List[Tuple[str, str]] = []  # (raw_lower, norm)
        filenames: List[str] = []

        for u in urls_in_content:
            asset_urls.append((u.lower(), normalize_asset_str(u)))

        has_attachments = bool(attachments)
        has_media = False
        has_gif = False

        if attachments:
            for att in attachments:
                fname = getattr(att, "filename", "") or ""
                fname_lower = fname.lower()
                if fname_lower:
                    filenames.append(fname_lower)
                if fname_lower.endswith(".gif"):
                    has_gif = True
                if any(fname_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm", ".mkv")):
                    has_media = True

                att_url = getattr(att, "url", "") or ""
                if att_url:
                    asset_urls.append((att_url.lower(), normalize_asset_str(att_url)))
                    if ".gif" in att_url.lower():
                        has_gif = True
                proxy_url = getattr(att, "proxy_url", "") or ""
                if proxy_url:
                    asset_urls.append((proxy_url.lower(), normalize_asset_str(proxy_url)))
                    if ".gif" in proxy_url.lower():
                        has_gif = True

        if embeds:
            for em in embeds:
                em_type = getattr(em, "type", "")
                if em_type in ("gifv", "image", "video"):
                    has_media = True
                    if em_type == "gifv":
                        has_gif = True

                em_url = getattr(em, "url", None)
                if em_url:
                    asset_urls.append((em_url.lower(), normalize_asset_str(em_url)))
                    if "tenor.com" in em_url.lower() or "giphy.com" in em_url.lower() or ".gif" in em_url.lower():
                        has_gif = True

                for attr in ("image", "thumbnail", "video"):
                    obj = getattr(em, attr, None)
                    if obj:
                        has_media = True
                        u = getattr(obj, "url", None)
                        if u:
                            asset_urls.append((u.lower(), normalize_asset_str(u)))
                            if ".gif" in u.lower() or "tenor.com" in u.lower() or "giphy.com" in u.lower():
                                has_gif = True
                        pu = getattr(obj, "proxy_url", None)
                        if pu:
                            asset_urls.append((pu.lower(), normalize_asset_str(pu)))
                            if ".gif" in pu.lower():
                                has_gif = True

        if "tenor.com" in content_lower or "giphy.com" in content_lower or ".gif" in content_lower:
            has_gif = True
        if any(ext in content_lower for ext in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm")):
            has_media = True

        def _matches_asset(val_raw: str, val_norm: str) -> bool:
            if not val_raw:
                return False
            for r_u, n_u in asset_urls:
                if val_norm and (val_norm == n_u or val_norm in n_u or n_u in val_norm):
                    return True
                if val_raw in r_u or r_u in val_raw:
                    return True
            for fn in filenames:
                if val_raw == fn or val_raw in fn or fn in val_raw:
                    return True
                if val_norm and (val_norm.endswith("/" + fn) or fn in val_norm):
                    return True
            return False

        for rule in restrictions:
            ch_filter = rule.get("channel_id")
            if channel_id and ch_filter and ch_filter != channel_id:
                continue

            rtype = rule["restrict_type"].lower()
            raw_val = (rule.get("content") or "").strip()
            val_lower = raw_val.lower()
            norm_val = normalize_asset_str(raw_val) if raw_val else ""

            if rtype == "role":
                target_rid = None
                if raw_val.isdigit():
                    target_rid = int(raw_val)
                else:
                    m = re.match(r"^<@&(\d+)>$", raw_val)
                    if m:
                        target_rid = int(m.group(1))
                if target_rid and author_role_ids and target_rid in author_role_ids:
                    return (rule, f"restricted role <@&{target_rid}>")

            elif rtype == "media":
                if not raw_val or val_lower in ("all", "*"):
                    if has_media or has_attachments:
                        return (rule, "media / attachment")
                else:
                    if _matches_asset(val_lower, norm_val):
                        return (rule, f"restricted media (`{raw_val}`)")
                    if val_lower in content_lower:
                        return (rule, f"restricted media (`{raw_val}`)")

            elif rtype == "gif":
                if not raw_val or val_lower in ("all", "*"):
                    if has_gif:
                        return (rule, "GIF")
                else:
                    if _matches_asset(val_lower, norm_val):
                        return (rule, f"restricted GIF (`{raw_val}`)")
                    if norm_val and any(norm_val in n_u for _, n_u in asset_urls):
                        return (rule, f"restricted GIF (`{raw_val}`)")
                    if val_lower in content_lower:
                        return (rule, f"restricted GIF (`{raw_val}`)")

            elif rtype == "link":
                if not raw_val or val_lower in ("all", "*"):
                    if urls_in_content or any(r_u for r_u, _ in asset_urls):
                        return (rule, "link / URL")
                else:
                    if _matches_asset(val_lower, norm_val):
                        return (rule, f"restricted link (`{raw_val}`)")
                    if any(val_lower in r_u for r_u, _ in asset_urls):
                        return (rule, f"restricted link (`{raw_val}`)")
                    if val_lower in content_lower:
                        return (rule, f"restricted link (`{raw_val}`)")

            elif rtype in ("word", "msg", "message"):
                if raw_val:
                    if ("/" in raw_val or "." in raw_val) and _matches_asset(val_lower, norm_val):
                        return (rule, f"restricted word/link (`{raw_val}`)")
                    if re.match(r"^\w+$", val_lower):
                        if re.search(r"(?i)\b" + re.escape(raw_val) + r"\b", raw_content):
                            return (rule, f"restricted word (`{raw_val}`)")
                    else:
                        if val_lower in content_lower:
                            return (rule, f"restricted phrase (`{raw_val}`)")
                        if any(val_lower in fn for fn in filenames):
                            return (rule, f"restricted phrase (`{raw_val}`)")

        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or (await self._is_exempt(message.author)):
            return
        if not self.bot.is_feature_enabled(message.guild.id, "restrict"):
            return

        restrictions = await self._get_guild_restrictions(message.guild.id)
        if not restrictions:
            return

        author_roles = getattr(message.author, "roles", [])
        author_role_ids = [r.id for r in author_roles] if author_roles else []

        result = self.evaluate_restriction(
            restrictions=restrictions,
            content=message.content or "",
            attachments=message.attachments,
            embeds=message.embeds,
            channel_id=message.channel.id,
            author_role_ids=author_role_ids
        )
        if not result:
            return

        matched_restriction, matched_reason = result

        try:
            if hasattr(self.bot, "ignored_message_ids"):
                self.bot.ignored_message_ids.add(message.id)
            await message.delete()
        except Exception:
            pass

        # For role auto-deletion, silently delete without sending in-chat warning notifications
        if matched_restriction.get("restrict_type") != "role":
            try:
                warn_text = f"⚠️ {message.author.mention}, your message was deleted because it contained **{matched_reason}**."
                await message.channel.send(warn_text, delete_after=5)
            except Exception:
                pass

        content = message.content or ""
        preview = (content[:300] + "...") if len(content) > 300 else (content or "*[Media / Attachment]*")
        is_role_rule = matched_restriction.get("restrict_type") == "role"
        if is_role_rule:
            now_ts = time.time()
            user_key = (message.guild.id, message.author.id)
            if (now_ts - self._last_role_delete_log.get(user_key, 0.0)) < 3.0:
                # Silently drop duplicate log embeds if user spams messages rapidly
                return
            self._last_role_delete_log[user_key] = now_ts

        log_title = "🛡️ AutoMod: Restricted Role Message Deleted" if is_role_rule else "🛡️ AutoMod: Restricted Content Deleted"
        eb = log_embed(
            log_title,
            f"**User:** {message.author.mention} (`{message.author.name}` - `{message.author.id}`)\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Violated Rule:** Restricted **{matched_reason}** (Rule #{matched_restriction['id']})\n\n"
            f"**Deleted Message Preview:**\n{preview}",
            color=COLOR_ERROR,
            thumbnail=message.author.display_avatar.url,
            author_name=f"{message.author.display_name} ({message.author})",
            author_icon=message.author.display_avatar.url
        )
        eb.set_footer(text=f"User ID: {message.author.id} • Rule #{matched_restriction['id']}")
        await self._send_restrict_log(message.guild, eb)

    @commands.hybrid_group(
        name="restrict",
        aliases=["restrictmsg", "censor", "filter", "blacklistmsg"],
        description="Restrict and auto-delete messages containing specific words, links, media, or GIFs",
        fallback="list"
    )
    async def restrict_group(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        # Quick Restrict trigger: user replied to a message and typed &restrict
        if ctx.message and ctx.message.reference and ctx.message.reference.message_id:
            return await self.execute_quick_restrict(ctx, extra_arg=getattr(ctx, "subcommand_passed", None))

        restrictions = await self._get_guild_restrictions(ctx.guild.id)
        exempt_staff = await db.get_restrict_exempt_staff(ctx.guild.id)
        staff_str = "ON (Staff/Owner immune)" if exempt_staff else "OFF (Everyone filtered)"
        if not restrictions:
            embed = discord.Embed(
                title="🛡️ Message Restrictions (AutoMod)",
                description=(
                    "No message restrictions are active for this server!\n\n"
                    f"• **Staff Exemption:** `{staff_str}`\n\n"
                    "**How to Add Restrictions:**\n"
                    "• `&restrict add word <word>` - Auto-deletes messages containing the word\n"
                    "• `&restrict add link [domain/url]` - Auto-deletes links\n"
                    "• `&restrict add media [#channel]` - Auto-deletes images/attachments\n"
                    "• `&restrict add gif [url/#channel]` - Auto-deletes GIFs or specific GIF URL\n"
                    "• `&restrict add role @Role [#channel]` - Auto-deletes messages sent by this role\n"
                    "• `&autodeleterole @Role [#channel]` - Dedicated auto-delete role command\n"
                    "• Reply to message + `&restrict` - Quick-restrict replied message\n\n"
                    "**How to Clear / Remove Rules:**\n"
                    "• `&restrict clear <id>` or `&unrestrict <id>` - Clear a restriction rule by Rule ID\n"
                    "• `&unrestrict @Role` - Clear auto-delete rule for a role\n"
                    "• `&restrict clear all` - Clear all active restrictions\n\n"
                    "**Audit Logs & Configuration:**\n"
                    "• `&restrict log [#channel]` - Set or view the restriction audit log channel\n"
                    "• Auto-logs all rule adds, quick-restricts, rule removes, and auto-blocked messages!\n\n"
                    "**Testing & Staff Bypass:**\n"
                    "• `&restrict test <url/word>` - Test if content would be blocked\n"
                    "• `&restrict exempt_staff` - Toggle staff immunity ON/OFF (turn OFF to test live messages)\n\n"
                    "**Slash Commands:**\n"
                    "• `/restrict add type: [word/link/media/gif/role] content: [optional] channel: [optional]`\n"
                    "• `/autodeleterole role: @Role channel: [optional]`\n"
                    "• `/restrict clear rule: [id/all]`\n"
                    "• `/restrict log channel: [optional]`\n"
                    "• `/restrict test content: <url/word>`\n"
                    "• `/restrict exempt_staff enabled: [True/False]`"
                ),
                color=COLOR_INFO
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title=f"🛡️ Active Message Restrictions ({len(restrictions)})",
            description=f"Staff Exemption: **{staff_str}** *(Use `/restrict exempt_staff` to change)*\nUse `/restrict test <content>` to safely verify if a message will be blocked.",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc)
        )
        for r in restrictions[:25]:
            rtype = r["restrict_type"].upper()
            val = r.get("content") or "All"
            ch_str = f"in <#{r['channel_id']}>" if r.get("channel_id") else "Server-wide"
            if rtype == "ROLE":
                role_obj = ctx.guild.get_role(int(val)) if val.isdigit() else None
                role_str = role_obj.mention if role_obj else (f"<@&{val}>" if val.isdigit() else f"`{val}`")
                embed.add_field(
                    name=f"#{r['id']} | [ROLE] {role_str}",
                    value=f"• Scope: **{ch_str}**\n• Action: Auto-deletes all messages sent by this role",
                    inline=False
                )
            else:
                embed.add_field(
                    name=f"#{r['id']} | [{rtype}] `{val}`",
                    value=f"• Scope: **{ch_str}**",
                    inline=False
                )
        embed.set_footer(text=f"Total: {len(restrictions)} • Clear: &restrict clear <id> or &unrestrict <id>")
        await ctx.send(embed=embed)

    @restrict_group.command(name="add", aliases=["set", "create"], description="Add a new restriction (word, link, media, gif, role)")
    @app_commands.describe(
        type="Type of restriction: 'word', 'link', 'media', 'gif', or 'role'",
        content="The word, phrase, link domain, or role mention/ID to restrict (optional for media/gif/all links)",
        channel="Optional channel to apply this restriction to (defaults to server-wide)"
    )
    async def restrict_add(
        self,
        ctx: commands.Context,
        type: Literal["word", "link", "media", "gif", "role", "msg"],
        content: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None
    ):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to configure restrictions."), ephemeral=True)

        clean_type = "word" if type in ("word", "msg") else type.lower()
        clean_content = content.strip() if content else None
        ch_id = channel.id if channel else None

        target_role: Optional[discord.Role] = None
        if clean_type == "role":
            if not clean_content:
                return await ctx.send(embed=error_embed("Missing Role", "Please specify the role to auto-delete messages for (`&restrict add role @Role` or `&autodeleterole @Role`)."), ephemeral=True)

            m = re.match(r"^<@&?(\d+)>$", clean_content)
            if m:
                target_role = ctx.guild.get_role(int(m.group(1)))
            elif clean_content.isdigit():
                target_role = ctx.guild.get_role(int(clean_content))
            else:
                target_role = discord.utils.find(lambda r: r.name.lower() == clean_content.lower(), ctx.guild.roles)

            if not target_role:
                return await ctx.send(embed=error_embed("Invalid Role", f"Could not find a role matching `{clean_content}`. Please mention the role or use its ID/name."), ephemeral=True)

            clean_content = str(target_role.id)

        if clean_type == "word" and not clean_content:
            return await ctx.send(embed=error_embed("Missing Word", "Please specify the word or phrase to restrict (`/restrict add type: word content: <word>`)."), ephemeral=True)

        rid = await db.add_restriction(
            guild_id=ctx.guild.id,
            restrict_type=clean_type,
            content=clean_content,
            channel_id=ch_id,
            created_by=ctx.author.id
        )
        self._invalidate_cache(ctx.guild.id)

        target_desc = f"in {channel.mention}" if channel else "**Server-wide**"
        if clean_type == "role" and target_role:
            rule_desc = f"{target_role.mention} (`{target_role.name}` - ID: `{target_role.id}`)"
        else:
            rule_desc = f"`{clean_content}`" if clean_content else f"All {clean_type.upper()}s"

        embed = discord.Embed(
            title="🛡️ Role Auto-Delete Rule Added" if clean_type == "role" else "🛡️ Restriction Added",
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Rule ID", value=f"`#{rid}`", inline=True)
        embed.add_field(name="Type", value=f"`{clean_type.upper()}`", inline=True)
        embed.add_field(name="Target Scope", value=target_desc, inline=True)
        if clean_type == "role":
            embed.add_field(name="Restricted Role", value=rule_desc, inline=False)
            embed.add_field(name="Action", value="Auto-deletes all messages sent by members with this role as soon as they send them.", inline=False)
        else:
            embed.add_field(name="Restricted Content", value=rule_desc, inline=False)
        embed.set_footer(text=f"Added by {ctx.author.display_name} • Bot will auto-delete matching messages")
        await ctx.send(embed=embed)

        # Log event to audit channel
        log_eb = log_embed(
            "🛡️ Role Auto-Delete Rule Added" if clean_type == "role" else "🛡️ Restriction Rule Added",
            f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Rule ID:** `#{rid}`\n"
            f"**Type:** `{clean_type.upper()}`\n"
            + (f"**Restricted Role:** {target_role.mention} (`{target_role.name}` - `{target_role.id}`)\n" if clean_type == "role" and target_role else f"**Restricted Content:** {rule_desc}\n")
            + f"**Scope:** {target_desc}\n"
            + ("**Action:** Auto-deletes all messages sent by members with this role\n" if clean_type == "role" else ""),
            color=COLOR_SUCCESS,
            thumbnail=ctx.author.display_avatar.url,
            author_name=f"{ctx.author.display_name} ({ctx.author})",
            author_icon=ctx.author.display_avatar.url
        )
        log_eb.set_footer(text=f"Moderator ID: {ctx.author.id} • Rule #{rid}")
        await self._send_restrict_log(ctx.guild, log_eb)

    async def _execute_clear(self, ctx: commands.Context, rule: Optional[str] = None):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Messages, Manage Server, or Administrator permission to clear restrictions."), ephemeral=True)

        if not rule:
            count = await db.clear_restrictions(ctx.guild.id)
            self._invalidate_cache(ctx.guild.id)
            embed = success_embed(
                "Restrictions Cleared",
                f"Successfully cleared all **{count}** restriction(s) for this server.\n\n"
                f"💡 *Tip: To clear an individual rule by ID next time, use `{ctx.prefix}restrict clear <id>` or `{ctx.prefix}unrestrict <id>`.*"
            )
            log_eb = log_embed(
                "🧹 All Restriction Rules Cleared",
                f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                f"**Action:** All restrictions cleared for server\n"
                f"**Total Rules Cleared:** `{count}`",
                color=COLOR_ERROR,
                thumbnail=ctx.author.display_avatar.url,
                author_name=f"{ctx.author.display_name} ({ctx.author})",
                author_icon=ctx.author.display_avatar.url
            )
            log_eb.set_footer(text=f"Moderator ID: {ctx.author.id} • Action: Clear All")
            await self._send_restrict_log(ctx.guild, log_eb)
            return await ctx.send(embed=embed)

        clean_rule = rule.strip()
        if clean_rule.lower() in ("all", "*", "everything"):
            count = await db.clear_restrictions(ctx.guild.id)
            self._invalidate_cache(ctx.guild.id)
            log_eb = log_embed(
                "🧹 All Restriction Rules Cleared",
                f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                f"**Action:** All restrictions cleared for server\n"
                f"**Total Rules Cleared:** `{count}`",
                color=COLOR_ERROR,
                thumbnail=ctx.author.display_avatar.url,
                author_name=f"{ctx.author.display_name} ({ctx.author})",
                author_icon=ctx.author.display_avatar.url
            )
            log_eb.set_footer(text=f"Moderator ID: {ctx.author.id} • Action: Clear All")
            await self._send_restrict_log(ctx.guild, log_eb)
            return await ctx.send(embed=success_embed("Restrictions Cleared", f"Successfully cleared all **{count}** restriction(s) for this server."))

        # Check if argument contains rule ID(s) (e.g. "5", "#5", "1, 2", "1 2 3")
        tokens = [t.strip().lstrip("#") for t in re.split(r"[\s,]+", clean_rule) if t.strip()]
        if tokens and all(t.isdigit() for t in tokens):
            existing = await db.get_restrictions(ctx.guild.id)
            rule_lookup = {r["id"]: r for r in existing}
            ids = [int(t) for t in tokens]
            has_matching_rule_id = any(rid in rule_lookup for rid in ids)
            if has_matching_rule_id:
                removed = []
                not_found = []
                for rid in ids:
                    if await db.remove_restriction(ctx.guild.id, rid):
                        removed.append(rid)
                    else:
                        not_found.append(rid)

                if removed:
                    self._invalidate_cache(ctx.guild.id)
                    rem_str = ", ".join(f"`#{r}`" for r in removed)
                    msg = f"Successfully cleared restriction rule(s): {rem_str}."
                    if not_found:
                        nf_str = ", ".join(f"`#{nf}`" for nf in not_found)
                        msg += f"\n*(Rule ID(s) not found: {nf_str})*"
                    embed = success_embed("Restriction Cleared", msg)
                    embed.set_footer(text=f"Use {ctx.prefix}restrict list to view active restrictions")

                    # Send audit log embed
                    details_lines = []
                    for rid in removed:
                        rinfo = rule_lookup.get(rid)
                        if rinfo:
                            rtype = rinfo.get("restrict_type", "rule").upper()
                            rval = rinfo.get("content") or f"All {rtype}s"
                            rch = f"in <#{rinfo['channel_id']}>" if rinfo.get("channel_id") else "Server-wide"
                            if rtype == "ROLE" and rval.isdigit():
                                r_role = ctx.guild.get_role(int(rval))
                                rval_str = f"{r_role.mention} ({r_role.name})" if r_role else f"<@&{rval}>"
                                details_lines.append(f"• Rule `#{rid}`: [ROLE] {rval_str} ({rch})")
                            else:
                                details_lines.append(f"• Rule `#{rid}`: [{rtype}] `{rval}` ({rch})")
                        else:
                            details_lines.append(f"• Rule `#{rid}`")

                    log_eb = log_embed(
                        "🗑️ Restriction Rule Removed",
                        f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                        f"**Removed Rules:** {rem_str}\n\n"
                        f"**Rule Details:**\n" + "\n".join(details_lines),
                        color=COLOR_WARNING,
                        thumbnail=ctx.author.display_avatar.url,
                        author_name=f"{ctx.author.display_name} ({ctx.author})",
                        author_icon=ctx.author.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Moderator ID: {ctx.author.id} • Removed: {rem_str}")
                    await self._send_restrict_log(ctx.guild, log_eb)

                    return await ctx.send(embed=embed)
                else:
                    nf_str = ", ".join(f"`#{nf}`" for nf in not_found)
                    return await ctx.send(
                        embed=error_embed("Not Found", f"No active restriction found with Rule ID: {nf_str}.\nUse `{ctx.prefix}restrict list` to view active rule IDs."),
                        ephemeral=True
                    )

        # Otherwise treat as query (content, word, url, or type)
        count = await db.remove_restriction_by_query(ctx.guild.id, clean_rule)
        if count > 0:
            self._invalidate_cache(ctx.guild.id)
            embed = success_embed("Restriction Cleared", f"Successfully removed **{count}** restriction(s) matching `{clean_rule}`.")
            embed.set_footer(text=f"Use {ctx.prefix}restrict list to view active restrictions")

            log_eb = log_embed(
                "🗑️ Restriction Rule(s) Removed",
                f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                f"**Query Match:** `{clean_rule}`\n"
                f"**Total Removed:** `{count}` rule(s)",
                color=COLOR_WARNING,
                thumbnail=ctx.author.display_avatar.url,
                author_name=f"{ctx.author.display_name} ({ctx.author})",
                author_icon=ctx.author.display_avatar.url
            )
            log_eb.set_footer(text=f"Moderator ID: {ctx.author.id}")
            await self._send_restrict_log(ctx.guild, log_eb)

            return await ctx.send(embed=embed)
        else:
            return await ctx.send(
                embed=error_embed("Not Found", f"No restriction found matching `{clean_rule}`.\nUse `{ctx.prefix}restrict list` to view active restrictions and Rule IDs."),
                ephemeral=True
            )

    @restrict_group.command(name="remove", aliases=["delete", "del", "rem"], description="Remove a restriction by Rule ID or query")
    @app_commands.describe(query="The Rule ID (e.g. 1 or #1) or restricted word/type to remove")
    async def restrict_remove(self, ctx: commands.Context, *, query: str):
        await self._execute_clear(ctx, rule=query)

    @restrict_group.command(name="show", aliases=["view"], description="Show all active restrictions")
    async def restrict_show(self, ctx: commands.Context):
        await self.restrict_group(ctx)

    @restrict_group.command(name="clear", aliases=["reset"], description="Clear a restriction by Rule ID or clear all")
    @app_commands.describe(rule="The Rule ID (e.g. 1 or #1) to clear, multiple IDs, or 'all' to clear everything")
    async def restrict_clear(self, ctx: commands.Context, *, rule: Optional[str] = None):
        await self._execute_clear(ctx, rule=rule)

    @restrict_group.command(name="log", aliases=["logs", "logchannel"], description="Set or view the log channel for restriction add/remove/block events")
    @app_commands.describe(channel="Channel where restriction logs will be sent (leave empty to view current)")
    async def restrict_log(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Messages, Manage Server, or Administrator permission to configure logging."), ephemeral=True)

        if channel:
            await db.set_log_channel(ctx.guild.id, "restrict", channel.id)
            embed = success_embed(
                "Restrict Log Channel Set",
                f"Restriction events (adds, removes, and auto-blocked content) will now be logged to {channel.mention}."
            )
            return await ctx.send(embed=embed)

        spec_ch_id = await db.get_log_channel(ctx.guild.id, "restrict")
        mod_ch_id = await db.get_log_channel(ctx.guild.id, "mod")
        serv_ch_id = await db.get_log_channel(ctx.guild.id, "server")

        spec_ch = ctx.guild.get_channel(spec_ch_id) if spec_ch_id else None
        mod_ch = ctx.guild.get_channel(mod_ch_id) if mod_ch_id else None
        serv_ch = ctx.guild.get_channel(serv_ch_id) if serv_ch_id else None

        active_target = spec_ch or mod_ch or serv_ch
        active_str = active_target.mention if active_target else "*Auto-detecting standard log channels (#mod-logs / #server-logs)*"

        embed = discord.Embed(
            title="🛡️ Restriction Logging Configuration",
            description=(
                f"• **Active Destination:** {active_str}\n"
                f"• **Dedicated Restrict Log:** {spec_ch.mention if spec_ch else '*Not set (using fallback)*'}\n"
                f"• **Mod Log Fallback:** {mod_ch.mention if mod_ch else '*Not set*'}\n\n"
                f"**How to Change:**\n"
                f"• `{ctx.prefix}restrict log #channel` — Set dedicated channel for restriction logs\n"
                f"• `{ctx.prefix}logs setup` — Automatically create private `#mod-logs` and audit channels"
            ),
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @restrict_group.command(name="exempt_staff", aliases=["staff_exempt", "staffexempt", "toggle_staff"], description="Toggle whether staff and server owner are immune to restrictions")
    @app_commands.describe(
        enabled="True = Staff/Owner immune (default). False = Restrictions apply to everyone including Staff/Owner."
    )
    async def restrict_exempt_staff(self, ctx: commands.Context, enabled: Optional[bool] = None):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to configure restrictions."), ephemeral=True)

        current = await db.get_restrict_exempt_staff(ctx.guild.id)
        new_val = (not current) if enabled is None else enabled

        await db.set_restrict_exempt_staff(ctx.guild.id, new_val)
        self._staff_exempt_cache[ctx.guild.id] = new_val

        status_text = "ENABLED (Staff & Owner are **EXEMPT** from filters)" if new_val else "DISABLED (Filters apply to **EVERYONE**, including Staff & Owner)"
        tip_text = (
            "*(Staff and Server Owner will NOT have their messages deleted by restrictions.)*"
            if new_val else
            "*(Staff and Server Owner will also have their messages filtered/deleted. Perfect for testing!)*"
        )
        color = COLOR_SUCCESS if new_val else COLOR_WARNING
        embed = discord.Embed(
            title="🛡️ Staff Exemption Setting",
            description=f"Staff exemption is now **{status_text}**.\n\n{tip_text}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Updated by {ctx.author.display_name} • Use /restrict test to safely test rules")
        await ctx.send(embed=embed)

    @restrict_group.command(name="test", aliases=["check"], description="Test whether a message, word, link, or GIF URL would be blocked by active rules")
    @app_commands.describe(
        content="The message text, word, link, or media/GIF URL to test"
    )
    async def restrict_test(self, ctx: commands.Context, content: str):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to test restrictions."), ephemeral=True)

        restrictions = await self._get_guild_restrictions(ctx.guild.id)
        if not restrictions:
            embed = discord.Embed(
                title="🛡️ Restriction Test: 🟢 ALLOWED",
                description=f"No active restrictions found for this server. The message would **not** be deleted.\n\n**Tested Content:**\n`{content[:500]}`",
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            return await ctx.send(embed=embed)

        author_roles = getattr(ctx.author, "roles", [])
        author_role_ids = [r.id for r in author_roles] if author_roles else []
        is_user_exempt = await self._is_exempt(ctx.author)
        result = self.evaluate_restriction(
            restrictions=restrictions,
            content=content,
            attachments=None,
            embeds=None,
            channel_id=ctx.channel.id,
            author_role_ids=author_role_ids
        )

        if result:
            rule, reason = result
            rid = rule["id"]
            rtype = rule["restrict_type"].upper()
            rval = rule.get("content") or "All"
            if rtype == "ROLE" and rval.isdigit():
                r_role = ctx.guild.get_role(int(rval))
                rule_display = f"Role {r_role.mention} ({r_role.name})" if r_role else f"Role <@&{rval}>"
            else:
                rule_display = f"[{rtype}] {rval}"
            exempt_note = ""
            if is_user_exempt:
                exempt_note = (
                    "\n\n⚠️ **Staff Exemption Alert:**\n"
                    "Because you are Server Owner/Staff, your live messages are currently **EXEMPT**.\n"
                    "If you post this in chat right now, the bot will **NOT** delete it.\n"
                    "To have the bot enforce rules on staff/owner as well, run `/restrict exempt_staff enabled: False`."
                )

            embed = discord.Embed(
                title="🛡️ Restriction Test: 🔴 WOULD BE BLOCKED",
                description=(
                    f"A live message with this content will be **AUTOMATICALLY DELETED**!\n\n"
                    f"• **Violated Rule:** Rule #{rid} (`{rule_display}`)\n"
                    f"• **Reason:** **{reason}**\n"
                    f"• **Tested Channel:** {ctx.channel.mention}"
                    f"{exempt_note}"
                ),
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Tested Content", value=f"`{content[:500]}`", inline=False)
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="🛡️ Restriction Test: 🟢 ALLOWED",
                description=(
                    f"This content does **not** violate any of the {len(restrictions)} active restriction rule(s).\n"
                    f"It would **not** be deleted."
                ),
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Tested Content", value=f"`{content[:500]}`", inline=False)
            await ctx.send(embed=embed)

    async def execute_quick_restrict(self, ctx: commands.Context, extra_arg: Optional[str] = None):
        """
        Quick Restrict: Auto-identifies content type from a replied message (GIF, Media, Link, Word),
        adds a restriction rule to block it in the future, and deletes the offending message.
        """
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), delete_after=10)

        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Messages, Manage Server, or Administrator permission to restrict content."), delete_after=10)

        ref = ctx.message.reference
        if not ref or not ref.message_id:
            return await ctx.send(embed=error_embed("No Message Replied", "Please **reply** to the message you want to restrict and type `&restrict`!"), delete_after=10)

        target_msg = ref.cached_message
        if not target_msg:
            try:
                target_msg = await ctx.channel.fetch_message(ref.message_id)
            except Exception as e:
                return await ctx.send(embed=error_embed("Message Not Found", f"Could not fetch the referenced message: {e}"), delete_after=10)

        content = (target_msg.content or "").strip()
        attachments = target_msg.attachments or []
        embeds = target_msg.embeds or []

        identified_type = None
        restricted_val = None

        # Check channel scope if user specified "here" or "channel"
        scope_channel_id = None
        if extra_arg:
            clean_arg = str(extra_arg).strip().lower()
            if clean_arg in ("here", "channel", "this"):
                scope_channel_id = ctx.channel.id

        # 1. Check for GIF (attachments, embeds, links)
        gif_url = None
        for att in attachments:
            fname = (getattr(att, "filename", "") or "").lower()
            u = (getattr(att, "url", "") or "").lower()
            if fname.endswith(".gif") or ".gif" in u:
                gif_url = getattr(att, "url", None)
                break
        if not gif_url:
            for em in embeds:
                if getattr(em, "type", "") == "gifv":
                    gif_url = getattr(em, "url", None)
                    break
                for attr in ("image", "thumbnail", "video"):
                    obj = getattr(em, attr, None)
                    if obj and getattr(obj, "url", None):
                        u = obj.url.lower()
                        if ".gif" in u or "tenor.com" in u or "giphy.com" in u:
                            gif_url = obj.url
                            break
        if not gif_url and content:
            for u in re.findall(r"(?:https?://)\S+", content, re.IGNORECASE):
                u_lower = u.lower()
                if ".gif" in u_lower or "tenor.com" in u_lower or "giphy.com" in u_lower:
                    gif_url = u
                    break

        if gif_url:
            identified_type = "gif"
            restricted_val = gif_url

        # 2. Check for other Media (attachments, image/video embeds)
        if not identified_type:
            media_item = None
            for att in attachments:
                fname = (getattr(att, "filename", "") or "").lower()
                if any(fname.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".mp3", ".ogg")):
                    media_item = getattr(att, "url", None) or getattr(att, "filename", None)
                    break
            if not media_item:
                for em in embeds:
                    if getattr(em, "type", "") in ("image", "video"):
                        media_item = getattr(em, "url", None)
                        break
                    for attr in ("image", "thumbnail", "video"):
                        obj = getattr(em, attr, None)
                        if obj and getattr(obj, "url", None):
                            media_item = obj.url
                            break
            if media_item:
                identified_type = "media"
                restricted_val = media_item

        # 3. Check for Link / URL
        if not identified_type and content:
            urls = re.findall(r"(?:https?://|discord\.gg/)\S+", content, re.IGNORECASE)
            if urls:
                identified_type = "link"
                restricted_val = urls[0]

        # 4. Check for Text / Word / Phrase
        if not identified_type and content:
            identified_type = "word"
            restricted_val = content

        # 5. Fallback check for any attachment
        if not identified_type and attachments:
            identified_type = "media"
            att0 = attachments[0]
            restricted_val = getattr(att0, "url", None) or getattr(att0, "filename", "attachment")

        if not identified_type or not restricted_val:
            return await ctx.send(embed=error_embed("Cannot Identify", "Could not identify any text, link, media, or GIF in the referenced message to restrict."), delete_after=10)

        # Add restriction to database
        rid = await db.add_restriction(
            guild_id=ctx.guild.id,
            restrict_type=identified_type,
            content=restricted_val,
            channel_id=scope_channel_id,
            created_by=ctx.author.id
        )
        self._invalidate_cache(ctx.guild.id)

        # Delete offending message
        deleted_offending = False
        try:
            if hasattr(self.bot, "ignored_message_ids"):
                self.bot.ignored_message_ids.add(target_msg.id)
            await target_msg.delete()
            deleted_offending = True
        except Exception:
            pass

        # Delete command invocation message
        try:
            await ctx.message.delete()
        except Exception:
            pass

        # Send confirmation embed (auto-deleting after 10s)
        scope_str = f"in {ctx.channel.mention}" if scope_channel_id else "Server-wide"
        status_lines = [
            f"• **Auto-Identified Type:** `{identified_type.upper()}`",
            f"• **Blocked Item:** `{restricted_val[:160]}`",
            f"• **Scope:** **{scope_str}**",
            f"• **Rule ID:** `#{rid}`",
            f"• **Author of Message:** {target_msg.author.mention} (`{target_msg.author.id}`)",
            f"• **Offending Message:** {'🗑️ Deleted' if deleted_offending else '⚠️ Kept (missing perms)'}"
        ]

        embed = discord.Embed(
            title="⚡ Quick Restrict Activated",
            description="\n".join(status_lines) + "\n\n💡 *Matching messages sent in the future will be automatically deleted.*",
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Blocked by {ctx.author.display_name} • Auto-deletes in 10s • Remove with &restrict remove #{rid}")

        await ctx.channel.send(embed=embed, delete_after=10)

        # Log event to configured mod/restrict log channel
        log_eb = log_embed(
            "⚡ Quick Restrict Rule Added",
            f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Channel:** {ctx.channel.mention}\n"
            f"**Auto-Identified Type:** `{identified_type.upper()}` (Rule `#{rid}`)\n"
            f"**Target User:** {target_msg.author.mention} (`{target_msg.author.id}`)\n"
            f"**Blocked Content:** `{restricted_val[:250]}`",
            color=COLOR_WARNING,
            thumbnail=target_msg.author.display_avatar.url,
            author_name=f"{ctx.author.display_name} (Quick Restrict)",
            author_icon=ctx.author.display_avatar.url
        )
        log_eb.set_footer(text=f"Moderator ID: {ctx.author.id} • Rule #{rid}")
        await self._send_restrict_log(ctx.guild, log_eb)

    @commands.command(
        name="quickrestrict",
        aliases=["qrestrict"],
        help="Quickly restrict and delete a message by replying to it (Prefix only)"
    )
    async def quick_restrict_cmd(self, ctx: commands.Context, *, scope: Optional[str] = None):
        """Prefix-only command to quick-restrict a replied message."""
        await self.execute_quick_restrict(ctx, extra_arg=scope)

    @commands.command(
        name="unrestrict",
        aliases=["clearrestrict", "clear_restrict", "delrestrict", "removerestrict"],
        help="Clear a restriction rule by ID or clear all rules (Prefix only)"
    )
    async def unrestrict_cmd(self, ctx: commands.Context, *, rule: Optional[str] = None):
        """Prefix-only command to easily clear a restriction rule by ID (e.g. &unrestrict 1 or &clearrestrict 1)"""
        if not rule:
            return await ctx.send(
                embed=error_embed(
                    "Missing Rule ID",
                    f"Please specify the Rule ID to clear!\n\n"
                    f"**Usage:**\n"
                    f"• `{ctx.prefix}unrestrict <rule_id>` (e.g. `{ctx.prefix}unrestrict 1`)\n"
                    f"• `{ctx.prefix}clearrestrict <rule_id>`\n"
                    f"• `{ctx.prefix}restrict clear <rule_id>`\n"
                    f"• `{ctx.prefix}restrict clear all`\n\n"
                    f"Type `{ctx.prefix}restrict list` to see active rule IDs."
                ),
                delete_after=15
            )
        await self._execute_clear(ctx, rule=rule)

    @commands.command(
        name="restrictlog",
        aliases=["restrictlogs", "autorestrictlog"],
        help="Set or view the log channel for restriction add and remove events (Prefix only)"
    )
    async def restrict_log_cmd(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Prefix command to set or view restriction logging channel."""
        await self.restrict_log(ctx, channel=channel)

    def _parse_autodeleterole_prefix_query(self, guild: discord.Guild, text: Optional[str]) -> Dict[str, Any]:
        if not text or not text.strip():
            return {"action": "list"}
        raw = text.strip()
        low = raw.lower()
        if low in ("list", "view", "show"):
            return {"action": "list"}
        if low in ("clear", "clear all", "clearall", "reset"):
            return {"action": "clear_all"}

        action: Literal["add", "remove", "list", "clear_all"] = "add"
        if re.match(r"(?i)^(remove|delete|del)\s+", raw):
            action = "remove"
            raw = re.sub(r"(?i)^(remove|delete|del)\s+", "", raw).strip()
        elif re.match(r"(?i)^(add|set|create)\s+", raw):
            action = "add"
            raw = re.sub(r"(?i)^(add|set|create)\s+", "", raw).strip()

        if action == "remove" and raw.isdigit():
            return {"action": "remove", "rule_id": int(raw)}

        # Check for channel mention <#123> at the end
        channel_obj = None
        ch_match = re.search(r"<#(\d+)>$", raw)
        if ch_match:
            channel_obj = guild.get_channel(int(ch_match.group(1)))
            raw = raw[:ch_match.start()].strip()

        # Find role by mention, ID, or name
        role_obj = None
        role_match = re.search(r"<@&?(\d+)>", raw)
        if role_match:
            role_obj = guild.get_role(int(role_match.group(1)))
        elif raw.isdigit():
            role_obj = guild.get_role(int(raw))
        else:
            role_obj = discord.utils.find(lambda r: r.name.lower() == raw.lower(), guild.roles)

        return {
            "action": action,
            "role": role_obj,
            "channel": channel_obj,
            "rule_id": int(raw) if (action == "remove" and raw.isdigit() and not role_obj) else None
        }

    async def _execute_autodeleterole(
        self,
        ctx_or_interaction: Union[commands.Context, discord.Interaction],
        action: Literal["add", "remove", "list", "clear_all"] = "add",
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        rule_id: Optional[int] = None
    ):
        is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
        guild = ctx_or_interaction.guild
        user = ctx_or_interaction.user if is_interaction else ctx_or_interaction.author

        async def send_reply(embed: discord.Embed, ephemeral: bool = False):
            if is_interaction:
                try:
                    if ctx_or_interaction.response.is_done():
                        await ctx_or_interaction.followup.send(embed=embed, ephemeral=ephemeral)
                    else:
                        await ctx_or_interaction.response.send_message(embed=embed, ephemeral=ephemeral)
                except Exception as e:
                    logger.debug(f"Error responding to interaction in autodeleterole: {e}")
            else:
                await ctx_or_interaction.send(embed=embed)

        if not guild:
            return await send_reply(error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not self._is_authorized(ctx_or_interaction):
            return await send_reply(error_embed("Permission Denied", "You need Manage Messages, Manage Server, or Administrator permission to configure role auto-delete."), ephemeral=True)

        prefix = "/" if is_interaction else getattr(ctx_or_interaction, "prefix", "&")

        # If action is "add" but neither role nor rule_id was given, treat as "list"
        if action == "add" and not role and not rule_id:
            action = "list"

        # -------------------------------------------------------------
        # 1. ACTION: LIST
        # -------------------------------------------------------------
        if action == "list":
            restrictions = await self._get_guild_restrictions(guild.id)
            role_restrictions = [r for r in restrictions if r.get("restrict_type") == "role"]
            if not role_restrictions:
                return await send_reply(
                    embed=discord.Embed(
                        title="🛡️ Auto-Delete Role Rules (0 Active)",
                        description=(
                            "No roles are currently configured for auto-deletion!\n\n"
                            "**How to Auto-Delete Messages from a Role:**\n"
                            f"• `{prefix}autodeleterole role: @Role` — Auto-deletes all messages sent by this role server-wide\n"
                            f"• `{prefix}autodeleterole role: @Role channel: #channel` — Auto-deletes in `#channel`\n\n"
                            "**How to Remove / Clear:**\n"
                            f"• `{prefix}autodeleterole action: remove role: @Role` — Remove auto-delete for a role\n"
                            f"• `{prefix}autodeleterole action: remove rule_id: <id>` — Remove by Rule ID\n"
                            f"• `{prefix}autodeleterole action: clear_all` — Clear all active rules"
                        ),
                        color=COLOR_INFO
                    )
                )

            embed = discord.Embed(
                title=f"🛡️ Active Role Auto-Delete Rules ({len(role_restrictions)})",
                description="Messages sent by members with these roles are automatically deleted upon sending:",
                color=COLOR_PRIMARY,
                timestamp=datetime.now(timezone.utc)
            )
            for r in role_restrictions:
                val = r.get("content") or ""
                r_obj = guild.get_role(int(val)) if val.isdigit() else None
                r_str = r_obj.mention if r_obj else (f"<@&{val}>" if val.isdigit() else f"`{val}`")
                ch_str = f"in <#{r['channel_id']}>" if r.get("channel_id") else "Server-wide"
                embed.add_field(
                    name=f"Rule #{r['id']} | {r_str}",
                    value=f"• Scope: **{ch_str}**\n• Action: 🛡️ Silent Auto-Delete on Send",
                    inline=False
                )
            embed.set_footer(text=f"Total: {len(role_restrictions)} • Remove: {prefix}autodeleterole action: remove role: @Role")
            return await send_reply(embed=embed)

        # -------------------------------------------------------------
        # 2. ACTION: CLEAR ALL
        # -------------------------------------------------------------
        elif action == "clear_all":
            async with aiosqlite.connect(db.db_path) as conn:
                cursor = await conn.execute(
                    "DELETE FROM restricted_content WHERE guild_id = ? AND restrict_type = 'role'",
                    (guild.id,)
                )
                count = cursor.rowcount
                await conn.commit()
            self._invalidate_cache(guild.id)

            if count > 0:
                log_eb = log_embed(
                    "🧹 All Role Auto-Delete Rules Cleared",
                    f"**Moderator:** {user.mention} (`{user.id}`)\n"
                    f"**Action:** All role auto-delete rules cleared\n"
                    f"**Total Removed:** `{count}` rule(s)",
                    color=COLOR_ERROR,
                    thumbnail=user.display_avatar.url,
                    author_name=f"{user.display_name} ({user})",
                    author_icon=user.display_avatar.url
                )
                log_eb.set_footer(text=f"Moderator ID: {user.id}")
                await self._send_restrict_log(guild, log_eb)

            return await send_reply(
                embed=success_embed(
                    "Role Auto-Delete Cleared",
                    f"Successfully cleared all **{count}** role auto-delete restriction(s)."
                )
            )

        # -------------------------------------------------------------
        # 3. ACTION: REMOVE
        # -------------------------------------------------------------
        elif action == "remove":
            if rule_id is not None:
                existing = await db.get_restrictions(guild.id)
                rule_lookup = {r["id"]: r for r in existing}
                removed = await db.remove_restriction(guild.id, rule_id)
                if removed:
                    self._invalidate_cache(guild.id)
                    rinfo = rule_lookup.get(rule_id)
                    rval = rinfo.get("content") if rinfo else str(rule_id)
                    r_role = guild.get_role(int(rval)) if (rinfo and rval and rval.isdigit()) else None
                    role_name_str = f"role {r_role.mention}" if r_role else f"Rule #{rule_id}"

                    log_eb = log_embed(
                        "🗑️ Role Auto-Delete Rule Removed",
                        f"**Moderator:** {user.mention} (`{user.id}`)\n"
                        f"**Removed Rule:** Rule `#{rule_id}`\n"
                        f"**Target:** {role_name_str}",
                        color=COLOR_WARNING,
                        thumbnail=user.display_avatar.url,
                        author_name=f"{user.display_name} ({user})",
                        author_icon=user.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Moderator ID: {user.id} • Rule #{rule_id}")
                    await self._send_restrict_log(guild, log_eb)

                    return await send_reply(
                        embed=success_embed(
                            "Rule Removed",
                            f"Successfully removed role auto-delete Rule `#{rule_id}` ({role_name_str})."
                        )
                    )
                else:
                    return await send_reply(
                        embed=error_embed(
                            "Rule Not Found",
                            f"No active restriction rule found with ID `#{rule_id}`. Use `{prefix}autodeleterole action: list` to view active IDs."
                        ),
                        ephemeral=True
                    )

            elif role is not None:
                count = await db.remove_restriction_by_query(guild.id, str(role.id))
                if count > 0:
                    self._invalidate_cache(guild.id)

                    log_eb = log_embed(
                        "🗑️ Role Auto-Delete Rule Removed",
                        f"**Moderator:** {user.mention} (`{user.id}`)\n"
                        f"**Role:** {role.mention} (`{role.name}` - `{role.id}`)\n"
                        f"**Total Removed:** `{count}` rule(s)",
                        color=COLOR_WARNING,
                        thumbnail=user.display_avatar.url,
                        author_name=f"{user.display_name} ({user})",
                        author_icon=user.display_avatar.url
                    )
                    log_eb.set_footer(text=f"Moderator ID: {user.id}")
                    await self._send_restrict_log(guild, log_eb)

                    return await send_reply(
                        embed=success_embed(
                            "Role Auto-Delete Removed",
                            f"Successfully removed auto-delete rule(s) for {role.mention} (`{role.name}`)."
                        )
                    )
                else:
                    return await send_reply(
                        embed=error_embed(
                            "Not Found",
                            f"No active auto-delete rule was found for {role.mention}. Use `{prefix}autodeleterole action: list` to view active rules."
                        ),
                        ephemeral=True
                    )
            else:
                return await send_reply(
                    embed=error_embed(
                        "Missing Parameter",
                        f"Please specify the role or rule ID to remove!\n• `{prefix}autodeleterole action: remove role: @Role`\n• `{prefix}autodeleterole action: remove rule_id: <id>`"
                    ),
                    ephemeral=True
                )

        # -------------------------------------------------------------
        # 4. ACTION: ADD
        # -------------------------------------------------------------
        elif action == "add":
            if not role:
                return await send_reply(
                    embed=error_embed(
                        "Missing Role",
                        f"Please specify the role to auto-delete messages for (`{prefix}autodeleterole role: @Role`)."
                    ),
                    ephemeral=True
                )

            ch_id = channel.id if channel else None
            rid = await db.add_restriction(
                guild_id=guild.id,
                restrict_type="role",
                content=str(role.id),
                channel_id=ch_id,
                created_by=user.id
            )
            self._invalidate_cache(guild.id)

            target_desc = f"in {channel.mention}" if channel else "**Server-wide**"
            rule_desc = f"{role.mention} (`{role.name}` - ID: `{role.id}`)"

            embed = discord.Embed(
                title="🛡️ Role Auto-Delete Rule Added",
                description="Messages sent by members with this role will be **automatically deleted on send**.",
                color=COLOR_SUCCESS,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Rule ID", value=f"`#{rid}`", inline=True)
            embed.add_field(name="Type", value="`ROLE`", inline=True)
            embed.add_field(name="Target Scope", value=target_desc, inline=True)
            embed.add_field(name="Restricted Role", value=rule_desc, inline=False)
            embed.add_field(name="Protection Status", value="🛡️ **Silent Auto-Delete Active** (Messages deleted on send)", inline=False)
            embed.set_footer(text=f"Added by {user.display_name} • Remove: {prefix}autodeleterole action: remove role: @{role.name}")
            await send_reply(embed=embed)

            # Audit log
            log_eb = log_embed(
                "🛡️ Role Auto-Delete Rule Added",
                f"**Moderator:** {user.mention} (`{user.id}`)\n"
                f"**Rule ID:** `#{rid}`\n"
                f"**Type:** `ROLE`\n"
                f"**Restricted Role:** {role.mention} (`{role.name}` - `{role.id}`)\n"
                f"**Scope:** {target_desc}\n"
                f"**Action:** Silent Auto-Delete on Send",
                color=COLOR_SUCCESS,
                thumbnail=user.display_avatar.url,
                author_name=f"{user.display_name} ({user})",
                author_icon=user.display_avatar.url
            )
            log_eb.set_footer(text=f"Moderator ID: {user.id} • Rule #{rid}")
            await self._send_restrict_log(guild, log_eb)

    @app_commands.command(
        name="autodeleterole",
        description="Auto-delete messages sent by a role: add, remove, list, clear"
    )
    @app_commands.describe(
        action="Action: Add role auto-delete (default), Remove a role, List active rules, or Clear all",
        role="The role to auto-delete messages for (or remove from auto-delete)",
        channel="Optional channel to apply to (leave empty for Server-wide / all channels)",
        rule_id="Optional Rule ID to remove (e.g. 1)"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def autodeleterole_slash(
        self,
        interaction: discord.Interaction,
        action: Literal["add", "remove", "list", "clear_all"] = "add",
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        rule_id: Optional[int] = None
    ):
        """Application (slash) command with all variations in 1 command."""
        await self._execute_autodeleterole(
            ctx_or_interaction=interaction,
            action=action,
            role=role,
            channel=channel,
            rule_id=rule_id
        )

    @commands.command(
        name="autodeleterole",
        aliases=["deleterole", "roleautodelete", "autodelete"],
        help="Auto-delete all messages sent by members with a specific role (Prefix: add, remove, list, clear)"
    )
    async def auto_delete_role_cmd(self, ctx: commands.Context, *, query: Optional[str] = None):
        """Prefix command supporting all variations (&autodeleterole @Role, &autodeleterole remove @Role, &autodeleterole list, etc.)"""
        parsed = self._parse_autodeleterole_prefix_query(ctx.guild, query)
        await self._execute_autodeleterole(
            ctx_or_interaction=ctx,
            action=parsed.get("action", "list"),
            role=parsed.get("role"),
            channel=parsed.get("channel"),
            rule_id=parsed.get("rule_id")
        )


# ==============================================================================
# 11.7 COG: REPLAY (SOCIAL MEDIA VIDEO EMBED FIXER)
# ==============================================================================
class ReplayDeleteView(discord.ui.View):
    def __init__(self, author_id: int, guild_owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.guild_owner_id = guild_owner_id
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        is_author = user.id == self.author_id
        is_owner = bool(self.guild_owner_id and user.id == self.guild_owner_id)
        is_master = user.id == RESTORE_MASTER_USER_ID
        is_staff = False
        if isinstance(user, discord.Member):
            is_staff = bool(user.guild_permissions.manage_messages or user.guild_permissions.administrator)

        if is_author or is_owner or is_master or is_staff:
            try:
                await interaction.message.delete()
            except Exception:
                await interaction.response.send_message("❌ Failed to delete replay message.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Only the person who shared the link or server staff can delete this replay.", ephemeral=True)

    async def on_timeout(self):
        try:
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


class ReplayToggleView(discord.ui.View):
    def __init__(self, cog: "Replay", guild_id: int, initial_state: bool, author_id: int, guild_owner_id: Optional[int] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.is_active = initial_state
        self.author_id = author_id
        self.guild_owner_id = guild_owner_id
        self.message: Optional[discord.Message] = None
        self._update_button()

    def _update_button(self):
        self.clear_items()
        if self.is_active:
            btn = discord.ui.Button(
                label="Turn OFF",
                style=discord.ButtonStyle.danger,
                emoji="🔴",
                custom_id="replay_btn_toggle"
            )
        else:
            btn = discord.ui.Button(
                label="Turn ON",
                style=discord.ButtonStyle.success,
                emoji="🟢",
                custom_id="replay_btn_toggle"
            )
        btn.callback = self.toggle_callback
        self.add_item(btn)

    async def toggle_callback(self, interaction: discord.Interaction):
        user = interaction.user
        is_owner = bool(self.guild_owner_id and user.id == self.guild_owner_id)
        is_master = user.id == RESTORE_MASTER_USER_ID
        is_staff = False
        if isinstance(user, discord.Member):
            is_staff = bool(user.guild_permissions.manage_guild or user.guild_permissions.administrator)

        if not (is_owner or is_master or is_staff):
            return await interaction.response.send_message("❌ Only server managers or owners can toggle this setting.", ephemeral=True)

        self.is_active = not self.is_active
        await db.update_replay_setting(self.guild_id, "enabled", 1 if self.is_active else 0)
        self.cog._invalidate_cache(self.guild_id)
        self._update_button()

        embed = discord.Embed(
            title="🎬 Auto-Replay Video Embedder",
            description=(
                f"Auto-replay is currently: **{'🟢 ON (Enabled)' if self.is_active else '🔴 OFF (Disabled)'}**\n\n"
                "When enabled, videos from **X/Twitter**, **Instagram**, **YouTube Shorts**, and **TikTok** "
                f"will {'automatically play right inside Discord.' if self.is_active else 'not be modified.'}"
            ),
            color=COLOR_SUCCESS if self.is_active else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Click the button below to turn ON/OFF anytime.")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        try:
            self.clear_items()
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


class Replay(commands.Cog, name="Replay (Video Embed Fixer)"):
    """
    Automatically re-embeds videos from X/Twitter, Instagram, YouTube Shorts,
    TikTok, and Reddit so they play natively inside Discord.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[int, Dict[str, Any]] = {}

        self.patterns = [
            (
                "twitter",
                "𝕏 (Twitter)",
                re.compile(r"(?<!<)https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/([a-zA-Z0-9_]{1,50}/status/\d+|i/status/\d+)(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://fixupx.com/{m.group(1)}"
            ),
            (
                "instagram",
                "Instagram",
                re.compile(r"(?<!<)https?://(?:(?:www\.|m\.)?instagram\.com|instagr\.am)/(?:share/)?(reel|reels|p|tv)/([a-zA-Z0-9_\-]+)/?(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://zzinstagram.com/{'reel' if m.group(1).lower() in ('reel', 'reels') else m.group(1).lower()}/{m.group(2)}/"
            ),
            (
                "youtube",
                "YouTube",
                re.compile(r"(?<!<)https?://(?:(?:www\.|m\.)?youtube\.com|youtu\.be)/shorts/([a-zA-Z0-9_\-]+)(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://www.youtube.com/watch?v={m.group(1)}"
            ),
            (
                "tiktok",
                "TikTok",
                re.compile(r"(?<!<)https?://(?:www\.)?tiktok\.com/(@[a-zA-Z0-9_\.]+/video/\d+)(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://www.vxtiktok.com/{m.group(1)}"
            ),
            (
                "tiktok",
                "TikTok",
                re.compile(r"(?<!<)https?://(?:vm|vt)\.tiktok\.com/([a-zA-Z0-9_\-]+)/?(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://vm.vxtiktok.com/{m.group(1)}/"
            ),
            (
                "reddit",
                "Reddit",
                re.compile(r"(?<!<)https?://(?:www\.|old\.)?reddit\.com/(r/[a-zA-Z0-9_]+/comments/[a-zA-Z0-9_]+/[a-zA-Z0-9_]+/?)(?:\?\S*)?(?!>)", re.I),
                lambda m: f"https://www.rxddit.com/{m.group(1)}"
            ),
        ]

    def _is_authorized(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id or ctx.author.id == RESTORE_MASTER_USER_ID:
            return True
        return ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator

    async def _get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        if guild_id in self._cache:
            return self._cache[guild_id]
        settings = await db.get_replay_settings(guild_id)
        self._cache[guild_id] = settings
        return settings

    def _invalidate_cache(self, guild_id: int):
        self._cache.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        content = message.content or ""
        if not content or "http" not in content.lower():
            return

        # Check if message was ignored or deleted by auto-mod
        if hasattr(self.bot, "ignored_message_ids") and message.id in self.bot.ignored_message_ids:
            return

        # Fetch guild settings
        settings = await self._get_guild_settings(message.guild.id)
        if not settings.get("enabled", 1):
            return

        # Check disabled channels
        disabled_chans = settings.get("disabled_channels", "")
        if isinstance(disabled_chans, str):
            disabled_list = [c.strip() for c in disabled_chans.split(",") if c.strip()]
        else:
            disabled_list = [str(c) for c in disabled_chans]
        if message.channel and str(message.channel.id) in disabled_list:
            return

        # Strip intentionally suppressed links <https://...>
        clean_text = re.sub(r"<https?://\S+>", "", content)
        if not clean_text or "http" not in clean_text.lower():
            return

        # Check for link restriction in AutoMod if user is not exempt
        restrict_cog = self.bot.get_cog("Message Restriction (AutoMod)")
        if restrict_cog and hasattr(restrict_cog, "_is_exempt"):
            is_exempt = await restrict_cog._is_exempt(message.author)
            if not is_exempt:
                rules = await restrict_cog._get_guild_restrictions(message.guild.id)
                if any(r.get("restrict_type", "").lower() == "link" for r in rules):
                    return

        converted_links: List[Tuple[str, str, str]] = []
        seen_urls = set()

        for plat_key, plat_name, pattern, fixer in self.patterns:
            if not settings.get(f"{plat_key}_enabled", 1):
                continue
            for match in pattern.finditer(clean_text):
                orig_url = match.group(0)
                try:
                    fixed_url = fixer(match)
                except Exception:
                    continue
                if fixed_url not in seen_urls:
                    seen_urls.add(fixed_url)
                    converted_links.append((orig_url, fixed_url, plat_name))

        if not converted_links:
            return

        # Optionally suppress original message's embed
        if settings.get("suppress_original", 1) and message.guild.me.guild_permissions.manage_messages:
            try:
                await message.edit(suppress=True)
            except Exception:
                pass

        # Format reply
        if len(converted_links) == 1:
            orig, fixed, plat = converted_links[0]
            reply_text = f"[Replay: {plat}](<{orig}>)\n{fixed}"
        else:
            lines = [f"[Replay: {plat}](<{orig}>)\n{fixed}" for orig, fixed, plat in converted_links]
            reply_text = "\n\n".join(lines)

        view = ReplayDeleteView(author_id=message.author.id, guild_owner_id=message.guild.owner_id)
        try:
            sent_msg = await message.reply(content=reply_text, mention_author=False, view=view)
            view.message = sent_msg
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.hybrid_command(
        name="replay",
        aliases=["videofix", "embedfix"],
        description="Toggle auto-replay for videos (X, Instagram, YouTube Shorts, TikTok)"
    )
    @app_commands.describe(status="Turn ON or OFF (optional)")
    async def replay_command(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission."), ephemeral=True)

        settings = await self._get_guild_settings(ctx.guild.id)
        current = bool(settings.get("enabled", 1))

        if status is not None:
            if status.lower() == "on":
                new_val = 1
            elif status.lower() == "off":
                new_val = 0
            else:
                new_val = 0 if current else 1

            await db.update_replay_setting(ctx.guild.id, "enabled", new_val)
            self._invalidate_cache(ctx.guild.id)
            is_active = bool(new_val)
            embed = discord.Embed(
                title="🎬 Auto-Replay Video Embedder",
                description=(
                    f"Auto-replay is now: **{'🟢 ON (Enabled)' if is_active else '🔴 OFF (Disabled)'}**\n\n"
                    "Videos from **X/Twitter**, **Instagram**, **YouTube Shorts**, and **TikTok** "
                    f"will {'automatically play right inside Discord.' if is_active else 'not be modified.'}"
                ),
                color=COLOR_SUCCESS if is_active else COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Click the button below to turn ON/OFF anytime.")
            view = ReplayToggleView(self, ctx.guild.id, is_active, ctx.author.id, ctx.guild.owner_id)
            sent = await ctx.send(embed=embed, view=view)
            view.message = sent
            return

        # Show status with interactive ON/OFF button
        embed = discord.Embed(
            title="🎬 Auto-Replay Video Embedder",
            description=(
                f"Auto-replay is currently: **{'🟢 ON (Enabled)' if current else '🔴 OFF (Disabled)'}**\n\n"
                "When enabled, videos from **X/Twitter**, **Instagram**, **YouTube Shorts**, and **TikTok** "
                "automatically play right inside Discord."
            ),
            color=COLOR_SUCCESS if current else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Click the button below to turn ON/OFF anytime.")
        view = ReplayToggleView(self, ctx.guild.id, current, ctx.author.id, ctx.guild.owner_id)
        sent = await ctx.send(embed=embed, view=view)
        view.message = sent


# ==============================================================================
# 11.8 COG: FEATURE CONTROL (MODULE TOGGLE DASHBOARD)
# ==============================================================================
FEATURE_METADATA = {
    "moveer": {
        "name": "Voice Moveer",
        "emoji": "🔊",
        "desc": "Voice movement commands (/move, /cmove, /fmove, etc.)"
    },
    "tempvoice": {
        "name": "Temp Voice Channels",
        "emoji": "🎙️",
        "desc": "Dynamic Join to Create temporary voice channels"
    },
    "autoresponder": {
        "name": "Auto Responder",
        "emoji": "💬",
        "desc": "Automatic keyword trigger replies"
    },
    "restrict": {
        "name": "Message Restrict / AutoMod",
        "emoji": "🛡️",
        "desc": "Auto-deletion of restricted words, links, media, and GIFs"
    },
    "replay": {
        "name": "Video Replay Embedder",
        "emoji": "🎬",
        "desc": "Automatic playable video embeds for X, Instagram, YouTube Shorts, TikTok"
    },
    "restore": {
        "name": "Message Restore",
        "emoji": "♻️",
        "desc": "Purged message recovery and restoration"
    },
    "afk": {
        "name": "AFK System",
        "emoji": "💤",
        "desc": "AFK statuses, return notifications, and mention alerts"
    },
    "logging": {
        "name": "Audit Logging",
        "emoji": "📋",
        "desc": "Logging suite for messages, voice, mod, and server events"
    }
}


class SingleFeatureToggleView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, feature_key: str, initial_state: bool, author_id: int, guild_owner_id: Optional[int] = None):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.feature_key = feature_key
        self.is_active = initial_state
        self.author_id = author_id
        self.guild_owner_id = guild_owner_id
        self.message: Optional[discord.Message] = None
        self._update_button()

    def _update_button(self):
        self.clear_items()
        meta = FEATURE_METADATA.get(self.feature_key, {"name": self.feature_key.capitalize(), "emoji": "⚙️"})
        if self.is_active:
            btn = discord.ui.Button(
                label=f"Turn OFF {meta['name']}",
                style=discord.ButtonStyle.danger,
                emoji="🔴",
                custom_id=f"feat_toggle_{self.feature_key}"
            )
        else:
            btn = discord.ui.Button(
                label=f"Turn ON {meta['name']}",
                style=discord.ButtonStyle.success,
                emoji="🟢",
                custom_id=f"feat_toggle_{self.feature_key}"
            )
        btn.callback = self.toggle_callback
        self.add_item(btn)

    async def toggle_callback(self, interaction: discord.Interaction):
        user = interaction.user
        is_owner = bool(self.guild_owner_id and user.id == self.guild_owner_id)
        is_master = user.id == RESTORE_MASTER_USER_ID
        is_staff = False
        if isinstance(user, discord.Member):
            is_staff = bool(user.guild_permissions.manage_guild or user.guild_permissions.administrator)

        if not (is_owner or is_master or is_staff):
            return await interaction.response.send_message("❌ Only server managers or owners can toggle this setting.", ephemeral=True)

        self.is_active = not self.is_active
        await self.bot.set_feature_enabled(self.guild_id, self.feature_key, self.is_active)
        self._update_button()

        meta = FEATURE_METADATA.get(self.feature_key, {"name": self.feature_key.capitalize(), "emoji": "⚙️", "desc": ""})
        status_str = "🟢 ON (Enabled)" if self.is_active else "🔴 OFF (Disabled)"
        embed = discord.Embed(
            title=f"{meta['emoji']} Feature: {meta['name']}",
            description=(
                f"**Current Status:** {status_str}\n\n"
                f"**Description:** {meta.get('desc', '')}\n\n"
                f"Click the button below to turn **{meta['name']}** {'OFF' if self.is_active else 'ON'} anytime."
            ),
            color=COLOR_SUCCESS if self.is_active else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Requested by {user.display_name}")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        try:
            self.clear_items()
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


class ModulesDashboardView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, author_id: int, guild_owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id
        self.guild_owner_id = guild_owner_id
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self):
        self.clear_items()
        select_options = []
        for key, meta in FEATURE_METADATA.items():
            is_on = self.bot.is_feature_enabled(self.guild_id, key)
            state_label = "🟢 ON" if is_on else "🔴 OFF"
            select_options.append(
                discord.SelectOption(
                    label=f"{meta['name']} ({state_label})",
                    value=key,
                    description=f"Click to toggle {'OFF' if is_on else 'ON'}",
                    emoji=meta["emoji"]
                )
            )

        select = discord.ui.Select(
            placeholder="Select a feature to toggle ON / OFF...",
            options=select_options,
            min_values=1,
            max_values=1,
            custom_id="modules_select_toggle"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        user = interaction.user
        is_owner = bool(self.guild_owner_id and user.id == self.guild_owner_id)
        is_master = user.id == RESTORE_MASTER_USER_ID
        is_staff = False
        if isinstance(user, discord.Member):
            is_staff = bool(user.guild_permissions.manage_guild or user.guild_permissions.administrator)

        if not (is_owner or is_master or is_staff):
            return await interaction.response.send_message("❌ Only server managers or owners can toggle this setting.", ephemeral=True)

        selected_key = interaction.data["values"][0]
        current_state = self.bot.is_feature_enabled(self.guild_id, selected_key)
        new_state = not current_state
        await self.bot.set_feature_enabled(self.guild_id, selected_key, new_state)

        self._build_components()
        embed = self.build_dashboard_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def build_dashboard_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🛠️ Server Bot Features Control Panel",
            description="Manage and toggle any bot system ON or OFF for this server using the menu below.\n",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc)
        )
        lines = []
        for key, meta in FEATURE_METADATA.items():
            is_on = self.bot.is_feature_enabled(self.guild_id, key)
            icon = "🟢 **ON**" if is_on else "🔴 **OFF**"
            lines.append(f"{meta['emoji']} **{meta['name']}:** {icon}\n╰ *{meta['desc']}*")
        embed.add_field(name="System Status", value="\n\n".join(lines), inline=False)
        embed.set_footer(text="Select any feature from the dropdown below to flip it ON / OFF.")
        return embed

    async def on_timeout(self):
        try:
            self.clear_items()
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


# ==============================================================================
# 11.8 COG: DISCORD SERVER MEMORY & DISK QUOTA MANAGER
# ==============================================================================
class ServerMemory(commands.Cog, name="Discord Server Memory"):
    """
    Manages external Discord server memory storage and local host disk quota protection.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.disk_cleanup_loop.start()

    def cog_unload(self):
        self.disk_cleanup_loop.cancel()

    @tasks.loop(minutes=5)
    async def disk_cleanup_loop(self):
        """Runs periodic local cache disk sweeps to keep VPS disk usage under 15MB."""
        try:
            cleanup_local_cache(max_age_seconds=180)
            await db.prune_purged_messages(max_per_guild=500)
        except Exception:
            pass

    @disk_cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    def _is_authorized(self, member: discord.Member) -> bool:
        if member.id == member.guild.owner_id or member.id == RESTORE_MASTER_USER_ID:
            return True
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def execute_setup(self, ctx_or_msg: Union[commands.Context, discord.Message], custom_storage_id: Optional[int] = None):
        guild = ctx_or_msg.guild
        author = ctx_or_msg.author
        channel = ctx_or_msg.channel

        if isinstance(author, discord.Member) and not self._is_authorized(author):
            if isinstance(ctx_or_msg, commands.Context):
                eb = error_embed("Permission Denied", "You need Administrator or Manage Server permission to configure Bot Memory.")
                return await ctx_or_msg.send(embed=eb, ephemeral=True)
            else:
                return  # Silently ignore non-prefixed message for normal members

        target_storage_id = custom_storage_id or self.bot.memory_manager.get_storage_guild_id()

        load_eb = discord.Embed(
            title="⚙️ Initializing Discord Memory Storage",
            description=(
                f"Connecting to Memory Storage Server (`{target_storage_id}`)...\n"
                f"• Creating category for **{guild.name}**\n"
                f"• Setting up separate `#messages`, `#media-storage`, and `#purged-logs` channels...\n"
                f"• Enforcing local disk quota protection"
            ),
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        if isinstance(ctx_or_msg, commands.Context):
            status_msg = await ctx_or_msg.send(embed=load_eb)
        else:
            status_msg = await channel.send(embed=load_eb)

        try:
            channels = await self.bot.memory_manager.get_or_setup_channels(guild, custom_storage_id=target_storage_id)
        except Exception as e:
            err_eb = discord.Embed(
                title="❌ Memory Setup Failed",
                description=(
                    f"Could not connect to or configure the Memory Storage Server (`{target_storage_id}`).\n\n"
                    f"**Reason:** {e}\n\n"
                    f"**How to Fix:**\n"
                    f"1. Make sure the bot is invited to the storage server (`{target_storage_id}`).\n"
                    f"2. Ensure the bot has **Administrator** or **Manage Channels** permission in that server.\n"
                    f"3. Run `setup memory` again."
                ),
                color=COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            if hasattr(status_msg, "edit"):
                return await status_msg.edit(embed=err_eb)
            else:
                return await channel.send(embed=err_eb)

        freed_count = cleanup_local_cache(force_all=True)
        await db.prune_purged_messages(max_per_guild=500)
        try:
            await db.vacuum()
        except Exception:
            pass
        cache_mb = get_local_cache_size_mb()

        storage_guild: discord.Guild = channels["storage_guild"]
        cat: discord.CategoryChannel = channels["category"]
        msg_ch: discord.TextChannel = channels["messages_channel"]
        med_ch: discord.TextChannel = channels["media_channel"]
        prg_ch: discord.TextChannel = channels.get("purged-logs") or channels.get("purges_channel")

        success_eb = discord.Embed(
            title="✅ Discord Server Memory Setup Complete",
            description=(
                f"Successfully connected **{guild.name}** to external Discord Memory Storage!\n"
                f"All message data, media attachments, and purge logs will now be hosted in Discord memory.\n\n"
                f"🛡️ **Local Host Disk Quota Protected:**\n"
                f"Your host's 1GB limit will never be exceeded because all files are now stored directly in Discord!"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        success_eb.add_field(
            name="📁 Storage Server",
            value=f"**{storage_guild.name}** (`{storage_guild.id}`)",
            inline=True
        )
        success_eb.add_field(
            name="📂 Dedicated Category",
            value=f"`{cat.name}`",
            inline=True
        )
        success_eb.add_field(
            name="💾 Memory Channels",
            value=(
                f"• 📝 **Messages:** {msg_ch.mention} (`#messages`) — Keeps Msg ID with Date\n"
                f"• 📦 **Media:** {med_ch.mention} (`#media-storage`) — Preserved media hosting\n"
                f"• 🧹 **Purged:** {prg_ch.mention} (`#purged-logs`) — Purged records & Transcripts"
            ),
            inline=False
        )
        success_eb.add_field(
            name="🧹 Host Disk Cleanup",
            value=f"Freed **{freed_count}** cached file(s). Local cache size: **{cache_mb} MB** (Safe < 1GB limit).",
            inline=False
        )
        success_eb.set_footer(text=f"Setup executed by {author.display_name} • Type 'clean disk' anytime to wipe local temp files")

        if hasattr(status_msg, "edit"):
            await status_msg.edit(embed=success_eb)
        else:
            await channel.send(embed=success_eb)

    async def execute_status(self, ctx_or_msg: Union[commands.Context, discord.Message]):
        guild = ctx_or_msg.guild
        channel = ctx_or_msg.channel

        stored = await db.get_storage_channels(guild.id)
        storage_gid = stored["storage_guild_id"] if stored else self.bot.memory_manager.get_storage_guild_id()
        storage_guild = await self.bot.memory_manager.get_storage_guild(storage_gid)

        cache_mb = get_local_cache_size_mb()

        eb = discord.Embed(
            title="🧠 Bot Memory & Storage Status",
            color=COLOR_INFO,
            timestamp=datetime.now(timezone.utc)
        )
        eb.add_field(
            name="Storage Server",
            value=f"{storage_guild.name} (`{storage_gid}`)" if storage_guild else f"Server ID: `{storage_gid}` *(Not Connected / Missing)*",
            inline=False
        )
        if stored and storage_guild:
            cat = storage_guild.get_channel(stored.get("category_id"))
            msg_ch = storage_guild.get_channel(stored.get("messages_channel_id"))
            med_ch = storage_guild.get_channel(stored.get("media_channel_id"))
            prg_ch = storage_guild.get_channel(stored.get("purges_channel_id"))
            cat_name = cat.name if cat else "Missing"
            ch_info = (
                f"• Messages: {msg_ch.mention if msg_ch else 'None'}\n"
                f"• Media: {med_ch.mention if med_ch else 'None'}\n"
                f"• Purged Logs: {prg_ch.mention if prg_ch else 'None'}"
            )
            eb.add_field(name=f"Dedicated Category: `{cat_name}`", value=ch_info, inline=False)
            eb.add_field(name="Memory State", value="✅ **Active & Connected**", inline=True)
        else:
            eb.add_field(
                name="Memory State",
                value="⚠️ **Not configured for this server yet.**\nType `setup memory` to initialize!",
                inline=False
            )

        eb.add_field(
            name="Host Disk Usage",
            value=f"**{cache_mb} MB** / 1000 MB (Quota Safe)",
            inline=True
        )

        if isinstance(ctx_or_msg, commands.Context):
            await ctx_or_msg.send(embed=eb)
        else:
            await channel.send(embed=eb)

    async def execute_cleanup(self, ctx_or_msg: Union[commands.Context, discord.Message]):
        guild = ctx_or_msg.guild
        author = ctx_or_msg.author
        channel = ctx_or_msg.channel

        if isinstance(author, discord.Member) and not self._is_authorized(author):
            eb = error_embed("Permission Denied", "You need Administrator or Manage Server permission to clean disk cache.")
            if isinstance(ctx_or_msg, commands.Context):
                return await ctx_or_msg.send(embed=eb, ephemeral=True)
            else:
                return await channel.send(embed=eb, delete_after=8)

        before_mb = get_local_cache_size_mb()
        freed = cleanup_local_cache(force_all=True)
        pruned_msgs = await db.prune_purged_messages(max_per_guild=500)
        try:
            await db.vacuum()
        except Exception:
            pass
        after_mb = get_local_cache_size_mb()

        eb = discord.Embed(
            title="🧹 Disk Quota Cleanup Complete",
            description=(
                f"Successfully purged temporary cache files from local host disk!\n\n"
                f"• **Files Deleted:** {freed}\n"
                f"• **Old DB Records Pruned:** {pruned_msgs}\n"
                f"• **Disk Cache Size:** `{before_mb} MB` ➔ `{after_mb} MB`\n"
                f"• **Status:** Safe (Well under 1GB quota limit)"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        if isinstance(ctx_or_msg, commands.Context):
            await ctx_or_msg.send(embed=eb)
        else:
            await channel.send(embed=eb)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Automatically provisions memory category and channels when bot joins a new server."""
        storage_gid = self.bot.memory_manager.get_storage_guild_id()
        if guild.id == storage_gid:
            return
        try:
            if self.bot.memory_manager.is_storage_configured():
                await self.bot.memory_manager.get_or_setup_channels(guild)
                logger.info(f"Auto-connected new guild {guild.name} ({guild.id}) to memory storage.")
        except Exception as e:
            logger.debug(f"Auto-connect on guild join failed for {guild.id}: {e}")
        try:
            await self.bot.ensure_guild_logging(guild, auto_create=False)
            logger.info(f"Discovered logging channels for new guild {guild.name} ({guild.id}).")
        except Exception as e:
            logger.debug(f"Auto-logging on guild join failed for {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """No-prefix command router for bot memory management."""
        if not message.guild or message.author.bot:
            return

        clean = message.content.strip().lower()
        parts = clean.split()
        if not parts:
            return

        is_setup = False
        custom_storage_id = None

        if clean in ("setup memory", "setup bot memory", "setup storage", "setupmemory", "setup-memory", "setup_memory", "memory setup", "init memory", "initmemory"):
            is_setup = True
        elif len(parts) >= 2 and parts[0] == "setup" and parts[1] in ("memory", "storage"):
            is_setup = True
            if len(parts) >= 3 and parts[2].isdigit():
                custom_storage_id = int(parts[2])

        is_cleanup = clean in ("cleanup memory", "clean memory", "clean disk", "cleandisk", "cleanmemory")
        is_status = clean in ("memory status", "status memory", "bot memory", "storage status")

        if is_setup:
            await self.execute_setup(message, custom_storage_id)
        elif is_cleanup:
            await self.execute_cleanup(message)
        elif is_status:
            await self.execute_status(message)

    @commands.command(
        name="setup_memory",
        aliases=["setupmemory", "initmemory"],
        help="Set up Discord memory storage in the dedicated storage server (Prefix only, hidden from slash commands)"
    )
    async def setup_memory_cmd(self, ctx: commands.Context, storage_guild_id: Optional[str] = None):
        custom_id = int(storage_guild_id.strip()) if storage_guild_id and storage_guild_id.strip().isdigit() else None
        await self.execute_setup(ctx, custom_storage_id=custom_id)

    @commands.group(
        name="memory",
        help="Manage Discord memory storage and host disk quota (Prefix only)",
        invoke_without_command=True
    )
    async def memory_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self.execute_status(ctx)

    @memory_group.command(name="setup", help="Configure memory category and channels for this server")
    async def memory_setup_subcmd(self, ctx: commands.Context, storage_guild_id: Optional[str] = None):
        custom_id = int(storage_guild_id.strip()) if storage_guild_id and storage_guild_id.strip().isdigit() else None
        await self.execute_setup(ctx, custom_storage_id=custom_id)

    @memory_group.command(name="status", help="Check Discord memory channels and disk quota")
    async def memory_status_subcmd(self, ctx: commands.Context):
        await self.execute_status(ctx)

    @memory_group.command(name="cleanup", aliases=["clean", "cleandisk"], help="Clean up local disk cache and prune old DB records")
    async def memory_cleanup_subcmd(self, ctx: commands.Context):
        await self.execute_cleanup(ctx)

    @commands.command(
        name="cleandisk",
        aliases=["clean_disk", "cleanup_memory", "cleanmemory"],
        help="Manually sweep host disk cache (Prefix only)"
    )
    async def clean_disk_cmd(self, ctx: commands.Context):
        await self.execute_cleanup(ctx)


class FeatureControl(commands.Cog, name="Feature Control (Modules)"):
    """
    Control and toggle each bot function ON / OFF.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_authorized(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id or ctx.author.id == RESTORE_MASTER_USER_ID:
            return True
        return ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator

    async def _send_single_toggle(self, ctx: commands.Context, feature_key: str, status: Optional[Literal["on", "off", "toggle"]] = None):
        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission."), ephemeral=True)

        meta = FEATURE_METADATA.get(feature_key, {"name": feature_key.capitalize(), "emoji": "⚙️", "desc": ""})
        current_state = self.bot.is_feature_enabled(ctx.guild.id, feature_key)

        if status is not None:
            if status.lower() == "on":
                new_state = True
            elif status.lower() == "off":
                new_state = False
            else:
                new_state = not current_state

            await self.bot.set_feature_enabled(ctx.guild.id, feature_key, new_state)
            status_str = "🟢 ON (Enabled)" if new_state else "🔴 OFF (Disabled)"
            embed = discord.Embed(
                title=f"{meta['emoji']} Feature: {meta['name']}",
                description=(
                    f"**Current Status:** {status_str}\n\n"
                    f"**Description:** {meta.get('desc', '')}\n\n"
                    f"Click the button below to turn **{meta['name']}** {'OFF' if new_state else 'ON'} anytime."
                ),
                color=COLOR_SUCCESS if new_state else COLOR_ERROR,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Requested by {ctx.author.display_name}")
            view = SingleFeatureToggleView(self.bot, ctx.guild.id, feature_key, new_state, ctx.author.id, ctx.guild.owner_id)
            sent = await ctx.send(embed=embed, view=view)
            view.message = sent
            return

        status_str = "🟢 ON (Enabled)" if current_state else "🔴 OFF (Disabled)"
        embed = discord.Embed(
            title=f"{meta['emoji']} Feature: {meta['name']}",
            description=(
                f"**Current Status:** {status_str}\n\n"
                f"**Description:** {meta.get('desc', '')}\n\n"
                f"Click the button below to turn **{meta['name']}** {'OFF' if current_state else 'ON'} anytime."
            ),
            color=COLOR_SUCCESS if current_state else COLOR_ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        view = SingleFeatureToggleView(self.bot, ctx.guild.id, feature_key, current_state, ctx.author.id, ctx.guild.owner_id)
        sent = await ctx.send(embed=embed, view=view)
        view.message = sent

    @commands.hybrid_command(
        name="toggle",
        aliases=["features", "modules", "botsettings"],
        description="View or toggle bot features ON/OFF (Moveer, TempVoice, AutoResponder, etc.)"
    )
    @app_commands.describe(
        feature="The specific bot function to toggle (optional - leave blank for full dashboard)",
        status="Directly turn ON or OFF (optional)"
    )
    async def toggle_command(
        self,
        ctx: commands.Context,
        feature: Optional[Literal["moveer", "tempvoice", "autoresponder", "restrict", "replay", "restore", "afk", "logging"]] = None,
        status: Optional[Literal["on", "off", "toggle"]] = None
    ):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        if not self._is_authorized(ctx):
            return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission."), ephemeral=True)

        if feature:
            return await self._send_single_toggle(ctx, feature.lower(), status)

        view = ModulesDashboardView(self.bot, ctx.guild.id, ctx.author.id, ctx.guild.owner_id)
        embed = view.build_dashboard_embed()
        sent = await ctx.send(embed=embed, view=view)
        view.message = sent

    @commands.hybrid_command(name="toggle_moveer", description="Turn Voice Moveer ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_moveer(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "moveer", status)

    @commands.hybrid_command(name="toggle_tempvoice", description="Turn Temp Voice Channels ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_tempvoice(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "tempvoice", status)

    @commands.hybrid_command(name="toggle_autoresponder", aliases=["toggle_ar"], description="Turn Auto Responder ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_autoresponder(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "autoresponder", status)

    @commands.hybrid_command(name="toggle_restrict", aliases=["toggle_automod"], description="Turn Content Restrict (AutoMod) ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_restrict(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "restrict", status)

    @commands.hybrid_command(name="toggle_restore", description="Turn Message Restore ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_restore(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "restore", status)

    @commands.hybrid_command(name="toggle_afk", description="Turn AFK System ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_afk(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "afk", status)

    @commands.hybrid_command(name="toggle_logging", aliases=["toggle_logs"], description="Turn Audit Logging ON or OFF")
    @app_commands.describe(status="Directly turn ON or OFF")
    async def toggle_logging(self, ctx: commands.Context, status: Optional[Literal["on", "off", "toggle"]] = None):
        await self._send_single_toggle(ctx, "logging", status)


# ==============================================================================
# 12. COG: AUDIT LOGGING SUITE
# ==============================================================================
class Logging(commands.Cog, name="Audit Logging (Sapphire)"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_channel_for_log(self, guild: discord.Guild, log_type: str) -> Optional[discord.TextChannel]:
        if not self.bot.is_feature_enabled(guild.id, "logging"):
            return None
        channel_id = await db.get_log_channel(guild.id, log_type) or await db.get_log_channel(guild.id, "all")
        if channel_id: return guild.get_channel(channel_id)
        return None

    def find_voice_command_invoker(self, guild: discord.Guild, keywords: list[str], target_member: Optional[discord.Member] = None, bot_user: Optional[discord.User] = None) -> Optional[discord.Member]:
        custom_cache = getattr(self.bot, "message_cache", {})
        now = datetime.now(timezone.utc)
        recent_candidates = []
        for c in custom_cache.values():
            if c.get("guild_id") != guild.id or c.get("is_bot"):
                continue
            time_diff = abs((now - c["created_at"]).total_seconds())
            if time_diff > 45:
                continue
            content = (c.get("content") or "").lower()
            score = 0
            if bot_user and (str(bot_user.id) in content or f"<@{bot_user.id}>" in content):
                score += 3
            if target_member and (str(target_member.id) in content or f"<@{target_member.id}>" in content or target_member.name.lower() in content):
                score += 2
            if any(k in content for k in keywords):
                score += 1
            if score > 0:
                recent_candidates.append((score, time_diff, c["author_id"]))

        if recent_candidates:
            recent_candidates.sort(key=lambda x: (-x[0], x[1]))
            best_author_id = recent_candidates[0][2]
            return guild.get_member(best_author_id)
        return None

    @commands.hybrid_group(name="logs", description="Configure audit and event logging channels")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def logs_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            sub = ctx.subcommand_passed
            if sub and sub.lower() in ("search", "find"):
                args = ctx.message.content.split()[2:] if ctx.message else []
                user_token = args[0] if args else ""
                cat = args[1] if len(args) > 1 else "all"
                return await self._handle_search_logs(ctx, user_token, cat)
            await self.status(ctx)

    @logs_group.command(name="setup", description="Automatically verify and configure all logs in the right category")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def setup_logs(self, ctx: commands.Context):
        await ctx.defer()
        guild = ctx.guild
        configs = await self.bot.ensure_guild_logging(guild, auto_create=True)
        ch_mentions = [f"<#{cid}>" for cid in configs.values() if guild.get_channel(cid)]
        await ctx.send(embed=success_embed(
            "Logs Setup Complete",
            f"Configured and verified **{len(ch_mentions)}** logging channels (including AutoMod/Restrict logs):\n" + "\n".join(ch_mentions)
        ))

    @logs_group.command(name="set", description="Set log channel for an event category")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def set_log(self, ctx: commands.Context, category: Literal["message", "voice", "member", "mod", "server", "restrict", "all"], channel: discord.TextChannel):
        await db.set_log_channel(ctx.guild.id, category, channel.id)
        await ctx.send(embed=success_embed("Log Channel Configured", f"Logs for **{category}** will be sent to {channel.mention}."))

    @logs_group.command(name="status", description="View all configured log channels")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        lines = []
        for cat in ["all", "message", "voice", "member", "mod", "server", "restrict"]:
            ch_id = await db.get_log_channel(ctx.guild.id, cat)
            ch = ctx.guild.get_channel(ch_id) if ch_id else None
            lines.append(f"• **{cat.capitalize()} Logs:** {ch.mention if ch else '*Not Configured*'}")
        await ctx.send(embed=info_embed("Audit Logging Channels", "\n".join(lines)))

    async def _resolve_user_flexible(self, guild: discord.Guild, token: str) -> Optional[Union[discord.Member, discord.User]]:
        if not token: return None
        token = token.strip()
        m = re.match(r"^<@!?(\d+)>$", token)
        if m:
            uid = int(m.group(1))
            mem = guild.get_member(uid)
            if mem: return mem
            try: return await self.bot.fetch_user(uid)
            except Exception: pass
            return self.bot.get_user(uid)

        if token.isdigit():
            uid = int(token)
            mem = guild.get_member(uid)
            if mem: return mem
            try: return await self.bot.fetch_user(uid)
            except Exception: pass
            return self.bot.get_user(uid)

        clean = token.lstrip("@").lower()
        for mem in guild.members:
            if mem.name.lower() == clean or mem.display_name.lower() == clean or str(mem).lower() == clean:
                return mem
        for u in self.bot.users:
            if u.name.lower() == clean or str(u).lower() == clean:
                return u
        return None

    async def _collect_user_logs(self, guild: discord.Guild, target: Union[discord.Member, discord.User]) -> dict:
        data = {
            "messages": [],
            "mod": [],
            "audit": [],
            "voice": [],
            "timeline": []
        }

        # 1. Database purged / deleted messages
        try:
            purged_rows = await db.get_user_purged_messages(guild.id, target.id, limit=60)
            for r in purged_rows:
                data["messages"].append({
                    "timestamp": r["timestamp"],
                    "channel_id": r["channel_id"],
                    "content": r["content"] or "",
                    "has_media": bool(r.get("media_files") or r.get("attachments")),
                    "is_webhook": bool(r.get("is_webhook", 0))
                })
        except Exception:
            pass

        # 2. Moderation: Check if currently banned
        try:
            ban_entry = await guild.fetch_ban(target)
            data["mod"].append({
                "timestamp": time.time(),
                "action": "🔨 Currently Banned",
                "moderator": "Server Ban",
                "reason": ban_entry.reason or "No reason provided"
            })
        except Exception:
            pass

        # 3. Discord Audit Logs (Kicks, Bans, Timeouts, etc.)
        if guild.me.guild_permissions.view_audit_log or guild.me.guild_permissions.administrator:
            # A. Kicks targeting user
            try:
                async for entry in guild.audit_logs(limit=40, action=discord.AuditLogAction.kick):
                    if entry.target and getattr(entry.target, "id", None) == target.id:
                        mod_name = f"{entry.user.mention} (`{entry.user.id}`)" if entry.user else "Unknown"
                        data["mod"].append({
                            "timestamp": entry.created_at.timestamp(),
                            "action": "👢 Kicked",
                            "moderator": mod_name,
                            "reason": entry.reason or "No reason provided"
                        })
            except Exception: pass

            # B. Bans targeting user
            try:
                async for entry in guild.audit_logs(limit=40, action=discord.AuditLogAction.ban):
                    if entry.target and getattr(entry.target, "id", None) == target.id:
                        mod_name = f"{entry.user.mention} (`{entry.user.id}`)" if entry.user else "Unknown"
                        data["mod"].append({
                            "timestamp": entry.created_at.timestamp(),
                            "action": "🔨 Banned",
                            "moderator": mod_name,
                            "reason": entry.reason or "No reason provided"
                        })
            except Exception: pass

            # C. Unbans targeting user
            try:
                async for entry in guild.audit_logs(limit=40, action=discord.AuditLogAction.unban):
                    if entry.target and getattr(entry.target, "id", None) == target.id:
                        mod_name = f"{entry.user.mention} (`{entry.user.id}`)" if entry.user else "Unknown"
                        data["mod"].append({
                            "timestamp": entry.created_at.timestamp(),
                            "action": "🔓 Unbanned",
                            "moderator": mod_name,
                            "reason": entry.reason or "No reason provided"
                        })
            except Exception: pass

            # D. Timeouts (Member Update)
            try:
                async for entry in guild.audit_logs(limit=50, action=discord.AuditLogAction.member_update):
                    if entry.target and getattr(entry.target, "id", None) == target.id:
                        if hasattr(entry.after, "timed_out_until") and entry.after.timed_out_until:
                            mod_name = f"{entry.user.mention} (`{entry.user.id}`)" if entry.user else "Unknown"
                            data["mod"].append({
                                "timestamp": entry.created_at.timestamp(),
                                "action": "⏳ Timed Out",
                                "moderator": mod_name,
                                "reason": entry.reason or "No reason provided"
                            })
            except Exception: pass

            # E. Audit Logs executed BY this user
            try:
                async for entry in guild.audit_logs(limit=30, user=target):
                    action_name = str(entry.action).replace("AuditLogAction.", "").replace("_", " ").title()
                    target_str = ""
                    if entry.target:
                        if hasattr(entry.target, "mention"): target_str = f" on {entry.target.mention}"
                        elif hasattr(entry.target, "name"): target_str = f" on #{entry.target.name}"
                        else: target_str = f" on {entry.target}"
                    data["audit"].append({
                        "timestamp": entry.created_at.timestamp(),
                        "action": f"Executed: {action_name}{target_str}",
                        "actor": "Self",
                        "reason": entry.reason or ""
                    })
            except Exception: pass

            # F. Voice disconnects / moves
            try:
                for v_action, label in [(discord.AuditLogAction.member_disconnect, "Disconnected from Voice"), (discord.AuditLogAction.member_move, "Moved Voice Channel")]:
                    async for entry in guild.audit_logs(limit=25, action=v_action):
                        if entry.target and getattr(entry.target, "id", None) == target.id:
                            actor_name = f"by {entry.user.mention}" if entry.user else ""
                            data["voice"].append({
                                "timestamp": entry.created_at.timestamp(),
                                "action": label,
                                "actor": actor_name,
                                "reason": entry.reason or ""
                            })
            except Exception: pass

        # Sort each category by timestamp descending
        for cat in ["messages", "mod", "audit", "voice"]:
            data[cat].sort(key=lambda x: x["timestamp"], reverse=True)

        # Build timeline for overview
        timeline = []
        for m in data["messages"]:
            snippet = (m["content"][:40] + "...") if len(m["content"]) > 40 else m["content"]
            timeline.append({
                "timestamp": m["timestamp"],
                "text": f"🗑️ Deleted message in <#{m['channel_id']}>: \"{snippet}\"" if snippet else f"🗑️ Deleted message in <#{m['channel_id']}>"
            })
        for m in data["mod"]:
            timeline.append({
                "timestamp": m["timestamp"],
                "text": f"🛡️ **{m['action']}** by {m.get('moderator', 'Mod')}"
            })
        for a in data["audit"]:
            timeline.append({
                "timestamp": a["timestamp"],
                "text": f"📜 {a['action']}"
            })
        for v in data["voice"]:
            timeline.append({
                "timestamp": v["timestamp"],
                "text": f"🔊 {v['action']} {v.get('actor', '')}"
            })

        timeline.sort(key=lambda x: x["timestamp"], reverse=True)
        data["timeline"] = timeline
        return data

    async def _handle_search_logs(self, ctx: commands.Context, user_token: str, category: str = "all"):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

        is_exempt = ctx.author.id in (ctx.guild.owner_id, RESTORE_MASTER_USER_ID)
        has_perm = (
            ctx.author.guild_permissions.view_audit_log or
            ctx.author.guild_permissions.manage_guild or
            ctx.author.guild_permissions.manage_messages or
            ctx.author.guild_permissions.administrator
        )
        if not is_exempt and not has_perm:
            return await ctx.send(embed=error_embed("Missing Permissions", "You need the **View Audit Log** or **Manage Server** permission to search logs."), ephemeral=True)

        if not user_token or not user_token.strip():
            return await ctx.send(embed=error_embed("Missing Target", "Please specify a user to search logs for (`&logs search @user` or `&searchlogs @user`)."), ephemeral=True)

        await ctx.defer()

        target = await self._resolve_user_flexible(ctx.guild, user_token.strip().split()[0])
        if not target:
            return await ctx.send(embed=error_embed("User Not Found", f"Could not find any user matching `{user_token}`.\nPlease mention them or provide their User ID."), ephemeral=True)

        cat = category.lower() if category else "all"
        if cat not in ("all", "messages", "mod", "audit", "voice"):
            cat = "all"

        data = await self._collect_user_logs(ctx.guild, target)
        view = LogSearchView(ctx.author.id, target, data, initial_category=cat)
        msg = await ctx.send(embed=view.render_embed(), view=view)
        view.message = msg

    @commands.hybrid_command(
        name="searchlogs",
        aliases=["logsearch", "userlogs"],
        description="Search logs (deleted messages, mod actions, audit log) for a specific person"
    )
    @app_commands.default_permissions(view_audit_log=True)
    @app_commands.describe(
        user="The user to search logs for (mention, username, or ID)",
        category="Category to view (all, messages, mod, audit, voice)"
    )
    async def searchlogs(self, ctx: commands.Context, user: str, category: Optional[Literal["all", "messages", "mod", "audit", "voice"]] = "all"):
        await self._handle_search_logs(ctx, user, category or "all")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or (self.bot.user and message.author.id == self.bot.user.id):
            return
        if hasattr(self.bot, "store_cached_message"):
            self.bot.store_cached_message(message)
        if message.attachments or message.embeds or any(ext in (message.content or "").lower() for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", "tenor.com", "giphy.com", "notsobot")):
            asyncio.create_task(cache_message_media(message, bot=self.bot))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild: return
        ignored = getattr(self.bot, "ignored_message_ids", set())
        if payload.message_id in ignored: return

        ch = await self.get_channel_for_log(guild, "message")
        if not ch: return

        msg = payload.cached_message
        if not msg:
            custom_cache = getattr(self.bot, "message_cache", {})
            if payload.message_id in custom_cache:
                msg = CachedMessageWrapper(custom_cache[payload.message_id])

        channel_obj = guild.get_channel(payload.channel_id)
        ch_mention = channel_obj.mention if channel_obj else f"<#{payload.channel_id}>"

        if msg:
            if self.bot.user and msg.author.id == self.bot.user.id: return
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

            cached_media = list(get_cached_media(payload.message_id))
            if not cached_media:
                if getattr(msg, "attachments", None):
                    for att in msg.attachments:
                        item = await download_attachment(att, RECENT_DIR, prefix=f"del_{payload.message_id}")
                        if item: cached_media.append(item)
                if getattr(msg, "embeds", None):
                    for idx, em in enumerate(msg.embeds):
                        img_url = (em.image.url if em.image else None) or (em.thumbnail.url if em.thumbnail else None)
                        if img_url and not any(it.get("url") == img_url for it in cached_media):
                            item = await download_media_url(img_url, RECENT_DIR, prefix=f"del_{payload.message_id}_em{idx}")
                            if item: cached_media.append(item)

            files = []
            if cached_media:
                for item in cached_media:
                    fp = item["file_path"]
                    if os.path.exists(fp):
                        files.append(discord.File(fp, filename=item["filename"]))
                        if item.get("is_image") and not embed.image.url:
                            embed.set_image(url=f"attachment://{item['filename']}")
                embed.add_field(name="Media Preserved", value=f"✅ Preserved **{len(files)}** media item(s) below.")
            elif getattr(msg, "embeds", None):
                for em in msg.embeds:
                    img_url = (em.image.url if em.image else None) or (em.thumbnail.url if em.thumbnail else None)
                    if img_url and not embed.image.url:
                        embed.set_image(url=img_url)
                        break
            elif getattr(msg, "attachments", None):
                embed.add_field(name="Attachments", value="\n".join([getattr(a, "url", str(a)) for a in msg.attachments]))

            remote_media_urls = []
            if hasattr(self.bot, "memory_manager"):
                try:
                    res = await self.bot.memory_manager.store_message_data(
                        guild=guild,
                        message_id=payload.message_id,
                        channel=getattr(msg, "channel", discord.Object(id=payload.channel_id)),
                        author=msg.author,
                        content=msg.content or "",
                        created_at=msg.created_at,
                        attachments=getattr(msg, "attachments", None),
                        embeds=getattr(msg, "embeds", None),
                        media_files=[item["file_path"] for item in cached_media if os.path.exists(item.get("file_path", ""))]
                    )
                    if res and res.get("remote_media_urls"):
                        remote_media_urls = res["remote_media_urls"]
                except Exception as mem_err:
                    logger.debug(f"Memory store error on delete: {mem_err}")

            if remote_media_urls:
                media_paths = ",".join(remote_media_urls)
            else:
                media_paths = ",".join([item["file_path"] for item in cached_media if os.path.exists(item.get("file_path", ""))]) if cached_media else ""

            att_urls = ",".join([a.url for a in msg.attachments]) if msg.attachments else ""
            embeds_json = json.dumps([e.to_dict() for e in msg.embeds]) if msg.embeds else ""
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
                if files: await ch.send(embed=embed, files=files)
                else: await ch.send(embed=embed)
            except Exception: pass
            finally:
                cleanup_local_cache(max_age_seconds=60)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild: return
        ignored = getattr(self.bot, "ignored_message_ids", set())
        unhandled_ids = [mid for mid in payload.message_ids if mid not in ignored]
        if not unhandled_ids: return

        ch = await self.get_channel_for_log(guild, "message")
        if not ch: return

        channel_obj = guild.get_channel(payload.channel_id)
        ch_mention = channel_obj.mention if channel_obj else f"<#{payload.channel_id}>"

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
        except Exception: pass

        found_messages = {}
        for m in payload.cached_messages:
            if m.id in unhandled_ids: found_messages[m.id] = m

        custom_cache = getattr(self.bot, "message_cache", {})
        for mid in unhandled_ids:
            if mid not in found_messages and mid in custom_cache:
                found_messages[mid] = CachedMessageWrapper(custom_cache[mid])

        all_msgs = list(found_messages.values())
        all_msgs.sort(key=lambda x: x.created_at)

        invoker = None
        command_used = None
        for m in reversed(all_msgs):
            if not m.author.bot and (not purger or m.author.id != purger.id):
                content = (m.content or "").strip()
                if is_moderation_command_message(content) or any(content.lower().startswith(p) for p in ["!", "?", "-", ";", ".", "/", "=", "+", "~", "$"]):
                    invoker = m.author
                    command_used = content
                    break

        if not invoker:
            channel_msgs = [c for c in custom_cache.values() if c.get("channel_id") == payload.channel_id and not c.get("is_bot")]
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

        media_map = {}
        for m in all_msgs:
            cached_m = list(get_cached_media(m.id))
            if not cached_m:
                if getattr(m, "attachments", None):
                    for att in m.attachments:
                        item = await download_attachment(att, RECENT_DIR, prefix=f"bulk_{payload.channel_id}_{m.id}")
                        if item: cached_m.append(item)
                if getattr(m, "embeds", None):
                    for idx, em in enumerate(m.embeds):
                        img_url = (em.image.url if em.image else None) or (em.thumbnail.url if em.thumbnail else None)
                        if img_url and not any(it.get("url") == img_url for it in cached_m):
                            item = await download_media_url(img_url, RECENT_DIR, prefix=f"bulk_{payload.channel_id}_{m.id}_em{idx}")
                            if item: cached_m.append(item)
            if cached_m:
                media_map[m.id] = cached_m

        transcript_file = None
        html_bytes = None
        ts_filename = f"transcript-purge-{payload.channel_id}-{int(datetime.now(timezone.utc).timestamp())}.html"
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
                html_bytes = html_text.encode("utf-8")
                transcript_file = discord.File(io.BytesIO(html_bytes), filename=ts_filename)
            except Exception: pass

        all_media_items = []
        for mid, items in media_map.items():
            for it in items:
                it_copy = dict(it)
                it_copy["message_id"] = mid
                all_media_items.append(it_copy)

        remote_media_map = {}
        if hasattr(self.bot, "memory_manager"):
            try:
                mod_for_ts = invoker or purger or guild.me
                remote_media_map = await self.bot.memory_manager.store_purged_data(
                    guild=guild,
                    channel=channel_obj or ch,
                    moderator=mod_for_ts,
                    messages=all_msgs,
                    media_items=all_media_items,
                    transcript_bytes=html_bytes,
                    transcript_filename=ts_filename
                )
            except Exception as mem_err:
                logger.error(f"Failed to store bulk purge in memory manager: {mem_err}")

        messages_to_save = []
        for m in all_msgs:
            msg_remote_urls = remote_media_map.get(m.id, [])
            if msg_remote_urls:
                saved_paths = ",".join(msg_remote_urls)
            elif m.id in media_map:
                saved_paths = ",".join([item["file_path"] for item in media_map[m.id] if os.path.exists(item.get("file_path", ""))])
            else:
                saved_paths = ""
            att_urls = ",".join([a.url for a in m.attachments]) if m.attachments else ""
            embeds_json = json.dumps([e.to_dict() for e in m.embeds]) if m.embeds else ""
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
                if command_used: desc_lines.append(f"**Command Used:** `{command_used}`")
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
                if command_used: desc_lines.append(f"**Command Used:** `{command_used}`")
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
            if transcript_file: await ch.send(embed=embed, file=transcript_file)
            else: await ch.send(embed=embed)
        except Exception: pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or (self.bot.user and before.author.id == self.bot.user.id): return

        if hasattr(self.bot, "store_cached_message"):
            self.bot.store_cached_message(after)
        if getattr(after, "attachments", None) or getattr(after, "embeds", None):
            asyncio.create_task(cache_message_media(after, bot=self.bot))

        if before.content == after.content and len(before.embeds) == len(after.embeds): return

        ch = await self.get_channel_for_log(before.guild, "message")
        if not ch: return

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
        try: await ch.send(embed=embed)
        except Exception: pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        ch = await self.get_channel_for_log(member.guild, "voice")
        if not ch: return
        pfp = member.display_avatar.url

        # -------------------------------------------------------------
        # 1. MEMBER JOINED VOICE
        # -------------------------------------------------------------
        if before.channel is None and after.channel is not None:
            if member.bot: return
            embed = log_embed(
                "📥 Member Joined Voice",
                f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n**Channel:** 🔊 {after.channel.name} (`{after.channel.id}`)",
                color=COLOR_SUCCESS,
                thumbnail=pfp,
                author_name=f"{member.display_name} ({member})",
                author_icon=pfp
            )
            embed.set_footer(text=f"User ID: {member.id}")
            try: await ch.send(embed=embed)
            except Exception: pass

        # -------------------------------------------------------------
        # 2. MEMBER LEFT VOICE OR DISCONNECTED
        # -------------------------------------------------------------
        elif before.channel is not None and after.channel is None:
            tracked = None
            if hasattr(self.bot, "pop_voice_action"):
                tracked = self.bot.pop_voice_action(member.guild.id, member.id, "disconnect")
            
            disconnector = None
            via = None
            invoker = None

            if tracked:
                disconnector = tracked.get("moderator")
                via = tracked.get("via", "")
            else:
                if member.guild.me.guild_permissions.view_audit_log or member.guild.me.guild_permissions.administrator:
                    try:
                        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_disconnect):
                            time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                            if time_diff <= 8:
                                disconnector = entry.user
                                break
                    except Exception: pass


            if disconnector:
                if disconnector.bot:
                    invoker = self.find_voice_command_invoker(member.guild, ["disconnect", "dc", "vkick", "voicekick"], target_member=member, bot_user=disconnector)

                title = "🚫 Member Disconnected from Voice" if not disconnector.bot else "🚫 Member Disconnected by Bot"
                desc_lines = [
                    f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)",
                    f"**Disconnected By:** {disconnector.mention} (`{disconnector.name}` - `{disconnector.id}`)",
                ]
                if invoker and invoker.id != disconnector.id:
                    desc_lines.append(f"**Command Used By:** {invoker.mention} (`{invoker.name}` - `{invoker.id}`)")
                if via:
                    desc_lines.append(f"**Via Command:** `{via}`")
                desc_lines.append(f"**Channel:** 🔊 {before.channel.name} (`{before.channel.id}`)")

                discon_pfp = disconnector.display_avatar.url
                embed = log_embed(
                    title,
                    "\n".join(desc_lines),
                    color=COLOR_ERROR,
                    thumbnail=pfp,
                    author_name=f"Disconnected by {disconnector.display_name} ({disconnector})",
                    author_icon=discon_pfp
                )
                if invoker and hasattr(invoker, "display_avatar"):
                    embed.set_footer(text=f"Invoked by {invoker.display_name} • User ID: {invoker.id}", icon_url=invoker.display_avatar.url)
                else:
                    embed.set_footer(text=f"Target ID: {member.id} • Moderator ID: {disconnector.id}")
                try: await ch.send(embed=embed)
                except Exception: pass
            else:
                if member.bot: return
                embed = log_embed(
                    "📤 Member Left Voice",
                    f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n**Channel:** 🔊 {before.channel.name} (`{before.channel.id}`)",
                    color=COLOR_ERROR,
                    thumbnail=pfp,
                    author_name=f"{member.display_name} ({member})",
                    author_icon=pfp
                )
                embed.set_footer(text=f"User ID: {member.id}")
                try: await ch.send(embed=embed)
                except Exception: pass

        # -------------------------------------------------------------
        # 3. MEMBER SWITCHED OR MOVED BY MODERATOR / BOT
        # -------------------------------------------------------------
        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            tracked = None
            if hasattr(self.bot, "pop_voice_action"):
                tracked = self.bot.pop_voice_action(member.guild.id, member.id, "move")

            mover = None
            via = None
            invoker = None

            if tracked:
                mover = tracked.get("moderator")
                via = tracked.get("via", "")
            else:
                if member.guild.me.guild_permissions.view_audit_log or member.guild.me.guild_permissions.administrator:
                    try:
                        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_move):
                            time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                            target_chan = getattr(getattr(entry, "extra", None), "channel", None)
                            target_chan_id = getattr(target_chan, "id", None)
                            if time_diff <= 8 and (target_chan_id is None or target_chan_id == after.channel.id):
                                mover = entry.user
                                break
                    except Exception: pass


            if mover:
                if mover.bot:
                    invoker = self.find_voice_command_invoker(member.guild, ["move", "cmove", "fmove", "tmove", "drag", "pull", "bring", "rmove"], target_member=member, bot_user=mover)

                title = "🔀 Member Moved by Moderator" if not mover.bot else "🔀 Member Moved by Bot"
                desc_lines = [
                    f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)",
                    f"**Moved By:** {mover.mention} (`{mover.name}` - `{mover.id}`)",
                ]
                if invoker and invoker.id != mover.id:
                    desc_lines.append(f"**Command Used By:** {invoker.mention} (`{invoker.name}` - `{invoker.id}`)")
                if via:
                    desc_lines.append(f"**Via Command:** `{via}`")
                desc_lines.extend([
                    f"**From:** 🔊 {before.channel.name} (`{before.channel.id}`)",
                    f"**To:** 🔊 {after.channel.name} (`{after.channel.id}`)"
                ])

                mover_pfp = mover.display_avatar.url
                embed = log_embed(
                    title,
                    "\n".join(desc_lines),
                    color=COLOR_VOICE,
                    thumbnail=pfp,
                    author_name=f"Moved by {mover.display_name} ({mover})",
                    author_icon=mover_pfp
                )
                if invoker and hasattr(invoker, "display_avatar"):
                    embed.set_footer(text=f"Invoked by {invoker.display_name} • User ID: {invoker.id}", icon_url=invoker.display_avatar.url)
                else:
                    embed.set_footer(text=f"Target ID: {member.id} • Moderator ID: {mover.id}")
                try: await ch.send(embed=embed)
                except Exception: pass
            else:
                if member.bot: return
                embed = log_embed(
                    "🔄 Member Switched Voice Channel",
                    f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n**From:** 🔊 {before.channel.name} (`{before.channel.id}`)\n**To:** 🔊 {after.channel.name} (`{after.channel.id}`)",
                    color=COLOR_VOICE,
                    thumbnail=pfp,
                    author_name=f"{member.display_name} ({member})",
                    author_icon=pfp
                )
                embed.set_footer(text=f"User ID: {member.id}")
                try: await ch.send(embed=embed)
                except Exception: pass

        # -------------------------------------------------------------
        # 4. SERVER MUTE / DEAFEN
        # -------------------------------------------------------------
        elif before.mute != after.mute or before.deaf != after.deaf:
            tracked = None
            if hasattr(self.bot, "pop_voice_action"):
                tracked = self.bot.pop_voice_action(member.guild.id, member.id, "mute") or self.bot.pop_voice_action(member.guild.id, member.id, "deafen")
            
            updater = None
            via = None
            if tracked:
                updater = tracked.get("moderator")
                via = tracked.get("via", "")
            else:
                if member.guild.me.guild_permissions.view_audit_log or member.guild.me.guild_permissions.administrator:
                    try:
                        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                            target_id = getattr(entry.target, "id", None)
                            if target_id == member.id:
                                time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                                if time_diff <= 8:
                                    updater = entry.user
                                    break
                    except Exception: pass

            changes = []
            if before.mute != after.mute: changes.append(f"Server Mute: `{'Muted' if after.mute else 'Unmuted'}`")
            if before.deaf != after.deaf: changes.append(f"Server Deaf: `{'Deafened' if after.deaf else 'Undeafened'}`")
            
            desc_lines = [f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)"]
            if updater:
                desc_lines.append(f"**Updated By:** {updater.mention} (`{updater.name}` - `{updater.id}`)")
                if via:
                    desc_lines.append(f"**Via Command:** `{via}`")
            current_vc = after.channel or before.channel
            if current_vc:
                desc_lines.append(f"**Channel:** 🔊 {current_vc.name} (`{current_vc.id}`)")
            desc_lines.append("**Changes:**\n" + "\n".join(changes))

            author_name = f"Updated by {updater.display_name} ({updater})" if updater else f"{member.display_name} ({member})"
            author_icon = updater.display_avatar.url if updater else pfp
            embed = log_embed(
                "🎙️ Member Voice State Changed",
                "\n".join(desc_lines),
                color=COLOR_INFO,
                thumbnail=pfp,
                author_name=author_name,
                author_icon=author_icon
            )
            if updater:
                embed.set_footer(text=f"Target ID: {member.id} • Moderator ID: {updater.id}")
            else:
                embed.set_footer(text=f"User ID: {member.id}")
            try: await ch.send(embed=embed)
            except Exception: pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ch = await self.get_channel_for_log(member.guild, "member")
        if not ch: return
        pfp = member.display_avatar.url
        embed = log_embed(
            "👤 Member Joined",
            f"**Member:** {member.mention} (`{member.name}`)\n**ID:** `{member.id}`\n**Account Created:** <t:{int(member.created_at.timestamp())}:R>\n**Member Count:** {member.guild.member_count}",
            color=COLOR_SUCCESS, thumbnail=pfp, author_name=f"{member.display_name} ({member})", author_icon=pfp
        )
        try: await ch.send(embed=embed)
        except Exception: pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ch_member = await self.get_channel_for_log(member.guild, "member")
        ch_mod = await self.get_channel_for_log(member.guild, "mod")
        if not ch_member and not ch_mod: return
        pfp = member.display_avatar.url

        kicker = None
        kick_reason = "No reason provided"

        # 1. Check internal tracking
        tracked = None
        if hasattr(self.bot, "pop_mod_action"):
            tracked = self.bot.pop_mod_action(member.guild.id, member.id, "kick")
        if tracked:
            kicker = tracked.get("moderator")
            kick_reason = tracked.get("reason") or "No reason provided"
        else:
            # 2. Check Discord Audit Log
            try:
                if member.guild.me.guild_permissions.view_audit_log or member.guild.me.guild_permissions.administrator:
                    async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                        if entry.target and entry.target.id == member.id:
                            if abs((datetime.now(timezone.utc) - entry.created_at).total_seconds()) < 15:
                                kicker = entry.user
                                kick_reason = entry.reason or "No reason provided"
                                break
            except Exception: pass

        roles = [r.name for r in member.roles if r.name != "@everyone"]
        if kicker:
            title = "👢 Member Kicked"
            desc = (
                f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n"
                f"**Kicked By:** {kicker.mention} (`{kicker.name}` - `{kicker.id}`)\n"
                f"**Reason:** {kick_reason}\n"
                f"**Member Count:** {member.guild.member_count}"
            )
            author_name = f"Kicked by {kicker.display_name} ({kicker})"
            author_icon = kicker.display_avatar.url
            embed = log_embed(title, desc, color=COLOR_ERROR, thumbnail=pfp, author_name=author_name, author_icon=author_icon)
            embed.set_footer(text=f"Target ID: {member.id} • Moderator ID: {kicker.id}")

            # Send to mod log if available, otherwise member log
            dest = ch_mod or ch_member
            try: await dest.send(embed=embed)
            except Exception: pass
            if ch_mod and ch_member and ch_mod.id != ch_member.id:
                try: await ch_member.send(embed=embed)
                except Exception: pass
        else:
            title = "👤 Member Left"
            desc = (
                f"**Member:** {member.mention} (`{member.name}` - `{member.id}`)\n"
                f"**Roles:** {', '.join(roles) if roles else '*None*'}\n"
                f"**Member Count:** {member.guild.member_count}"
            )
            embed = log_embed(title, desc, color=COLOR_ERROR, thumbnail=pfp, author_name=f"{member.display_name} ({member})", author_icon=pfp)
            embed.set_footer(text=f"User ID: {member.id}")
            dest = ch_member or ch_mod
            try: await dest.send(embed=embed)
            except Exception: pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        pfp = after.display_avatar.url

        # -------------------------------------------------------------
        # 1. TIMEOUT (MUTE) & UNTIMEOUT (UNMUTE) LOGGING
        # -------------------------------------------------------------
        if before.timed_out_until != after.timed_out_until:
            mod_ch = await self.get_channel_for_log(after.guild, "mod")
            if mod_ch:
                is_timeout = after.is_timed_out()
                action_type = "timeout" if is_timeout else "untimeout"

                moderator = None
                reason_str = "No reason provided"
                duration_str = ""

                # 1. Check internal bot tracking
                tracked = None
                if hasattr(self.bot, "pop_mod_action"):
                    tracked = self.bot.pop_mod_action(after.guild.id, after.id, action_type)
                if tracked:
                    moderator = tracked.get("moderator")
                    reason_str = tracked.get("reason") or "No reason provided"
                    duration_str = tracked.get("duration_str", "")
                else:
                    # 2. Check Discord Audit Log
                    try:
                        if after.guild.me.guild_permissions.view_audit_log or after.guild.me.guild_permissions.administrator:
                            async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                                if entry.target and entry.target.id == after.id:
                                    time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                                    if time_diff <= 15:
                                        moderator = entry.user
                                        reason_str = entry.reason or "No reason provided"
                                        break
                    except Exception: pass

                mod_mention = f"{moderator.mention} (`{moderator.name}` - `{moderator.id}`)" if moderator else "Unknown Moderator / AutoMod"
                author_icon = moderator.display_avatar.url if moderator else pfp

                if is_timeout:
                    title = "⏳ Member Timed Out (Muted)"
                    until_ts = int(after.timed_out_until.timestamp())
                    desc = (
                        f"**Member:** {after.mention} (`{after.name}` - `{after.id}`)\n"
                        f"**Moderator:** {mod_mention}\n"
                        f"**Timeout Until:** <t:{until_ts}:R> (<t:{until_ts}:f>)\n"
                        f"**Reason:** {reason_str}"
                    )
                    color = COLOR_WARNING
                    author_name = f"Timed out by {moderator.display_name} ({moderator})" if moderator else title
                else:
                    title = "⏱️ Member Timeout Removed (Unmuted)"
                    desc = (
                        f"**Member:** {after.mention} (`{after.name}` - `{after.id}`)\n"
                        f"**Moderator:** {mod_mention}\n"
                        f"**Reason:** {reason_str}"
                    )
                    color = COLOR_SUCCESS
                    author_name = f"Timeout removed by {moderator.display_name} ({moderator})" if moderator else title

                embed = log_embed(title, desc, color=color, thumbnail=pfp, author_name=author_name, author_icon=author_icon)
                if moderator:
                    embed.set_footer(text=f"Target ID: {after.id} • Moderator ID: {moderator.id}")
                else:
                    embed.set_footer(text=f"Target ID: {after.id}")
                try: await mod_ch.send(embed=embed)
                except Exception: pass

        ch = await self.get_channel_for_log(before.guild, "member")
        if not ch: return

        if before.nick != after.nick:
            embed = log_embed("🏷️ Nickname Changed", f"**Member:** {after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}", color=COLOR_INFO, thumbnail=pfp, author_name=f"{after.display_name} ({after})", author_icon=pfp)
            await ch.send(embed=embed)
        elif before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            lines = [f"**Member:** {after.mention}"]
            if added: lines.append(f"**Added Roles:** {', '.join(added)}")
            if removed: lines.append(f"**Removed Roles:** {', '.join(removed)}")
            embed = log_embed("🛡️ Member Roles Updated", "\n".join(lines), color=COLOR_INFO, thumbnail=pfp, author_name=f"{after.display_name} ({after})", author_icon=pfp)
            await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        ch = await self.get_channel_for_log(channel.guild, "server")
        if not ch: return
        embed = log_embed("📁 Channel Created", f"**Channel:** #{channel.name} (`{channel.id}`)", color=COLOR_SUCCESS)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ch = await self.get_channel_for_log(channel.guild, "server")
        if not ch: return
        embed = log_embed("📁 Channel Deleted", f"**Channel:** #{channel.name} (`{channel.id}`)", color=COLOR_ERROR)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        ch = await self.get_channel_for_log(role.guild, "server")
        if not ch: return
        embed = log_embed("🎭 Role Created", f"**Role:** {role.name} (`{role.id}`)", color=COLOR_SUCCESS)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        ch = await self.get_channel_for_log(role.guild, "server")
        if not ch: return
        embed = log_embed("🎭 Role Deleted", f"**Role:** {role.name} (`{role.id}`)", color=COLOR_ERROR)
        await ch.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        ch = await self.get_channel_for_log(guild, "mod")
        if not ch: return
        moderator = None
        reason_str = "No reason provided"

        tracked = None
        if hasattr(self.bot, "pop_mod_action"):
            tracked = self.bot.pop_mod_action(guild.id, user.id, "ban")
        if tracked:
            moderator = tracked.get("moderator")
            reason_str = tracked.get("reason") or "No reason provided"
        else:
            try:
                if guild.me.guild_permissions.view_audit_log or guild.me.guild_permissions.administrator:
                    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                        if entry.target and entry.target.id == user.id:
                            time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                            if time_diff <= 15:
                                moderator = entry.user
                                reason_str = entry.reason or "No reason provided"
                                break
            except Exception: pass

        pfp = user.display_avatar.url
        author_name = f"Banned by {moderator.display_name} ({moderator})" if moderator else "🔨 Member Banned"
        author_icon = moderator.display_avatar.url if moderator else pfp
        mod_mention = f"{moderator.mention} (`{moderator.name}` - `{moderator.id}`)" if moderator else "Unknown Moderator"

        desc = f"**User:** {user.mention} (`{user.name}` - `{user.id}`)\n**Banned By:** {mod_mention}\n**Reason:** {reason_str}"
        embed = log_embed("🔨 Member Banned", desc, color=COLOR_ERROR, thumbnail=pfp, author_name=author_name, author_icon=author_icon)
        if moderator:
            embed.set_footer(text=f"Target ID: {user.id} • Moderator ID: {moderator.id}")
        else:
            embed.set_footer(text=f"Target ID: {user.id}")
        try: await ch.send(embed=embed)
        except Exception: pass

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        ch = await self.get_channel_for_log(guild, "mod")
        if not ch: return
        moderator = None
        reason_str = "No reason provided"

        tracked = None
        if hasattr(self.bot, "pop_mod_action"):
            tracked = self.bot.pop_mod_action(guild.id, user.id, "unban")
        if tracked:
            moderator = tracked.get("moderator")
            reason_str = tracked.get("reason") or "No reason provided"
        else:
            try:
                if guild.me.guild_permissions.view_audit_log or guild.me.guild_permissions.administrator:
                    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                        if entry.target and entry.target.id == user.id:
                            time_diff = abs((datetime.now(timezone.utc) - entry.created_at).total_seconds())
                            if time_diff <= 15:
                                moderator = entry.user
                                reason_str = entry.reason or "No reason provided"
                                break
            except Exception: pass

        pfp = user.display_avatar.url
        author_name = f"Unbanned by {moderator.display_name} ({moderator})" if moderator else "🔓 Member Unbanned"
        author_icon = moderator.display_avatar.url if moderator else pfp
        mod_mention = f"{moderator.mention} (`{moderator.name}` - `{moderator.id}`)" if moderator else "Unknown Moderator"

        desc = f"**User:** {user.mention} (`{user.name}` - `{user.id}`)\n**Unbanned By:** {mod_mention}\n**Reason:** {reason_str}"
        embed = log_embed("🔓 Member Unbanned", desc, color=COLOR_SUCCESS, thumbnail=pfp, author_name=author_name, author_icon=author_icon)
        if moderator:
            embed.set_footer(text=f"Target ID: {user.id} • Moderator ID: {moderator.id}")
        else:
            embed.set_footer(text=f"Target ID: {user.id}")
        try: await ch.send(embed=embed)
        except Exception: pass

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.TextChannel):
        if not channel.guild: return
        try:
            current_webhooks = await channel.webhooks()
            current_ids = {wh.id for wh in current_webhooks}
            for wh in current_webhooks:
                avatar_url = wh.avatar.url if wh.avatar else ""
                await db.save_webhook_backup(wh.id, channel.guild.id, channel.id, wh.name, avatar_url)
            previous_backups = await db.get_backed_up_webhooks(channel.guild.id, channel.id)
            for prev in previous_backups:
                if prev["webhook_id"] not in current_ids:
                    ch_log = await self.get_channel_for_log(channel.guild, "server") or await self.get_channel_for_log(channel.guild, "mod")
                    if ch_log:
                        embed = log_embed("🗑️ Webhook Deleted", f"**Webhook Name:** `{prev['name']}`\n**Channel:** {channel.mention}\n**Previous ID:** `{prev['webhook_id']}`\n\n💡 Staff can restore anytime: `/webhook restore {prev['name']}`", color=COLOR_ERROR)
                        await ch_log.send(embed=embed)
        except Exception: pass

# ==============================================================================
# 13. CORE BOT CLIENT & COMMAND ROUTER
# ==============================================================================
async def get_prefix(bot: commands.Bot, message: discord.Message):
    if not message.guild:
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)
    try:
        settings = await db.get_guild_settings(message.guild.id)
        guild_prefix = settings.get("prefix")
        if guild_prefix and guild_prefix.strip():
            return commands.when_mentioned_or(guild_prefix.strip())(bot, message)
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)
    except Exception:
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)


# ==============================================================================
# 12.5 INTERACTIVE HELP COMMAND SYSTEM (NON-OVERWHELMING)
# ==============================================================================

HELP_CATEGORIES = {
    "automod": {
        "title": "🛡️ AutoMod & Auto-Delete Role",
        "description": "Auto-delete spam words, links, media, and messages sent by restricted roles.",
        "icon": "🛡️",
        "commands": [
            {
                "name": "autodeleterole",
                "syntax": "/autodeleterole [action] [role] [channel] [rule_id]",
                "prefix_syntax": "&autodeleterole @Role [#channel] [action]",
                "aliases": ["deleterole", "roleautodelete", "autodelete"],
                "desc": "Auto-deletes messages sent by members with this role as soon as they are sent.",
                "perms": "Manage Messages",
                "examples": [
                    "&autodeleterole @Spammer",
                    "&autodeleterole @Guest #general",
                    "&autodeleterole list",
                    "&autodeleterole remove @Spammer",
                    "&autodeleterole clear all"
                ]
            },
            {
                "name": "restrict add",
                "syntax": "/restrict add type: <type> [content] [channel]",
                "prefix_syntax": "&restrict add <word|link|media|gif|role> [content] [#channel]",
                "aliases": ["restrict"],
                "desc": "Auto-deletes messages containing specified words, links, media, or GIFs.",
                "perms": "Manage Server",
                "examples": [
                    "&restrict add word badword",
                    "&restrict add link discord.gg",
                    "&restrict add media #announcements",
                    "&restrict add gif",
                    "&restrict add role @Muted"
                ]
            },
            {
                "name": "restrict clear",
                "syntax": "/restrict clear rule: <id|all>",
                "prefix_syntax": "&restrict clear <rule_id> | &restrict clear all",
                "aliases": ["unrestrict"],
                "desc": "Removes a specific restriction rule by Rule ID or clears all active restrictions.",
                "perms": "Manage Server",
                "examples": [
                    "&restrict clear 3",
                    "&unrestrict @Muted",
                    "&restrict clear all"
                ]
            },
            {
                "name": "restrict test",
                "syntax": "/restrict test content: <text>",
                "prefix_syntax": "&restrict test <word/link/phrase>",
                "aliases": [],
                "desc": "Safely tests if a message or link triggers active filters without posting it.",
                "perms": "Manage Server",
                "examples": [
                    "&restrict test hello world",
                    "&restrict test https://discord.gg/test"
                ]
            },
            {
                "name": "restrict exempt_staff",
                "syntax": "/restrict exempt_staff enabled: [True/False]",
                "prefix_syntax": "&restrict exempt_staff",
                "aliases": ["staff_exempt"],
                "desc": "Toggles immunity for staff and server owner (turn OFF to test live restrictions).",
                "perms": "Manage Server",
                "examples": [
                    "&restrict exempt_staff"
                ]
            },
            {
                "name": "restrict log",
                "syntax": "/restrict log channel: [#channel]",
                "prefix_syntax": "&restrict log [#channel]",
                "aliases": ["restrictlog"],
                "desc": "Sets or displays the dedicated channel for AutoMod and restriction audit logs.",
                "perms": "Manage Server",
                "examples": [
                    "&restrict log #mod-logs",
                    "&restrict log"
                ]
            }
        ]
    },
    "moderation": {
        "title": "🔨 Moderation",
        "description": "Essential moderation commands to keep your server safe and organized.",
        "icon": "🔨",
        "commands": [
            {
                "name": "ban",
                "syntax": "/ban member: @user [reason]",
                "prefix_syntax": "&ban @user [reason]",
                "aliases": [],
                "desc": "Bans a member from the server with an optional reason logged to audit logs.",
                "perms": "Ban Members",
                "examples": ["&ban @user Breaking rules", "&ban 123456789 Raiding"]
            },
            {
                "name": "kick",
                "syntax": "/kick member: @user [reason]",
                "prefix_syntax": "&kick @user [reason]",
                "aliases": [],
                "desc": "Kicks a member from the server.",
                "perms": "Kick Members",
                "examples": ["&kick @user Inactive"]
            },
            {
                "name": "timeout",
                "syntax": "/timeout member: @user duration: <10m/1h/1d> [reason]",
                "prefix_syntax": "&mute / &timeout @user <duration> [reason]",
                "aliases": ["mute"],
                "desc": "Mutes/timeouts a member for a specified duration (e.g. 10m, 1h, 1d).",
                "perms": "Moderate Members",
                "examples": ["&mute @user 30m Spamming", "&timeout @user 1d Toxicity"]
            },
            {
                "name": "untimeout",
                "syntax": "/untimeout member: @user [reason]",
                "prefix_syntax": "&unmute / &untimeout @user [reason]",
                "aliases": ["unmute"],
                "desc": "Removes active timeout/mute from a member.",
                "perms": "Moderate Members",
                "examples": ["&unmute @user Appeal accepted"]
            },
            {
                "name": "purge",
                "syntax": "/purge amount: <1-100>",
                "prefix_syntax": "&purge <amount> | &clear <amount>",
                "aliases": ["clear"],
                "desc": "Bulk deletes recent messages from the current channel.",
                "perms": "Manage Messages",
                "examples": ["&purge 20", "&clear 50"]
            },
            {
                "name": "warn",
                "syntax": "/warn member: @user reason: <text>",
                "prefix_syntax": "&warn @user <reason>",
                "aliases": [],
                "desc": "Issues a formal warning recorded in the moderation database.",
                "perms": "Manage Messages",
                "examples": ["&warn @user Excessive caps"]
            },
            {
                "name": "warnings",
                "syntax": "/warnings member: @user",
                "prefix_syntax": "&warnings @user",
                "aliases": ["warns"],
                "desc": "Displays active warning count and history for a user.",
                "perms": "Manage Messages",
                "examples": ["&warnings @user"]
            },
            {
                "name": "lock / unlock",
                "syntax": "/lock [#channel] | /unlock [#channel]",
                "prefix_syntax": "&lock [#channel] | &unlock [#channel]",
                "aliases": [],
                "desc": "Locks or unlocks channel sending permissions for @everyone.",
                "perms": "Manage Channels",
                "examples": ["&lock", "&unlock #general"]
            },
            {
                "name": "slowmode",
                "syntax": "/slowmode seconds: <0-21600>",
                "prefix_syntax": "&slowmode <seconds>",
                "aliases": [],
                "desc": "Adjusts slowmode rate limit in the current channel.",
                "perms": "Manage Channels",
                "examples": ["&slowmode 5", "&slowmode 0"]
            },
            {
                "name": "snipe",
                "syntax": "Prefix only",
                "prefix_syntax": "&snipe",
                "aliases": [],
                "desc": "Retrieves the most recently deleted message in this channel.",
                "perms": "Manage Messages",
                "examples": ["&snipe"]
            }
        ]
    },
    "logging": {
        "title": "📜 Audit Logging",
        "description": "Comprehensive audit logging for chat edits/deletions, voice events, roles, and server changes.",
        "icon": "📜",
        "commands": [
            {
                "name": "autologs",
                "syntax": "/autologs",
                "prefix_syntax": "&autologs",
                "aliases": ["setuplogs"],
                "desc": "Auto-creates and organizes all 9 specialized log channels under a clean Category in 1 click.",
                "perms": "Manage Server",
                "examples": ["/autologs", "&autologs"]
            },
            {
                "name": "setlog",
                "syntax": "/setlog type: <type> channel: [#channel]",
                "prefix_syntax": "&setlog <type> [#channel]",
                "aliases": [],
                "desc": "Assigns a specific event type (message, voice, role, member, server, mod) to a channel.",
                "perms": "Manage Server",
                "examples": ["&setlog message #msg-logs", "&setlog voice #voice-logs"]
            },
            {
                "name": "checklogs",
                "syntax": "/checklogs",
                "prefix_syntax": "&checklogs",
                "aliases": [],
                "desc": "Inspects all configured log channels, webhook status, and missing permissions.",
                "perms": "Manage Server",
                "examples": ["/checklogs", "&checklogs"]
            },
            {
                "name": "logstatus",
                "syntax": "/logstatus",
                "prefix_syntax": "&logstatus",
                "aliases": [],
                "desc": "Displays a visual status embed of all active logging channels.",
                "perms": "Manage Server",
                "examples": ["&logstatus"]
            },
            {
                "name": "ignorechannel",
                "syntax": "Prefix only",
                "prefix_syntax": "&ignorechannel [#channel] | &unignorechannel [#channel]",
                "aliases": ["unignorechannel"],
                "desc": "Excludes a channel from triggering message edit/delete audit logs.",
                "perms": "Manage Server",
                "examples": ["&ignorechannel #bot-spam", "&unignorechannel #bot-spam"]
            }
        ]
    },
    "voice": {
        "title": "🔊 Voice & Moveer",
        "description": "Mass voice movements, voice moderation, and Join-to-Create dynamic rooms.",
        "icon": "🔊",
        "commands": [
            {
                "name": "move",
                "syntax": "Prefix only",
                "prefix_syntax": "&move <@user/#channel> <target_channel>",
                "aliases": [],
                "desc": "Moves a member or everyone in a voice channel into a target channel.",
                "perms": "Move Members",
                "examples": ["&move @user Gaming 1", "&move #Lobby #Gaming"]
            },
            {
                "name": "fmove",
                "syntax": "Prefix only",
                "prefix_syntax": "&fmove <@Role> <target_channel>",
                "aliases": [],
                "desc": "Moves all connected voice users with a specific role into target channel.",
                "perms": "Move Members",
                "examples": ["&fmove @Squad Gaming 1"]
            },
            {
                "name": "cmove",
                "syntax": "Prefix only",
                "prefix_syntax": "&cmove <source_channel> <target_channel>",
                "aliases": [],
                "desc": "Moves everyone from one voice channel to another channel.",
                "perms": "Move Members",
                "examples": ["&cmove Channel-A Channel-B"]
            },
            {
                "name": "vkick",
                "syntax": "Prefix only",
                "prefix_syntax": "&vkick <@user>",
                "aliases": [],
                "desc": "Disconnects a member from their active voice channel.",
                "perms": "Move Members",
                "examples": ["&vkick @user"]
            },
            {
                "name": "vmute / vunmute",
                "syntax": "Prefix only",
                "prefix_syntax": "&vmute <@user> | &vunmute <@user>",
                "aliases": ["vunmute"],
                "desc": "Server mutes or unmutes a user in voice channels.",
                "perms": "Mute Members",
                "examples": ["&vmute @user", "&vunmute @user"]
            },
            {
                "name": "tempvoice",
                "syntax": "/tempvoice setup",
                "prefix_syntax": "&tempvoice setup",
                "aliases": ["jointocreate"],
                "desc": "Configures the Join-to-Create dynamic temporary voice channel system.",
                "perms": "Manage Channels",
                "examples": ["/tempvoice setup", "&tempvoice setup"]
            }
        ]
    },
    "roles": {
        "title": "🎭 Role Management",
        "description": "Role assignments, temporary roles with timers, and persistent sticky roles.",
        "icon": "🎭",
        "commands": [
            {
                "name": "role add / remove",
                "syntax": "/role add member: @user role: @role",
                "prefix_syntax": "&role add @user @role | &role remove @user @role",
                "aliases": [],
                "desc": "Adds or removes a role from a member.",
                "perms": "Manage Roles",
                "examples": ["&role add @user VIP", "&role remove @user Muted"]
            },
            {
                "name": "temprole",
                "syntax": "/temprole member: @user role: @role duration: <time>",
                "prefix_syntax": "&temprole @user @role <duration>",
                "aliases": [],
                "desc": "Assigns a role temporarily that automatically gets removed after duration (e.g. 7d, 24h).",
                "perms": "Manage Roles",
                "examples": ["&temprole @user VIP 7d", "&temprole @user Booster 30d"]
            },
            {
                "name": "sticky add",
                "syntax": "/sticky add role: @role",
                "prefix_syntax": "&sticky add @role",
                "aliases": ["sticky"],
                "desc": "Adds a sticky role that automatically re-applies if member leaves and rejoins.",
                "perms": "Manage Roles",
                "examples": ["&sticky add @Member", "/sticky add role: @Verified"]
            },
            {
                "name": "sticky remove",
                "syntax": "/sticky remove role: @role",
                "prefix_syntax": "&sticky remove @role",
                "aliases": [],
                "desc": "Removes a role from sticky role enforcement.",
                "perms": "Manage Roles",
                "examples": ["&sticky remove @Member"]
            },
            {
                "name": "sticky list",
                "syntax": "/sticky list",
                "prefix_syntax": "&sticky list",
                "aliases": [],
                "desc": "Displays all active sticky roles for this server.",
                "perms": "Manage Roles",
                "examples": ["&sticky list"]
            }
        ]
    },
    "autoresponder": {
        "title": "🤖 AutoResponder & AFK",
        "description": "Trigger-response text automations and user AFK status notifications.",
        "icon": "🤖",
        "commands": [
            {
                "name": "autoresponder add",
                "syntax": "/autoresponder add trigger: <text> reply: <text>",
                "prefix_syntax": "&autoresponder add <trigger> | <reply>",
                "aliases": ["ar"],
                "desc": "Registers an automatic reply whenever a trigger phrase is sent in chat.",
                "perms": "Manage Server",
                "examples": ["&autoresponder add !rules | Read #rules!", "&autoresponder add ip | mc.hypixel.net"]
            },
            {
                "name": "autoresponder list",
                "syntax": "/autoresponder list",
                "prefix_syntax": "&autoresponder list",
                "aliases": [],
                "desc": "Lists all active trigger phrases and automated responses.",
                "perms": "Manage Server",
                "examples": ["&autoresponder list"]
            },
            {
                "name": "autoresponder remove",
                "syntax": "/autoresponder remove id: <id>",
                "prefix_syntax": "&autoresponder remove <id>",
                "aliases": [],
                "desc": "Deletes an auto-responder trigger by ID.",
                "perms": "Manage Server",
                "examples": ["&autoresponder remove 2"]
            },
            {
                "name": "afk",
                "syntax": "/afk [reason]",
                "prefix_syntax": "&afk [reason]",
                "aliases": [],
                "desc": "Sets your AFK status; automatically notifies users if they mention you while away.",
                "perms": "Everyone",
                "examples": ["&afk Studying for exams", "&afk Sleeping", "/afk reason: BRB"]
            }
        ]
    },
    "utility": {
        "title": "🛠️ Utility & Replay",
        "description": "Server insights, member avatars, bot latency, custom prefix, and media link fixers.",
        "icon": "🛠️",
        "commands": [
            {
                "name": "ping",
                "syntax": "/ping",
                "prefix_syntax": "&ping",
                "aliases": ["latency"],
                "desc": "Shows bot response time, API latency, and operational status.",
                "perms": "Everyone",
                "examples": ["&ping", "/ping"]
            },
            {
                "name": "serverinfo",
                "syntax": "/serverinfo",
                "prefix_syntax": "&serverinfo",
                "aliases": ["sinfo"],
                "desc": "Displays detailed server statistics, member counts, boost level, and creation date.",
                "perms": "Everyone",
                "examples": ["&serverinfo", "/serverinfo"]
            },
            {
                "name": "userinfo",
                "syntax": "/userinfo [member: @user]",
                "prefix_syntax": "&userinfo [@user]",
                "aliases": ["uinfo", "whois"],
                "desc": "Shows member join date, account creation date, roles, permissions, and avatar.",
                "perms": "Everyone",
                "examples": ["&userinfo", "&userinfo @user"]
            },
            {
                "name": "avatar",
                "syntax": "/avatar [member: @user]",
                "prefix_syntax": "&avatar [@user]",
                "aliases": ["av"],
                "desc": "Displays full resolution avatar image of a user or server.",
                "perms": "Everyone",
                "examples": ["&avatar", "&avatar @user"]
            },
            {
                "name": "prefix",
                "syntax": "/prefix [new_prefix]",
                "prefix_syntax": "&prefix [new_prefix]",
                "aliases": ["setprefix"],
                "desc": "Displays or changes the custom prefix for this server.",
                "perms": "Manage Server",
                "examples": ["&prefix", "&prefix !"]
            },
            {
                "name": "Replay (Auto Embed)",
                "syntax": "Automatic",
                "prefix_syntax": "Post link in chat",
                "aliases": ["replay"],
                "desc": "Automatically fixes TikTok, Instagram, Twitter/X, and Reddit links into playable video embeds.",
                "perms": "Everyone",
                "examples": ["Post TikTok/Instagram/Twitter URL in any channel"]
            }
        ]
    },
    "backup": {
        "title": "💾 Backup & Storage",
        "description": "Server structure snapshots and Discord cloud memory management.",
        "icon": "💾",
        "commands": [
            {
                "name": "backup create",
                "syntax": "/backup create",
                "prefix_syntax": "&backup create",
                "aliases": ["backup"],
                "desc": "Creates a secure backup of server channels, permissions, and roles.",
                "perms": "Administrator",
                "examples": ["/backup create", "&backup create"]
            },
            {
                "name": "backup restore",
                "syntax": "/backup restore id: <backup_id>",
                "prefix_syntax": "&backup restore <backup_id>",
                "aliases": [],
                "desc": "Restores server structure from a saved backup snapshot.",
                "perms": "Server Owner",
                "examples": ["&backup restore 1"]
            },
            {
                "name": "memory setup",
                "syntax": "/memory setup",
                "prefix_syntax": "&memory setup",
                "aliases": ["memory"],
                "desc": "Configures hidden memory category and storage channels for server files.",
                "perms": "Administrator",
                "examples": ["/memory setup", "&memory setup"]
            },
            {
                "name": "memory status",
                "syntax": "/memory status",
                "prefix_syntax": "&memory status",
                "aliases": [],
                "desc": "Checks server storage quota and Discord memory channel status.",
                "perms": "Administrator",
                "examples": ["&memory status"]
            }
        ]
    }
}

def render_command_card(cmd: dict, cat_title: str, prefix: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📖 Command: {cmd['name']}",
        description=f"{cmd['desc']}\n\n**Category:** {cat_title}",
        color=COLOR_INFO,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Slash Syntax", value=f"`{cmd['syntax']}`", inline=False)
    p_syn = cmd['prefix_syntax'].replace('&', prefix)
    embed.add_field(name="Prefix Syntax", value=f"`{p_syn}`", inline=False)
    embed.add_field(name="Required Permissions", value=f"`{cmd['perms']}`", inline=True)
    if cmd.get("aliases"):
        embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd["aliases"]), inline=True)
    if cmd.get("examples"):
        ex_text = "\n".join(f"• `{ex.replace('&', prefix)}`" for ex in cmd["examples"])
        embed.add_field(name="Examples", value=ex_text, inline=False)
    embed.set_footer(text=f"Prefix: {prefix} • Type {prefix}help to open the full interactive menu")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, current_category: str = "overview"):
        options = [
            discord.SelectOption(
                label="Overview",
                value="overview",
                description="Command Center overview & quick tips",
                emoji="🏠",
                default=(current_category == "overview")
            ),
            discord.SelectOption(
                label="AutoMod & Auto-Delete Role",
                value="automod",
                description="Auto-delete roles, word/link/media/GIF filters",
                emoji="🛡️",
                default=(current_category == "automod")
            ),
            discord.SelectOption(
                label="Moderation",
                value="moderation",
                description="Ban, kick, timeout, purge, warn, lock",
                emoji="🔨",
                default=(current_category == "moderation")
            ),
            discord.SelectOption(
                label="Audit Logging",
                value="logging",
                description="Autologs, setlog, log channels & health",
                emoji="📜",
                default=(current_category == "logging")
            ),
            discord.SelectOption(
                label="Voice & Moveer",
                value="voice",
                description="Move users, temp voice rooms, voice kick",
                emoji="🔊",
                default=(current_category == "voice")
            ),
            discord.SelectOption(
                label="Role Management",
                value="roles",
                description="Add/remove roles, temp roles, sticky roles",
                emoji="🎭",
                default=(current_category == "roles")
            ),
            discord.SelectOption(
                label="AutoResponder & AFK",
                value="autoresponder",
                description="Automated text replies & AFK status",
                emoji="🤖",
                default=(current_category == "autoresponder")
            ),
            discord.SelectOption(
                label="Utility & Replay",
                value="utility",
                description="Serverinfo, userinfo, ping, video fixers",
                emoji="🛠️",
                default=(current_category == "utility")
            ),
            discord.SelectOption(
                label="Backup & Storage",
                value="backup",
                description="Server layout backups & Discord cloud memory",
                emoji="💾",
                default=(current_category == "backup")
            )
        ]
        super().__init__(placeholder="📂 Choose a category to explore...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "HelpView" = self.view
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != view.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the person who opened this help menu can navigate it.", ephemeral=True)

        view.current_category = self.values[0]
        view.update_components()
        embed = view.render_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class HelpView(discord.ui.View):
    def __init__(self, author_id: int, bot_client: commands.Bot, prefix: str = "&", initial_category: str = "overview"):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.bot = bot_client
        self.prefix = prefix
        self.current_category = initial_category
        self.message: Optional[Union[discord.Message, discord.InteractionMessage]] = None
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(HelpCategorySelect(self.current_category))

        home_btn = discord.ui.Button(
            label="Home",
            emoji="🏠",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_category == "overview"),
            custom_id="help_home_btn"
        )
        home_btn.callback = self.home_callback
        self.add_item(home_btn)

        close_btn = discord.ui.Button(
            label="Close",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id="help_close_btn"
        )
        close_btn.callback = self.close_callback
        self.add_item(close_btn)

    async def home_callback(self, interaction: discord.Interaction):
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != self.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the person who opened this help menu can navigate it.", ephemeral=True)
        self.current_category = "overview"
        self.update_components()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def close_callback(self, interaction: discord.Interaction):
        is_server_owner = bool(interaction.guild and (interaction.user.id == interaction.guild.owner_id or interaction.user.id == RESTORE_MASTER_USER_ID))
        if interaction.user.id != self.author_id and not is_server_owner:
            return await interaction.response.send_message("Only the person who opened this help menu can close it.", ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        try:
            if self.message:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
        except Exception:
            pass

    def render_embed(self) -> discord.Embed:
        bot_user = self.bot.user
        bot_name = bot_user.name if bot_user else "Moveer Sapphire"
        avatar_url = bot_user.display_avatar.url if bot_user else None

        if self.current_category == "overview":
            embed = discord.Embed(
                title=f"🤖 {bot_name} | Command Center",
                description=(
                    f"Welcome to **{bot_name}**! A complete, modular bot for moderation, AutoMod, audit logs, voice moves, temporary channels, and server protection.\n\n"
                    "### 📂 Command Categories\n"
                    "Select any category from the dropdown menu below to view its commands:\n\n"
                    "• 🛡️ **AutoMod & Auto-Delete Role** — Auto-delete messages by role & filter spam words/links\n"
                    "• 🔨 **Moderation** — Bans, timeouts/mutes, warnings, locks, and bulk purging\n"
                    "• 📜 **Audit Logging** — 1-click autologs setup & dedicated event channels\n"
                    "• 🔊 **Voice & Moveer** — Member moves, voice kick, and Join-to-Create temp channels\n"
                    "• 🎭 **Role Management** — Add/remove roles, temp roles with timers & sticky roles\n"
                    "• 🤖 **AutoResponder & AFK** — Custom keyword auto-responses & AFK status\n"
                    "• 🛠️ **Utility & Replay** — Server info, user info, ping, and social media video embeds\n"
                    "• 💾 **Backup & Storage** — Server layout backup snapshots & Discord cloud storage\n\n"
                    "---\n"
                    f"💡 **Quick Tips:**\n"
                    f"• Prefix: `{self.prefix}` *(e.g. `{self.prefix}autodeleterole` or `{self.prefix}autologs`)*\n"
                    f"• Slash Commands: Type `/` to browse interactive commands with auto-complete\n"
                    f"• Direct Lookup: Type `{self.prefix}help <command>` *(e.g. `{self.prefix}help autodeleterole`)*"
                ),
                color=COLOR_PRIMARY,
                timestamp=datetime.now(timezone.utc)
            )
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            embed.set_footer(text=f"Prefix: {self.prefix} • Choose a category below")
            return embed

        cat_info = HELP_CATEGORIES.get(self.current_category)
        if not cat_info:
            return discord.Embed(title="Error", description="Category not found.", color=COLOR_ERROR)

        cat_colors = {
            "automod": 0x3498DB,
            "moderation": 0xE74C3C,
            "logging": 0x5865F2,
            "voice": 0x9B59B6,
            "roles": 0x2ECC71,
            "autoresponder": 0x1ABC9C,
            "utility": 0xF39C12,
            "backup": 0x34495E
        }

        embed = discord.Embed(
            title=f"{cat_info['title']} ({len(cat_info['commands'])} commands)",
            description=f"{cat_info['description']}\n\n*Choose another category from the dropdown or click **Home**.*",
            color=cat_colors.get(self.current_category, COLOR_PRIMARY),
            timestamp=datetime.now(timezone.utc)
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        for cmd in cat_info["commands"]:
            embed.add_field(
                name=f"▸ {cmd['name']}",
                value=(
                    f"• **Slash:** `{cmd['syntax']}`\n"
                    f"• **Prefix:** `{cmd['prefix_syntax'].replace('&', self.prefix)}`\n"
                    f"• **Info:** {cmd['desc']}\n"
                    f"• **Perms:** `{cmd['perms']}`"
                ),
                inline=False
            )

        embed.set_footer(text=f"Prefix: {self.prefix} • Tip: {self.prefix}help <command> for detailed usage")
        return embed


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _resolve_prefix(self, ctx_or_guild: Union[commands.Context, discord.Guild, None]) -> str:
        if isinstance(ctx_or_guild, commands.Context):
            return ctx_or_guild.prefix or DEFAULT_PREFIX or "&"
        elif isinstance(ctx_or_guild, discord.Guild):
            try:
                settings = await db.get_guild_settings(ctx_or_guild.id)
                return settings.get("prefix") or DEFAULT_PREFIX or "&"
            except Exception:
                return DEFAULT_PREFIX or "&"
        return DEFAULT_PREFIX or "&"

    @commands.command(
        name="help",
        aliases=["commands", "cmds", "menu"],
        help="Open the interactive command menu or inspect a command"
    )
    async def help_cmd(self, ctx: commands.Context, *, query: Optional[str] = None):
        """Interactive help menu: &help, &help <category>, or &help <command>"""
        prefix = await self._resolve_prefix(ctx)

        if query:
            q = query.strip().lower()
            # 1. Match category
            cat_match = None
            if q in HELP_CATEGORIES:
                cat_match = q
            else:
                for cid, cdata in HELP_CATEGORIES.items():
                    if q == cid or q in cdata["title"].lower():
                        cat_match = cid
                        break

            if cat_match:
                view = HelpView(author_id=ctx.author.id, bot_client=self.bot, prefix=prefix, initial_category=cat_match)
                embed = view.render_embed()
                msg = await ctx.send(embed=embed, view=view)
                view.message = msg
                return

            # 2. Match command or alias
            found_cmd, found_cat = None, None
            for cid, cdata in HELP_CATEGORIES.items():
                for cmd in cdata["commands"]:
                    cname = cmd["name"].lower()
                    if q == cname or q in cname.split():
                        found_cmd, found_cat = cmd, cdata["title"]
                        break
                    aliases = [a.lower() for a in cmd.get("aliases", [])]
                    if q in aliases:
                        found_cmd, found_cat = cmd, cdata["title"]
                        break
                if found_cmd:
                    break

            if found_cmd:
                embed = render_command_card(found_cmd, found_cat, prefix)
                return await ctx.send(embed=embed)

            # Not found
            return await ctx.send(
                embed=error_embed(
                    "Command Not Found",
                    f"No command or category matching `{query}` was found.\nType `{prefix}help` to view all categories!"
                )
            )

        # Overview
        view = HelpView(author_id=ctx.author.id, bot_client=self.bot, prefix=prefix, initial_category="overview")
        embed = view.render_embed()
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    @app_commands.command(
        name="help",
        description="Open the interactive bot help menu or inspect a specific command"
    )
    @app_commands.describe(
        query="Optional command or category name to inspect directly (e.g. autodeleterole, moderation)"
    )
    async def help_slash(self, interaction: discord.Interaction, query: Optional[str] = None):
        """Slash command for interactive help menu."""
        prefix = await self._resolve_prefix(interaction.guild)

        if query:
            q = query.strip().lower()
            cat_match = None
            if q in HELP_CATEGORIES:
                cat_match = q
            else:
                for cid, cdata in HELP_CATEGORIES.items():
                    if q == cid or q in cdata["title"].lower():
                        cat_match = cid
                        break

            if cat_match:
                view = HelpView(author_id=interaction.user.id, bot_client=self.bot, prefix=prefix, initial_category=cat_match)
                embed = view.render_embed()
                await interaction.response.send_message(embed=embed, view=view)
                view.message = await interaction.original_response()
                return

            found_cmd, found_cat = None, None
            for cid, cdata in HELP_CATEGORIES.items():
                for cmd in cdata["commands"]:
                    cname = cmd["name"].lower()
                    if q == cname or q in cname.split():
                        found_cmd, found_cat = cmd, cdata["title"]
                        break
                    aliases = [a.lower() for a in cmd.get("aliases", [])]
                    if q in aliases:
                        found_cmd, found_cat = cmd, cdata["title"]
                        break
                if found_cmd:
                    break

            if found_cmd:
                embed = render_command_card(found_cmd, found_cat, prefix)
                return await interaction.response.send_message(embed=embed)

            return await interaction.response.send_message(
                embed=error_embed(
                    "Command Not Found",
                    f"No command or category matching `{query}` was found.\nType `/help` or `{prefix}help` to view all categories!"
                ),
                ephemeral=True
            )

        view = HelpView(author_id=interaction.user.id, bot_client=self.bot, prefix=prefix, initial_category="overview")
        embed = view.render_embed()
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()


class FeatureDisabledError(commands.CheckFailure):
    def __init__(self, feature_name: str):
        super().__init__(f"The **{feature_name}** feature is currently **disabled** on this server.")
        self.feature_name = feature_name


class MoveerSapphireBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.presences = True
        intents.guilds = True

        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            max_messages=10000
        )
        self.ignored_message_ids: set[int] = set()
        self.message_cache = {}
        self.max_custom_cache = 10000
        self.recent_voice_actions: dict = {}
        self.recent_mod_actions: dict = {}
        self.module_cache: Dict[int, Dict[str, bool]] = {}
        self.memory_manager = DiscordMemoryManager(self)

    def is_feature_enabled(self, guild_id: int, module_name: str) -> bool:
        guild_cache = self.module_cache.get(guild_id)
        if guild_cache is not None and module_name.lower() in guild_cache:
            return guild_cache[module_name.lower()]
        return True

    async def is_feature_enabled_async(self, guild_id: int, module_name: str) -> bool:
        if guild_id not in self.module_cache:
            statuses = await db.get_all_module_statuses(guild_id)
            self.module_cache[guild_id] = statuses
        return self.module_cache[guild_id].get(module_name.lower(), True)

    async def set_feature_enabled(self, guild_id: int, module_name: str, enabled: bool):
        await db.set_module_status(guild_id, module_name, enabled)
        if guild_id not in self.module_cache:
            self.module_cache[guild_id] = {}
        self.module_cache[guild_id][module_name.lower()] = enabled

    def track_voice_action(self, guild_id: int, member_id: int, action: str, moderator: discord.Member, channel_id: Optional[int] = None, via: str = ""):
        self.recent_voice_actions[(guild_id, member_id, action)] = {
            "moderator": moderator,
            "channel_id": channel_id,
            "time": time.time(),
            "via": via
        }
        now = time.time()
        expired = [k for k, v in self.recent_voice_actions.items() if now - v["time"] > 60]
        for k in expired:
            self.recent_voice_actions.pop(k, None)

    def pop_voice_action(self, guild_id: int, member_id: int, action: str, max_age: float = 15.0) -> Optional[dict]:
        key = (guild_id, member_id, action)
        record = self.recent_voice_actions.pop(key, None)
        if record and (time.time() - record["time"]) <= max_age:
            return record
        return None

    def track_mod_action(self, guild_id: int, target_id: int, action: str, moderator: discord.Member, reason: str = "", duration_str: str = ""):
        self.recent_mod_actions[(guild_id, target_id, action)] = {
            "moderator": moderator,
            "reason": reason,
            "duration_str": duration_str,
            "time": time.time()
        }
        now = time.time()
        expired = [k for k, v in self.recent_mod_actions.items() if now - v["time"] > 60]
        for k in expired:
            self.recent_mod_actions.pop(k, None)

    def pop_mod_action(self, guild_id: int, target_id: int, action: str, max_age: float = 20.0) -> Optional[dict]:
        key = (guild_id, target_id, action)
        record = self.recent_mod_actions.pop(key, None)
        if record and (time.time() - record["time"]) <= max_age:
            return record
        return None

    def store_cached_message(self, message: discord.Message):
        if not message or not message.guild: return
        if self.user and message.author.id == self.user.id: return
        avatar_url = message.author.display_avatar.url if hasattr(message.author, "display_avatar") else ""
        self.message_cache[message.id] = {
            "id": message.id,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "author_username": str(message.author),
            "author_avatar_url": avatar_url,
            "is_bot": message.author.bot,
            "is_webhook": message.webhook_id is not None,
            "content": message.content or "",
            "attachments": [{"filename": a.filename, "url": a.url, "size": a.size} for a in message.attachments],
            "embeds": [e.to_dict() for e in message.embeds],
            "created_at": message.created_at,
            "webhook_id": message.webhook_id
        }
        if len(self.message_cache) > self.max_custom_cache:
            keys = list(self.message_cache.keys())[:500]
            for k in keys: self.message_cache.pop(k, None)

    async def setup_hook(self):
        logger.info("Initializing database...")
        await db.init_tables()
        logger.info("Database initialized successfully.")

        # Register all core cogs directly (100% self-contained, no subfolder dependencies)
        logger.info("Registering cogs directly...")
        await self.add_cog(Moveer(self))
        await self.add_cog(TempVoice(self))
        await self.add_cog(Utility(self))
        await self.add_cog(RoleManagement(self))
        await self.add_cog(Moderation(self))
        await self.add_cog(AFK(self))
        await self.add_cog(Restore(self))
        await self.add_cog(AutoResponder(self))
        await self.add_cog(MessageRestrict(self))
        await self.add_cog(Replay(self))
        await self.add_cog(FeatureControl(self))
        await self.add_cog(ServerMemory(self))
        await self.add_cog(Logging(self))
        await self.add_cog(HelpCog(self))
        logger.info("All cogs registered successfully.")

        sync_on_startup = os.getenv("SYNC_COMMANDS_ON_STARTUP", "false").lower() in ("true", "1")
        if sync_on_startup:
            logger.info("Syncing slash commands globally (SYNC_COMMANDS_ON_STARTUP enabled)...")
            try:
                synced = await self.tree.sync()
                logger.info(f"Synced {len(synced)} application (slash) commands globally.")
            except Exception as e:
                logger.error(f"Error syncing slash commands: {e}")
        else:
            logger.info("Startup slash command sync skipped to prevent Discord 429 rate limits. Use '&sync' or '/sync' in Discord if commands need syncing.")

    LOG_SPECS = [
        {
            "type": "message",
            "name": "💬┃message-logs",
            "patterns": ["message-log", "msg-log", "chat-log", "message_log", "msg_log", "messagelogs"],
            "desc": "Message edits, deletes, and purged chat"
        },
        {
            "type": "voice",
            "name": "🔊┃voice-logs",
            "patterns": ["voice-log", "vc-log", "voice_log", "vc_log", "voicelogs"],
            "desc": "Voice channel joins, leaves, moves, and state changes"
        },
        {
            "type": "member",
            "name": "👤┃member-logs",
            "patterns": ["member-log", "user-log", "join-leave", "member_log", "user_log", "memberlogs"],
            "desc": "Member joins, leaves, role updates, nickname changes, and kicks/bans"
        },
        {
            "type": "mod",
            "name": "🛡️┃mod-logs",
            "patterns": ["mod-log", "moderation-log", "mod_log", "punishment-log", "modlogs"],
            "desc": "Moderator actions, channel locks, purges, and warnings"
        },
        {
            "type": "server",
            "name": "⚙️┃server-logs",
            "patterns": ["server-log", "server_log", "guild-log", "serverlogs"],
            "desc": "Channel, role, and server setting changes"
        },
        {
            "type": "restrict",
            "name": "🛡️┃restrict-logs",
            "patterns": ["restrict-log", "restrict_log", "automod-log", "automod_log", "censor-log", "filter-log", "restrictlogs"],
            "desc": "AutoMod restrictions, rule additions, removals, and auto-blocked messages"
        },
    ]

    async def ensure_guild_logging(self, guild: discord.Guild, auto_create: bool = True) -> Dict[str, int]:
        """
        Passive / Active server logging features:
        - Scans current server to discover existing log channels matching patterns.
        - If auto_create is True (default or explicit admin setup command), creates missing channels in '📋 LOGS'.
        - If auto_create is False (passive background sweep), only links existing channels without creating new ones to prevent 429 rate limits.
        """
        storage_gid = self.memory_manager.get_storage_guild_id()
        if guild.id == storage_gid:
            return {}

        current_configs = await db.get_guild_logging_config(guild.id)

        # 1. First pass: reconcile existing valid channels or match text channels by pattern
        for spec in self.LOG_SPECS:
            ltype = spec["type"]
            cid = current_configs.get(ltype)
            ch = guild.get_channel(cid) if cid else None
            if not (ch and isinstance(ch, discord.TextChannel)):
                found_ch = None
                for candidate in guild.text_channels:
                    cname = candidate.name.lower().replace("┃", "-").replace(" ", "-")
                    if any(p in cname for p in spec["patterns"]) or candidate.name.lower() == spec["name"].lower():
                        found_ch = candidate
                        break
                if found_ch:
                    await db.set_log_channel(guild.id, ltype, found_ch.id)
                    current_configs[ltype] = found_ch.id

        # If not asked to create channels (passive sweep / startup), return now without making API calls
        if not auto_create:
            return current_configs

        # 2. Identify missing channels
        missing_specs = [
            s for s in self.LOG_SPECS
            if not (guild.get_channel(current_configs.get(s["type"], 0)) and isinstance(guild.get_channel(current_configs.get(s["type"], 0)), discord.TextChannel))
        ]

        if not missing_specs:
            return current_configs

        # 3. Check if bot has permissions to manage channels
        me = guild.me
        if not (me and me.guild_permissions.manage_channels):
            logger.debug(f"Cannot auto-create missing log channels in {guild.name} ({guild.id}): Missing Manage Channels permission.")
            return current_configs

        # 4. Find the right category
        target_category = None
        # Option A: Check where existing configured log channels are placed
        for ltype, cid in current_configs.items():
            ch = guild.get_channel(cid)
            if ch and isinstance(ch, discord.TextChannel) and ch.category:
                target_category = ch.category
                break

        # Option B: Look for any existing category named 'LOGS' or containing 'log' / 'audit'
        if not target_category:
            for cat in guild.categories:
                cname = cat.name.lower()
                if any(k in cname for k in ("log", "audit")):
                    target_category = cat
                    break

        # Option C: Create private '📋 LOGS' category
        if not target_category:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True)
                }
                target_category = await guild.create_category(name="📋 LOGS", overwrites=overwrites)
            except Exception as cat_err:
                logger.error(f"Failed to create logs category in {guild.name}: {cat_err}")
                return current_configs

        # 5. Create missing channels inside target_category
        for spec in missing_specs:
            ltype = spec["type"]
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True)
                }
                new_ch = await guild.create_text_channel(
                    name=spec["name"],
                    category=target_category,
                    overwrites=overwrites,
                    topic=spec["desc"]
                )
                await db.set_log_channel(guild.id, ltype, new_ch.id)
                safe_name = new_ch.name.encode('ascii', 'replace').decode('ascii')
                safe_cat = target_category.name.encode('ascii', 'replace').decode('ascii')
                logger.info(f"Auto-created log channel '{safe_name}' in category '{safe_cat}' for {guild.id}.")
            except Exception as ch_err:
                logger.error(f"Failed to create log channel '{ltype}' in {guild.id}: {ch_err}")

        return current_configs

    async def auto_update_all_servers(self):
        """Passively reconciles and links log channels across all servers without spamming channel creations."""
        await self.wait_until_ready()
        await asyncio.sleep(5.0)
        storage_gid = self.memory_manager.get_storage_guild_id()
        for g in self.guilds:
            if g.id == storage_gid:
                continue
            try:
                await self.ensure_guild_logging(g, auto_create=False)
            except Exception as e:
                logger.debug(f"Passive logging discovery skipped for {g.name} ({g.id}): {e}")
            await asyncio.sleep(0.3)

    @tasks.loop(hours=24)
    async def auto_server_update_loop(self):
        """Periodic 24-hour passive sweep to reconcile existing log channels."""
        logger.info("Running 24-hour passive logging channel check...")
        await self.auto_update_all_servers()

    @auto_server_update_loop.before_loop
    async def before_auto_server_update_loop(self):
        await self.wait_until_ready()

    async def _auto_connect_memory_storage(self):
        """Automatically connects joined guilds to memory storage on startup if storage server is joined."""
        await self.wait_until_ready()
        storage_gid = self.memory_manager.get_storage_guild_id()
        storage_guild = self.get_guild(storage_gid) or next((g for g in self.guilds if g.id == storage_gid), None)
        if not storage_guild:
            logger.info("External Discord memory storage guild not active or not joined. Memory auto-connect skipped.")
            return

        for g in self.guilds:
            if g.id == storage_gid:
                continue
            try:
                await self.memory_manager.get_or_setup_channels(g)
                logger.info(f"Auto-connected {g.name} ({g.id}) to Discord memory storage.")
            except Exception as e:
                logger.debug(f"Auto-connect memory skipped for {g.id}: {e}")
            await asyncio.sleep(0.5)

    async def on_ready(self):
        if getattr(self, "_ready_initialized", False):
            logger.info(f"Bot reconnected to gateway as {self.user}. Skipping duplicate on_ready initialization.")
            return
        self._ready_initialized = True

        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Serving {len(self.guilds)} guilds with {sum(g.member_count or 0 for g in self.guilds)} members.")
        activity = discord.Activity(type=discord.ActivityType.listening, name="Logs & Voice | /help")
        await self.change_presence(status=discord.Status.online, activity=activity)
        try:
            freed = cleanup_local_cache(force_all=True)
            logger.info(f"Startup disk cleanup: Purged {freed} local media cache files.")
        except Exception as cache_err:
            logger.error(f"Error cleaning local cache on ready: {cache_err}")
        for g in self.guilds:
            try:
                self.module_cache[g.id] = await db.get_all_module_statuses(g.id)
            except Exception:
                pass
        asyncio.create_task(self._auto_connect_memory_storage())
        asyncio.create_task(self.auto_update_all_servers())
        if not self.auto_server_update_loop.is_running():
            self.auto_server_update_loop.start()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if hasattr(ctx.command, 'on_error'): return
        error = getattr(error, 'original', error)
        if isinstance(error, (commands.CommandNotFound, discord.NotFound)): return

        embed: Optional[discord.Embed] = None
        if isinstance(error, FeatureDisabledError):
            embed = error_embed(f"{error.feature_name} Disabled", str(error))
        elif isinstance(error, commands.MissingPermissions):
            if ctx.guild and (ctx.author.id == ctx.guild.owner_id or ctx.author.id == RESTORE_MASTER_USER_ID):
                try:
                    return await ctx.reinvoke()
                except Exception as reinvoke_err:
                    logger.error(f"Error reinvoking command for owner: {reinvoke_err}")
            perms = ", ".join([p.replace("_", " ").title() for p in error.missing_permissions])
            embed = error_embed("Permission Required", f"You need **{perms}** permission.")
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([p.replace("_", " ").title() for p in error.missing_permissions])
            embed = error_embed("Bot Missing Permission", f"Bot requires **{perms}** permission.")
        else:
            embed = error_embed("Command Error", str(error))

        if not embed:
            return

        try:
            inter = getattr(ctx, "interaction", None)
            if inter:
                if hasattr(inter, "is_expired") and inter.is_expired():
                    return
                if inter.response.is_done():
                    await inter.followup.send(embed=embed, ephemeral=True)
                else:
                    await inter.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
        except (discord.NotFound, discord.InteractionResponded):
            pass
        except Exception as send_err:
            logger.debug(f"Could not send command error response: {send_err}")

bot = MoveerSapphireBot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    error = getattr(error, 'original', error)
    if isinstance(error, discord.NotFound):
        return
    if isinstance(error, FeatureDisabledError):
        embed = error_embed(f"{error.feature_name} Disabled", str(error))
    elif isinstance(error, app_commands.MissingPermissions):
        perms = ", ".join([p.replace("_", " ").title() for p in error.missing_permissions])
        embed = error_embed("Permission Required", f"You need **{perms}** permission.")
    elif isinstance(error, app_commands.BotMissingPermissions):
        perms = ", ".join([p.replace("_", " ").title() for p in error.missing_permissions])
        embed = error_embed("Bot Missing Permission", f"Bot requires **{perms}** permission.")
    else:
        embed = error_embed("Command Error", str(error))

    try:
        if hasattr(interaction, "is_expired") and interaction.is_expired():
            return
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except (discord.NotFound, discord.InteractionResponded):
        pass
    except Exception as e:
        logger.debug(f"Could not send app command error response: {e}")

@bot.hybrid_command(name="sync", aliases=["resync", "synclogs"], description="Sync slash commands and reconnect/restore old log channels")
@app_commands.describe(guild_only="Sync commands specifically to this server (default True)")
async def sync_cmd(ctx: commands.Context, guild_only: bool = True):
    if ctx.author.id != RESTORE_MASTER_USER_ID and ctx.author.id != ctx.guild.owner_id and not ctx.author.guild_permissions.manage_guild and not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=error_embed("Permission Denied", "You need Manage Server or Administrator permission to run sync."), ephemeral=True)

    guild = ctx.guild
    if not guild:
        return await ctx.send(embed=error_embed("Server Only", "This command can only be used in a server."), ephemeral=True)

    await ctx.defer(ephemeral=False)

    synced_count = 0
    sync_error = None
    try:
        if guild_only:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            synced_count = len(synced)
        else:
            synced = await bot.tree.sync()
            synced_count = len(synced)
    except Exception as e:
        sync_error = str(e)
        try:
            synced = await bot.tree.sync()
            synced_count = len(synced)
            sync_error = None
        except Exception as e2:
            sync_error = str(e2)

    current_configs = await bot.ensure_guild_logging(guild, auto_create=False)
    all_configs = await db.get_all_logging_configs()
    for row in all_configs:
        ch_id = row["channel_id"]
        l_type = row["log_type"]
        if guild.get_channel(ch_id) and l_type not in current_configs:
            await db.set_log_channel(guild.id, l_type, ch_id)
            current_configs[l_type] = ch_id

    name_patterns = {
        "message": ["message-log", "msg-log", "chat-log", "message_log", "msg_log"],
        "voice": ["voice-log", "vc-log", "voice_log", "vc_log", "voicelogs"],
        "member": ["member-log", "user-log", "join-leave", "member_log", "user_log"],
        "mod": ["mod-log", "moderation-log", "mod_log", "punishment-log"],
        "server": ["server-log", "server_log", "guild-log"],
        "restrict": ["restrict-log", "restrict_log", "automod-log", "automod_log", "filter-log"],
        "all": ["audit-log", "audit_log", "bot-log", "bot_logs", "all-logs", "logs"]
    }

    reconnected = {}
    for cat in ["all", "message", "voice", "member", "mod", "server", "restrict"]:
        existing_ch_id = current_configs.get(cat)
        ch_obj = guild.get_channel(existing_ch_id) if existing_ch_id else None
        
        if not ch_obj:
            patterns = name_patterns.get(cat, [])
            for ch in guild.text_channels:
                cname = ch.name.lower()
                if any(p in cname for p in patterns):
                    ch_obj = ch
                    await db.set_log_channel(guild.id, cat, ch.id)
                    current_configs[cat] = ch.id
                    break
        
        if ch_obj:
            perms = ch_obj.permissions_for(guild.me)
            can_send = perms.send_messages and perms.embed_links
            reconnected[cat] = (ch_obj, can_send)
        else:
            reconnected[cat] = (None, False)

    embed = discord.Embed(
        title="🔄 Server & Bot Synchronization Complete",
        description=f"Successfully re-synchronized **{guild.name}** with the bot system.",
        color=COLOR_SUCCESS,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)

    if sync_error:
        embed.add_field(name="⚡ Slash Commands", value=f"⚠️ Notice: `{sync_error}`", inline=False)
    else:
        embed.add_field(name="⚡ Slash Commands", value=f"✅ Synced **{synced_count}** command(s) to this server.", inline=False)

    log_lines = []
    for cat, (ch_obj, can_send) in reconnected.items():
        cat_name = cat.capitalize()
        if ch_obj:
            perm_icon = "✅ (Active)" if can_send else "⚠️ (Missing Send/Embed perms)"
            log_lines.append(f"• **{cat_name} Logs:** {ch_obj.mention} {perm_icon}")
        else:
            log_lines.append(f"• **{cat_name} Logs:** *Not Configured*")
    
    embed.add_field(name="📋 Connected Log Channels", value="\n".join(log_lines), inline=False)

    needed_perms = {
        "Manage Webhooks": guild.me.guild_permissions.manage_webhooks,
        "Manage Roles": guild.me.guild_permissions.manage_roles,
        "Manage Channels": guild.me.guild_permissions.manage_channels,
        "Moderate Members": guild.me.guild_permissions.moderate_members,
        "View Audit Log": guild.me.guild_permissions.view_audit_log,
    }
    perm_summary = " ".join([f"{'✅' if v else '❌'} {k}" for k, v in needed_perms.items()])
    embed.add_field(name="🛡️ Bot Permissions", value=perm_summary, inline=False)
    embed.set_footer(text=f"Requested by {ctx.author} | ID: {ctx.author.id}")

    await ctx.send(embed=embed)

@bot.hybrid_group(name="prefix", description="View or change server command prefix", fallback="show")
async def prefix_group(ctx: commands.Context, new_prefix: Optional[str] = None):
    if new_prefix is not None:
        is_exempt = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id) or ctx.author.id == RESTORE_MASTER_USER_ID
        if not ctx.author.guild_permissions.manage_guild and not is_exempt:
            return await ctx.send(embed=error_embed("Permission Required", "Need Manage Server permission to change prefix."), ephemeral=True)
        if new_prefix.lower() in ("none", "reset", "clear", "default"):
            await db.update_guild_setting(ctx.guild.id, "prefix", None)
            return await ctx.send(embed=success_embed("Prefix Cleared", f"Prefix cleared for **{ctx.guild.name}**. Run commands directly without prefix."))
        if len(new_prefix) > 5:
            return await ctx.send(embed=error_embed("Prefix Too Long", "Prefix cannot exceed 5 characters."), ephemeral=True)
        await db.update_guild_setting(ctx.guild.id, "prefix", new_prefix)
        return await ctx.send(embed=success_embed("Prefix Updated", f"Prefix updated to `{new_prefix}`."))

    settings = await db.get_guild_settings(ctx.guild.id)
    current_prefix = settings.get("prefix")
    if current_prefix:
        desc = f"Current prefix for **{ctx.guild.name}** is: `{current_prefix}`\n• **Change:** `/prefix set <prefix>`\n• **Reset:** `/prefix reset`"
    else:
        desc = f"**No prefix** is set for **{ctx.guild.name}**!\n• Run commands directly without prefix (e.g. `help`, `ping`, `move`).\n• Or use slash commands (`/help`, `/restore`, `/logs`)."
    await ctx.send(embed=info_embed("Server Prefix", desc))

@prefix_group.command(name="set", description="Set a new command prefix for this server")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(new_prefix="New prefix character (max 5 chars)")
async def prefix_set(ctx: commands.Context, new_prefix: str):
    is_exempt = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id) or ctx.author.id == RESTORE_MASTER_USER_ID
    if not ctx.author.guild_permissions.manage_guild and not is_exempt:
        return await ctx.send(embed=error_embed("Permission Required", "Need Manage Server permission to change prefix."), ephemeral=True)
    if len(new_prefix) > 5:
        return await ctx.send(embed=error_embed("Prefix Too Long", "Prefix cannot exceed 5 characters."), ephemeral=True)
    await db.update_guild_setting(ctx.guild.id, "prefix", new_prefix)
    await ctx.send(embed=success_embed("Prefix Updated", f"Prefix updated to `{new_prefix}`."))

@prefix_group.command(name="reset", description="Reset to no prefix")
@app_commands.default_permissions(manage_guild=True)
async def prefix_reset(ctx: commands.Context):
    is_exempt = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id) or ctx.author.id == RESTORE_MASTER_USER_ID
    if not ctx.author.guild_permissions.manage_guild and not is_exempt:
        return await ctx.send(embed=error_embed("Permission Required", "Need Manage Server permission to change prefix."), ephemeral=True)
    await db.update_guild_setting(ctx.guild.id, "prefix", None)
    await ctx.send(embed=success_embed("Prefix Cleared", "Commands can now be run without prefix."))

@bot.hybrid_command(name="setprefix", description="Change command prefix (shortcut)")
@app_commands.default_permissions(manage_guild=True)
async def set_prefix_shortcut(ctx: commands.Context, new_prefix: str):
    is_exempt = bool(ctx.guild and ctx.author.id == ctx.guild.owner_id) or ctx.author.id == RESTORE_MASTER_USER_ID
    if not ctx.author.guild_permissions.manage_guild and not is_exempt:
        return await ctx.send(embed=error_embed("Permission Required", "Need Manage Server permission to change prefix."), ephemeral=True)
    if new_prefix.lower() in ("none", "reset", "clear", "default"):
        await db.update_guild_setting(ctx.guild.id, "prefix", None)
        return await ctx.send(embed=success_embed("Prefix Cleared", "Prefix cleared."))
    if len(new_prefix) > 5:
        return await ctx.send(embed=error_embed("Prefix Too Long", "Max 5 characters."), ephemeral=True)
    await db.update_guild_setting(ctx.guild.id, "prefix", new_prefix)
    await ctx.send(embed=success_embed("Prefix Updated", f"Prefix updated to `{new_prefix}`."))

def main():
    if not TOKEN or TOKEN.strip() == "YOUR_DISCORD_BOT_TOKEN_HERE":
        print("\n" + "="*70)
        print("ERROR: DISCORD_TOKEN is missing or not set!")
        print("Please set your DISCORD_TOKEN in .env or the host panel.")
        print("="*70 + "\n")
        sys.exit(1)
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
