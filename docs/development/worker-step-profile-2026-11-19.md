# 워커 단계의 연결·검증 비용 실측

2026-11-19 · 시작 `5372daf4411145d25e7c3ad7fe6ae80f66546841`.

## 선택과 범위

직전 원장 실측의 가장 긴 구간은 최종 데이터 검증 확인부터 target 활성화 준비까지였다. 그 안에는 업무·데이터·복원 시험, 증거 저장, 신뢰 확인과 authority 갱신이 함께 있었다. 구간만 보고 검사를 제거하거나 TLS가 원인이라고 단정하지 않고 함수별 계측부터 추가했다.

이번 변경은 테스트 하네스와 문서뿐이다. 제품 소스는 `faa8897837c5fd2e909e908a7316202ddba66922` 이후 그대로이며, 연결 재사용·pool·증거/권한 캐시·검사 생략·시간 및 재시도 연장은 없다. 다른 세션은 `c344934717de6ce52279ad06c28c796f8e85b729`에서 시작했고, 이후 진행 중인 volume adapter·volume unit test 변경은 이번 소유 경로와 겹치지 않았다. 해당 worktree·팀 저장소·AWS·공유 클러스터는 수정하지 않았다.

## 구현과 커밋

| 커밋 | 변경 |
|---|---|
| `9f6eb3008600bf507ba67f8a1ec0974db78d7615` | 실제 네이티브 워커 단계별 cProfile·wall time 계측, 안전한 함수 allowlist 출력, 실패 원문 유지, 전후 비계측 비교 |
| `40ced1756e86b62d659299331001d2a42167f7ad` | 실제 연결 factory 코드로 제어면·업무 admin·업무 role 경로를 구분, 단계별 driver 호출 수와 범주 합계 대조 |

인스턴스 하나의 `_step`만 문맥 안에서 감싼다. 원래 worker 메서드, DB driver, catalog, 승인·lease·RLS 구현은 교체하지 않는다. 문맥 종료 시 wrapper를 제거하며 원래 결과·예외 객체를 그대로 전달한다. SQL·인자·지역 변수·반환 본문·DSN·예외 메시지·raw pstats는 출력하지 않는다. 기존 profiler·다른 스레드·중첩·알 수 없는 단계는 거부한다.

제어면과 업무 DB를 구분할 때도 DSN이나 역할 인자를 읽어 분류하지 않는다. 실제 factory의 고유 코드 객체를 고정된 범주에 연결한다. 같은 코드 객체를 두 범주에 연결하는 모호한 설정은 거부한다. 기록한 것은 연결 생성 **시도**이며 접속 거부 시험도 포함한다.

## 실행 기준과 시험

- 고정 PostgreSQL 17.12 arm64 이미지 `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac`, 전용 tmpfs 384MiB·메모리 512MiB.
- Python 3.12.16, psycopg 3.3.6, 고정 security 소스 `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`.
- 실제 loopback HTTP 등록·승인·admission과 실제 TLS DB를 사용하는 기존 `service_journey` 및 `HandoffWorker`.
- UUID 전용 컨테이너·네트워크, 실행마다 별도 업무 DB·runtime role. 각 업무 fixture 종료 시 해당 fixture DB만 정리.
- 기존 fixture의 쓰기 정지 60초·완료 대기 120초 합성 예산을 유지. **실제 클라이언트 부하는 실행하지 않았다.** 앞선 5초·8회 HTTP 클라이언트 기준은 변경하지 않았고 이번 묶음으로 통과를 주장하지 않는다.

첫 묶음은 단위 11건·실제 TLS 네이티브 전환 3건, 총 14건이 통과했다. 연결 범주 추가 후 단위 12건·실제 전환 3건, 총 15건이 통과했다. 서로 겹치는 시험이므로 29개 독립 사례로 합산하지 않는다. 원래 반환값·예외 보존, 문맥 실패 후 wrapper 제거, 기존 profiler 유지, 다른 스레드·중첩 거부, 누락·중복·실패 측정의 미완료 표시와 인자 비노출을 확인했다.

양쪽 묶음의 실제 세 전환은 모두 HANDOFF_COMPLETE, 효과 전이 21건·authority 이력 10건·검증 참조 16건을 확인했다. 저장된 결과는 모두 pass였고 목적지의 orders·work_items·idempotency_results는 각각 기존 50행을 보존했다. 프로파일러를 제거한 뒤 원래 클래스 메서드가 유지됨도 확인했다. 최종 묶음에서는 각 단계의 driver connect 호출 수가 세 factory 범주 합계와 정확히 같았다.

TLS 확인 및 client 인증서 없는 연결 거부는 기존 runner에서 수행했다. ClockWatch는 첫 묶음 56개·최종 묶음 45개 표본이 모두 통과했다. 이는 fixture의 실제 연결 보안 확인이며 운영 사용자·외부 IdP 인수를 뜻하지 않는다.

## 전체 실행의 변동도 보존

| 묶음 | 비계측 이전 drive | 계측 drive | 비계측 이후 drive |
|---|---:|---:|---:|
| 최초 수집기 | 5,364.300ms | 8,813.978ms | 4,718.095ms |
| 연결 범주 추가 | 4,710.221ms | 4,903.698ms | 4,235.380ms |

각 표본은 별도 fixture에서 실행했고 호스트 부하·DB 시점이 다르다. 계측은 자체 overhead를 추가한다. 두 묶음 사이 제품 성능을 최적화하지 않았으므로 계측 drive가 8.814초에서 4.904초로 줄었다는 수치를 개선 효과로 해석하지 않는다. 전후 차이를 profiler overhead라고 정확히 빼지도 않는다.

최종 묶음의 DB 행 기록 기준 pause 준비→route 확인은 비계측 이전 2,850.523ms, 계측 3,223.570ms, 비계측 이후 2,646.866ms였다. 전체 drive나 단계 wall time과 경계가 다르며 commit 시각·클라이언트 중단 시간을 뜻하지 않는다.

## 최종 계측의 단계별 결과

| 단계 | 단계 wall | 제어면 연결 시도 / inclusive | 업무 admin 시도 | 업무 role 시도 |
|---|---:|---:|---:|---:|
| stage_target | 826.752ms | 47 / 333.475ms | 12 | 1 |
| pause_admission | 260.881ms | 16 / 97.718ms | 2 | 0 |
| fence_source_writes | 467.527ms | 23 / 141.458ms | 10 | 2 |
| copy_content | 302.214ms | 16 / 92.332ms | 5 | 0 |
| verify_final_state | 1,014.646ms | 41 / 530.146ms | 11 | 0 |
| record_activation_intent | 433.728ms | 19 / 185.849ms | 2 | 0 |
| activate_target | 479.992ms | 23 / 189.812ms | 8 | 3 |
| commit_route | 393.321ms | 23 / 146.195ms | 4 | 2 |
| stabilize | 386.625ms | 25 / 171.615ms | 2 | 0 |

9단계 wall 합은 4,565.686ms다. 단계 밖 초기 조회·lease 갱신·종료·프로파일 결과 정리 등은 전체 drive에 포함되지만 이 합계에는 없다. 현재 스레드만 계측하므로 별도 lease keeper·API 스레드의 비용도 전부 측정한 것이 아니다.

| 함수 범주 | 단계 내부 호출 수 | inclusive 합계 |
|---|---:|---:|
| 제어면 연결 factory | 233 | 1,888.600ms |
| 업무 admin 연결 factory | 56 | 379.131ms |
| 업무 role 연결 factory | 8 | 53.353ms |
| 실제 driver connect | 297 | 2,319.346ms |
| Cursor.execute | 1,447 | 1,374.293ms |
| 명시적 transaction 진입 / 종료 | 267 / 267 | 155.931ms / 139.226ms |
| authority.advance_from_ledger | 9 | 1,731.787ms |
| evidence.prove | 16 | 626.893ms |
| verifier.resolve | 34 | 490.708ms |
| evidence.sign | 16 | 10.709ms |

**행들은 중첩된다.** factory와 driver connect, probe와 그 내부 SQL, evidence와 그 내부 신뢰·저장, authority와 그 내부 증거 확인을 더하면 이중 계산이다. 트랜잭션 함수 호출 수는 업무 transaction 수와 동의어가 아니다. 297회에는 거부된 접속도 있으며 실제 성공 연결 297개라는 뜻이 아니다.

`verify_final_state`의 content_equal 3회 합은 71.771ms, order_effects_once 1회는 8.469ms였다. 활성화 의도 단계의 restore_drill은 15.599ms, stabilize의 추가 restore_drill은 66.082ms였다. 이 실행에서는 검증 함수 자체보다 제어면 연결·증거/판정 경로를 우선 조사할 이유가 있다. 다만 앞선 Pod의 945.899ms 구간과 이번 `_step` 범위는 다르므로 수치를 직접 대응시키지 않는다.

## 다음 선택

다음 후보를 **제어면의 제한된 연결 재사용 실험**으로 좁힌다. 업무 DB admin·role 경로, 실제 CONNECT 거부·잘못된 CA/호스트명 시험, 독립 source/target 검증 연결은 대상으로 섞지 않는다.

변경 전에 tenant RLS의 트랜잭션 단위 초기화·반납, non-owner/NOBYPASSRLS 확인, 권한 회수, 실패/취소 후 연결 정리, 동시 요청, 현재 승인·verifier 신뢰와 epoch·lease 회귀를 고정해야 한다. 34회의 신뢰 조회가 관측됐다는 이유로 승인·신뢰를 캐시하거나 검사 횟수를 줄이지 않는다. 표준 pool 도입이 적절한지도 기존 설치·의존성·운영 계약과 함께 검토해야 하며, 이번에는 pool을 구현하지 않았다.

실제 HTTP 클라이언트 한도 소진과 [이전 로컬 실측](cutover-critical-path-2026-11-19.md)은 계속 별도 근거로 남는다. 이번 native 묶음은 제품 이미지·실제 worker Pod·HTTP 부하·클라우드→독립 온프렘·운영 무중단·금융 보안 자격의 인수가 아니다. 합성 IdP·계획·일부 신뢰 입력·로컬 파일 witness의 한계도 유지한다.

## 증거와 정리

원문은 `local-dev-records/worker-step-profile-20261119`의 `native`와 `scoped`에 보존했다. 공개 증거에 원문 19개의 SHA-256·크기·경로, 하네스 및 제품 소스 해시와 실제 결과를 연결했다. 키·인증서·접속 설정은 제외했다. raw 단계 파일은 후속 검증 전 자료임을 표시하고, 실제 SQL 검증을 마친 파일과 구분했다.

정확한 시험 소유 컨테이너·네트워크 4개가 모두 없음을 별도 읽기 전용으로 확인했다. 캐시 이미지는 유지했다. 각 runner에는 `caffeinate -di`를 사용했고 전역 잠자기 설정은 변경하지 않았다.

[측정 방법](../worker-step-profile.md) · [실측 증거 JSON](../evidence/worker-step-profile-20261119.json).
