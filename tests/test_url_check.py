"""Tests für den SSRF-URL-Validator (utils/url_check.py).

DNS wird über monkeypatch von socket.getaddrinfo simuliert – kein echter
Netzwerkzugriff. Die drei Modi (off/warn/block) steuert config.URL_VALIDATION,
das enforce_url_policy() zur Laufzeit liest.
"""

import asyncio
import socket

import pytest

import config
from utils import url_check
from utils.url_check import check_url, enforce_url_policy


def fake_resolver(mapping):
    """getaddrinfo-Ersatz: Hostname → Liste von IP-Strings."""

    def _fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"unbekannter Host: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in mapping[host]
        ]

    return _fake


# ---------------------------------------------------------------------------
# check_url – Scheme-Prüfung (kein DNS nötig)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://radio.example/stream",
    "file:///etc/passwd",
    "gopher://radio.example/1",
    "radio.example/stream",   # kein Scheme
    "http://",                # kein Hostname
])
def test_check_url_rejects_non_http_schemes(url):
    assert check_url(url) == (False, "scheme")


# ---------------------------------------------------------------------------
# check_url – IP-Prüfung nach DNS-Resolve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "10.0.0.1",        # privat (RFC 1918)
    "172.16.0.5",      # privat (RFC 1918)
    "192.168.1.10",    # privat (RFC 1918)
    "169.254.13.37",   # link-local
    "127.0.0.1",       # loopback
])
def test_check_url_rejects_private_ipv4(monkeypatch, ip):
    monkeypatch.setattr(url_check.socket, "getaddrinfo", fake_resolver({ip: [ip]}))
    assert check_url(f"http://{ip}/stream") == (False, "private")


def test_check_url_rejects_hostname_resolving_to_loopback(monkeypatch):
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"localhost": ["127.0.0.1"]}),
    )
    assert check_url("http://localhost:8080/stream") == (False, "private")


def test_check_url_rejects_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo", fake_resolver({"::1": ["::1"]})
    )
    assert check_url("http://[::1]:8000/stream") == (False, "private")


def test_check_url_accepts_public_ip(monkeypatch):
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"radio.example": ["93.184.216.34"]}),
    )
    assert check_url("https://radio.example/stream") == (True, None)


def test_check_url_allows_unresolvable_host(monkeypatch):
    """Nicht auflösbar → ok; der Stream-Start scheitert dann von selbst."""
    monkeypatch.setattr(url_check.socket, "getaddrinfo", fake_resolver({}))
    assert check_url("http://gibtsnicht.example/stream") == (True, None)


# ---------------------------------------------------------------------------
# enforce_url_policy – die drei Modi
# ---------------------------------------------------------------------------

def test_mode_off_skips_check_entirely(monkeypatch, fake_ctx):
    monkeypatch.setattr(config, "URL_VALIDATION", "off")

    def boom(url):
        raise AssertionError("check_url darf im off-Modus nicht laufen")

    monkeypatch.setattr(url_check, "check_url", boom)
    ok = asyncio.run(enforce_url_policy(fake_ctx, "http://192.168.1.1/stream"))
    assert ok is True
    assert fake_ctx.sent == []


def test_mode_warn_plays_but_warns(monkeypatch, fake_ctx):
    monkeypatch.setattr(config, "URL_VALIDATION", "warn")
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"192.168.1.1": ["192.168.1.1"]}),
    )
    ok = asyncio.run(enforce_url_policy(fake_ctx, "http://192.168.1.1/stream"))
    assert ok is True                 # Wiedergabe läuft wie heute
    assert len(fake_ctx.sent) == 1    # aber Hinweis im Channel


def test_mode_warn_valid_url_no_message(monkeypatch, fake_ctx):
    monkeypatch.setattr(config, "URL_VALIDATION", "warn")
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"radio.example": ["93.184.216.34"]}),
    )
    ok = asyncio.run(enforce_url_policy(fake_ctx, "http://radio.example/stream"))
    assert ok is True
    assert fake_ctx.sent == []


def test_mode_block_rejects_private(monkeypatch, fake_ctx):
    monkeypatch.setattr(config, "URL_VALIDATION", "block")
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"192.168.1.1": ["192.168.1.1"]}),
    )
    ok = asyncio.run(enforce_url_policy(fake_ctx, "http://192.168.1.1/stream"))
    assert ok is False
    assert len(fake_ctx.sent) == 1


def test_mode_block_accepts_public(monkeypatch, fake_ctx):
    monkeypatch.setattr(config, "URL_VALIDATION", "block")
    monkeypatch.setattr(
        url_check.socket, "getaddrinfo",
        fake_resolver({"radio.example": ["93.184.216.34"]}),
    )
    ok = asyncio.run(enforce_url_policy(fake_ctx, "http://radio.example/stream"))
    assert ok is True
    assert fake_ctx.sent == []


# ---------------------------------------------------------------------------
# Flag-Parsing – Default und ungültige Werte fallen auf "warn" zurück
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "warn"),        # Flag nicht gesetzt
    ("", "warn"),
    ("warn", "warn"),
    ("OFF", "off"),
    ("Block", "block"),
    ("quatsch", "warn"),   # Tippfehler → sicherer Default
])
def test_parse_url_validation(raw, expected):
    assert config._parse_url_validation(raw) == expected
