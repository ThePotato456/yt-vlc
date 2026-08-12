# yt-vlc

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![yt-dlp 2026.07.04](https://img.shields.io/badge/yt--dlp-2026.07.04-FF0000?logo=youtube&logoColor=white)
![Deno 2.8.1](https://img.shields.io/badge/Deno-2.8.1-000000?logo=deno&logoColor=white)
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
- Uses combined streams only at 720p or better, otherwise preferring up to 1080p
- Replaces the currently playing VLC item when the script is run again
- Provides a colorized, in-place status line that keeps terminal output compact
- Displays live progress bars while downloading prerequisites
- Animates media resolution with a Braille spinner
- Shows title, creator, duration, quality, codecs, and estimated media size
- Supports the full yt-dlp format-selector syntax
- Downloads pinned Windows builds of yt-dlp, Deno, and portable VLC automatically
- Enables yt-dlp's JavaScript challenge solver for reliable YouTube extraction
- Reuses local tools after the first run
- Accepts custom yt-dlp, Deno, and VLC executable paths
- Can print resolved stream URLs without opening VLC
- Prevents accidental playlist expansion

## Requirements

- Windows 10 or later
- Python 3.10 or later
- Internet access during initial setup and stream resolution

No manual yt-dlp, JavaScript runtime, or VLC installation is required. On first use, the script
creates the following local structure:

```text
bin/
├── deno.exe
├── yt-dlp.exe
└── vlc/
    ├── vlc.exe
    ├── libvlc.dll
    ├── libvlccore.dll
    └── plugins/
```

The bundled versions are pinned to yt-dlp `2026.07.04`, Deno `2.8.1`, and VLC `3.0.23`.
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
3. Invite the bot with **View Channels**, **Send Messages**,
   **Read Message History**, and **Manage Messages** permissions. Manage
   Messages lets it remove sensitive debrid requests posted publicly.
4. Install the Python dependency and create the local configuration:

   ```powershell
   python -m pip install -r .\requirements.txt
   Copy-Item .\.env.example .\.env
   ```

5. Add the bot token to `.env`. If the bot belongs to exactly one server,
   owner-only DM controls target it automatically. Set `DISCORD_GUILD_ID` when
   the bot belongs to multiple servers. Optionally set
   `DISCORD_REQUEST_CHANNEL_ID` for one server request channel. Logging defaults
   to `INFO` in `logs/discord_bot.log`; use `DISCORD_LOG_LEVEL` to change the
   verbosity or set `DISCORD_LOG_FILE=off` for console-only output. Bot-launched
   VLC defaults to DirectSound for more reliable Discord application-audio
   capture; set `VLC_AUDIO_OUTPUT` to `waveout`, `mmdevice`, or `automatic` to
   choose another VLC output module.
6. Start the bot:

   ```powershell
   python .\discord_bot.py
   ```

   Once Discord connects, the bot prepares its bundled tools and opens an idle
   VLC window with the local controller ready. The first request can therefore
   hand off media without waiting for VLC to launch.

Request media from Discord:

```text
!play https://www.youtube.com/watch?v=VIDEO_ID
```

Playback commands:

| Command | Behavior |
|---|---|
| `!play <URL>`, `!p <URL>` | Add a media URL to the lazy queue |
| `!pause` | Pause the current item |
| `!resume` | Resume the paused item |
| `!skip`, `!s` | Stop the current item and advance to the next request |
| `!seek <[+/-]seconds\|[+/-]MM:SS\|[+/-]HH:MM:SS>` | Seek absolutely, or move relative to the current time with `+`/`-` |
| `!stop` | Stop playback and clear pending requests without closing VLC |
| `!queue`, `!q` | Show bot requests plus VLC's live playlist and active item |

Seek positions may be written as raw seconds (`!seek 90`), minutes and seconds
(`!seek 01:30`), or hours, minutes, and seconds (`!seek 1:02:30`). Seeking
beyond the current media duration is rejected. Prefix a value with `+` to move
forward from the current playback time, such as `!seek +10`, `!seek +60`, or
`!seek +05:00`. Prefix it with `-` to rewind, such as `!seek -10`, `!seek -60`,
or `!seek -05:00`. Rewinding past the beginning lands at `0:00`. Values without
a modifier remain absolute positions. `!skip` retains its queue-control behavior
and always advances to the next request.

`!queue` reads both the bot's lazy request queue and VLC's own live playlist.
Files or network media added manually through VLC therefore appear even when
they did not originate from `!play`, and VLC's actual active item is marked
**Now playing**. Local folder paths and raw signed stream URLs are not exposed
in Discord. `!seek` also operates on VLC's actual active item, so manually
added files do not need a corresponding bot request.

The commands work in the configured server and in direct messages from the
Discord application owner. DM commands from every other account are rejected.
When the bot belongs to one server, owner DMs automatically share that server's
queue and VLC player.
Queue displays and failure messages redact credential-bearing links from TorBox,
Real-Debrid, AllDebrid, Premiumize, and similar debrid services. Ordinary social
media and YouTube links remain visible.
When a sensitive link is submitted in a server channel, the bot deletes the
original command before queuing it. It refuses the request if deletion fails.
Messages sent directly to the bot are already private and are not deleted.

When yt-dlp reports a login, private-media, HTTP 401/403, or bot-verification
failure, the failed request receives a **Retry with cookies.txt** button. Only
the original requester can use it. The bot opens a DM and accepts one
Netscape-format `.txt` cookie export within 60 seconds, up to 2 MiB. The upload
is validated, filtered to domains associated with the requested site, and the
Discord upload is deleted immediately. The retry returns to the lazy queue, so
the media is not resolved until it reaches the front.

Retry cookies are never installed as the bot's saved cookies. They remain in
memory while queued, are written with private permissions to a temporary file
only while yt-dlp resolves that retry, and are deleted afterward. If Discord
cannot delete the uploaded DM, the bot warns the requester to remove it
manually.

Authenticated extraction can expose a different set of formats than the public
attempt. Every resolution uses the bundled Deno runtime for yt-dlp's YouTube
JavaScript challenge solver. If the normal quality selector is genuinely
unavailable after cookie upload, the bot retries once with a flexible selector,
preferring streams up to 1080p before allowing yt-dlp's broader compatibility
fallbacks. The same temporary cookie file is used for both attempts and then
removed.

Each server has an independent in-memory `GuildState`. Requests enter as raw
URLs. To shorten transitions without letting signed links age in the queue, the
bot prepares only the next request when VLC reaches the final eight seconds of
the current item. It never starts that item early. The bot keeps one VLC window
open through a password-protected localhost interface, so a Discord screen share
stays attached between requests. Reaching the end, pressing VLC's Stop button,
or using `!skip` advances the queue; `!stop` clears the pending queue instead.

When the bot connects, it waits up to 30 seconds for VLC's local controller to
become ready before reporting warm-up completion in the log. If VLC opens
without a working controller, the bot closes only that failed instance and
retries once with fresh credentials and a new local port. A healthy session is
reused across Discord reconnects and playback requests without restarting it.
When the bot can see multiple guilds, set `DISCORD_GUILD_ID` so startup knows
which guild state owns the single screen-shared VLC window.

The console and rotating log file record bot startup, queue changes, resolution,
VLC initialization and recovery, playback controls, and errors with tracebacks.
Each log file is capped at 5 MiB with three backups. Sensitive TorBox,
Real-Debrid, and similar debrid URLs are redacted from both normal entries and
exception tracebacks.

Discord screen-share audio uses VLC's `directsound` output by default. This
avoids the automatic Windows multimedia output path that can become distorted
for capture listeners even while local playback remains clear. If a particular
Windows audio driver still crackles, set `VLC_AUDIO_OUTPUT=waveout` in `.env`
and restart the bot. This setting changes only audio delivery to Windows and
Discord; it does not change media quality, formats, or network caching.

If playback time stops advancing for 12 seconds, the bot re-resolves the current
request once with a combined-first 720p fallback and attempts to resume near the
previous timestamp. Recovery is limited to one attempt so a bad source cannot
loop indefinitely.

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
| `b[height>=720][height<=1080]/bv*[height<=1080]+ba/b[height<=1080]/b` | Combined at 720p+, otherwise separate streams up to 1080p |
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

The default selector is
`b[height>=720][height<=1080]/bv*[height<=1080]+ba/b[height<=1080]/b`.

## Command-line options

```text
usage: yt_vlc.py [-h] [-f FORMAT] [--yt-dlp PATH] [--deno PATH]
                 [--vlc PATH] [--print-only] [url]
```

| Option | Description |
|---|---|
| `url` | Media-page URL; prompted for when omitted |
| `-f`, `--format FORMAT` | yt-dlp format selector |
| `--yt-dlp PATH` | Override the bundled yt-dlp executable |
| `--deno PATH` | Override the bundled Deno JavaScript runtime |
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
  --deno "C:\Tools\deno.exe" `
  --vlc "C:\Program Files\VideoLAN\VLC\vlc.exe" `
  "URL"
```

## How it works

1. The script ensures its pinned tools are available under `./bin`.
2. yt-dlp resolves the supplied page using `--no-playlist`, the selected
   format, `-g`, and the bundled Deno runtime for JavaScript challenges.
3. A single URL is opened directly in VLC. If VLC is already running, its
   current item is replaced instead of opening another player window.
4. If two URLs are returned, the first is treated as video and the second as
   audio and attached through VLC's item-specific `:input-slave` option.

The script passes `--one-instance` and `--no-playlist-enqueue` to VLC. This
makes repeat runs behave like media requests: the new item starts immediately
in the existing player rather than being queued or opened in another window.
