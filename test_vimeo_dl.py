import pytest
import requests
import threading

import vimeo_dl


def test_parse_hls_attrs_strips_quotes_and_keeps_unquoted_values():
    attrs = vimeo_dl.parse_hls_attrs('TYPE=AUDIO,GROUP-ID="audio-high",DEFAULT=YES,URI="a/b.m3u8?x=1,2"')
    assert attrs == {
        "TYPE": "AUDIO",
        "GROUP-ID": "audio-high",
        "DEFAULT": "YES",
        "URI": "a/b.m3u8?x=1,2",
    }


def test_pick_by_height_prefers_540_over_720():
    items = [{"height": 720}, {"height": 540}]
    assert pick_height(items) == 540


def test_pick_by_height_falls_back_to_720():
    items = [{"height": 1080}, {"height": 720}]
    assert pick_height(items) == 720


def test_pick_by_height_raises_when_neither_exists():
    with pytest.raises(RuntimeError, match="No 540p or 720p"):
        pick_height([{"height": 1080}])


def pick_height(items):
    return vimeo_dl.pick_by_height(items, lambda v: v["height"])["height"]


def test_resolve_byterange_without_offset_continues_previous_range():
    last_end = {}
    assert vimeo_dl.resolve_byterange("100@0", "u", last_end) == (0, 100)
    assert vimeo_dl.resolve_byterange("50", "u", last_end) == (100, 150)


def test_resolve_byterange_with_offset_ignores_previous_range():
    last_end = {"u": 500}
    assert vimeo_dl.resolve_byterange("10@20", "u", last_end) == (20, 30)


def test_load_hls_media_keeps_last_segment(tmp_path, monkeypatch):
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:5.0,\nseg0.m4s\n"
        "#EXTINF:5.0,\nseg1.m4s\n"
        "#EXTINF:5.0,\nseg2.m4s\n"
        "#EXT-X-ENDLIST\n"
    )
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url: type("R", (), {"text": playlist})())
    monkeypatch.setattr(vimeo_dl, "fetch_part", lambda url, byterange=None: b"init")
    init, segments = vimeo_dl.load_hls_media("https://cdn.example/a/media.m3u8")
    assert init == b"init"
    assert [url for url, _ in segments] == [
        "https://cdn.example/a/seg0.m4s",
        "https://cdn.example/a/seg1.m4s",
        "https://cdn.example/a/seg2.m4s",
    ]


def test_load_hls_media_rejects_encryption(monkeypatch):
    playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:1,\nseg0.m4s\n'
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url: type("R", (), {"text": playlist})())
    with pytest.raises(RuntimeError, match="Encrypted HLS"):
        vimeo_dl.load_hls_media("https://cdn.example/media.m3u8")


def test_load_json_tracks_rejects_segment_without_url():
    playlist = {
        "base_url": "https://cdn.example/a/",
        "video": [{"height": 540, "base_url": "v/", "init_segment": "", "segments": [{"url": "s0.m4s"}, {}]}],
        "audio": [],
    }
    with pytest.raises(RuntimeError, match="has no url"):
        vimeo_dl.load_json_tracks("https://cdn.example/a/playlist.json", playlist)


class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def test_fetch_part_retries_transient_server_error(monkeypatch):
    fake = FakeSession([FakeResponse(503), FakeResponse(200, b"segment")])
    monkeypatch.setattr(vimeo_dl, "get_session", lambda: fake)
    monkeypatch.setattr(vimeo_dl.time, "sleep", lambda seconds: None)
    assert vimeo_dl.fetch_part("https://cdn.example/seg.m4s") == b"segment"
    assert fake.calls == 2


def test_fetch_part_does_not_retry_client_error(monkeypatch):
    fake = FakeSession([FakeResponse(404), FakeResponse(200, b"unused")])
    monkeypatch.setattr(vimeo_dl, "get_session", lambda: fake)
    monkeypatch.setattr(vimeo_dl.time, "sleep", lambda seconds: None)
    with pytest.raises(requests.HTTPError):
        vimeo_dl.fetch_part("https://cdn.example/seg.m4s")
    assert fake.calls == 1


def test_fetch_part_gives_up_after_retry_budget(monkeypatch):
    attempts = len(vimeo_dl.RETRY_BACKOFF) + 1
    fake = FakeSession([FakeResponse(503)] * attempts)
    monkeypatch.setattr(vimeo_dl, "get_session", lambda: fake)
    monkeypatch.setattr(vimeo_dl.time, "sleep", lambda seconds: None)
    with pytest.raises(requests.HTTPError):
        vimeo_dl.fetch_part("https://cdn.example/seg.m4s")
    assert fake.calls == attempts


def test_get_session_is_separate_per_thread():
    sessions = []
    thread = threading.Thread(target=lambda: sessions.append(vimeo_dl.get_session()))
    thread.start()
    thread.join()
    assert sessions[0] is not vimeo_dl.get_session()
