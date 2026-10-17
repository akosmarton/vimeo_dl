#!/usr/bin/python3
import base64
import itertools
import os
import queue
import re
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
HLS_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
UUID_PATH_RE = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")

session = requests.Session()

class DownloadCancelled(Exception):
    pass


def http_get(url):
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r


def fetch_part(url, byterange=None):
    headers = {}
    if byterange is not None:
        start, end = byterange  # end is exclusive
        headers["Range"] = f"bytes={start}-{end - 1}"
    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    if byterange is not None and r.status_code != 206:
        raise RuntimeError(f"Server ignored byte range request for {url}")
    return r.content


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


def load_json_tracks(master_url, playlist):
    base_url = urllib.parse.urljoin(master_url, playlist.get("base_url"))
    video = pick_by_height(playlist.get("video", []), lambda v: v.get("height"))
    video_base_url = urllib.parse.urljoin(base_url, video.get("base_url"))
    tracks = {
        "clip_id": playlist.get("clip_id") or default_clip_id(),
        "video": (
            base64.b64decode(video.get("init_segment", "")),
            [(urllib.parse.urljoin(video_base_url, s.get("url")), None) for s in video.get("segments", [])],
        ),
        "audio": None,
    }
    audio_list = playlist.get("audio", [])
    if audio_list:
        audio = audio_list[0]
        audio_base_url = urllib.parse.urljoin(base_url, audio.get("base_url"))
        tracks["audio"] = (
            base64.b64decode(audio.get("init_segment", "")),
            [(urllib.parse.urljoin(audio_base_url, s.get("url")), None) for s in audio.get("segments", [])],
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


def merge(inputs, output):
    ffmpeg = FFmpeg().option("n")  # never overwrite an existing file
    for path in inputs:
        ffmpeg = ffmpeg.input(path)
    try:
        ffmpeg.output(output, codec="copy").execute()
    except FFmpegError as exc:
        raise RuntimeError(f"ffmpeg merge failed: {exc}") from exc


def run_download(master_url, report, cancel_event):
    report("status", "Loading playlist...")
    tracks = load_tracks(master_url)
    clip_id = tracks["clip_id"]
    output = clip_id + ".mp4"
    if os.path.exists(output):
        raise RuntimeError(f"Output file already exists: {output}")

    video_init, video_parts = tracks["video"]
    audio = tracks["audio"]
    report("max", len(video_parts) + (len(audio[1]) if audio else 0))

    # Temp files live in the working directory; TemporaryDirectory removes them on every exit path.
    with tempfile.TemporaryDirectory(prefix=clip_id + "-", dir=os.getcwd()) as tmp:
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
        report("status", "Merging...")
        try:
            merge(inputs, output)
        except Exception:
            if os.path.exists(output):
                os.remove(output)
            raise
    return output


def worker(master_url, events, cancel_event):
    def report(kind, value=None):
        events.put((kind, value))

    try:
        with keep.running():
            output = run_download(master_url, report, cancel_event)
        report("done", output)
    except DownloadCancelled:
        report("cancelled")
    except Exception as exc:
        traceback.print_exc()
        report("error", str(exc) or exc.__class__.__name__)


def main():
    import tkinter as tk
    from tkinter import messagebox
    from tkinter import ttk

    events = queue.Queue()
    cancel_event = threading.Event()
    state = {"worker": None, "poll_job": None, "closing": False}

    root = tk.Tk()
    root.title('Vimeo Downloader')
    url_var = tk.StringVar()

    def reset_controls():
        sub_btn.config(state=tk.NORMAL, text='Download')
        url_entry.config(state=tk.NORMAL)
        progressbar.configure(value=0)

    def handle_event(kind, value):
        if kind == "status":
            sub_btn.config(text=value)
        elif kind == "max":
            progressbar.configure(maximum=value, value=0)
        elif kind == "step":
            progressbar.step()
        elif kind == "done":
            reset_controls()
            messagebox.showinfo("Success", f"Download and merge completed: {value}")
        elif kind == "error":
            reset_controls()
            messagebox.showerror("Error", value)
        elif kind == "cancelled":
            reset_controls()

    def poll_events():
        state["poll_job"] = None
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
            state["poll_job"] = root.after(100, poll_events)
        elif state["closing"]:
            root.destroy()

    def download_video():
        if state["worker"] is not None and state["worker"].is_alive():
            return
        cancel_event.clear()
        sub_btn.config(state=tk.DISABLED, text='Downloading...')
        url_entry.config(state=tk.DISABLED)
        state["worker"] = threading.Thread(
            target=worker, args=(url_var.get().strip(), events, cancel_event), daemon=True
        )
        state["worker"].start()
        # Only one poll chain may exist; a pending one from a previous run keeps polling.
        if state["poll_job"] is None:
            state["poll_job"] = root.after(100, poll_events)

    def on_close():
        if state["worker"] is not None and state["worker"].is_alive():
            state["closing"] = True
            cancel_event.set()
            sub_btn.config(text='Cancelling...')
            return
        root.destroy()

    def show_error(self, *args):
        err = traceback.format_exception(*args)
        messagebox.showerror("Error", err[-1])
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    url_label = tk.Label(root, text='URL', font=('calibre', 10, 'bold'))
    url_label.grid(row=0, column=0)
    url_entry = tk.Entry(root, textvariable=url_var, font=('calibre', 10, 'normal'), width=80)
    url_entry.grid(row=0, column=1)
    sub_btn = tk.Button(root, text='Download', command=download_video)
    sub_btn.grid(row=1, column=1)
    progressbar = ttk.Progressbar()
    progressbar.grid(row=2, column=0, columnspan=2, sticky='we')

    tk.Tk.report_callback_exception = show_error
    root.mainloop()


if __name__ == "__main__":
    main()
