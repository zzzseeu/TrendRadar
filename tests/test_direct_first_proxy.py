import unittest
from unittest.mock import MagicMock, patch

import requests

from trendradar.crawler.article_content import ArticleContentFetcher
from trendradar.crawler.http import DirectFirstSession
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.core.loader import _load_rss_config


class DirectFirstSessionTests(unittest.TestCase):
    def _build_client(self, *, use_proxy=False, proxy_url="http://proxy:7892"):
        direct = MagicMock(name="direct_session")
        proxy = MagicMock(name="proxy_session")
        patcher = patch(
            "trendradar.crawler.http.requests.Session",
            side_effect=[direct, proxy],
        )
        factory = patcher.start()
        self.addCleanup(patcher.stop)
        client = DirectFirstSession(
            headers={"User-Agent": "test"},
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )
        self.assertEqual(factory.call_count, 2)
        return client, direct, proxy

    def test_sessions_ignore_environment_proxy_and_fallback_is_explicit(self):
        _, direct, proxy = self._build_client()

        self.assertFalse(direct.trust_env)
        self.assertFalse(proxy.trust_env)
        direct.proxies.update.assert_not_called()
        proxy.proxies.update.assert_called_once_with(
            {"http": "http://proxy:7892", "https": "http://proxy:7892"}
        )

    def test_direct_success_does_not_use_proxy(self):
        client, direct, proxy = self._build_client()
        response = MagicMock(status_code=200)
        direct.get.return_value = response

        result = client.get("https://example.com/feed")

        self.assertIs(result, response)
        proxy.get.assert_not_called()

    def test_connection_failure_retries_once_with_proxy(self):
        client, direct, proxy = self._build_client()
        direct.get.side_effect = requests.ConnectionError("dns failed")
        response = MagicMock(status_code=200)
        proxy.get.return_value = response

        result = client.get("https://example.com/feed", timeout=15)

        self.assertIs(result, response)
        proxy.get.assert_called_once_with(
            "https://example.com/feed",
            timeout=15,
        )

    def test_retryable_http_status_retries_with_proxy(self):
        client, direct, proxy = self._build_client()
        direct_response = MagicMock(status_code=403)
        proxy_response = MagicMock(status_code=200)
        direct.get.return_value = direct_response
        proxy.get.return_value = proxy_response

        result = client.get("https://example.com/feed")

        self.assertIs(result, proxy_response)
        direct_response.close.assert_called_once_with()
        proxy.get.assert_called_once_with("https://example.com/feed")

    def test_non_retryable_http_status_does_not_use_proxy(self):
        client, direct, proxy = self._build_client()
        response = MagicMock(status_code=404)
        direct.get.return_value = response

        result = client.get("https://example.com/missing")

        self.assertIs(result, response)
        proxy.get.assert_not_called()

    def test_explicit_proxy_mode_skips_direct_request(self):
        client, direct, proxy = self._build_client(use_proxy=True)
        response = MagicMock(status_code=200)
        proxy.get.return_value = response

        result = client.get("https://example.com/feed")

        self.assertIs(result, response)
        direct.get.assert_not_called()


class NewsFetcherIntegrationTests(unittest.TestCase):
    def test_news_proxy_environment_overrides_local_config_proxy(self):
        config = {
            "rss": {"enabled": True},
            "advanced": {
                "crawler": {"default_proxy": "http://127.0.0.1:10801"},
                "rss": {"use_proxy": False},
            },
        }

        with patch.dict(
            "os.environ",
            {"NEWS_PROXY_URL": "http://host.docker.internal:7892"},
            clear=False,
        ):
            rss_config = _load_rss_config(config)

        self.assertEqual(
            rss_config["PROXY_URL"],
            "http://host.docker.internal:7892",
        )

    def test_rss_fetcher_uses_direct_first_session(self):
        fetcher = RSSFetcher(
            feeds=[RSSFeedConfig(id="test", name="Test", url="https://example.com/feed")],
            proxy_url="http://proxy:7892",
        )

        self.assertIsInstance(fetcher.session, DirectFirstSession)

    def test_article_fetcher_uses_direct_first_session(self):
        fetcher = ArticleContentFetcher(
            proxy_url="http://proxy:7892",
        )

        self.assertIsInstance(fetcher.session, DirectFirstSession)


if __name__ == "__main__":
    unittest.main()
