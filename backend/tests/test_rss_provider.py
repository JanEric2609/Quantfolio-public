"""Tests for RSS feed provider service."""

from unittest.mock import patch


from app.foundation.rss_provider import (
    parse_feed_xml,
    fetch_feed,
    fetch_all_feeds,
    invalidate_cache,
    get_default_feed_urls,
    _clean_html,
    _extract_domain,
    _feed_cache,
)


SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test News Feed</title>
    <link>https://example.com</link>
    <description>A test RSS feed</description>
    <item>
      <title>Market Rally Continues</title>
      <link>https://example.com/news/1</link>
      <description>Stocks rose sharply on strong earnings reports.</description>
      <pubDate>Mon, 10 Jun 2026 12:00:00 GMT</pubDate>
      <source>Reuters</source>
    </item>
    <item>
      <title>Fed Signals Rate Pause</title>
      <link>https://example.com/news/2</link>
      <description>Federal Reserve indicates steady rates.</description>
      <pubDate>Mon, 10 Jun 2026 11:00:00 GMT</pubDate>
      <source>MarketWatch</source>
    </item>
    <item>
      <title></title>
      <link></link>
      <description>Empty item should be skipped</description>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test Feed</title>
  <entry>
    <title>AI Stocks Surge</title>
    <link href="https://example.com/atom/1"/>
    <summary>AI-related stocks see major gains.</summary>
    <published>2026-06-10T10:00:00Z</published>
  </entry>
  <entry>
    <title>Oil Prices Drop</title>
    <link href="https://example.com/atom/2"/>
    <summary>Crude oil falls on supply concerns.</summary>
    <updated>2026-06-10T09:00:00Z</updated>
  </entry>
</feed>
"""

EMPTY_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Empty Feed</title>
    <link>https://example.com</link>
  </channel>
</rss>
"""

INVALID_XML = "This is not valid XML at all <><><><"


def test_parse_rss_feed():
    result = parse_feed_xml(SAMPLE_RSS_XML, "https://test.com/feed")
    assert result["ok"] is True
    assert result["feed_title"] == "Test News Feed"
    assert len(result["items"]) == 2
    assert result["items"][0]["title"] == "Market Rally Continues"
    assert result["items"][0]["link"] == "https://example.com/news/1"
    assert result["items"][0]["source"] == "Reuters"
    assert "Stocks rose sharply" in result["items"][0]["summary"]


def test_parse_rss_empty_item_skipped():
    result = parse_feed_xml(SAMPLE_RSS_XML, "https://test.com/feed")
    assert len(result["items"]) == 2
    assert all(item["title"] for item in result["items"])


def test_parse_atom_feed():
    result = parse_feed_xml(SAMPLE_ATOM_XML, "https://test.com/atom")
    assert result["ok"] is True
    assert result["feed_title"] == "Atom Test Feed"
    assert len(result["items"]) == 2
    assert result["items"][0]["title"] == "AI Stocks Surge"
    assert result["items"][0]["link"] == "https://example.com/atom/1"


def test_parse_empty_feed():
    result = parse_feed_xml(EMPTY_FEED_XML, "https://test.com/empty")
    assert result["ok"] is False
    assert result["items"] == []
    assert "No items found" in result["error"]


def test_parse_invalid_xml():
    result = parse_feed_xml(INVALID_XML, "https://test.com/bad")
    assert result["ok"] is False
    assert result["items"] == []
    assert "parse error" in result["error"].lower() or "XML" in result["error"]


def test_clean_html():
    assert _clean_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert _clean_html("No tags here") == "No tags here"
    assert _clean_html("<a href='x'>Link</a> text") == "Link text"
    assert _clean_html("") == ""


def test_extract_domain():
    assert _extract_domain("https://www.reuters.com/business") == "Reuters"
    assert _extract_domain("https://feeds.marketwatch.com/top") == "Marketwatch"
    assert _extract_domain("https://example.com") == "Example"
    assert _extract_domain("not-a-url") == "Unknown"
    assert _extract_domain("https://finance.yahoo.com/news") == "Yahoo"


def test_default_feed_urls():
    urls = get_default_feed_urls()
    assert len(urls) == 7
    assert all(isinstance(u, str) and u.startswith("http") for u in urls)


def test_cache_invalidation():
    _feed_cache["test_url"] = {"items": [], "fetched_at": 0, "feed_title": "cached"}
    invalidate_cache("test_url")
    assert "test_url" not in _feed_cache


def test_cache_clear_all():
    _feed_cache["url1"] = {"items": [], "fetched_at": 0}
    _feed_cache["url2"] = {"items": [], "fetched_at": 0}
    invalidate_cache()
    assert len(_feed_cache) == 0


def test_fetch_feed_returns_cached():
    _feed_cache["https://cached.com/feed"] = {
        "items": [{"title": "Cached Item", "link": "", "summary": "", "published": "", "source": ""}],
        "feed_title": "Cached Feed",
        "fetched_at": 9999999999,
    }
    result = fetch_feed("https://cached.com/feed")
    assert result["ok"] is True
    assert result["cached"] is True
    assert len(result["items"]) == 1
    _feed_cache.clear()


def test_fetch_feed_http_error():
    with patch("app.foundation.rss_provider.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = Exception("Connection refused")
        result = fetch_feed("https://unreachable.com/feed")
        assert result["ok"] is False
        assert "error" in result


def test_fetch_feed_pins_request_to_validated_ip():
    """DNS-rebinding regression test: fetch_feed() must connect to the IP
    address it validated via validate_outbound_url(), not let httpx re-resolve
    the hostname independently at connect time (GHSA-489g-7rxv-6c8q shape).
    """
    invalidate_cache()
    validated_ip = "93.184.216.34"

    def fake_getaddrinfo(host, *args, **kwargs):
        assert host == "rebind-test.example"
        return [(2, 1, 6, "", (validated_ip, 0))]

    with patch("app.foundation.core.net.socket.getaddrinfo", side_effect=fake_getaddrinfo) as mock_resolve, \
            patch("app.foundation.rss_provider.httpx.Client") as mock_client_cls:
        mock_get = mock_client_cls.return_value.__enter__.return_value.get
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = SAMPLE_RSS_XML

        result = fetch_feed("https://rebind-test.example/feed")

        # Exactly one DNS resolution happened (inside validate_outbound_url);
        # the pinned client.get() was never given the chance to resolve the
        # hostname again.
        assert mock_resolve.call_count == 1
        assert mock_get.call_count == 1

        called_url = mock_get.call_args.args[0]
        called_kwargs = mock_get.call_args.kwargs
        assert called_url == f"https://{validated_ip}:443/feed"
        assert called_kwargs["headers"]["Host"] == "rebind-test.example"
        assert called_kwargs["extensions"] == {"sni_hostname": "rebind-test.example"}

    assert result["ok"] is True
    invalidate_cache()


def test_fetch_all_feeds():
    invalidate_cache()
    with patch("app.foundation.rss_provider.fetch_feed") as mock_fetch:
        mock_fetch.side_effect = [
            {"ok": True, "items": [{"title": "A", "link": "", "summary": "", "published": "", "source": ""}], "feed_title": "Feed A"},
            {"ok": False, "items": [], "feed_title": "", "error": "timeout"},
        ]
        with patch("app.foundation.rss_provider.get_feed_urls") as mock_urls:
            mock_urls.return_value = ["https://a.com", "https://b.com"]
            result = fetch_all_feeds()
            assert len(result["items"]) == 1
            assert len(result["errors"]) == 1
            assert "Feed A" in result["sources"]
    invalidate_cache()


def test_get_feed_urls_defaults():
    from app.foundation.rss_provider import get_feed_urls
    with patch.dict("os.environ", {}, clear=True):
        urls = get_feed_urls(db=None)
        assert len(urls) == 7
        assert all(isinstance(u, str) for u in urls)


def test_get_feed_urls_env_var():
    from app.foundation.rss_provider import get_feed_urls
    with patch.dict("os.environ", {"RSS_FEED_URLS": "https://custom.com/feed1, https://custom.com/feed2"}):
        urls = get_feed_urls(db=None)
        assert urls == ["https://custom.com/feed1", "https://custom.com/feed2"]
