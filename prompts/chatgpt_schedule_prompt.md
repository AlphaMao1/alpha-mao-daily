# ChatGPT Scheduled Task 更新提示词

请更新现有 Alpha Mao 每日日报 Scheduled Task：

1. 每天固定读取 `data/latest.json` 的 raw.githubusercontent.com URL。
2. 不要临时网页搜索来补齐信息。
3. 只使用 JSON 中的 `eligible_items` 写日报正文。
4. `failures` 只用于抓取概览和风险提示。
5. 输出结构必须保持：

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
