"""Claude API로 입찰 공고 첨부 텍스트에서 사업 주요 내용 + 우리 회사 참여 가능 여부 판단."""
from __future__ import annotations

import json
import os
import re

import anthropic

MODEL = "claude-haiku-4-5-20251001"

# 우리 회사 프로필 (입찰 참여 가능성 판단 기준)
COMPANY_PROFILE = """[우리 회사 프로필]
- 기업 규모: 중견기업 (※ 중소기업/소상공인이 아니며, 대기업도 아님)
- 소재지: 서울특별시
- 보유 업종코드:
  - 1169 학술연구용역
  - 3156 평생교육시설(원격)
  - 3198 평생직업교육학원
  - 6527 이러닝콘텐츠업
  - 6528 이러닝솔루션업
  - 6529 이러닝서비스업
  - 9999 기타자유업종
"""

PROMPT_TEMPLATE = """다음은 공공기관 입찰공고 첨부파일에서 추출한 텍스트야. 우리 회사 입찰 참여 가능 여부를 판단해줘.

{profile}

[추출할 항목 — JSON으로 응답]
1. **summary**: 사업 주요 내용 (1~2문장, 60자 내외)
2. **participation**: 우리 회사 입찰 참여 가능 여부
   - "참여 가능": 명시된 제한이 우리 회사에 영향 없음 (또는 제한 자체가 없음)
   - "참여 불가": 명시된 제한 중 하나라도 우리 회사를 배제
3. **ineligible_reason**: 참여 불가일 때 구체 사유 한 문장. 참여 가능이면 빈 문자열.

[판단 가이드]
- "중소기업만" / "중소기업·소상공인만" / "소기업·소상공인 확인서 필수" → 우리는 중견기업 → **불가**
- "대기업 제외" → 우리는 중견 → **가능**
- "직접생산자만" / "제조업 한정" → 용역 중심인 우리 업종 → **불가**
- "특정 지역 소재 업체만" (예: 강원도/부산 등 서울 외) → **불가**. "전국" 또는 명시 없음 → **가능**
- "특정 업종코드만" (예: 5220 숙박업, 여행업 등) → 우리 보유 업종에 없으면 **불가**. 우리 업종(학술연구용역/평생교육/이러닝/기타자유업종 등) 중 하나라도 부합하면 **가능**
- 업종/지역/규모 제한 명시 없음 → **가능**
- 입찰참가자격에 "전문업체 인증" 등이 필요한데 우리 업종 중 하나로 충족 가능해 보이면 **가능**

응답 형식 (코드블록 없이 JSON만):
{{"summary": "...", "participation": "참여 가능", "ineligible_reason": ""}}

공고명: {title}

첨부 텍스트:
---
{text}
---
"""


def extract(title: str, text: str) -> dict:
    """LLM으로 사업 주요 내용 + 우리 회사 참여 가능 여부 추출.

    Returns: {summary, participation, ineligible_reason, cost_in, cost_out}
    """
    if not text or len(text) < 50:
        return {"summary": "", "participation": "", "ineligible_reason": "", "cost_in": 0, "cost_out": 0}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = PROMPT_TEMPLATE.format(profile=COMPANY_PROFILE, title=title, text=text)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"  ⚠️ LLM 호출 실패: {e}")
        return {"summary": "", "participation": "", "ineligible_reason": "", "cost_in": 0, "cost_out": 0}

    body = resp.content[0].text.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.MULTILINE).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", body, flags=re.DOTALL)
        data = json.loads(m.group()) if m else {}

    return {
        "summary": (data.get("summary") or "").strip(),
        "participation": (data.get("participation") or "").strip(),
        "ineligible_reason": (data.get("ineligible_reason") or "").strip(),
        "cost_in": resp.usage.input_tokens,
        "cost_out": resp.usage.output_tokens,
    }
