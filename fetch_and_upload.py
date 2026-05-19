"""나라장터 입찰공고/사전규격 자동 수집 → 첨부파일 LLM 분석 → Google Sheets 업로드.

레퍼런스 시트(공공교육팀 AI/데이터 입찰사업 리스트) 호환 17컬럼.
사업 주요 내용 / 중소기업 참여제한은 첨부파일을 Claude Haiku로 분석.

환경변수:
  - G2B_API_KEY         : data.go.kr 인증키
  - GOOGLE_OAUTH_JSON   : OAuth credentials JSON 전체
  - SHEET_ID            : 마스터 스프레드시트 ID
  - ANTHROPIC_API_KEY   : Claude API 키
  - DAYS_BACK           : (옵션) 며칠 전부터 수집 (기본 2)
  - MAX_PAGES           : (옵션) API 최대 페이지 수 (기본 15)
  - ENRICH_LLM          : (옵션) LLM 추출 켜기 (기본 1 = 켜짐)
"""

import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import Counter

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from attachment_parser import extract_text
from llm_extract import extract as llm_extract

TAB_NAME = "수집결과"
HEADER = [
    "작성일시", "발주기관", "공고번호", "구분", "공고명",
    "공개일시", "마감일시", "유찰 여부", "사업 분류", "사업 주요 내용",
    "중소기업 참여제한", "예산 (VAT 별도)", "참고자료", "검색 키워드",
    "히스토리", "비고", "코멘트",
]

SOLO = ["교육", "온라인", "이러닝"]
COND = ["AI", "디지털", "데이터"]
EXCLUDE = ["급식", "학교행사", "안전교육", "구매"]


def normalize(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", "", s).lower()


def match_keywords(text: str):
    n = normalize(text)
    for ex in EXCLUDE:
        if normalize(ex) in n:
            return None
    matched = []
    for kw in SOLO:
        if normalize(kw) in n:
            matched.append(kw)
    has_ctx = ("교육" in n) or ("용역" in n)
    if has_ctx:
        for kw in COND:
            if normalize(kw) in n:
                matched.append(kw)
    return matched if matched else None


def classify_bsns(text: str) -> str:
    n = normalize(text)
    if any(k in n for k in ["이러닝", "elearning", "lms"]):
        return "이러닝"
    if any(k in n for k in ["콘텐츠제작", "컨텐츠제작", "콘텐츠개발", "컨텐츠개발", "영상제작", "영상개발", "동영상제작"]):
        return "컨텐츠제작"
    has_on = "온라인" in n
    has_off = any(k in n for k in ["오프라인", "집합교육", "현장교육", "캠프", "워크숍"])
    if has_on and has_off:
        return "온/오프라인 복합"
    if has_on or "비대면" in n:
        return "온라인"
    if any(k in n for k in ["과정개발", "커리큘럼개발", "교육과정개발", "교육과정설계", "교육체계", "교육과정연구"]):
        return "과정개발"
    if has_off:
        return "오프라인"
    return ""


def format_budget(amount) -> str:
    if not amount:
        return ""
    try:
        n = int(float(amount))
        if n <= 0:
            return ""
        return f"₩{n:,}"
    except (TypeError, ValueError):
        return ""


def participation_limit(yn: str) -> str:
    if yn == "Y":
        return "참여 불가"
    if yn == "N":
        return "참여 가능"
    return ""


def format_date(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return f"{d.year}. {d.month}. {d.day}"
        except ValueError:
            continue
    return s


def fetch_all(url: str, api_key: str, bgn: str, end: str, max_pages: int):
    items = []
    for page in range(1, max_pages + 1):
        params = {
            "serviceKey": api_key, "inqryDiv": "1",
            "inqryBgnDt": bgn, "inqryEndDt": end,
            "pageNo": str(page), "numOfRows": "100", "type": "json",
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        body = r.json().get("response", {}).get("body", {})
        page_items = body.get("items", [])
        if isinstance(page_items, dict):
            page_items = page_items.get("item", [])
        if not page_items:
            break
        items.extend(page_items)
        total = body.get("totalCount", 0)
        print(f"  page {page}: {len(page_items)}건 누적 {len(items)}/{total}")
        if len(items) >= total:
            break
    return items


def collect():
    """API에서 입찰공고+사전규격 수집 후 매칭 필터링.

    Returns: list of (row_17cols, attach_url, attach_filename)
    """
    api_key = os.environ["G2B_API_KEY"]
    days_back = int(os.environ.get("DAYS_BACK", "2"))
    max_pages = int(os.environ.get("MAX_PAGES", "15"))

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    yesterday_end = (now - dt.timedelta(days=1)).replace(hour=23, minute=59)
    start = (now - dt.timedelta(days=days_back)).replace(hour=0, minute=0)
    bgn = start.strftime("%Y%m%d%H%M")
    end = yesterday_end.strftime("%Y%m%d%H%M")
    collected_date = f"{now.year}. {now.month}. {now.day}"

    print(f"수집 기간 (KST): {bgn} ~ {end}  | max_pages={max_pages}")

    print("\n[1/2] 입찰공고 수집")
    bid_url = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServc"
    bid_items = fetch_all(bid_url, api_key, bgn, end, max_pages)

    print("\n[2/2] 사전규격 수집")
    pre_url = "https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc"
    pre_items = fetch_all(pre_url, api_key, bgn, end, max_pages)

    results = []
    bid_n = pre_n = 0

    for it in bid_items:
        name = it.get("bidNtceNm", "")
        matched = match_keywords(name)
        if not matched:
            continue
        bid_n += 1
        no = it.get("bidNtceNo", "")
        ord_ = it.get("bidNtceOrd", "")
        ann_no = f"{no} - {ord_}" if no and ord_ else (no or "")
        org = it.get("dminsttNm", "") or it.get("ntceInsttNm", "")
        open_dt = format_date(it.get("bidNtceDt", ""))
        close_dt = format_date(it.get("bidClseDt", "") or it.get("opengDt", ""))
        budget = format_budget(it.get("presmptPrce") or it.get("asignBdgtAmt"))
        limit = participation_limit(it.get("bidPrtcptLmtYn", ""))
        link = it.get("bidNtceDtlUrl") or (f"https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo={no}&bidPbancOrd=000" if no else "")
        bsns = classify_bsns(name)
        row = [
            collected_date, org, ann_no, "BID", name,
            open_dt, close_dt, "", bsns, "",
            limit, budget, link, ", ".join(matched),
            "", "", "",
        ]
        results.append((row, it.get("ntceSpecDocUrl1", ""), it.get("ntceSpecFileNm1", "")))

    for it in pre_items:
        name = it.get("prdctClsfcNoNm", "") or it.get("bidNtceNm", "")
        matched = match_keywords(name)
        if not matched:
            continue
        pre_n += 1
        no = it.get("bfSpecRgstNo", "")
        if not no:
            no = "PRE-" + hashlib.md5((name + (it.get("rlDminsttNm", "") or "")).encode()).hexdigest()[:10]
        org = it.get("rlDminsttNm", "") or it.get("orderInsttNm", "") or it.get("ntceInsttNm", "")
        open_dt = format_date(it.get("rgstDt", "") or it.get("chgDt", ""))
        close_dt = format_date(it.get("opninRgstClseDt", "") or it.get("rcptDt", ""))
        budget = format_budget(it.get("asignBdgtAmt"))
        link = it.get("specDocFileUrl1", "")
        bsns = classify_bsns(name)
        row = [
            collected_date, org, no, "PRE", name,
            open_dt, close_dt, "", bsns, "",
            "", budget, link, ", ".join(matched),
            "", "사전규격", "",
        ]
        # PRE 첨부 파일명 추정 (확장자만 — 실제 파일명 미제공)
        attach_fname = "spec.pdf" if link.endswith(".pdf") else "spec.hwpx"  # 추정
        results.append((row, link, attach_fname))

    print(f"\n매칭: BID {bid_n}건 / PRE {pre_n}건 / 합계 {len(results)}건")
    return results, now


def enrich_rows(triples):
    """각 행에 대해 첨부 다운로드+파싱+LLM 호출 → 사업 주요 내용/참여제한 채움.

    Returns: enriched rows + 누적 토큰/비용 통계
    """
    enable = os.environ.get("ENRICH_LLM", "1") == "1"
    if not enable:
        print("\nLLM 추출 비활성화 (ENRICH_LLM=0)")
        return [r for r, _, _ in triples], {"processed": 0, "tok_in": 0, "tok_out": 0}

    print(f"\n=== LLM 첨부 분석 시작 ({len(triples)}건) ===")
    stats = {"processed": 0, "tok_in": 0, "tok_out": 0, "ok": 0, "no_attach": 0, "unsupported": 0, "fail": 0}
    enriched_rows = []
    for i, (row, url, fname) in enumerate(triples, 1):
        title = row[4]
        if not url:
            stats["no_attach"] += 1
            enriched_rows.append(row)
            continue
        # PRE는 파일명에 확장자 추정 안전성 낮음 — URL 기반 추정 시도
        if row[3] == "PRE" and "." not in fname:
            fname = "spec.pdf"

        text, status = extract_text(url, fname)
        if status == "unsupported":
            stats["unsupported"] += 1
            enriched_rows.append(row)
            continue
        if status == "fail" or not text:
            stats["fail"] += 1
            enriched_rows.append(row)
            continue

        out = llm_extract(title, text)
        stats["processed"] += 1
        stats["tok_in"] += out["cost_in"]
        stats["tok_out"] += out["cost_out"]
        if out["summary"]:
            stats["ok"] += 1
            row[9] = out["summary"]  # 사업 주요 내용
            # 참여제한: API에서 이미 값 있으면 유지, 없거나 LLM이 더 구체적이면 덮어씀
            if out["participation"]:
                if out["participation_note"]:
                    row[10] = f"{out['participation']} ({out['participation_note']})"
                else:
                    row[10] = out["participation"]
        if i % 5 == 0:
            print(f"  진행: {i}/{len(triples)} (성공 {stats['ok']}, 미지원 {stats['unsupported']}, 실패 {stats['fail']})")
        enriched_rows.append(row)

    return enriched_rows, stats


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


def upload(rows, sheet_id: str):
    if not rows:
        print("업로드할 행 없음")
        return 0, 0
    creds = load_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB_NAME}!C2:C",
    ).execute()
    existing = {r[0] for r in resp.get("values", []) if r and r[0]}

    new_rows = []
    skipped = 0
    for row in rows:
        key = row[2]
        if key in existing:
            skipped += 1
            continue
        new_rows.append(row)

    if new_rows:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{TAB_NAME}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
    return len(new_rows), skipped


def estimate_cost(tok_in: int, tok_out: int) -> str:
    # Claude Haiku 4.5 pricing: $0.80/M input, $4.00/M output
    cost = tok_in * 0.80 / 1_000_000 + tok_out * 4.00 / 1_000_000
    return f"${cost:.4f}"


def summarize_deadlines(rows, now):
    soon = []
    for r in rows:
        c = (r[6] or "").strip()
        if not c:
            continue
        try:
            d = dt.datetime.strptime(c, "%Y. %m. %d")
        except ValueError:
            continue
        days = (d.date() - now.date()).days
        if 0 <= days <= 7:
            soon.append((days, d, r))

    if not soon:
        print("\n마감 임박 7일 이내: 없음")
        return
    print(f"\n=== 마감 임박 7일 이내 {len(soon)}건 ===")
    for days, d, r in sorted(soon, key=lambda x: x[0]):
        print(f"  D-{days} [{r[3]}] {r[4][:55]}")
        if r[9]:
            print(f"        📝 {r[9][:80]}")
        print(f"        {r[1]} | 마감 {r[6]} | 예산 {r[11] or 'N/A'} | KW: {r[13]}")


def main():
    sheet_id = os.environ["SHEET_ID"]
    triples, now = collect()
    enriched_rows, stats = enrich_rows(triples)
    added, skipped = upload(enriched_rows, sheet_id)
    print(f"\n시트 업데이트: 신규 {added}건 / 중복 {skipped}건 스킵")
    print(f"시트: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    if stats["processed"]:
        print(f"\nLLM 분석: 성공 {stats['ok']} / 미지원(HWP) {stats['unsupported']} / 다운로드·파싱 실패 {stats['fail']} / 첨부없음 {stats['no_attach']}")
        print(f"토큰: in {stats['tok_in']:,} / out {stats['tok_out']:,}  → 예상비용 {estimate_cost(stats['tok_in'], stats['tok_out'])}")

    kw_count = Counter()
    bsns_count = Counter()
    for r in enriched_rows:
        for k in r[13].split(","):
            k = k.strip()
            if k:
                kw_count[k] += 1
        if r[8]:
            bsns_count[r[8]] += 1
    if kw_count:
        print("\n키워드 분포: " + " · ".join(f"{k} {v}" for k, v in kw_count.most_common()))
    if bsns_count:
        print("사업 분류: " + " · ".join(f"{k} {v}" for k, v in bsns_count.most_common()))

    summarize_deadlines(enriched_rows, now)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
