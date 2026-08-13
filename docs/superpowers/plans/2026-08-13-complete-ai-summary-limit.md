# AI 新闻摘要完整句与 450 字上限实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 删除摘要的固定字符裁剪，要求周报 AI 摘要不超过 450 字并以完整句子结束。

**架构：** 在 `trendradar.ai.filter` 中集中定义摘要上限和严格校验函数，严格分类与证据校审共用该函数；普通模式只移除旧裁剪以保持兼容。Prompt 负责要求模型生成完整句，代码只拒绝违规响应，不修改摘要正文。

**技术栈：** Python 3.12、`unittest`、项目 `.venv`、DeepSeek/OpenAI 兼容 JSON Object 响应。

---

## 文件结构

- 创建 `tests/test_ai_summary_length_contract.py`：锁定 450 字、完整句、校审和 Prompt 契约。
- 修改 `trendradar/ai/filter.py`：增加统一严格摘要校验，删除三处固定 `[:300]` 裁剪。
- 修改 `config/ai_filter/prompt.txt`：把摘要建议范围改为 180–450 字，并明确必须以完整句子结束。
- 按测试失败位置修改既有严格分类测试夹具：给需要作为合法摘要的简写文本补句末标点，不改变测试业务语义。

### 任务 1：建立摘要长度与完整句契约

**文件：**
- 创建：`tests/test_ai_summary_length_contract.py`
- 修改：`trendradar/ai/filter.py`
- 修改：`config/ai_filter/prompt.txt`

- [ ] **步骤 1：编写失败的测试**

新增测试，直接覆盖严格解析和证据校审：

```python
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from trendradar.ai.filter import AIFilter, _InvalidClassificationResponse


ROOT = Path(__file__).resolve().parents[1]


def strict_payload(summary: str) -> str:
    return json.dumps({"items": [{
        "id": 1,
        "include": True,
        "species_scope": "rice",
        "tag_id": 11,
        "score": 0.8,
        "importance_score": 0.8,
        "summary": summary,
    }]}, ensure_ascii=False)


class AISummaryLengthContractTests(unittest.TestCase):
    def setUp(self):
        self.filter = AIFilter.__new__(AIFilter)
        self.filter.debug = False
        self.titles = [{
            "id": 1,
            "title": "水稻产业动态",
            "content": "完整正文证据。",
            "content_level": "full_text",
            "module_type": "current_events",
        }]
        self.tags = [{"id": 11, "tag": "水稻产业时事动态"}]

    def parse(self, summary):
        return self.filter._parse_classify_response(
            strict_payload(summary), self.titles, self.tags, strict=True
        )

    def test_complete_summary_up_to_450_characters_is_preserved(self):
        summary = "甲" * 449 + "。"
        result = self.parse(summary)
        self.assertEqual(result[0]["ai_summary"], summary)

    def test_summary_over_450_characters_is_rejected_without_truncation(self):
        with self.assertRaises(_InvalidClassificationResponse):
            self.parse("甲" * 450 + "。")

    def test_incomplete_summary_is_rejected_without_truncation(self):
        with self.assertRaises(_InvalidClassificationResponse):
            self.parse("甲" * 300)

    def test_grounding_review_rejects_incomplete_summary(self):
        self.filter.client = Mock()
        self.filter.client.chat.return_value = json.dumps({
            "items": [{"id": 1, "summary": "乙" * 450}]
        }, ensure_ascii=False)
        results = [{"news_item_id": 1, "ai_summary": "原始完整摘要。"}]
        self.assertFalse(self.filter._review_item_summaries(self.titles, results))
        self.assertEqual(results[0]["ai_summary"], "原始完整摘要。")

    def test_prompt_requires_450_character_complete_sentences(self):
        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(encoding="utf-8")
        self.assertIn("180-450 字", prompt)
        self.assertIn("完整句子", prompt)
        self.assertNotIn("180-300 字", prompt)
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
.venv/bin/python -m unittest tests.test_ai_summary_length_contract -v
```

预期：450 字合法摘要被旧代码裁成 300 字；超长和残句没有被拒绝；Prompt 仍包含 `180-300 字`。

- [ ] **步骤 3：实现统一严格摘要校验**

在 `trendradar/ai/filter.py` 的分类协议常量附近增加：

```python
AI_SUMMARY_MAX_CHARS = 450
_SUMMARY_SENTENCE_END_RE = re.compile(r"[。！？.!?][\"'”’」』）)\]】]*$")


def _validate_ai_summary(value: Any) -> str:
    summary = " ".join(str(value or "").split())
    if not summary:
        raise _InvalidClassificationResponse("分类摘要为空")
    if len(summary) > AI_SUMMARY_MAX_CHARS:
        raise _InvalidClassificationResponse(
            f"分类摘要超过 {AI_SUMMARY_MAX_CHARS} 字"
        )
    if not _SUMMARY_SENTENCE_END_RE.search(summary):
        raise _InvalidClassificationResponse("分类摘要不是完整句子")
    return summary
```

严格解析时对 `include=true` 的摘要调用 `_validate_ai_summary`，保存其原值；`include=false` 仍只要求非空，因为它不会进入报告。证据校审返回的每条摘要也调用同一函数，任一失败时 `_review_item_summaries` 返回 `False`，且不得覆盖原始结果。

删除三处摘要切片：

```python
# strict result
"ai_summary": _validate_ai_summary(item["summary"]),

# review result
summary = _validate_ai_summary(item.get("summary", ""))

# ordinary result: 保持兼容，只规范空白，不裁剪
ai_summary = " ".join(str(item.get("summary", "")).split())
```

修改 `config/ai_filter/prompt.txt`：

```text
每条保留新闻必须给出一条中文 summary：有正文或摘要证据时建议 180-450 字，必须在 450 字内以完整句子结束；概括对象、背景、主要进展、影响和证据局限；只有标题时保持简短，不得为凑字数扩写，同样必须以完整句子结束
```

- [ ] **步骤 4：运行新增测试验证通过**

运行：

```bash
.venv/bin/python -m unittest tests.test_ai_summary_length_contract -v
```

预期：`Ran 5 tests`，`OK`。

### 任务 2：迁移严格分类夹具并验证兼容性

**文件：**
- 修改：`tests/test_ai_filter_module_contract.py`
- 修改：`tests/test_ai_filter_classification_resilience.py`
- 修改：测试输出指出的其他严格分类 fixture 文件，仅给合法摘要补句末标点。

- [ ] **步骤 1：运行受影响测试收集真实失败**

运行：

```bash
.venv/bin/python -m unittest \
  tests.test_ai_filter_module_contract \
  tests.test_ai_filter_classification_resilience \
  tests.test_daily_delivery_review3 \
  tests.test_daily_delivery_review4 -v
```

预期：旧合法 fixture 中没有句末标点的摘要因新契约失败；超长/残句新增契约保持通过。

- [ ] **步骤 2：机械迁移合法 fixture**

只对被测试视为合法的摘要补充句末标点，例如：

```python
"summary": "政策部署。"
"summary": "科研成果。"
"summary": "仅标题显示：木豆基因组完成。"
```

故意测试空摘要、错误类型、遗漏字段或非法响应的 fixture 不修改。

- [ ] **步骤 3：运行受影响测试验证通过**

运行步骤 1 的同一命令。

预期：全部测试 `OK`，没有 `FAIL` 或 `ERROR`。

- [ ] **步骤 4：验证静态契约与差异**

运行：

```bash
rg -n '\[:300\]|180-300 字' trendradar/ai/filter.py config/ai_filter/prompt.txt
git diff --check
```

预期：`rg` 无匹配（退出码 1）；`git diff --check` 退出码 0。

- [ ] **步骤 5：提交实现**

仅暂存本计划涉及的文件，保留工作区现有其他修改：

```bash
git add \
  config/ai_filter/prompt.txt \
  trendradar/ai/filter.py \
  tests/test_ai_summary_length_contract.py \
  tests/test_ai_filter_module_contract.py \
  tests/test_ai_filter_classification_resilience.py \
  tests/test_daily_delivery_review3.py \
  tests/test_daily_delivery_review4.py
git commit -m "fix(ai): 保证新闻摘要完整且不超过450字"
```

