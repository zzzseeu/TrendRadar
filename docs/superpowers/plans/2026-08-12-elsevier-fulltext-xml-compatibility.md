# Elsevier 全文 XML 兼容实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 Elsevier 全文客户端正确提取官方 XSD 允许的标准文章、简化文章、转换文章及无正文节点时的 `rawtext`。

**架构：** 只扩展 `parse_full_text_xml()` 的内部候选定位，不改变客户端请求和上层降级接口。结构化正文保持最高优先级；`rawtext` 仅作为唯一 `xocs:doc` 没有可用正文时的明确回退。

**技术栈：** Python 3、`xml.etree.ElementTree`、`unittest`

---

## 文件结构

- 修改：`tests/test_elsevier_full_text.py`，增加脱敏 XML 结构回归测试。
- 修改：`trendradar/crawler/elsevier.py`，扩展正文定位和 `rawtext` 回退。

### 任务 1：兼容官方 Elsevier 正文结构

**文件：**
- 修改：`tests/test_elsevier_full_text.py`
- 修改：`trendradar/crawler/elsevier.py:41-67`

- [ ] **步骤 1：编写失败测试**

新增以下行为测试：

```python
def test_parse_uses_doc_rawtext_when_body_is_absent():
    xml = b"""<response><originalText><doc><meta />
      <rawtext>Abstract\n\nRice result.</rawtext>
      <serial-item><simple-article /></serial-item>
    </doc></originalText></response>"""
    self.assertEqual(parse_full_text_xml(xml), "Abstract Rice result.")

def test_parse_accepts_simple_and_converted_article_bodies():
    # 分别验证 simple-article/body 与 converted-article/body。
```

同时验证结构化 `body` 优先于同一 `doc` 的 `rawtext`，并验证多个 `rawtext` 失败关闭。

- [ ] **步骤 2：运行红灯**

运行：

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_elsevier_full_text.ElsevierFullTextClientTests -v
```

预期：新 `rawtext`、`simple-article`、`converted-article` 用例失败；既有标准正文用例通过。

- [ ] **步骤 3：实现最小兼容**

在 `parse_full_text_xml()` 中：

```python
original_texts = [e for e in root.iter() if _local_name(e.tag) == "originalText"]
if len(original_texts) != 1:
    return ""

bodies = _find_supported_article_bodies(original_texts[0])
if len(bodies) == 1:
    return _extract_outermost_paragraphs(bodies[0])
if bodies:
    return ""
return _extract_unique_doc_rawtext(original_texts[0])
```

支持的文章祖先仅限 `article`、`simple-article`、`converted-article`；`rawtext` 必须是唯一 `doc` 的唯一直接子元素，并统一折叠空白。

- [ ] **步骤 4：运行绿灯和集成回归**

运行：

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest tests.test_elsevier_full_text -v
```

预期：全部通过，0 failures/errors。

- [ ] **步骤 5：真实 Rice Science 只读验证**

使用已配置凭据请求 PII `S1672630826001034`，只打印状态与正文长度。预期：`status=full_text` 且长度大于 300，不输出正文或凭据。

- [ ] **步骤 6：静态检查并提交**

```bash
git diff --check
git add tests/test_elsevier_full_text.py trendradar/crawler/elsevier.py \
  docs/superpowers/plans/2026-08-12-elsevier-fulltext-xml-compatibility.md
git commit -m "fix(elsevier): 兼容多种全文 XML 结构"
```
