"""나라장터 첨부파일 다운로드 및 텍스트 추출.

지원 형식:
  - .pdf  : pdfplumber
  - .hwpx : zipfile + ElementTree (Contents/section*.xml 의 텍스트 노드)
  - .hwp  : pyhwp hwp5txt CLI (서브프로세스). HWP5 포맷만 지원, 매우 구버전 HWP는 실패할 수 있음.
"""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

USER_AGENT = "Mozilla/5.0 (bid-monitor)"
MAX_CHARS = 8000  # LLM 토큰 절약. 첨부 상단 N자만 사용 (사업 개요는 대부분 앞쪽에 위치)


def download(url: str, connect_timeout: int = 5, read_timeout: int = 30) -> bytes | None:
    # connect/read를 분리: g2b.go.kr가 응답을 안 할 때 빨리 포기해 워크플로 30분 한계 안에서 끝낸다.
    # read는 살아있는 다운로드가 큰 hwp/pdf를 받을 시간을 보장.
    if not url:
        return None
    try:
        r = requests.get(url, timeout=(connect_timeout, read_timeout), headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        if len(r.content) < 100:
            return None
        return r.content
    except Exception as e:
        print(f"  ⚠️ 다운로드 실패 ({url[:80]}): {e}")
        return None


def parse_pdf(content: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:20]:  # max 20 페이지
            t = page.extract_text()
            if t:
                parts.append(t)
            if sum(len(p) for p in parts) > MAX_CHARS:
                break
    return "\n".join(parts)


def parse_hwp(content: bytes) -> str:
    """HWP (구버전 바이너리) → pyhwp의 hwp5txt CLI로 텍스트 추출."""
    with tempfile.NamedTemporaryFile(suffix=".hwp", delete=False) as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        result = subprocess.run(
            ["hwp5txt", tmp_path],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=60,
        )
        if result.returncode != 0:
            return ""
        return result.stdout[:MAX_CHARS]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def parse_hwpx(content: bytes) -> str:
    """HWPX = ZIP + XML. Contents/section*.xml 안의 text 노드 추출."""
    parts = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            section_names = sorted(n for n in z.namelist()
                                   if n.startswith("Contents/section") and n.endswith(".xml"))
            for name in section_names:
                with z.open(name) as f:
                    xml = f.read().decode("utf-8", errors="ignore")
                try:
                    root = ET.fromstring(xml)
                    for elem in root.iter():
                        if elem.text and elem.text.strip():
                            parts.append(elem.text.strip())
                except ET.ParseError:
                    # fallback: 정규식으로 태그 제거
                    parts.append(re.sub(r"<[^>]+>", " ", xml))
                if sum(len(p) for p in parts) > MAX_CHARS:
                    break
    except zipfile.BadZipFile:
        return ""
    return "\n".join(parts)


def sniff_format(content: bytes) -> str:
    """다운로드된 바이트의 시그니처로 형식 추론.

    Returns: 'pdf' / 'hwpx' / 'hwp' / 'unknown'
    """
    if len(content) < 8:
        return "unknown"
    # PDF: 보통 첫 1KB 안에 %PDF 시그니처 (BOM 등으로 인해 0번이 아닐 수 있음)
    if b"%PDF" in content[:1024]:
        return "pdf"
    # HWPX = ZIP (HWP5+xml). PK\x03\x04
    if content[:4] == b"PK\x03\x04":
        # ZIP인데 HWPX인지 확인은 내용 보는 게 정확하지만, 첨부 컨텍스트에서는 hwpx로 시도
        return "hwpx"
    # HWP (OLE compound document)
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "hwp"
    return "unknown"


def extract_text(url: str, filename: str) -> tuple[str, str]:
    """첨부 다운로드 후 텍스트 추출.

    파일명 확장자보다 다운로드된 바이트의 매직 시그니처를 우선 사용한다.
    (PRE의 경우 파일명을 알 수 없어 'spec.pdf'로 가정하는데 실제론 HWP일 수 있음)

    Returns:
        (text, status): status는 'ok' / 'unsupported' / 'fail' 중 하나.
    """
    content = download(url)
    if content is None:
        return "", "fail"

    fmt = sniff_format(content)
    if fmt == "unknown":
        # 시그니처 추론 실패 — 확장자로 한번 더 시도
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("pdf", "hwpx", "hwp"):
            fmt = ext
        else:
            return "", "unsupported"

    try:
        if fmt == "pdf":
            text = parse_pdf(content)
        elif fmt == "hwpx":
            text = parse_hwpx(content)
        else:  # hwp
            text = parse_hwp(content)
        text = text[:MAX_CHARS]
        if len(text) < 100:
            return "", "fail"
        return text, "ok"
    except Exception as e:
        print(f"  ⚠️ 파싱 실패 ({filename}, sniffed={fmt}): {e}")
        return "", "fail"
