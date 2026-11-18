# 운영 중 쓰기를 유지하는 복원 시험의 스냅샷 경계

2026-11-19 · `PostgresHandoffProbes.restore_drill`의 독립 검증 수정.

## 발견과 재현

조회 backpressure 비교의 첫 실제 워커 실행은 경로 효과까지 확인됐으나 `operations.restore_test`가 FAIL이었다. 제어면은 `FAILBACK_REQUIRED / CONTRACT_BREACH`를 유지했고 시험은 90초 뒤 완료 대기를 실패했다. 복원 검사를 제거하거나 결과를 PASS로 고정하지 않았다.

기존 probe는 기본 READ COMMITTED 연결로 테이블을 하나씩 복사한 뒤, **새 연결에서 읽은 현재 대상 DB**와 복원본을 비교했다. 이 사이 새 주문이 커밋되면 테이블마다 시점이 달라지고, 정상적인 신규 데이터도 복원 오류처럼 보일 수 있었다.

실제 별도 DB 연결로 첫 테이블 COPY 직후 주문·작업·결과를 한 트랜잭션으로 커밋하는 시험을 추가했다. 수정 전에는 현재 DB가 세 테이블 모두 51행인데 복원본은 50·51·51행으로 섞였고 probe가 FAIL이었다. 원 실행의 테이블별 원문은 보존되지 않았으므로 이것을 동일 실행의 모든 내부 사건을 복원한 것으로 주장하지 않는다. 동일한 시점 경합을 별도의 통제된 실제 DB 시험에서 재현한 것이다.

## 수정한 경계

1. 선언된 원본·목적지 DB를 scratch로 받지 않는다.
2. 실제 연결이 돌려주는 `current_database()`도 확인해 scratch가 현재 대상 DB로 연결되는 경우 거부한다. 참조 프로필은 서로 다른 DB 이름을 요구한다. 임의 alias registry나 여러 서버의 물리 신원을 일반적으로 증명하는 기능은 아니다.
3. 대상 읽기 연결을 `REPEATABLE READ, READ ONLY`로 설정하고 실제 격리·읽기 전용 상태를 확인한다.
4. 같은 스냅샷에서 각 테이블의 기대 digest와 COPY 데이터를 읽는다.
5. scratch 쓰기 트랜잭션 안에서 실제 복원 digest를 계산하고 비교한다. 다른 시험의 후속 TRUNCATE와 검증이 섞이지 않도록 한다.
6. 스냅샷 ID·실제 DB 이름·격리 상태·테이블별 결과를 관측에 남긴다. 해당 결과의 증거 digest는 기존 서명·게이트 경로로 전달한다.

PostgreSQL의 [Repeatable Read](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-REPEATABLE-READ)는 트랜잭션 내 읽기를 같은 스냅샷으로 유지한다. [SET TRANSACTION](https://www.postgresql.org/docs/17/sql-set-transaction.html)은 첫 데이터 조회 전에 적용한다. 새로운 업무 쓰기를 막거나 source fencing을 풀어서 검증을 통과시키는 방식이 아니다.

## 유지하는 실패 조건

복원 INSERT trigger로 실제 금액을 변조한 경우 계속 FAIL이어야 한다. 같은 이름이나 연결 별칭으로 활성 대상 DB를 scratch로 지정하면 TRUNCATE 전에 거부해야 한다. 연결·조회 오류, 읽기 모드 설정 실패, 실제 digest 불일치는 성공으로 처리하지 않는다. 이전·복귀·1ms 쓰기 정지 예산 초과의 failback 요구·출발지 없이 복원 시험·물리 행 순서가 다른 동일 데이터도 회귀 시험에 포함했다.

동시 커밋 시험의 수정 후 결과는 현재 DB 51·51·51행, 복원본 50·50·50행이다. 새 51번째 주문을 누락했다는 뜻이 아니라 복원 기준 스냅샷 뒤의 정상 쓰기다. **복원본이 최신 live DB와 같거나 RPO가 0이라는 주장은 하지 않는다.** 스냅샷 이후 데이터 보존에는 별도 WAL·복구 지점·보관 정책이 필요하다.

## 시험 저장소와 검증 한계

초기 9건 회귀 묶음은 전용 tmpfs 384MiB에서 마지막 DB 생성 중 `DiskFull`로 오류가 났다. 해당 실행과 두 시계 관측 실패를 보존했다. 첫 동시성 시험은 이미 통과했지만 실행 전체는 통과로 세지 않았다.

다수의 작은 DB를 runner 정리 시점까지 유지하는 기존 worker/startup 시험과 같은 정책으로 restore suite의 전용 tmpfs를 1GiB, 컨테이너 메모리 상한을 1.5GiB로 설정했다. 이후 10건 모두 통과했고 사용량은 약 442MiB였다. 공유 저장소나 요청·DB 연결·재시도·복구 시간 제한은 변경하지 않았다. 과거 다른 실행의 원인 미확정 시계 실패까지 DiskFull로 소급하지 않는다.

이 probe는 로컬 참조 프로필의 복사·복원 검증이다. 영속 백업 매체, WAL/PITR, 보존·키·재해 복구·대용량 부하·DDL 동시 변경을 운영 자격으로 검증한 것은 아니다. 긴 스냅샷의 자원·vacuum 영향과 실제 백업 도구 연결은 별도다. `durable_backup_qualified`는 false다.

[전체 실행·실패 기록](development/lookup-backpressure-2026-11-19.md) · [증거](evidence/lookup-backpressure-20261119.json) · [조회 backpressure](lookup-backpressure.md).
