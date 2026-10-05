"""Outbound URL validation for SSRF prevention.

Policy
------
This is a self-hosted personal-finance application.  Its integrations
(LLM, OpenBB, Obsidian) are *expected* to run on localhost or a local
LAN, so loopback (127.x) and RFC-1918 private ranges are ALLOWED by
default.  What is never allowed, regardless of context:

  • Non-http/https schemes (file://, gopher://, dict://, ftp://, …)
  • Link-local addresses (169.254.0.0/16) — covers cloud-metadata
    endpoints such as 169.254.169.254 (AWS/GCP/Azure IMDS), the primary
    SSRF risk for cloud-hosted instances.
  • IPv6 link-local (fe80::/10)
  • The "unspecified" address 0.0.0.0 / ::
  • Hostnames that resolve to any of the above

For callers that explicitly want to prevent private-network access (e.g.
fetching public third-party URLs), pass ``allow_private=False``.  That
additionally blocks loopback and RFC-1918 ranges.

DNS-rebinding / TOCTOU note
----------------------------
``validate_outbound_url`` resolves the hostname *once* and validates every
address it returns.  If a caller then lets its HTTP client re-resolve the
same hostname to make the actual connection, an attacker who controls DNS
for that hostname (e.g. a malicious RSS feed host with a very short TTL)
can serve an allowed address at check-time and a blocked one — such as the
cloud metadata IP ``169.254.169.254`` — at connect-time. This is the
classic DNS-rebinding SSRF bypass (matches GHSA-489g-7rxv-6c8q's shape:
validate-then-separately-fetch).

To close that gap, ``validate_outbound_url`` also returns the resolved IP
it validated. Callers MUST pin their actual outbound request to that IP
(see ``pin_url_to_ip``) instead of letting the HTTP client resolve the
hostname a second time.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urlunparse


# Link-local range — always blocked (cloud IMDS lives here)
_LINK_LOCAL_V4 = ipaddress.IPv4Network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.IPv6Network("fe80::/10")


def _check_ip(addr: str, *, allow_private: bool) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Raise ValueError if *addr* violates the outbound policy; else return the parsed IP."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # addr is a hostname that couldn't be parsed as IP — shouldn't
        # happen after socket.getaddrinfo resolution, but be safe.
        raise ValueError(f"Cannot parse resolved address: {addr!r}")

    # Always block link-local (169.254.x.x / fe80::)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _LINK_LOCAL_V4:
        raise ValueError(
            f"Outbound request to link-local address {ip} is blocked "
            "(cloud metadata endpoint protection)."
        )
    if isinstance(ip, ipaddress.IPv6Address):
        # Check IPv6 link-local
        if ip in _LINK_LOCAL_V6:
            raise ValueError(
                f"Outbound request to IPv6 link-local address {ip} is blocked."
            )
        # Check IPv4-mapped IPv6 addresses (e.g., ::ffff:169.254.169.254)
        if ip.ipv4_mapped:
            mapped_v4 = ip.ipv4_mapped
            if mapped_v4 in _LINK_LOCAL_V4:
                raise ValueError(
                    f"Outbound request to IPv4-mapped IPv6 link-local address {ip} "
                    f"(maps to {mapped_v4}) is blocked (cloud metadata endpoint protection)."
                )

    # Always block the unspecified addresses
    if ip.is_unspecified:
        raise ValueError(f"Outbound request to unspecified address {ip} is blocked.")

    # Conditionally block loopback + private ranges
    if not allow_private:
        if ip.is_loopback:
            raise ValueError(
                f"Outbound request to loopback address {ip} is blocked "
                "in public-target mode."
            )
        # "not global", not just "private": 100.64.0.0/10 (CGNAT — the
        # Tailscale tailnet) is neither private nor global in ipaddress, and
        # must be as unreachable as the LAN from a public-target fetch.
        if not ip.is_global or ip.is_multicast:
            raise ValueError(
                f"Outbound request to non-public address {ip} is blocked "
                "in public-target mode."
            )
    return ip


def validate_outbound_url(url: str, *, allow_private: bool = True) -> tuple[str, str]:
    """Validate *url* before making an outbound HTTP request.

    Parameters
    ----------
    url:
        The URL string supplied by the caller (typically a user-provided
        configuration value).
    allow_private:
        When ``True`` (default) loopback and RFC-1918 private addresses are
        permitted — appropriate for self-hosted integrations (LLM, OpenBB,
        Obsidian) that legitimately run on localhost or a local network.
        When ``False`` loopback and private ranges are also rejected; use
        this for URLs that should only ever reach public internet hosts.

    Returns
    -------
    tuple[str, str]
        ``(url, resolved_ip)`` — the original *url* string, unchanged, and
        the first validated IP address resolved for its hostname (a string,
        IPv6 addresses unbracketed). Callers making the actual outbound
        request MUST use ``resolved_ip`` to pin the connection (see
        ``pin_url_to_ip``) rather than letting the HTTP client re-resolve
        the hostname — otherwise a DNS-rebinding attacker can swap in a
        blocked address between this check and the real connection.

    Raises
    ------
    ValueError
        With a human-readable message if the URL violates policy.  The
        caller should catch this and return a 400-style API error; the
        message is safe to surface to authenticated users but should NOT
        be forwarded verbatim from deeper exceptions.
    """
    if not url or not url.strip():
        raise ValueError("URL must not be empty.")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme {parsed.scheme!r} is not allowed; only http and https are permitted."
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL contains no hostname.")

    # Resolve the hostname to one or more IP addresses and validate each.
    # This catches DNS names that resolve to blocked ranges (e.g. "metadata.internal").
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Hostname {hostname!r} could not be resolved: {exc}") from exc

    if not infos:
        raise ValueError(f"Hostname {hostname!r} resolved to no addresses.")

    resolved_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in infos:
        # sockaddr is (address, port) for IPv4, (address, port, flow, scope) for IPv6
        ip = _check_ip(str(sockaddr[0]), allow_private=allow_private)
        if resolved_ip is None:
            resolved_ip = str(ip)

    assert resolved_ip is not None  # infos is non-empty, loop always runs
    return url, resolved_ip


def pin_url_to_ip(url: str, resolved_ip: str) -> tuple[str, str, dict[str, str]]:
    """Rewrite *url* to connect directly to *resolved_ip*, closing the DNS-rebinding gap.

    Pairs with ``validate_outbound_url``: once a hostname's IP has been
    validated, the actual request must be pinned to that IP rather than
    letting the HTTP client resolve the hostname again (which could return
    a different, blocked address). The original hostname is preserved for
    TLS SNI and the ``Host`` header so virtual-hosted / CDN-backed targets
    and certificate validation keep working.

    Returns
    -------
    tuple[str, str, dict[str, str]]
        ``(pinned_url, hostname, extensions)`` where *pinned_url* has its
        netloc replaced by *resolved_ip* (bracketed for IPv6), *hostname*
        is the original hostname (use it for a ``Host`` header), and
        *extensions* is the httpx request-extensions dict
        (``{"sni_hostname": hostname}``) to pass as ``extensions=...``.

    Example
    -------
    >>> url, resolved_ip = validate_outbound_url(feed_url, allow_private=False)
    >>> pinned_url, hostname, extensions = pin_url_to_ip(url, resolved_ip)
    >>> httpx.get(pinned_url, headers={"Host": hostname, ...}, extensions=extensions)
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL contains no hostname.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip_obj = ipaddress.ip_address(resolved_ip)
    ip_host = f"[{resolved_ip}]" if ip_obj.version == 6 else resolved_ip

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"

    netloc = f"{userinfo}{ip_host}:{port}"
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    extensions = {"sni_hostname": hostname}
    return pinned_url, hostname, extensions
