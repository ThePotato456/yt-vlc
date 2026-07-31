# yt-vlc

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![yt-dlp 2026.07.04](https://img.shields.io/badge/yt--dlp-2026.07.04-FF0000?logo=youtube&logoColor=white)
![VLC 3.0.23](https://img.shields.io/badge/VLC-3.0.23-FF8800?logo=vlcmediaplayer&logoColor=white)

Stream online media in VLC without downloading the video first. `yt-vlc`
resolves a media page through `yt-dlp -g`, then sends the resulting network
stream URLs directly to VLC.

When a site provides separate video and audio streams, the script opens the
video as VLC's primary input and attaches the audio with `--input-slave`.

## Screenshots

### Stream resolution and playback

![yt-vlc media information and VLC handoff](docs/screenshots/playback.png)

### First-run dependency setup

![yt-vlc prerequisite download progress](docs/screenshots/first-run-setup.png)

## Features

- Plays supported media pages directly in VLC
- Handles combined streams and separate video/audio streams
- Replaces the currently playing VLC item when the script is run again
- Provides a colorized, in-place status line that keeps terminal output compact
- Displays live progress bars while downloading prerequisites
- Animates media resolution with a Braille spinner
- Shows title, creator, duration, quality, codecs, and estimated media size
- Supports the full yt-dlp format-selector syntax
- Downloads pinned Windows builds of yt-dlp and portable VLC automatically
- Reuses local tools after the first run
- Accepts custom yt-dlp and VLC executable paths
- Can print resolved stream URLs without opening VLC
- Prevents accidental playlist expansion

## Requirements

- Windows 10 or later
- Python 3.10 or later
- Internet access during initial setup and stream resolution

No manual yt-dlp or VLC installation is required. On first use, the script
creates the following local structure:

```text
bin/
├── yt-dlp.exe
└── vlc/
    ├── vlc.exe
    ├── libvlc.dll
    ├── libvlccore.dll
    └── plugins/
```

The bundled versions are pinned to yt-dlp `2026.07.04` and VLC `3.0.23`.
Existing files are reused on subsequent runs, and `bin/` is excluded from Git.

## Quick start

Open PowerShell in the project directory and pass a media-page URL:

```powershell
python .\yt_vlc.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Alternatively, start the script without a URL and paste one at the prompt:

```powershell
python .\yt_vlc.py
```

```text
Media URL: https://www.youtube.com/watch?v=VIDEO_ID
```

To use a PowerShell variable:

```powershell
$URL = "https://www.youtube.com/watch?v=VIDEO_ID"
python .\yt_vlc.py $URL
```

## Discord requests

The optional Discord bot accepts `!play <URL>` and adds the request to a lazy
playback queue. Run the bot on the same logged-in Windows session as VLC.

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. On the bot settings page, enable **Message Content Intent**.
3. Invite the bot with **View Channels**, **Send Messages**, and
   **Read Message History** permissions.
4. Install the Python dependency and create the local configuration:

   ```powershell
   python -m pip install -r .\requirements.txt
   Copy-Item .\.env.example .\.env
   ```

5. Add the bot token to `.env`. Set `DISCORD_GUILD_ID` for the intended server,
   and optionally set `DISCORD_REQUEST_CHANNEL_ID` for one request channel.
6. Start the bot:

   ```powershell
   python .\discord_bot.py
   ```

Request media from Discord:

```text
!play https://www.youtube.com/watch?v=VIDEO_ID
```

Each server has an independent in-memory `GuildState`. Requests enter the queue
as raw URLs; yt-dlp does not resolve a queued link until every earlier item has
finished playing. The bot keeps one VLC window open and controls it through a
password-protected localhost interface, so a Discord screen share stays attached
between requests. Reaching the end or pressing Stop advances the queue; the next
URL is resolved only at that point. A compact **Now playing** response is posted
only when that request reaches the front.

## Format selection

Available formats depend on the media and website. Inspect them with:

```powershell
.\bin\yt-dlp.exe -F "URL"
```

Pass a selector with `-f` or `--format`:

```powershell
python .\yt_vlc.py -f "bv*[height<=1080]+ba/b[height<=1080]" "URL"
```

| Selector | Behavior |
|---|---|
| `best` or `b` | Best combined video and audio stream |
| `bestvideo+bestaudio` or `bv+ba` | Best separate video and audio streams |
| `bestvideo*+bestaudio/best` | Best quality with a combined-stream fallback |
| `bv*[height<=1080]+ba/b[height<=1080]` | Best stream up to 1080p |
| `bv*[height<=720]+ba/b[height<=720]` | Best stream up to 720p |
| `bestaudio` or `ba` | Best audio-only stream |
| `worst` or `w` | Lowest-quality combined stream |
| `bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]` | Prefer MP4 video and M4A audio |
| `137+140` | Combine exact format IDs reported by `-F` |

Selector operators:

| Operator | Meaning |
|---|---|
| `+` | Combine separate streams |
| `/` | Fall back to the next selector |
| `[...]` | Filter by properties such as height, extension, or codec |

The default selector is `bestvideo*+bestaudio/best`.

## Command-line options

```text
usage: yt_vlc.py [-h] [-f FORMAT] [--yt-dlp PATH] [--vlc PATH]
                 [--print-only] [url]
```

| Option | Description |
|---|---|
| `url` | Media-page URL; prompted for when omitted |
| `-f`, `--format FORMAT` | yt-dlp format selector |
| `--yt-dlp PATH` | Override the bundled yt-dlp executable |
| `--vlc PATH` | Override the bundled VLC executable |
| `--print-only` | Print resolved URLs without launching VLC |
| `-h`, `--help` | Show detailed, colorized command help |

Examples:

```powershell
# Print the resolved network URL or URLs
python .\yt_vlc.py --print-only "URL"

# Select exact video and audio format IDs
python .\yt_vlc.py -f "137+140" "URL"

# Use existing executable installations
python .\yt_vlc.py `
  --yt-dlp "C:\Tools\yt-dlp.exe" `
  --vlc "C:\Program Files\VideoLAN\VLC\vlc.exe" `
  "URL"
```

## How it works

1. The script ensures its pinned tools are available under `./bin`.
2. yt-dlp resolves the supplied page using `--no-playlist`, the selected
   format, and `-g`.
3. A single URL is opened directly in VLC. If VLC is already running, its
   current item is replaced instead of opening another player window.
4. If two URLs are returned, the first is treated as video and the second as
   audio and attached through VLC's item-specific `:input-slave` option.

The script passes `--one-instance` and `--no-playlist-enqueue` to VLC. This
makes repeat runs behave like media requests: the new item starts immediately
in the existing player rather than being queued or opened in another window.
