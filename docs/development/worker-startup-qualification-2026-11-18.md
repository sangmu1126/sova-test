# 워커 시작 계약 개발 기록

2026-11-18 · 개인 브랜치 `feature/migration-request-qualification`.

## 기준과 경계

- 시작 SHA: `bcf35e5dc87f2380ea31a664b9a72b9e8fd152b5`.
- Security 생산자: `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`.
- 상대 브랜치 관측: `e65f72a0c71f48b07e61a54c227251c7145cbc1d`. 문서 변경과 이후 볼륨·세션·flow-check 작업이 있었으나 이번 수정 경로와 겹치지 않았다.
- 소유 범위를 `service/config.py`, 기존 소유 `main.py`, 전용 단위·통합 시험·실행기·이 문서로 확장해 로컬 `SESSION-SCOPES.json`에 게시했다. 상대의 전체 계획이나 동의를 확인했다는 뜻은 아니다.
- 어댑터·원장·domain·SQL 스키마·클러스터·팀 저장소·다른 작업 트리·가상환경은 수정하지 않았다.

## 재현·수정

`WorkerConfig.from_env`에 lease 600·stall 700을 주면 생성되지만 `PostgresExecutionOwnership.duration(600)`은 거부했다. 재현 원문 `before.json`을 보존했다. 기존 설정의 상한 숫자만 바꾸는 대신 실제 제어 원장의 duration 검사를 서비스 설정에서 재사용했다. 서비스 최소 10초와 기본값 120초는 유지했다.

초 단위 설정은 제한된 ASCII 숫자 문자열로 검사한다. 원래 `int`가 받아들이던 밑줄·공백 등의 모호한 표기를 거부한다. 키·DSN 내용은 오류에 포함하지 않는다.

시작 경로를 읽으면서 폴링 간격의 0·NaN·무한대·stall 이상 값과 잘못된 health port도 시작 전에 거부하도록 보완했다. `run_worker`의 catalog·연결·Pod 관측·listener가 호출되기 전 검사한다. 실행 도중의 lease 정책이나 정지 증명 규칙은 변경하지 않았다.

## 검증 결과

- `9f520a0`: 설정과 원장 lease 검사 결합, polling·health 인자 fail-fast.
- `638ec32`: 신규 단위 26개, 실제 DB lease 3개, 기존 설정 회귀와 startup suite 연결.
- 전용 suite: **116 PASS, 0 FAIL, 0 ERROR, 0 SKIP**. 기존 83개 회귀 포함.
- 별도 단위 suite: **85 PASS, 0 FAIL, 0 ERROR, 0 SKIP**. 두 실행의 겹치는 시험을 합산하지 않는다.
- 실제 PostgreSQL에서 10·120·300초 발급의 DB 시각 범위를 확인하고 owner·valid_until 해제를 확인했다. 업무 효과는 실행하지 않았다.
- Ruff·공백 검사 통과. 전용 DB·네트워크 정리 확인.
- 측정 소스 43개가 `638ec32da0405ef356c788d0f9b38ab9a2ea65e0`의 파일과 일치했다. 시각 감시 262개 샘플을 통과했다. 문서 커밋은 시험 대상 코드를 변경하지 않는다.

## 검증 환경 문제와 해결

처음 사용한 보안 인수용 Python 환경에는 PyMySQL 등이 없었다. 전체 service deployment 단위 파일을 추가로 실행했을 때 **5개 실패·19개 건너뜀**이 발생했다. 이를 코드 통과로 처리하지 않고 `optional-catalog.xml/log`에 보존했다.

공유 가상환경에 패키지를 설치하지 않았다. 의존성이 이미 갖춰진 기존 검증 환경을 찾아 같은 후보 코드를 읽기 전용으로 재실행했다. 전체 85개가 통과했으며 `complete-unit.xml/log`와 환경 버전을 남겼다. bytecode·pytest cache 쓰기는 막았다. 최초 실패 기록을 삭제하지 않았다.

## 증거와 남은 범위

비공개 원문은 `local-dev-records/worker-startup-qualification-20261118/`에 있다. [공개 요약](../evidence/worker-startup-qualification-20261118.json)에 소스·원문 해시·환경·각 실행 결과를 연결한다. 실제 자격·secret·DSN은 공개하지 않는다.

실제 배포된 CLI 워커와 자격·네트워크·이미지·Pod 정지 증명의 운영 인수는 미완료다. 이번 결과는 시작 계약과 원장 범위의 정합성 증거이며 운영 배포·금융 보안 인증 통과로 표현하지 않는다.
