# AI 标签 JSON 容错实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 AI 标签提取能够恢复字符串内未转义控制字符，并在其他 JSON 错误时低温重试一次。

**架构：** 保持 `AIFilter.extract_tags()` 为唯一编排入口，将控制字符兼容限制在 `_parse_tags_response()` 内。首次无法解析时，把原响应作为对话上下文请求一次纯 JSON 修复；所有标签提取调用固定使用 `temperature=0`，现有字段校验和最终关键词回退语义保持不变。

**技术栈：** Python 3.12、标准库 `json`、`unittest`、`unittest.mock`、Docker Compose、项目容器内 `.venv`。

---

### 任务 1：用回归测试固定容错行为

**文件：**
- 创建：`tests/test_ai_filter_tag_json_resilience.py`
- 测试：`trendradar/ai/filter.py:142-201`
- 测试：`trendradar/ai/filter.py:313-328`

- [ ] **步骤 1：编写控制字符兼容测试**

创建 `TagJsonResilienceTests`，通过 `AIFilter.__new__(AIFilter)` 构造最小对象，并断言以下响应可解析：

```python
response = (
    '{"tags":[{"tag":"基因组育种",'
    '"description":"第一行' + "\n" + '第二行' + "\t" + '关键词"}]}'
)
self.assertEqual(
    self.ai_filter._parse_tags_response(response),
    [{"tag": "基因组育种", "description": "第一行\n第二行\t关键词"}],
)
```

- [ ] **步骤 2：编写低温调用和重试测试**

使用 `Mock` 客户端覆盖：

```python
self.ai_filter.client.chat.side_effect = [
    '{"tags":[}',
    '{"tags":[{"tag":"水稻育种","description":"有效"}]}',
]
tags = self.ai_filter.extract_tags("关注水稻育种")
self.assertEqual(tags[0]["tag"], "水稻育种")
self.assertEqual(self.ai_filter.client.chat.call_count, 2)
for call in self.ai_filter.client.chat.call_args_list:
    self.assertEqual(call.kwargs["temperature"], 0)
```

再覆盖连续两次不可解析时返回 `[]`，并确认恰好调用 2 次。

- [ ] **步骤 3：运行目标测试验证红灯**

运行：

```bash
docker compose --env-file /mnt/d/project/trendradar/docker/.env \
  -f /mnt/d/project/trendradar/docker/docker-compose.yml run --rm -T --no-deps \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar/.worktrees/ai-tag-json-resilience/trendradar:/app/trendradar:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-tag-json-resilience/config:/app/config:ro \
  -v /mnt/d/project/trendradar/.worktrees/ai-tag-json-resilience/tests:/app/tests:ro \
  trendradar -m unittest tests.test_ai_filter_tag_json_resilience -v
```

预期：控制字符测试因严格 `json.loads()` 失败；重试测试因当前仅调用 1 次且未传 `temperature=0` 失败。

### 任务 2：实现定向兼容与一次重试

**文件：**
- 修改：`trendradar/ai/filter.py:142-201`
- 修改：`trendradar/ai/filter.py:313-328`
- 测试：`tests/test_ai_filter_tag_json_resilience.py`

- [ ] **步骤 1：实现控制字符定向兼容**

在 `_parse_tags_response()` 中先严格解析，仅针对控制字符错误重试：

```python
try:
    data = json.loads(json_str)
except json.JSONDecodeError as error:
    if not error.msg.startswith("Invalid control character"):
        raise
    data = json.loads(json_str, strict=False)
```

- [ ] **步骤 2：实现低温请求和解析失败重试**

`extract_tags()` 第一次调用使用 `temperature=0`。捕获第一次 `JSONDecodeError` 后，追加原始 assistant 响应和纯 JSON 修复要求，再以 `temperature=0` 调用一次；第二次失败沿用现有空列表返回和错误日志。

修复提示词固定为：

```text
上一个响应不是可解析的 JSON。请修正语法并仅返回一个 JSON 对象，
格式必须为 {"tags":[{"tag":"标签名","description":"描述"}]}。
字符串内的换行和制表符必须转义，不要添加 Markdown 或解释。
```

- [ ] **步骤 3：运行目标测试验证绿灯**

重复任务 1 步骤 3 的命令，预期所有测试通过。

- [ ] **步骤 4：运行完整测试**

使用相同挂载执行：

```bash
/app/.venv/bin/python -m unittest discover -s tests
```

预期：全部通过，且没有 `FAILED` 或 `ERROR`。

- [ ] **步骤 5：检查并提交实现**

```bash
git diff --check
git add trendradar/ai/filter.py tests/test_ai_filter_tag_json_resilience.py
git commit -m "fix(AI筛选): 容错标签JSON控制字符"
```

### 任务 3：集成与真实运行验证

**文件：**
- 验证：`docker/docker-compose.yml`
- 验证：`output/html/2026-08-05/`

- [ ] **步骤 1：快进合并功能分支到 `main`**

确认主工作区仅保留用户原有变更，然后执行：

```bash
git merge --ff-only agent/ai-tag-json-resilience
```

- [ ] **步骤 2：在主分支重新运行完整测试**

将主目录的 `trendradar/`、`config/`、`tests/` 挂载到测试容器，预期全部通过。

- [ ] **步骤 3：重建并重启服务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  up -d --build --force-recreate trendradar
```

确认容器为 `Up`，定时表达式仍为 `0 9 * * *`。

- [ ] **步骤 4：立即补跑一次任务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml \
  exec -T trendradar python -m trendradar
```

核对日志：标签提取成功或在解析失败后重试成功；AI 摘要和翻译有明确结果；通知成功或因无匹配内容而明确跳过。若模型/API 仍超时，准确报告外部故障，不宣称本地修复失效。

- [ ] **步骤 5：清理隔离工作树**

确认分支已合并且工作树干净后，移除 `.worktrees/ai-tag-json-resilience` 并删除临时分支。
