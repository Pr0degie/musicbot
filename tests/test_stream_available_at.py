"""stream_wait_seconds: Wartezeit bis zur Freigabe einer YouTube-Stream-URL."""

from utils.ffmpeg import STREAM_AVAILABLE_MAX_WAIT, stream_wait_seconds


def test_waits_until_available_at():
    assert stream_wait_seconds({"available_at": 1004}, now=1000.0) == 4.0


def test_no_wait_without_or_past_available_at():
    assert stream_wait_seconds({}, now=1000.0) == 0.0
    assert stream_wait_seconds(None, now=1000.0) == 0.0
    assert stream_wait_seconds({"available_at": "1004"}, now=1000.0) == 0.0
    assert stream_wait_seconds({"available_at": 990}, now=1000.0) == 0.0


def test_wait_is_capped():
    assert stream_wait_seconds({"available_at": 5000}, now=1000.0) == STREAM_AVAILABLE_MAX_WAIT
