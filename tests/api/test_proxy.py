"""Tests for X-PSX-Proxy passthrough and its SSRF hardening — no network."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psxdata import PSXClient
from psxdata.proxy import normalize_proxy
from psxdata.scrapers import token as token_module

from api.main import app
from api.proxy import PROXY_HEADER, ProxyPassthrough, pin_proxy

PUBLIC_IP = "93.184.216.34"
PROXY = "http://alice:s3cret@proxy.example.com:8080"
PINNED = f"http://alice:s3cret@{PUBLIC_IP}:8080"


def resolver_for(*addrs: str):
    return lambda host, port: list(addrs)


def no_connect(host: str, port: int) -> None:
    return None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def enabled() -> ProxyPassthrough:
    passthrough = ProxyPassthrough(True, resolver=resolver_for(PUBLIC_IP), connector=no_connect)
    app.state.proxy_passthrough = passthrough
    return passthrough


def _quote_df() -> pd.DataFrame:
    return pd.DataFrame([{"symbol": "ENGRO", "sector": "Fertilizer", "price": 300.0}])


# ---------------------------------------------------------------------------
# pin_proxy — validation and SSRF checks
# ---------------------------------------------------------------------------

class TestPinProxy:
    def test_pins_hostname_to_resolved_public_ip(self):
        assert pin_proxy(PROXY, resolver_for(PUBLIC_IP)) == PINNED

    def test_socks5h_without_credentials(self):
        assert pin_proxy("socks5h://p.example.com:1080", resolver_for(PUBLIC_IP)) == (
            f"socks5h://{PUBLIC_IP}:1080"
        )

    def test_prefers_ipv4_and_brackets_ipv6(self):
        v6 = "2606:4700:4700::1111"
        assert pin_proxy("http://p:8080", resolver_for(v6, PUBLIC_IP)).endswith(f"{PUBLIC_IP}:8080")
        assert pin_proxy("http://p:8080", resolver_for(v6)) == f"http://[{v6}]:8080"

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",          # loopback
            "10.0.0.5",           # private
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",    # cloud metadata / link-local
            "100.64.0.1",         # carrier-grade NAT
            "0.0.0.0",
            "224.0.0.1",          # multicast
            "::1",
            "fd00::1",            # unique local
            "fe80::1%eth0",       # link-local with scope id
            "::ffff:10.0.0.1",    # IPv4-mapped private
            "2002:0a00:0001::1",  # 6to4 wrapping 10.0.0.1
        ],
    )
    def test_rejects_non_public_addresses(self, address):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy("http://p.example.com:8080", resolver_for(address))
        assert excinfo.value.status_code == 400

    def test_rejects_when_any_resolved_address_is_private(self):
        with pytest.raises(HTTPException):
            pin_proxy("http://p.example.com:8080", resolver_for(PUBLIC_IP, "10.0.0.1"))

    def test_rejects_ip_literal_private_host(self):
        def resolver(host, port):
            return [host]
        with pytest.raises(HTTPException):
            pin_proxy("http://169.254.169.254:8080", resolver)

    @pytest.mark.parametrize(
        "url",
        [
            "https://p.example.com:8443",      # https proxies cannot be IP-pinned
            "ftp://p.example.com:2121",
            "p.example.com:8080",
            "http://p.example.com",            # no explicit port
            "http://p.example.com:22",         # low port
            "http://p.example.com:6379x",
            "http://p.example.com:8080/path",
            "http://p.example.com:8080?x=1",
            "http://:8080",
            "",
            "http://p.example.com:8080" + "a" * 2048,
        ],
    )
    def test_rejects_malformed_or_disallowed_urls(self, url):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy(url, resolver_for(PUBLIC_IP))
        assert excinfo.value.status_code == 400

    def test_unresolvable_host(self):
        def resolver(host, port):
            raise OSError("no such host")
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy(PROXY, resolver)
        assert "resolved" in excinfo.value.detail

    def test_rejection_never_echoes_credentials(self):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy("http://alice:s3cret@p.example.com:8080", resolver_for("10.0.0.1"))
        assert "s3cret" not in excinfo.value.detail
        assert "alice" not in excinfo.value.detail


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class TestEndpoints:
    def test_no_header_uses_shared_default(self, client):
        with patch("psxdata.quote", return_value=_quote_df()) as default_quote:
            resp = client.get("/stocks/ENGRO/quote")
        assert resp.status_code == 200
        default_quote.assert_called_once_with("ENGRO")

    def test_header_rejected_when_disabled(self, client):
        with patch("psxdata.quote") as default_quote:
            resp = client.get("/stocks/ENGRO/quote", headers={PROXY_HEADER: PROXY})
        assert resp.status_code == 400
        assert "not enabled" in resp.json()["error"]["message"]
        default_quote.assert_not_called()

    def test_proxied_request_uses_pinned_proxy_and_no_cache(self, client, enabled):
        with (
            patch.object(PSXClient, "quote", return_value=_quote_df()) as proxied_quote,
            patch("psxdata.quote") as default_quote,
        ):
            resp = client.get("/stocks/ENGRO/quote", headers={PROXY_HEADER: PROXY})
        assert resp.status_code == 200
        default_quote.assert_not_called()
        proxied_quote.assert_called_once_with("ENGRO", cache=False)
        pooled = enabled._clients[PINNED]
        assert pooled._historical._session.proxies == {"http": PINNED, "https": PINNED}

    def test_proxied_historical_bypasses_shared_cache(self, client, enabled):
        df = pd.DataFrame({
            "date": [pd.Timestamp("2024-01-05")],
            "open": [1.0], "high": [2.0], "low": [0.5],
            "close": [1.5], "volume": [10], "is_anomaly": [False],
        })
        with patch.object(PSXClient, "stocks", return_value=df) as proxied_stocks:
            resp = client.get("/stocks/ENGRO/historical", headers={PROXY_HEADER: PROXY})
        assert resp.status_code == 200
        assert resp.headers["X-Cache"] == "BYPASS"
        assert resp.json()["meta"]["cached"] is False
        proxied_stocks.assert_called_once_with("ENGRO", cache=False)
        # The shared cache was not populated: an unproxied call still misses
        with patch("psxdata.stocks", return_value=df):
            resp = client.get("/stocks/ENGRO/historical")
        assert resp.headers["X-Cache"] == "MISS"

    def test_private_proxy_rejected_with_400(self, client):
        app.state.proxy_passthrough = ProxyPassthrough(
            True, resolver=resolver_for("169.254.169.254"), connector=no_connect
        )
        with patch.object(PSXClient, "quote") as proxied_quote:
            resp = client.get("/stocks/ENGRO/quote", headers={PROXY_HEADER: PROXY})
        assert resp.status_code == 400
        assert "s3cret" not in resp.text
        proxied_quote.assert_not_called()

    def test_unreachable_proxy_returns_502(self, client):
        def refuse(host, port):
            raise ConnectionRefusedError
        app.state.proxy_passthrough = ProxyPassthrough(
            True, resolver=resolver_for(PUBLIC_IP), connector=refuse
        )
        resp = client.get("/stocks/ENGRO/quote", headers={PROXY_HEADER: PROXY})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "proxy_unreachable"
        assert "s3cret" not in resp.text

    def test_connect_check_targets_pinned_ip(self, client, enabled):
        seen = []
        enabled._connector = lambda host, port: seen.append((host, port))
        with patch.object(PSXClient, "quote", return_value=_quote_df()):
            client.get("/stocks/ENGRO/quote", headers={PROXY_HEADER: PROXY})
        assert seen == [(PUBLIC_IP, 8080)]

    def test_per_ip_rate_limit(self, client):
        app.state.proxy_passthrough = ProxyPassthrough(
            True, resolver=resolver_for(PUBLIC_IP), connector=no_connect, per_ip_per_minute=2
        )
        with patch.object(PSXClient, "screener", return_value=pd.DataFrame()):
            codes = [
                client.get("/screener", headers={PROXY_HEADER: PROXY}).status_code
                for _ in range(3)
            ]
        assert codes == [200, 200, 429]

    def test_concurrency_cap(self, client):
        passthrough = ProxyPassthrough(
            True, resolver=resolver_for(PUBLIC_IP), connector=no_connect, max_concurrent=1
        )
        app.state.proxy_passthrough = passthrough
        passthrough._slots.acquire()  # simulate one proxied request in flight
        try:
            resp = client.get("/screener", headers={PROXY_HEADER: PROXY})
        finally:
            passthrough._slots.release()
        assert resp.status_code == 429

    def test_slot_released_after_upstream_error(self, client):
        from psxdata.exceptions import PSXConnectionError

        passthrough = ProxyPassthrough(
            True, resolver=resolver_for(PUBLIC_IP), connector=no_connect, max_concurrent=1
        )
        app.state.proxy_passthrough = passthrough
        with patch.object(PSXClient, "screener", side_effect=PSXConnectionError("down")):
            assert client.get("/screener", headers={PROXY_HEADER: PROXY}).status_code == 503
        with patch.object(PSXClient, "screener", return_value=pd.DataFrame()):
            assert client.get("/screener", headers={PROXY_HEADER: PROXY}).status_code == 200


class TestPooling:
    def test_client_pool_bounded_and_token_providers_pruned(self, monkeypatch):
        monkeypatch.setattr(token_module, "_proxy_providers", {})
        passthrough = ProxyPassthrough(True, max_clients=1)
        first = f"http://{PUBLIC_IP}:8080"
        second = f"http://{PUBLIC_IP}:8081"

        passthrough._client_for(first)
        token_module.get_default_provider(normalize_proxy(first))
        assert len(token_module._proxy_providers) == 1

        passthrough._client_for(second)
        assert list(passthrough._clients) == [second]
        assert token_module._proxy_providers == {}

    def test_same_proxy_reuses_client(self):
        passthrough = ProxyPassthrough(True)
        url = f"http://{PUBLIC_IP}:8080"
        assert passthrough._client_for(url) is passthrough._client_for(url)


class TestFromEnv:
    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("no", False),
    ])
    def test_flag(self, value, expected):
        assert ProxyPassthrough.from_env({"PSX_PROXY_PASSTHROUGH": value}).enabled is expected

    def test_default_off(self):
        assert ProxyPassthrough.from_env({}).enabled is False
