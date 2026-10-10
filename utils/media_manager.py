import os
import asyncio
import base64
import logging
from typing import List, Optional, Tuple
import discord
from collections import OrderedDict

logger = logging.getLogger("MediaManager")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "data", "media_cache")
RECENT_DIR = os.path.join(MEDIA_DIR, "recent")
PURGE_DIR = os.path.join(MEDIA_DIR, "purge")

os.makedirs(RECENT_DIR, exist_ok=True)
os.makedirs(PURGE_DIR, exist_ok=True)

# In-memory mapping of message_id -> list of local file metadata for recent messages
_RECENT_CACHE: OrderedDict[int, List[dict]] = OrderedDict()
MAX_RECENT_MESSAGES = 1000
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024  # 10 MB per file limit

def _save_file_sync(file_path: str, data: bytes):
    with open(file_path, "wb") as f:
        f.write(data)

async def download_attachment(attachment: discord.Attachment, dest_dir: str, prefix: str = "") -> Optional[dict]:
    """Download a discord attachment and save locally."""
    if attachment.size > MAX_DOWNLOAD_SIZE:
        logger.warning(f"Attachment {attachment.filename} ({attachment.size} bytes) exceeds limit.")
        return None

    safe_filename = "".join(c for c in attachment.filename if c.isalnum() or c in "._- ")
    unique_filename = f"{prefix}_{attachment.id}_{safe_filename}"
    file_path = os.path.join(dest_dir, unique_filename)

    try:
        data = await attachment.read()
        await asyncio.to_thread(_save_file_sync, file_path, data)

        content_type = attachment.content_type or "application/octet-stream"
        is_image = content_type.startswith("image/") or any(safe_filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])

        return {
            "file_path": file_path,
            "filename": safe_filename,
            "content_type": content_type,
            "is_image": is_image,
            "size": attachment.size,
            "url": attachment.url
        }
    except Exception as e:
        logger.error(f"Failed to download attachment {attachment.filename}: {e}")
        return None

async def cache_message_media(message: discord.Message):
    """Cache attachments from incoming messages (including other bots) so they can be logged if deleted."""
    if not message.attachments:
        return

    saved_items = []
    for att in message.attachments:
        item = await download_attachment(att, RECENT_DIR, prefix=f"msg_{message.id}")
        if item:
            saved_items.append(item)

    if saved_items:
        _RECENT_CACHE[message.id] = saved_items
        # Evict oldest if exceeding limit
        while len(_RECENT_CACHE) > MAX_RECENT_MESSAGES:
            oldest_id, old_items = _RECENT_CACHE.popitem(last=False)
            for old_item in old_items:
                try:
                    if os.path.exists(old_item["file_path"]):
                        os.remove(old_item["file_path"])
                except Exception:
                    pass

def get_cached_media(message_id: int) -> List[dict]:
    """Retrieve cached media metadata for a message."""
    return _RECENT_CACHE.get(message_id, [])

def create_discord_files_from_paths(paths: List[str]) -> List[discord.File]:
    """Create discord.File objects from a list of local file paths."""
    files = []
    for fp in paths:
        if os.path.exists(fp):
            try:
                # Original filename is after the prefix and id
                basename = os.path.basename(fp)
                # Format: prefix_id_originalName
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                files.append(discord.File(fp, filename=display_name))
            except Exception as e:
                logger.error(f"Failed to prepare file {fp}: {e}")
    return files

def get_base64_data_uri(file_path: str, content_type: Optional[str] = None) -> Optional[str]:
    """Convert an image file to a base64 data URI for embedding directly in HTML."""
    if not os.path.exists(file_path):
        return None
    try:
        size = os.path.getsize(file_path)
        if size > 3 * 1024 * 1024:  # Do not base64 encode files larger than 3MB in HTML
            return None
        with open(file_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ctype = content_type or "image/png"
        return f"data:{ctype};base64,{encoded}"
    except Exception:
        return None
