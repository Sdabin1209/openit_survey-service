"""서비스 안의 오류. 경계 호출 오류(BoundaryError)와 구분한다."""


class SurveyServiceError(Exception):
    pass


class StoreError(SurveyServiceError):
    """저장소를 쓸 수 없다(DB 오류 등). 값(발화 등)은 메시지에 담지 않는다."""


class StoreEffectError(SurveyServiceError):
    """저장 결정을 기록에 반영할 수 없다(없는 슬롯, 맞지 않는 확인 대기 ID 등). 커밋 전체를 거부한다."""


class TurnIdConflict(SurveyServiceError):
    """같은 (세션, 턴 ID)에 다른 입력이 왔다."""


class ReleaseUnavailable(SurveyServiceError):
    """세션이 묶인 릴리스를 쓸 수 없다."""
