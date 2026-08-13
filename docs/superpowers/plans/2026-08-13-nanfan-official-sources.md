# 南繁官方固定监控源实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将海南“中国南繁”和三亚市农业农村局的两个栏目接入现有固定新闻采集主线。

**架构：** 三个来源都使用现有 `web_news` 抓取器。解析器新增三个 profile：南繁专栏接受列表中带日期的文章链接；三亚两个综合栏目使用同一组水稻、南繁和种业关键词预过滤。中英文配置使用相同 ID、入口和启用状态。

**技术栈：** Python 3.12、`html.parser`、PyYAML、`unittest`、TrendRadar `web_news` 解析器。

---

## 文件结构

- 修改 `trendradar/crawler/rss/web_news.py`：登记三个官网 profile 和共享的三亚南繁关键词。
- 修改 `config/config.yaml`：增加三个正式启用的固定源。
- 修改 `config/config.en.yaml`：与正式配置保持相同的固定源入口和 ID。
- 修改 `tests/test_official_rice_sources.py`：用接近真实官网结构的离线 HTML 验证解析、过滤和配置契约。

### 任务 1：定义三个来源的解析契约

**文件：**
- 修改：`tests/test_official_rice_sources.py`

- [ ] **步骤 1：编写失败的 profile 解析测试**

在 `OrdinaryOfficialSourceProfileTests` 中加入三个真实结构用例：

```python
def test_nanfan_official_profiles_parse_real_list_structures(self):
    cases = (
        (
            "hainan-nanfan-news",
            "https://agri.hainan.gov.cn/hnsnyt/zgnf/xwzx/xwjx/",
            "https://example.gov.cn/nanfan-update",
            "南繁育种创新取得新进展",
            "海南省农业农村厅中国南繁",
        ),
        (
            "sanya-agri-documents",
            "https://ny.sanya.gov.cn/nyjsite/bmwjxx/newxxgklist.shtml",
            "/nyjsite/bmwjxx/202608/abc.shtml",
            "三亚市南繁小院建设方案",
            "三亚市农业农村局",
        ),
        (
            "sanya-agri-news",
            "https://ny.sanya.gov.cn/nyjsite/gzdt/list2.shtml",
            "/nyjsite/gzdt/202608/abc.shtml",
            "三亚水稻育种工作取得新进展",
            "三亚市农业农村局",
        ),
    )
```

HTML 片段必须分别采用官网的 `li + 日期文本`、部门文件“标题/发布日期”和工作动态 `li > em + a` 结构，并断言每个结果的标题、URL、日期和作者。

- [ ] **步骤 2：编写失败的三亚关键词预过滤测试**

```python
def test_sanya_general_sections_keep_nanfan_terms_and_drop_unrelated_items(self):
    accepted_titles = (
        "国家南繁科研育种基地建设取得新进展",
        "三亚种质资源平台投入使用",
        "水稻新品种进入示范阶段",
    )
    unrelated_title = "三亚开展海洋牧场建后管护工作"
```

分别对 `sanya-agri-documents` 和 `sanya-agri-news` 断言三个相关标题可解析，无关标题触发“未找到新闻条目”。

- [ ] **步骤 3：编写失败的配置一致性测试**

```python
def test_nanfan_official_sources_are_enabled_in_both_configs(self):
    expected = {
        "hainan-nanfan-news": "https://agri.hainan.gov.cn/hnsnyt/zgnf/xwzx/xwjx/",
        "sanya-agri-documents": "https://ny.sanya.gov.cn/nyjsite/bmwjxx/newxxgklist.shtml",
        "sanya-agri-news": "https://ny.sanya.gov.cn/nyjsite/gzdt/list2.shtml",
    }
```

读取 `config.yaml` 和 `config.en.yaml`，断言每个 ID 只出现一次、URL 精确相等、`source_type == "web_news"` 且未禁用。

- [ ] **步骤 4：运行测试验证红灯**

运行：

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_official_rice_sources.OrdinaryOfficialSourceProfileTests -v
```

预期：新增用例因“未注册的网页新闻源”及配置缺少三个 ID 而失败；原有用例继续通过。

### 任务 2：实现 profile 和固定源配置

**文件：**
- 修改：`trendradar/crawler/rss/web_news.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 测试：`tests/test_official_rice_sources.py`

- [ ] **步骤 1：登记 profile 和共享关键词**

在 `web_news.py` 定义：

```python
_NANFAN_RICE_TERMS = (
    "水稻", "稻米", "稻谷", "稻作", "南繁",
    "种业", "育种", "制种", "种质", "品种",
)
```

并在 `_PROFILES` 增加：

```python
"hainan-nanfan-news": _WebNewsProfile(
    "海南省农业农村厅中国南繁",
    _patterns(r"^https?://"),
    require_date=True,
),
"sanya-agri-documents": _WebNewsProfile(
    "三亚市农业农村局",
    _patterns(r"^https://ny\.sanya\.gov\.cn/nyjsite/bmwjxx/20\d{4}/[0-9a-f]+\.shtml$"),
    require_date=True,
    required_terms=_NANFAN_RICE_TERMS,
),
"sanya-agri-news": _WebNewsProfile(
    "三亚市农业农村局",
    _patterns(r"^https://ny\.sanya\.gov\.cn/nyjsite/gzdt/20\d{4}/[0-9a-f]+\.shtml$"),
    require_date=True,
    required_terms=_NANFAN_RICE_TERMS,
),
```

南繁 profile 的宽 URL 规则只在官网列表容器同时提供可解析日期时生效；导航链接没有日期，仍会被 `require_date` 排除。

- [ ] **步骤 2：在中英文配置加入固定源**

在两份配置的官方时事来源区域加入：

```yaml
- id: "hainan-nanfan-news"
  name: "海南省农业农村厅中国南繁新闻"
  url: "https://agri.hainan.gov.cn/hnsnyt/zgnf/xwzx/xwjx/"
  source_type: "web_news"
  max_items: 30

- id: "sanya-agri-documents"
  name: "三亚市农业农村局部门文件"
  url: "https://ny.sanya.gov.cn/nyjsite/bmwjxx/newxxgklist.shtml"
  source_type: "web_news"
  max_items: 30

- id: "sanya-agri-news"
  name: "三亚市农业农村局工作动态"
  url: "https://ny.sanya.gov.cn/nyjsite/gzdt/list2.shtml"
  source_type: "web_news"
  max_items: 30
```

不增加 `enabled: false`，使三个来源默认启用。

- [ ] **步骤 3：运行聚焦测试验证绿灯**

运行：

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_official_rice_sources.OrdinaryOfficialSourceProfileTests -v
```

预期：全部通过，0 failures，0 errors。

- [ ] **步骤 4：运行官方来源与抓取器回归**

运行：

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_official_rice_sources \
  tests.test_direct_first_proxy -v
```

预期：全部通过；既有官方来源、浏览器请求头和代理回退行为不变。

- [ ] **步骤 5：执行静态检查**

运行：

```bash
git diff --check
```

预期：退出码 0，无输出。

- [ ] **步骤 6：提交实现**

```bash
git add \
  config/config.yaml \
  config/config.en.yaml \
  trendradar/crawler/rss/web_news.py \
  tests/test_official_rice_sources.py
git commit -m "feat(南繁): 接入三个官方固定监控源"
```
