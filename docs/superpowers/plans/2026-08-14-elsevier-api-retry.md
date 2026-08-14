# Elsevier API 三次重试实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 对 Elsevier API 的瞬时失败最多尝试三次，三次均失败后才访问 ScienceDirect 网页。

**架构：** 保持 `ElsevierFullTextClient.fetch()` 的单次请求职责，在 `ArticleContentFetcher` 中增加重试协调。协调层识别可重试状态、执行 0.5/1 秒退避、成功即返回正文；不可重试结果或重试耗尽后复用现有网页、摘要、标题降级链。

**技术栈：** Python、requests、unittest、MagicMock。

---

## 文件结构

- 修改：`trendradar/crawler/article_content.py` —— 实现 Elsevier API 重试协调和安全日志。
- 修改：`tests/test_elsevier_full_text.py` —— 固定成功、耗尽及不可重试三种行为。

### 任务 1：Elsevier API 重试协调

**文件：**
- 修改：`tests/test_elsevier_full_text.py`
- 修改：`trendradar/crawler/article_content.py`

- [ ] **步骤 1：编写失败测试**

在 `ArticleContentElsevierIntegrationTests` 增加三个测试：

```python
@patch("trendradar.crawler.article_content.time.sleep")
def test_transient_api_failures_retry_until_third_attempt_succeeds(self, sleep):
    api_client = MagicMock()
    api_client.fetch.side_effect = [
        ElsevierFetchResult("", "request_failed"),
        ElsevierFetchResult("", "http_503"),
        ElsevierFetchResult("A" * 800, "full_text"),
    ]
    fetcher = ArticleContentFetcher(min_body_chars=300, elsevier_client=api_client)
    fetcher.session = MagicMock()
    fetcher._is_public_http_url = MagicMock(return_value=True)

    result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

    self.assertEqual(result.fetch_status, "elsevier_full_text")
    self.assertEqual(api_client.fetch.call_count, 3)
    self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])
    fetcher.session.get.assert_not_called()
```

```python
@patch("trendradar.crawler.article_content.time.sleep")
def test_three_transient_api_failures_then_fall_back_to_html(self, sleep):
    api_client = MagicMock()
    api_client.fetch.side_effect = [
        ElsevierFetchResult("", "timeout"),
        ElsevierFetchResult("", "http_429"),
        ElsevierFetchResult("", "http_500"),
    ]
    fetcher = ArticleContentFetcher(elsevier_client=api_client)
    fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
    fetcher._is_public_http_url = MagicMock(return_value=True)

    result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

    self.assertEqual(result.fetch_status, "full_text")
    self.assertEqual(api_client.fetch.call_count, 3)
    self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])
    fetcher.session.get.assert_called_once()
```

```python
@patch("trendradar.crawler.article_content.time.sleep")
def test_non_retryable_api_status_falls_back_without_retry(self, sleep):
    api_client = MagicMock()
    api_client.fetch.return_value = ElsevierFetchResult("", "http_403")
    fetcher = ArticleContentFetcher(elsevier_client=api_client)
    fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
    fetcher._is_public_http_url = MagicMock(return_value=True)

    result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

    self.assertEqual(result.fetch_status, "full_text")
    api_client.fetch.assert_called_once_with(SCIENCEDIRECT_URL)
    sleep.assert_not_called()
```

- [ ] **步骤 2：运行三项测试验证红灯**

运行：

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True /mnt/d/project/trendradar/.venv/bin/python \
  -m unittest \
  tests.test_elsevier_full_text.ArticleContentElsevierIntegrationTests.test_transient_api_failures_retry_until_third_attempt_succeeds \
  tests.test_elsevier_full_text.ArticleContentElsevierIntegrationTests.test_three_transient_api_failures_then_fall_back_to_html \
  tests.test_elsevier_full_text.ArticleContentElsevierIntegrationTests.test_non_retryable_api_status_falls_back_without_retry \
  -v
```

预期：前两项因当前只请求一次而失败；第三项为既有行为基线并通过。

- [ ] **步骤 3：实现最小重试协调**

在 `article_content.py` 导入 `time`，增加常量和辅助方法：

```python
_ELSEVIER_MAX_ATTEMPTS = 3
_ELSEVIER_RETRY_DELAYS = (0.5, 1.0)


def _is_retryable_elsevier_status(status: str) -> bool:
    if status in {"timeout", "request_failed", "http_429"}:
        return True
    if status.startswith("http_"):
        try:
            return 500 <= int(status.removeprefix("http_")) <= 599
        except ValueError:
            return False
    return False
```

在 `ArticleContentFetcher` 中用 `_fetch_elsevier_with_retry(url)` 替换单次 `fetch()`；每次可重试失败打印 PII、次数和状态，前两次按常量调用 `time.sleep()`。合法全文立即返回；不可重试结果或第三次失败返回最后结果，让现有 HTML 降级继续执行。`requests.RequestException` 视为 `request_failed`，其他异常直接退出 API 重试并进入网页链。

- [ ] **步骤 4：运行完整 Elsevier 聚焦回归**

运行：

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True /mnt/d/project/trendradar/.venv/bin/python \
  -m unittest tests.test_elsevier_full_text tests.test_direct_first_proxy -v
```

预期：全部 `OK`；现有 API、网页、摘要和标题降级契约不变。

- [ ] **步骤 5：检查并提交**

```bash
git diff --check
git add trendradar/crawler/article_content.py tests/test_elsevier_full_text.py
git commit -m "fix(Elsevier): 为全文 API 增加三次重试"
```

预期：提交只包含正文协调层和对应测试。
