#!/usr/bin/env python3
"""
YouTube to MP3 Downloader
Downloads YouTube videos/playlists as high-quality (320 kbps) MP3 audio files
with metadata and high-resolution thumbnail album art embedded.
"""

import argparse
import os
import sys
import subprocess
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("Error: 'yt-dlp' is not installed.")
    print("Please install it by running: pip install yt-dlp")
    sys.exit(1)


# Default directories
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_DIR = SCRIPT_DIR / "downloads"


class DownloadLogger:
    def debug(self, msg):
        if msg.startswith('[debug]'):
            return

    def info(self, msg):
        print(msg)

    def warning(self, msg):
        print(f"\033[93m[Warning]\033[0m {msg}")

    def error(self, msg):
        print(f"\033[91m[Error]\033[0m {msg}")


def get_ydl_options(output_dir: Path, quality: str = "320", allow_playlist: bool = True):
    """
    Constructs yt-dlp options tailored for highest quality MP3 extraction.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(output_dir / "%(title)s.%(ext)s")

    ydl_opts = {
        # Select highest quality audio stream available
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'windowsfilenames': True,  # Clean safe filenames on Windows
        'noplaylist': not allow_playlist,
        'writethumbnail': True,    # Download thumbnail to embed as cover art
        'logger': DownloadLogger(),
        'postprocessors': [
            # 1. Extract audio and re-encode to MP3 at preferred quality (320 kbps)
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': str(quality),
            },
            # 2. Embed audio metadata (title, artist, album, uploader, date)
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
            # 3. Embed high-res thumbnail directly as ID3 album art
            {
                'key': 'EmbedThumbnail',
            },
        ],
        'postprocessor_args': {
            'FFmpegExtractAudio': ['-id3v2_version', '3'],
        },
        'ignoreerrors': True,  # Continue if one video in a playlist fails
        'quiet': False,
        'no_warnings': False,
    }
    return ydl_opts


def download_links(urls, output_dir: Path, quality: str = "320", allow_playlist: bool = True):
    """
    Downloads list of URLs to the target directory.
    """
    if not urls:
        print("No URLs provided.")
        return False

    ydl_opts = get_ydl_options(output_dir, quality=quality, allow_playlist=allow_playlist)

    print(f"\n" + "=" * 65)
    print(f" Output Folder : {output_dir}")
    print(f" MP3 Quality   : {quality} kbps (Max Bitrate)")
    print(f" Target URLs   : {len(urls)}")
    print("=" * 65 + "\n")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            exit_code = ydl.download(urls)
            if exit_code == 0:
                print("\n\033[92m[SUCCESS] All downloads finished successfully!\033[0m")
            else:
                print(f"\n\033[93m[COMPLETED] Downloads completed with return code {exit_code}.\033[0m")
            return True
    except Exception as e:
        print(f"\n\033[91m[FAILED] Error during download: {e}\033[0m")
        return False


def open_folder(path: Path):
    """Opens folder in Windows Explorer."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.run(["xdg-open", str(path)])


def interactive_mode(output_dir: Path, quality: str = "320"):
    """
    Runs an interactive loop prompting the user for YouTube links.
    """
    print("\n" + "=" * 65)
    print("       YOUTUBE TO HIGH-QUALITY MP3 DOWNLOADER (yt-dlp)")
    print("=" * 65)
    print(f" * MP3 Quality: {quality} kbps")
    print(f" * Save Folder: {output_dir}")
    print("=" * 65)
    print(" Commands:")
    print("   - Paste any YouTube link(s) to download")
    print("   - Type 'open' to open downloads folder in File Explorer")
    print("   - Type 'q' or 'exit' to exit")
    print("-" * 65)

    while True:
        try:
            user_input = input("\nEnter YouTube Link(s): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("q", "quit", "exit"):
            print("Goodbye!")
            break
        elif cmd == "open":
            print(f"Opening folder: {output_dir}")
            open_folder(output_dir)
            continue
        elif cmd == "songs":
            parent_songs = SCRIPT_DIR.parent / "songs"
            if parent_songs.is_dir():
                output_dir = parent_songs
                print(f"Switched target folder to: {output_dir}")
            else:
                print(f"Folder '{parent_songs}' does not exist.")
            continue
        elif cmd == "clear" or cmd == "cls":
            os.system('cls' if os.name == 'nt' else 'clear')
            continue

        # Split multiple links separated by whitespace or comma
        raw_urls = [u.strip(", \t\r\n\"'") for u in user_input.replace(",", " ").split()]
        urls = [u for u in raw_urls if u.startswith("http://") or u.startswith("https://") or "youtube.com" in u or "youtu.be" in u]

        if not urls:
            print("\033[91mPlease enter a valid YouTube URL (e.g. https://www.youtube.com/watch?v=...)\033[0m")
            continue

        download_links(urls, output_dir=output_dir, quality=quality, allow_playlist=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download YouTube videos and playlists as high-quality (320kbps) MP3 files."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more YouTube URLs to download (if omitted, interactive mode starts)."
    )
    parser.add_argument(
        "-o", "--output",
        default=str(DEFAULT_DOWNLOAD_DIR),
        help=f"Directory to save MP3 files to (default: {DEFAULT_DOWNLOAD_DIR})"
    )
    parser.add_argument(
        "-q", "--quality",
        default="320",
        help="Audio quality / bitrate in kbps (default: 320 for highest MP3 quality, or 0 for VBR best)"
    )
    parser.add_argument(
        "-f", "--file",
        help="Text file containing YouTube URLs (one per line)"
    )
    parser.add_argument(
        "--no-playlist",
        action="store_true",
        help="Download only the single video if a playlist link is given"
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the download destination folder in File Explorer"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output).resolve()

    if args.open:
        open_folder(output_dir)
        return

    # Check for URLs from file
    file_urls = []
    if args.file:
        file_path = Path(args.file)
        if file_path.is_file():
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        file_urls.append(line)
        else:
            print(f"Error: File '{args.file}' not found.")
            sys.exit(1)

    all_urls = args.urls + file_urls

    if all_urls:
        download_links(
            all_urls,
            output_dir=output_dir,
            quality=args.quality,
            allow_playlist=not args.no_playlist
        )
    else:
        # No CLI arguments provided, launch interactive prompt
        interactive_mode(output_dir, quality=args.quality)


if __name__ == "__main__":
    main()
