# coding=utf-8
"""
存储后端抽象基类和数据模型

定义统一的存储接口，所有存储后端都需要实现这些方法
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set


@dataclass
class NewsItem:
    """新闻条目数据模型（热榜数据）"""

    title: str                          # 新闻标题
    source_id: str                      # 来源平台ID（如 toutiao, baidu）
    source_name: str = ""               # 来源平台名称（运行时使用，数据库不存储）
    rank: int = 0                       # 排名
    url: str = ""                       # 链接 URL
    mobile_url: str = ""                # 移动端 URL
    crawl_time: str = ""                # 抓取时间（HH:MM 格式）

    # 统计信息（用于分析）
    ranks: List[int] = field(default_factory=list)  # 历史排名列表
    first_time: str = ""                # 首次出现时间
    last_time: str = ""                 # 最后出现时间
    count: int = 1                      # 出现次数
    rank_timeline: List[Dict[str, Any]] = field(default_factory=list)  # 完整排名时间线
                                        # 格式: [{"time": "09:30", "rank": 1}, {"time": "10:00", "rank": 2}, ...]
                                        # None 表示脱榜: [{"time": "11:00", "rank": None}]

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "title": self.title,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "rank": self.rank,
            "url": self.url,
            "mobile_url": self.mobile_url,
            "crawl_time": self.crawl_time,
            "ranks": self.ranks,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "count": self.count,
            "rank_timeline": self.rank_timeline,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NewsItem":
        """从字典创建"""
        return cls(
            title=data.get("title", ""),
            source_id=data.get("source_id", ""),
            source_name=data.get("source_name", ""),
            rank=data.get("rank", 0),
            url=data.get("url", ""),
            mobile_url=data.get("mobile_url", ""),
            crawl_time=data.get("crawl_time", ""),
            ranks=data.get("ranks", []),
            first_time=data.get("first_time", ""),
            last_time=data.get("last_time", ""),
            count=data.get("count", 1),
            rank_timeline=data.get("rank_timeline", []),
        )


@dataclass
class RSSItem:
    """RSS 条目数据模型"""

    title: str                          # 标题
    feed_id: str                        # RSS 源 ID（如 "hacker-news"）
    feed_name: str = ""                 # RSS 源名称（运行时使用）
    url: str = ""                       # 文章链接
    guid: str = ""                      # GUID/ID（RSS guid 或 Atom id）
    published_at: str = ""              # RSS 发布时间（ISO 格式）
    summary: str = ""                   # 摘要/描述
    author: str = ""                    # 作者
    crawl_time: str = ""                # 抓取时间（HH:MM 格式）

    # 统计信息
    first_time: str = ""                # 首次抓取时间
    last_time: str = ""                 # 最后抓取时间
    count: int = 1                      # 抓取次数

    # 新闻搜索聚合元数据（普通 RSS 保持默认值）
    source_count: int = 1               # 独立报道来源数
    pre_hot_score: float = 0.0           # AI 筛选前热点分
    search_topic: str = ""              # 搜索主题 ID
    search_providers: str = ""          # 搜索供应商（逗号分隔）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "title": self.title,
            "feed_id": self.feed_id,
            "feed_name": self.feed_name,
            "url": self.url,
            "guid": self.guid,
            "published_at": self.published_at,
            "summary": self.summary,
            "author": self.author,
            "crawl_time": self.crawl_time,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "count": self.count,
            "source_count": self.source_count,
            "pre_hot_score": self.pre_hot_score,
            "search_topic": self.search_topic,
            "search_providers": self.search_providers,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RSSItem":
        """从字典创建"""
        return cls(
            title=data.get("title", ""),
            feed_id=data.get("feed_id", ""),
            feed_name=data.get("feed_name", ""),
            url=data.get("url", ""),
            guid=data.get("guid", ""),
            published_at=data.get("published_at", ""),
            summary=data.get("summary", ""),
            author=data.get("author", ""),
            crawl_time=data.get("crawl_time", ""),
            first_time=data.get("first_time", ""),
            last_time=data.get("last_time", ""),
            count=data.get("count", 1),
            source_count=data.get("source_count", 1),
            pre_hot_score=data.get("pre_hot_score", 0.0),
            search_topic=data.get("search_topic", ""),
            search_providers=data.get("search_providers", ""),
        )


@dataclass
class RSSData:
    """
    RSS 数据集合

    结构:
    - date: 日期（YYYY-MM-DD）
    - crawl_time: 抓取时间（HH:MM）
    - items: 按 feed_id 分组的 RSS 条目
    - id_to_name: feed_id 到名称的映射
    - failed_ids: 失败的 feed_id 列表
    """

    date: str                                   # 日期
    crawl_time: str                             # 抓取时间
    items: Dict[str, List[RSSItem]]             # 按 feed_id 分组的条目
    id_to_name: Dict[str, str] = field(default_factory=dict)   # ID到名称映射
    failed_ids: List[str] = field(default_factory=list)        # 失败的ID

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        items_dict = {}
        for feed_id, rss_list in self.items.items():
            items_dict[feed_id] = [item.to_dict() for item in rss_list]

        return {
            "date": self.date,
            "crawl_time": self.crawl_time,
            "items": items_dict,
            "id_to_name": self.id_to_name,
            "failed_ids": self.failed_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RSSData":
        """从字典创建"""
        items = {}
        items_data = data.get("items", {})
        for feed_id, rss_list in items_data.items():
            items[feed_id] = [RSSItem.from_dict(item) for item in rss_list]

        return cls(
            date=data.get("date", ""),
            crawl_time=data.get("crawl_time", ""),
            items=items,
            id_to_name=data.get("id_to_name", {}),
            failed_ids=data.get("failed_ids", []),
        )

    def get_total_count(self) -> int:
        """获取条目总数"""
        return sum(len(rss_list) for rss_list in self.items.values())


@dataclass
class NewsData:
    """
    新闻数据集合

    结构:
    - date: 日期（YYYY-MM-DD）
    - crawl_time: 抓取时间（HH时MM分）
    - items: 按来源ID分组的新闻条目
    - id_to_name: 来源ID到名称的映射
    - failed_ids: 失败的来源ID列表
    """

    date: str                                   # 日期
    crawl_time: str                             # 抓取时间
    items: Dict[str, List[NewsItem]]            # 按来源分组的新闻
    id_to_name: Dict[str, str] = field(default_factory=dict)   # ID到名称映射
    failed_ids: List[str] = field(default_factory=list)        # 失败的ID

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        items_dict = {}
        for source_id, news_list in self.items.items():
            items_dict[source_id] = [item.to_dict() for item in news_list]

        return {
            "date": self.date,
            "crawl_time": self.crawl_time,
            "items": items_dict,
            "id_to_name": self.id_to_name,
            "failed_ids": self.failed_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NewsData":
        """从字典创建"""
        items = {}
        items_data = data.get("items", {})
        for source_id, news_list in items_data.items():
            items[source_id] = [NewsItem.from_dict(item) for item in news_list]

        return cls(
            date=data.get("date", ""),
            crawl_time=data.get("crawl_time", ""),
            items=items,
            id_to_name=data.get("id_to_name", {}),
            failed_ids=data.get("failed_ids", []),
        )

    def get_total_count(self) -> int:
        """获取新闻总数"""
        return sum(len(news_list) for news_list in self.items.values())

    def merge_with(self, other: "NewsData") -> "NewsData":
        """
        合并另一个 NewsData 到当前数据

        合并规则:
        - 相同 source_id + title 的新闻合并排名历史
        - 更新 last_time 和 count
        - 保留较早的 first_time
        """
        merged_items = {}

        # 复制当前数据
        for source_id, news_list in self.items.items():
            merged_items[source_id] = {item.title: item for item in news_list}

        # 合并其他数据
        for source_id, news_list in other.items.items():
            if source_id not in merged_items:
                merged_items[source_id] = {}

            for item in news_list:
                if item.title in merged_items[source_id]:
                    # 合并已存在的新闻
                    existing = merged_items[source_id][item.title]

                    # 合并排名
                    existing_ranks = set(existing.ranks) if existing.ranks else set()
                    new_ranks = set(item.ranks) if item.ranks else set()
                    merged_ranks = sorted(existing_ranks | new_ranks)
                    existing.ranks = merged_ranks

                    # 更新时间
                    if item.first_time and (not existing.first_time or item.first_time < existing.first_time):
                        existing.first_time = item.first_time
                    if item.last_time and (not existing.last_time or item.last_time > existing.last_time):
                        existing.last_time = item.last_time

                    # 更新计数
                    existing.count += 1

                    # 保留URL（如果原来没有）
                    if not existing.url and item.url:
                        existing.url = item.url
                    if not existing.mobile_url and item.mobile_url:
                        existing.mobile_url = item.mobile_url
                else:
                    # 添加新新闻
                    merged_items[source_id][item.title] = item

        # 转换回列表格式
        final_items = {}
        for source_id, items_dict in merged_items.items():
            final_items[source_id] = list(items_dict.values())

        # 合并 id_to_name
        merged_id_to_name = {**self.id_to_name, **other.id_to_name}

        # 合并 failed_ids（去重）
        merged_failed_ids = list(set(self.failed_ids + other.failed_ids))

        return NewsData(
            date=self.date or other.date,
            crawl_time=other.crawl_time,  # 使用较新的抓取时间
            items=final_items,
            id_to_name=merged_id_to_name,
            failed_ids=merged_failed_ids,
        )


class StorageBackend(ABC):
    """
    存储后端抽象基类

    所有存储后端都需要实现这些方法，以支持:
    - 保存新闻数据
    - 读取当天所有数据
    - 检测新增新闻
    - 生成报告文件（TXT/HTML）
    """

    @abstractmethod
    def save_news_data(self, data: NewsData) -> bool:
        """
        保存新闻数据

        Args:
            data: 新闻数据

        Returns:
            是否保存成功
        """
        pass

    @abstractmethod
    def get_today_all_data(self, date: Optional[str] = None) -> Optional[NewsData]:
        """
        获取指定日期的所有新闻数据

        Args:
            date: 日期字符串（YYYY-MM-DD），默认为今天

        Returns:
            合并后的新闻数据，如果没有数据返回 None
        """
        pass

    @abstractmethod
    def get_latest_crawl_data(self, date: Optional[str] = None) -> Optional[NewsData]:
        """
        获取最新一次抓取的数据

        Args:
            date: 日期字符串，默认为今天

        Returns:
            最新抓取的新闻数据
        """
        pass

    @abstractmethod
    def detect_new_titles(self, current_data: NewsData) -> Dict[str, Dict]:
        """
        检测新增的标题

        Args:
            current_data: 当前抓取的数据

        Returns:
            新增的标题数据，格式: {source_id: {title: title_data}}
        """
        pass

    @abstractmethod
    def save_txt_snapshot(self, data: NewsData) -> Optional[str]:
        """
        保存 TXT 快照（可选功能，本地环境可用）

        Args:
            data: 新闻数据

        Returns:
            保存的文件路径，如果不支持返回 None
        """
        pass

    @abstractmethod
    def save_html_report(self, html_content: str, filename: str) -> Optional[str]:
        """
        保存 HTML 报告

        Args:
            html_content: HTML 内容
            filename: 文件名

        Returns:
            保存的文件路径
        """
        pass

    @abstractmethod
    def is_first_crawl_today(self, date: Optional[str] = None) -> bool:
        """
        检查是否是当天第一次抓取

        Args:
            date: 日期字符串，默认为今天

        Returns:
            是否是第一次抓取
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """
        清理资源（如临时文件、数据库连接等）
        """
        pass

    @abstractmethod
    def cleanup_old_data(self, retention_days: int) -> int:
        """
        清理过期数据

        Args:
            retention_days: 保留天数（0 表示不清理）

        Returns:
            删除的日期目录数量
        """
        pass

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """
        存储后端名称
        """
        pass

    @property
    @abstractmethod
    def supports_txt(self) -> bool:
        """
        是否支持生成 TXT 快照
        """
        pass

    # === 时间段执行记录（调度系统）===

    def has_period_executed(self, date_str: str, period_key: str, action: str) -> bool:
        """
        检查指定时间段的某个 action 是否已执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否已执行
        """
        return False

    def record_period_execution(self, date_str: str, period_key: str, action: str) -> bool:
        """
        记录时间段的 action 执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否记录成功
        """
        return False

    def record_period_execution_strict(
        self, date_str: str, period_key: str, action: str
    ) -> bool:
        """严格记录时间段执行；不支持原子持久化的后端必须明确失败。"""
        raise NotImplementedError("存储后端不支持严格时间段执行记录")

    def get_latest_period_execution(
        self,
        period_key: str,
        action: str,
        through_date: str,
    ) -> Optional[str]:
        """返回截止日期内最近一次成功执行的本地时区时间。"""
        return None

    # === AI 智能筛选（默认实现，子类通过 mixin 覆盖） ===

    def begin_batch(self) -> None:
        """开启批量模式（远程后端延迟上传，本地后端无操作）"""
        pass

    def end_batch(self) -> None:
        """结束批量模式"""
        pass

    def end_batch_strict(self) -> None:
        """严格结束批量模式；任何持久化失败必须向上抛出。"""
        raise NotImplementedError("存储后端不支持严格批量持久化")

    def get_active_ai_filter_tags(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> List[Dict]:
        return []

    def get_ai_filter_tag_snapshot_strict(
        self,
        date: Optional[str] = None,
        interests_file: str = "ai_interests.txt",
    ) -> Dict:
        """严格读取同一事务视图中的 active 标签/hash/version。"""
        raise NotImplementedError("存储后端不支持严格 AI 标签快照")

    def replace_ai_filter_tags_strict(
        self,
        tags: List[Dict],
        version: int,
        prompt_hash: str,
        date: Optional[str] = None,
        interests_file: str = "ai_interests.txt",
    ) -> Dict:
        """事务性全量替换标签并返回提交后的严格快照。"""
        raise NotImplementedError("存储后端不支持严格 AI 标签替换")

    def get_latest_prompt_hash(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> Optional[str]:
        return None

    def get_latest_ai_filter_tag_version(self, date: Optional[str] = None) -> int:
        return 0

    def deprecate_all_ai_filter_tags(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def save_ai_filter_tags(self, tags: List[Dict], version: int, prompt_hash: str, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def save_ai_filter_results(self, results: List[Dict], date: Optional[str] = None) -> int:
        return 0

    def get_active_ai_filter_results(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> List[Dict]:
        return []

    def get_active_ai_filter_results_strict(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> List[Dict]:
        """严格读取 active 分类结果；读取错误不得伪装成空结果。"""
        raise NotImplementedError("存储后端不支持严格 AI 分类结果读取")

    def deprecate_specific_ai_filter_tags(self, tag_ids: List[int], date: Optional[str] = None) -> int:
        return 0

    def update_ai_filter_tags_hash(self, interests_file: str, new_hash: str, date: Optional[str] = None) -> int:
        return 0

    def update_ai_filter_tag_descriptions(self, tag_updates: List[Dict], date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def update_ai_filter_tag_priorities(self, tag_priorities: List[Dict], date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def save_analyzed_news(self, news_ids: List[str], source_type: str, interests_file: str, prompt_hash: str, matched_ids: Set[str], date: Optional[str] = None) -> int:
        return 0

    def get_analyzed_news_ids(self, source_type: str = "hotlist", date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> Set[str]:
        return set()

    def get_analyzed_news_ids_strict(self, source_type: str = "hotlist", date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> Set[str]:
        """严格读取已分析 ID；读取错误不得伪装成空集合。"""
        raise NotImplementedError("存储后端不支持严格 AI 已分析 ID 读取")

    def replace_ai_filter_batch_strict(
        self,
        results: List[Dict],
        succeeded_news_ids: List[int],
        succeeded_rss_ids: List[int],
        interests_file: str,
        prompt_hash: str,
        date: Optional[str] = None,
    ) -> Dict[str, int]:
        """事务性替换本轮分类结果及 matched/unmatched 状态。"""
        raise NotImplementedError("存储后端不支持严格 AI 批次替换")

    def clear_analyzed_news(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def clear_unmatched_analyzed_news(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        return 0

    def get_all_news_ids(self, date: Optional[str] = None) -> List[Dict]:
        return []

    def get_all_rss_ids(self, date: Optional[str] = None) -> List[Dict]:
        return []

    def get_all_rss_ids_strict(
        self, date: Optional[str] = None
    ) -> List[Dict]:
        """严格读取 RSS ID；未显式实现的第三方后端必须失败关闭。"""
        raise NotImplementedError("存储后端不支持严格 RSS ID 读取")

    def get_earliest_rss_discoveries_strict(
        self, candidate_identities: Set[tuple], through_date: str
    ) -> Dict[tuple, tuple[str, str]]:
        """批量读取候选 identity 在全部现存日库中的最早发现。"""
        raise NotImplementedError("存储后端不支持严格 RSS 历史发现查询")

    def get_rss_feed_statuses(
        self, date: Optional[str] = None
    ) -> Dict[str, str]:
        """返回指定日库中每个 RSS 源的最新抓取状态。

        默认实现仅从聚合数据推导，供未实现状态查询的存储后端保持兼容。
        """
        get_rss_data = getattr(self, "get_rss_data", None)
        if not callable(get_rss_data):
            return {}
        data = get_rss_data(date)
        if data is None:
            return {}
        statuses = {feed_id: "success" for feed_id in data.items}
        statuses.update({feed_id: "failed" for feed_id in data.failed_ids})
        return statuses

    def get_rss_data_strict(self, date: Optional[str] = None) -> Optional[RSSData]:
        """严格读取 RSS 日库；未显式实现的第三方后端必须失败关闭。"""
        raise NotImplementedError("存储后端不支持严格 RSS 数据读取")

    def get_rss_feed_statuses_strict(
        self, date: Optional[str] = None
    ) -> Dict[str, str]:
        """严格读取 RSS 来源状态；未显式实现的第三方后端失败关闭。"""
        raise NotImplementedError("存储后端不支持严格 RSS 状态读取")


def convert_crawl_results_to_news_data(
    results: Dict[str, Dict],
    id_to_name: Dict[str, str],
    failed_ids: List[str],
    crawl_time: str,
    crawl_date: str,
) -> NewsData:
    """
    将爬虫结果转换为 NewsData 格式

    Args:
        results: 爬虫返回的结果 {source_id: {title: {ranks: [], url: "", mobileUrl: ""}}}
        id_to_name: 来源ID到名称的映射
        failed_ids: 失败的来源ID
        crawl_time: 抓取时间（HH:MM）
        crawl_date: 抓取日期（YYYY-MM-DD）

    Returns:
        NewsData 对象
    """
    items = {}

    for source_id, titles_data in results.items():
        source_name = id_to_name.get(source_id, source_id)
        news_list = []

        for title, data in titles_data.items():
            ranks = data.get("ranks", [])
            url = data.get("url", "")
            mobile_url = data.get("mobileUrl", "")

            rank = ranks[0] if ranks else 99

            news_item = NewsItem(
                title=title,
                source_id=source_id,
                source_name=source_name,
                rank=rank,
                url=url,
                mobile_url=mobile_url,
                crawl_time=crawl_time,
                ranks=ranks,
                first_time=crawl_time,
                last_time=crawl_time,
                count=1,
            )
            news_list.append(news_item)

        items[source_id] = news_list

    return NewsData(
        date=crawl_date,
        crawl_time=crawl_time,
        items=items,
        id_to_name=id_to_name,
        failed_ids=failed_ids,
    )
