"""서비스 안의 오류. 경계 호출 오류(BoundaryError)와 구분한다."""


class SurveyServiceError(Exception):
    pass


class StoreError(SurveyServiceError):
    """저장소를 쓸 수 없다(DB 오류 등). 값(발화 등)은 메시지에 담지 않는다."""


class StoreEffectError(SurveyServiceError):
    """저장 결정을 기록에 반영할 수 없다(없는 슬롯, 맞지 않는 확인 대기 ID 등). 커밋 전체를 거부한다."""


class TurnIdConflict(SurveyServiceError):
    """같은 (세션, 턴 ID)에 다른 입력이 왔다."""


class TurnInProgress(SurveyServiceError):
    """같은 (세션, 턴 ID)가 아직 처리 중이다. 새로 시작하지 않는다(FAULT_POLICY 2). API는 409 + Retry-After로 알린다."""


class ReleaseUnavailable(SurveyServiceError):
    """세션이 묶인 릴리스를 쓸 수 없다."""


class SessionNotFound(SurveyServiceError):
    pass


class SessionEnded(SurveyServiceError):
    pass


class InferenceLoadError(SurveyServiceError):
    """모델을 적재할 수 없다. code는 거부 사유(상태·런북에 쓴다)."""
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable      # 일시적이라 같은 릴리스를 나중에 다시 시도할 수 있는 실패(추론 서버가 아직 뜨는 중 등)


class TurnInputError(Exception):
    """응답·추출 입력을 조립할 수 없다(표·문항 세트에 없음). code는 값 없는 이름."""
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StaffCallRejected(SurveyServiceError):
    """재알림·실패 기록 거절. kind: NO_OPEN_ALERT(409), OUTCOME_MISMATCH(409), INTERVAL(429), RETRY_LIMIT(429)."""

    def __init__(self, kind: str, retry_after_s: int = 0) -> None:
        super().__init__(kind)
        self.kind, self.retry_after_s = kind, retry_after_s


class DeliveryFailed(SurveyServiceError):
    """알림은 저장됐지만 닿은 전달 경로가 없다(SSE 연결 없음, 푸시 접수 없음)."""
