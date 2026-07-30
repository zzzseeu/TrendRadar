# coding=utf-8
"""新闻抓取使用的直连优先 HTTP 会话。"""

from typing import Mapping, Optional

import requests


class DirectFirstSession:
    """新闻请求默认直连，网络失败或可重试状态码时使用显式代理重试。"""

    def __init__(
        self,
        headers: Optional[Mapping[str, str]] = None,
        use_proxy: bool = False,
        proxy_url: str = "",
    ) -> None:
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url.strip()

        self._direct = requests.Session()
        self._direct.trust_env = False

        self._proxy = requests.Session()
        self._proxy.trust_env = False
        if self.proxy_url:
            self._proxy.proxies.update(
                {"http": self.proxy_url, "https": self.proxy_url}
            )

        if headers:
            self._direct.headers.update(headers)
            self._proxy.headers.update(headers)

    @staticmethod
    def _should_retry_status(status_code: int) -> bool:
        return (
            status_code == 403
            or status_code in {408, 429}
            or 500 <= status_code <= 599
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        """执行 GET；显式代理模式保持代理优先，否则直连失败后代理重试一次。"""
        if self.use_proxy and self.proxy_url:
            return self._proxy.get(url, **kwargs)

        try:
            response = self._direct.get(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if not self.proxy_url:
                raise
            print(f"[网络] 直连失败 ({type(exc).__name__})，使用代理重试")
            return self._proxy.get(url, **kwargs)

        if self.proxy_url and self._should_retry_status(response.status_code):
            status_code = response.status_code
            response.close()
            print(f"[网络] 直连返回 HTTP {status_code}，使用代理重试")
            return self._proxy.get(url, **kwargs)

        return response

    def close(self) -> None:
        self._direct.close()
        self._proxy.close()
