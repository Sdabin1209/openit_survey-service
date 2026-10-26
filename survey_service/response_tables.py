"""표 찾기: LoRA-B 입력(ResponseRequest)의 `topic`, `known`, `ask`, `previous`, `current`, `hold`, `next.text`를 만든다.

**미확정, 사용자 확인 필요.** 표 내용(`schema/data/response_tables.json`)은 학습 데이터셋 v2(`dataset_10_14`)의 B 입력을 표본에서 역으로 읽어
만든 초안이다(IO_SPEC 7장 미정). 이 모듈은 표를 읽어 문장을 조립할 뿐 판단하지 않는다. 표에 없는 경우는 지어내지 않고 `None`/폴백을 쓰며
호출자가 센다(`stats`).

저장 값의 표시는 모델 키(`Q7_1[soju].amount`)로 한다. 칸 키에서 대괄호(종류)를 뗀 것이 표의 칸 이름이다.
"""
from __future__ import annotations

import fnmatch
import re
import string
from typing import Any, Iterable, Mapping, Optional

# ---------------------------------------------------------------- 키 다루기


def slot_name(key: str) -> str:
    """`Q7_1[soju].amount` -> `Q7_1.amount`."""
    return re.sub(r"\[[^\]]*\]", "", key)


def item_of(key: str) -> str:
    """`Q7_1[soju].amount` -> `Q7_1`."""
    return key.rsplit(".", 1)[0].split("[")[0]


def repeat_key_of(key: str) -> str:
    m = re.search(r"\[([^\]]*)\]", key)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 조사

_DIGIT_BATCHIM = {"0": True, "1": True, "2": False, "3": True, "4": False, "5": False, "6": True, "7": True, "8": True, "9": False}


def _has_batchim(text: str) -> bool:
    """마지막 글자에 받침이 있는가. 괄호 설명은 건너뛰고 숫자는 우리말 읽기(일·이·삼…)로 본다."""
    t = re.sub(r"\([^)]*\)\s*$", "", text.rstrip())
    t = t.rstrip(" )")
    if not t:
        return False
    ch = t[-1]
    if ch.isdigit():
        return _DIGIT_BATCHIM[ch]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return False


def josa(word: str, pair: str) -> str:
    """`josa("와인", "을/를")` -> `와인을`."""
    with_b, without_b = pair.split("/")
    if pair == "으로/로":
        last = re.sub(r"\([^)]*\)\s*$", "", word.rstrip()).rstrip(" )")
        batch = _has_batchim(word) and not (last and "가" <= last[-1] <= "힣" and (ord(last[-1]) - 0xAC00) % 28 == 8)
        return word + (with_b if batch else without_b)
    return word + (with_b if _has_batchim(word) else without_b)


class _Fmt(string.Formatter):
    """`{bev:을/를}`처럼 조사를 붙이는 형식 지정."""

    def format_field(self, value, spec):
        if "/" in spec:
            return josa(str(value), spec)
        return super().format_field(value, spec)


_FMT = _Fmt()


def _num(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


# ---------------------------------------------------------------- 표


class ResponseTables:
    """표(JSON)를 읽어 B 입력의 문장 필드를 조립한다."""

    def __init__(self, data: Mapping) -> None:
        self.data = data
        self.status: str = data["table_status"]
        self.questions: dict = data["questions"]
        self.bev: dict = data["beverages"]
        self.units: dict = data["units"]
        self.periods: dict = data["periods"]
        self.approx: str = data.get("approx_suffix", "쯤")
        self._phrases = list(data["value_phrases"].items())
        self._ask_rules = data["ask"]
        self.stats = {"ask_fallback": 0, "phrase_fallback": 0}

    # ---- 문항 단위
    def topic(self, item: str) -> str:
        return self._q(item)["topic"]

    def name(self, item: str) -> str:
        return self._q(item)["name"]

    def _q(self, item: str) -> dict:
        try:
            return self.questions[item]
        except KeyError:
            raise KeyError(f"표에 없는 문항: {item}") from None

    def hold(self, targets: Iterable[str]) -> str:
        """HOLD·SKIP의 `hold`: 대상 칸이 정확히 표의 부분 칸이면 그 이름, 아니면 문항 이름."""
        targets = list(targets)
        item = item_of(targets[0])
        q = self._q(item)
        by = q.get("hold_by_targets") or {}
        names = {slot_name(t) for t in targets}
        for slot, label in by.items():
            if names == {slot}:
                return label
        return q["name"]

    # ---- 값 문장
    def _spec(self, slot: str) -> Optional[Mapping]:
        for pat, spec in self._phrases:
            if fnmatch.fnmatchcase(slot, pat):
                return spec
        return None

    def _approx(self, text: str, precision: Optional[str]) -> str:
        return text + self.approx if precision == "approximate" else text

    def _minutes(self, n: Any) -> str:
        n = int(n) if float(n).is_integer() else n
        h, m = divmod(int(n), 60) if isinstance(n, int) else (0, n)
        parts = []
        if h:
            parts.append(f"{h}시간")
        if m or not h:
            parts.append(f"{_num(m)}분")
        return " ".join(parts)

    def phrases(self, items: list[Mapping], lookup: Mapping[str, Any]) -> list[tuple[str, str]]:
        """`items`(키, 값, precision)의 문장들 [(문항, 문장)]. 같은 말의 주기·단위 칸은 짝 칸의 문장에 합친다.

        `lookup`: 같은 뜻의 값을 찾을 곳(키 -> 값)으로, 이번 말에서 정해진 값을 먼저 쓰고 없으면 저장된 값(state)을 쓴다."""
        by_key = {i["key"]: i for i in items}
        merged: set[str] = set()
        out: list[tuple[str, str]] = []
        for it in items:
            key, val, prec = it["key"], it["value"], it.get("precision")
            if key in merged:
                continue
            slot = slot_name(key)
            spec = self._spec(slot)
            if spec is None:
                self.stats["phrase_fallback"] += 1
                out.append((item_of(key), f"{slot.split('.')[-1]} {_num(val)}"))
                continue
            kind = spec["kind"]
            if kind == "bool":
                text = spec["true" if val else "false"]
            elif kind == "enum":
                text = spec["map"].get(str(val))
                if text is None:
                    self.stats["phrase_fallback"] += 1
                    text = str(val)
            elif kind == "number":
                text = self._approx(_special(spec, val) or _FMT.format(spec["template"], n=_num(val)), prec)
            elif kind == "minutes":
                text = self._approx(_FMT.format(spec["template"], d=self._minutes(val)), prec)
            elif kind == "frequency":
                unit_key = _with_repeat(spec["unit_slot"], key)
                unit = by_key[unit_key]["value"] if unit_key in by_key else lookup.get(unit_key)
                if unit_key in by_key:
                    merged.add(unit_key)
                period = self.periods.get(unit) if unit is not None else None
                text = self._approx(_FMT.format(spec["template" if period else "template_no_unit"], period=period or "", n=_num(val)), prec)
            elif kind == "unit_only":
                text = _FMT.format(spec["template"], period=self.periods.get(val, str(val)))
            elif kind == "amount":
                unit_key = _with_repeat(spec["unit_slot"], key)
                bev = self.bev.get(repeat_key_of(key), repeat_key_of(key))
                unit = by_key[unit_key]["value"] if unit_key in by_key else lookup.get(unit_key)
                if unit_key in by_key:
                    merged.add(unit_key)
                if unit is None:
                    text = self._approx(_FMT.format(spec["template_no_unit"], bev=bev, n=_num(val)), prec)
                else:
                    text = self._approx(_FMT.format(spec["template"], bev=bev, n=_num(val), unit=self.units.get(unit, unit)), prec)
            elif kind == "amount_unit":      # 단위 칸 홀로: 짝 양이 이번 말에 없을 때
                bev = self.bev.get(repeat_key_of(key), repeat_key_of(key))
                text = _FMT.format(spec["template"], bev=bev, unit=self.units.get(val, val))
            else:
                raise ValueError(f"알 수 없는 표 종류: {kind}")
            out.append((item_of(key), text))
        return out

    def known(self, saved: list[Mapping], lookup: Mapping[str, Any]) -> str:
        """이번 말에서 저장된 값들의 문장. 첫 문항은 이름 없이, 다른 문항은 `이름: 문장`으로. 문항 안은 `, `로 잇는다."""
        groups: list[tuple[str, list[str]]] = []
        for item, text in self.phrases(saved, lookup):
            if groups and groups[-1][0] == item:
                groups[-1][1].append(text)
            elif any(g[0] == item for g in groups):
                next(g for g in groups if g[0] == item)[1].append(text)
            else:
                groups.append((item, [text]))
        parts = []
        for n, (item, texts) in enumerate(groups):
            body = ", ".join(texts)
            parts.append(body if n == 0 else f"{self.name(item)}: {body}")
        return ", ".join(parts)

    def value_text(self, items: list[Mapping], lookup: Mapping[str, Any], prefix: str = "") -> str:
        """previous/current용: 문항 구분 없이 문장들을 `, `로 잇는다."""
        text = ", ".join(t for _, t in self.phrases(items, lookup))
        return prefix + text if text else text

    # ---- ask
    def ask(self, reason: str, targets: list[str], values: list[Mapping], state: Mapping[str, Any]) -> Optional[str]:
        """REASK·CONFIRM의 `ask`. 표에 규칙이 없으면 None(호출자가 폴백)."""
        names = {slot_name(t) for t in targets}
        ctx = self._ctx(targets, values, state)
        for rule in self._ask_rules:
            if rule["reason"] != reason:
                continue
            slots = rule.get("slots")
            if slots is not None and not any(fnmatch.fnmatchcase(n, p) for n in names for p in slots):
                continue
            try:
                return _FMT.format(rule["template"], **ctx)
            except KeyError:
                continue          # 이 규칙이 필요로 하는 값이 없다 -> 다음 규칙
        return None

    def ask_or_topic(self, reason: str, targets: list[str], values: list[Mapping], state: Mapping[str, Any], item: str) -> str:
        text = self.ask(reason, targets, values, state)
        if text is None:
            self.stats["ask_fallback"] += 1
            return self.topic(item)
        return text

    def _ctx(self, targets: list[str], values: list[Mapping], state: Mapping[str, Any]) -> dict:
        ctx: dict[str, Any] = {}
        t0 = targets[0] if targets else None
        if t0:
            item = item_of(t0)
            ctx["name"] = self.questions[item]["name"] if item in self.questions else ""
            rk = repeat_key_of(t0)
            if rk:
                ctx["bev"] = self.bev.get(rk, rk)
        by = {v["key"]: v for v in values}
        if t0:
            amount_key = re.sub(r"\.(unit)$", ".amount", t0)
            unit_key = re.sub(r"\.(amount)$", ".unit", t0)
            a = by.get(amount_key, {}).get("value", state.get(amount_key))
            u = by.get(unit_key, {}).get("value", state.get(unit_key))
            if a is not None:
                ctx["amount"] = _num(a)
            if u is not None:
                ctx["unit"] = self.units.get(u, u)
        for v in values:
            if slot_name(v["key"]) == "Q7.frequency" and v["value"] is not None:
                ctx["freq"] = _num(v["value"])
        return ctx

    # ---- 남은 칸
    def remaining_text(self, item: str, filled: Mapping[str, Any]) -> Optional[str]:
        """일부 칸이 이미 저장된 문항으로 NEXT할 때(57행 문제) 남은 칸만 묻는 문장. 표에 칸 설명이 없는 문항이거나 남은 칸이 없으면 None."""
        q = self.questions.get(item)
        if not q or not q.get("slots"):
            return None
        rest = []
        for s in q["slots"]:
            if s["slot"] in filled:
                continue
            cond = s.get("only_if")
            if cond and filled.get(cond["slot"]) != cond["value"]:
                continue
            rest.append(s["desc"])
        if not rest:
            return None
        lead = q.get("remaining_lead", "")
        last = rest[-1]
        obj = last if last.endswith("지") else josa(last, "을/를")
        body = ", ".join(rest[:-1] + [obj])
        return f"{lead} {body} 말씀해 주세요.".strip()


def _special(spec: Mapping, val: Any) -> Optional[str]:
    sp = spec.get("special") or {}
    return sp.get(_num(val))


def _with_repeat(slot: str, like_key: str) -> str:
    """슬롯 이름에 `like_key`의 종류(대괄호)를 붙여 같은 종류의 키로 만든다."""
    rk = repeat_key_of(like_key)
    if not rk:
        return slot
    q, s = slot.rsplit(".", 1)
    return f"{q}[{rk}].{s}"
