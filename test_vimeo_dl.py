import pytest

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
