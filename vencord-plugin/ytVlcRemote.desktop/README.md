# YtVlcRemote Vencord userplugin

Windows-only Vencord userplugin for Discord Canary. It exposes an authenticated
REST service on IPv4 loopback and controls the logged-in account's guild voice
connection and exact VLC application stream.

The native process verifies that every stream PID belongs to the requested
`vlc.exe`, resolves that process's main HWND, and requires a matching Electron
window capture source. It never falls back to a display source.

## Install for development

Link or copy this complete directory to a current Vencord checkout as:

```text
Vencord/src/userplugins/ytVlcRemote.desktop/
```

Then run Vencord's normal desktop build and injection flow against Discord
Canary. Enable **YtVlcRemote** in Vencord settings and restart Canary. Copy the
generated token with the plugin's **Copy token** control.

Changing the port requires restarting Canary. Regenerating the token takes
effect immediately and invalidates the previous token.

## REST contract

The default base URL is `http://127.0.0.1:38423`. `/v1/health` is intentionally
unauthenticated; all other endpoints require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health` | Native bridge liveness |
| `GET` | `/v1/status` | Confirmed voice and stream state |
| `GET` | `/v1/capture-sources` | Window-only capture source metadata |
| `PUT`, `DELETE` | `/v1/voice` | Join/move/configure or leave guild voice |
| `PUT`, `DELETE` | `/v1/stream` | Start/replace or stop exact VLC sharing |
| `PUT` | `/v1/session` | Join/move, mute/deafen, wait 1.5 seconds for voice state to settle, and share VLC atomically |

Mutations are serialized and wait for Discord store confirmation. The queue is
bounded, request bodies are limited to 16 KiB, browser Origin requests and
unexpected Host headers are rejected, and returned failures are sanitized.

The combined session payload is:

```json
{
  "guild_id": "123456789012345678",
  "channel_id": "234567890123456789",
  "self_mute": true,
  "self_deaf": true,
  "stream": {
    "pid": 4321,
    "executable_path": "C:\\path\\to\\vlc.exe",
    "audio": true,
    "resolution": 720,
    "fps": 30
  }
}
```

Only ordinary guild voice channels and audio-enabled 720p/30 FPS requests are
accepted. The adapter asks Discord for that quality and reports
`quality_unavailable` if Discord does not accept it; it does not modify or
bypass account entitlements.
