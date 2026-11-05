#!/usr/bin/python3
import argparse
import base64
import itertools
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests
from ffmpeg import FFmpeg, FFmpegError
from wakepy import keep

HTTP_TIMEOUT = (10, 60)  # (connect, read) seconds
DOWNLOAD_WORKERS = 8
DOWNLOAD_WINDOW = 32  # max segment requests in flight
RETRY_BACKOFF = (1, 2, 4)  # seconds to wait before each retry of a failed segment request
HLS_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
UUID_PATH_RE = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")

_thread_local = threading.local()


def get_session():
    # requests.Session is not guaranteed thread-safe and segment fetches run on worker threads.
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _thread_local.session = requests.Session()
    return session


class DownloadCancelled(Exception):
    pass


def http_get(url):
    r = get_session().get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


def is_transient(exc):
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, requests.RequestException)


def fetch_part_once(url, byterange=None):
    headers = {}
    if byterange is not None:
        start, end = byterange  # end is exclusive
        headers["Range"] = f"bytes={start}-{end - 1}"
    r = get_session().get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    if byterange is not None and r.status_code != 206:
        raise RuntimeError(f"Server ignored byte range request for {url}")
    return r.content


def fetch_part(url, byterange=None):
    for delay in RETRY_BACKOFF:
        try:
            return fetch_part_once(url, byterange)
        except requests.RequestException as exc:
            if not is_transient(exc):
                raise
            time.sleep(delay)
    return fetch_part_once(url, byterange)


def default_clip_id():
    return "vimeo_" + str(int(time.time()))


def parse_hls_attrs(text):
    return {key: value.strip('"') for key, value in HLS_ATTR_RE.findall(text)}


def pick_by_height(items, height_of):
    for wanted in (540, 720):
        for item in items:
            if height_of(item) == wanted:
                return item
    raise RuntimeError("No 540p or 720p video found")


def segment_urls(base_url, segments):
    urls = []
    for segment in segments:
        if not segment.get("url"):
            raise RuntimeError("Playlist segment has no url")
        urls.append(urllib.parse.urljoin(base_url, segment["url"]))
    return urls


def load_json_tracks(master_url, playlist):
    base_url = urllib.parse.urljoin(master_url, playlist.get("base_url"))
    video = pick_by_height(playlist.get("video", []), lambda v: v.get("height"))
    video_base_url = urllib.parse.urljoin(base_url, video.get("base_url"))
    tracks = {
        "clip_id": playlist.get("clip_id") or default_clip_id(),
        "video": (
            base64.b64decode(video.get("init_segment", "")),
            [(url, None) for url in segment_urls(video_base_url, video.get("segments", []))],
        ),
        "audio": None,
    }
    audio_list = playlist.get("audio", [])
    if audio_list:
        audio = audio_list[0]
        audio_base_url = urllib.parse.urljoin(base_url, audio.get("base_url"))
        tracks["audio"] = (
            base64.b64decode(audio.get("init_segment", "")),
            [(url, None) for url in segment_urls(audio_base_url, audio.get("segments", []))],
        )
    return tracks


def resolve_byterange(value, uri, last_end):
    # HLS BYTERANGE is "length[@offset]"; without an offset the range
    # continues where the previous range of the same resource ended.
    length_text, _, offset_text = value.partition("@")
    start = int(offset_text) if offset_text else last_end.get(uri, 0)
    end = start + int(length_text)
    last_end[uri] = end
    return start, end


def load_hls_media(url):
    init_bytes = b""
    segments = []
    last_end = {}
    pending_range = None
    for raw_line in http_get(url).text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-KEY:"):
            method = parse_hls_attrs(line.split(":", 1)[1]).get("METHOD")
            if method != "NONE":
                raise RuntimeError(f"Encrypted HLS is not supported (METHOD={method})")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_hls_attrs(line.split(":", 1)[1])
            init_url = urllib.parse.urljoin(url, attrs["URI"])
            init_range = resolve_byterange(attrs["BYTERANGE"], init_url, last_end) if "BYTERANGE" in attrs else None
            init_bytes = fetch_part(init_url, init_range)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = line.split(":", 1)[1]
        elif line and not line.startswith("#"):
            segment_url = urllib.parse.urljoin(url, line)
            byterange = resolve_byterange(pending_range, segment_url, last_end) if pending_range else None
            pending_range = None
            segments.append((segment_url, byterange))
    if not segments:
        raise RuntimeError(f"No segments in media playlist: {url}")
    return init_bytes, segments


def load_hls_tracks(master_url, text):
    variants = []
    audio_renditions = []
    pending_attrs = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_hls_attrs(line.split(":", 1)[1])
            if attrs.get("TYPE") == "AUDIO":
                audio_renditions.append(attrs)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending_attrs = parse_hls_attrs(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending_attrs is not None:
            variants.append((pending_attrs, urllib.parse.urljoin(master_url, line)))
            pending_attrs = None
    if not variants:
        raise RuntimeError("Not a master playlist: no variants found")

    def height_of(variant):
        resolution = variant[0].get("RESOLUTION", "0x0")
        return int(resolution.split("x")[1])

    attrs, video_url = pick_by_height(variants, height_of)

    # A variant without an AUDIO group carries its audio muxed into the video track.
    audio = None
    group_id = attrs.get("AUDIO")
    if group_id:
        group = [r for r in audio_renditions if r.get("GROUP-ID") == group_id]
        if not group:
            raise RuntimeError("No audio rendition found for the selected video")
        rendition = next((r for r in group if r.get("DEFAULT") == "YES"), group[0])
        if "URI" in rendition:
            audio = load_hls_media(urllib.parse.urljoin(master_url, rendition["URI"]))

    clip_match = UUID_PATH_RE.search(master_url)
    return {
        "clip_id": clip_match.group(1) if clip_match else default_clip_id(),
        "video": load_hls_media(video_url),
        "audio": audio,
    }


def load_tracks(url):
    r = http_get(url)
    if r.text.lstrip().startswith("#EXTM3U"):
        return load_hls_tracks(url, r.text)
    try:
        playlist = r.json()
    except ValueError as exc:
        raise RuntimeError("Unsupported playlist: neither HLS nor JSON") from exc
    return load_json_tracks(url, playlist)


def ordered_fetch(parts, cancel_event):
    """Yield part bodies in playlist order, keeping at most DOWNLOAD_WINDOW requests in flight."""
    pool = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
    try:
        queued = deque()
        parts_iter = iter(parts)
        for part in itertools.islice(parts_iter, DOWNLOAD_WINDOW):
            queued.append(pool.submit(fetch_part, *part))
        while queued:
            if cancel_event.is_set():
                raise DownloadCancelled()
            body = queued.popleft().result()
            nxt = next(parts_iter, None)
            if nxt is not None:
                queued.append(pool.submit(fetch_part, *nxt))
            yield body
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def write_stream(path, init_bytes, parts, report, cancel_event):
    with open(path, "wb") as f:  # "wb" truncates any leftover file
        f.write(init_bytes)
        for body in ordered_fetch(parts, cancel_event):
            f.write(body)
            report("step")


def merge(inputs, output, cancel_event):
    ffmpeg = FFmpeg().option("n")  # never overwrite an existing file
    for path in inputs:
        ffmpeg = ffmpeg.input(path)
    job = ffmpeg.output(output, codec="copy")
    finished = threading.Event()
    terminated = threading.Event()

    def watch_cancel():
        # terminate() is only accepted while execute() is running, so retry until it is.
        while not finished.wait(0.1):
            if cancel_event.is_set():
                try:
                    job.terminate()
                except FFmpegError:
                    continue
                terminated.set()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        job.execute()
    except FFmpegError as exc:
        raise RuntimeError(f"ffmpeg merge failed: {exc}") from exc
    finally:
        finished.set()
        watcher.join()
    # execute() returns normally after SIGTERM, so check the flag explicitly.
    if terminated.is_set():
        raise DownloadCancelled()


def run_download(master_url, output_dir, report, cancel_event):
    if not os.path.isdir(output_dir):
        raise RuntimeError(f"Output directory does not exist: {output_dir}")
    report("status", "Loading playlist...")
    tracks = load_tracks(master_url)
    clip_id = tracks["clip_id"]
    output = os.path.join(output_dir, clip_id + ".mp4")
    if os.path.exists(output):
        raise RuntimeError(f"Output file already exists: {output}")

    video_init, video_parts = tracks["video"]
    audio = tracks["audio"]
    report("max", len(video_parts) + (len(audio[1]) if audio else 0))

    # Temp files live in the output directory; TemporaryDirectory removes them on every exit path.
    with tempfile.TemporaryDirectory(prefix=clip_id + "-", dir=output_dir) as tmp:
        inputs = []
        video_path = os.path.join(tmp, "video.mp4")
        report("status", "Downloading...")
        write_stream(video_path, video_init, video_parts, report, cancel_event)
        inputs.append(video_path)
        if audio:
            audio_init, audio_parts = audio
            audio_path = os.path.join(tmp, "audio.mp4")
            write_stream(audio_path, audio_init, audio_parts, report, cancel_event)
            inputs.append(audio_path)
        if cancel_event.is_set():
            raise DownloadCancelled()
        report("status", "Merging...")
        try:
            merge(inputs, output, cancel_event)
        except Exception:
            if os.path.exists(output):
                os.remove(output)
            raise
    return output


def worker(master_url, output_dir, events, cancel_event, print_traceback=True):
    def report(kind, value=None):
        events.put((kind, value))

    try:
        with keep.running():
            output = run_download(master_url, output_dir, report, cancel_event)
        report("done", output)
    except DownloadCancelled:
        report("cancelled")
    except Exception as exc:
        if print_traceback:
            traceback.print_exc()
        report("error", str(exc) or exc.__class__.__name__)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="vimeo_dl",
        description="Download a Vimeo video from its playlist URL. Without a URL the GUI starts.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="playlist URL; when given, the download runs in the terminal and no window is opened",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=os.getcwd(),
        help="directory for the merged MP4 (default: current directory)",
    )
    args = parser.parse_args(argv)
    if args.url is not None:
        args.url = args.url.strip()
        if not args.url:
            parser.error("URL must not be empty")
    return args


def run_cli(master_url, output_dir):
    """Download in the terminal and return the process exit code: 0, 1 on error, 130 when cancelled."""
    events = queue.Queue()
    cancel_event = threading.Event()
    state = {"maximum": 0, "value": 0, "line_open": False}
    exit_code = 0

    def end_progress_line():
        if state["line_open"]:
            print(file=sys.stderr)
            state["line_open"] = False

    def on_event(kind, value):
        nonlocal exit_code
        if kind == "status":
            end_progress_line()
            print(value, file=sys.stderr)
        elif kind == "max":
            state["maximum"], state["value"] = value, 0
        elif kind == "step":
            state["value"] += 1
            if state["maximum"]:
                print(f"\r{state['value']}/{state['maximum']} segments", end="", file=sys.stderr, flush=True)
                state["line_open"] = True
        elif kind == "done":
            end_progress_line()
            print(value)  # the only stdout output, so scripts can capture the path
        elif kind == "error":
            end_progress_line()
            print(f"Error: {value}", file=sys.stderr)
            exit_code = 1
        elif kind == "cancelled":
            end_progress_line()
            print("Cancelled", file=sys.stderr)
            exit_code = 130

    thread = threading.Thread(
        target=worker,
        args=(master_url, output_dir, events, cancel_event, False),
        daemon=True,
    )
    thread.start()
    while thread.is_alive() or not events.empty():
        try:
            kind, value = events.get(timeout=0.1)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            # The worker checks cancel_event and removes its temp files before it exits.
            if cancel_event.is_set():
                raise
            print("\nCancelling...", file=sys.stderr)
            cancel_event.set()
            continue
        on_event(kind, value)
    return exit_code


def run_gui():
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    events = queue.Queue()
    cancel_event = threading.Event()
    state = {"worker": None, "poll_id": 0, "closing": False, "maximum": 0, "value": 0}

    def on_activate(app):
        window = Gtk.ApplicationWindow(application=app, title="Vimeo Downloader")
        url_entry = Gtk.Entry(hexpand=True, width_chars=60)
        output_entry = Gtk.Entry(hexpand=True, width_chars=60, text=os.getcwd())
        browse_btn = Gtk.Button(label="Browse...")
        output_box = Gtk.Box(spacing=8, hexpand=True)
        output_box.append(output_entry)
        output_box.append(browse_btn)
        download_btn = Gtk.Button(label="Download", hexpand=True)
        cancel_btn = Gtk.Button(label="Cancel", sensitive=False)
        button_box = Gtk.Box(spacing=8, hexpand=True)
        button_box.append(download_btn)
        button_box.append(cancel_btn)
        progressbar = Gtk.ProgressBar()

        grid = Gtk.Grid(
            column_spacing=8,
            row_spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        grid.attach(Gtk.Label(label="URL", css_classes=["heading"]), 0, 0, 1, 1)
        grid.attach(url_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Save to", css_classes=["heading"]), 0, 1, 1, 1)
        grid.attach(output_box, 1, 1, 1, 1)
        grid.attach(button_box, 1, 2, 1, 1)
        grid.attach(progressbar, 0, 3, 2, 1)
        window.set_child(grid)

        def reset_controls():
            download_btn.set_label("Download")
            download_btn.set_sensitive(True)
            cancel_btn.set_sensitive(False)
            url_entry.set_sensitive(True)
            output_entry.set_sensitive(True)
            browse_btn.set_sensitive(True)
            progressbar.set_fraction(0)

        def alert(message, detail):
            dialog = Gtk.AlertDialog()
            dialog.set_message(message)
            dialog.set_detail(detail)
            dialog.set_buttons(["OK"])
            dialog.show(window)

        def handle_event(kind, value):
            if kind == "status":
                if not cancel_event.is_set():
                    download_btn.set_label(value)
                cancel_btn.set_sensitive(not cancel_event.is_set())
            elif kind == "max":
                state["maximum"], state["value"] = value, 0
                progressbar.set_fraction(0)
            elif kind == "step":
                state["value"] += 1
                if state["maximum"]:
                    progressbar.set_fraction(min(state["value"] / state["maximum"], 1.0))
            elif kind == "done":
                reset_controls()
                alert("Success", f"Download and merge completed: {value}")
            elif kind == "error":
                reset_controls()
                alert("Error", value)
            elif kind == "cancelled":
                reset_controls()

        def poll_events():
            # Check liveness before draining: the worker's last event is queued before it exits,
            # so a drain after seeing "not alive" cannot miss it.
            running = state["worker"].is_alive()
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                handle_event(kind, value)
            if running:
                return GLib.SOURCE_CONTINUE
            state["poll_id"] = 0
            if state["closing"]:
                window.destroy()
            return GLib.SOURCE_REMOVE

        def choose_folder(_button):
            dialog = Gtk.FileDialog(title="Select output directory")
            current = output_entry.get_text().strip()
            if os.path.isdir(current):
                dialog.set_initial_folder(Gio.File.new_for_path(current))
            dialog.select_folder(window, None, on_folder_chosen)

        def on_folder_chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return  # dialog dismissed
            output_entry.set_text(folder.get_path())

        def start_download(_button):
            if state["worker"] is not None and state["worker"].is_alive():
                return
            cancel_event.clear()
            download_btn.set_sensitive(False)
            download_btn.set_label("Downloading...")
            url_entry.set_sensitive(False)
            output_entry.set_sensitive(False)
            browse_btn.set_sensitive(False)
            cancel_btn.set_sensitive(True)
            state["worker"] = threading.Thread(
                target=worker,
                args=(url_entry.get_text().strip(), output_entry.get_text().strip(), events, cancel_event),
                daemon=True,
            )
            state["worker"].start()
            # Only one poll source may exist; a pending one from a previous run keeps polling.
            if state["poll_id"] == 0:
                state["poll_id"] = GLib.timeout_add(100, poll_events)

        def on_cancel(_button):
            cancel_event.set()
            cancel_btn.set_sensitive(False)
            download_btn.set_label("Cancelling...")

        def on_close_request(_window):
            if state["worker"] is not None and state["worker"].is_alive():
                state["closing"] = True
                cancel_event.set()
                download_btn.set_label("Cancelling...")
                return True  # keep the window open until the worker has stopped
            return False  # let GTK close the window

        download_btn.connect("clicked", start_download)
        browse_btn.connect("clicked", choose_folder)
        cancel_btn.connect("clicked", on_cancel)
        window.connect("close-request", on_close_request)
        window.present()

    Gtk.Window.set_default_icon_name("io.github.akosmarton.vimeo_dl")
    app = Gtk.Application(application_id="io.github.akosmarton.vimeo_dl")
    app.connect("activate", on_activate)
    app.run(None)


def main(argv=None):
    args = parse_args(argv)
    if args.url is not None:
        return run_cli(args.url, args.output_dir)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
