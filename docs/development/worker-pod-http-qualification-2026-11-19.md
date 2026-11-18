# 실제 HTTPS 앱·지속 요청 연결 개발 기록

2026-11-19 · 개인 `feature/migration-request-qualification`.

## 기준과 경계

시작 SHA는 `f2c7ae96fc8699eb3356ba9a46d1685a7c24cfe1`이다. 다른 작업 트리는 `d664566`에서 깨끗했고, 수정 경로가 겹치지 않았다. 새 launcher·client·HTTP fixture·프로필과 기존 소유 qualification 실행기·Pod 시험만 수정했다. worker·adapter·quiescence 제품 구현과 공유 클러스터·팀 저장소·AWS는 변경하지 않았다.

## 기능별 변경

- `7f66845`: 기존 주문 API의 실제 HTTPS Pod launcher, TLS를 검증하는 별도 부하 client, SQLite/JSONL 기록과 오류·지연 측정.
- `e7c6d04`: 기존 실제 Pod 복구 시나리오를 함수로 재사용하고, 같은 Service 주소의 지속 업무 요청을 연결하는 `pod-http` suite.
- `cb7d921`: 기존 MQ-02 프로필과 실제 경로 설명의 차이를 발견해 별도 HTTPS·Pod 프로필에 원장을 결속. 의도한 오류 구간이 기록됐는지도 확인.

앱 Pod만 시험 코드를 ConfigMap으로 받는다. worker는 앞서 파일 439개를 대조한 고정 이미지를 그대로 실행한다. 앱은 DB별 고정 연결을 사용하고, client는 Service URL을 바꾸지 않았다. 이 실행은 새 연결을 측정하며, 기존 keep-alive 연결이 자동으로 다른 backend에 옮겨진다고 가정하지 않았다.

## 환경 문제와 이전 실행

run-01은 `/private/tmp/sova-contract-security-producer-20261114`의 Git metadata·소스가 사라져 자원 생성 전에 중단됐다. 삭제 원인은 확인하지 못했다. local Git에 같은 생산자 커밋이 남아 있어 별도의 workspace 저장소로 정확히 그 SHA만 복구했다. 원래 개인 Security checkout은 수정하지 않았다. 최초 unit 수집도 같은 원인으로 실패했으며, source 복구 뒤 62개가 통과했다.

run-02는 실제 HTTPS 흐름과 DB 대조가 통과했지만 client 원장이 기존 MQ-02 프로필을 참조했다. 이 프로필에는 loopback·시험 Route로 적혀 있어 실제 범위와 맞지 않았다. 새 전용 프로필을 만들고 run-03을 실행했다. run-02는 탐색 기록으로 남기고 최종 프로필 검증으로 합치지 않았다.

## 최종 결과

run-03은 **1 PASS, 실패·오류·건너뜀 0**, Starlette 경고 1건이다. 신규 단위 회귀 6개·기존 요청 원장 회귀 56개도 통과했다.

측정 소스 43개가 `cb7d921…`의 파일과 일치하며 DB 시각 감시 180개 샘플을 통과했다. 원래 워커가 정지된 구간에도 실제 목적지 HTTP 성공 49건을 관측했다. 이 구간의 연결 오류 8건도 같은 원문에 남아 있다.

- 실제 HTTPS 최초 요청 100건 중 75건 성공, 503 16건·연결 오류 9건.
- 최초 실패 25건은 최종 lookup에서 404를 확인한 뒤 같은 키로 다시 제출했다.
- 논리 주문 100건 전부 확인, 미완료·미확인 0. 과거 주문 50건 보존.
- 최종 관측은 주문·멱등 결과·work item·금액을 독립 SQL snapshot으로 대조했다.
- 전체 시도 150회, 최대 논리 완료 지연 약 21.537초. 최초 실패는 재시도로 지우지 않았다.
- 올바른 HTTPS 검증과 잘못된 CA/hostname 거부를 실제 TLS handshake로 확인했다.
- 기존 live-owner 차단·SA/Pod UID·Secret/Pod 403·실제 runtime 종료·successor 복구·Service PATCH 1회도 함께 통과했다.

부하 종료 후에 재시도했으므로 최대 논리 완료 지연에는 그 대기가 포함된다. 그 값을 서비스 불가용 시간이나 생산 복구 SLO로 사용하지 않는다. 최종 주문이 모두 복구됐다는 이유로 25% 최초 실패를 무중단 성공으로 바꾸지 않는다.

## 증거와 인계

원문은 `local-dev-records/worker-pod-http-qualification-20261119/`에 보존했다. 원장 DB·시도 JSONL·독립 관측·대조 결과·client 로그·API 감사·시각 감시·JUnit을 연결했다. [공개 요약](../evidence/worker-pod-http-qualification-20261119.json)에 원문 해시와 코드·프로필 식별자를 기록하며 token·키·kubeconfig는 포함하지 않는다.

전용 client·kind·임시 태그·DB·네트워크는 정리했다. 실제 제품의 다음 문제는 오류를 숨기는 것이 아니라, target 사전 준비·쓰기 대기·재시도의 책임과 한도를 정하고 같은 원장으로 전후 차이를 측정하는 것이다. 실제 AWS→독립 온프렘·운영 자격·무중단 판정은 남아 있다.
