"""[4] 저널/학회 등급 대조.

기획서 §4-2: SJR(전체 분야 Quartile) + CORE(CS 컨퍼런스 A*/A/B/C).
사람이 분야별 리스트를 직접 만들지 않는다. 두 사이트에서 받은 CSV를 대조만 한다.

**검증 결과([3])에 영향을 주지 않는다.** 노트에 참고 태그로만 기록한다.
등급 없는 저널이 가짜인 것도 아니고, Q1이라고 내용이 옳은 것도 아니다.

데이터 파일이 없으면 조용히 "unknown"을 반환하고 계속 간다.
등급 대조는 부가 기능이지 파이프라인의 전제조건이 아니다.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

from core.models import PaperCandidate, VenueTier

log = logging.getLogger(__name__)

# 저널명 표기 흔들림 흡수용
_NOISE_RE = re.compile(r"\b(the|of|and|for|on|in|a|an|journal|proceedings|conference|international|annual)\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

TOP_SJR_QUARTILES = {"Q1"}
TOP_CORE_RANKS = {"A*", "A"}


def normalize_venue(name: str | None) -> str:
    """저널/학회명 정규화. 'Proc. of the 39th Intl. Conf. on ML' 같은 표기를 흡수."""
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s)
    s = _NOISE_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _load_csv(path: Path, name_col: str, value_col: str, sep: str = ",") -> dict[str, str]:
    if not path.exists():
        log.info("등급 데이터 없음, 건너뜀: %s", path)
        return {}
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=sep)
            if reader.fieldnames is None:
                return {}
            cols = {c.strip().lower(): c for c in reader.fieldnames}
            nc = cols.get(name_col.lower())
            vc = cols.get(value_col.lower())
            if not nc or not vc:
                log.warning("%s: '%s'/'%s' 컬럼을 찾을 수 없음 (있는 컬럼: %s)",
                            path.name, name_col, value_col, reader.fieldnames)
                return {}
            for row in reader:
                key = normalize_venue(row.get(nc))
                val = (row.get(vc) or "").strip()
                if key and val:
                    out.setdefault(key, val)
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        log.warning("등급 데이터 읽기 실패 (%s): %s", path, e)
        return {}
    log.info("%s에서 %d개 항목 로드", path.name, len(out))
    return out


@lru_cache(maxsize=4)
def _cached_sjr(path_str: str, sep: str) -> dict[str, str]:
    # SJR 다운로드 XLS를 CSV로 저장하면 'Title', 'SJR Best Quartile' 컬럼이 있다.
    return _load_csv(Path(path_str), "title", "sjr best quartile", sep)


@lru_cache(maxsize=4)
def _cached_core(path_str: str) -> dict[str, str]:
    # CORE 포털 CSV: 'title', 'rank' (헤더가 없는 버전도 있어 아래에서 폴백)
    d = _load_csv(Path(path_str), "title", "rank")
    if d:
        return d
    # 헤더 없는 CORE export 폴백: id,title,acronym,source,rank,...
    p = Path(path_str)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with p.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 5:
                    key = normalize_venue(row[1])
                    acro = normalize_venue(row[2])
                    rank = row[4].strip()
                    if key and rank:
                        out.setdefault(key, rank)
                    if acro and rank:
                        out.setdefault(acro, rank)
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}
    return out


def load_sjr_rankings(cfg: dict) -> dict[str, str]:
    """`data/sjr_rankings.csv` → {정규화된 저널명: Quartile}"""
    d = cfg.get("venue", {})
    return _cached_sjr(str(d.get("sjr_path", "data/sjr_rankings.csv")), d.get("sjr_delimiter", ";"))


def load_core_rankings(cfg: dict) -> dict[str, str]:
    """`data/core_rankings.csv` → {정규화된 학회명: A*/A/B/C}"""
    d = cfg.get("venue", {})
    return _cached_core(str(d.get("core_path", "data/core_rankings.csv")))


def check_venue_tier(candidate: PaperCandidate, cfg: dict) -> VenueTier:
    """venue명을 SJR/CORE와 대조해 top/normal/unknown 반환."""
    key = normalize_venue(candidate.venue)
    if not key:
        return "unknown"

    sjr = load_sjr_rankings(cfg)
    q = sjr.get(key)
    if q:
        return "top" if q.strip().upper() in TOP_SJR_QUARTILES else "normal"

    core = load_core_rankings(cfg)
    rank = core.get(key)
    if rank:
        return "top" if rank.strip().upper() in TOP_CORE_RANKS else "normal"

    return "unknown"


def venue_tier_detail(candidate: PaperCandidate, cfg: dict) -> tuple[VenueTier, str]:
    """등급 + 근거 문자열. 노트 frontmatter에 근거까지 남기기 위한 용도."""
    key = normalize_venue(candidate.venue)
    if not key:
        return "unknown", "venue 정보 없음"
    q = load_sjr_rankings(cfg).get(key)
    if q:
        tier = "top" if q.strip().upper() in TOP_SJR_QUARTILES else "normal"
        return tier, f"SJR {q}"
    rank = load_core_rankings(cfg).get(key)
    if rank:
        tier = "top" if rank.strip().upper() in TOP_CORE_RANKS else "normal"
        return tier, f"CORE {rank}"
    return "unknown", "SJR/CORE 목록에 없음"
