"""위험 감지 규칙 엔진(G01 입력원). 설정 `SURVEY_RISK_RULES_FILE`(JSON)을 읽어 환자 발화를 규칙과 맞춰 본다.

- 이 모듈은 임상 규칙을 담지 않는다. 규칙은 전부 운영이 파일로 준다. 기본 규칙 파일은 없다(설정이 없으면 비활성).
- 규칙 파일 형식: {"version": str, "rules": [{"id": str, "description": str, "match": {"any_of": [{"contains": str} | {"regex": str}]}}]}.
  모르는 키, 빈 목록, 중복 id, 잘못된 정규식은 시작을 거부한다(ConfigError).
- 맞춤: 발화와 `contains` 문구는 유니코드 NFC로 맞추고 대소문자를 구분하지 않는다. `regex`는 NFC 발화에 re.search(대소문자 구분).
  규칙은 파일 순서로 보고 처음 걸린 규칙 하나만 쓴다.
- 걸리면 `RiskSource` 포트 계약대로 StaffAlert(kind RISK)를 돌려준다. 이유(reasons)의 evidence에는 발화 원문이 아니라 `rule:<규칙 id>`만 넣는다.
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
EVIDENCE_PREFIX = "rule:"
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


class RuleRiskSource:
    """`RiskSource` 구현. 파일을 한 번 읽어 고정한다(바꾸려면 다시 시작)."""

    def __init__(self, raw: bytes) -> None:
        self.version, self.rules = parse_rules(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self._lock = threading.Lock()
        self.hits: dict[str, int] = {}

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
            if any(c in folded for c in r.contains) or any(rx.search(text) for rx in r.regex):
                return r.id
        return None

    def check(self, *, extract_input: Mapping, extract_output: Mapping, utterance: str) -> Optional[Mapping]:
        rid = self.match(utterance or "")
        if rid is None:
            return None
        with self._lock:
            self.hits[rid] = self.hits.get(rid, 0) + 1
        return {"kind": "RISK", "reasons": [{"kind": "RISK_SIGNAL", "evidence": [EVIDENCE_PREFIX + rid]}]}

    def describe(self) -> dict:
        with self._lock:
            hits = dict(self.hits)
        return {"active": True, "source": "RULES_FILE", "rule_count": len(self.rules), "version": self.version, "sha256": self.sha256,
                "hits_by_rule": hits}


def rule_id_of(alert: Mapping) -> Optional[str]:
    """StaffAlert에서 규칙 id를 꺼낸다(evidence의 `rule:<id>`). 없으면 None."""
    for r in alert.get("reasons") or []:
        for ev in r.get("evidence") or []:
            if isinstance(ev, str) and ev.startswith(EVIDENCE_PREFIX):
                return ev[len(EVIDENCE_PREFIX):]
    return None
