# Discord Music Bot

Bot Discord phát nhạc YouTube, hỗ trợ cả slash commands (`/`) và prefix commands (`!`).

## Features

| Command | Slash | Prefix | Description |
|---------|-------|--------|-------------|
| play | `/play <query>` | `!play <query>` | Phát nhạc từ YouTube (URL hoặc tìm kiếm) |
| pause | `/pause` | `!pause` | Tạm dừng bài đang phát |
| resume | `/resume` | `!resume` | Tiếp tục phát |
| skip | `/skip` | `!skip` | Bỏ qua bài hiện tại |
| stop | `/stop` | `!stop` | Dừng phát và ngắt kết nối |
| queue | `/queue` | `!queue` | Xem danh sách chờ |
| nowplaying | `/nowplaying` | `!np` | Xem bài đang phát |
| volume | `/volume <0-100>` | `!vol <0-100>` | Chỉnh âm lượng |
| loop | `/loop` | `!loop` | Bật/tắt lặp bài |

## Requirements

- Python 3.10+
- FFmpeg
- Discord Bot Token

## Setup

### 1. Install FFmpeg

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows - download from https://ffmpeg.org/download.html
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Create Discord Bot

1. Truy cập [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → đặt tên → **Create**
3. Vào tab **Bot** → click **Reset Token** → copy token
4. Bật **Message Content Intent** trong phần **Privileged Gateway Intents**
5. Vào tab **OAuth2** → **URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Connect`, `Speak`, `Embed Links`
6. Copy URL và mời bot vào server

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and paste your bot token
```

### 5. Run the bot

```bash
python bot.py
```

## Docker (Optional)

```bash
docker build -t discord-music-bot .
docker run -d --env-file .env discord-music-bot
```

## Tech Stack

- [discord.py](https://discordpy.readthedocs.io/) — Discord API wrapper
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube audio extraction
- [FFmpeg](https://ffmpeg.org/) — Audio processing
- [PyNaCl](https://pynacl.readthedocs.io/) — Voice encryption
