"""Per-request PSX proxy passthrough (``X-PSX-Proxy`` header) with SSRF hardening.

A caller may send ``X-PSX-Proxy: <url>`` to have this server fetch PSX data
through the caller's proxy. The server then opens a connection to an address
the caller chose, so every proxy is checked before use:

- The feature is off unless ``PSX_PROXY_PASSTHROUGH`` is set to a true value.
- Only ``http``, ``socks5`` and ``socks5h`` proxies. ``https`` proxies are
  refused: TLS to the proxy verifies its hostname, which rules out IP pinning.
- An explicit port is required: 80, 443, or 1024-65535.
- Every address the host resolves to must be public (globally routable), and
  the connection is pinned to the checked IP, so DNS rebinding cannot point it
  somewhere else between the check and the request.
- A short TCP connect check rejects dead proxies before any PSX request.
- Per-IP rate limit, a global concurrency cap, and a bounded pool of clients.

Proxied requests share the same caches as all other requests: the proxy only
changes the egress to PSX, not the data. PSX is HTTPS-only and certificates are
verified, so a proxy tunnels TLS end to end and cannot alter what it relays.

Credentials in the proxy URL are never logged or echoed back.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import psxdata
from fastapi import Header, HTTPException, Request
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from psxdata import PSXClient
from psxdata.proxy import normalize_proxy
from psxdata.scrapers import token as token_module
from slowapi.util import get_remote_address

PROXY_HEADER = "X-PSX-Proxy"
ENABLE_ENV = "PSX_PROXY_PASSTHROUGH"
ALLOWED_SCHEMES = frozenset({"http", "socks5", "socks5h"})
MAX_PROXY_URL_LENGTH = 2048
CONNECT_TIMEOUT = 5.0
MAX_CLIENTS = 32
MAX_CONCURRENT = 4
PER_IP_PER_MINUTE = 10

Resolver = Callable[[str, int], list[str]]
Connector = Callable[[str, int], None]


class ProxyUnreachableError(Exception):
    """The caller's proxy passed validation but did not accept a TCP connection."""


def _resolve(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _connect(host: str, port: int) -> None:
    socket.create_connection((host, port), timeout=CONNECT_TIMEOUT).close()


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 addresses embedded in IPv6 must be public too
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        return all(_is_public(e) for e in embedded if e is not None)
    return True


def _reject(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"{PROXY_HEADER}: {message}")


def pin_proxy(raw: str, resolver: Resolver = _resolve) -> str:
    """Validate a caller-supplied proxy URL and return it pinned to a checked IP.

    Raises:
        HTTPException: 400 for any rejected proxy. The message never includes
            the URL, so credentials are not echoed back.
    """
    raw = raw.strip()
    if not raw or len(raw) > MAX_PROXY_URL_LENGTH:
        raise _reject("must be a proxy URL of at most 2048 characters")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _reject("scheme must be http://, socks5:// or socks5h://")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise _reject("must not contain a path, query or fragment")
    try:
        port = parts.port
    except ValueError:
        raise _reject("invalid port") from None
    if port is None:
        raise _reject("must include an explicit port")
    if not (port in (80, 443) or 1024 <= port <= 65535):
        raise _reject("port must be 80, 443, or between 1024 and 65535")
    host = parts.hostname
    if not host:
        raise _reject("must include a host")

    try:
        ips = {ipaddress.ip_address(addr.split("%", 1)[0]) for addr in resolver(host, port)}
    except (OSError, UnicodeError, ValueError):
        raise _reject("host could not be resolved") from None
    if not ips or not all(_is_public(ip) for ip in ips):
        raise _reject("host must resolve only to public internet addresses")

    ip = min(ips, key=lambda a: (a.version, int(a)))  # prefer IPv4
    host_part = f"[{ip}]" if ip.version == 6 else str(ip)
    userinfo = parts.netloc.rpartition("@")[0] + "@" if "@" in parts.netloc else ""
    pinned = f"{scheme}://{userinfo}{host_part}:{port}"
    try:
        normalize_proxy(pinned)
    except (ValueError, TypeError):
        raise _reject("invalid proxy URL") from None
    except ImportError:
        raise _reject("SOCKS proxies are not available on this server") from None
    return pinned


def _proxy_key(pinned: str) -> frozenset[tuple[str, str]]:
    # Same key psxdata uses for its per-proxy token providers
    return frozenset((normalize_proxy(pinned) or {}).items())


class PsxSource:
    """Where a request's PSX data comes from: the shared default client, or a proxied one.

    Both read and write the same caches; only the route to PSX differs.
    """

    def __init__(self, client: PSXClient | None = None) -> None:
        self._client = client

    @property
    def proxied(self) -> bool:
        return self._client is not None

    def fetch(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._client is None:
            return getattr(psxdata, name)(*args, **kwargs)
        return getattr(self._client, name)(*args, **kwargs)


class ProxyPassthrough:
    """Validates caller proxies and hands out bounded, pooled proxied clients."""

    def __init__(
        self,
        enabled: bool,
        *,
        resolver: Resolver = _resolve,
        connector: Connector = _connect,
        max_clients: int = MAX_CLIENTS,
        max_concurrent: int = MAX_CONCURRENT,
        per_ip_per_minute: int = PER_IP_PER_MINUTE,
    ) -> None:
        self.enabled = enabled
        self._resolver = resolver
        self._connector = connector
        self._max_clients = max_clients
        self._clients: OrderedDict[str, PSXClient] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._rate_limiter = MovingWindowRateLimiter(MemoryStorage())
        self._rate = RateLimitItemPerMinute(per_ip_per_minute)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProxyPassthrough:
        enabled = env.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(enabled)

    @contextmanager
    def acquire(self, request: Request, raw: str) -> Iterator[PsxSource]:
        """Validate *raw*, reserve a concurrency slot, and yield a proxied source."""
        if not self.enabled:
            raise HTTPException(
                status_code=400, detail=f"{PROXY_HEADER} is not enabled on this server"
            )
        # Rate-limit before DNS resolution so the check itself cannot be spammed
        if not self._rate_limiter.hit(self._rate, "psx-proxy", get_remote_address(request)):
            raise HTTPException(status_code=429, detail="Too many proxied requests")
        pinned = pin_proxy(raw, self._resolver)
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status_code=429, detail="Too many proxied requests in progress; retry shortly"
            )
        try:
            parts = urlsplit(pinned)
            try:
                self._connector(parts.hostname or "", parts.port or 0)
            except OSError:
                raise ProxyUnreachableError(
                    f"{PROXY_HEADER}: proxy did not accept a connection"
                ) from None
            yield PsxSource(self._client_for(pinned))
        finally:
            self._slots.release()

    def _client_for(self, pinned: str) -> PSXClient:
        with self._lock:
            client = self._clients.get(pinned)
            if client is not None:
                self._clients.move_to_end(pinned)
                return client
            # Default cache_dir: the same on-disk cache the module-level functions use
            client = PSXClient(proxy=pinned)
            self._clients[pinned] = client
            if len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
                self._prune_token_providers()
            return client

    def _prune_token_providers(self) -> None:
        """Drop psxdata's per-proxy token providers for clients no longer pooled.

        psxdata keeps one provider per proxy for the life of the process; on a
        server fed arbitrary proxies that would grow without bound.
        """
        live = {_proxy_key(url) for url in self._clients}
        with token_module._default_lock:
            for key in [k for k in token_module._proxy_providers if k not in live]:
                del token_module._proxy_providers[key]


def get_proxy_passthrough(request: Request) -> ProxyPassthrough:
    """Return the app's ProxyPassthrough; build one from the environment if lifespan did not run."""
    passthrough: ProxyPassthrough | None = getattr(request.app.state, "proxy_passthrough", None)
    if passthrough is None:
        passthrough = ProxyPassthrough.from_env(os.environ)
        request.app.state.proxy_passthrough = passthrough
    return passthrough


def psx_source(
    request: Request,
    x_psx_proxy: str | None = Header(
        default=None,
        alias=PROXY_HEADER,
        description=(
            "Optional proxy for this request's PSX traffic: http://, socks5:// or socks5h://, "
            "with an explicit port and optional user:pass@. Must resolve to a public address. "
            "Proxied requests share the normal cache but have stricter rate limits. Only "
            "honoured when the server enables proxy passthrough."
        ),
    ),
) -> Iterator[PsxSource]:
    """FastAPI dependency: the PSX data source for this request."""
    if x_psx_proxy is None:
        yield PsxSource()
        return
    with get_proxy_passthrough(request).acquire(request, x_psx_proxy) as source:
        yield source
