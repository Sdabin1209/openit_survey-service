"""표 찾기: LoRA-B 입력(ResponseRequest)의 `topic`, `known`, `ask`, `previous`, `current`, `hold`를 만든다(`next`는 extraction.py가 붙인다).

**기준은 데이터셋 담당의 규칙이다.** 학습 데이터의 B 입력은 학습 서버 `~/WS/dataset/scripts/labels.py`(2026-10-14)의 함수들
(`topic`, `short_topic`, `value_text`, `_group_texts`, `ask_text`, `_known_texts`, `hold_text`, `previous_text`, `_conflict_rows`, `b_input`)로 만들어졌고,
그 README가 이것을 '서비스 표 찾기의 기준 구현'이라 부른다. 이 모듈은 그 조합 로직(그룹핑, 조사, 시간·년 표기, 술 단위, previous/current)을 옮긴 것이고
어휘와 고정 문장은 `schema/data/response_tables.json`에 있다. 문장은 원본과 글자 단위로 같아야 한다(tests/test_response_tables_rules.py).
**확정 상태: 데이터셋 담당 확인 대기**(표 승인 기록 미확인, IO_SPEC 7장 2번 미정). 원본에 없는 서비스 쪽 판단은 `remaining_text`(57행 문제)뿐이다.

판단은 하지 않는다. 표에 없어서 원본 규칙이 예외를 던질 입력(모르는 문항, 표에 없는 Q3 값 등)은 지어내지 않고 폴백 문장을 쓰며 `stats`로 센다.
원본의 일반 분기(else)가 만든 문장은 원본이 만드는 문장이므로 폴백이 아니다(`ask_generic`, `phrase_generic`으로만 센다).
저장 값의 키는 모델 키(`Q7_1[soju].amount`)다.
"""
from __future__ import annotations

import re

from ..wiring.errors import TurnInputError
import string
import threading
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


def parse_key(key: str) -> tuple[str, str, str]:
    """'Q7_1[soju].amount' -> ('Q7_1', 'soju', 'amount'), 'Q1.D01.diagnosed' -> ('Q1.D01', '', 'diagnosed')."""
    if "[" in key:
        item, rest = key.split("[", 1)
        rk, field = rest.split("].", 1)
        return item, rk, field
    item, field = key.rsplit(".", 1)
    return item, "", field


# ---------------------------------------------------------------- 조사·숫자 표기 (원본과 같은 규칙)


def josa(word: str, pair: str) -> str:
    """받침에 맞는 조사: josa('소주', '을/를') -> '소주를'. 괄호로 끝나면 괄호 앞 글자로 판단한다. 숫자는 끝소리 대략(0·1·3·6·7·8·l·m·n이면 받침)."""
    a, b = pair.split("/")
    base = re.sub(r"\(.*?\)$", "", word).strip() or word
    ch = base[-1]
    if "가" <= ch <= "힣":
        has = (ord(ch) - 0xAC00) % 28 != 0
    else:
        has = ch in "0136789lmn"
    return word + (a if has else b)


def n_ko(x: Any) -> str:
    """숫자 표기: 2.0 -> '2', 0.5 -> '0.5'."""
    return str(int(x)) if float(x).is_integer() else str(x)


def minutes_text(m: Any, t: Mapping) -> str:
    m = int(round(m))
    if m >= 60 and m % 60:
        return t["hours_minutes"].format(h=m // 60, m=m % 60)
    if m >= 60:
        return t["hours"].format(h=m // 60)
    return t["minutes"].format(m=m)


class _Fmt(string.Formatter):
    """`{name:을/를}`처럼 조사를 붙이는 형식 지정."""

    def format_field(self, value, spec):
        if "/" in spec:
            return josa(str(value), spec)
        return super().format_field(value, spec)


_FMT = _Fmt()


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float))          # 원본과 같다(bool도 int라 포함)


# ---------------------------------------------------------------- 표


class ResponseTables:
    """표(JSON)를 읽어 B 입력의 문장 필드를 조립한다."""

    def __init__(self, data: Mapping) -> None:
        self.data = data
        self.status: str = data["table_status"]
        self.diseases: dict = data["diseases"]
        self.topic_templates: dict = data["topic_templates"]
        self.short_templates: dict = data["short_topic_templates"]
        self.topics: dict = data["topics"]
        self.shorts: dict = data["short_topics"]
        self.field_short: dict = data["field_short"]
        self.bev: dict = data["beverages"]
        self.units: dict = data["drink_units"]
        self.periods: dict = data["periods"]
        self.use_pattern: dict = data["use_pattern"]
        self.approx_suffix: str = data["approx_suffix"]
        self.t: dict = data["texts"]
        self.remaining: dict = data.get("remaining", {})
        self._lock = threading.Lock()
        self.stats = {"ask_fallback": 0, "phrase_fallback": 0, "ask_generic": 0, "phrase_generic": 0}

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    # ---- 문항 이름
    def _disease_item(self, item: str, templates: Mapping) -> Optional[str]:
        for prefix, tpl in templates.items():
            if item.startswith(prefix):
                return _FMT.format(tpl, disease=self.diseases[item])
        return None

    def topic(self, item: str) -> str:
        """B 입력 'topic': 지금 다루는 문항이 무엇인지 사람 말로. 표에 없는 문항이면 KeyError."""
        text = self._disease_item(item, self.topic_templates)
        return text if text is not None else self.topics[item]

    def short_topic(self, item: str) -> str:
        """보류·거절 때 '그 부분'을 가리키는 짧은 이름."""
        text = self._disease_item(item, self.short_templates)
        return text if text is not None else self.shorts[item]

    name = short_topic

    # ---- 값 문장
    def value_text(self, item: str, field: str, value: Any, repeat_key: str = "", precision: str = "exact") -> Optional[str]:
        """칸 하나의 값을 상담원이 되짚을 수 있는 사람 말로. 값이 없으면 None."""
        if value is None:
            return None
        t = self.t
        approx = self.approx_suffix if precision == "approximate" else ""
        q5 = item == "Q5_1"
        if field in ("diagnosed", "on_medication", "family_history"):
            return t[field]["true" if value else "false"]
        if item == "Q3":
            return t["q3"][value]
        if item in ("Q4", "Q5", "Q6") and field == "answer":
            if item == "Q4":
                return t["q4_answer"]["true" if value else "false"]
            return t["tried_answer"]["true" if value else "false"]
        if field == "status":
            now = t["status_now"]["Q5_1" if q5 else "default"]
            return now if value == "current" else t["status_former"]
        verb = t["smoke_verb"]["Q5_1" if q5 else "default"]
        if field == "total_years":
            return t["total_years"].format(verb=verb, years=self._years(value), approx=approx)
        if field == "daily_count":
            return t["daily_count"].format(n=n_ko(value), approx=approx)
        if field == "years_since_quit":
            return t["years_since_quit"].format(years=self._years(value), approx=approx)
        if field == "use_pattern":
            up = t["use_pattern"]
            if value in ("no", "daily"):
                return up[value]
            return up["other"].format(days=self.use_pattern.get(value, value).replace(up["strip_prefix"], ""))
        if field == "does_not_drink":
            return t["does_not_drink"]["true" if value else "false"]
        if item == "Q7" and field == "frequency":
            return t["q7_frequency"].format(n=n_ko(value), approx=approx) if _is_num(value) else str(value)
        if item == "Q7" and field == "unit":
            return t["q7_unit"].format(period=self.periods.get(value, value))
        if field == "amount":
            return f"{n_ko(value)}{approx}" if _is_num(value) else str(value)
        if field == "unit":
            return self.units.get(value, value)
        if field == "days":
            d = t["days"]
            return d["template"].format(week=d["Q10" if item == "Q10" else "default"], n=n_ko(value), approx=approx)
        if field == "duration_minutes":
            return t["duration"].format(d=minutes_text(value, t["minutes"]), approx=approx)
        self._count("phrase_generic")
        return t["other_value"].format(field=field, value=value)

    def _years(self, y: Any) -> str:
        return self.t["half_year"] if y == 0.5 else f"{n_ko(y)}년"

    def group_texts(self, rows: Iterable[tuple]) -> list[str]:
        """[(item, repeat_key, field, value, precision)] -> 사람 말 목록. 같은 문항의 횟수+기간, 술 종류+양+단위는 한 덩어리로 묶는다."""
        t = self.t
        out: list[str] = []
        drinks: dict = {}
        q7: dict = {}
        for item, rk, field, value, prec in rows:
            if item in ("Q7_1", "Q7_2") and rk:
                drinks.setdefault((item, rk), {})[field] = (value, prec)
                continue
            if item == "Q7" and field in ("frequency", "unit"):
                q7[field] = (value, prec)
                continue
            text = self._safe_value_text(item, field, value, rk, prec)
            if text:
                out.append(text)
        if q7:
            f = q7.get("frequency", (None, "exact"))
            u = q7.get("unit", (None, "exact"))
            if f[0] is not None and u[0] is not None:
                out.append(t["q7_group"].format(period=self.periods.get(u[0], u[0]), freq=self.value_text("Q7", "frequency", f[0], "", f[1])))
            elif f[0] is not None:
                out.append(t["q7_group_no_unit"].format(freq=self.value_text("Q7", "frequency", f[0], "", f[1])))
            elif u[0] is not None:
                out.append(self.value_text("Q7", "unit", u[0]))
        for (item, rk), d in drinks.items():
            name = self.bev.get(rk, rk)
            amt, unit = d.get("amount", (None, "exact")), d.get("unit", (None, "exact"))
            amt_t = self.value_text(item, "amount", amt[0], rk, amt[1]) if amt[0] is not None else None
            unit_t = self.units.get(unit[0], unit[0]) if unit[0] is not None else None
            if amt_t and unit_t:
                approx = amt_t.endswith(self.approx_suffix)        # 어림 표시('쯤')는 단위 뒤에 붙인다: '맥주 1565cc쯤', '소주 2병쯤'
                out.append(t["drink_group"].format(name=name, amount=amt_t.rstrip(self.approx_suffix), unit=unit_t, approx=self.approx_suffix if approx else ""))
            elif unit_t:
                out.append(t["drink_unit_only"].format(name=name, unit=unit_t))
            elif amt_t:
                out.append(t["drink_amount_only"].format(name=name, amount=amt_t))
            else:
                out.append(name)
        return out

    def _safe_value_text(self, item, field, value, rk, prec):
        """원본 value_text는 표에 없는 Q3 값에서 예외를 던진다. 서비스는 턴을 죽이지 않고 값 이름 문장으로 대신하고 센다."""
        try:
            return self.value_text(item, field, value, rk, prec)
        except (KeyError, TypeError, ValueError):
            self._count("phrase_fallback")
            return self.t["other_value"].format(field=field, value=value)

    # ---- ask
    def _drink_ctx(self, rows: Mapping, item: str, rk: str) -> tuple[str, Optional[str]]:
        name = self.bev.get(rk, rk) if rk else self.t["drink_default_name"]
        unit = rows.get((item, rk, "unit"))
        return name, (self.units.get(unit, unit) if unit else None)

    def ask_text(self, reason: Optional[str], targets: Iterable[str], rows: Mapping) -> str:
        """REASK·CONFIRM에서 상담원이 무엇을 물어야 하는지 사람 말로.
        rows: {(item, repeat_key, field): value} 이번 턴 값과 저장 값."""
        t = self.t
        parts: list[str] = []
        for key in targets:
            item, rk, field = parse_key(key)
            fs = self.field_short.get(field, field)
            if reason == "MISSING":
                parts.append(self._ask_missing(item, rk, field, rows))
            elif reason in ("VAGUE_QUANTITY", "MISSING_NUMBER"):
                parts.append(self._ask_number(item, rk, field, rows))
            elif reason == "OUT_OF_RANGE":
                fixed = t["ask_out_of_range"].get(field)
                if fixed is None:
                    self._count("ask_generic")
                parts.append(fixed if fixed is not None else _FMT.format(t["ask_field_again"], field=fs))
            elif reason == "COUNT_EXCEEDS_DAYS":
                parts.append(t["ask_count_exceeds_days"])
            elif reason == "RANGE_SPANS_OPTIONS":
                sp = t["ask_range_spans"]
                parts.append(sp.get(field, sp["default"]))
            elif reason == "UNKNOWN_UNIT":
                name, _ = self._drink_ctx(rows, item, rk)
                parts.append(_FMT.format(t["ask_unknown_unit"], name=name))
            elif reason == "OTHER_BEVERAGE":
                parts.append(t["ask_other_beverage"])
            elif reason == "BEVERAGE_MISSING":
                parts.append(t["ask_beverage_missing"])
            elif reason == "INVALID_OPTION":
                parts.append(self._ask_option(item, field, rows))
            elif reason == "CONFLICT_WITH_PREVIOUS":
                parts.append(t["ask_conflict_with_previous"])
            elif reason == "CONFLICT_MAX_BELOW_USUAL":
                parts.append(t["ask_conflict_max_below_usual"])
            elif reason == "CONFLICT_DOES_NOT_DRINK":
                parts.append(t["ask_conflict_does_not_drink"])
            else:
                self._count("ask_generic")
                parts.append(_FMT.format(t["ask_field_again"], field=fs))
        return ", ".join(dict.fromkeys(parts))            # 같은 문장이 반복되면 하나로

    def _ask_missing(self, item: str, rk: str, field: str, rows: Mapping) -> str:
        m = self.t["ask_missing"]
        q5 = "Q5_1" if item == "Q5_1" else "default"
        if field in ("diagnosed", "on_medication"):
            return _FMT.format(m[field], disease=self.diseases[item])
        if field in ("status", "total_years", "daily_count"):
            return m[field][q5]
        if field == "years_since_quit":
            return m[field]
        if item == "Q7" and field == "unit":
            f = rows.get(("Q7", "", "frequency"))
            n = m["q7_unit_n"].format(n=n_ko(f)) if _is_num(f) else ""
            return m["q7_unit"].format(n=n)
        if item == "Q7" and field == "frequency":
            u = rows.get(("Q7", "", "unit"))
            return m["q7_frequency"].format(period=self.periods.get(u, m["q7_frequency_default_period"]))
        if item in ("Q7_1", "Q7_2"):
            name, unit_ko = self._drink_ctx(rows, item, rk)
            when = m["drink_when"][item]
            if field == "amount":
                if unit_ko and unit_ko != "cc":
                    return _FMT.format(m["drink_amount"], when=when, name=name, unit=unit_ko)
                return _FMT.format(m["drink_amount_no_unit"], when=when, name=name)
            if field == "unit":
                amt = rows.get((item, rk, "amount"))
                a = n_ko(amt) if _is_num(amt) else ""
                return _FMT.format(m["drink_unit"], name=name, amount=a) if a else _FMT.format(m["drink_unit_no_amount"], name=name)
        self._count("ask_generic")
        return f"{self.field_short.get(field, field)}"

    def _ask_option(self, item: str, field: str, rows: Mapping) -> str:
        """INVALID_OPTION(선택지에 없는 답): 문진표 선택지를 사람 말로 보여 주며 다시 묻는다."""
        o = self.t["ask_option"]
        if item == "Q3":
            return o["q3"]
        if field == "status" or (item == "Q7" and field == "unit"):
            return self._ask_missing(item, "", field, rows)
        if field == "use_pattern":
            return o["use_pattern"]
        self._count("ask_generic")
        return _FMT.format(o["default"], field=self.field_short.get(field, o["default_field"]))

    def _ask_number(self, item: str, rk: str, field: str, rows: Mapping) -> str:
        n = self.t["ask_number"]
        if field == "days":
            return n["days"] if item != "Q10" else n["days_Q10"]
        if field in ("duration_minutes", "frequency", "daily_count", "use_pattern"):
            return n[field]
        if field == "amount":
            name, unit_ko = self._drink_ctx(rows, item, rk)
            return _FMT.format(n["amount"], name=name, unit=unit_ko) if unit_ko else _FMT.format(n["amount_no_unit"], name=name)
        self._count("ask_generic")
        return _FMT.format(n["default"], field=self.field_short.get(field, field))

    # ---- known / hold / previous / current
    def known_texts(self, rows: list[tuple], focus: str) -> list[str]:
        """known 문장 목록. 한 턴에 여러 문항 값이 함께 정해지면 지금 문항(focus)이 아닌 값 앞에 그 문항의 짧은 이름을 붙인다."""
        if all(r[0] == focus for r in rows):
            return self.group_texts(rows)
        out = self.group_texts([r for r in rows if r[0] == focus])
        for item in dict.fromkeys(r[0] for r in rows if r[0] != focus):
            texts = self.group_texts([r for r in rows if r[0] == item])
            if texts:
                out.append(self.t["known_other_item"].format(short=self.short_topic(item), texts=", ".join(texts)))
        return out

    def hold_text(self, targets: Iterable[str], asked_item: str) -> str:
        """보류·거절로 비워 두는 부분의 이름. 여러 칸 문항에서 한 칸만 비우면 '일반담배 중 피운 기간'처럼 칸까지 말한다."""
        targets = list(targets)
        items = list(dict.fromkeys(parse_key(t)[0] for t in targets)) or [asked_item]
        fields = list(dict.fromkeys(parse_key(t)[2] for t in targets))
        item = items[0]
        if len(items) == 1 and len(fields) == 1 and (item in ("Q4_1", "Q5_1") or item.startswith("Q1.")):
            fs = self.t["hold_status_q5"] if item == "Q5_1" and fields[0] == "status" else self.field_short.get(fields[0], fields[0])
            return self.t["hold_field"].format(short=self.short_topic(item), field=fs)
        return self.short_topic(item)

    def conflict_rows(self, a_out: Mapping, state: Optional[Mapping] = None) -> list[tuple]:
        rows = []
        for v in a_out["values"]:
            if v["status"] == "CONFLICT" and v["value"] is not None:
                item, rk, field = parse_key(v["key"])
                # '술 안 마심'과 음주량이 한 턴에 함께 나온 충돌: '안 마심'은 previous로 말하므로 current에서 뺀다
                if a_out.get("reason") == "CONFLICT_DOES_NOT_DRINK" and (item, field) == ("Q7", "does_not_drink"):
                    continue
                rows.append((item, rk, field, v["value"], v["precision"]))
        # 술은 단위가 CONFLICT가 아니어도 함께 말해야 뜻이 통한다
        conflict_drinks = {(r[0], r[1]) for r in rows if r[0] in ("Q7_1", "Q7_2")}
        for v in a_out["values"]:
            item, rk, field = parse_key(v["key"])
            if (item, rk) in conflict_drinks and field == "unit" and v["value"] is not None:
                rows.append((item, rk, field, v["value"], v["precision"]))
        if state and any(r[0] == "Q7" and r[2] == "frequency" for r in rows) and not any(r[0] == "Q7" and r[2] == "unit" for r in rows) \
                and (state.get("Q7.unit") or {}).get("value") is not None:
            rows.append(("Q7", "", "unit", state["Q7.unit"]["value"], "exact"))
        return rows

    def previous_text(self, a_out: Mapping, state: Mapping) -> str:
        """CONFIRM의 이전 값: 충돌한 칸의 저장 값. 평소량보다 적은 최대량이면 평소량, 안 마신다고 했으면 그 답."""
        reason = a_out.get("reason")
        rows: list[tuple] = []
        if reason == "CONFLICT_DOES_NOT_DRINK":
            return self.t["previous_does_not_drink"]
        if reason == "CONFLICT_MAX_BELOW_USUAL":
            for k, s in state.items():
                it, rk, fd = parse_key(k)
                if it == "Q7_1" and s.get("value") is not None:
                    rows.append((it, rk, fd, s["value"], "exact"))
            text = ", ".join(self.group_texts(rows))
            return self.t["previous_usual"].format(text=text) if text else ""
        keys = set(a_out.get("targets", []))
        if "Q7.frequency" in keys and (state.get("Q7.unit") or {}).get("value") is not None:
            keys.add("Q7.unit")
        for k, s in state.items():
            if k in keys and s.get("value") is not None:
                it, rk, fd = parse_key(k)
                rows.append((it, rk, fd, s["value"], "exact"))
                if it in ("Q7_1", "Q7_2"):
                    u = state.get(f"{it}[{rk}].unit")
                    if u and fd != "unit" and u.get("value") is not None:
                        rows.append((it, rk, "unit", u["value"], "exact"))
        return ", ".join(self.group_texts(rows))

    # ---- B 입력 (next 제외)
    def b_fields(self, a_out: Mapping, asked_item: str, utterance: str, state: Mapping) -> dict:
        """LoRA-B 입력의 `next`를 뺀 필드. a_out: LoRA-A 출력(ActionExtraction), asked_item: A 입력 question_id, state: A 입력 state."""
        action = a_out["action"]
        rows_now: dict = {}
        known_rows: list[tuple] = []
        for v in a_out["values"]:
            item, rk, field = parse_key(v["key"])
            rows_now[(item, rk, field)] = v["value"]
            if v["status"] in ("ANSWERED", "CORRECTION") and v["value"] is not None:
                known_rows.append((item, rk, field, v["value"], v["precision"]))
        rows_all: dict = {}                    # 같은 문항의 저장 값도 ask 문장에 쓸 수 있게 합친다(예: 술 종류는 앞 턴에, 양은 이번 턴에)
        for k, s in state.items():
            it, rk, fd = parse_key(k)
            if s.get("value") is not None:
                rows_all[(it, rk, fd)] = s["value"]
        rows_all.update({k: v for k, v in rows_now.items() if v is not None})
        focus = parse_key(a_out["targets"][0])[0] if a_out.get("targets") else \
            (parse_key(a_out["values"][0]["key"])[0] if a_out["values"] else asked_item)
        b: dict = {"action": action}
        if a_out.get("reason"):
            b["reason"] = a_out["reason"]
        b["topic"] = self._topic_or_item(focus)
        b["user_utterance"] = utterance
        fields_now = {(r[0], r[2]) for r in known_rows}        # Q7 횟수만 이번에 말했으면(정정 등) 저장된 기준 기간을 붙인다
        if ("Q7", "frequency") in fields_now and ("Q7", "unit") not in fields_now and (state.get("Q7.unit") or {}).get("value") is not None:
            known_rows.append(("Q7", "", "unit", state["Q7.unit"]["value"], "exact"))
        b["known"] = "" if action == "CONFIRM" else ", ".join(self._known_safe(known_rows, focus))      # 확인 턴은 known을 비우고 previous·current로만 말한다
        if action in ("REASK", "CONFIRM"):
            b["ask"] = self.ask_or_topic(a_out.get("reason"), a_out.get("targets", []), rows_all, focus)
        if action == "CONFIRM":
            b["previous"] = self.previous_text(a_out, state)
            b["current"] = ", ".join(self.group_texts(self.conflict_rows(a_out, state)))
        if action in ("HOLD", "SKIP"):
            b["hold"] = self._hold_safe(a_out.get("targets", []), asked_item)
        if a_out.get("note"):
            b["note"] = a_out["note"]
        return b

    def _topic_or_item(self, item: str) -> str:
        try:
            return self.topic(item)
        except KeyError:
            self._count("phrase_fallback")
            return item

    def _known_safe(self, rows, focus):
        try:
            return self.known_texts(rows, focus)
        except KeyError:
            self._count("phrase_fallback")
            return []

    def _hold_safe(self, targets, asked_item):
        try:
            return self.hold_text(targets, asked_item)
        except KeyError:
            self._count("phrase_fallback")
            return asked_item

    def ask_or_topic(self, reason: Optional[str], targets: Iterable[str], rows: Mapping, focus: str) -> str:
        """원본 규칙으로 `ask`를 만든다. 원본 규칙이 예외를 던질 입력(표에 없는 문항 등)만 문항 topic으로 대신하고 센다."""
        try:
            return self.ask_text(reason, list(targets), rows)
        except (KeyError, TypeError, ValueError):
            self._count("ask_fallback")
            return self._topic_or_item(focus)

    # ---- 남은 칸 (원본에 없는 서비스 쪽 판단: 57행 문제)
    def remaining_text(self, item: str, filled: Mapping[str, Any]) -> Optional[str]:
        """일부 칸이 이미 저장된 문항으로 NEXT할 때(57행 문제) 남은 칸만 묻는 문장. 표에 칸 설명이 없는 문항이거나 남은 칸이 없으면 None.
        학습 분포 밖의 문장이다(원본 규칙에 없다)."""
        q = self.remaining.get(item)
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
        last = rest[-1]
        obj = last if last.endswith("지") else josa(last, "을/를")
        body = ", ".join(rest[:-1] + [obj])
        return f"{q.get('lead', '')} {body} 말씀해 주세요.".strip()


# ---------------------------------------------------------------- 응답 입력(표 찾기)

SAME_ACTIONS = ("REASK", "CONFIRM", "REDIRECT")


def sorted_deep(x: Any) -> Any:
    """모든 수준의 키를 정렬한다(B 입력의 학습 데이터 형태)."""
    if isinstance(x, dict):
        return {k: sorted_deep(x[k]) for k in sorted(x)}
    if isinstance(x, list):
        return [sorted_deep(v) for v in x]
    return x


def _next_text(item: str, bank: Any, tables: ResponseTables, filled: Mapping[str, Any], partial_ok: bool) -> str:
    text = bank.sentence(item)
    if text is None:
        raise TurnInputError("NO_QUESTION_SENTENCE")
    if partial_ok and any(slot_name(k).startswith(item + ".") for k in filled):
        rem = tables.remaining_text(item, {slot_name(k): v for k, v in filled.items()})
        if rem:
            return rem            # 57행 문제: 이미 저장된 칸은 다시 묻지 않고 남은 칸만 묻는다
    return text


def build_start_request(bank: Any, tables: ResponseTables, first_item: str) -> dict:
    """세션 시작: A를 거치지 않는 ResponseRequest(action=START)."""
    text = bank.sentence(first_item)
    if text is None:
        raise TurnInputError("NO_QUESTION_SENTENCE")
    return sorted_deep({"action": "START", "topic": tables.topic(first_item),
                        "next": {"type": "NEXT", "question_id": first_item, "text": text}})


def build_response_request(out: Mapping, extract_input: Mapping, doc_after: Mapping, bank: Any, tables: ResponseTables) -> dict:
    """ResponseRequest(START 외). 표 찾기만 한다. 문장 필드(topic, known, ask, previous, current, hold)는 데이터셋 담당의 규칙을 옮긴
    `ResponseTables.b_fields`가 만들고, 여기서는 `next`만 붙인다(문항 은행의 질문 문장, 57행 문제는 남은 칸 문장)."""
    nxt = out["next_item"]
    req: dict[str, Any] = tables.b_fields(out, extract_input["question_id"], extract_input["user_utterance"], extract_input["state"])
    if nxt == "END":
        req["next"] = {"type": "END"}
    elif out["action"] in SAME_ACTIONS:
        req["next"] = {"type": "SAME", "question_id": nxt, "text": _next_text(nxt, bank, tables, {}, False)}
    else:
        filled = {k: v[0] for k, v in _answers_by_key(doc_after["answer_records"]).items() if item_of(k) == nxt}
        req["next"] = {"type": "NEXT", "question_id": nxt, "text": _next_text(nxt, bank, tables, filled, True)}
    return sorted_deep(req)


def _slot_key(slot: Mapping) -> tuple:
    return (slot["question_id"], slot.get("repeat_key", ""), slot["slot_id"])


def _model_key(slot: Mapping) -> str:
    """SlotRef -> 모델 키(`Q7_1[soju].amount`). 왜 복제인가: 컴포넌트끼리 import 금지(DESIGN 2절, 한계 L2)."""
    rk = slot.get("repeat_key", "")
    return f"{slot['question_id']}[{rk}].{slot['slot_id']}" if rk else f"{slot['question_id']}.{slot['slot_id']}"


def _slot_ref(key: str) -> dict:
    """모델 키 -> SlotRef. 종류 괄호가 없으면 repeat_key는 빈 문자열(한계 L2: 복제)."""
    head, slot_id = key.rsplit(".", 1)
    item, _, rest = head.partition("[")
    return {"question_id": item, "repeat_key": rest[:-1] if rest else "", "slot_id": slot_id}


def _answers_by_key(records: list) -> dict:
    """저장된 값(answer)이 있는 칸: 모델 키 -> (값, precision)(한계 L2: 복제)."""
    out = {}
    for r in records:
        cv = r["current_value"]
        if isinstance(cv, dict) and "answer" in cv:
            out[_model_key(r["slot"])] = (cv["answer"], r.get("precision", "unspecified"))
    return out


