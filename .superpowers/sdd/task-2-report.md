# 任务 2 报告：南繁官方固定监控源

## 改动

- 在 `web_news.py` 登记海南中国南繁、三亚部门文件、三亚工作动态三个
  `web_news` profile；三亚来源共用南繁/水稻关键词集。
- 关键词资格判断使用完整列表项文本，避免短摘要因展示长度阈值未生成
  `summary` 时被误排除；输出摘要行为不变。
- 在中英文配置的官方时事来源区域加入三个默认启用的固定源。
- 机械修正三亚测试对相对链接的预期为绝对 URL。现有解析器统一通过
  `urljoin` 规范化文章 URL；经主任务裁定，保持这一既有契约，不为三亚
  引入例外。

## 验证命令与结果

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_official_rice_sources.OrdinaryOfficialSourceProfileTests -v
```

结果：`Ran 11 tests`，`OK`。

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_official_rice_sources \
  tests.test_direct_first_proxy -v
```

结果：`Ran 23 tests`，`OK`。

```bash
git diff --check
```

结果：退出码 0，无输出。

## 自审

- profile 名称、URL 正则、日期要求和共享关键词均与任务说明逐项一致。
- 三个配置项未设置 `enabled: false`，因此默认启用。
- 南繁宽 URL 规则仍受 `require_date=True` 约束，导航链接会被排除。
- 回归确认浏览器请求头与直连优先/代理回退行为未变化。

## 疑虑

无实现疑虑。任务测试原先要求三亚相对 URL，但与现有统一绝对 URL
规范化相冲突；已按主任务裁定修正测试预期并保留既有行为。

## 提交

`feat(南繁): 接入三个官方固定监控源`
