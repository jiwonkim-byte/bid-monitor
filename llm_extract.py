"""Claude API로 입찰 공고 첨부 텍스트에서 사업 주요 내용 + 중소기업 참여제한 추출."""
from __future__ import annotations

import json
import os
import re

import anthropic

MODEL = "claude-haiku-4-5-20251001"

PROMPT_TEMPLATE = """다음은 공공기관 입찰공고 첨부파일에서 추출한 텍스트야. 두 가지 정보를 추출해줘.

1. **사업 주요 내용** (1~2문장, 60자 내외 한국어): 이 사업이 무엇을 하는지 핵심만 간결하게.
2. **중소기업 참여제한**: 아래 중 하나로 분류. 추가 조건이 있으면 짧게 부연.
   - "참여 가능": 중소기업도 입찰 가능
   - "참여 불가": 중소기업 입찰 제한 (대기업 한정, 직접생산자 한정 등)
   - "": 텍스트에 명시되지 않음

응답은 JSON 형식만 반환. 텍스트 설명 없이.
```json
{{"summary": "사업 주요 내용", "participation": "참여 가능|참여 불가|", "participation_note": "추가 조건 (선택)"}}
```

공고명: {title}

첨부 텍스트:
---
{text}
---
"""


def extract(title: str, text: str) -> dict:
    """LLM으로 사업 주요 내용 + 참여제한 추출.

    Returns: {"summary": str, "participation": str, "participation_note": str, "cost_in": int, "cost_out": int}
    """
    if not text or len(text) < 50:
        return {"summary": "", "participation": "", "participation_note": "", "cost_in": 0, "cost_out": 0}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = PROMPT_TEMPLATE.format(title=title, text=text)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"  ⚠️ LLM 호출 실패: {e}")
        return {"summary": "", "participation": "", "participation_note": "", "cost_in": 0, "cost_out": 0}

    body = resp.content[0].text.strip()
    # JSON 추출 (코드블록 마크다운 제거)
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.MULTILINE).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # 마지막 fallback: 가장 큰 {} 블록
        m = re.search(r"\{[^{}]*\}", body, flags=re.DOTALL)
        data = json.loads(m.group()) if m else {}

    return {
        "summary": (data.get("summary") or "").strip(),
        "participation": (data.get("participation") or "").strip(),
        "participation_note": (data.get("participation_note") or "").strip(),
        "cost_in": resp.usage.input_tokens,
        "cost_out": resp.usage.output_tokens,
    }


