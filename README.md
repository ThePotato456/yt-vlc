# yt-vlc

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![yt-dlp 2026.07.04](https://img.shields.io/badge/yt--dlp-2026.07.04-FF0000?logo=youtube&logoColor=white)
![Deno 2.8.1](https://img.shields.io/badge/Deno-2.8.1-000000?logo=deno&logoColor=white)
![VLC 3.0.23](https://img.shields.io/badge/VLC-3.0.23-FF8800?logo=vlcmediaplayer&logoColor=white)

Stream online media directly in VLC with a polished Windows CLI and an optional
Discord request bot.

`yt-vlc` uses `yt-dlp -g` to resolve a media page, then hands the resulting
network stream URLs to VLC. Separate video and audio streams are supported
without downloading the complete media file first.

## Screenshots

### Stream resolution and playback

![yt-vlc media information and VLC handoff](docs/screenshots/playback.png)

### First-run dependency setup

![yt-vlc prerequisite download progress](docs/screenshots/first-run-setup.png)

## Highlights

- Resolves media supported by yt-dlp and opens it directly in VLC
- Supports combined streams and separate video/audio streams
- Prefers combined video at 720p or better, then separate streams up to 1080p
- Replaces the current VLC item when the CLI is run again
- Displays a compact status interface, download progress, and media metadata
- Downloads pinned portable builds of yt-dlp, Deno, and VLC on first use
- Uses Deno for yt-dlp's YouTube JavaScript challenge handling
- Provides an optional Discord bot with a lazy request queue
- Controls VLC playback, seeking, local files, and the native VLC playlist
- Keeps one VLC window open so a Discord application share remains attached
- Redacts sensitive debrid URLs from Discord output and logs

## Requirements

- Windows 10 or later
- Python 3.10 or later
- Internet access for initial setup and network playback

The CLI uses only the Python standard library. The Discord bot additionally
requires the package listed in `requirements.txt`.

## Installation

Clone the repository and enter the project directory:

```powershell
git clone https://github.com/ThePotato456/yt-vlc.git
Set-Location .\yt-vlc
```

Creating a virtual environment is recommended:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
```

On first launch, the program downloads its pinned Windows dependencies into
`./bin`:

```text
bin/
|-- deno.exe
|-- yt-dlp.exe
`-- vlc/
    |-- vlc.exe
    |-- libvlc.dll
    |-- libvlccore.dll
    `-- plugins/
```

Existing files are reused on later runs. The `bin/` directory is excluded from
Git.

## CLI usage

Pass a media-page URL:

```powershell
python .\yt_vlc.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Run without a URL to enter one interactively:

```powershell
python .\yt_vlc.py
```

### Options

```text
usage: yt_vlc.py [-h] [-f FORMAT] [--yt-dlp PATH] [--deno PATH]
                 [--vlc PATH] [--print-only] [url]
```

| Option | Description |
|---|---|
| `url` | Media-page URL; prompted for when omitted |
| `-f`, `--format FORMAT` | yt-dlp format selector |
| `--yt-dlp PATH` | Use a specific yt-dlp executable |
| `--deno PATH` | Use a specific Deno executable |
| `--vlc PATH` | Use a specific VLC executable |
| `--print-only` | Print resolved stream URLs without opening VLC |
| `-h`, `--help` | Show the full colorized help message |

Examples:

```powershell
# Print the resolved stream URL or URLs
python .\yt_vlc.py --print-only "URL"

# Select exact video and audio format IDs
python .\yt_vlc.py -f "137+140" "URL"

# Use existing tool installations
python .\yt_vlc.py `
  --yt-dlp "C:\Tools\yt-dlp.exe" `
  --deno "C:\Tools\deno.exe" `
  --vlc "C:\Program Files\VideoLAN\VLC\vlc.exe" `
  "URL"
```

## Format selection

Available formats depend on the media and extractor. Inspect them with:

```powershell
.\bin\yt-dlp.exe -F "URL"
```

Then pass a yt-dlp selector with `-f` or `--format`:

```powershell
python .\yt_vlc.py -f "bv*[height<=1080]+ba/b[height<=1080]" "URL"
```

| Selector | Behavior |
|---|---|
| `best` or `b` | Best combined video and audio stream |
| `bestvideo+bestaudio` or `bv+ba` | Best separate video and audio streams |
| `bestvideo*+bestaudio/best` | Best quality with a combined fallback |
| `bv*[height<=1080]+ba/b[height<=1080]` | Best stream up to 1080p |
| `bv*[height<=720]+ba/b[height<=720]` | Best stream up to 720p |
| `bestaudio` or `ba` | Best audio-only stream |
| `bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]` | Prefer MP4 video and M4A audio |
| `137+140` | Combine exact format IDs reported by `-F` |

The default selector is:

```text
b[height>=720][height<=1080]/bv*[height<=1080]+ba/b[height<=1080]/b
```

It prefers a combined stream between 720p and 1080p. If one is unavailable,
it selects separate video and audio streams up to 1080p, followed by broader
compatibility fallbacks.

## Discord bot

The optional Discord bot turns the VLC instance into a requestable player. It
maintains a lazy URL queue, exposes playback controls, and can browse trusted
local media beneath `./media`.

### Setup

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent** on the bot page.
3. Invite the bot with these permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Manage Messages
4. Copy the example configuration:

   ```powershell
   Copy-Item .\.env.example .\.env
   ```

5. Add your Discord bot token to `.env`:

   ```dotenv
   DISCORD_BOT_TOKEN=
   ```

6. Start the bot:

   ```powershell
   .\start.bat
   ```

   You can also run `python .\discord_bot.py` from an activated environment.

At startup, the bot prepares its bundled tools and opens an idle VLC window.
Share that VLC application in Discord once; the same window remains open across
requests and playlist changes.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Required | Discord bot token |
| `DISCORD_GUILD_ID` | Auto when only one guild is visible | Guild controlled by owner-only DM commands and the shared VLC instance |
| `DISCORD_REQUEST_CHANNEL_ID` | Any allowed guild channel | Restrict guild commands to one channel |
| `DISCORD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `DISCORD_LOG_FILE` | `logs/discord_bot.log` | Rotating log path; use `off` for console-only logging |
| `VLC_AUDIO_OUTPUT` | `directsound` | VLC audio module: `directsound`, `waveout`, `mmdevice`, or `automatic` |

Never commit `.env`. It is excluded by the repository's `.gitignore`.

### Commands

| Command | Behavior |
|---|---|
| `!play <URL> [URL ...]`, `!p <URL> [URL ...]` | Add one or more media URLs to the lazy queue in order |
| `!local`, `!localqueue`, `!media` | Browse and queue files beneath `./media` |
| `!pause` | Pause the active VLC item |
| `!resume` | Resume the active VLC item |
| `!skip`, `!s` | Advance to the next VLC playlist item or queued request |
| `!seek <position>` | Seek absolutely or relative to the current time |
| `!stop` | Stop playback and clear pending bot requests without closing VLC |
| `!clear`, `!clearplaylist` | Clear the VLC playlist and all bot requests without closing VLC |
| `!queue`, `!q` | Show the bot queue and VLC's live playlist |

Seek values accept seconds, `MM:SS`, or `HH:MM:SS`:

```text
!seek 90       # absolute: 1:30
!seek 01:30    # absolute: 1:30
!seek +10      # forward 10 seconds
!seek -05:00   # back 5 minutes
```

Playback commands operate on VLC itself, so pause, resume, seek, queue, and
clear also work with files added manually through VLC.

Queue multiple links in one command by separating them with spaces. The bot
validates the complete batch first, then adds up to 25 links in the exact order
provided:

```text
!play https://example.com/first https://example.com/second https://example.com/third
```

### Local media

Run `!local` to open a requester-scoped browser rooted at `./media`. The bot
creates this directory when needed and supports common video and audio formats,
including MP4, MKV, WebM, MOV, AVI, MP3, M4A, FLAC, WAV, OGG, and Opus.

The browser supports:

- Folder navigation and pagination
- Queueing individual files
- Queueing an entire folder recursively in one VLC operation
- Appending to VLC without replacing its active playlist
- Starting the first new item automatically when VLC is idle

Every selected path is resolved beneath `./media` before use. Paths that escape
the directory are rejected, and absolute local paths are not shown in Discord.
The directory is excluded from Git to prevent accidental media commits.

### Queue behavior

Each guild has an independent in-memory request state. URLs remain unresolved
until they reach the front of the queue, which prevents signed stream URLs from
expiring while they wait. Near the end of the active item, the bot may prepare
only the next request to reduce transition time.

`!queue` combines the bot's pending requests with VLC's native playlist, so it
also reflects files or streams added outside Discord. Discord embeds are
truncated safely when a playlist is too long for one message.

## Privacy and security

- `.env`, logs, downloaded tools, local media, and Python environments are
  excluded from Git.
- VLC's control interface binds to `127.0.0.1` and uses a newly generated
  password for each bot launch.
- Owner-only DM commands can control the configured guild; DMs from other users
  are rejected.
- Credential-bearing TorBox, Real-Debrid, AllDebrid, Premiumize, and similar
  URLs are redacted from Discord responses, queue displays, and logs.
- Public commands containing a sensitive link are deleted before the request is
  accepted. The request is rejected if deletion fails.
- Cookie retries accept one requester-scoped Netscape `cookies.txt` upload in
  DM. It is filtered to the requested service and used for one retry. The bot
  deletes the upload immediately when Discord permits it and warns the requester
  if manual deletion is required.

Cookie files and authenticated stream URLs are sensitive. Use a dedicated bot,
limit its permissions to the intended server, and rotate credentials if they
are ever exposed.

## How it works

1. The program ensures the pinned tools exist beneath `./bin`.
2. yt-dlp resolves the page with `--no-playlist`, `-g`, the selected format,
   and the bundled Deno runtime.
3. A combined stream is opened directly in VLC, or separate audio is attached
   to the video through VLC's item-specific `input-slave` option.
4. CLI launches reuse the existing VLC instance and replace its current item.
5. The Discord bot instead keeps a dedicated VLC process and controls its
   playlist through VLC's password-protected local HTTP interface.

## Troubleshooting

### The requested format is unavailable

List the source's formats with `yt-dlp -F`, then choose a compatible selector
with `-f`. Availability can differ for public and authenticated extraction.

### VLC opens but the Discord bot cannot control it

The bot waits for VLC's local interface and retries initialization once with a
new port and password. Check `logs/discord_bot.log` for the underlying error and
confirm no security software is blocking localhost connections.

### Discord viewers hear distorted audio

The bot defaults to DirectSound for application-audio capture. If distortion
continues, set this in `.env` and restart the bot:

```dotenv
VLC_AUDIO_OUTPUT=waveout
```

### Playback stalls

If playback time stops advancing for 12 seconds, the bot makes one recovery
attempt with a combined-first 720p compatibility selector and resumes near the
previous timestamp when possible.

## Development

Run the test suite from the repository root:

```powershell
python -m unittest tests.test_discord_bot
```

The current suite covers queue behavior, VLC control, seeking, local media,
cookie retry handling, sensitive-link redaction, and playback recovery.

## Contributing

Bug reports and focused pull requests are welcome. Please describe the playback
source, expected behavior, actual behavior, and relevant redacted log output.
Never include authentication cookies, bot tokens, signed media URLs, or private
local paths in an issue.

## Responsible use

This project is a playback helper around yt-dlp and VLC. Supported sites and
formats can change over time. Use it only with media you are authorized to
access, and follow the applicable service terms and local laws.
