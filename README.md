# Alpha Mao Daily

Alpha Mao Daily 是一个稳定的每日日报采集工程：GitHub Actions 负责完整抓取和落盘，ChatGPT 或 OpenAI API 只读取已采集产物生成中文日报。它不依赖 ChatGPT 每天临时浏览网页来凑信息。

## 产物

- `data/latest.json`：给 ChatGPT Scheduled Task 读取的轻量 brief JSON。
- `data/latest_raw_pointer.json`：指向当天 raw JSON 和 brief JSON。
- `data/YYYY-MM-DD/alpha_mao_daily_raw.json`：完整采集结果，包含成功项和失败项。
- `data/YYYY-MM-DD/alpha_mao_daily_brief.json`：当天 brief JSON。
- `reports/latest.md`：配置 `OPENAI_API_KEY` 后生成的最新中文日报。
- `reports/YYYY-MM-DD/alpha_mao_daily.md`：当天中文日报。

## 本地运行 collector

```bash
python -m pip install -r requirements.txt
python src/collector.py
```

运行后检查：

```bash
ls data
```

至少应看到：

```text
latest.json
latest_raw_pointer.json
YYYY-MM-DD/alpha_mao_daily_raw.json
YYYY-MM-DD/alpha_mao_daily_brief.json
```

## 本地生成 report

如果环境变量里有 `OPENAI_API_KEY`：

```bash
python src/generate_report.py
```

如果没有 `OPENAI_API_KEY`，脚本会输出：

```text
OPENAI_API_KEY not set; skipped report generation.
```

这不是 collector 失败，`data/latest.json` 仍然可供 ChatGPT schedule 使用。

## GitHub Actions

Workflow 文件：

```text
.github/workflows/alpha-mao-daily.yml
```

计划时间：

```yaml
schedule:
  - cron: "30 23 * * *"
```

这是 UTC 23:30，约等于北京时间 07:30。

也可以手动触发：

```bash
gh workflow run alpha-mao-daily.yml
```

## Secrets

建议配置：

```text
OPENAI_API_KEY
OPENAI_MODEL
YOUTUBE_COOKIES_B64
YOUTUBE_PROXY_URL
```

- `OPENAI_API_KEY`：用于 GitHub Actions 直接生成 `reports/latest.md`。
- `OPENAI_MODEL`：用于指定日报生成模型；不设置时脚本会使用内置默认模型。
- `YOUTUBE_COOKIES_B64`：可选，用于提高 YouTube transcript 抓取成功率。
- `YOUTUBE_PROXY_URL`：可选，仅用于 YouTube 列表和 transcript 抓取；RSS、GitHub、AIHOT 不走这个代理。

不要把真实 secret 写入 README、代码、`.env` 或终端日志。

## 配置 YOUTUBE_COOKIES_B64

1. 从浏览器导出 Netscape 格式的 `cookies.txt`。
2. 在本机把文件 base64 编码。
3. 用 GitHub CLI 交互式设置 secret：

```bash
gh secret set YOUTUBE_COOKIES_B64
```

粘贴 base64 内容时不要把它提交进仓库。没有这个 secret 时，YouTube transcript 仍会尝试公开字幕；失败项会写入 JSON。

## 配置 YouTube 住宅代理

如果 GitHub Actions runner 被 YouTube 拦截，可以配置住宅代理：

```bash
gh secret set YOUTUBE_PROXY_URL
```

粘贴代理 URL，例如：

```text
http://user:password@host:port
socks5h://user:password@host:port
```

不要把代理账号密码写进代码、README、`.env` 或日志。这个代理只会用于 YouTube 相关请求。

## 查看 latest.json

GitHub 仓库为 public 时，ChatGPT schedule 可读取：

```text
https://raw.githubusercontent.com/<OWNER>/<REPO>/main/data/latest.json
```

如果默认分支不是 `main`，把 URL 中的分支名换成实际默认分支。

## 查看 reports/latest.md

配置 `OPENAI_API_KEY` 且报告生成成功后，可读取：

```text
https://raw.githubusercontent.com/<OWNER>/<REPO>/main/reports/latest.md
```

## 交给 ChatGPT Scheduled Task

把 `data/latest.json` 的 raw URL 交给 ChatGPT，并要求它每天读取这个 JSON，按 `prompts/chatgpt_schedule_prompt.md` 的结构生成中文日报。ChatGPT 不应再自行搜索网页补材料。
