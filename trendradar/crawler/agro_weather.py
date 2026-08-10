# coding=utf-8
"""Client for the China Meteorological Administration weekly agro-weather report."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import re

import pytz

from trendradar.crawler.http import DirectFirstSession


OFFICIAL_AGRO_WEATHER_URL = "https://www.nmc.cn/publish/agro/ten-week/index.html"
_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
)
_REVIEW_RANGE_PATTERN = re.compile(
    r"本周\s*[（(]\s*"
    r"(?P<start>(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日)\s*"
    r"(?:至|[-—~～])\s*"
    r"(?P<end>(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日)\s*[）)]"
)
_SECTION_ONE = re.compile(r"^一、\s*本周天气特点及农业影响分析")
_SECTION_TWO = re.compile(r"^二、\s*未来天气对农业生产影响预估及建议")
_SECTION_HEADING = re.compile(r"^[一二三四五六七八九十]+、")
_OUTLOOK = re.compile(r"未来\s*10\s*天")
_RECOMMENDATION = re.compile(r"建议\s*[：:]")


class AgroWeatherFetchError(RuntimeError):
    """The official agro-weather page could not be fetched or safely parsed."""


class _VisibleTextParser(HTMLParser):
    """Extract human-visible, block-level text without an HTML dependency."""

    _BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "div", "td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self.blocks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if normalized:
            self._parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag in self._BLOCK_TAGS and self._parts:
            text = " ".join(self._parts).strip()
            if text:
                self.blocks.append(text)
            self._parts.clear()

    def close(self) -> None:
        super().close()
        if self._parts:
            text = " ".join(self._parts).strip()
            if text:
                self.blocks.append(text)
            self._parts.clear()


@dataclass(frozen=True)
class AgroWeatherReport:
    title: str
    report_date: date
    reviewed_start: date
    reviewed_end: date
    impact: str
    outlook: str
    recommendations: str
    source_url: str

    def belongs_to_run(self, run_at: datetime, timezone_name: str) -> bool:
        local_date = run_at.astimezone(pytz.timezone(timezone_name)).date()
        valid_report_dates = {local_date, local_date - timedelta(days=1)}
        valid_review_ends = {
            local_date - timedelta(days=1),
            local_date - timedelta(days=2),
        }
        return (
            self.report_date in valid_report_dates
            and self.reviewed_end in valid_review_ends
            and (self.reviewed_end - self.reviewed_start).days == 6
            and bool(self.impact.strip())
            and bool(self.outlook.strip())
            and bool(self.recommendations.strip())
        )


class AgroWeatherClient:
    """Fetch and validate only the current official national agro-weather report."""

    def __init__(
        self,
        session: DirectFirstSession | None = None,
        *,
        source_url: str = OFFICIAL_AGRO_WEATHER_URL,
        timeout: int = 30,
        timezone_name: str = "Asia/Shanghai",
        use_proxy: bool = False,
        proxy_url: str = "",
    ) -> None:
        if source_url != OFFICIAL_AGRO_WEATHER_URL:
            raise ValueError("农业气象周报只接受中央气象台官方栏目 URL")
        self.source_url = source_url
        self.timeout = max(1, int(timeout))
        self.timezone_name = timezone_name
        self.session = session or DirectFirstSession(
            headers={"User-Agent": "TrendRadar/2.0 Agro Weather"},
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )

    def fetch_latest(self, run_at: datetime) -> AgroWeatherReport | None:
        """Request and return this run's report, or ``None`` for a stale report."""
        try:
            response = self.session.get(self.source_url, timeout=self.timeout)
            response.raise_for_status()
        except Exception as exc:
            raise AgroWeatherFetchError(
                f"农业气象周报请求失败: {self.source_url} ({type(exc).__name__})"
            ) from exc

        try:
            report = self._parse(response.text)
        except AgroWeatherFetchError:
            raise
        except Exception as exc:
            raise AgroWeatherFetchError(
                f"农业气象周报解析失败: {self.source_url} ({type(exc).__name__})"
            ) from exc

        return report if report.belongs_to_run(run_at, self.timezone_name) else None

    def _parse(self, html: str) -> AgroWeatherReport:
        if not isinstance(html, str) or not html.strip():
            self._fail("页面正文为空")

        parser = _VisibleTextParser()
        parser.feed(html)
        parser.close()
        blocks = parser.blocks
        title_index = next(
            (index for index, block in enumerate(blocks) if "全国农业气象周报" in block),
            None,
        )
        if title_index is None:
            self._fail("缺少全国农业气象周报标题")

        first_section = next(
            (index for index, block in enumerate(blocks) if _SECTION_ONE.match(block)),
            None,
        )
        second_section = next(
            (index for index, block in enumerate(blocks) if _SECTION_TWO.match(block)),
            None,
        )
        if first_section is None or second_section is None or first_section >= second_section:
            self._fail("缺少农业影响或未来天气章节")

        title = blocks[title_index]
        metadata_blocks = blocks[title_index + 1 : first_section]
        signing_metadata = next(
            (block for block in metadata_blocks if "签发" in block), None
        )
        report_date = self._parse_full_date(
            _DATE_PATTERN.search(signing_metadata or ""), "签发日期"
        )
        review_text = " ".join(blocks[first_section + 1 : second_section])
        review_match = _REVIEW_RANGE_PATTERN.search(review_text)
        if review_match is None:
            self._fail("缺少本周回顾起止日期")
        reviewed_start = self._parse_partial_date(review_match.group("start"), report_date)
        reviewed_end = self._parse_partial_date(review_match.group("end"), report_date)
        reviewed_start, reviewed_end = self._normalize_review_years(
            reviewed_start, reviewed_end, report_date
        )

        impact = " ".join(blocks[first_section + 1 : second_section]).strip()
        next_section = next(
            (
                index
                for index in range(second_section + 1, len(blocks))
                if _SECTION_HEADING.match(blocks[index])
            ),
            len(blocks),
        )
        second_blocks = blocks[second_section + 1 : next_section]
        outlook_blocks = [
            block for block in second_blocks if not _RECOMMENDATION.search(block)
        ]
        outlook = " ".join(outlook_blocks).strip()
        recommendations = " ".join(
            block for block in second_blocks if _RECOMMENDATION.search(block)
        ).strip()
        impact_content = _REVIEW_RANGE_PATTERN.sub("", impact).strip(" ，。；;")
        if not impact_content:
            self._fail("缺少农业影响内容")
        if not outlook or not _OUTLOOK.search(outlook):
            self._fail("缺少未来10天展望")
        if not recommendations:
            self._fail("缺少农事建议")

        return AgroWeatherReport(
            title=title,
            report_date=report_date,
            reviewed_start=reviewed_start,
            reviewed_end=reviewed_end,
            impact=impact,
            outlook=outlook,
            recommendations=recommendations,
            source_url=self.source_url,
        )

    def _parse_full_date(self, match: re.Match[str] | None, label: str) -> date:
        if match is None:
            self._fail(f"缺少{label}")
        return date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )

    def _parse_partial_date(self, value: str, fallback: date) -> date:
        match = _DATE_PATTERN.search(value)
        if match:
            return self._parse_full_date(match, "本周回顾日期")
        partial = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
        if partial is None:
            self._fail("本周回顾日期格式错误")
        return date(fallback.year, int(partial.group(1)), int(partial.group(2)))

    @staticmethod
    def _normalize_review_years(
        reviewed_start: date, reviewed_end: date, report_date: date
    ) -> tuple[date, date]:
        """Correct omitted years in a review period around New Year."""
        if reviewed_end > report_date + timedelta(days=7):
            reviewed_start = reviewed_start.replace(year=reviewed_start.year - 1)
            reviewed_end = reviewed_end.replace(year=reviewed_end.year - 1)
        if reviewed_start > reviewed_end:
            reviewed_start = reviewed_start.replace(year=reviewed_start.year - 1)
        return reviewed_start, reviewed_end

    def _fail(self, detail: str) -> None:
        raise AgroWeatherFetchError(f"农业气象周报结构错误: {detail}: {self.source_url}")
