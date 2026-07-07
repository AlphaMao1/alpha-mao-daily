# ChatGPT Scheduled Task 更新提示词

请更新现有 Alpha Mao 每日日报 Scheduled Task：

1. 每天固定读取这个 JSON：
   `https://raw.githubusercontent.com/AlphaMao1/alpha-mao-daily/main/data/latest.json`
2. 不要临时网页搜索来补齐信息。
3. 只使用 JSON 中的 `eligible_items` 写日报正文。
4. 每条 eligible item 的正文证据来自 `text_excerpt`；`title`、`source/channel/full_name`、`url/original_url` 只作为来源标识。
5. `text_source_field` 表示 `text_excerpt` 来自原始采集里的哪个字段，例如 RSS 的 `full_text`、YouTube 的 `transcript_text`、GitHub 的 `readme_text`、AIHOT 的 `original_text`。
6. `text_truncated_in_raw=true` 只表示原始长文本被截断，不表示不可用；可以正常使用 `text_excerpt`，但不要声称读过完整外部网页。
7. `failures` 只用于抓取概览和风险提示，不能写成事实正文。
8. 如果某一类 `eligible_items.<type>` 为空，就在对应章节写“今日无可用正文项”，不要用失败项或外部搜索补齐。
9. 输出结构必须保持：

```text
Alpha Mao 的日报 - YYYY-MM-DD

0. 抓取概览
1. 今天最重要的 3-5 个判断
2. RSS / 文章
3. YouTube / Transcript
4. GitHub / 工具
5. AIHOT / 第三方聚合源
6. 今日行动清单
7. 长期沉淀线索
8. 低优先级 / 暂不处理
9. 最终压缩版
```

建议执行时间：Asia/Shanghai 08:15 每日。
