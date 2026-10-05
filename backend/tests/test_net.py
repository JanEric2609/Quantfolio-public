"""Tests for outbound URL validation / SSRF prevention (app.foundation.core.net)."""

from unittest.mock import patch

import pytest

from app.foundation.core.net import pin_url_to_ip, validate_outbound_url


def _addrinfo(ip: str, family=None):
    import socket as _socket

    fam = family or (_socket.AF_INET6 if ":" in ip else _socket.AF_INET)
    sockaddr = (ip, 0, 0, 0) if fam == _socket.AF_INET6 else (ip, 0)
    return [(fam, _socket.SOCK_STREAM, 6, "", sockaddr)]


class TestValidateOutboundUrl:
    def test_returns_url_and_resolved_ip(self):
        with patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            url, resolved_ip = validate_outbound_url("https://example.com/feed", allow_private=False)
        assert url == "https://example.com/feed"
        assert resolved_ip == "93.184.216.34"

    def test_picks_first_validated_address_when_multiple(self):
        infos = _addrinfo("1.1.1.1") + _addrinfo("8.8.8.8")
        with patch("app.foundation.core.net.socket.getaddrinfo", return_value=infos):
            _, resolved_ip = validate_outbound_url("https://example.com/", allow_private=False)
        assert resolved_ip == "1.1.1.1"

    def test_blocks_link_local_metadata_ip(self):
        with (
            patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")),
            pytest.raises(ValueError, match="link-local"),
        ):
            validate_outbound_url("http://metadata.internal/", allow_private=False)

    def test_blocks_private_ip_when_disallowed(self):
        with (
            patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")),
            pytest.raises(ValueError, match="non-public"),
        ):
            validate_outbound_url("http://internal.example/", allow_private=False)

    @pytest.mark.parametrize("ip", ["100.64.0.1", "100.100.100.100", "198.18.0.1", "224.0.0.1", "2001:db8::1"])
    def test_blocks_non_global_ranges_when_disallowed(self, ip):
        # 100.64.0.0/10 is the Tailscale CGNAT range: neither "private" nor
        # "global" to ipaddress, so an is_private check let it through.
        with (
            patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo(ip)),
            pytest.raises(ValueError, match="non-public"),
        ):
            validate_outbound_url("http://tailnet-host.example/", allow_private=False)

    def test_allows_private_ip_when_allowed(self):
        with patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo("192.168.1.10")):
            _, resolved_ip = validate_outbound_url("http://nas.local/", allow_private=True)
        assert resolved_ip == "192.168.1.10"

    def test_ipv6_resolved_address_returned_unbracketed(self):
        with patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo("2606:2800:220:1:248:1893:25c8:1946")):
            _, resolved_ip = validate_outbound_url("https://example.com/", allow_private=False)
        assert resolved_ip == "2606:2800:220:1:248:1893:25c8:1946"


class TestPinUrlToIp:
    def test_pins_ipv4_and_preserves_path_query(self):
        pinned, hostname, extensions = pin_url_to_ip(
            "https://example.com/feed?x=1", "93.184.216.34"
        )
        assert pinned == "https://93.184.216.34:443/feed?x=1"
        assert hostname == "example.com"
        assert extensions == {"sni_hostname": "example.com"}

    def test_pins_ipv6_with_brackets(self):
        pinned, hostname, _extensions = pin_url_to_ip(
            "https://example.com/feed", "2606:2800:220:1:248:1893:25c8:1946"
        )
        assert pinned == "https://[2606:2800:220:1:248:1893:25c8:1946]:443/feed"
        assert hostname == "example.com"

    def test_uses_explicit_port_when_present(self):
        pinned, _hostname, _extensions = pin_url_to_ip("http://example.com:8080/x", "10.1.2.3")
        assert pinned == "http://10.1.2.3:8080/x"

    def test_http_default_port(self):
        pinned, _, _ = pin_url_to_ip("http://example.com/x", "10.1.2.3")
        assert pinned == "http://10.1.2.3:80/x"


class TestDnsRebindingResistance:
    """Prove that once validate_outbound_url resolves & validates a hostname,
    the actual outbound request is pinned to that IP and does not perform a
    second, independent DNS resolution that an attacker could poison."""

    def test_pinned_request_targets_validated_ip_not_a_later_resolution(self):
        # Attacker's DNS answers an allowed IP at validation time...
        allowed_ip = "93.184.216.34"
        with patch("app.foundation.core.net.socket.getaddrinfo", return_value=_addrinfo(allowed_ip)) as mock_resolve:
            url, resolved_ip = validate_outbound_url("https://rebind.example/feed", allow_private=False)
            assert resolved_ip == allowed_ip
            assert mock_resolve.call_count == 1

        pinned_url, hostname, extensions = pin_url_to_ip(url, resolved_ip)

        # ...and then swaps the record to a blocked metadata IP for the
        # *connect-time* lookup. A caller that pins to `resolved_ip` (as
        # rss_provider.fetch_feed does) never performs that second lookup,
        # so the rebind can't take effect — the pinned URL still targets the
        # address that was actually validated.
        assert pinned_url.startswith(f"https://{allowed_ip}:443/")
        assert "https://rebind.example" not in pinned_url
        assert hostname == "rebind.example"
        assert extensions == {"sni_hostname": "rebind.example"}
