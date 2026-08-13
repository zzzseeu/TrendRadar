# 国内外时事动态研判实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让周报 `current_events_trends` 在同一 JSON 字段内固定输出国内动态和国外动态两部分。

**架构：** 保持现有 AI 响应结构、证据校验和 PDF 渲染不变，只收紧 `config/ai_analysis_prompt.txt` 的生成协议。使用静态契约测试锁定标签顺序、地域归类、空态和禁止重复要求。

**技术栈：** 文本 Prompt、Python `unittest`、项目虚拟环境。

---

## 文件结构

- 修改：`tests/test_weekly_configuration.py` —— 增加国内外时事研判 Prompt 契约。
- 修改：`config/ai_analysis_prompt.txt` —— 定义 `current_events_trends` 的国内外双段输出规则。

### 任务 1：收紧时事动态研判 Prompt

**文件：**
- 修改：`tests/test_weekly_configuration.py`
- 修改：`config/ai_analysis_prompt.txt`

- [ ] **步骤 1：编写失败的 Prompt 契约测试**

在 `WeeklyConfigurationTests` 中新增：

```python
def test_weekly_current_events_prompt_splits_domestic_and_international(self):
    prompt = (ROOT / "config/ai_analysis_prompt.txt").read_text(
        encoding="utf-8"
    )

    domestic = prompt.index("【国内动态】")
    international = prompt.index("【国外动态】")
    self.assertLess(domestic, international)
    for token in (
        "中国大陆中央与地方政府",
        "国内科研机构、企业和产业动态",
        "其他国家、境外机构及国际组织",
        "国际组织按发布主体归入国外动态",
        "本期暂无相关动态",
        "同一新闻不得在两部分重复",
        "不得引用另一部分新闻补齐",
    ):
        with self.subTest(token=token):
            self.assertIn(token, prompt)
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True /mnt/d/project/trendradar/.venv/bin/python \
  -m unittest \
  tests.test_weekly_configuration.WeeklyConfigurationTests.test_weekly_current_events_prompt_splits_domestic_and_international \
  -v
```

预期：`FAIL`，首个缺失内容为 `【国内动态】`。

- [ ] **步骤 3：最小修改分析 Prompt**

将 `config/ai_analysis_prompt.txt` 中 `7. current_events_trends` 改为：

```text
7. current_events_trends：时事动态。
   - 仅使用 module_type=current_events 的水稻时事模块新闻，按以下固定顺序输出：
     【国内动态】概括中国大陆中央与地方政府、国内科研机构、企业和产业动态。
     【国外动态】概括其他国家、境外机构及国际组织发布的动态；国际组织按发布主体归入国外动态，即使内容涉及中国项目。
   - 每部分只能引用支持该部分结论的 [current_events:N] 证据，同一新闻不得在两部分重复。
   - 某部分没有可用新闻时，在该部分写“本期暂无相关动态”，不得引用另一部分新闻补齐，不得根据常识虚构内容。
   - 企业宣传、会议和调研只要包含可核验的水稻产业信息即可纳入，不得补写输入中没有的规模、效果或结论。
```

同步把 JSON 示例值改为：

```json
"current_events_trends": "【国内动态】…… [current_events:1]\\n【国外动态】…… [current_events:2]"
```

- [ ] **步骤 4：运行聚焦测试并确认绿灯**

运行：

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True /mnt/d/project/trendradar/.venv/bin/python \
  -m unittest tests.test_weekly_configuration tests.test_ai_analyzer_response -v
```

预期：全部 `OK`；既有 JSON 解析和周报证据校验不受影响。

- [ ] **步骤 5：检查差异并提交**

运行：

```bash
git diff --check
git add tests/test_weekly_configuration.py config/ai_analysis_prompt.txt
git commit -m "feat(周报): 拆分国内外时事动态研判"
```

预期：差异检查无输出；提交仅包含上述两个实现文件。
