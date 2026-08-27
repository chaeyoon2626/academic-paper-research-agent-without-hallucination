"""[9-1] Zotero 내보내기.

## 왜 세 가지 형식인가

Zotero는 여러 형식을 받는데 쓰임새가 다르다.

- **RIS** — Zotero가 가장 잘 읽는다. 초록·태그·메모를 온전히 옮긴다.
  일반적으로는 이걸 쓰면 된다.
- **BibTeX** — LaTeX으로 논문을 쓸 때. `\\cite{key}`에 쓸 인용 키가 생긴다.
  초록과 메모 지원이 RIS보다 약하다.
- **CSV** — Zotero가 직접 읽지는 않지만, 엑셀에서 목록을 훑거나
  공동연구자에게 보낼 때 쓴다.

## 검증 정보를 함께 넘긴다

이 도구의 요점은 "이 논문이 실존하는가"와 "요약의 근거가 무엇인가"다.
그 정보를 빼고 서지만 넘기면 Zotero에서는 **전부 똑같아 보인다.**

그래서 태그로 넣는다:
    verify/verified, summary/full_text, tier/top ...

Zotero에서 `summary/abstract_only` 태그로 걸러내면 "전문을 더 구해야 하는
논문"이 바로 나온다.

## 무엇을 내보내는가

기본은 **검증을 통과한 것만**이다. `not_found`(존재 확인 실패)를 Zotero에
넣으면 나중에 그게 검증 실패한 항목인지 잊어버린다. `uncertain`은 선택적으로
포함할 수 있게 하되, 태그로 구분한다.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from core.models import NoteRecord, VerificationResult

log = logging.getLogger(__name__)

# BibTeX에서 이스케이프가 필요한 문자
_BIB_ESCAPE = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_NONWORD = re.compile(r"[^\w]", re.UNICODE)


def _bib_escape(s: str | None) -> str:
    if not s:
        return ""
    out = []
    for ch in str(s):
        out.append(_BIB_ESCAPE.get(ch, ch))
    return "".join(out).replace("\n", " ").strip()


def _split_name(name: str) -> tuple[str, str]:
    """'Lewis, Patrick' → ('Lewis', 'Patrick')

    서지 형식은 성/이름을 나눠 받는다. 붙여서 넣으면 Zotero가 저자를
    한 덩어리로 취급해 정렬과 인용이 어긋난다.

    **쉼표가 없으면 나누지 않는다.** 공백으로만 구분된 이름은 어느 쪽이
    성인지 알 수 없다 — 영어권은 뒤("Yann LeCun"의 LeCun), 한국어 로마자
    표기는 앞("Kim Minsu"의 Kim)이 성이다. 추측해서 뒤집으면 인용이
    "Minsu, K."처럼 틀리게 나온다.

    그래서 확신이 없을 때는 **전체를 성 자리에 그대로 둔다.** Zotero는 이걸
    단일 필드 이름으로 받아 원문 그대로 표시한다. 틀린 순서로 뒤집는 것보다
    낫고, 사용자가 Zotero에서 직접 고칠 수도 있다.

    쉼표는 서지 데이터에서 "성, 이름"을 뜻하는 표준 표기라 신뢰할 수 있다.
    Crossref는 성/이름을 나눠 주므로 `indexes.py`가 이 형태로 저장한다.
    """
    n = (name or "").strip()
    if not n:
        return "", ""
    if "," in n:
        last, _, first = n.partition(",")
        return last.strip(), first.strip()
    # 구분 불가 — 원문 그대로 둔다
    return n, ""


def _tags(rec: NoteRecord) -> list[str]:
    """검증 정보를 태그로. Zotero에서 이걸로 걸러낸다."""
    depth = rec.summary.depth if rec.summary else "no_summary"
    tags = [
        f"verify/{rec.verification.status}",
        f"summary/{depth}",
    ]
    if rec.venue_tier != "unknown":
        tags.append(f"tier/{rec.venue_tier}")
    if rec.fulltext_failure not in ("none", ""):
        tags.append(f"fulltext/{rec.fulltext_failure}")
    if rec.summary and rec.summary.depth == "abstract_only" and rec.summary.method_is_missing:
        tags.append("method/missing")
    return tags


def _note_text(rec: NoteRecord) -> str:
    """Zotero 항목에 붙일 메모. 신뢰 수준을 맨 앞에 둔다."""
    depth = rec.summary.depth if rec.summary else "no_summary"
    label = {
        "full_text": "전문 기반 요약",
        "abstract_only": "초록 기반 정리 — 전문 요약 아님",
        "no_summary": "요약 없음",
    }.get(depth, depth)

    lines = [f"[{label}]", f"검증: {rec.verification.status} — {rec.verification.reason}"]

    if rec.summary:
        if rec.summary.quotes:
            ok = rec.summary.verified_quote_count
            lines.append(f"인용 원문 대조: {ok}/{len(rec.summary.quotes)} 통과")
        if depth == "abstract_only":
            if rec.summary.method_is_missing:
                lines.append("⚠ 방법 정보가 초록에 없어 비어 있습니다.")
            if rec.summary.open_questions.strip():
                lines.append("전문 확인 필요: "
                             + rec.summary.open_questions.strip().replace("\n", " "))
        for head, text in (("배경", rec.summary.background),
                           ("방법", rec.summary.method),
                           ("핵심 결과", rec.summary.key_results_md)):
            if text and text.strip():
                lines.append(f"[{head}] {text.strip()}")

    if rec.fulltext_failure not in ("none", ""):
        lines.append(f"전문 미확보 사유: {rec.fulltext_detail or rec.fulltext_failure}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# RIS — Zotero가 가장 잘 읽는 형식
# ---------------------------------------------------------------------------


def to_ris(records: list[NoteRecord], uncertain: list[VerificationResult] | None = None) -> str:
    lines: list[str] = []

    def emit(cand, tags, note, ty="JOUR"):
        lines.append(f"TY  - {ty}")
        lines.append(f"TI  - {cand.title}")
        for a in cand.authors:
            last, first = _split_name(a)
            lines.append(f"AU  - {last}, {first}" if first else f"AU  - {last}")
        if cand.year:
            lines.append(f"PY  - {cand.year}")
        if cand.venue:
            lines.append(f"JO  - {cand.venue}")
        if cand.doi:
            lines.append(f"DO  - {cand.doi}")
        if cand.url:
            lines.append(f"UR  - {cand.url}")
        elif cand.doi:
            lines.append(f"UR  - https://doi.org/{cand.doi}")
        if cand.abstract:
            lines.append(f"AB  - {cand.abstract.replace(chr(10), ' ')}")
        for t in tags:
            lines.append(f"KW  - {t}")
        if note:
            # RIS는 줄바꿈을 못 담으므로 한 줄로 만든다
            lines.append(f"N1  - {note.replace(chr(10), ' | ')}")
        lines.append("ER  - ")
        lines.append("")

    for r in records:
        ty = "JOUR"
        if r.candidate.arxiv_id and not r.candidate.doi:
            ty = "GEN"      # 프리프린트
        emit(r.candidate, _tags(r), _note_text(r), ty)

    for v in uncertain or []:
        emit(v.candidate, ["verify/uncertain", "summary/no_summary"],
             f"[검증 불확실] {v.reason}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BibTeX — LaTeX으로 쓸 때
# ---------------------------------------------------------------------------


def _cite_key(cand, used: set[str]) -> str:
    """`kim2024organizational` 형태. 중복이면 뒤에 a, b를 붙인다."""
    last = _split_name(cand.authors[0])[0].lower() if cand.authors else "unknown"
    last = _NONWORD.sub("", last) or "unknown"
    year = cand.year or "nd"

    word = ""
    for w in re.findall(r"[A-Za-z]{4,}", cand.title or ""):
        if w.lower() not in {"the", "and", "for", "with", "from", "that", "this",
                             "using", "based", "study", "toward", "towards"}:
            word = w.lower()
            break

    base = f"{last}{year}{word}"
    key = base
    suffix = ord("a")
    while key in used:
        key = f"{base}{chr(suffix)}"
        suffix += 1
    used.add(key)
    return key


def to_bibtex(records: list[NoteRecord], uncertain: list[VerificationResult] | None = None) -> str:
    out: list[str] = []
    used: set[str] = set()

    def emit(cand, tags, note):
        key = _cite_key(cand, used)
        entry = "article"
        if cand.arxiv_id and not cand.doi:
            entry = "misc"

        fields = [f"  title = {{{_bib_escape(cand.title)}}}"]
        if cand.authors:
            names = " and ".join(
                f"{_bib_escape(_split_name(a)[0])}, {_bib_escape(_split_name(a)[1])}".rstrip(", ")
                for a in cand.authors
            )
            fields.append(f"  author = {{{names}}}")
        if cand.year:
            fields.append(f"  year = {{{cand.year}}}")
        if cand.venue:
            label = "journal" if entry == "article" else "howpublished"
            fields.append(f"  {label} = {{{_bib_escape(cand.venue)}}}")
        if cand.doi:
            fields.append(f"  doi = {{{cand.doi}}}")
        if cand.url:
            fields.append(f"  url = {{{cand.url}}}")
        if cand.abstract:
            fields.append(f"  abstract = {{{_bib_escape(cand.abstract)}}}")
        if tags:
            fields.append(f"  keywords = {{{', '.join(tags)}}}")
        if note:
            fields.append(f"  note = {{{_bib_escape(note.replace(chr(10), ' | '))}}}")

        out.append(f"@{entry}{{{key},\n" + ",\n".join(fields) + "\n}\n")

    out.append(f"% paper-research-agent 내보내기 — "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
               f"% keywords 필드의 verify/ · summary/ 태그로 신뢰 수준을 구분하세요.\n")

    for r in records:
        emit(r.candidate, _tags(r), _note_text(r))
    for v in uncertain or []:
        emit(v.candidate, ["verify/uncertain"], f"[검증 불확실] {v.reason}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# CSV — 엑셀로 훑을 때
# ---------------------------------------------------------------------------


CSV_COLUMNS = [
    "제목", "저자", "연도", "게재처", "DOI", "arXiv", "URL",
    "검증", "요약 근거", "저널 등급", "인용 대조", "방법 정보",
    "전문 미확보 사유", "노트 경로",
]


def write_csv(records: list[NoteRecord], path: str,
              uncertain: list[VerificationResult] | None = None) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # utf-8-sig: 엑셀이 BOM 없이는 한글을 깨뜨린다
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)

        for r in records:
            c = r.candidate
            depth = r.summary.depth if r.summary else "no_summary"
            quotes = ""
            if r.summary and r.summary.quotes:
                quotes = f"{r.summary.verified_quote_count}/{len(r.summary.quotes)}"
            method = ""
            if r.summary and depth == "abstract_only":
                method = "없음" if r.summary.method_is_missing else "있음"
            w.writerow([
                c.title, "; ".join(c.authors), c.year or "", c.venue or "",
                c.doi or "", c.arxiv_id or "", c.url or "",
                r.verification.status, depth, r.venue_tier, quotes, method,
                r.fulltext_detail if r.fulltext_failure != "none" else "",
                Path(r.note_path).name if r.note_path else "",
            ])

        for v in uncertain or []:
            c = v.candidate
            w.writerow([c.title, "; ".join(c.authors), c.year or "", c.venue or "",
                        c.doi or "", c.arxiv_id or "", c.url or "",
                        "uncertain", "no_summary", "", "", "", v.reason, ""])

    return str(p)


# ---------------------------------------------------------------------------
# 한 번에 내보내기
# ---------------------------------------------------------------------------


def export_all(
    question: str,
    records: list[NoteRecord],
    cfg: dict,
    uncertain: list[VerificationResult] | None = None,
) -> list[str]:
    """세션 결과를 Zotero용 파일로 저장. 만들어진 경로 목록 반환."""
    ex = cfg.get("export", {})
    if not ex.get("enabled", True) or not records:
        return []

    include_uncertain = bool(ex.get("include_uncertain", False))
    unc = uncertain if include_uncertain else None

    from core.obsidian_writer import session_root
    outdir = session_root(cfg) / ex.get("dir", "exports")
    outdir.mkdir(parents=True, exist_ok=True)

    # 세션 폴더 안이므로 파일명에 날짜를 또 붙이지 않는다
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", question)[:50].strip() or "논문 목록"

    written: list[str] = []
    formats = ex.get("formats", ["ris", "bibtex", "csv"])

    try:
        if "ris" in formats:
            p = outdir / f"{base}.ris"
            p.write_text(to_ris(records, unc), encoding="utf-8")
            written.append(str(p))
        if "bibtex" in formats:
            p = outdir / f"{base}.bib"
            p.write_text(to_bibtex(records, unc), encoding="utf-8")
            written.append(str(p))
        if "csv" in formats:
            written.append(write_csv(records, str(outdir / f"{base}.csv"), unc))
    except OSError as e:
        log.warning("내보내기 실패: %s", e)

    log.info("Zotero 내보내기 %d개 파일", len(written))
    return written
