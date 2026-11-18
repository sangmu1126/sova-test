# HTTP 요청 원장과 이전 후 업무 결과 대조 · MQ-01/02

2026-11-18 · 개인 개발 브랜치 `feature/migration-request-qualification`.

## 이번 구현의 범위

기존 [프로덕션 자격 계획](migration-production-qualification.md)의 첫 연결이다. 실행기가 DB를 옮겼다는 결과와 별도로, 클라이언트가 보낸 요청과 최종 DB의 주문·작업·결과 기록을 대조한다. 운영 승인이나 지원 자격을 발급하지 않는다.

MQ-01은 [시험 프로필 초안](../examples/qualification/orders-http-postgres-v1.json)을 추가한 상태다. 업무·자료형·전송·실행 경로를 명시하고 파일 SHA-256을 원장에 결속한다. 허용 지연·오류율·복구 시간·부하 조건은 아직 미정이다. MQ-02는 아래 제한된 로컬 구성 요소 경로에서 검증했다. 운영 API·워커·클라우드 전체 인수(MQ-03 이후)는 별도다.

## 다른 세션과의 작업 경계

| 구분 | 범위 |
|---|---|
| 이번 작업 | `src/sova/qualification/`, 전용 HTTP 시험 앱·DB 실행기, 해당 단위·통합 시험, 프로필·이 문서·개발 기록 |
| 관측한 다른 작업 | `feature/contract-driven-foundation`, 기준 `11f9018`: 볼륨·MongoDB·Kafka·관리형 클러스터·관련 공통 문서 |
| 관측상 겹침 | 기준 커밋 이후 변경 경로와 이번 소유 경로의 교집합 0개 |
| 기존 코드 접점 | `tests/postgres_business.py`, `tests/handoff_drive.py`, 기존 coordinator·승인·효과 원장·adapter·probe를 변경 없이 소비 |
| 이번에 변경하지 않은 것 | 기존 어댑터·공통 원장·서비스 API/워커·infra·다른 작업 트리·Sky·팀 저장소 |
| 시험 환경 | 실행마다 새 이름·라벨·전용 네트워크·루프백 포트의 PostgreSQL. 공유 kind DB를 초기화하거나 새 시험에 사용하지 않음 |

외부 세션 전체의 작업 계획을 확인하거나 상호 합의한 것은 아니다. 현재 Git 상태·변경 경로를 관측한 결과다. 로컬 `personal-development/SESSION-SCOPES.json`에 이번 소유 범위를 게시했다. 이 파일은 OS 잠금이나 타 세션의 동의를 대신하지 않는다. 통합 전에 최신 경로와 계약을 다시 확인한다.

## 구성과 실제 동작

```text
독립 HTTP 클라이언트
  ├─ 전송 전 SQLite에 논리 요청·시도 영속 기록
  ├─ POST /orders → 실제 loopback TCP → 시험 앱 → 앱 역할로 PostgreSQL 쓰기
  └─ 응답·실패·재조회·재시도 기록

기존 인계 드라이버
  └─ 계약·계획·승인·효과 원장 → 실제 차단·복사·대조·활성화·시험 경로 전환

독립 결과 조회
  └─ 최종 DB의 읽기 전용 repeatable-read snapshot
     → 요청별 주문·멱등 결과·완료 작업 수 대조
     → 정합성 / 클라이언트 확인 상태 / 전체 대기 / 미해결을 별도 보고
```

| 파일 | 책임 |
|---|---|
| [requests.py](../src/sova/qualification/requests.py) | 불변 논리 요청, SQLite FULL 커밋, 시도·결과 분리, 요청별 정합성·확인 상태·지연 대조 |
| [http_orders.py](../src/sova/qualification/http_orders.py) | 고정 origin으로 실제 HTTP 전송, 전송 전 기록, 제한된 결과 수용, 오류·키 비노출 |
| [postgres_orders.py](../src/sova/qualification/postgres_orders.py) | 최종 DB를 별도로 조회. 쿼리 실패를 빈 정상 결과로 만들지 않으며 금액 정밀도를 보존 |
| [보고 CLI](../src/sova/qualification/__main__.py) | 원장과 관측 JSON을 읽어 측정 보고서 출력. 중복 JSON 키·과대 입력·잘못된 scope 거부 |
| [시험 앱](../tests/qualification_orders.py) | 기존 주문 fixture의 HTTP 표면. run 전용 Bearer와 tenant/run 검사. 고객 인증 제품이 아님 |
| [전용 DB 실행기](../tests/qualification_postgres.py) | 캐시 이미지 ID 고정, 이번 실행 전용 PostgreSQL/TLS/자원 정리. 공유 시험 환경을 사용하지 않음 |

### 요청·시도·업무의 구분

- 원장은 하나의 tenant·run·프로필 digest에 결속된다.
- 하나의 논리 요청은 `request_id`와 `(customer, idempotency_key)`에 결속된다. 새 request ID로 같은 업무를 중복 집계하지 않는다. 같은 키라도 고객이 다르면 다른 업무다.
- 재전송에는 같은 논리 요청을 사용하고 매번 새 attempt를 기록한다. 금액은 양수의 제한된 소수 문자열이며 부동소수점·NaN·묵시적 변환을 받지 않는다.
- 시도 기록을 커밋한 뒤 HTTP를 보낸다. 프로세스가 응답 기록 전에 멈추면 미완료 시도가 남는다. 재시작 시 그것을 성공으로 채우지 않는다.
- 오류·재시도가 있어도 최초 요청부터 완료 확인까지의 시간을 유지한다. 재개한 프로세스는 새 clock domain이므로 이전 프로세스와의 단조 시각을 빼지 않고 전체 시간을 미확인으로 둔다.
- 조회 전에 제출 시도 기록이 필요하다. 기존에 있던 주문을 조회만 하고 이번 실행에서 처리한 것으로 표시하지 않는다.

### 정합성과 사용자 확인의 구분

| 상태 | 의미 |
|---|---|
| `CONFIRMED` | 정상 영속 결과가 있고 클라이언트의 성공 응답 또는 재조회 영수증도 일치 |
| `UNRESOLVED_COMMIT` | DB에는 결과가 있지만 클라이언트가 정상 결과를 확인하지 못함 |
| `NOT_COMMITTED` | 제출 결과가 기록됐고 완전한 최종 관측에서 영속 결과가 없음 |
| `UNKNOWN` | 전송/관측/진행 상태를 충분히 판단할 수 없음 |
| `VIOLATION` | 성공 응답한 주문 누락, 중복 주문, 금액/작업/결과/영수증 불일치 등 검출 |

`data_integrity=SATISFIED`여도 응답을 못 받은 주문이 남을 수 있다. 전체 요청 상태를 함께 읽는다. `service_continuity=NOT_QUALIFIED`, `qualification_issued=false`, `authorizes_execution=false`는 모든 보고서에 유지한다. 숫자 SLO가 없으므로 지연 자격을 발급하지 않는다.

SQLite 파일은 로컬 소유자가 수정할 수 있다. append-only 트리거·FULL 커밋은 운영 WORM·독립 감사·서명 증거를 대신하지 않는다. 실제 판정 경로로 연결할 때는 기존 Security 발행자 신뢰·서명·폐기·scope/revision과 현재 승인 계약에 명시적으로 결속해야 한다.

## 검증 결과

최종 실행: **단위 56개 + 실제 HTTP/PostgreSQL 통합 3개 = 59 PASS, 0 FAIL, 0 SKIP**. Ruff 통과. [공개 가능한 결과 요약·소스 지문](evidence/http-request-qualification-20261118.json).

| 실제 시험 장면 | 확인한 것 |
|---|---|
| 전환 전·쓰기 차단 중·전환 후 주문 | 3개 논리 요청, POST 4회, 차단 중 503 1회와 재시도 기록 보존, 최종 3개 확인 |
| 커밋 후 HTTP 응답 지연/timeout | DB에는 저장됐어도 먼저 `UNRESOLVED_COMMIT`. 이전 뒤 재조회·동일 요청 재시도로 1개 주문만 존재 |
| 최종 DB 주문 삭제 | 성공 응답한 주문의 실제 행·연관 기록을 삭제해 `ACKNOWLEDGED_RESULT_MISSING` 검출 |
| 소수점 변조 | 실제 DB 금액 `10.00→10.001` 변경을 반올림하지 않고 검출 |
| 잘못된 토큰·다른 tenant | 각각 401·403, 주문 미생성 |
| DB 접속 인증 | 검증된 서버 TLS로 접속, 이번 run의 클라이언트 인증서가 없으면 거부, 인증된 접속은 계속 성공 |

통합 시험은 arm64 PostgreSQL **17.12** 컨테이너 한 대 안의 서로 다른 논리 DB를 사용했다. HTTP는 실제 TCP지만 loopback 평문이다. DB는 서버 인증서·호스트명 검증과 run 전용 클라이언트 인증서를 사용했다. 이 인증서는 합성 시험 역할들이 공유하므로 고객별 workload 신원 자격이 아니다.

기존 coordinator/승인/효과 driver를 사용했으며 실제 운영 HTTP API·OIDC·배포 워커·외부 Edge 경유 시험이 아니다. 시험 Route는 메모리 객체다. 두 독립 서버·AWS→VM·HA·부하·복귀의 새 운영 자격이나 금융 보안 인증으로 합산하지 않는다.

## 로컬 재현

기존 환경의 Python, 고정된 Security source, 이 저장소의 `service`·`dev` 의존성이 필요하다. 이번 실행은 Python 3.12.16을 사용했다. DB 이미지는 아래 정확한 arm64 캐시 ID다. 실행기가 다른 태그로 바꾸거나 이미지를 자동 다운로드하지 않는다.

```sh
python tests/qualification_postgres.py \
  --image sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac \
  --security-source /path/to/pinned-security/src \
  --artifacts /path/to/new-private-run-directory
```

Security 생산자 기준은 `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`다. 실행기는 새 컨테이너·네트워크·인증서를 만들고 합성 DB를 사용한다. 이름과 소유권 라벨이 일치하는 시험 자원만 정리하며 다른 서버에 대신 접속하지 않는다. 데이터는 휘발성이며 실제 고객 데이터를 넣지 않는다. 생성 포트는 loopback 한 주소인지 재검사한다. 전용 bridge는 외부 egress 차단 시험 결과를 제공하지 않는다.

원문은 로컬 `local-dev-records/migration-qualification-20261118/run-05/`에 저장했다. 공개 요약에는 경로·토큰·키를 넣지 않았다. 새 경로에서 재현하면 새 source/환경 지문으로 기록하고 이전 자격으로 합치지 않는다.

보고 CLI는 다음과 같다.

```sh
python -m sova.qualification reconcile \
  --journal /path/to/client.sqlite \
  --observation /path/to/final-observation.json
```

- 종료 0: 현재 자료에서 정합성 위반이 없고 모든 논리 요청의 결과가 확인됨. 운영 자격은 여전히 발급되지 않음.
- 종료 2: 위반 또는 입력·관측 자료 오류.
- 종료 3: 결과 불명·관측 불완전·미확정/미처리 요청·빈 실행. 저장된 주문이 있어도 클라이언트가 확인하지 못했다면 0으로 끝내지 않음.

최종 관측은 트래픽을 멈추고 인계를 마친 뒤 수집한다. `complete=true`는 쿼리 집합을 모두 읽었다는 뜻이며 원격 writer가 영원히 쓰지 못한다는 증거가 아니다.

## 다음 연결 지점

다음 MQ-03은 실제 서비스 API·승인·배포 워커에서 같은 업무를 실행하고, 목적지 신규 쓰기 뒤 복귀까지 원장과 대조하는 것이다. 현재 세션의 파일 소유 범위를 넘는 서비스·공통 fixture 수정이 필요하면 먼저 최신 작업 경로와 소유 기록을 확인한다. 이번 브랜치를 다른 세션의 작업 트리에 자동 병합하지 않는다.

본 문서는 프로덕션급 완성으로 넘어가기 위한 첫 측정 경로다. 필요한 다음 조건은 수치 SLO·부하/네트워크·실행기 장애·외부 효과·독립 서명·운영 복구·만료/회수 인수다.
