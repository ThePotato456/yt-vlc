#!/usr/bin/env python3
"""Resolve a media page with yt-dlp and play its streams in VLC."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


DEFAULT_FORMAT = "bestvideo*+bestaudio/best"
APP_DIR = Path(__file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
YT_DLP_URL = (
    "https://github.com/yt-dlp/yt-dlp/releases/download/2026.07.04/yt-dlp.exe"
)
VLC_URL = "https://get.videolan.org/vlc/3.0.23/win64/vlc-3.0.23-win64.zip"
CHUNK_SIZE = 256 * 1024
METADATA_PREFIX = "__YTVLC_META__"
METADATA_FIELDS = (
    "title",
    "uploader",
    "channel",
    "duration",
    "duration_string",
    "resolution",
    "format_id",
    "ext",
    "fps",
    "vcodec",
    "acodec",
    "filesize_approx",
)

BANNER = r"""
 __   _______       __     ___     ____
 \ \ / /_   _|      \ \   / / |   / ___|
  \ V /  | |  _____  \ \ / /| |  | |
   | |   | | |_____|  \ V / | |__| |___
   |_|   |_|           \_/  |_____\____|
"""

_ui_lock = threading.Lock()


def supports_color(stream: object = sys.stdout) -> bool:
    """Return whether a stream supports interactive ANSI styling."""
    is_tty = getattr(stream, "isatty", lambda: False)()
    return (
        is_tty
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def colored(text: str, code: str, stream: object = sys.stdout) -> str:
    """Apply terminal color when supported by the destination stream."""
    return f"\033[{code}m{text}\033[0m" if supports_color(stream) else text


def ui(message: str = "", *, end: str = "\n") -> None:
    """Write user-interface output to stderr, preserving stdout for stream URLs."""
    with _ui_lock:
        print(message, end=end, file=sys.stderr, flush=True)


def clear_live() -> None:
    """Erase the current terminal line using portable carriage-return behavior."""
    if not sys.stderr.isatty():
        return
    width = shutil.get_terminal_size(fallback=(88, 24)).columns
    with _ui_lock:
        print("\r" + " " * max(0, width - 1) + "\r", end="", file=sys.stderr, flush=True)


def live(message: str) -> None:
    """Replace the current status line without adding to terminal history."""
    if not sys.stderr.isatty():
        ui(message)
        return
    width = shutil.get_terminal_size(fallback=(88, 24)).columns
    with _ui_lock:
        print(
            "\r" + " " * max(0, width - 1) + "\r" + message,
            end="",
            file=sys.stderr,
            flush=True,
        )


def finish_live(message: str) -> None:
    """Replace the live line and make its final state persistent."""
    clear_live()
    ui(message)


def print_banner() -> None:
    ui(colored(BANNER.rstrip(), "1;36", sys.stderr))
    ui(colored("       direct network playback via yt-dlp + VLC", "2", sys.stderr))
    ui()


def status(label: str, message: str, color: str = "36") -> None:
    clear_live()
    marker = colored(f"[{label}]", f"1;{color}", sys.stderr)
    ui(f"{marker} {message}")


def live_status(label: str, message: str, color: str = "36") -> None:
    marker = colored(f"[{label}]", f"1;{color}", sys.stderr)
    live(f"{marker} {message}")


class BrailleSpinner:
    """Animate a status line without delaying the work running on the main thread."""

    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, message: str) -> None:
        self.message = message
        self.started = 0.0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.enabled = sys.stderr.isatty()

    def __enter__(self) -> BrailleSpinner:
        if self.enabled:
            self.started = time.monotonic()
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()
        return self

    def _animate(self) -> None:
        frame = 0
        while not self.stop_event.is_set():
            glyph = colored(self.frames[frame % len(self.frames)], "1;35", sys.stderr)
            elapsed = time.monotonic() - self.started
            live(f"{glyph} {self.message} {elapsed:4.1f}s")
            frame += 1
            self.stop_event.wait(0.08)

    def __exit__(self, *_: object) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        clear_live()


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def human_duration(value: object) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return ""
    total_seconds = round(value)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def metadata_template() -> str:
    values = "\t".join(f"%({field})j" for field in METADATA_FIELDS)
    return f"video:{METADATA_PREFIX}{values}"


def parse_metadata(line: str) -> dict[str, object]:
    values = line.removeprefix(METADATA_PREFIX).split("\t")
    if len(values) != len(METADATA_FIELDS):
        return {}

    decoded: list[object] = []
    for value in values:
        try:
            decoded.append(json.loads(value))
        except json.JSONDecodeError:
            decoded.append(value)
    return dict(zip(METADATA_FIELDS, decoded))


def usable_metadata(value: object) -> bool:
    return value not in (None, "", "NA", "none", "None")


def show_media_info(info: dict[str, object]) -> None:
    """Render a compact media summary beneath the resolver result."""
    if not info:
        return

    title = info.get("title")
    if usable_metadata(title):
        status("MEDIA", str(title), "35")

    creator = info.get("uploader")
    if not usable_metadata(creator):
        creator = info.get("channel")

    resolution = info.get("resolution")
    fps = info.get("fps")
    quality = str(resolution) if usable_metadata(resolution) else ""
    if usable_metadata(fps):
        quality += f" @ {fps} fps" if quality else f"{fps} fps"

    format_parts = [
        str(info[field])
        for field in ("format_id", "ext")
        if usable_metadata(info.get(field))
    ]
    codecs = [
        str(info[field])
        for field in ("vcodec", "acodec")
        if usable_metadata(info.get(field)) and info.get(field) != "none"
    ]

    rows: list[tuple[str, str]] = []
    if usable_metadata(creator):
        rows.append(("creator", str(creator)))
    duration = human_duration(info.get("duration"))
    if not duration and usable_metadata(info.get("duration_string")):
        duration = str(info["duration_string"])
    if duration:
        rows.append(("duration", duration))
    if quality:
        rows.append(("quality", quality))
    if format_parts:
        rows.append(("format", " • ".join(format_parts)))
    if codecs:
        rows.append(("codecs", " + ".join(codecs)))
    size = info.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        rows.append(("size", f"~{human_size(int(size))}"))

    for label, value in rows:
        label_text = colored(label, "1;33", sys.stderr) + " " * (8 - len(label))
        ui(f"        {label_text} {value}")


def progress_line(name: str, downloaded: int, total: int | None) -> str:
    terminal_width = shutil.get_terminal_size(fallback=(88, 24)).columns
    bar_width = max(12, min(32, terminal_width - 52))
    if total and total > 0:
        fraction = min(downloaded / total, 1.0)
        filled = round(bar_width * fraction)
        bar = "#" * filled + "-" * (bar_width - filled)
        return (
            f"  {name:<12} [{bar}] {fraction:>6.1%}  "
            f"{human_size(downloaded):>9} / {human_size(total)}"
        )
    pulse = (downloaded // CHUNK_SIZE) % bar_width
    bar = "-" * pulse + ">" + "-" * (bar_width - pulse - 1)
    return f"  {name:<12} [{bar}]  {human_size(downloaded):>9}"


def format_help_text() -> str:
    heading = lambda value: colored(value, "1;36")
    selector = lambda value: colored(value, "1;33")
    command = lambda value: colored(value, "32")
    symbol = lambda value: colored(value, "1;35")

    return f"""{heading("format selection:")}
  Available formats depend on the video and website. List them with:
    {command('yt-dlp -F "URL"')}

  {heading("Common selectors:")}
    {selector("best, b")}                       Best combined video and audio stream
    {selector("bestvideo+bestaudio, bv+ba")}    Best separate video and audio streams
    {selector("bestvideo*+bestaudio/best")}     Best quality, with a combined fallback
    {selector("bv*[height<=1080]+ba/")}
      {selector("b[height<=1080]")}             Up to 1080p, with a combined fallback
    {selector("bv*[height<=720]+ba/")}
      {selector("b[height<=720]")}              Up to 720p, with a combined fallback
    {selector("bestaudio, ba")}                 Audio only
    {selector("worst, w")}                      Lowest-quality combined stream
    {selector("bv*[ext=mp4]+ba[ext=m4a]/")}
      {selector("b[ext=mp4]")}                  Prefer MP4 video and M4A audio
    {selector("137+140")}                       Exact video and audio format IDs from -F

  {heading("Selector symbols:")}
    {symbol("+")}  combine separate streams
    {symbol("/")}  use the next selector as a fallback
    {symbol("[]")} filter by properties such as height, extension, or codec

{heading("examples:")}
  {command('python yt_vlc.py "URL"')}
  {command('python yt_vlc.py -f "bv*[height<=1080]+ba/b[height<=1080]" "URL"')}
  {command('python yt_vlc.py -f "137+140" "URL"')}
  {command('python yt_vlc.py --print-only "URL"')}

The default selector is {selector(DEFAULT_FORMAT)}. VLC receives one combined
URL when available, or a video URL with the audio URL attached as an input
slave when yt-dlp returns two streams.

On first use, pinned Windows executables are downloaded into ./bin. Existing
files are reused; --yt-dlp and --vlc override the bundled executable paths.
"""


def download(url: str, destination: Path) -> None:
    """Download to a temporary sibling, then atomically move it into place."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.download")
    status("GET", f"Downloading {destination.name}", "34")
    request = urllib.request.Request(url, headers={"User-Agent": "yt-vlc/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else None
            downloaded = 0
            interactive = sys.stderr.isatty()
            with temporary.open("wb") as output:
                while chunk := response.read(CHUNK_SIZE):
                    output.write(chunk)
                    downloaded += len(chunk)
                    if interactive:
                        live(progress_line(destination.name, downloaded, total))
            if interactive:
                finish_live(progress_line(destination.name, downloaded, total))
            else:
                ui(progress_line(destination.name, downloaded, total))
        temporary.replace(destination)
        status("OK", f"Downloaded {destination.name} ({human_size(downloaded)})", "32")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def extract_portable_vlc(archive: Path, destination: Path) -> None:
    """Safely extract the complete VLC distribution into its own directory."""
    with zipfile.ZipFile(archive) as package:
        executables = [
            member
            for member in package.infolist()
            if Path(member.filename).name.lower() == "vlc.exe" and not member.is_dir()
        ]
        if len(executables) != 1:
            raise RuntimeError(
                f"Expected one vlc.exe in the VLC archive, found {len(executables)}"
            )

        archive_root = Path(executables[0].filename).parent
        with tempfile.TemporaryDirectory(
            prefix=".vlc-extract-", dir=destination.parent
        ) as temporary_dir:
            extracted = Path(temporary_dir) / "vlc"
            extracted.mkdir()

            for member in package.infolist():
                member_path = Path(member.filename)
                try:
                    relative = member_path.relative_to(archive_root)
                except ValueError:
                    continue
                if not relative.parts or ".." in relative.parts:
                    continue

                target = extracted / relative
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

            if not (extracted / "vlc.exe").is_file():
                raise RuntimeError("VLC extraction did not produce vlc.exe")
            extracted.replace(destination)


def ensure_bundled_tools() -> tuple[Path, Path]:
    """Create ./bin and install pinned yt-dlp and portable VLC if absent."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    yt_dlp = BIN_DIR / "yt-dlp.exe"
    vlc_directory = BIN_DIR / "vlc"
    vlc = vlc_directory / "vlc.exe"

    if not yt_dlp.is_file():
        download(YT_DLP_URL, yt_dlp)
    else:
        live_status("OK", "yt-dlp is ready", "32")

    if not vlc.is_file():
        with tempfile.TemporaryDirectory(prefix="yt-vlc-") as temporary_dir:
            archive = Path(temporary_dir) / "vlc.zip"
            download(VLC_URL, archive)
            live_status("...", "Extracting portable VLC", "33")
            extract_portable_vlc(archive, vlc_directory)
            live_status("OK", "Portable VLC extracted", "32")
    else:
        live_status("OK", "VLC is ready", "32")

    return yt_dlp, vlc


def find_program(
    name: str, override: str | None = None, bundled: Path | None = None
) -> str:
    """Return an executable path, with a few useful Windows fallbacks for VLC."""
    if override:
        path = shutil.which(override) or (override if Path(override).is_file() else None)
        if path:
            return str(path)
        raise FileNotFoundError(f"Could not find {name}: {override}")

    if bundled and bundled.is_file():
        return str(bundled)

    path = shutil.which(name)
    if path:
        return path

    if name == "vlc":
        candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "VideoLAN/VLC/vlc.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN/VLC/vlc.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError(
        f"Could not find {name}. Install it or provide --{name.replace('_', '-')} PATH."
    )


def resolve_streams(
    yt_dlp: str, page_url: str, format_selector: str
) -> tuple[list[str], dict[str, object]]:
    command = [
        yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "-f",
        format_selector,
        "--print",
        metadata_template(),
        "-g",
        page_url,
    ]
    with BrailleSpinner("Resolving media information"):
        result = subprocess.run(command, capture_output=True, text=True, check=False)

    if result.returncode:
        message = result.stderr.strip() or "yt-dlp could not resolve the URL"
        raise RuntimeError(message)

    streams: list[str] = []
    metadata: dict[str, object] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(METADATA_PREFIX):
            metadata = parse_metadata(line)
        else:
            streams.append(line)
    if not streams:
        raise RuntimeError("yt-dlp returned no stream URLs")
    if len(streams) > 2:
        raise RuntimeError(
            f"yt-dlp returned {len(streams)} URLs; expected one combined stream or "
            "separate video and audio streams"
        )
    return streams, metadata


def vlc_command(vlc: str, streams: list[str]) -> list[str]:
    # A fresh VLC process is required for reliable --input-slave handling.
    # VLC's single-instance handoff can silently discard the companion audio URL.
    command = [vlc, "--no-one-instance", streams[0]]
    if len(streams) == 2:
        command.append(f"--input-slave={streams[1]}")
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve a page with yt-dlp -g and open the resulting stream(s) in VLC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=format_help_text(),
    )
    parser.add_argument(
        "url", nargs="?", help="Video or media page URL (prompted for when omitted)"
    )
    parser.add_argument(
        "-f",
        "--format",
        default=DEFAULT_FORMAT,
        help=f"yt-dlp format selector (default: {DEFAULT_FORMAT})",
    )
    parser.add_argument("--yt-dlp", metavar="PATH", help="yt-dlp executable or path")
    parser.add_argument("--vlc", metavar="PATH", help="VLC executable or path")
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print resolved stream URLs without launching VLC",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        print_banner()
        page_url = args.url
        if not page_url:
            ui(colored("Media URL", "1;36", sys.stderr) + ": ", end="")
            page_url = input().strip()
        if not page_url:
            raise RuntimeError("No media URL was provided")

        stage_count = 2 if args.print_only else 3
        live_status(f"1/{stage_count}", "Checking prerequisites")
        bundled_yt_dlp, bundled_vlc = ensure_bundled_tools()
        yt_dlp = find_program("yt-dlp", args.yt_dlp, bundled_yt_dlp)

        live_status(f"2/{stage_count}", "Resolving network streams")
        started = time.monotonic()
        streams, media_info = resolve_streams(yt_dlp, page_url, args.format)
        elapsed = time.monotonic() - started
        stream_description = (
            "separate video + audio" if len(streams) == 2 else "combined video/audio"
        )
        live_status(
            "OK",
            f"Resolved {len(streams)} stream{'s' if len(streams) != 1 else ''} "
            f"({stream_description}) in {elapsed:.1f}s",
            "32",
        )
        show_media_info(media_info)

        if args.print_only:
            clear_live()
            print("\n".join(streams))
            return 0

        vlc = find_program("vlc", args.vlc, bundled_vlc)
        live_status("3/3", f"Opening {stream_description} in VLC")
        subprocess.Popen(vlc_command(vlc, streams), cwd=Path(vlc).parent)
        status("OK", "Stream handed off to VLC — enjoy", "32")
        return 0
    except (EOFError, KeyboardInterrupt):
        ui()
        status("--", "Cancelled", "33")
        return 130
    except (
        FileNotFoundError,
        RuntimeError,
        OSError,
        urllib.error.URLError,
        zipfile.BadZipFile,
    ) as error:
        status("ERR", str(error), "31")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
