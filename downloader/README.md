# YouTube to MP3 Downloader (Highest Quality)

A lightweight tool that uses `yt-dlp` and `ffmpeg` to download YouTube videos and playlists as highest fidelity (320 kbps) MP3 audio files with embedded metadata and album art.

---

## Folder Structure

```text
YouTube_Audio_Downloader/
├── run.bat              # Quick launcher (double-click to run interactively)
├── downloader.py        # Core Python script using yt-dlp & ffmpeg
├── requirements.txt     # Dependencies (yt-dlp, mutagen)
├── README.md            # Documentation and instructions
└── downloads/           # Output directory where your MP3 files are saved
```

---

## Features

- **Highest Audio Quality**: Automatically extracts the best audio stream available and encodes to 320 kbps MP3.
- **Embedded Cover Art & Metadata**: Embeds high-resolution YouTube thumbnails as MP3 album art, plus title, artist/uploader, description, and release year via ID3 tags.
- **Playlist Support**: Can download entire playlists or single videos.
- **Multiple Usage Modes**:
  - Interactive mode (paste links one after another in a loop)
  - Direct command-line arguments
  - Batch download from a text file containing multiple URLs
- **Organized Storage**: Keeps your general Downloads clean by saving all audio files directly inside `downloads/`.

---

## How to Use

### Method 1: Double-Click Launcher (Easiest)
1. Double-click `run.bat`.
2. A terminal window will open:
   - Paste any YouTube link(s) and press **Enter** to download.
   - Type `songs` to switch download target directly to `DJAITest/songs/`.
   - Type `open` and press **Enter** to open the active downloads folder in File Explorer.
   - Type `q` or `exit` to close the tool.

### Method 2: Command Line (CLI)

Navigate to this directory or run directly with Python:

```powershell
# 1. Interactive prompt
python downloader.py

# 2. Download one or more links
python downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"

# 3. Download multiple links at once
python downloader.py "https://www.youtube.com/watch?v=ID1" "https://www.youtube.com/watch?v=ID2"

# 4. Batch download from a text file (one URL per line)
python downloader.py --file links.txt

# 5. Specify a custom output folder
python downloader.py -o "D:\MyMusic" "https://www.youtube.com/watch?v=VIDEO_ID"

# 6. Open the downloads folder in Windows Explorer
python downloader.py --open
```

---

## Command Reference

| Flag / Option | Description |
|---|---|
| `urls` | One or more YouTube URLs (interactive mode opens if omitted) |
| `-o`, `--output` | Destination directory (default: `./downloads`) |
| `-q`, `--quality` | Audio quality in kbps (default: `320` for max CBR, or `0` for best VBR) |
| `-f`, `--file` | Path to text file containing URLs to download |
| `--no-playlist` | If link is part of a playlist, download only the single video |
| `--open` | Open the output folder in File Explorer and exit |

---

## Requirements

- **Python 3.8+**
- **ffmpeg** (available on system PATH)
- **Python Packages**:
  ```bash
  pip install -r requirements.txt
  ```
  *(Packages: `yt-dlp`, `mutagen`)*
