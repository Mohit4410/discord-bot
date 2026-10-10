# 🚀 Moveer & Sapphire All-in-One Discord Bot

A modular, high-performance Discord bot written in **Python (discord.py 2.x)** and powered by asynchronous **SQLite (`aiosqlite`)**, bringing together **all the voice-moving capabilities of Moveer** and **all the server management, logging, moderation, and community features of Sapphire**.

Supports both modern **Slash Commands (`/`)** and customizable **Prefix Commands (`!`)** with per-server dynamic configuration.

---

## 🌟 Feature Summary

### 🔊 1. Moveer Voice Suite
- **Single User Move:** `/move @user #channel`
- **Channel Move (cmove):** `/cmove #source #target` — moves everyone from channel A to B.
- **Force / Role Move (fmove):** `/fmove @role #channel` — moves all users with a role in any voice channel to target channel.
- **Channel Role Move (rmove):** `/rmove @role #channel` — moves users with a role from your current voice channel to target channel.
- **Drag & Mass Drag:** `/drag @user` pulls a user to your channel; `/massdrag #channel` pulls an entire voice channel into yours.
- **Spread:** `/spread #ch1, #ch2` — distributes users from your channel evenly across multiple channels (ideal for scrims/teams).
- **Game Move (gmove):** `/gmove "Valorant" #channel` — moves all voice users currently playing a specific game.
- **Random Move:** `/randommove #channel` — picks a random user from your channel and moves them.
- **Voice Disconnect / Kick:** `/vkick @user` and `/massvkick #channel`.
- **Server Mute / Deafen:** `/vmute`, `/vunmute`, `/vdeaf`, `/vundeaf`.
- **Dynamic Join-to-Create Voice Channels:** Auto-creates temporary voice rooms with interactive button controls (🔒 Lock/Unlock, ✏️ Rename, 👥 User Limit) that delete themselves when empty.

### 🛡️ 2. Sapphire Moderation & Auto-Mod
- **Punishment Commands:** `/ban`, `/unban`, `/kick`, `/timeout` (e.g. `10m`, `2h`, `1d`), `/untimeout`, `/warn`, `/warnings`, `/clearwarns`.
- **Channel Management:** `/purge [amount] [user]`, `/slowmode [seconds]`, `/lock`, `/unlock`, `/nick`.
- **Automated Protections (Auto-Mod):**
  - **Anti-Invite:** Blocks Discord invite links.
  - **Anti-Link:** Blocks external URLs.
  - **Anti-Spam:** Prevents rapid-fire message spamming.
  - **Anti-Caps:** Auto-detects and removes messages exceeding caps percentage threshold.
  - **Custom Bad Words Filter:** Blacklist words with add/remove management.
  - **Configurable Action:** Choose between `delete`, `warn`, or `timeout`.

### 📋 3. Sapphire Audit Logging
Comprehensive categorization into dedicated channels:
- **Message Logs:** Edits, deletions, attachment tracking, jump-to-message links.
- **Voice Logs:** Member joined, left, switched channels, or was server muted/deafened.
- **Member Logs:** Joins (with account creation age), leaves, nickname changes, role additions/removals.
- **Channel & Role Logs:** Created, deleted, and modified roles/channels.
- Configure via `/logs set [category] #channel` or view with `/logs status`.

### 🎭 4. Roles, Welcome, Leveling & Community
- **Interactive Button Roles:** Clickable buttons on embeds allowing users to self-assign roles.
- **Autoroles:** Automatically assign roles to newcomers upon joining.
- **Welcome & Goodbye Cards:** Custom placeholders (`{user}`, `{user_mention}`, `{server}`, `{member_count}`) with embeds.
- **Leveling & XP System:** Text activity XP, level progression, visual text progress bar rank cards (`/rank`), and server leaderboard (`/leaderboard`).
- **Custom Commands & Auto-Responders:** Keyword triggers (`exact`, `contains`, `startswith`) replying with dynamic placeholders.
- **Starboard:** Highlights top community messages with customizable star threshold.
- **Utilities:** `/serverinfo`, `/userinfo`, `/avatar`, `/poll`, `/remind` (background timed notifications), `/ping`, `/botinfo`.

---

## 🛠️ Installation & Setup

### 1. Requirements
- **Python 3.10+** (Tested on Python 3.12)
- Dependencies: `pip install -r requirements.txt`

### 2. Configure Discord Bot Application
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give your bot a name.
3. In the left menu, select **Bot**:
   - Click **Reset Token** and copy the bot token.
   - Under **Privileged Gateway Intents**, enable:
     - ✅ **Presence Intent** (required for `/gmove` and user status)
     - ✅ **Server Members Intent** (required for autoroles, welcome messages, logging, role moves)
     - ✅ **Message Content Intent** (required for prefix commands, automod, custom triggers, leveling XP)
4. Go to **OAuth2** -> **URL Generator**:
   - Check `bot` and `applications.commands`.
   - Select permissions: `Administrator` (recommended for full Moveer voice & moderation features) or individually select permissions (`Move Members`, `Mute Members`, `Deafen Members`, `Manage Roles`, `Manage Channels`, `Kick Members`, `Ban Members`, `Moderate Members`, `Manage Messages`, `Send Messages`, `Embed Links`, `Read Message History`, etc.).
   - Copy the invite link and add the bot to your server.

### 3. Setup `.env`
In this project folder, edit the [`.env`](file:///d:/tools/logs%20bot/.env) file:
```env
DISCORD_TOKEN=your_bot_token_here
BOT_PREFIX=!
```

### 4. Run the Bot
Double-click `start.bat` or run:
```powershell
python bot.py
```

---

## 📖 Command Reference

### Moveer Commands
| Command | Arguments | Description |
|---|---|---|
| `/move` | `user`, `channel` | Move a user to a voice channel |
| `/cmove` | `from_channel`, `to_channel` | Move all users from channel A to B |
| `/fmove` | `role`, `channel` | Move all members with role across any voice channel to target |
| `/rmove` | `role`, `channel` | Move role members from your voice channel to target |
| `/drag` | `user` | Pull a user into your current voice channel |
| `/massdrag` | `from_channel` | Pull all members from another channel into yours |
| `/spread` | `channels` | Distribute members evenly across comma-separated channels |
| `/gmove` | `game`, `channel` | Move everyone playing a specific game to a channel |
| `/randommove`| `channel` | Move a random user from your channel to target |
| `/vkick` | `user` | Disconnect a user from voice |
| `/massvkick`| `channel` | Disconnect everyone from a voice channel |
| `/vmute` / `/vunmute` | `user` | Server mute or unmute a user in voice |
| `/vdeaf` / `/vundeaf` | `user` | Server deafen or undeafen a user in voice |
| `/tempvoice setup` | `[category]`, `[channel_name]` | Create Join-to-Create dynamic temporary voice rooms |
| `/tempvoice disable`| — | Disable Join-to-Create voice channels |

### Sapphire Moderation Commands
| Command | Arguments | Description |
|---|---|---|
| `/ban` | `user`, `[reason]`, `[delete_message_days]` | Ban a user |
| `/unban` | `user_id`, `[reason]` | Unban a user by their ID |
| `/kick` | `user`, `[reason]` | Kick a user |
| `/timeout` | `user`, `duration`, `[reason]` | Timeout user (e.g. `10m`, `2h`, `1d`) |
| `/untimeout` | `user`, `[reason]` | Remove timeout from user |
| `/warn` | `user`, `[reason]` | Issue a formal warning |
| `/warnings` | `user` | View warning history of a user |
| `/clearwarns`| `user` | Clear all warnings of a user |
| `/purge` | `amount`, `[user]` | Purge 1-100 messages (automatically saved for `/restore`) |
| `/restore` | `[amount]` | **Killer Feature:** Restore purged messages via webhooks with the original user's name and avatar (NQN / Tupperbox style) |
| `/slowmode` | `seconds` | Set slowmode delay |
| `/lock` / `/unlock` | `[channel]`, `[reason]` | Lock or unlock a text channel |
| `/nick` | `user`, `[nickname]` | Change a member's nickname |

### Auto-Mod Commands
| Command | Arguments | Description |
|---|---|---|
| `/automod status` | — | View all current AutoMod settings |
| `/automod antiinvite` | `enabled: True/False` | Block Discord invite links |
| `/automod antilink` | `enabled: True/False` | Block external hyperlinks |
| `/automod antispam` | `enabled: True/False` | Prevent rapid message spamming |
| `/automod anticaps` | `enabled: True/False`, `[threshold]` | Filter excessive uppercase letters |
| `/automod badwords` | `action: add/remove/list`, `[word]` | Manage custom prohibited words |
| `/automod action` | `punishment: delete/warn/timeout` | Punishment applied on violation |

### Audit Logging Commands
| Command | Arguments | Description |
|---|---|---|
| `/logs setup` | `[category_name]` | **Auto-Setup:** Automatically creates a private category, creates 5 dedicated channels (`message-logs`, `voice-logs`, `member-logs`, `mod-logs`, `server-logs`), and links them to the database |
| `/logs set` | `category`, `channel` | Set channel for `message`, `voice`, `member`, `mod`, `server`, or `all` |
| `/logs status` | — | View all configured logging channels |

### Roles, Welcome, Leveling & Customization
| Command | Arguments | Description |
|---|---|---|
| `/buttonrole` | `role`, `label`, `[channel]`, `[style]`, `[emoji]` | Create interactive self-role button message |
| `/autorole add` / `remove` / `list` | `role` | Configure roles automatically assigned on join |
| `/welcome set` | `channel`, `[message]` | Setup welcome card with placeholders |
| `/welcome disable` / `test` | — | Disable or test welcome card |
| `/leave set` / `disable` | `channel`, `[message]` | Setup goodbye messages |
| `/rank` | `[user]` | Visual XP rank card with progress bar |
| `/leaderboard` | — | Server XP top 10 rankings |
| `/leveling toggle` / `channel` | — | Configure leveling system |
| `/customcommand add` | `trigger`, `response`, `[match_type]` | Add custom keyword auto-response |
| `/customcommand remove` / `list` | `trigger` | Manage custom commands |
| `/starboard set` | `channel`, `[threshold]` | Setup Starboard channel and ⭐ threshold |
| `/starboard disable` | — | Turn off Starboard |
| `/remind` | `duration`, `text` | Timed reminder (e.g. `10m Buy groceries`) |
| `/poll` | `question`, `options` | Create multi-choice interactive poll |
| `/prefix` | `[new_prefix]` | View current prefix or change it directly (e.g. `!prefix ?` or `/prefix set ?`) |
| `/setprefix` | `new_prefix` | Change text command prefix for this server (shortcut) |

---

## 🗄️ Database Architecture
The bot uses an asynchronous local SQLite database (`bot_database.sqlite`) which is automatically created and migrated on first launch:
- `guild_settings`: Dynamic prefixes, welcome/leave toggles, starboard config, temp voice config, leveling toggles.
- `logging_config`: Categorized channel mapping for message, voice, member, mod, and server logs.
- `automod_config`: Anti-invite, anti-link, anti-spam, anti-caps, bad words, and punishment actions.
- `warnings`: User disciplinary log with timestamp and moderator ID.
- `levels`: Persistent XP, level counts, message counts, and cooldown tracker.
- `button_roles` & `autoroles`: Persistent button views restored automatically across bot restarts.
- `temp_voice_channels`: Tracks dynamic voice rooms and auto-cleans on empty.
- `custom_commands`: Keyword auto-responders.
- `starboard_messages`: Message tracking and star counters.
- `reminders`: Dispatched through a background task loop.
