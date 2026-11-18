# 워커 프로세스 인계·강제 종료 시험 개발 기록

2026-11-18 · 개인 브랜치 `feature/migration-request-qualification`.

## 기준·작업 경계

- 시작 HEAD: `b0360f09ec1ec9ab22b8fb10d3377d6d9403c47b`.
- 고정 Security 생산자: `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`.
- 관측한 다른 세션: `96e701509d7fba4cf0188a1f94f4e8a92ad592fc`, 작업 트리 clean. 이번 소유 경로와 겹치지 않았다.
- 소유 경로: 새 `tests/qualification_worker.py`, `tests/integration/test_worker_process_journey.py`, 기존 전용 DB 실행기의 suite 선택, 새 문서·증거.
- 기존 워커·어댑터·domain·SQL 스키마·배포 설정은 수정하지 않았다. 다른 세션의 작업 트리나 팀 저장소에서 커밋·병합하지 않았다.

## 선택 이유

API 프로세스 재시작은 검증했으나 업무 실행은 여전히 같은 pytest 프로세스의 `drive` 호출이었다. 다음 연결은 워커가 스스로 DB를 폴링하고 프로세스 종료 경계에서 권한과 미해결 효과가 유지되는지 확인하는 것으로 잡았다.

기존 배포 CLI는 Kubernetes 경로 저장소·Pod 신원·정지 증명이 필요하다. 공유 클러스터를 이 시험에 사용하지 않는다. 우선 실제 `HandoffWorker.run`을 별도 OS 프로세스에서 실행하고, HTTP 시험 앱과 공유하는 경로 파일만 fixture로 연결했다. 배포 CLI가 검증됐다고 표현하지 않는다.

기존 PostgreSQL adapter를 감싼 관측 wrapper는 적용 전·커밋 후 trace를 남기고 `copy_content` 후에만 대기한다. 실제 적용과 독립 probe·서명·lease 판단은 기존 코드가 수행한다. 결과를 PASS로 바꾸는 hook은 없다.

## 두 종료의 차이

- SIGTERM: 신호 수신을 확인한 뒤 barrier를 해제한다. 현재 효과의 readback·원장 확정·lease 해제 후 다른 프로세스가 이어서 진행한다.
- SIGKILL: 실제 프로세스를 죽이고 wait한다. DB의 자연 lease 만료 뒤에도 미해결 효과와 정지 증명 부재 때문에 다른 프로세스가 진행하지 못해야 한다.

2초 lease는 시험 helper의 값이다. 제품 `WorkerConfig`의 허용 범위·기본값을 변경하거나 DB 만료 시각을 조작하지 않았다. SIGKILL 뒤의 재개를 위해 가짜 Pod 정지 증명을 발급하지 않는다.

## 기록과 미검증

### 최종 실행

- `run-03`: **83 PASS, 0 FAIL, 0 ERROR, 0 SKIP**. 기존 81개와 신규 2개를 같은 환경에서 검증했다.
- 구현·시험 커밋 `ae509a1abd7eeda7e00c7bcf6cb00f8e46e72d00`. 측정 소스 40개 SHA-256이 커밋 파일과 일치했다. Ruff·공백 검사 통과.
- SIGTERM 장면: A·B 프로세스 모두 정상 종료 0. A의 현재 효과 확정 후 B가 새 epoch로 진행했다. 복사 적용 trace 1회, 최종 효과 전부 `confirmed_applied`, 주문 3개 확인·503 1회 유지.
- SIGKILL 장면: A의 실제 종료 -9, B의 정상 종료 0을 wait했다. DB lease 자연 만료 후에도 authority `SOURCE_FENCED`와 `in_flight` 복사 효과가 유지됐다. B의 효과 적용은 없었다. 기존 주문 1개와 차단된 요청 1개를 구분했다.
- 시각 샘플 234개 통과. DB tmpfs 사용량은 실행 종료 시 약 327MiB/1GiB였다. 컨테이너·네트워크 정리 확인.

마지막 범위 재확인에서 상대 작업 트리의 HEAD는 `96e7015`였고 공통 구현 안내·결함 기록·cloud-env 안내·새 관리형 환경 기록이 수정 중이었다. 그 파일들은 건드리거나 이 브랜치에 포함하지 않았다.

### 배포 전 추가 확인

코드 열람상 `WorkerConfig`는 lease 10~900초를 허용하지만 `PostgresExecutionOwnership.duration`은 1~300초를 허용한다. 현재 기본값 120초에는 모순이 없으나 경계값 일치와 잘못된 설정의 시작 전 거부를 별도 시험해야 한다. 이번 helper의 2초 lease로 배포 CLI 설정 계약이 검증됐다고 표현하지 않는다. 해당 제품 코드는 이번 범위에서 수정하지 않았다.

후속 [워커 시작 계약 작업](worker-startup-qualification-2026-11-18.md)에서 이 불일치를 재현하고 `9f520a0`으로 수정했다. 설정 10·120·300초의 실제 DB 발급·해제와 잘못된 시작 인자의 조기 거부를 별도 실행으로 검증했다. 위 문단은 발견 당시의 기록이다.

### 실패 실행 보존

| 실행 | 관측 | 조치 |
|---|---|---|
| run-01 | 전용 PostgreSQL의 384MiB tmpfs 용량이 누적 논리 DB·WAL에 부족했다. checkpoint·DB 생성/정리 오류가 발생해 새 워커 시험에 도달하지 못했다 | worker suite의 실행 소유 DB만 1GiB, 컨테이너 메모리 한도 1.5GiB로 설정. 공유 DB 초기화·제품 조건 완화 없음 |
| run-02 | 기존 81개 회귀는 통과했으나 새 워커 2개는 자식 프로세스가 API 시험 helper를 따라 불러오면서 `test_service_api`를 찾지 못해 시작 실패 | 자식에서 API fixture 의존성을 제거하고 부모 controller에서만 파일 helper를 가져오도록 분리. 실패 로그도 보존하도록 수정 |

실패 실행의 자원 정리 기록을 남겼다. 용량 변경은 시험 배치가 만드는 DB template·WAL을 위한 환경 확보다. 고객 디스크 고갈 장애에 대한 운영 자격으로 해석하지 않는다.

비공개 원문 위치는 `local-dev-records/migration-worker-qualification-20261118/`이다. 공개 [실행 요약](../evidence/worker-process-qualification-20261118.json)은 소스 지문·상태·원문 해시만 제공한다. [범위 문서](../migration-worker-qualification.md)에 정상 인계와 의도된 복구 차단을 구분한다.

실제 Kubernetes 배포·정지 증명 기반 복구·두 독립 서버·클라우드 이전·외부 Edge·고객 IdP·독립 보존·부하/SLO는 남아 있다. 데이터 안전성 시험의 통과를 서비스 연속성이나 금융 운영 인증으로 바꾸지 않는다.
