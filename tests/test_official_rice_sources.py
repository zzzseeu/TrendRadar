import unittest

from trendradar.crawler.rss.web_news import (
    parse_official_document_html,
    parse_web_news_html,
)


def _page(article_url, title, *, date="2026-08-09", summary="水稻产业发展取得明确进展"):
    return f"""
    <html><body>
      <nav><a href="/">首页导航</a><a href="?page=2">下一页</a></nav>
      <article class="news-card">
        <h2><a href="{article_url}">{title}</a></h2>
        <time datetime="{date}">{date}</time>
        <p class="summary">{summary}</p>
      </article>
    </body></html>
    """


class OrdinaryOfficialSourceProfileTests(unittest.TestCase):
    CASES = (
        ("vietnam-ppd", "https://ppd.gov.vn/", "/van-ban-chinh-sach/lua-gao.html"),
        ("ndrc-rice", "https://www.ndrc.gov.cn/fzggw/jgsj/jgs/sjdt/", "/fzggw/jgsj/jgs/sjdt/202608/t20260809_123.html"),
        ("stats-grain", "https://www.stats.gov.cn/sj/zxfb/", "/sj/zxfb/202608/t20260809_123.html"),
        ("moa-seed-notices", "https://zys.moa.gov.cn/gsgg/", "/gsgg/202608/t20260809_123.htm"),
        ("heilongjiang-rice", "https://nynct.hlj.gov.cn/nynct/c115385/public_list.shtml", "/nynct/c115394/202608/c00_123.shtml"),
        ("hunan-rice", "https://agri.hunan.gov.cn/agri/xxgk/tzgg/", "/agri/xxgk/tzgg/202608/t20260809_123.html"),
        ("hubei-rice", "https://nyt.hubei.gov.cn/bmdt/yw/gfxx/index.shtml", "/bmdt/yw/gfxx/202608/t20260809_123.shtml"),
        ("jiangsu-rice", "https://nynct.jiangsu.gov.cn/col/col12433/index.html", "/art/2026/8/9/art_12433_123.html"),
        ("philrice-news", "https://www.philrice.gov.ph/news/", "/rice-seed-program-expands/"),
    )

    def test_profiles_extract_only_dated_official_articles(self):
        for feed_id, page_url, article_url in self.CASES:
            with self.subTest(feed_id=feed_id):
                items = parse_web_news_html(
                    _page(article_url, "水稻品种和产业项目取得新进展"),
                    feed_id,
                    page_url,
                )
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].published_at, "2026-08-09")
                self.assertIn("水稻", items[0].title)
                self.assertNotIn("page=2", items[0].url)

    def test_provincial_profiles_drop_unrelated_agriculture_articles(self):
        for feed_id, page_url, article_url in self.CASES[4:8]:
            with self.subTest(feed_id=feed_id):
                html = _page(
                    article_url,
                    "生猪屠宰管理工作通知",
                    summary="本通知只涉及畜牧和兽医管理",
                )
                with self.assertRaisesRegex(ValueError, "未找到新闻条目"):
                    parse_web_news_html(html, feed_id, page_url)

    def test_structure_mismatch_is_failure_not_successful_empty(self):
        with self.assertRaisesRegex(ValueError, "未找到新闻条目"):
            parse_web_news_html(
                "<html><nav><a href='/'>栏目导航</a></nav></html>",
                "ndrc-rice",
                "https://www.ndrc.gov.cn/fzggw/jgsj/jgs/sjdt/",
            )


class OfficialDocumentSourceTests(unittest.TestCase):
    def test_amis_and_maff_extract_dated_official_documents(self):
        cases = (
            (
                "amis-rice",
                "https://www.amis-outlook.org/market-monitor",
                "https://legacy.amis-outlook.org/fileadmin/user_upload/amis/docs/Market_monitor/AMIS_Market_Monitor_July_2026.pdf",
                "AMIS Market Monitor July 2026",
            ),
            (
                "japan-maff-rice",
                "https://www.maff.go.jp/j/seisan/keikaku/beikoku_sisin/",
                "https://www.maff.go.jp/j/seisan/keikaku/soukatu/attach/pdf/index-1.pdf",
                "米穀の需給及び価格の安定に関する基本指針",
            ),
        )
        for feed_id, page_url, document_url, title in cases:
            with self.subTest(feed_id=feed_id):
                html = _page(document_url, title, date="2026-08-05")
                items = parse_official_document_html(html, feed_id, page_url)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].published_at, "2026-08-05")
                self.assertTrue(items[0].url.endswith(".pdf"))

    def test_document_page_with_only_navigation_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "未找到官方文档"):
            parse_official_document_html(
                "<html><nav><a href='/'>Home</a></nav></html>",
                "amis-rice",
                "https://www.amis-outlook.org/market-monitor",
            )


if __name__ == "__main__":
    unittest.main()
