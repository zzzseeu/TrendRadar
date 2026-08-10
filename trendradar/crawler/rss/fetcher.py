# coding=utf-8
"""
RSS 抓取器

负责从配置的 RSS 源抓取数据并转换为标准格式
"""

import time
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Dict, Optional, Tuple

import requests

from trendradar.crawler.http import DirectFirstSession
from .parser import RSSParser
from trendradar.storage.base import RSSItem, RSSData
from trendradar.utils.time import get_configured_time, DEFAULT_TIMEZONE


@dataclass
class RSSFeedConfig:
    """RSS 源配置"""
    id: str                     # 源 ID
    name: str                   # 显示名称
    url: str                    # RSS URL
    max_items: int = 0          # 最大条目数（0=不限制）
    enabled: bool = True        # 是否启用
    source_type: str = "rss"    # 来源类型：rss | irri_news | web_news | corteva_news
    fetch_url: str = ""         # 可选抓取地址，对外链接仍使用 url


class RSSFetcher:
    """RSS 抓取器"""

    def __init__(
        self,
        feeds: List[RSSFeedConfig],
        request_interval: int = 2000,
        timeout: int = 15,
        use_proxy: bool = False,
        proxy_url: str = "",
        timezone: str = DEFAULT_TIMEZONE,
        get_time_func: Optional[Callable[[], datetime]] = None,
    ):
        """
        初始化抓取器

        Args:
            feeds: RSS 源配置列表
            request_interval: 请求间隔（毫秒）
            timeout: 请求超时（秒）
            use_proxy: 是否使用代理
            proxy_url: 代理 URL
            timezone: 时区配置（如 'Asia/Shanghai'）
        """
        self.feeds = [f for f in feeds if f.enabled]
        self.request_interval = request_interval
        self.timeout = timeout
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url
        self.timezone = timezone
        self.get_time = get_time_func or (
            lambda: get_configured_time(self.timezone)
        )

        self.parser = RSSParser()
        self.session = self._create_session()

    def _create_session(self) -> DirectFirstSession:
        """创建请求会话"""
        return DirectFirstSession(
            headers={
                "User-Agent": "TrendRadar/2.0 RSS Reader (https://github.com/trendradar)",
                "Accept": "application/feed+json, application/json, application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            use_proxy=self.use_proxy,
            proxy_url=self.proxy_url,
        )

    def fetch_feed(
        self,
        feed: RSSFeedConfig,
        run_at: Optional[datetime] = None,
    ) -> Tuple[List[RSSItem], Optional[str]]:
        """
        抓取单个 RSS 源

        Args:
            feed: RSS 源配置

        Returns:
            (条目列表, 错误信息) 元组
        """
        try:
            now = run_at or self.get_time()
            request_url = (feed.fetch_url or feed.url).format(year=now.year)
            response = self.session.get(request_url, timeout=self.timeout)
            response.raise_for_status()

            if feed.source_type == "irri_news":
                from .irri import parse_irri_news_html
                parsed_items = parse_irri_news_html(response.text)
            elif feed.source_type == "web_news":
                from .web_news import parse_web_news_html
                # 部分中文政府/科研站未声明正确编码，requests 会误判为 ISO-8859-1。
                response.encoding = response.apparent_encoding or response.encoding
                parsed_items = parse_web_news_html(response.text, feed.id, request_url)
            elif feed.source_type == "corteva_news":
                from .web_news import parse_corteva_news_json
                parsed_items = parse_corteva_news_json(response.text)
            else:
                parsed_items = self.parser.parse(response.text, feed.url)

            # 限制条目数量（0=不限制）
            if feed.max_items > 0:
                parsed_items = parsed_items[:feed.max_items]

            # IRRI 列表页会截断长标题，仅对被截断的条目读取详情页补全。
            if feed.source_type == "irri_news":
                from .irri import build_irri_translate_url, parse_irri_article_title_html
                enriched_count = 0
                for parsed in parsed_items:
                    if not parsed.title.endswith("..."):
                        continue
                    try:
                        detail_response = self.session.get(
                            build_irri_translate_url(parsed.url), timeout=self.timeout
                        )
                        detail_response.raise_for_status()
                        full_title = parse_irri_article_title_html(detail_response.text)
                        if full_title:
                            parsed.title = full_title
                            enriched_count += 1
                    except requests.RequestException as e:
                        print(f"[RSS] {feed.name}: 标题补全失败 ({parsed.url}): {e}")
                if enriched_count:
                    print(f"[RSS] {feed.name}: 补全 {enriched_count} 个长标题")

            # 转换为 RSSItem（使用配置的时区）
            crawl_time = now.strftime("%Y-%m-%d %H:%M:%S")
            items = []

            for parsed in parsed_items:
                item = RSSItem(
                    title=parsed.title,
                    feed_id=feed.id,
                    feed_name=feed.name,
                    url=parsed.url,
                    guid=parsed.guid or "",
                    published_at=parsed.published_at or "",
                    summary=parsed.summary or "",
                    author=parsed.author or "",
                    crawl_time=crawl_time,
                    first_time=crawl_time,
                    last_time=crawl_time,
                    count=1,
                )
                items.append(item)

            print(f"[RSS] {feed.name}: 获取 {len(items)} 条")
            return items, None

        except requests.Timeout:
            error = f"请求超时 ({self.timeout}s)"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except requests.RequestException as e:
            error = f"请求失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except ValueError as e:
            error = f"解析失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except Exception as e:
            error = f"未知错误: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

    def fetch_all(self) -> RSSData:
        """
        抓取所有 RSS 源

        Returns:
            RSSData 对象
        """
        all_items: Dict[str, List[RSSItem]] = {}
        id_to_name: Dict[str, str] = {}
        failed_ids: List[str] = []

        # 使用配置的时区
        now = self.get_time()
        crawl_time = now.strftime("%Y-%m-%d %H:%M:%S")
        crawl_date = now.strftime("%Y-%m-%d")

        print(f"[RSS] 开始抓取 {len(self.feeds)} 个 RSS 源...")

        for i, feed in enumerate(self.feeds):
            # 请求间隔（带随机波动）
            if i > 0:
                interval = self.request_interval / 1000
                jitter = random.uniform(-0.2, 0.2) * interval
                time.sleep(interval + jitter)

            items, error = self.fetch_feed(feed, run_at=now)

            id_to_name[feed.id] = feed.name

            if error:
                failed_ids.append(feed.id)
            else:
                all_items[feed.id] = items

        total_items = sum(len(items) for items in all_items.values())
        print(f"[RSS] 抓取完成: {len(all_items)} 个源成功, {len(failed_ids)} 个失败, 共 {total_items} 条")

        return RSSData(
            date=crawl_date,
            crawl_time=crawl_time,
            items=all_items,
            id_to_name=id_to_name,
            failed_ids=failed_ids,
        )

    @classmethod
    def from_config(cls, config: Dict) -> "RSSFetcher":
        """
        从配置字典创建抓取器

        Args:
            config: 配置字典，格式如下：
                {
                    "enabled": true,
                    "request_interval": 2000,
                    "feeds": [
                        {"id": "hacker-news", "name": "Hacker News", "url": "..."}
                    ]
                }

        Returns:
            RSSFetcher 实例
        """
        feeds = []
        for feed_config in config.get("feeds", []):
            feed = RSSFeedConfig(
                id=feed_config.get("id", ""),
                name=feed_config.get("name", ""),
                url=feed_config.get("url", ""),
                max_items=feed_config.get("max_items", 0),  # 0=不限制
                enabled=feed_config.get("enabled", True),
                source_type=feed_config.get("source_type", "rss"),
                fetch_url=feed_config.get("fetch_url", ""),
            )
            if feed.id and feed.url:
                feeds.append(feed)

        return cls(
            feeds=feeds,
            request_interval=config.get("request_interval", 2000),
            timeout=config.get("timeout", 15),
            use_proxy=config.get("use_proxy", False),
            proxy_url=config.get("proxy_url", ""),
            timezone=config.get("timezone", DEFAULT_TIMEZONE),
        )
