"""위험 감지 규칙 엔진(G01 입력원). 설정 `SURVEY_RISK_RULES_FILE`(JSON)을 읽어 환자 발화를 규칙과 맞춰 본다.

- 이 모듈은 임상 규칙을 담지 않는다. 규칙은 전부 운영이 파일로 준다. 기본 규칙 파일은 없다(설정이 없으면 비활성).
- 규칙 파일 형식: {"version": str, "rules": [{"id": str, "description": str, "match": {"any_of": [{"contains": str} | {"regex": str}]}}]}.
  모르는 키, 빈 목록, 중복 id, 잘못된 정규식은 시작을 거부한다(ConfigError).
- 맞춤: 발화와 `contains` 문구는 유니코드 NFC로 맞추고 대소문자를 구분하지 않는다. `regex`는 NFC 발화에 re.search(대소문자 구분).
  규칙은 파일 순서로 보고 처음 걸린 규칙 하나만 쓴다.
- 걸리면 `RiskSource` 포트 계약대로 StaffAlert(kind RISK)를 돌려준다. 이유(reasons)의 evidence에는 발화 원문이 아니라 `signal:internal:<규칙 id>`만 넣는다
  (외부 감지기 API `POST /v1/sessions/{id}/risk-signals`와 같은 표기. 이 엔진은 서비스 안의 기본 감지기 하나다).
  알림, 푸시, 접근 기록, 로그 어디에도 발화 원문이 가지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Optional

from .settings import ConfigError

RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVIDENCE_PREFIX = "signal:"      # evidence = "signal:<source>:<code>"; 알림의 rule_id는 "<source>:<code>"
INTERNAL_SOURCE = "internal"
MAX_PATTERN = 500


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    contains: tuple     # NFC + casefold 된 문구
    regex: tuple        # 컴파일된 정규식


def _bad(msg: str) -> ConfigError:
    return ConfigError("위험 규칙 파일 오류: " + msg)


def _only(obj, allowed: set, where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise _bad(f"{where}에 모르는 키가 있다: {sorted(extra)}")


def parse_rules(raw: bytes) -> tuple[str, list[Rule]]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _bad("UTF-8 JSON이 아니다") from None
    if not isinstance(doc, dict):
        raise _bad("최상위가 객체가 아니다")
    _only(doc, {"version", "rules", "_comment"}, "최상위")   # _comment: JSON에 주석이 없어 설명용 키 하나만 허용(무시)
    version = doc.get("version")
    if not isinstance(version, str) or not version.strip():
        raise _bad("version은 비어 있지 않은 문자열이어야 한다")
    rules_raw = doc.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise _bad("rules는 비어 있지 않은 배열이어야 한다")
    seen, rules = set(), []
    for i, r in enumerate(rules_raw):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            raise _bad(f"{where}가 객체가 아니다")
        _only(r, {"id", "description", "match"}, where)
        rid = r.get("id")
        if not isinstance(rid, str) or not RULE_ID.match(rid):
            raise _bad(f"{where}.id는 영문·숫자·._- 1~64자여야 한다")
        if rid in seen:
            raise _bad(f"규칙 id가 겹친다: {rid}")
        seen.add(rid)
        if not isinstance(r.get("description"), str) or not r["description"].strip():
            raise _bad(f"{where}.description은 비어 있지 않은 문자열이어야 한다")
        match = r.get("match")
        if not isinstance(match, dict):
            raise _bad(f"{where}.match가 객체가 아니다")
        _only(match, {"any_of"}, where + ".match")
        any_of = match.get("any_of")
        if not isinstance(any_of, list) or not any_of:
            raise _bad(f"{where}.match.any_of는 비어 있지 않은 배열이어야 한다")
        contains, regexes = [], []
        for j, c in enumerate(any_of):
            cw = f"{where}.match.any_of[{j}]"
            if not isinstance(c, dict) or len(c) != 1 or next(iter(c)) not in ("contains", "regex"):
                raise _bad(f"{cw}는 contains 또는 regex 하나만 가져야 한다")
            kind, text = next(iter(c.items()))
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_PATTERN:
                raise _bad(f"{cw}.{kind}는 비어 있지 않은 {MAX_PATTERN}자 이하 문자열이어야 한다")
            if kind == "contains":
                contains.append(_norm(text).casefold())
            else:
                try:
                    regexes.append(re.compile(text))
                except re.error:
                    raise _bad(f"{cw}.regex가 올바른 정규식이 아니다") from None
        rules.append(Rule(rid, r["description"], tuple(contains), tuple(regexes)))
    return version, rules


# ADR-0013(위험 규칙 엔진 위치), D88 U2. **임상 검토 전 임시 기본 목록이다.** 널리 쓰이는 레드플래그(자해·자살, 흉통, 호흡곤란, 의식저하, 심한 출혈, 마비·뇌졸중 증상, 도움 요청)를
# 보수적으로(놓침 비용 > 거짓 알림 비용) 한국어 표현 변형과 함께 넣었다. 띄어쓰기는 무시하고 맞춘다. 병원이 `SURVEY_RISK_RULES_FILE`로 자기 목록을 주면 이 목록은 쓰이지 않는다.
# 임상 검토와 확정은 OPEN_GAPS(검토 전까지 /readyz의 staff_alert_source.provisional=true).
PROVISIONAL_VERSION = "provisional-2026-10-28"
_PROVISIONAL = {
    "self-harm": ("자살", "죽고싶", "죽을래", "죽어버리", "죽고만싶", "스스로목숨", "목숨을끊", "자해", "손목을그", "극단적선택", "살기싫", "사는게싫", "삶을끝"),
    "chest-pain": ("가슴이아", "가슴이너무아", "흉통", "가슴통증", "가슴이조이", "가슴이답답", "가슴을쥐어", "심장이아", "심장이조여", "심장이터"),
    "dyspnea": ("숨이안쉬", "숨을못쉬", "숨쉬기힘", "숨쉬기가힘", "숨쉬기어렵", "호흡곤란", "숨이막", "숨막혀", "숨이차서말", "숨이넘어"),
    "consciousness": ("의식을잃", "의식이없", "정신을잃", "정신을못차", "기절", "쓰러졌", "쓰러질것", "쓰러질거", "의식이흐", "정신이혼미"),
    "bleeding": ("피가많이", "피가계속나", "피가멈추지", "출혈이심", "피를토", "토혈", "피를많이", "하혈이심", "피가쏟"),
    "paralysis": ("마비", "팔다리에힘이안", "한쪽팔이안", "한쪽다리가안", "말이안나", "말이어눌", "말이잘안나", "입이돌아", "얼굴이한쪽", "한쪽이저리"),
    "help": ("살려", "도와주세요", "응급", "119", "구급차"),
}


def _provisional_raw() -> bytes:
    rules = [{"id": f"prov-{k}", "description": f"임시 기본 목록(임상 검토 전): {k}", "match": {"any_of": [{"contains": p} for p in ph]}}
             for k, ph in _PROVISIONAL.items()]
    return json.dumps({"version": PROVISIONAL_VERSION, "rules": rules}, ensure_ascii=False).encode("utf-8")


class RuleRiskSource:
    """`RiskSource` 구현. 파일을 한 번 읽어 고정한다(바꾸려면 다시 시작)."""

    utterance_only = True      # 발화 원문만 본다: 모델 추출과 무관하게 모든 턴에 적용할 수 있다(D88 U1)

    def __init__(self, raw: bytes, *, provisional: bool = False) -> None:
        self.provisional = provisional          # 임상 검토 전 임시 목록이면 True(/readyz에 드러난다)
        self.version, self.rules = parse_rules(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self._lock = threading.Lock()
        self.hits: dict[str, int] = {}

    @classmethod
    def provisional_default(cls) -> "RuleRiskSource":
        return cls(_provisional_raw(), provisional=True)

    @classmethod
    def from_file(cls, path: str) -> "RuleRiskSource":
        try:
            with open(path, "rb") as f:
                raw = f.read(1_000_001)
        except OSError as e:
            raise ConfigError(f"위험 규칙 파일을 읽을 수 없다: {type(e).__name__}") from None
        if len(raw) > 1_000_000:
            raise _bad("파일이 1MB를 넘는다")
        return cls(raw)

    def match(self, utterance: str) -> Optional[str]:
        text = _norm(utterance)
        folded = text.casefold()
        compact = re.sub(r"\s+", "", folded)           # 띄어쓰기 변형을 무시한다("숨이 안 쉬어져요" = "숨이안쉬어져요")
        for r in self.rules:
            if any(c in folded or c.replace(" ", "") in compact for c in r.contains) or any(rx.search(text) for rx in r.regex):
                return r.id
        return None

    def screen(self, utterance: str, extra_phrases=()) -> Optional[Mapping]:
        """모델 추출 전에 발화 원문만 본다(D88 U1). `extra_phrases`는 문항 세트에 저작된 risk_phrases(U2)."""
        rid = self.match(utterance or "")
        if rid is None:
            compact = re.sub(r"\s+", "", _norm(utterance or "").casefold())
            for ph in extra_phrases:
                if isinstance(ph, str) and ph.strip() and re.sub(r"\s+", "", _norm(ph).casefold()) in compact:
                    rid = "bank-phrase"
                    break
        if rid is None:
            return None
        with self._lock:
            self.hits[rid] = self.hits.get(rid, 0) + 1
        return risk_alert(INTERNAL_SOURCE, rid, "RISK")

    def check(self, *, extract_input: Mapping, extract_output: Mapping, utterance: str) -> Optional[Mapping]:
        rid = self.match(utterance or "")
        if rid is None:
            return None
        with self._lock:
            self.hits[rid] = self.hits.get(rid, 0) + 1
        return risk_alert(INTERNAL_SOURCE, rid, "RISK")

    def describe(self) -> dict:
        with self._lock:
            hits = dict(self.hits)
        return {"active": True, "source": "PROVISIONAL_DEFAULT" if self.provisional else "RULES_FILE", "provisional": self.provisional,
                "clinically_reviewed": not self.provisional, "rule_count": len(self.rules), "version": self.version, "sha256": self.sha256,
                "hits_by_rule": hits}


def risk_alert(source: str, code: str, severity: str = "RISK") -> dict:
    """위험 신호(내부 규칙 엔진과 외부 감지기 API가 함께 쓴다)를 StaffAlert로. 발화 원문은 없고 evidence에 `signal:<source>:<code>`만 담는다."""
    return {"kind": severity, "reasons": [{"kind": "RISK_SIGNAL", "evidence": [f"{EVIDENCE_PREFIX}{source}:{code}"]}]}


def rule_id_of(alert: Mapping) -> Optional[str]:
    """StaffAlert에서 `<source>:<code>`를 꺼낸다(evidence의 `signal:<source>:<code>`). 없으면 None."""
    for r in alert.get("reasons") or []:
        for ev in r.get("evidence") or []:
            if isinstance(ev, str) and ev.startswith(EVIDENCE_PREFIX):
                return ev[len(EVIDENCE_PREFIX):]
    return None
