# AGENTS.md

Guidance for agents working in this repository.

## Project

GTK 4 desktop app (PyGObject) that downloads Vimeo videos. It reads a Vimeo master
playlist URL, selects a 540p (fallback 720p) video track and an audio track,
downloads their segments, and merges them with ffmpeg into `<clip_id>.mp4`
in the directory set in the "Save to" field (default: the working directory).

Supported playlist formats, dispatched by `load_tracks()`:
- HLS master playlist (`#EXTM3U`), handled by `load_hls_tracks()` and `load_hls_media()`.
- Vimeo JSON playlist, handled by `load_json_tracks()`.

Encrypted HLS (`#EXT-X-KEY` with a method other than `NONE`) is rejected with an error.

## Layout

- `vimeo_dl.py`: the whole application (parsers, downloader, CLI, GUI).
- `test_vimeo_dl.py`: pytest tests for the pure parser helpers. No network.
- `requirements.txt`: runtime dependencies only. `pytest` is not listed.
- `.venv/`: local environment, gitignored. Do not commit it.

## Setup

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt pytest
```

The `ffmpeg` binary must be on `PATH`. `merge()` calls it through `python-ffmpeg`
and uses `-n`, so it never overwrites an existing output file.

## Run

```
.venv/bin/python vimeo_dl.py
```

The system `python3` does not have the runtime packages (`ffmpeg`, `requests`,
`wakepy`), so it fails with `ModuleNotFoundError: No module named 'ffmpeg'`.

Paste a Vimeo playlist URL into the window, optionally change the "Save to" directory, and press Download.

With a URL as the first argument the app runs headless in the terminal instead
and never imports `gi`:

```
.venv/bin/python vimeo_dl.py <playlist-url> [-o OUTPUT_DIR]
```

`run_cli()` reuses the same `worker()` thread and event protocol as the GUI;
progress goes to stderr, the merged path is the only stdout line. Exit codes:
`0` success, `1` error, `130` cancelled. Keep that contract when changing it.

## Test

```
.venv/bin/python -m pytest -q test_vimeo_dl.py
```

Tests must not depend on live Vimeo URLs. Those URLs are signed and expire
(the `exp=` query parameter). Use inline playlist strings and `monkeypatch`
on `http_get` / `fetch_part`.


## Code conventions

- Keep `vimeo_dl.py` a single module. Do not add packages unless asked.
- The GUI is built inside `run_gui()` with `Gtk.Application`; `main()` only parses the
  command line and dispatches to `run_cli()` or `run_gui()`. Module-level code must not
  import `gi` or create windows, so the parsers stay importable in tests.
- All network calls go through `session` with `HTTP_TIMEOUT`. Do not add
  bare `requests.get` calls.
- Downloads run in a worker thread. The worker reports through the `events`
  queue; only `poll_events()` (a `GLib.timeout_add` source) touches GTK widgets.
  Keep a single poll source (`state["poll_id"]`).
- Segment order must match playlist order. `ordered_fetch()` keeps at most
  `DOWNLOAD_WINDOW` requests in flight.
- Temp files go in a `TemporaryDirectory` inside the working directory, so
  cleanup happens on every exit path.

## Language and text

- Code, identifiers, comments, docstrings, error messages, and documentation
  are in English.

## Git

- Do not commit, amend, rebase, reset, tag, stash, or push without explicit
  user approval for that specific action.
- Ask before each commit separately, naming the files and the proposed message.
  One approval covers one commit only.
- Do not push to any remote or open pull requests or issues without approval.

## Verification before finishing a change

- Parser or selection change: run the pytest suite.
- Download pipeline change: run an end-to-end download against a local HTTP
  fixture (a generated master playlist, media playlists, and fragmented MP4
  segments served with `python -m http.server`). Confirm the output with
  `ffprobe`. Do not add the fixture to the repository unless asked.
- GUI change: launch `.venv/bin/python vimeo_dl.py` and exercise the changed path.
  State in the report if a display is not available.
