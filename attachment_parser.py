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


def download(url: str, timeout: int = 30) -> bytes | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
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


def extract_text(url: str, filename: str) -> tuple[str, str]:
    """첨부 다운로드 후 텍스트 추출.

    Returns:
        (text, status): status는 'ok' / 'unsupported' / 'fail' 중 하나.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("pdf", "hwpx", "hwp"):
        return "", "unsupported"

    content = download(url)
    if content is None:
        return "", "fail"

    try:
        if ext == "pdf":
            text = parse_pdf(content)
        elif ext == "hwpx":
            text = parse_hwpx(content)
        else:  # hwp
            text = parse_hwp(content)
        text = text[:MAX_CHARS]
        if len(text) < 100:
            return "", "fail"
        return text, "ok"
    except Exception as e:
        print(f"  ⚠️ 파싱 실패 ({filename}): {e}")
        return "", "fail"
