"""조립 기본값: 실제 시계. 컴포넌트는 `time`을 직접 읽지 않고(ADR-0050) 시계를 인자로 받는다. 인자를 주지 않았을 때의 기본값이 여기 있고, wiring(App, db.open_stores)은 같은 실제 시계를 명시적으로 넘긴다.
왜 별도 파일인가: 컴포넌트가 wiring을 import할 수 없어서(구조 검사 2번), 기본값을 컴포넌트 밖의 이 진입점 모듈에 둔다. 컴포넌트 코드 안에는 `import time`이 없다(tests/test_structure.py가 강제한다).
"""
import time

REAL_WALL_CLOCK = time.time           # 벽시계(epoch 초): 저장 시각, 전송 시각처럼 기록에 남는 값
REAL_MONOTONIC = time.monotonic       # 단조 시계: 마감·예산·간격
REAL_SLEEP = time.sleep
