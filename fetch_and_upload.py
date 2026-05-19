"""나라장터 입찰공고/사전규격 자동 수집 → Google Sheets 업로드.

GitHub Actions 환경변수:
  - G2B_API_KEY         : data.go.kr 인증키
  - GOOGLE_OAUTH_JSON   : OAuth credentials JSON 전체 (token/refresh_token/...)
  - SHEET_ID            : 마스터 스프레드시트 ID
  - DAYS_BACK           : (옵션) 며칠 전부터 수집할지 (기본 2)
"""

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TAB_NAME = "수집결과"
HEADER = ["공고번호", "구분", "수집일", "공고명", "발주기관", "마감일", "키워드", "링크"]

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


def fetch_all(url: str, api_key: str, bgn: str, end: str, max_pages: int = 5):
    items = []
    for page in range(1, max_pages + 1):
        params = {
            "serviceKey": api_key,
            "inqryDiv": "1",
            "inqryBgnDt": bgn,
            "inqryEndDt": end,
            "pageNo": str(page),
            "numOfRows": "100",
            "type": "json",
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
    api_key = os.environ["G2B_API_KEY"]
    days_back = int(os.environ.get("DAYS_BACK", "2"))

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))  # KST
    yesterday_end = (now - dt.timedelta(days=1)).replace(hour=23, minute=59)
    start = (now - dt.timedelta(days=days_back)).replace(hour=0, minute=0)
    bgn = start.strftime("%Y%m%d%H%M")
    end = yesterday_end.strftime("%Y%m%d%H%M")
    collected_date = now.strftime("%Y-%m-%d")

    print(f"수집 기간 (KST): {bgn} ~ {end}")

    print("\n[1/2] 입찰공고 수집")
    bid_url = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServc"
    bid_items = fetch_all(bid_url, api_key, bgn, end)

    print("\n[2/2] 사전규격 수집")
    pre_url = "https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc"
    pre_items = fetch_all(pre_url, api_key, bgn, end)

    rows = []
    bid_n = pre_n = 0

    for it in bid_items:
        name = it.get("bidNtceNm", "")
        matched = match_keywords(name)
        if not matched:
            continue
        bid_n += 1
        no = it.get("bidNtceNo", "")
        org = it.get("dminsttNm", "") or it.get("ntceInsttNm", "")
        close = it.get("bidClseDt", "") or it.get("opengDt", "")
        link = f"https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo={no}&bidPbancOrd=000" if no else ""
        rows.append([no, "BID", collected_date, name, org, close, ", ".join(matched), link])

    for it in pre_items:
        name = it.get("prdctClsfcNoNm", "") or it.get("bidNtceNm", "")
        matched = match_keywords(name)
        if not matched:
            continue
        pre_n += 1
        no = it.get("bfSpecRgstNo", "")
        if not no:
            no = "PRE-" + hashlib.md5((name + (it.get("rlDminsttNm", "") or "")).encode()).hexdigest()[:10]
        org = it.get("rlDminsttNm", "") or it.get("ntceInsttNm", "")
        close = it.get("opninRgstClseDt", "") or it.get("rcptDt", "")
        rows.append([no, "PRE", collected_date, name, org, close, ", ".join(matched), ""])

    print(f"\n매칭: BID {bid_n}건 / PRE {pre_n}건 / 합계 {len(rows)}건")
    return rows, now


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
        spreadsheetId=sheet_id, range=f"{TAB_NAME}!A2:A",
    ).execute()
    existing = {r[0] for r in resp.get("values", []) if r and r[0]}

    new_rows = []
    skipped = 0
    for row in rows:
        if row[0] in existing:
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


def summarize_deadlines(rows, now):
    soon = []
    for r in rows:
        c = (r[5] or "").strip()
        if not c:
            continue
        d = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y-%m-%d"):
            try:
                d = dt.datetime.strptime(c, fmt)
                break
            except ValueError:
                pass
        if not d:
            continue
        days = (d.date() - now.date()).days
        if 0 <= days <= 7:
            soon.append((days, d, r))

    if not soon:
        print("\n마감 임박 7일 이내: 없음")
        return
    print(f"\n=== 마감 임박 7일 이내 {len(soon)}건 ===")
    for days, d, r in sorted(soon, key=lambda x: x[0]):
        print(f"  D-{days} [{r[1]}] {r[3][:55]}")
        print(f"        {r[4]} | {d:%Y-%m-%d %H:%M} | KW: {r[6]}")


def main():
    sheet_id = os.environ["SHEET_ID"]
    rows, now = collect()
    added, skipped = upload(rows, sheet_id)
    print(f"\n시트 업데이트: 신규 {added}건 / 중복 {skipped}건 스킵")
    print(f"시트: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

    kw_count = Counter()
    for r in rows:
        for k in r[6].split(","):
            k = k.strip()
            if k:
                kw_count[k] += 1
    if kw_count:
        print("\n키워드 분포: " + " · ".join(f"{k} {v}" for k, v in kw_count.most_common()))

    summarize_deadlines(rows, now)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
