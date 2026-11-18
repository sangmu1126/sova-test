# 워커 단계별 DB·검증 비용 계측

읽기 전용 [원장 시각 측정](cutover-critical-path.md)에서 드러난 효과 사이 구간을 조사하는 qualification 전용 도구다. 실제 네이티브 `HandoffWorker`를 실행하며 연결·SQL·검증·증거 저장·판정 함수의 비용을 수집한다. 제품의 실행·권한·트랜잭션·증거 정책은 변경하지 않는다.

## 측정 방법

1. 등록·승인·admission은 기존 실제 loopback HTTP 경로로 수행한다.
2. 해당 워커 인스턴스의 `_step` 호출만 감싼다. 클래스나 전역 driver 함수를 교체하지 않는다.
3. 현재 스레드에서만 단계마다 `cProfile`을 켜고, 단계 전체 wall time은 `perf_counter_ns`로 잰다.
4. 고정한 함수 코드 객체의 호출 횟수·self time·inclusive time만 내보낸다. 실제 연결 factory의 코드 객체로 제어면·업무 admin·업무 role 경로를 구분하고, 호출별 DSN이나 역할 인자는 수집하지 않는다. SQL·인자·반환 본문·지역 변수·파일 경로·raw pstats는 내보내지 않는다.
5. 원래 반환값과 예외 객체를 그대로 전달한다. 실패 단계도 `raised`로 남기고 문맥 종료 시 인스턴스 wrapper를 제거한다.
6. 실제 계획 원장을 읽어 전환 상태·21개 효과 전이·10개 authority 이력·16개 검증 참조를 별도로 확인한다. SQL 데이터 확인은 계측 완료와 구분한다.

기존 profiler가 있는 스레드·다른 스레드·중첩 호출·알 수 없는 단계에는 계측을 시작하지 않는다. 실패를 숨기거나 재실행하는 동작은 없다. 시작한 drive가 실패하거나 뒤의 확인이 실패해도 `-raw.json`에 당시 단계 측정을 남긴다.

## 숫자를 읽는 기준

| 값 | 의미와 제한 |
|---|---|
| 단계 wall time | 해당 `_step` 안의 실제 경과 시간. 단계 전 lease 갱신·상태 조회와 단계 밖 처리는 제외 |
| 연결 inclusive time | 실제 `psycopg.Connection.connect`에 머문 시간. TLS만의 비용이 아니며 매번 새 연결 사용. 거부된 접속 시도도 횟수에 포함 |
| factory별 inclusive time | 코드 경로별 연결 생성 호출 envelope. driver connect와 중첩되므로 둘을 더하지 않음 |
| SQL execute inclusive time | 실제 `Cursor.execute` 호출 envelope. 모든 COPY·fetch·commit 비용 전체는 아님 |
| 트랜잭션 enter/exit | 명시적 transaction 진입·종료 함수의 비용. 업무 트랜잭션 수와 같다고 가정하지 않음 |
| probe inclusive time | DB 호출 등을 포함한 해당 검증 함수의 시간 |
| evidence/authority inclusive time | 내부 DB·검증·서명 등의 하위 호출을 포함한 시간 |
| 함수 self time | profiler가 계산한 직접 실행 시간. 네이티브 I/O와 대기 해석에는 한계가 있음 |

**Inclusive 값은 서로 중첩되므로 더해서 전체 지연을 만들지 않는다.** 단계별 connect inclusive time의 합은 그 단계들의 connect 비용 요약이지 순수 네트워크 비용이나 전환 중단 시간이 아니다. 계측되지 않은 API·lease keeper 스레드도 있을 수 있다.

계측은 실행 시간을 늘린다. 계측 전후의 별도 비계측 실행을 함께 남기되, 이 세 표본으로 overhead를 빼거나 통계적 향상을 주장하지 않는다. 단일 프로세스의 실제 DB 실행이며 앞선 CLI Pod·HTTPS 부하 실행의 동일한 타임라인이 아니다. 이전 클러스터 구간에 측정 비율을 그대로 곱하지 않는다.

이 묶음은 기존 `service_journey`의 합성 가용성 예산인 쓰기 정지 60초·완료 대기 120초를 그대로 사용한다. 클라이언트 부하는 실행하지 않는다. 앞선 실제 HTTP 부하의 5초·8회 완료 기준을 통과한 것으로 사용할 수 없으며, 해당 기준과 기존 소진 기록은 유지한다.

## 변경 금지 경계와 후속 실험

연결 재사용·pool·증거 캐시·권한 캐시를 넣지 않는다. tenant RLS, non-owner/NOBYPASSRLS 검증, 현재 승인·issuer·신뢰 검증, 단일 writer·epoch·lease, 독립 복원 시험을 그대로 수행한다. 시간·재시도 한도도 완화하지 않는다.

연결 비용이 크더라도 다음 실험에서 pool 반환 후 tenant 상태 초기화, 권한 회수, 실패 트랜잭션·취소 정리, 동시성 및 epoch 회귀가 먼저 필요하다. 우선 후보는 제어면 연결로 한정한다. 업무 role의 실제 CONNECT 거부 및 TLS 시험은 신선한 연결을 요구하므로 재사용 대상으로 섞지 않는다. 이번 도구는 그 선택의 근거를 수집할 뿐 실행 자격이나 운영 무중단을 발급하지 않는다.

[개발 기록](development/worker-step-profile-2026-11-19.md) · [실측 증거](evidence/worker-step-profile-20261119.json).
