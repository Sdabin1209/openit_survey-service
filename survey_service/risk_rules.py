"""위험 감지 규칙 엔진(G01 입력원). 설정 `SURVEY_RISK_RULES_FILE`(JSON)을 읽어 환자 발화를 규칙과 맞춰 본다.

- 규칙은 운영이 파일로 준다. 파일이 없으면 **임상 검토 전 임시 기본 목록**(`_PROVISIONAL`)이 켜진다(`SURVEY_RISK_DEFAULT_RULES=off`로 끄면 비활성).
- 규칙 파일 형식: {"version": str, "rules": [{"id": str, "description": str, "match": {"any_of": [{"contains": str} | {"regex": str}]}}]}.
  모르는 키, 빈 목록, 중복 id, 잘못된 정규식은 시작을 거부한다(ConfigError).
- 맞춤: 발화와 `contains` 문구는 유니코드 NFC로 맞추고 대소문자를 구분하지 않으며, 구절은 어절 시작에서만 시작하고 구절 안의 공백은 있어도 없어도 된다. 같은 절 뒤쪽의 부정 표지나 앞쪽의 가족·제3자 표지가 있으면 정지하지 않고 세기만 한다(아래 `_PROVISIONAL` 주석). `regex`는 NFC 발화에 re.search(대소문자 구분).
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
# 놓침 비용 > 거짓 알림 비용으로 넣되, 정상 응답("자살 생각은 없어요", "가슴이 아프지 않아요")이 정지로 가지 않게 매칭을 정밀화했다(2차 점검 H1·H2):
#  - 구절은 **어절 시작**에서만 시작한다. 구절 안의 공백은 있어도 없어도 된다("숨이안쉬어"). 어절을 가로질러 합쳐 맞추지 않는다.
#  - 구절 **뒤 같은 절**에 부정 표지(없/않/아니/못…)가 있으면 부정된 위험 구절로 보고 정지하지 않는다(`negated_hits`로 센다). 구절 자체가 부정형인 위험 표현은 `_RAW_NEGATIVE`에 두어 부정 처리를 하지 않는다.
#  - 같은 절 앞에 가족·제3자 표지가 있고 본인 표지가 없으면 정지하지 않는다(`third_party_hits`). 영어 구절은 앞쪽 부정(no/not/without)을 본다.
#  - 숫자 문맥: "119"는 단독으로 쓰지 않고 "119 불러", "119에 전화"처럼 행동 구절로만 둔다("혈압이 119에 80" 제외).
# 구조 유지(deploy E2E 5k·5m이 ast로 읽는다): `_PROVISIONAL`은 {규칙 키: 구절 문자열 튜플}의 리터럴이고 "chest-pain"의 첫 구절은 부정 처리 없이 그대로 적중한다.
# 병원이 `SURVEY_RISK_RULES_FILE`로 자기 목록을 주면 이 목록은 쓰이지 않는다. 임상 검토와 확정은 OPEN_GAPS(검토 전까지 /readyz의 staff_alert_source.provisional=true).
PROVISIONAL_VERSION = "provisional-2026-10-28.2"
_PROVISIONAL = {
    "self-harm": ("자살", "죽고 싶", "죽을 것 같", "죽을 거 같", "죽을래", "죽어버리", "자해", "손목을 긋", "손목을 그었", "손목을 그어", "목숨을 끊", "스스로 목숨",
                  "극단적 선택", "뛰어내리", "사라지고 싶", "다 끝내고 싶", "삶을 끝", "살고 싶지 않", "살기 싫", "사는 게 싫", "더 이상 살 수 없", "살 이유가 없",
                  "살 의미가 없", "suicide", "kill myself", "want to die"),
    "chest-pain": ("가슴이 아프", "가슴이 아파", "가슴이 너무 아", "흉통", "가슴 통증", "가슴에 통증", "가슴이 조이", "가슴이 답답", "가슴을 쥐어", "가슴이 쥐어",
                   "가슴이 터질", "가슴이 눌", "가슴 쪽이 아", "심장이 아프", "심장이 조여", "심장이 터질", "chest pain", "heart attack"),
    "dyspnea": ("숨이 안 쉬", "숨을 못 쉬", "숨쉬기 힘", "숨 쉬기 힘", "숨쉬기 너무 힘", "숨쉬기가 힘", "숨쉬기 어렵", "숨이 막", "숨이 차서 말", "숨이 잘 안 쉬",
                "숨이 가빠", "숨을 쉴 수 없", "호흡곤란", "호흡이 힘", "숨이 넘어", "can't breathe", "cannot breathe", "short of breath"),
    "consciousness": ("의식을 잃", "의식이 없", "정신을 잃", "정신을 못 차", "기절", "쓰러졌", "쓰러질 것", "쓰러질 거", "갑자기 쓰러", "의식이 흐", "정신이 혼미"),
    "bleeding": ("피가 많이", "피가 계속 나", "피가 멈추지 않", "출혈이 심", "피를 토", "토혈", "피를 많이", "하혈이 심", "피가 쏟", "heavy bleeding"),
    "paralysis": ("마비", "팔다리에 힘이 안", "한쪽 팔이 안", "한쪽 다리가 안", "말이 어눌", "말이 꼬", "발음이 이상", "혀가 꼬", "입이 돌아", "얼굴이 한쪽", "한쪽이 저리"),
    "help": ("살려 주", "살려줘", "살려 달", "도와주세요", "도와 주세요", "응급 상황", "응급이", "119 불러", "119를 불러", "119에 전화", "119 신고", "구급차", "help me", "emergency"),
}
# 구절 자체가 부정형(또는 부정어를 품은)인 위험 표현: 뒤에 부정 표지가 와도 위험이 부정되지 않는다("살고 싶지 않아요", "숨이 안 쉬어져요").
_RAW_NEGATIVE = frozenset({"살고 싶지 않", "더 이상 살 수 없", "살 이유가 없", "살 의미가 없", "숨이 안 쉬", "숨을 못 쉬", "숨이 잘 안 쉬", "숨을 쉴 수 없", "의식이 없",
                           "피가 멈추지 않", "팔다리에 힘이 안", "한쪽 팔이 안", "한쪽 다리가 안", "can't breathe", "cannot breathe"})
_BOUNDARY = r"(?<![0-9A-Za-z가-힣])"
_CLAUSE_END = re.compile(r"[.!?,;\n]|지만|는데|그리고|그런데|근데|하지만|그러나|\bbut\b|\band\b")
_NEG_KO = re.compile(r"없|않|아니|아닙|못\s*(?:했|한|하|느)|안\s*(?:들|드|하|해|나|아)")
_NEG_EN_BEFORE = re.compile(r"\b(?:no|not|never|without|don'?t|do not|didn'?t|haven'?t|denies|deny)\b")
_THIRD = re.compile(r"어머니|아버지|엄마|아빠|부모|가족|형제|형이|누나|언니|오빠|동생|할머니|할아버지|친구|이웃|아내|남편|아들|딸|아이가|옆 사람|옆사람")
_HISTORY = re.compile(r"년 전|개월 전|달 전|전에는|예전|옛날|어릴 때|어릴 적|젊을 때|젊을 적|[가-힣]*적부터|때부터|[았었였]었")   # 과거 병력 이야기의 표지
_NOW = re.compile(r"지금|방금|아까|갑자기|오늘|현재|지금도|계속")                                                  # 현재 증상이면 병력 표지가 있어도 정지한다
_SELF = re.compile(r"(?<![가-힣])(?:제가|저는|저도|내가|나는|나도|제|저|전|난)(?![가-힣])")


def _provisional_raw() -> bytes:
    rules = [{"id": f"prov-{k}", "description": f"임시 기본 목록(임상 검토 전): {k}", "match": {"any_of": [{"contains": p} for p in ph]}}
             for k, ph in _PROVISIONAL.items()]
    return json.dumps({"version": PROVISIONAL_VERSION, "rules": rules}, ensure_ascii=False).encode("utf-8")


def _phrase_regex(phrase: str):
    words = _norm(phrase).casefold().split()
    return re.compile(_BOUNDARY + r"\s*".join(re.escape(w) for w in words))


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """구절 [start, end)를 품은 절의 시작과 끝(문장 부호·접속 표현 기준)."""
    left = 0
    for m in re.finditer(r"[.!?,;\n]|지만|는데|그리고|그런데|근데|하지만|그러나", text[:start]):
        left = m.end()
    m = _CLAUSE_END.search(text, end)
    return left, (m.start() if m else len(text))


def phrase_context(text: str, start: int, end: int, phrase: str) -> str:
    """구절이 걸린 자리의 문맥 판정: 'hit'(위험), 'negated'(같은 절 뒤 부정), 'third_party'(가족·제3자 이야기)."""
    left, right = _clause_bounds(text, start, end)
    ascii_phrase = phrase.isascii()
    if ascii_phrase:
        if _NEG_EN_BEFORE.search(text[max(left, start - 25):start]) and phrase not in _RAW_NEGATIVE:
            return "negated"
    elif phrase not in _RAW_NEGATIVE and _NEG_KO.search(text[end:min(right, end + 30)]):
        return "negated"
    before = text[left:start]
    if _THIRD.search(before) and not _SELF.search(before):
        return "third_party"
    clause = text[left:right]
    if _HISTORY.search(clause) and not _NOW.search(clause):
        return "history"            # "몇 년 전에 한 번 쓰러졌었어요" 같은 병력 이야기: 정지하지 않고 센다(history_hits)
    return "hit"


class RuleRiskSource:
    """`RiskSource` 구현. 파일을 한 번 읽어 고정한다(바꾸려면 다시 시작)."""

    utterance_only = True      # 발화 원문만 본다: 모델 추출과 무관하게 모든 턴에 적용할 수 있다(D88 U1)

    def __init__(self, raw: bytes, *, provisional: bool = False) -> None:
        self.provisional = provisional          # 임상 검토 전 임시 목록이면 True(/readyz에 드러난다)
        self.version, self.rules = parse_rules(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self._lock = threading.Lock()
        self.hits: dict[str, int] = {}
        self.context_hits: dict[str, int] = {}       # 부정·제3자 이야기라 정지하지 않은 적중 건수(negated_hits, third_party_hits)

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
        for r in self.rules:
            for c in r.contains:                              # 구절 안의 공백은 있어도 없어도 된다. 어절을 가로질러 합쳐 맞추지 않는다(손목을 그냥 != 손목을 긋)
                for m in _phrase_regex(c).finditer(folded):
                    ctx = phrase_context(folded, m.start(), m.end(), c)
                    if ctx == "hit":
                        return r.id
                    with self._lock:
                        key = {"negated": "negated_hits", "third_party": "third_party_hits"}.get(ctx, "history_hits")
                        self.context_hits[key] = self.context_hits.get(key, 0) + 1
            if any(rx.search(text) for rx in r.regex):
                return r.id
        return None

    def screen(self, utterance: str, extra_phrases=()) -> Optional[Mapping]:
        """모델 추출 전에 발화 원문만 본다(D88 U1). `extra_phrases`는 문항 세트에 저작된 risk_phrases(U2)."""
        rid = self.match(utterance or "")
        if rid is None:
            folded = _norm(utterance or "").casefold()
            for ph in extra_phrases:
                if isinstance(ph, str) and ph.strip():
                    for m in _phrase_regex(ph).finditer(folded):
                        if phrase_context(folded, m.start(), m.end(), ph) == "hit":
                            rid = "bank-phrase"
                            break
                if rid:
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
                "hits_by_rule": hits, **{"negated_hits": 0, "third_party_hits": 0, "history_hits": 0}, **dict(self.context_hits)}


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
