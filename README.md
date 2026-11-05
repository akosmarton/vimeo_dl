# Vimeo Downloader

A small GTK 4 app that downloads a Vimeo video from its playlist URL and
saves it as an MP4 file in the current directory.

The app downloads one video track (540p, or 720p if 540p is not available)
and one audio track, then merges them with ffmpeg without re-encoding.

## Requirements

- Python 3 with PyGObject and GTK 4 from the system packages (Fedora: `sudo dnf install python3-gobject gtk4 ffmpeg`)
- [ffmpeg](https://ffmpeg.org/) on `PATH`
- Python packages from `requirements.txt`

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

### GUI

```
.venv/bin/python vimeo_dl.py
```

1. Paste the playlist URL into the **URL** field.
2. Press **Download**.
3. When the download finishes, the file `<clip_id>.mp4` is written to the
   current directory.

The window shows the status and a progress bar. Closing the window during a
download cancels it; the window closes once the worker has stopped.

### Command line

Passing a playlist URL as the positional argument skips the GUI:

```
.venv/bin/python vimeo_dl.py <playlist-url> [-o OUTPUT_DIR]
```

- `-o`, `--output-dir`: directory for the merged MP4. Defaults to the current
  directory.
- Progress, status, and errors go to stderr.
- On success the full path of the merged file is the only line written to
  stdout, so `$(...)` captures it.
- Ctrl-C cancels the download, removes the temporary files, and does not write
  an output file.

Exit codes: `0` on success, `1` on error, `130` when cancelled.

The installed launcher forwards arguments, so `vimeo_dl <url>` works the same
way.

## Supported playlists

The app accepts two playlist formats:

- **HLS master playlist** (`#EXTM3U`). The app picks the `RESOLUTION=960x540`
  variant, or `1280x720` if 540p is missing. Audio comes from the
  `EXT-X-MEDIA TYPE=AUDIO` entry. `EXT-X-MAP` and `EXT-X-BYTERANGE` are
  supported.
- **Vimeo JSON playlist**. The app picks the video track with height 540, or
  720 if 540 is missing, and uses the first audio track.

Encrypted HLS (`#EXT-X-KEY` with a method other than `NONE`) is not supported
and fails with an error.

Vimeo playlist URLs are signed and expire (the `exp=` parameter). Copy a fresh
URL from the browser's network tab if the download reports an HTTP error.

## Errors

- **`ModuleNotFoundError: No module named 'ffmpeg'`**: the app was started with
  the system Python. The `ffmpeg` Python module comes from `python-ffmpeg` in
  the venv, not from an OS package. Start the app with
  `.venv/bin/python vimeo_dl.py` or the installed `vimeo_dl` launcher, and
  re-run `./install.sh` if the venv is missing or stale.
- **`No 540p or 720p video found`**: the playlist has neither variant.
- **`Output file already exists`**: a file with the same name is already in the
  current directory. The app does not overwrite it.
- **`ffmpeg merge failed`**: ffmpeg returned an error. The partial output file
  is removed.

Segment files are written to a temporary directory named `<clip_id>-*` inside
the output directory. It is removed on every exit path, including Ctrl-C.

## Tests

The parser tests use inline playlist strings and do not need network access.

```
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q test_vimeo_dl.py
```

## Project layout

| File | Purpose |
| --- | --- |
| `vimeo_dl.py` | Application: playlist parsing, downloader, GUI |
| `test_vimeo_dl.py` | pytest tests for the parser helpers |
| `requirements.txt` | Runtime dependencies |
| `AGENTS.md` | Notes for contributors and coding agents |
