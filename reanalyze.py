"""특정 시트 행을 재분석.

입력: 환경변수 ROW_NUMBERS (콤마 구분 행 번호 리스트, 예: "138,298,742")

각 행에 대해:
  1. 시트 행 데이터 읽기 (구분/공고번호/공고명/공개일시)
  2. BID: 공개일시 일자 범위로 BID API fetch → bidNtceNo+ord 매칭 → ntceSpecDocUrl1/FileNm1 추출
     PRE: 시트 M열의 specDocFileUrl1 그대로 사용 (PRE API 재호출은 일자 인덱싱 비용 큼)
  3. 첨부 다운로드 + 파싱
  4. LLM 분석 (사업 주요 내용 + 회사 프로필 기반 참여 가능 여부)
  5. 시트의 J(10), K(11), P(16) 열만 batchUpdate

J(사업 주요 내용)와 K(중기 참여제한)가 둘 다 비어 있어도 한쪽씩 채워질 수 있도록 각각 독립 갱신.
P(비고)는 기존 값(예: "사전규격") 뒤에 사유를 " / " 로 이어붙임 (참여 불가시).

환경변수:
  - G2B_API_KEY
  - GOOGLE_OAUTH_JSON
  - SHEET_ID
  - ANTHROPIC_API_KEY
  - ROW_NUMBERS
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from attachment_parser import extract_text
from llm_extract import extract as llm_extract

TAB = "수집결과"
BID_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServc"


def load_creds() -> Credentials:
    data = json.loads(os.environ["GOOGLE_OAUTH_JSON"])
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    creds.refresh(Request())
    return creds


def parse_open_date(s: str) -> dt.date | None:
    """공개일시 문자열에서 날짜만 추출."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def normalize_ord(ord_: str) -> str:
    """차수 표기 정규화. '000' / '00' / '0' / '' → '000', '1' → '001' 등."""
    s = (ord_ or "").strip()
    if not s:
        return ""
    try:
        return f"{int(s):03d}"
    except ValueError:
        return s


def fetch_bid_attachments(api_key: str, dates: list[dt.date]) -> dict:
    """각 일자별로 BID API fetch → {(no, ord_normalized): (url, fname)} 사전 반환.

    공개일이 fetch 일자 범위 밖에 있는 공고도 있을 수 있어서 ±2일 여유.
    여러 첨부 URL(ntceSpecDocUrl1~10) 중 첫 번째로 존재하는 것을 사용.
    차수가 빈 공고는 (no, '') 키로도 추가 등록해 시트의 차수 누락 케이스 대응.
    """
    if not dates:
        return {}
    min_d = min(dates) - dt.timedelta(days=2)
    max_d = max(dates) + dt.timedelta(days=2)
    bgn = min_d.strftime("%Y%m%d") + "0000"
    end = max_d.strftime("%Y%m%d") + "2359"
    print(f"  BID API 재조회: {bgn} ~ {end}")
    mapping = {}
    for page in range(1, 30):
        params = {
            "serviceKey": api_key, "inqryDiv": "1",
            "inqryBgnDt": bgn, "inqryEndDt": end,
            "pageNo": str(page), "numOfRows": "100", "type": "json",
        }
        r = requests.get(BID_URL, params=params, timeout=30)
        r.raise_for_status()
        body = r.json().get("response", {}).get("body", {})
        items = body.get("items", [])
        if isinstance(items, dict):
            items = items.get("item", [])
        if not items:
            break
        for it in items:
            no = (it.get("bidNtceNo") or "").strip()
            if not no:
                continue
            ord_ = normalize_ord(it.get("bidNtceOrd"))
            # url1~10 중 첫 번째로 존재하는 것
            url = ""
            fname = ""
            for i in range(1, 11):
                u = (it.get(f"ntceSpecDocUrl{i}") or "").strip()
                if u:
                    url = u
                    fname = (it.get(f"ntceSpecFileNm{i}") or "").strip()
                    break
            entry = (url, fname)
            mapping[(no, ord_)] = entry
            # 시트 차수 누락 케이스 대응: (no, '') 키로도 등록 (이미 있으면 덮어쓰지 않음)
            mapping.setdefault((no, ""), entry)
        total = body.get("totalCount", 0)
        if len(items) < 100 or (total and page * 100 >= total):
            break
    print(f"  BID API: {len(mapping)}개 키 인덱싱 (url 있는 행만 의미 있음)")
    return mapping


def parse_ann_no(ann_no: str) -> tuple[str, str]:
    """시트 C열 '공고번호' 포맷 'R26BK01539728 - 000' → ('R26BK01539728', '000').
    차수는 정규화된 3자리 형태로 반환. 차수 없으면 빈 문자열.
    """
    s = (ann_no or "").strip()
    if " - " in s:
        no, ord_ = s.split(" - ", 1)
        return no.strip(), normalize_ord(ord_)
    return s, ""


def main():
    row_numbers_raw = os.environ.get("ROW_NUMBERS", "").strip()
    if not row_numbers_raw:
        print("ROW_NUMBERS 비어있음")
        sys.exit(1)
    row_numbers = sorted({int(x) for x in row_numbers_raw.split(",") if x.strip()})
    print(f"재분석 대상 행: {row_numbers}")

    sheet_id = os.environ["SHEET_ID"]
    api_key = os.environ["G2B_API_KEY"]
    creds = load_creds()
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # 1) 시트에서 대상 행 데이터 읽기 (A~Q)
    ranges = [f"{TAB}!A{rn}:Q{rn}" for rn in row_numbers]
    resp = svc.spreadsheets().values().batchGet(
        spreadsheetId=sheet_id, ranges=ranges,
    ).execute()

    rows = []  # list of (row_num, vals)
    for rn, vr in zip(row_numbers, resp["valueRanges"]):
        vals = vr.get("values", [[]])[0] if vr.get("values") else []
        while len(vals) < 17:
            vals.append("")
        rows.append((rn, vals))

    # 2) BID 공개일자 수집 → API 재조회
    bid_dates = []
    for rn, v in rows:
        if v[3] == "BID":
            d = parse_open_date(v[5])
            if d:
                bid_dates.append(d)
    bid_map = fetch_bid_attachments(api_key, bid_dates)

    # 3) 각 행 분석 + 시트 업데이트 준비
    stats = {"ok": 0, "no_url": 0, "fail": 0, "unsupported": 0, "skipped": 0,
             "tok_in": 0, "tok_out": 0}
    updates = []  # list of {range, values}

    for rn, v in rows:
        kind = v[3]
        ann_no = v[2]
        title = v[4]
        j_existing = v[9]
        k_existing = v[10]
        p_existing = v[15]

        # 양쪽이 의미 있게 채워져 있으면 스킵
        if j_existing and j_existing.strip() and k_existing and k_existing.strip() not in ("", "참여 가능", "참여 불가"):
            # K가 LLM이 채운 명확한 값이면 스킵
            pass

        if kind == "BID":
            no, ord_ = parse_ann_no(ann_no)
            tup = bid_map.get((no, ord_)) or bid_map.get((no, ""))
            if not tup:
                print(f"  행{rn} [BID] {ann_no} ({no!r},{ord_!r}): BID API 매핑 없음 — 스킵")
                stats["no_url"] += 1
                continue
            url, fname = tup
            if not url:
                print(f"  행{rn} [BID] {ann_no}: BID API에 첨부 URL 없음 — 스킵")
                stats["no_url"] += 1
                continue
            if not fname or "." not in fname:
                fname = "doc.bin"  # 매직 시그니처로 추론
        elif kind == "PRE":
            url = v[12].strip().split("\n")[0]
            if not url:
                print(f"  행{rn} [PRE] {ann_no}: 시트 M열 비어있음 — 스킵")
                stats["no_url"] += 1
                continue
            fname = "spec.bin"  # 매직 시그니처로 추론
        else:
            stats["skipped"] += 1
            continue

        print(f"\n  행{rn} [{kind}] {ann_no} | {title[:50]}")
        print(f"    첨부: {fname}  {url[:90]}")

        text, status = extract_text(url, fname)
        if status == "unsupported":
            stats["unsupported"] += 1
            print(f"    파싱 미지원 ({fname})")
            continue
        if status == "fail" or not text:
            stats["fail"] += 1
            print(f"    다운로드/파싱 실패")
            continue

        out = llm_extract(title, text)
        stats["tok_in"] += out["cost_in"]
        stats["tok_out"] += out["cost_out"]
        if not out["summary"]:
            stats["fail"] += 1
            print(f"    LLM 응답 없음")
            continue

        stats["ok"] += 1
        # J열 (col index 10 = J)
        updates.append({"range": f"{TAB}!J{rn}", "values": [[out["summary"]]]})
        # K열 (col index 11 = K) — LLM이 명확한 판단을 내렸을 때만 업데이트
        if out["participation"]:
            updates.append({"range": f"{TAB}!K{rn}", "values": [[out["participation"]]]})
        # P열 (col index 16) — 불가 사유 있을 때만, 기존 값과 " / " 결합
        if out["participation"] == "참여 불가" and out["ineligible_reason"]:
            new_p = f"{p_existing} / {out['ineligible_reason']}" if p_existing else out["ineligible_reason"]
            updates.append({"range": f"{TAB}!P{rn}", "values": [[new_p]]})

        print(f"    ✓ J='{out['summary'][:60]}'  K='{out['participation']}'")

    # 4) batchUpdate
    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
        print(f"\n시트 업데이트: {len(updates)}개 셀")

    # 5) 통계
    cost = stats["tok_in"] * 0.80 / 1_000_000 + stats["tok_out"] * 4.00 / 1_000_000
    print(f"\n=== 재분석 결과 ===")
    print(f"  성공: {stats['ok']} / 첨부URL없음: {stats['no_url']} / 미지원: {stats['unsupported']} / 실패: {stats['fail']}")
    print(f"  토큰: in {stats['tok_in']:,} / out {stats['tok_out']:,}  → 예상비용 ${cost:.4f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
