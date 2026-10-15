"""모델 런타임을 설정 가능한 HTTP 경계 뒤에 둔다(추론 컨테이너 분리 준비, D55).

서비스는 `SURVEY_INFERENCE_URL`이 있으면 모델을 직접 적재하지 않고 추론 서버(survey_service.inference_server)에 HTTP로 부른다.
이 프로토콜은 survey-service 영역 안의 내부 규약이다(이웃 계약이 아니다). 입력은 UtteranceParserInput / ResponseGeneratorInput,
출력은 ParserOutput / GeneratorOutput 그대로다.

  PUT  /v1/releases/{id}            이 릴리스를 적재해 둔다(멱등)
  POST /v1/releases/{id}/parse      본문 UtteranceParserInput -> ParserOutput
  POST /v1/releases/{id}/generate   본문 ResponseGeneratorInput -> GeneratorOutput
  GET  /healthz, /readyz            인증 없음
오류는 BoundaryError 본문과 상태 코드(HTTP.md 9절 표). 인증은 Bearer 토큰(환경 변수로만).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional

from .errors import SurveyServiceError


class RemoteInferenceError(SurveyServiceError):
    """추론 서버 호출 실패. code는 BoundaryError 코드(UNAVAILABLE 등)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _Http:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("base_url은 http:// 또는 https://로 시작해야 한다")
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Optional[Mapping] = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                rec = json.loads(e.read())
                code, msg = rec.get("code", "UNAVAILABLE"), rec.get("message", "")
            except ValueError:
                code, msg = ("UNAVAILABLE" if e.code >= 500 else "INVALID_RECORD"), ""
            raise RemoteInferenceError(code, f"추론 서버가 {e.code}를 돌려줬다 {msg}"[:200]) from None
        except (urllib.error.URLError, OSError, ValueError) as e:       # 연결 실패, 타임아웃, 깨진 응답
            raise RemoteInferenceError("UNAVAILABLE", f"추론 서버를 부를 수 없다 ({type(e).__name__})") from None


class RemoteModelSession:
    """Orchestrator가 아는 parse/generate. 추론 서버가 그 릴리스를 잊었으면(재시작) 한 번 다시 적재하고 재시도한다."""
    def __init__(self, http: _Http, release_id: str) -> None:
        self._http, self.release_id = http, release_id

    def _post(self, op: str, body: Mapping) -> dict:
        path = f"/v1/releases/{urllib.parse.quote(self.release_id, safe='')}"
        try:
            return self._http.call("POST", f"{path}/{op}", body)
        except RemoteInferenceError as e:
            if e.code != "NOT_FOUND":
                raise
        self._http.call("PUT", path)
        return self._http.call("POST", f"{path}/{op}", body)

    def parse(self, input: Mapping) -> dict:
        out = self._post("parse", input)
        if not isinstance(out, dict) or not isinstance(out.get("items"), list):
            raise RemoteInferenceError("INTEGRITY_ERROR", "ParserOutput 형식이 아니다")
        return out

    def generate(self, input: Mapping) -> dict:
        out = self._post("generate", input)
        if not isinstance(out, dict) or not isinstance(out.get("response_text"), str):
            raise RemoteInferenceError("INTEGRITY_ERROR", "GeneratorOutput 형식이 아니다")
        return out


class RemoteInference:
    """InferenceServer와 같은 load 인터페이스. 모델을 적재하는 대신 추론 서버에 릴리스 적재를 요청한다."""
    production_safe = True

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.backend = "remote"
        self._http = _Http(base_url, token, timeout)

    def load(self, manifest: Mapping, adapters, *, contract_version=None) -> RemoteModelSession:
        from .inference import InferenceLoadError
        rid = manifest["release_id"]
        try:
            self._http.call("PUT", f"/v1/releases/{urllib.parse.quote(rid, safe='')}")
        except RemoteInferenceError as e:
            raise InferenceLoadError("REMOTE_" + e.code, "추론 서버가 릴리스 적재를 거부했거나 부를 수 없다") from e
        return RemoteModelSession(self._http, rid)

    def ready(self) -> bool:
        try:
            req = urllib.request.Request(self._http.base + "/readyz")
            with self._http.opener.open(req, timeout=self._http.timeout) as r:
                return r.status == 200
        except Exception:
            return False
