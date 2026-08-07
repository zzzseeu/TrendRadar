# Elsevier Institutional Token 全文接入实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 ScienceDirect RSS 论文通过 Elsevier Article Retrieval API 获取结构化全文，并在认证、授权或解析失败时安全回退到现有正文抓取链路。

**架构：** 新增独立 `ElsevierFullTextClient`，负责 PII 识别、直连 API 请求和 XML 正文解析；`ArticleContentFetcher` 负责优先使用 API 正文以及现有 HTML、摘要、标题降级。配置加载器显式读取两项 Elsevier 环境变量，流水线把凭据传给正文抓取器，Docker Compose 只从本机 `.env` 注入密钥。

**技术栈：** Python 3.12、`requests`、`xml.etree.ElementTree`、`unittest`、Docker Compose

---

## 文件职责

- 创建 `trendradar/crawler/elsevier.py`：ScienceDirect PII 提取、Elsevier API 直连请求、XML 正文解析。
- 创建 `tests/test_elsevier_full_text.py`：客户端、正文抓取集成、配置和 Docker 传递测试。
- 修改 `trendradar/crawler/article_content.py`：优先采用 Elsevier API 正文并保留原有降级行为。
- 修改 `trendradar/ai/filter_pipeline.py`：把加载后的 Elsevier 凭据显式传给每个线程的正文抓取器。
- 修改 `trendradar/core/loader.py`：从环境变量加载 API Key 和 institutional token。
- 修改 `docker/docker-compose.yml`：向主服务注入两项 Elsevier 环境变量。
- 修改 `docker/.env.example`：记录空白凭据变量，不包含真实密钥。
- 本机修改 `docker/.env`：保存真实 institutional token；该文件不提交 Git。

### 任务 1：实现 Elsevier 全文客户端

**文件：**
- 创建：`trendradar/crawler/elsevier.py`
- 创建：`tests/test_elsevier_full_text.py`

- [ ] **步骤 1：编写 PII、请求和 XML 解析失败测试**

```python
FULL_TEXT_XML = b"""\
<full-text-retrieval-response xmlns:ce="urn:ce" xmlns:ja="urn:ja">
  <originalText>
    <ja:article><ja:body><ce:sections><ce:section>
      <ce:para>First <ce:bold>result</ce:bold> paragraph.</ce:para>
      <ce:para>Second result paragraph.</ce:para>
    </ce:section></ce:sections></ja:body></ja:article>
  </originalText>
</full-text-retrieval-response>
"""
METADATA_ONLY_XML = b"<full-text-retrieval-response><coredata /></full-text-retrieval-response>"


class ElsevierFullTextClientTests(unittest.TestCase):
    def test_extracts_only_sciencedirect_pii_urls(self):
        self.assertEqual(
            extract_sciencedirect_pii(
                "https://www.sciencedirect.com/science/article/pii/S1672630826000545?dgcid=rss"
            ),
            "S1672630826000545",
        )
        self.assertIsNone(extract_sciencedirect_pii("https://example.com/science/article/pii/S123"))

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_requests_full_xml_with_server_side_headers_and_no_proxy(self, session_factory):
        response = MagicMock(status_code=200, content=FULL_TEXT_XML)
        session_factory.return_value.get.return_value = response

        client = ElsevierFullTextClient("api-key", "inst-token", timeout=12)
        result = client.fetch(
            "https://www.sciencedirect.com/science/article/pii/S1672630826000545"
        )

        self.assertFalse(session_factory.return_value.trust_env)
        session_factory.return_value.get.assert_called_once_with(
            "https://api.elsevier.com/content/article/pii/S1672630826000545",
            params={"view": "FULL"},
            timeout=12,
        )
        self.assertEqual(result.status, "full_text")
        self.assertIn("First result paragraph", result.text)

    def test_parse_uses_article_body_paragraphs_in_order_without_nested_duplicates(self):
        text = parse_full_text_xml(FULL_TEXT_XML)
        self.assertEqual(text.count("First result paragraph"), 1)
        self.assertLess(text.index("First result paragraph"), text.index("Second result paragraph"))

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_non_200_timeout_broken_xml_and_metadata_only_are_unavailable(
        self, session_factory
    ):
        session_factory.return_value.get.side_effect = [
            MagicMock(status_code=401, content=b"auth failed"),
            requests.Timeout("slow"),
            MagicMock(status_code=200, content=b"<broken"),
            MagicMock(status_code=200, content=METADATA_ONLY_XML),
        ]
        client = ElsevierFullTextClient("api-key", "inst-token")

        statuses = [client.fetch(SCIENCEDIRECT_URL).status for _ in range(4)]

        self.assertEqual(
            statuses,
            ["http_401", "timeout", "invalid_xml", "body_unavailable"],
        )
```

测试夹具只包含最小 XML 结构：`originalText → article → body → sections → section → para`，正文使用虚构句子，不复制真实论文。

- [ ] **步骤 2：运行测试并确认因模块不存在而失败**

运行：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_elsevier_full_text.ElsevierFullTextClientTests -v
```

预期：FAIL，报错 `ModuleNotFoundError: No module named 'trendradar.crawler.elsevier'`。

- [ ] **步骤 3：编写最小客户端实现**

```python
@dataclass(frozen=True)
class ElsevierFetchResult:
    text: str
    status: str


def extract_sciencedirect_pii(url: str) -> Optional[str]:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() not in {"sciencedirect.com", "www.sciencedirect.com"}:
        return None
    match = re.fullmatch(r"/science/article/pii/([A-Za-z0-9]+)", parsed.path.rstrip("/"))
    return match.group(1) if match else None


class ElsevierFullTextClient:
    def __init__(self, api_key: str, inst_token: str, *, timeout: int = 12) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "X-ELS-APIKey": api_key,
            "X-ELS-Insttoken": inst_token,
            "Accept": "text/xml",
        })

    def fetch(self, url: str) -> ElsevierFetchResult:
        pii = extract_sciencedirect_pii(url)
        if not pii:
            return ElsevierFetchResult("", "unsupported_url")
        try:
            response = self.session.get(
                f"https://api.elsevier.com/content/article/pii/{pii}",
                params={"view": "FULL"},
                timeout=self.timeout,
            )
        except requests.Timeout:
            return ElsevierFetchResult("", "timeout")
        except requests.RequestException:
            return ElsevierFetchResult("", "request_failed")
        if response.status_code != 200:
            return ElsevierFetchResult("", f"http_{response.status_code}")
        try:
            text = parse_full_text_xml(response.content)
        except (ET.ParseError, UnicodeError, ValueError):
            return ElsevierFetchResult("", "invalid_xml")
        return ElsevierFetchResult(text, "full_text" if text else "body_unavailable")
```

`parse_full_text_xml()` 只在 `body` 元素内提取 `para`；若没有 `para`，再提取 `simple-para`。使用 `itertext()` 合并行内标签并统一空白，避免同时提取父子节点造成重复。

- [ ] **步骤 4：运行客户端测试并确认通过**

运行同步骤 2。

预期：客户端测试全部 PASS。

- [ ] **步骤 5：提交客户端与测试**

```bash
git add trendradar/crawler/elsevier.py tests/test_elsevier_full_text.py
git commit -m "feat(Elsevier): 添加机构令牌全文客户端"
```

### 任务 2：接入现有正文抓取与安全降级

**文件：**
- 修改：`trendradar/crawler/article_content.py`
- 修改：`tests/test_elsevier_full_text.py`

- [ ] **步骤 1：编写 API 优先与降级测试**

```python
class ArticleContentElsevierIntegrationTests(unittest.TestCase):
    def test_sciencedirect_api_full_text_is_used_before_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("A" * 800, "full_text")
        fetcher = ArticleContentFetcher(
            min_body_chars=300,
            elsevier_client=api_client,
        )
        fetcher.session = MagicMock()

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "elsevier_full_text")
        fetcher.session.get.assert_not_called()

    def test_metadata_only_api_response_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("", "body_unavailable")
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_missing_credentials_preserves_existing_html_summary_title_behavior(self):
        fetcher = ArticleContentFetcher(elsevier_api_key="", elsevier_inst_token="")
        self.assertIsNone(fetcher.elsevier_client)
```

另加截断测试，确认 API 正文超过 `max_content_chars` 后使用现有截断风险提示。

- [ ] **步骤 2：运行测试并确认因构造参数和优先路径缺失而失败**

运行：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_elsevier_full_text.ArticleContentElsevierIntegrationTests -v
```

预期：FAIL，`ArticleContentFetcher` 不接受 Elsevier 客户端或凭据参数。

- [ ] **步骤 3：实现 API 优先和原链路回退**

给 `ArticleContentFetcher.__init__()` 添加：

```python
elsevier_api_key: str = "",
elsevier_inst_token: str = "",
elsevier_client: Optional[ElsevierFullTextClient] = None,
```

仅当显式客户端存在，或两项凭据均非空时设置 `self.elsevier_client`。在公网 URL 校验后、HTML 请求前调用：

```python
if self.elsevier_client and extract_sciencedirect_pii(url):
    api_result = self.elsevier_client.fetch(url)
    if len(api_result.text) >= self.min_body_chars:
        return self._build_full_text_content(
            api_result.text,
            fetch_status="elsevier_full_text",
            source_note="正文来自 Elsevier Article Retrieval API",
        )
```

抽取 `_build_full_text_content()` 复用截断和风险提示逻辑。任何空结果继续执行现有 HTML 代码，不改变摘要和标题降级。

- [ ] **步骤 4：运行新增集成测试与原正文抓取测试**

运行：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_elsevier_full_text tests.test_direct_first_proxy -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交正文抓取接入**

```bash
git add trendradar/crawler/article_content.py tests/test_elsevier_full_text.py
git commit -m "feat(正文抓取): 优先使用 Elsevier API 全文"
```

### 任务 3：传递配置与 Docker 凭据

**文件：**
- 修改：`trendradar/core/loader.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`docker/docker-compose.yml`
- 修改：`docker/.env.example`
- 修改：`tests/test_elsevier_full_text.py`

- [ ] **步骤 1：编写配置和流水线传递测试**

```python
class ElsevierConfigurationTests(unittest.TestCase):
    def test_loader_reads_elsevier_credentials_from_environment(self):
        with patch.dict(os.environ, {
            "ELSEVIER_API_KEY": "api-key",
            "ELSEVIER_INST_TOKEN": "inst-token",
        }, clear=False):
            config = _load_ai_filter_config({"ai_filter": {}})
        content = config["CONTENT_ENRICHMENT"]
        self.assertEqual(content["ELSEVIER_API_KEY"], "api-key")
        self.assertEqual(content["ELSEVIER_INST_TOKEN"], "inst-token")

    @patch("trendradar.ai.filter_pipeline.ArticleContentFetcher")
    def test_pipeline_passes_elsevier_credentials_to_fetcher(self, fetcher_class):
        fetcher_class.return_value.get.return_value = ArticleContent(
            text="summary",
            level="summary",
            risk_warning="limited",
            fetch_status="body_unavailable",
        )
        pipeline = AIFilterPipeline(
            {
                "RSS": {"ENABLED": True},
                "AI_FILTER": {"CONTENT_ENRICHMENT": {
                    "ENABLED": True,
                    "FETCH_FULL_TEXT": True,
                    "TIMEOUT": 12,
                    "MAX_CONTENT_CHARS": 5000,
                    "MIN_BODY_CHARS": 300,
                    "CONCURRENCY": 1,
                    "ELSEVIER_API_KEY": "api-key",
                    "ELSEVIER_INST_TOKEN": "inst-token",
                }},
            },
            MagicMock(),
            lambda: None,
        )

        pipeline._enrich_pending_items(
            [{"id": 1, "title": "Paper", "url": SCIENCEDIRECT_URL}],
            "RSS",
        )

        fetcher_class.assert_called_once_with(
            timeout=12,
            max_content_chars=5000,
            min_body_chars=300,
            use_proxy=False,
            proxy_url="",
            elsevier_api_key="api-key",
            elsevier_inst_token="inst-token",
        )

    def test_compose_and_example_declare_server_side_credentials(self):
        compose = (PROJECT_ROOT / "docker/docker-compose.yml").read_text()
        example = (PROJECT_ROOT / "docker/.env.example").read_text()
        self.assertIn("ELSEVIER_API_KEY=${ELSEVIER_API_KEY:-}", compose)
        self.assertIn("ELSEVIER_INST_TOKEN=${ELSEVIER_INST_TOKEN:-}", compose)
        self.assertIn("ELSEVIER_INST_TOKEN=", example)
        self.assertNotIn("inst-token", example)
```

- [ ] **步骤 2：运行配置测试并确认失败**

运行：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/docker:/app/docker:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_elsevier_full_text.ElsevierConfigurationTests -v
```

预期：FAIL，配置字典和 Compose 尚无 institutional token 字段。

- [ ] **步骤 3：实现显式配置传递**

在 `_load_ai_filter_config()` 的 `CONTENT_ENRICHMENT` 中增加：

```python
"ELSEVIER_API_KEY": _get_env_str("ELSEVIER_API_KEY"),
"ELSEVIER_INST_TOKEN": _get_env_str("ELSEVIER_INST_TOKEN"),
```

在 `AIFilterPipeline._enrich_pending_items()` 创建抓取器时传递：

```python
elsevier_api_key=config.get("ELSEVIER_API_KEY", ""),
elsevier_inst_token=config.get("ELSEVIER_INST_TOKEN", ""),
```

在 Compose 主服务环境中增加：

```yaml
- ELSEVIER_API_KEY=${ELSEVIER_API_KEY:-}
- ELSEVIER_INST_TOKEN=${ELSEVIER_INST_TOKEN:-}
```

`docker/.env.example` 增加两个空变量和服务端保密注释。

- [ ] **步骤 4：运行配置与相关流水线测试**

运行同步骤 2，并追加：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/config:/app/config:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_ai_filter_classification_resilience tests.test_elsevier_full_text -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交配置接入**

```bash
git add trendradar/core/loader.py trendradar/ai/filter_pipeline.py \
  docker/docker-compose.yml docker/.env.example tests/test_elsevier_full_text.py
git commit -m "feat(配置): 注入 Elsevier 机构令牌"
```

### 任务 4：完整验证与安全检查

**文件：**
- 检查：全部已修改文件

- [ ] **步骤 1：运行完整测试套件**

运行：

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/config:/app/config:ro" \
  -v "$PWD/docker:/app/docker:ro" \
  -v "$PWD/index.html:/app/index.html:ro" \
  -w /app docker-trendradar \
  -m unittest discover -s tests -p "test_*.py"
```

预期：全部测试通过，0 failures、0 errors。

- [ ] **步骤 2：运行差异和凭据扫描**

```bash
git diff --check
git status --short
git diff --cached --stat
rg -n "X-ELS-(APIKey|Insttoken)|ELSEVIER_(API_KEY|INST_TOKEN)" \
  trendradar docker tests
```

逐条确认：代码中只有变量名与请求头名称；没有真实凭据值；`docker/.env` 未被跟踪。

- [ ] **步骤 3：审查提交与规格覆盖**

```bash
git log --oneline main..HEAD
git diff --stat main HEAD
```

对照规格核验 PII、直连、请求头、XML 正文、回退、配置、测试与安全要求均有实现。

### 任务 5：合并、部署和真实 API 验证

**文件：**
- 本机修改但不提交：`docker/.env`

- [ ] **步骤 1：将开发分支快进合并到 `main`**

在主工作区执行：

```bash
git merge --ff-only agent/elsevier-full-text-api
```

预期：`main` 快进到功能分支最新提交，用户原有未跟踪文件与 `index.html` 修改保持不变。

- [ ] **步骤 2：安全写入本机 institutional token**

在已忽略的 `docker/.env` 中保留现有 `ELSEVIER_API_KEY`，并把本轮邮件签发的 institutional token 写入 `ELSEVIER_INST_TOKEN`。真实值不在计划、命令输出或日志中展开；写入后只检查两项变量是否非空。

- [ ] **步骤 3：重建并重启主服务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
```

预期：`trendradar` 状态为 `Up`，日志仍显示现有定时表达式。

- [ ] **步骤 4：使用真实客户端验证开放与非开放获取论文**

在容器中调用 `ArticleContentFetcher`，分别测试：

- Rice Science：`S1672630826000545`。
- Molecular Plant 非开放获取论文：`S1674205225003259`。

只输出 `level`、`fetch_status` 和文本长度。预期两篇均为：

```text
level=full_text
fetch_status=elsevier_full_text
text_length>=300
```

- [ ] **步骤 5：验证服务状态、凭据安全和 Git 状态**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
git status --short
git ls-files docker/.env
```

预期：服务正常；`git ls-files docker/.env` 无输出；主工作区原有个人改动保持不变。
