# Nature 与 Cell 正刊监控实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 Nature 与 Cell 正刊官方 RSS 加入固定学术来源，并确保来源证据分类为科研进展。

**架构：** 完全复用现有 RSS 抓取器。来源资格由 YAML 的 `content_category: scholarly` 提供，正文中明确出现期刊名时由 `source_evidence` 作为补充证据识别。

**技术栈：** YAML、Python `unittest`、现有 `feedparser` RSS 管线。

---

### 任务 1：增加正刊来源和分类契约

**文件：**
- 修改：`tests/test_weekly_source_evidence.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 修改：`trendradar/ai/source_evidence.py`

- [ ] **步骤 1：编写失败测试**

扩展学术来源集合，要求 `nature`、`cell` 均存在；同时测试普通机构文章正文出现 `Nature` 或 `Cell` 时归入科研进展。

- [ ] **步骤 2：验证红灯**

运行：`.venv/bin/python -m unittest tests.test_weekly_source_evidence.WeeklySourceEvidenceTests -v`

预期：配置集合或 Nature 正刊名称识别断言失败。

- [ ] **步骤 3：最小实现**

在两份 YAML 中加入：

```yaml
- id: "nature"
  name: "Nature"
  url: "https://www.nature.com/nature.rss"
  content_category: "scholarly"
  max_items: 30

- id: "cell"
  name: "Cell"
  url: "https://www.cell.com/cell/current.rss"
  content_category: "scholarly"
  max_items: 30
```

将 Nature 正刊加入明确期刊名称规则；Cell 已存在，不重复添加。

- [ ] **步骤 4：验证绿灯**

运行：`.venv/bin/python -m unittest tests.test_weekly_source_evidence.WeeklySourceEvidenceTests tests.test_weekly_configuration -v`

预期：全部通过。

- [ ] **步骤 5：静态检查**

运行：`git diff --check`

预期：无输出，退出码为 0。
