import html
import re
from datetime import datetime, timezone
from typing import List, Optional
import discord

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
    """Safely format Discord message content to HTML with basic markdown."""
    if not content:
        return ""
    
    # 1. Escape HTML
    text = html.escape(content)

    # 2. Code blocks (extract or replace)
    def codeblock_sub(match):
        lang = match.group(1)
        code = match.group(2)
        return f'<pre class="codeblock"><code class="language-{lang}">{code}</code></pre>'

    text = MARKDOWN_CODEBLOCK.sub(codeblock_sub, text)

    # 3. Inline code
    text = MARKDOWN_INLINE_CODE.sub(r'<code class="inline-code">\1</code>', text)

    # 4. Spoilers
    text = MARKDOWN_SPOILER.sub(r'<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">\1</span>', text)

    # 5. Bold, italic, underline, strikethrough
    text = MARKDOWN_BOLD.sub(r"<strong>\1</strong>", text)
    text = MARKDOWN_UNDERLINE.sub(r"<u>\1</u>", text)
    text = MARKDOWN_STRIKE.sub(r"<s>\1</s>", text)
    text = MARKDOWN_ITALIC.sub(r"<em>\1</em>", text)

    # 6. Mentions
    def user_sub(m):
        uid = int(m.group(1))
        name = f"@{uid}"
        if guild:
            member = guild.get_member(uid)
            if member:
                name = f"@{member.display_name}"
        return f'<span class="mention">{html.escape(name)}</span>'

    def channel_sub(m):
        cid = int(m.group(1))
        cname = f"#{cid}"
        if guild:
            ch = guild.get_channel(cid)
            if ch:
                cname = f"#{ch.name}"
        return f'<span class="mention">{html.escape(cname)}</span>'

    def role_sub(m):
        rid = int(m.group(1))
        rname = f"@{rid}"
        if guild:
            role = guild.get_role(rid)
            if role:
                rname = f"@{role.name}"
        return f'<span class="mention">{html.escape(rname)}</span>'

    text = USER_MENTION.sub(user_sub, text)
    text = CHANNEL_MENTION.sub(channel_sub, text)
    text = ROLE_MENTION.sub(role_sub, text)

    # 7. URLs
    text = URL_REGEX.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', text)

    # 8. Newlines
    text = text.replace("\n", "<br>")
    return text

def generate_html_transcript(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    moderator: discord.Member,
    messages: List[discord.Message],
    reason: Optional[str] = None,
    media_map: Optional[dict] = None
) -> str:
    """Generate a modern, self-contained, Discord-themed HTML transcript of purged messages."""
    from utils.media_manager import get_base64_data_uri

    # Sort messages chronologically (oldest first)
    sorted_messages = sorted(messages, key=lambda m: m.created_at)

    guild_name = html.escape(guild.name)
    channel_name = html.escape(channel.name)
    mod_name = html.escape(str(moderator))
    mod_id = moderator.id
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

        bot_badge = '<span class="bot-badge">BOT</span>' if author.bot else ""

        # Attachments HTML
        attachments_html = ""
        msg_saved_media = media_map.get(msg.id, []) if media_map else []
        saved_by_filename = {item["filename"]: item for item in msg_saved_media}

        if msg.attachments:
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

        # Embeds HTML
        embeds_html = ""
        if msg.embeds:
            embed_items = []
            for em in msg.embeds:
                em_color = f"#{em.color.value:06x}" if em.color else "#5865f2"
                em_title = f'<div class="embed-title">{html.escape(em.title)}</div>' if em.title else ""
                em_desc = f'<div class="embed-desc">{format_content(em.description or "", guild)}</div>' if em.description else ""
                
                fields_html = ""
                if em.fields:
                    f_list = []
                    for f in em.fields:
                        f_list.append(f'''
                        <div class="embed-field {'inline' if f.inline else ''}">
                            <div class="field-name">{html.escape(f.name)}</div>
                            <div class="field-value">{format_content(f.value, guild)}</div>
                        </div>''')
                    fields_html = f'<div class="embed-fields">{"".join(f_list)}</div>'

                footer_html = f'<div class="embed-footer">{html.escape(em.footer.text)}</div>' if (em.footer and em.footer.text) else ""

                embed_items.append(f'''
                <div class="discord-embed" style="border-left: 4px solid {em_color};">
                    {em_title}
                    {em_desc}
                    {fields_html}
                    {footer_html}
                </div>''')
            embeds_html = f'<div class="embeds-container">{"".join(embed_items)}</div>'

        # Message Card
        block = f'''
        <div class="message-card" data-author="{author_name.lower()} {username.lower()}" data-content="{html.escape(msg.content or '').lower()}">
            <img class="avatar" src="{avatar_url}" alt="{author_name}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'"/>
            <div class="message-body">
                <div class="message-header">
                    <span class="author-name">{author_name}</span>
                    {bot_badge}
                    <span class="author-tag">({username} • ID: {author_id})</span>
                    <span class="timestamp">{timestamp_str}</span>
                </div>
                {f'<div class="message-content">{content_html}</div>' if content_html else ''}
                {attachments_html}
                {embeds_html}
            </div>
        </div>
        '''
        message_blocks.append(block)

    messages_joined = "\n".join(message_blocks) if message_blocks else '<div class="empty-state">No message contents were captured.</div>'

    html_document = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Purge Transcript - #{channel_name} - {guild_name}</title>
    <style>
        :root {{
            --bg-primary: #313338;
            --bg-secondary: #2b2d31;
            --bg-tertiary: #1e1f22;
            --text-normal: #dbdee1;
            --text-muted: #949ba4;
            --text-link: #00a8fc;
            --blurple: #5865f2;
            --brand-green: #23a55a;
            --brand-red: #f23f43;
            --embed-bg: #2b2d31;
            --border-subtle: #3f4147;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: "gg sans", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-normal);
            font-size: 15px;
            line-height: 1.4;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        /* Header */
        header {{
            background-color: var(--bg-secondary);
            border-bottom: 1px solid var(--border-subtle);
            padding: 18px 24px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        }}
        .header-top {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 16px;
        }}
        .server-info {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}
        .server-icon {{
            width: 48px;
            height: 48px;
            border-radius: 14px;
            object-fit: cover;
            background-color: var(--bg-tertiary);
        }}
        .server-details h1 {{
            font-size: 20px;
            font-weight: 700;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .server-details .channel-name {{
            color: var(--text-link);
            font-weight: 600;
        }}
        .server-meta {{
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 3px;
        }}
        .stats-badge {{
            background-color: var(--bg-tertiary);
            border: 1px solid var(--border-subtle);
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            color: #ffffff;
        }}
        .stats-badge span {{
            color: var(--blurple);
        }}
        /* Search Box */
        .controls {{
            margin-top: 14px;
            display: flex;
            gap: 12px;
        }}
        .search-input {{
            flex: 1;
            background-color: var(--bg-tertiary);
            border: 1px solid var(--border-subtle);
            color: var(--text-normal);
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }}
        .search-input:focus {{
            border-color: var(--blurple);
        }}
        /* Main Chat Area */
        main {{
            flex: 1;
            max-width: 1200px;
            width: 100%;
            margin: 0 auto;
            padding: 20px 24px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}
        .message-card {{
            display: flex;
            gap: 16px;
            padding: 10px 14px;
            border-radius: 8px;
            transition: background 0.15s ease;
        }}
        .message-card:hover {{
            background-color: rgba(43, 45, 49, 0.4);
        }}
        .avatar {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            object-fit: cover;
            flex-shrink: 0;
            margin-top: 2px;
        }}
        .message-body {{
            flex: 1;
            min-width: 0;
        }}
        .message-header {{
            display: flex;
            align-items: baseline;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 4px;
        }}
        .author-name {{
            font-weight: 600;
            color: #f2f3f5;
            font-size: 15px;
        }}
        .bot-badge {{
            background-color: var(--blurple);
            color: #ffffff;
            font-size: 10px;
            font-weight: 700;
            padding: 1px 4px;
            border-radius: 3px;
            text-transform: uppercase;
        }}
        .author-tag {{
            font-size: 12px;
            color: var(--text-muted);
        }}
        .timestamp {{
            font-size: 12px;
            color: var(--text-muted);
            margin-left: 4px;
        }}
        .message-content {{
            font-size: 14.5px;
            color: var(--text-normal);
            word-break: break-word;
        }}
        /* Markdown Helpers */
        a {{
            color: var(--text-link);
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        .mention {{
            background-color: rgba(88, 101, 242, 0.3);
            color: #c9cdfb;
            padding: 1px 4px;
            border-radius: 3px;
            font-weight: 500;
        }}
        .inline-code {{
            background-color: var(--bg-tertiary);
            padding: 2px 5px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 13px;
        }}
        .codeblock {{
            background-color: var(--bg-tertiary);
            padding: 10px 14px;
            border-radius: 6px;
            margin: 6px 0;
            overflow-x: auto;
            font-family: Consolas, monospace;
            font-size: 13px;
            border: 1px solid var(--border-subtle);
        }}
        .spoiler {{
            background-color: #202225;
            color: transparent;
            cursor: pointer;
            padding: 0 4px;
            border-radius: 3px;
            user-select: none;
        }}
        .spoiler.revealed {{
            background-color: rgba(255,255,255,0.1);
            color: var(--text-normal);
            user-select: text;
        }}
        /* Attachments */
        .attachments-container {{
            margin-top: 8px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .attachment-image-card {{
            max-width: 400px;
            border-radius: 6px;
            overflow: hidden;
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-subtle);
        }}
        .attachment-img {{
            max-width: 100%;
            max-height: 300px;
            display: block;
            object-fit: contain;
        }}
        .attachment-meta {{
            padding: 4px 8px;
            font-size: 12px;
            background-color: var(--bg-tertiary);
        }}
        .attachment-file-card {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-subtle);
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 13px;
        }}
        /* Embeds */
        .embeds-container {{
            margin-top: 8px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .discord-embed {{
            background-color: var(--embed-bg);
            border-radius: 4px;
            padding: 12px 16px;
            max-width: 520px;
        }}
        .embed-title {{
            font-weight: 600;
            font-size: 15px;
            color: #ffffff;
            margin-bottom: 6px;
        }}
        .embed-desc {{
            font-size: 13.5px;
            color: var(--text-normal);
            margin-bottom: 8px;
        }}
        .embed-fields {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 6px;
        }}
        .embed-field {{
            flex: 1 1 100%;
        }}
        .embed-field.inline {{
            flex: 1 1 45%;
        }}
        .field-name {{
            font-size: 12px;
            font-weight: 700;
            color: var(--text-muted);
            margin-bottom: 2px;
        }}
        .field-value {{
            font-size: 13px;
            color: var(--text-normal);
        }}
        .embed-footer {{
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 8px;
        }}
        /* Footer */
        footer {{
            background-color: var(--bg-secondary);
            border-top: 1px solid var(--border-subtle);
            padding: 14px 24px;
            text-align: center;
            font-size: 13px;
            color: var(--text-muted);
            margin-top: auto;
        }}
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-top">
            <div class="server-info">
                <img class="server-icon" src="{guild_icon}" alt="{guild_name}"/>
                <div class="server-details">
                    <h1>{guild_name} <span class="channel-name">#{channel_name}</span></h1>
                    <div class="server-meta">
                        Moderator: <strong>{mod_name}</strong> (ID: {mod_id}) &bull; Date: {now_str}
                    </div>
                </div>
            </div>
            <div class="stats-badge">
                Purged: <span>{total_count}</span> message(s)
            </div>
        </div>
        <div class="controls">
            <input type="text" id="searchInput" class="search-input" placeholder="Search purged messages by user or keyword..." oninput="filterMessages()">
        </div>
    </header>

    <main id="messagesList">
        {messages_joined}
    </main>

    <footer>
        Purge Audit Transcript generated by Sapphire & Moveer Bot &bull; {now_str}
    </footer>

    <script>
        function filterMessages() {{
            const query = document.getElementById('searchInput').value.toLowerCase().trim();
            const cards = document.querySelectorAll('.message-card');
            cards.forEach(card => {{
                const author = card.getAttribute('data-author') || '';
                const content = card.getAttribute('data-content') || '';
                if (!query || author.includes(query) || content.includes(query)) {{
                    card.style.display = 'flex';
                }} else {{
                    card.style.display = 'none';
                }}
            }});
        }}
    </script>
</body>
</html>
'''
    return html_document
