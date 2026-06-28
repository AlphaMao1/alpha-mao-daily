from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PROMPT_PATH = ROOT / "prompts" / "report_prompt.md"


def main() -> int:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set; skipped report generation.")
        return 0

    latest_path = DATA_DIR / "latest.json"
    if not latest_path.exists():
        print("data/latest.json not found; run collector first.", file=sys.stderr)
        return 1

    brief = json.loads(latest_path.read_text(encoding="utf-8"))
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "你是严谨的中文日报编辑。只能使用用户提供 JSON 中的 eligible_items 写正文；"
                    "不要编造，不要使用失败项作为事实。"
                ),
            },
            {
                "role": "user",
                "content": prompt + "\n\n输入 JSON：\n" + json.dumps(brief, ensure_ascii=False, indent=2),
            },
        ],
    )

    markdown = extract_response_text(response).strip()
    if not markdown:
        print("OpenAI response was empty.", file=sys.stderr)
        return 1

    date = brief.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_dir = REPORTS_DIR / date
    dated_dir.mkdir(parents=True, exist_ok=True)
    dated_report = dated_dir / "alpha_mao_daily.md"
    latest_report = REPORTS_DIR / "latest.md"

    dated_report.write_text(markdown + "\n", encoding="utf-8", newline="\n")
    latest_report.write_text(markdown + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {dated_report.relative_to(ROOT)}")
    print(f"Wrote {latest_report.relative_to(ROOT)}")
    return 0


def extract_response_text(response: object) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


if __name__ == "__main__":
    raise SystemExit(main())
