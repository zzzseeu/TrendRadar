# 扩大作物育种新闻准入范围实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将「水稻优先」从跨作物新闻的准入门槛改为排序偏好，让具有实质育种价值的其他作物新闻也能进入推送候选。

**架构：** 保持现有抓取、24 小时过滤、AI 分类、去重和热点排序流程不变。通过 `ai_interests.txt` 定义业务范围，通过分类提示词定义证据、准入和评分规则，并用文本规则回归测试锁定这些约束。

**技术栈：** Python `unittest`、文本提示词、Docker Compose、容器内 uv 锁定环境

**规格：** `docs/superpowers/specs/2026-08-04-broaden-crop-breeding-filter-design.md`

---

## 文件结构

- 修改：`config/ai_interests.txt`——定义水稻优先、跨作物准入范围及低质量排除标准，同时将版本提升到 `1.2.0`。
- 修改：`config/ai_filter/prompt.txt`——规定其他作物按实质育种价值准入、仅标题证据的评分区间及水稻排序优先级。
- 创建：`tests/test_crop_breeding_filter_rules.py`——验证规则文件没有恢复为「必须迁移到水稻」的旧门槛，并锁定评分和风险提示要求。

### 任务 1：用失败测试锁定跨作物准入规则

**文件：**
- 创建：`tests/test_crop_breeding_filter_rules.py`
- 读取：`config/ai_interests.txt`
- 读取：`config/ai_filter/prompt.txt`
- 读取：`config/config.yaml`

- [ ] **步骤 1：编写失败的规则回归测试**

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CropBreedingFilterRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interests = (ROOT / "config/ai_interests.txt").read_text(
            encoding="utf-8"
        )
        cls.prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )
        cls.config = (ROOT / "config/config.yaml").read_text(encoding="utf-8")

    def test_other_crops_are_admitted_by_breeding_value(self):
        self.assertIn("不作为准入前提", self.interests)
        self.assertIn("实质育种价值", self.interests)
        self.assertIn("不再要求先证明可迁移到水稻", self.prompt)
        self.assertNotIn(
            "其他作物只有在方法、资源或育种模式可明确迁移到水稻时才保留",
            self.prompt,
        )

    def test_title_only_breeding_news_has_an_admissible_score_band(self):
        self.assertIn("0.70～0.78", self.prompt)
        self.assertIn("仅标题显示：", self.prompt)
        self.assertIn("min_score: 0.7", self.config)

    def test_rice_priority_affects_ranking_not_admission(self):
        self.assertIn("水稻研究优先排序", self.interests)
        self.assertIn("排序和重要性评分", self.prompt)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：在容器 uv 环境中运行测试并确认失败**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/tests:/app/tests:ro \
  trendradar -m unittest tests.test_crop_breeding_filter_rules -v
```

预期：`FAIL`。失败信息应显示新准入措辞或 `0.70～0.78` 尚未出现在当前规则文件中。

### 任务 2：修改兴趣范围和 AI 分类提示词

**文件：**
- 修改：`config/ai_interests.txt`
- 修改：`config/ai_filter/prompt.txt`
- 测试：`tests/test_crop_breeding_filter_rules.py`

- [ ] **步骤 1：更新兴趣文件版本和总原则**

将版本提升到 `1.2.0`，并把文件开头的跨作物原则改为：

```text
# 水稻研究优先排序，但不构成其他作物新闻的准入门槛。
# 其他作物只要具有明确的实质育种价值即可纳入；能否迁移到水稻
# 只用于排序和重要性加分，不作为准入前提。
```

- [ ] **步骤 2：扩展第 20 类兴趣和低优先级规则**

将第 20 类改为覆盖主要粮食作物、经济作物、蔬菜和果树：

```text
20. 其他作物的重要育种进展：关注玉米、小麦、大豆、马铃薯、油菜、棉花、番茄、果树及其他作物中具有实质育种价值的基因编辑、种质资源、基因或 QTL、分子标记、抗病抗逆、产量品质改良、基因组选择、泛基因组、智能育种、新品种、田间验证和品种审定进展。
```

将原有「仅关注其他作物，且方法和结果无法迁移至水稻育种」排除项替换为：

```text
* 其他作物内容仅涉及一般生产经营、栽培管理或生态现象，没有明确的育种技术、遗传资源、性状改良、品种或种业政策信息
```

在分类原则中加入：

```text
* 水稻研究优先排序；其他作物按实质育种价值准入，迁移到水稻的价值只作为重要性加分项。
```

- [ ] **步骤 3：修改 AI 分类提示词中的证据和跨作物规则**

用以下规则替换当前仅标题和跨作物限制：

```text
8. 仅有标题时，若标题明确包含育种技术、种质资源、基因或 QTL、分子标记、性状改良、品种、田间验证、品种审定或基因编辑等实质育种信息，必须保留，score 应为 0.70～0.78；summary 必须以“仅标题显示：”开头，不得根据常识扩写标题
9. 水稻研究优先排序；其他作物只要具有明确的实质育种价值即可保留，不再要求先证明可迁移到水稻。迁移价值只影响排序和重要性评分，不作为准入条件
```

在 `importance_score` 规则后补充有摘要或正文时的评分边界：

```text
16. 有摘要或正文支持的其他作物实质育种新闻，应按证据质量和育种价值在 0.70～0.90 内评定 score；水稻直接相关内容在同等条件下优先获得更高的相关度和重要性
```

- [ ] **步骤 4：运行目标测试并确认通过**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/tests:/app/tests:ro \
  trendradar -m unittest tests.test_crop_breeding_filter_rules -v
```

预期：3 个测试全部 `OK`。

- [ ] **步骤 5：检查变更格式并提交**

运行：

```bash
git diff --check -- config/ai_interests.txt config/ai_filter/prompt.txt tests/test_crop_breeding_filter_rules.py
git add config/ai_interests.txt config/ai_filter/prompt.txt tests/test_crop_breeding_filter_rules.py
git commit -m "feat(筛选): 放宽跨作物育种新闻准入"
```

预期：只提交上述 3 个文件，不包含 `output/`、`index.html` 或其他已有工作区内容。

### 任务 3：完整验证并部署配置

**文件：**
- 验证：`config/ai_interests.txt`
- 验证：`config/ai_filter/prompt.txt`
- 验证：`tests/test_crop_breeding_filter_rules.py`
- 保持：`docker/.env`

- [ ] **步骤 1：运行完整测试集**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/tests:/app/tests:ro \
  trendradar -m unittest discover -s tests -v
```

预期：全部测试通过，不出现 `FAILED` 或 `ERROR`。

- [ ] **步骤 2：核对部署前配置边界**

运行：

```bash
rg -n "^(CRON_SCHEDULE|IMMEDIATE_RUN)=" docker/.env
rg -n "Version: 1.2.0|不作为准入前提|0.70～0.78|0.70～0.90" \
  config/ai_interests.txt config/ai_filter/prompt.txt
```

预期：定时任务仍为每天 09:00，`IMMEDIATE_RUN=false`；新准入和评分规则均存在。

- [ ] **步骤 3：重启正式服务加载配置**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml ps trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml logs --tail=30 trendradar
```

预期：容器状态为 `Up`，日志中的 crontab 为 `0 9 * * *`。由于 `IMMEDIATE_RUN=false`，重启不立即抓取或推送。

- [ ] **步骤 4：确认工作区只剩用户原有内容**

运行：

```bash
git status --short
git log -2 --oneline
```

预期：实现文件已提交；原有 `index.html`、`output/` 和其他未跟踪文档保持原状。
