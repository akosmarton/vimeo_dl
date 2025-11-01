#!/usr/bin/python3
import traceback
import requests
import os
import urllib.parse
import base64
from ffmpeg import FFmpeg
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from wakepy import keep


def download_video():
    sub_btn.config(state=tk.DISABLED)
    sub_btn.config(text='Downloading...')
    url_entry.config(state=tk.DISABLED)
    root.update()

    with keep.running():
        master_json_url = url_var.get()
        r = requests.get(master_json_url)
        playlist = r.json()
        base_url = urllib.parse.urljoin(master_json_url, playlist.get("base_url"))
        video = playlist.get("video", [])[0]
        audio = playlist.get("audio", [])[0]
        video_base_url = urllib.parse.urljoin(base_url, video.get("base_url"))
        audio_base_url = urllib.parse.urljoin(base_url, audio.get("base_url"))

        fnv = playlist.get("clip_id") + "-video.mp4"
        fna = playlist.get("clip_id") + "-audio.mp4"

        fv = os.open(fnv, os.O_CREAT | os.O_WRONLY)
        fa = os.open(fna, os.O_CREAT | os.O_WRONLY)

        os.write(fv, base64.b64decode(video.get("init_segment", "")))
        os.write(fa, base64.b64decode(audio.get("init_segment", "")))

        segments_count = min(len(video.get("segments", [])), len(audio.get("segments", [])))
        progressbar.configure(maximum=segments_count)
        for i in range(segments_count - 1):
            progressbar.step()
            root.update()
            vurl = urllib.parse.urljoin(video_base_url, video.get("segments", [])[i].get("url"))
            vr = requests.get(vurl)
            os.write(fv, vr.content)
            aurl = urllib.parse.urljoin(audio_base_url, audio.get("segments", [])[i].get("url"))
            ar = requests.get(aurl)
            os.write(fa, ar.content)

        os.close(fv)
        os.close(fa)

        sub_btn.config(text='Merging...')
        root.update()
        FFmpeg().option("y").input(fnv).input(fna).output(playlist.get("clip_id") + ".mp4", codec="copy").execute()

        os.remove(fnv)
        os.remove(fna)

    messagebox.showinfo("Success", "Download and merge completed!")
    root.destroy()


root = tk.Tk()
root.title('Vimeo Downloader')
url_var = tk.StringVar()
url_label = tk.Label(root, text='URL', font=('calibre', 10, 'bold'))
url_label.grid(row=0, column=0)
url_entry = tk.Entry(root, textvariable=url_var, font=('calibre', 10, 'normal'), width=80)
url_entry.grid(row=0, column=1)
sub_btn = tk.Button(root, text='Download', command=download_video)
sub_btn.grid(row=1, column=1)
progressbar = ttk.Progressbar()
progressbar.grid(row=2, column=0, columnspan=2, sticky='we')


def show_error(self, *args):
    err = traceback.format_exception(*args)
    messagebox.showerror("Error", err[-1])
    root.destroy()


tk.Tk.report_callback_exception = show_error

root.mainloop()
