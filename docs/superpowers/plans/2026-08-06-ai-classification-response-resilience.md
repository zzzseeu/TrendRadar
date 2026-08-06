# AI 分类响应容错实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 AI 分类稳定区分合法无匹配和无效响应，避免异常批次被永久记录为不匹配。

**架构：** 在 `AIFilter` 内为分类 JSON 增加明确的结构错误类型，首次分类和唯一一次修复请求均使用温度 0。流水线继续以 `None` 表示批次失败，因此现有成功 ID 收集逻辑会自然保留失败新闻供下次重试。

**技术栈：** Python 3.12、`unittest`、`unittest.mock`、LiteLLM、SQLite、Docker Compose。

---

## 文件结构

- 创建：`tests/test_ai_filter_classification_resilience.py`，覆盖分类响应三态语义、低温修复重试和流水线失败状态。
- 修改：`trendradar/ai/filter.py`，实现分类 JSON 严格结构校验、一次修复重试和温度 0。
- 不修改：`trendradar/ai/filter_pipeline.py`，其 `None` 失败分支已经不会记录已分析状态，仅用回归测试锁定行为。

### 任务 1：锁定分类响应三态语义

**文件：**
- 创建：`tests/test_ai_filter_classification_resilience.py`
- 修改：`trendradar/ai/filter.py:17-25, 360-545`

- [ ] **步骤 1：编写失败测试**

在新测试文件中创建最小 `AIFilter`，配置固定提示词、单条新闻和单个标签。覆盖：

```python
class ClassificationResponseResilienceTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.ai_filter.classify_system = "只返回 JSON"
        self.ai_filter.classify_user = "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        self.ai_filter.summary_grounding_review_enabled = False
        self.ai_filter.client = Mock()
        self.titles = [{
            "id": 1,
            "title": "木豆端粒到端粒基因组完成",
            "content": "木豆端粒到端粒基因组完成",
            "content_level": "title_only",
        }]
        self.tags = [{"id": 18, "tag": "其他作物育种", "description": "其他作物育种进展"}]

    def test_valid_match_uses_zero_temperature(self):
        self.ai_filter.client.chat.return_value = (
            '[{"id":1,"tag_id":18,"score":0.82,'
            '"importance_score":0.78,"summary":"仅标题显示：木豆基因组完成"}]'
        )
        result = self.ai_filter.classify_batch(self.titles, self.tags, "育种")
        self.assertEqual(result[0]["news_item_id"], 1)
        self.assertEqual(self.ai_filter.client.chat.call_args.kwargs["temperature"], 0)

    def test_valid_empty_array_is_success_without_retry(self):
        self.ai_filter.client.chat.return_value = "[]"
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        self.assertEqual(self.ai_filter.client.chat.call_count, 1)

    def test_empty_response_retries_and_recovers(self):
        self.ai_filter.client.chat.side_effect = ["", "[]"]
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)

    def test_malformed_json_retries_and_recovers(self):
        self.ai_filter.client.chat.side_effect = ["[{", "[]"]
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        retry_messages = self.ai_filter.client.chat.call_args_list[1].args[0]
        self.assertEqual(retry_messages[-2], {"role": "assistant", "content": "[{"})
        self.assertIn("严格 JSON 数组", retry_messages[-1]["content"])

    def test_two_invalid_responses_return_failure(self):
        self.ai_filter.client.chat.side_effect = ["", "not-json"]
        self.assertIsNone(self.ai_filter.classify_batch(self.titles, self.tags, "育种"))
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
docker run --rm --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/trendradar:/app/trendradar:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/tests:/app/tests:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/config:/app/config:ro \
  docker-trendradar -m unittest discover -s /app/tests -p 'test_ai_filter_classification_resilience.py'
```

预期：低温参数、修复重试和失败语义测试失败；合法 `[]` 测试保持通过。

- [ ] **步骤 3：实现最小分类容错**

在 `trendradar/ai/filter.py` 增加：

```python
CLASSIFY_JSON_REPAIR_PROMPT = (
    "上一个响应不是可解析的分类 JSON。请修正语法并仅返回严格 JSON 数组。"
    "数组元素必须包含 id、tag_id、score、importance_score 和 summary；"
    "如果没有匹配新闻，请返回 []。不要添加 Markdown 或解释。"
)


class _InvalidClassificationResponse(ValueError):
    """分类响应无法可靠解释为合法 JSON 数组。"""
```

将 `_parse_classify_response()` 的空响应、JSON 错误和非数组顶层改为抛出 `_InvalidClassificationResponse`。字段合法的 `[]` 仍返回空列表。

将 `classify_batch()` 调整为：

```python
response = self.client.chat(messages, temperature=0)
try:
    results = self._parse_classify_response(response, titles, tags)
except _InvalidClassificationResponse as error:
    print(f"[AI筛选] 分类 JSON 解析失败，低温重试一次: {error}")
    repair_messages = list(messages)
    if response:
        repair_messages.append({"role": "assistant", "content": response})
    repair_messages.append({"role": "user", "content": CLASSIFY_JSON_REPAIR_PROMPT})
    try:
        repaired = self.client.chat(repair_messages, temperature=0)
        results = self._parse_classify_response(repaired, titles, tags)
    except _InvalidClassificationResponse as repair_error:
        print(f"[AI筛选] 分类响应修复失败，将在下次运行重试: {repair_error}")
        return None
```

保留现有网络/API 异常捕获和摘要校审逻辑。

- [ ] **步骤 4：运行目标测试并确认绿灯**

重复步骤 2 的命令。

预期：5 项测试全部通过。

- [ ] **步骤 5：提交任务 1**

```bash
git add tests/test_ai_filter_classification_resilience.py trendradar/ai/filter.py
git commit -m "fix(AI筛选): 容错分类JSON异常响应"
```

### 任务 2：锁定失败批次不落已分析状态

**文件：**
- 修改：`tests/test_ai_filter_classification_resilience.py`
- 验证：`trendradar/ai/filter_pipeline.py:371-476`

- [ ] **步骤 1：添加流水线回归测试**

```python
class ClassificationPipelineRetryTests(unittest.TestCase):
    def test_failed_rss_batch_does_not_return_succeeded_ids(self):
        pipeline = AIFilterPipeline.__new__(AIFilterPipeline)
        pipeline._content_config = {"ENABLED": False}
        pipeline._rss_use_proxy = False
        pipeline._rss_proxy_url = ""
        pipeline._enrich_pending_items = Mock(side_effect=lambda items, _label: items)
        ai_filter = Mock()
        ai_filter.classify_batch.return_value = None
        pending_rss = [{
            "id": 6,
            "title": "木豆基因组突破",
            "summary": "",
            "content": "木豆基因组突破",
            "content_level": "title_only",
        }]

        results, succeeded_news, succeeded_rss = pipeline._classify_batches(
            ai_filter,
            [],
            pending_rss,
            [{"id": 18, "tag": "其他作物育种"}],
            "育种",
            {"BATCH_SIZE": 10, "BATCH_INTERVAL": 0},
        )

        self.assertEqual(results, [])
        self.assertEqual(succeeded_news, [])
        self.assertEqual(succeeded_rss, [])
```

- [ ] **步骤 2：运行目标测试**

运行任务 1 步骤 2 的命令。

预期：6 项测试全部通过，证明现有流水线无需代码修改。

- [ ] **步骤 3：提交任务 2**

```bash
git add tests/test_ai_filter_classification_resilience.py
git commit -m "test(AI筛选): 验证失败批次保留待重试"
```

### 任务 3：完整验证、集成与部署

**文件：**
- 验证：`trendradar/ai/filter.py`
- 验证：`tests/test_ai_filter_classification_resilience.py`
- 数据：`output/news/2026-08-06.db`

- [ ] **步骤 1：运行完整测试**

运行：

```bash
docker run --rm --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/trendradar:/app/trendradar:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/tests:/app/tests:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience/config:/app/config:ro \
  docker-trendradar -m unittest discover -s /app/tests
```

预期：全部测试通过。

- [ ] **步骤 2：执行差异检查与代码审查**

```bash
git diff --check HEAD~2..HEAD
git status --short
```

预期：无空白错误，worktree 干净。代码审查不得存在 Critical 或 Important 问题。

- [ ] **步骤 3：快进合并到 `main`**

```bash
git -C /mnt/d/project/trendradar merge --ff-only agent/ai-classification-response-resilience
```

预期：`main` 快进到修复提交，用户原有未提交文件保持不变。

- [ ] **步骤 4：重建并检查服务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
docker compose --env-file docker/.env -f docker/docker-compose.yml logs --tail=50 trendradar
```

预期：容器为 `Up`，定时表达式保持 `0 9 * * *`。

- [ ] **步骤 5：精确备份并清除错误状态**

先查询并确认 6 条目标记录：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  .venv/bin/python -c "import sqlite3; c=sqlite3.connect('output/news/2026-08-06.db'); print(c.execute(\"select news_item_id,source_type,matched from ai_filter_analyzed_news where source_type='rss' and matched=0 order by news_item_id\").fetchall())"
```

将数据库备份到 `/tmp/trendradar-2026-08-06-news-before-classification-reset.db`，然后仅删除查询到的 RSS 未匹配状态：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  .venv/bin/python -c "import shutil,sqlite3; p='output/news/2026-08-06.db'; shutil.copy2(p,'/tmp/trendradar-2026-08-06-news-before-classification-reset.db'); c=sqlite3.connect(p); c.execute(\"delete from ai_filter_analyzed_news where source_type='rss' and matched=0 and news_item_id in (1,2,3,4,5,6)\"); c.commit(); print('deleted=',c.total_changes)"
```

预期：删除数量严格等于 6；其他数据不变。

- [ ] **步骤 6：立即实跑并核验**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar python -m trendradar
```

预期：分类请求不再因无效 JSON 把整批记为不匹配；若模型成功响应，至少重新分析今天的 6 条新闻并正常推送匹配项；若两次响应均无效，日志明确说明保留待重试，数据库中不产生新的错误未匹配状态。

- [ ] **步骤 7：清理临时 worktree**

确认主分支、服务和实跑结果后，移除 `/mnt/d/project/trendradar/.worktrees/ai-classification-response-resilience`，执行 `git worktree prune`，并删除已合并的临时分支。
