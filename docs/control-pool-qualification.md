# 제어면 연결 재사용 자격 실험

운영 기본 연결을 바꾸기 전 수행하는 **로컬 네이티브 qualification**이다. 자체 pool을 만들지 않고 `psycopg_pool==3.3.3`을 wheel SHA-256으로 고정해 작업 전용 폴더에 설치한다. 제품·공유 가상환경·업무 DB 연결은 변경하지 않는다.

## 계약

| 항목 | 실험 동작 |
|---|---|
| 범위 | 워커 제어면 runtime role만 재사용. 앱·배치·admin 업무 DB와 실제 접근 거부 probe는 새 연결 유지 |
| 크기 | 최대 4개, 최소 0개, 대기열 최대 4개. 시험별로 1·2개 상한도 검증 |
| 대기 | 풀 대기 1초, 연결 생성은 기존 서비스의 10초 접속 제한. 애플리케이션 효과 자동 재실행 없음 |
| 연결 대여 | 실제 session/current user 일치·LOGIN·NOBYPASSRLS·비관리자·현재 DB CONNECT·TLS 상태 확인 |
| repository 트랜잭션 | 기존 owner/bypass 검사, tenant `set_config(...,true)`, 현재 승인·신뢰·lease·epoch 검증 유지 |
| 반납 | 정상 commit 또는 예외 rollback 후 `DISCARD ALL`. Python 연결의 isolation/read_only/deferrable·row factory도 초기화 |
| 준비된 SQL | 자동 prepare 비활성화. 서버 DISCARD 이후 client/server prepared 상태가 엇갈리지 않도록 함 |
| 초기화 실패 | 표준 pool이 연결을 폐기. 오염된 backend를 다른 호출에 넘기지 않음 |
| 종료 | pool을 닫고 신규 대여 거부. 새 연결 방식으로 몰래 fallback하지 않음 |

반납 시 세션 초기화는 pool의 reset callback으로 수행한다. `DISCARD ALL`은 transaction 밖에서 실행하고, 이후 연결이 idle 상태로 돌아와야 한다. 이 구성은 [Psycopg pool 계약](https://www.psycopg.org/psycopg3/docs/api/pool.html)과 [PostgreSQL DISCARD 계약](https://www.postgresql.org/docs/17/sql-discard.html)에 따른다. Pool의 연결 재생성은 대여 준비이며 업무 명령·효과 재실행과 구분한다.

## 확인하는 경계

- 같은 backend PID 재사용 시 세션 tenant·search_path·임시 테이블·prepared statement·advisory lock 제거.
- SQL 오류·애플리케이션 예외·실제 statement_timeout에 의한 query 취소 후 rollback과 다음 정상 쿼리.
- read-only/repeatable-read/deferrable 속성의 다음 사용자 전파 방지.
- 동시 tenant 요청의 RLS 분리와 무범위 조회 0건, 다른 tenant의 계획 조회 거부.
- 기존 backend가 있어도 LOGIN·CONNECT 회수 또는 BYPASSRLS 부여 시 대여 거부.
- 현재 승인 회수 후 admission 거부, 현재 서명자 신뢰 회수 후 실제 서명 검증 거부.
- 살아 있는 실행자와 충돌 거부, epoch 증가 후 옛 실행자의 renew·release 거부.
- reset 실패 backend 폐기, 실제 종료된 backend 교체, 포화 시 유한한 대기와 종료 후 거부.
- 실제 등록·승인·워커 실행·서명 관측·원장·데이터 확인까지 기존 연결 방식과 비교.

## 미검증과 운영 반영 조건

이 실험의 1초는 **풀 자원 대기 제한**이다. 동기 check SQL의 네트워크 blackhole까지 1초 안에 종료된다는 보장이 아니다. 실제 서버 취소와 즉시 종료 backend 시험을 네트워크 단절·사용자 취소 전체의 검증으로 확대하지 않는다.

자격증명 교체·인증서 만료/회수 때 기존 세션 drain, 역할 멤버십 변경, 연결 생성 실패의 장기 반복, checkout/reset 중 네트워크 지연, 프로세스 강제 종료, 여러 worker/Pod·사이트 장애·장시간 메모리 및 누수·운영 SLO는 별도 인수가 필요하다. 조회 직후 권한이 바뀌는 경우까지 원자적으로 막는다고 주장하지 않는다. 기존 repository의 트랜잭션별 검사는 제거하지 않는다.

기존 native fixture는 쓰기 정지 60초·완료 대기 120초 예산을 사용하며, 이번 묶음에는 HTTP 클라이언트 부하가 없다. 5초·8회 완료 기준이나 실제 AWS→독립 온프렘 무중단을 통과한 것으로 사용할 수 없다. synthetic IdP·계획·일부 신뢰 입력과 로컬 witness의 한계도 남는다.

개인 실험을 통과해도 바로 제품 `_connect`, 배포 설정, 의존성 기본값에 넣지 않는다. 보안 회귀와 성능 결과를 함께 보고 별도 승격 변경으로 판단한다. 같은 머신의 소수 실행에서 나온 차이는 개선율이나 overhead 보정치로 사용하지 않는다.

[개발 기록](development/control-pool-2026-11-19.md) · [실측 증거](evidence/control-pool-20261119.json) · [선행 단계별 계측](worker-step-profile.md).
