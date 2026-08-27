"""[5] 무료 전문 다운로드.

핵심은 **다운로드된 게 진짜 PDF인지 확인하는 것**이다. 출판사 사이트는
접근 권한이 없을 때 404를 주지 않고 로그인 페이지 HTML을 200으로 준다.
그걸 그대로 저장하면 다음 단계에서 HTML 태그를 논문 본문으로 착각하고,
결국 LLM이 로그인 페이지를 "요약"하게 된다.

매직 바이트(`%PDF-`) 확인이 그 사고를 막는 마지막 방어선이다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MAX_BYTES = 80 * 1024 * 1024  # 80MB. 이보다 큰 논문 PDF는 사실상 없다.


class NotAPdfError(Exception):
    """받아온 파일이 PDF가 아니다 (대개 로그인/페이월 페이지)."""


# 랜딩 페이지 HTML에서 실제 PDF 링크를 찾을 때 쓰는 패턴.
# OA 링크의 상당수는 PDF가 아니라 논문 소개 페이지를 가리킨다.
_PDF_META = re.compile(
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_PDF_META_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    re.IGNORECASE,
)
_PDF_HREF = re.compile(
    r'<a[^>]+href=["\']([^"\']*(?:\.pdf|/pdf/|/download|bitstream)[^"\']*)["\']',
    re.IGNORECASE,
)


def find_pdf_link(html: str, base_url: str) -> str | None:
    """랜딩 페이지 HTML에서 실제 PDF 주소를 찾는다.

    `citation_pdf_url` 메타 태그를 먼저 본다. 이건 Google Scholar가 색인할 때
    쓰라고 출판사·리포지토리가 넣어두는 표준 태그라, 있으면 거의 정확하다.
    없으면 .pdf / /pdf/ / bitstream 같은 링크를 찾는다(DSpace·EPrints 계열
    리포지토리가 이 형태를 쓴다).
    """
    for pattern in (_PDF_META, _PDF_META_REV):
        m = pattern.search(html)
        if m:
            return urljoin(base_url, m.group(1))

    for m in _PDF_HREF.finditer(html):
        href = m.group(1)
        if href.lower().startswith(("mailto:", "javascript:")):
            continue
        return urljoin(base_url, href)
    return None


class PdfDownloadError(Exception):
    """네트워크/HTTP 레벨 실패."""


def _download(pdf_url: str, timeout: int, capture_html: bool) -> tuple[bytes, str, str]:
    """1회 시도. (데이터, content-type, HTML 본문) 반환.

    capture_html=True면 PDF가 아닐 때 예외 대신 HTML을 돌려준다.
    랜딩 페이지에서 진짜 PDF 링크를 찾기 위해서다.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; paper-research-agent/0.1)",
        "Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5",
    }
    try:
        with requests.get(pdf_url, stream=True, timeout=timeout, headers=headers,
                          allow_redirects=True) as r:
            if not r.ok:
                raise PdfDownloadError(f"HTTP {r.status_code} — {pdf_url}")
            ctype = r.headers.get("Content-Type", "").lower()

            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)

                if len(chunks) == 1 and not chunk.startswith(PDF_MAGIC):
                    if capture_html and ("html" in ctype or chunk.lstrip()[:1] == b"<"):
                        # 랜딩 페이지로 보인다. 링크를 찾아야 하니 조금 더 읽는다.
                        html = b"".join(chunks)
                        for extra in r.iter_content(chunk_size=64 * 1024):
                            html += extra
                            if len(html) > 512 * 1024:
                                break
                        return b"", ctype, html.decode("utf-8", errors="replace")
                    hint = "로그인/페이월 페이지로 보임" if "html" in ctype else f"Content-Type: {ctype}"
                    raise NotAPdfError(f"PDF가 아님 ({hint}) — {pdf_url}")

                if total > MAX_BYTES:
                    raise NotAPdfError(f"용량 초과 ({total} bytes) — {pdf_url}")

    except requests.RequestException as e:
        raise PdfDownloadError(f"다운로드 실패: {e}") from e

    if not chunks:
        raise NotAPdfError(f"빈 응답 — {pdf_url}")
    return b"".join(chunks), ctype, ""


def fetch_pdf(pdf_url: str, save_path: str, timeout: int = 60) -> str:
    """PDF를 내려받아 저장하고 경로를 반환.

    받아온 게 랜딩 페이지면 그 HTML에서 실제 PDF 링크를 찾아 한 번 더 시도한다.
    OA 링크의 상당수가 PDF가 아니라 논문 소개 페이지를 가리키기 때문에,
    이 한 단계로 전문 확보율이 눈에 띄게 올라간다.

    Raises:
        PdfDownloadError: 네트워크 실패 또는 non-2xx
        NotAPdfError:     PDF 매직 바이트 불일치 / 용량 초과
    """
    dest = Path(save_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    data, ctype, html = _download(pdf_url, timeout, capture_html=True)

    if not data and html:
        link = find_pdf_link(html, pdf_url)
        if not link:
            raise NotAPdfError(
                f"랜딩 페이지이며 PDF 링크를 찾지 못함 — {pdf_url}"
            )
        if link == pdf_url:
            raise NotAPdfError(f"랜딩 페이지가 자기 자신을 가리킴 — {pdf_url}")
        log.info("랜딩 페이지에서 PDF 링크 발견: %s", link)
        # 두 번째 시도는 HTML을 다시 받지 않는다 (무한 추적 방지)
        data, ctype, _ = _download(link, timeout, capture_html=False)

    if not data:
        raise NotAPdfError(f"내용을 받지 못함 — {pdf_url}")

    dest.write_bytes(data)
    log.info("PDF 저장: %s (%.1f KB)", dest, len(data) / 1024)
    return str(dest)
