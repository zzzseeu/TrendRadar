# 规则变更后重新分析未匹配新闻实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 筛选规则变化后，让严格 24 小时内、旧结论为“不匹配”的新闻重新进入 AI 分类，同时保留规则未变化时的分析去重。

**架构：** 将兴趣正文和分类提示词合并为同一个规则指纹；指纹变化进入现有标签更新流程。增量更新无论是否新增标签，都清除当前兴趣文件下的旧未匹配分析状态，随后继续使用现有 24 小时过滤、AI 分类、评分和推送流程。

**技术栈：** Python `unittest`、SQLite 存储接口、Docker Compose、容器内 uv 锁定环境

**规格：** `docs/superpowers/specs/2026-08-04-reclassify-stale-unmatched-news-design.md`

---

## 文件结构

- 创建：`tests/test_ai_filter_rule_invalidation.py`——验证分类提示词参与规则指纹，并验证无新增标签的增量更新也会失效旧未匹配记录。
- 修改：`trendradar/ai/filter.py`——把分类 system/user 提示词纳入规则指纹。
- 修改：`trendradar/ai/filter_pipeline.py`——规则指纹变化后的增量更新始终清除旧未匹配分析状态。

### 任务 1：用失败测试复现旧分析状态未失效

**文件：**

- 创建：`tests/test_ai_filter_rule_invalidation.py`
- 读取：`trendradar/ai/filter.py`
- 读取：`trendradar/ai/filter_pipeline.py`

- [ ] **步骤 1：编写规则指纹测试**

```python
import unittest

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline


def _filter_with_prompt(system: str, user: str) -> AIFilter:
    ai_filter = AIFilter.__new__(AIFilter)
    ai_filter.classify_system = system
    ai_filter.classify_user = user
    return ai_filter


class AIFilterRuleFingerprintTests(unittest.TestCase):
    def test_classification_prompt_change_changes_fingerprint(self):
        interests = "1. 水稻育种\n2. 其他作物育种"
        old_filter = _filter_with_prompt("旧分类规则", "{news_list}")
        new_filter = _filter_with_prompt("新分类规则", "{news_list}")

        self.assertNotEqual(
            old_filter.compute_interests_hash(interests),
            new_filter.compute_interests_hash(interests),
        )

    def test_interest_comments_do_not_change_fingerprint(self):
        ai_filter = _filter_with_prompt("分类规则", "{news_list}")

        self.assertEqual(
            ai_filter.compute_interests_hash("# Version: 1\n1. 水稻育种"),
            ai_filter.compute_interests_hash("# Version: 2\n1. 水稻育种"),
        )
```

- [ ] **步骤 2：编写增量失效测试**

在同一文件中加入：

```python
class _IncrementalStorageStub:
    def __init__(self):
        self.cleared_files = []

    def update_ai_filter_tag_descriptions(self, tags, interests_file):
        return len(tags)

    def update_ai_filter_tag_priorities(self, tags, interests_file):
        return len(tags)

    def update_ai_filter_tags_hash(self, interests_file, current_hash):
        return 1

    def clear_unmatched_analyzed_news(self, interests_file):
        self.cleared_files.append(interests_file)
        return 6


class IncrementalRuleInvalidationTests(unittest.TestCase):
    def test_incremental_update_without_new_tags_clears_unmatched_news(self):
        storage = _IncrementalStorageStub()
        pipeline = AIFilterPipeline(
            {"RSS": {"ENABLED": False}, "AI_FILTER": {}},
            storage,
            lambda: None,
        )

        pipeline._apply_incremental_update(
            old_tags=[{"id": 1, "tag": "育种"}],
            keep_tags=[{"tag": "育种", "description": "育种新闻"}],
            add_tags=[],
            remove_tags=[],
            change_ratio=0.0,
            threshold=0.6,
            new_version=2,
            current_hash="ai_interests.txt:new",
            effective_interests_file="ai_interests.txt",
        )

        self.assertEqual(storage.cleared_files, ["ai_interests.txt"])
```

- [ ] **步骤 3：运行目标测试并确认失败**

从主项目目录运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/.worktrees/reclassify-stale-unmatched/trendradar:/app/trendradar:ro \
  -v /mnt/d/project/trendradar/.worktrees/reclassify-stale-unmatched/tests:/app/tests:ro \
  trendradar -m unittest tests.test_ai_filter_rule_invalidation -v
```

预期：至少 2 个测试失败；旧实现的规则指纹不受分类提示词影响，且 `add_tags=[]` 时不会调用 `clear_unmatched_analyzed_news()`。

### 任务 2：实现规则指纹和旧未匹配记录失效

**文件：**

- 修改：`trendradar/ai/filter.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 测试：`tests/test_ai_filter_rule_invalidation.py`

- [ ] **步骤 1：把分类提示词纳入规则指纹**

将 `compute_interests_hash()` 的指纹输入改为：

```python
    def compute_interests_hash(
        self,
        interests_content: str,
        filename: str = "ai_interests.txt",
    ) -> str:
        """计算筛选规则指纹，格式为 filename:md5。"""
        interest_lines = []
        for line in interests_content.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                interest_lines.append(line)

        fingerprint_content = "\n".join(
            [
                "[interests]",
                "\n".join(interest_lines),
                "[classify_system]",
                self.classify_system.strip(),
                "[classify_user]",
                self.classify_user.strip(),
            ]
        )
        content_hash = hashlib.md5(
            fingerprint_content.encode("utf-8")
        ).hexdigest()
        return f"{filename}:{content_hash}"
```

- [ ] **步骤 2：增量更新始终清除旧未匹配状态**

将 `_apply_incremental_update()` 末尾的 `if add_tags:` 条件移除，保留一次清除和日志：

```python
        cleared = self.storage.clear_unmatched_analyzed_news(
            interests_file=effective_interests_file
        )
        if cleared > 0:
            print(
                f"[AI筛选]   清除 {cleared} 条旧规则下的不匹配记录，"
                "将在新规则下重新分析"
            )
```

该方法只在 `stored_hash != current_hash` 时被调用，因此规则未变化的普通重复运行不会清除分析状态。

- [ ] **步骤 3：运行目标测试并确认通过**

运行任务 1 步骤 3 的命令。

预期：3 个测试全部 `OK`。

- [ ] **步骤 4：运行完整测试集**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/.worktrees/reclassify-stale-unmatched/trendradar:/app/trendradar:ro \
  -v /mnt/d/project/trendradar/.worktrees/reclassify-stale-unmatched/config:/app/config:ro \
  -v /mnt/d/project/trendradar/.worktrees/reclassify-stale-unmatched/tests:/app/tests:ro \
  trendradar -m unittest discover -s tests -v
```

预期：全部测试通过，无 `FAILED` 或 `ERROR`。

- [ ] **步骤 5：检查并提交**

```bash
git diff --check -- trendradar/ai/filter.py trendradar/ai/filter_pipeline.py tests/test_ai_filter_rule_invalidation.py
git add trendradar/ai/filter.py trendradar/ai/filter_pipeline.py tests/test_ai_filter_rule_invalidation.py
git commit -m "fix(筛选): 规则变更后重析未匹配新闻"
```

预期：提交只包含上述 3 个文件。

### 任务 3：合并、重建并补跑

**文件：**

- 保持：`docker/.env`
- 验证：`output/news/2026-08-04.db`
- 验证：`output/rss/2026-08-04.db`

- [ ] **步骤 1：合并到主分支并再次运行完整测试**

使用快进合并，不包含主工作区现有的 `index.html`、历史文档或 `output/` 变更。合并后用主分支的 `trendradar/`、`config/` 和 `tests/` 挂载重新运行完整 `unittest`。

- [ ] **步骤 2：重建并重启正式服务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  up -d --build --force-recreate trendradar
```

预期：容器为 `Up`，日志仍显示 `0 9 * * *`。

- [ ] **步骤 3：立即补跑一次任务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  exec -T trendradar python -m trendradar
```

预期日志：

- 规则指纹发生变化；
- 清除旧规则下的不匹配记录；
- 仍在 24 小时内的候选重新发送给 AI 分类；
- 超过 24 小时的候选继续由新鲜度过滤；
- 是否推送由 AI 结果、`min_score = 0.7` 和热点最多 5 条共同决定。

- [ ] **步骤 4：验证服务和工作区**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml ps trendradar
git status --short
git log -3 --oneline
```

预期：正式容器保持 `Up`；主工作区仅保留用户原有未提交内容和本次生成的运行输出。

