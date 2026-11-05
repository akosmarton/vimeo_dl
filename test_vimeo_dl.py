import json
import threading

import pytest
import requests

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


def test_pick_by_height_falls_back_to_closest_height():
    assert pick_height([{"height": 1080}, {"height": 480}, {"height": 240}]) == 480
    assert pick_height([{"height": 270}]) == 270


def test_pick_by_height_raises_without_variants():
    with pytest.raises(RuntimeError, match="No video variants"):
        pick_height([])


def test_pick_by_height_prefers_exact_requested_height():
    items = [{"height": 720}, {"height": 1080}, {"height": 540}]
    assert vimeo_dl.pick_by_height(items, lambda v: v["height"], 1080)["height"] == 1080


def test_pick_by_height_uses_closest_variant_to_requested_height():
    items = [{"height": 720}, {"height": 480}, {"height": 240}]
    assert vimeo_dl.pick_by_height(items, lambda v: v["height"], 540)["height"] == 480
    assert vimeo_dl.pick_by_height(items, lambda v: v["height"], 900)["height"] == 720


def test_pick_by_height_best_returns_highest_variant():
    items = [{"height": 720}, {"height": 2160}, {"height": 540}]
    assert vimeo_dl.pick_by_height(items, lambda v: v["height"], vimeo_dl.QUALITY_BEST)["height"] == 2160


def test_parse_quality_accepts_auto_best_and_heights():
    assert vimeo_dl.parse_quality("auto") is None
    assert vimeo_dl.parse_quality("BEST") == vimeo_dl.QUALITY_BEST
    assert vimeo_dl.parse_quality("1080") == 1080
    assert vimeo_dl.parse_quality(" 720p ") == 720
    with pytest.raises(ValueError, match="invalid quality"):
        vimeo_dl.parse_quality("high")
    with pytest.raises(ValueError, match="positive"):
        vimeo_dl.parse_quality("0")


def test_load_hls_tracks_selects_requested_height(monkeypatch):
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n720.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2,RESOLUTION=1920x1080\n1080.m3u8\n"
    )
    monkeypatch.setattr(vimeo_dl, "load_hls_media", lambda url: (b"", [(url, None)]))
    tracks = vimeo_dl.load_hls_tracks("https://cdn.example/master.m3u8", master, 1080)
    assert tracks["video"][1] == [("https://cdn.example/1080.m3u8", None)]
    assert tracks["height"] == 1080


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


def test_player_config_parses_embedded_json():
    page = (
        '<script>window.playerConfig = {"request": {"files": {"hls": {"default_cdn": "a"}}}};</script>'
        "<script>window.other = 1;</script>"
    )
    assert vimeo_dl.player_config(page)["request"]["files"]["hls"]["default_cdn"] == "a"


def test_player_config_returns_none_without_config():
    assert vimeo_dl.player_config("<html><body>no player here</body></html>") is None


def test_player_config_returns_none_for_broken_json():
    assert vimeo_dl.player_config("<script>window.playerConfig = {broken</script>") is None


def test_player_page_url_skips_foreign_hosts_and_unescapes_content():
    page = (
        '<meta property="og:type" content="video.other">'
        '<meta property="og:video:url" content="https://evil.example/video/1">'
        '<meta property="og:video:url" content="https://player.vimeo.com/video/1?h=abc&amp;x=1">'
    )
    assert vimeo_dl.player_page_url(page) == "https://player.vimeo.com/video/1?h=abc&x=1"


def test_player_page_url_returns_none_without_vimeo_player():
    assert vimeo_dl.player_page_url('<meta name="twitter:player" content="https://evil.example/1">') is None


def test_hls_master_url_uses_avc_url_of_default_cdn():
    config = {
        "request": {
            "files": {
                "hls": {
                    "default_cdn": "b",
                    "cdns": {"a": {"url": "https://a.example/m.m3u8"}, "b": {"url": "https://b.example/m.m3u8", "avc_url": "https://b.example/avc.m3u8"}},
                }
            }
        }
    }
    assert vimeo_dl.hls_master_url(config) == "https://b.example/avc.m3u8"


def test_hls_master_url_falls_back_to_first_cdn():
    config = {"request": {"files": {"hls": {"cdns": {"only": {"url": "https://only.example/m.m3u8"}}}}}}
    assert vimeo_dl.hls_master_url(config) == "https://only.example/m.m3u8"


def test_hls_master_url_rejects_config_without_hls():
    with pytest.raises(RuntimeError, match="No HLS playlist"):
        vimeo_dl.hls_master_url({"request": {"files": {"dash": {"cdns": {}}}}})


class FakePage:
    def __init__(self, text, content_type="text/html"):
        self.text = text
        self.headers = {"content-type": content_type}

    def json(self):
        raise ValueError("not json")


def test_load_tracks_resolves_watch_page_to_signed_playlist(monkeypatch):
    watch_url = "https://vimeo.com/1"
    player_url = "https://player.vimeo.com/video/1?h=abc"
    master_url = "https://cdn.example/master.m3u8?exp=1"
    watch_page = f'<meta property="og:video:url" content="{player_url}">'
    config = {
        "request": {
            "files": {
                "hls": {
                    "default_cdn": "d",
                    "cdns": {"d": {"url": master_url}},
                }
            }
        }
    }
    player_page = f"<script>window.playerConfig = {json.dumps(config)};</script>"
    responses = {
        watch_url: FakePage(watch_page),
        player_url: FakePage(player_page),
        master_url: FakePage(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-high",DEFAULT=YES,URI="audio.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=960x540,AUDIO="audio-high"\n'
            "video.m3u8\n",
            "application/x-mpegURL",
        ),
        "https://cdn.example/video.m3u8": FakePage("#EXTM3U\n#EXTINF:1,\nseg0.m4s\n#EXT-X-ENDLIST\n", "application/x-mpegURL"),
        "https://cdn.example/audio.m3u8": FakePage("#EXTM3U\n#EXTINF:1,\naseg0.m4s\n#EXT-X-ENDLIST\n", "application/x-mpegURL"),
    }
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url, headers=None: responses[url])
    tracks = vimeo_dl.load_tracks(watch_url)
    assert tracks["video"][1] == [("https://cdn.example/seg0.m4s", None)]
    assert tracks["audio"][1] == [("https://cdn.example/aseg0.m4s", None)]


def test_load_tracks_rejects_page_without_player(monkeypatch):
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url, headers=None: FakePage("<html>nothing</html>"))
    with pytest.raises(RuntimeError, match="No Vimeo player found"):
        vimeo_dl.load_tracks("https://example.com/page")


def test_load_tracks_reports_cloudflare_challenge(monkeypatch):
    challenge = '<script>function turnstileLoad() { turnstile.render("#w", {}); }</script>'
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url, headers=None: FakePage(challenge))
    with pytest.raises(RuntimeError, match="Cloudflare challenge"):
        vimeo_dl.load_tracks("https://vimeo.com/1")


def test_load_tracks_reports_cloudflare_challenge_on_player_page(monkeypatch):
    responses = {
        "https://vimeo.com/1": FakePage('<meta property="og:video:url" content="https://player.vimeo.com/video/1?h=x">'),
        "https://player.vimeo.com/video/1?h=x": FakePage('<script>function turnstileLoad() {}</script>'),
    }
    monkeypatch.setattr(vimeo_dl, "http_get", lambda url, headers=None: responses[url])
    with pytest.raises(RuntimeError, match="Cloudflare challenge"):
        vimeo_dl.load_tracks("https://vimeo.com/1")


def test_load_tracks_reports_refused_request(monkeypatch):
    def refuse(url, headers=None):
        raise requests.HTTPError("401", response=FakeResponse(401))

    monkeypatch.setattr(vimeo_dl, "http_get", refuse)
    with pytest.raises(RuntimeError, match="HTTP 401/403"):
        vimeo_dl.load_tracks("https://vimeo.com/1")


def test_page_get_reports_refused_player_page(monkeypatch):
    def refuse(url, headers=None):
        raise requests.HTTPError("403", response=FakeResponse(403))

    monkeypatch.setattr(vimeo_dl, "http_get", refuse)
    with pytest.raises(RuntimeError, match="Cloudflare challenge"):
        vimeo_dl.page_get("https://player.vimeo.com/video/1?h=x")
