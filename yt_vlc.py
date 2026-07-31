#!/usr/bin/env python3
"""Resolve a media page with yt-dlp and play its streams in VLC."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
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


def colored(text: str, code: str) -> str:
    """Apply terminal color when help is being displayed interactively."""
    enabled = (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )
    return f"\033[{code}m{text}\033[0m" if enabled else text


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
    print(f"Downloading {destination.name}...")
    request = urllib.request.Request(url, headers={"User-Agent": "yt-vlc/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
        temporary.replace(destination)
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

    if not vlc.is_file():
        with tempfile.TemporaryDirectory(prefix="yt-vlc-") as temporary_dir:
            archive = Path(temporary_dir) / "vlc.zip"
            download(VLC_URL, archive)
            print("Extracting portable VLC (DLLs and plugins included)...")
            extract_portable_vlc(archive, vlc_directory)

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


def resolve_streams(yt_dlp: str, page_url: str, format_selector: str) -> list[str]:
    command = [
        yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "-f",
        format_selector,
        "-g",
        page_url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    if result.returncode:
        message = result.stderr.strip() or "yt-dlp could not resolve the URL"
        raise RuntimeError(message)

    streams = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not streams:
        raise RuntimeError("yt-dlp returned no stream URLs")
    if len(streams) > 2:
        raise RuntimeError(
            f"yt-dlp returned {len(streams)} URLs; expected one combined stream or "
            "separate video and audio streams"
        )
    return streams


def vlc_command(vlc: str, streams: list[str]) -> list[str]:
    command = [vlc, streams[0]]
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
        page_url = args.url or input("Media URL: ").strip()
        if not page_url:
            raise RuntimeError("No media URL was provided")

        bundled_yt_dlp, bundled_vlc = ensure_bundled_tools()
        yt_dlp = find_program("yt-dlp", args.yt_dlp, bundled_yt_dlp)
        streams = resolve_streams(yt_dlp, page_url, args.format)

        if args.print_only:
            print("\n".join(streams))
            return 0

        vlc = find_program("vlc", args.vlc, bundled_vlc)
        if len(streams) == 2:
            print("Opening separate video and audio streams in VLC...")
        else:
            print("Opening combined stream in VLC...")
        process = subprocess.Popen(vlc_command(vlc, streams), cwd=Path(vlc).parent)
        try:
            exit_code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return 0
        if exit_code:
            status = f"0x{exit_code:08X}" if os.name == "nt" else str(exit_code)
            raise RuntimeError(
                f"VLC exited immediately with code {exit_code} ({status})"
            )
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        RuntimeError,
        OSError,
        urllib.error.URLError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
