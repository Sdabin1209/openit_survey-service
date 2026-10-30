"""컴포넌트를 부르기 전에 그 컴포넌트의 입력 스키마로 검증한다(DESIGN.md 5절, 한계 L5의 해소). 컴포넌트 안에는 같은 검사가 없다.

규칙: 호출마다 payload를 만들어 `types.validate("<입력 타입>", payload, static=...)`로 검증한 뒤 컴포넌트를 부른다. 위반은 SchemaViolation(ValueError)이다.
`static`은 오래 사는 문서(문항 세트, 문항 흐름, 응답 표)를 담은 속성이다: 느려서(문항 세트 한 번에 약 100ms, 측정) 같은 객체는 한 번만 검증한다.
이 모듈은 업무 규칙을 두지 않는다(연결만).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .. import types
from ..components import blank_details as _blank, dispatch_input as _dispatch, extract_request, extraction_check, reask_cap, response_request, turn_route, write_plan


def column_of(found: bool, status: Optional[str]) -> str:
    types.validate("TurnRouteQuery", {"input_kind": "UTTERANCE", "session_found": bool(found), "status": status})
    return turn_route.column_of(found, status)


def route(input_kind: str, found: bool, status: Optional[str]):
    types.validate("TurnRouteQuery", {"input_kind": input_kind, "session_found": bool(found), "status": status})
    return turn_route.route(input_kind, found, status)


def effective_reask_cap(max_consecutive: int, session_settings: Optional[Mapping]) -> int:
    types.validate("ReaskCapQuery", {"max_consecutive": max_consecutive, "session_settings": session_settings})
    return reask_cap.effective_reask_cap(max_consecutive, session_settings)


def build_extract_input(doc: Mapping, bank_view: Any, flow: Any, utterance: str) -> dict:
    types.validate("ExtractRequestSource", {"session": doc, "bank": bank_view.raw, "flow": flow.raw, "utterance": utterance}, static=("bank", "flow"))
    return extract_request.build_extract_input(doc, bank_view, flow, utterance)


def check_slots(output: Mapping, doc: Mapping) -> list:
    types.validate("ExtractionCheckQuery", {"output": output, "session": doc})
    return list(extraction_check.check_slots(output, doc["answer_records"]))


def check_current_item(output: Mapping, doc: Mapping, flow: Any) -> list:
    types.validate("ExtractionCheckQuery", {"output": output, "session": doc, "flow": flow.raw}, static=("flow",))
    return list(extraction_check.check_current_item(output, doc, flow))


def plan_writes(doc: Mapping, output: Mapping) -> dict:
    types.validate("WritePlanQuery", {"session": doc, "output": output})
    return write_plan.plan_writes(doc, output)


def build_start_request(bank_view: Any, tables: Any, first_item: str) -> dict:
    types.validate("ResponseRequestSource", {"kind": "START", "bank": bank_view.raw, "tables": tables.data, "first_item": first_item}, static=("bank", "tables"))
    return response_request.build_start_request(bank_view, tables, first_item)


def build_response_request(out: Mapping, extract_input: Mapping, doc_after: Mapping, bank_view: Any, tables: Any) -> dict:
    types.validate("ResponseRequestSource", {"kind": "TURN", "bank": bank_view.raw, "tables": tables.data, "extract_input": extract_input,
                                             "extract_output": out, "session_doc": doc_after}, static=("bank", "tables"))
    return response_request.build_response_request(out, extract_input, doc_after, bank_view, tables)


def dispatch_input(doc: Mapping) -> dict:
    types.validate("SessionDocument", doc)
    return _dispatch.dispatch_input(doc)


def blank_details(doc: Mapping) -> list:
    types.validate("SessionDocument", doc)
    return _blank.blank_details(doc)


def screen(source: Any, utterance: Optional[str], extra_phrases: tuple) -> Optional[Mapping]:
    """위험 선별. 규칙 파일은 시작할 때 읽어 검증했으므로(RuleRiskSource.from_file) 여기서는 호출 입력만 본다."""
    types.validate("RiskScreenQuery", {"utterance": utterance, "extra_phrases": list(extra_phrases)})
    return source.screen(utterance, extra_phrases)


def store_load(store: Any, session_id: str) -> dict:
    types.validate("StoreLoadRequest", {"session_id": session_id})
    return store.load(session_id)


def store_commit(store: Any, req: Mapping, *, turn_id: str, input_hash: str, **kw) -> dict:
    payload = dict(req, turn_id=turn_id, input_hash=input_hash)
    if kw.get("trace_envelope") is not None:
        payload["trace_envelope"] = kw["trace_envelope"]
    types.validate("StoreCommitRequest", payload)
    return store.commit(req, turn_id=turn_id, input_hash=input_hash, **kw)
