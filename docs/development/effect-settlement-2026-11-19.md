# 실제 복사 중단과 조회 기반 복구 검증

2026-11-19 · 시작 `20dbd7133454b66628e6ef7a56e5a1c12c0a93ae`.

## 선택과 작업 경계

앞선 프로세스 guard는 실제 업무 쓰기 전에만 검증했다. 이번에는 기존 PostgreSQL offline-copy adapter가 실제 데이터를 복사하는 도중, 또는 commit을 마친 뒤 효과 확인을 기록하기 전에 워커를 죽이고 복구 결과를 확인했다.

다른 세션이 worker Execution.close·volume agent 종료 경로를 수정 중이어서 제품 worker와 그 종료 코드는 변경하지 않았다. 개인 qualification worktree에서 native worker fixture의 opt-in fault, 새 로컬 proof fixture, 실제 PostgreSQL/HTTP 시험과 runner만 변경했다. 다른 세션의 미커밋 코드는 가져오지 않았다.

| 커밋 | 변경 |
|---|---|
| `c79849f` | 실제 COPY commit 전후 SIGKILL·원격 요청 잔류·원격 정리 후 기존 readback recovery 검증 |
| `47d104e` | 관측 권한 부족을 부재로 처리하지 않도록 확인, 실제 runtime role 거부 시험, 중간 progress 원문 보존 |
| `1a50b1e` | source의 원래 50개 업무 관계·값과 양쪽 세 보호 테이블의 전체 행 수를 독립 SQL로 대조 |

기존 pool 구현·제품 `_connect`·worker/coordinator/effect 원장·배포 기본값은 그대로다. 실제 worker 프로세스는 기존 HandoffWorker.run을 사용하지만, native qualification adapter wrapper와 file route를 사용하므로 배포 CLI Pod와 구분한다.

## 실제 fault 경계

두 경우 모두 기존 API로 계획 등록·승인·admission을 완료했다. API는 실제 별도 CLI 프로세스이며 fixture JWKS는 실제 인증서 검증 HTTPS로 조회했다. 업무 요청은 별도의 loopback HTTP 앱으로 보내 실제 PostgreSQL에 저장했다.

1. **committed:** 기존 copy adapter의 실제 commit 뒤 원격 DB SELECT를 서버에서 대기시켰다. Effect는 아직 in_flight인 채 워커를 SIGKILL했다.
2. **uncommitted:** source·target에 동일한 trigger 정의를 설치해 기존 stage_target 구조 비교를 통과시켰다. 고유 태그를 가진 시험 worker가 target의 첫 행을 COPY할 때만 서버 trigger가 대기한다. 실제 COPY transaction이 끝나기 전에 SIGKILL했다.

Source의 기존 50개 주문과 시험 주문 1개는 모두 실제 SQL이다. 추가 audit trigger는 COPY의 orders TRUNCATE transaction이 commit됐는지 별도 테이블에 기록한다. Source/target trigger 정의를 같게 유지하고, 게이트나 구조 비교를 면제하지 않았다. 이 audit는 관측용 fixture이며 운영 WORM 감사 원장으로 주장하지 않는다.

최종 보강에서는 source의 orders·work_items·idempotency_results 각각 51행을 확인했다. 원래 ID 1~50의 고객·멱등 키·금액·완료 work 관계·confirmed 결과를 JOIN해 50건 모두 일치하는지 별도 SQL로 대조했다. Target도 세 테이블 각각 committed 53행 또는 uncommitted 0행임을 확인했다.

Relay는 TLS를 종료하지 않고, 해당 context가 끝날 때까지 upstream 소켓을 보유한다. 따라서 워커가 죽어도 이미 전달된 실제 DB 요청을 남기는 상황을 의도적으로 재현할 수 있다. 이는 일반 네트워크 장애 전체를 모사한 것이 아니며 payload를 해독·기록하지 않는다.

## 프로세스 사망과 lease 만료 후에도 복구 차단

실제 pg_stat_activity에서 고유 태그의 backend가 active/PgSleep인 것을 본 뒤 워커를 SIGKILL하고 Popen.wait로 종료를 확인했다. Lease 행의 시각을 고치지 않고 실제 DB 시계로 만료를 기다렸다.

최종 묶음에서 committed 경우에는 backend 1개가 active/PgSleep으로 남았다. Uncommitted 경우에는 source backend 1개가 idle in transaction/ClientRead, target backend 1개가 active/PgSleep으로 남았다. 두 경우 모두 프로세스 exit는 -9이고 lease도 만료됐지만 후속 워커는 복구 proof를 받지 못했다. 다음 mutation·readback도 시작하지 않았고 source route를 유지했다.

시험은 고유 application_name 태그와 이번 fixture의 DB 목록에 속한 backend만 종료했다. Relay 소켓과 thread도 정리한 뒤 실제 backend 부재를 다시 조회했다. 다른 세션이나 공유 DB를 대상으로 종료하지 않았다.

## 로컬 proof의 신뢰 조건

`NativeSettlement`는 자기가 관리하는 Popen·relay·run-owned DB만 아는 qualification 전용 issuer다. Process 종료, relay 종료, 실제 원격 세션 부재, 만료된 기존 lease, epoch와 pending command digest를 묶어 30초 proof를 발급한다. 기존 실행 원장이 source epoch·현재 pending digest·DB 기준 유효기간을 다시 확인한다. 변조한 artifact digest는 거부됐다.

부재 관측에는 충분한 세션 가시성이 있어야 한다. [PostgreSQL의 관측 제한](https://www.postgresql.org/docs/17/monitoring-stats.html)에 따라, observer가 전용 cluster/database와 superuser 또는 실제 사용 가능한 pg_read_all_stats 권한을 갖는지 확인했다. [pg_has_role의 USAGE](https://www.postgresql.org/docs/17/functions-info.html)를 사용해 NOINHERIT 멤버십을 그대로 사용 가능 권한으로 오해하지 않았다.

최종 시험에서는 실제 NOBYPASSRLS runtime role로 같은 관측을 시도했고, 권한 부족은 remote_state=unknown으로 남겼으며 proof를 발급하지 않았다. 이는 운영 runtime에 전역 관측 권한을 주라는 제안이 아니다. 이번 privileged observer는 disposable DB의 신뢰된 fixture다.

application_name은 client가 설정할 수 있어 운영 신원으로 사용할 수 없다. 정확한 소유 프로세스와 relay를 통제하는 이 fixture에서만 식별자로 사용했다. Kubernetes UID를 가짜로 만들거나 lambda로 Pod 사망을 가정하지 않았다. Native proof가 로컬 recovery lease를 허용한 것과 운영 proof/금융 보안 인수는 구분한다.

## 원격 정리 후 결과

| 항목 | commit 후 종료 | commit 전 종료 |
|---|---|---|
| old worker exit | 의도한 SIGKILL -9 | 의도한 SIGKILL -9 |
| 실제 lease 만료 뒤 원격 요청 | 1개 남아 proof 거부 | 2개 남아 proof 거부 |
| 원격 fencing·relay 종료 뒤 | 원격 세션 0개, 로컬 proof 1개 | 원격 세션 0개, 로컬 proof 1개 |
| successor의 copy apply | 0회 | 0회 |
| successor의 원래 copy command readback | 1회 | 1회 |
| 실제 COPY transaction commit audit | 1 | 0 |
| 최종 target orders | 53행 | 0행 |
| copy effect | confirmed_applied | unknown |
| 최종 epoch | 3 | 2 |
| 이전 완료 | HANDOFF_COMPLETE | 미완료 · RecoveryRequired |
| HTTP 확인 주문 / 보존한 오류 | 3개 확인 / 503 1회 | 1개 확인 / 503 2회 |

Commit 후 사례는 기존 copy 결과를 조회해 확인한 뒤 나머지 검증·activation·file route 전환을 진행했다. Target의 53행에는 원래 50행과 시험 주문 3개가 들어 있다. Copy를 두 번 실행해도 같은 데이터를 덮어쓸 수 있으므로, 단순 행 수만으로 재실행 여부를 판단하지 않았다. 실제 SQL audit의 1회 commit과 old trace 1회 apply, successor copy apply 0회·readback 1회를 함께 확인했다.

Commit 전 사례는 원격 세션 fencing으로 실제 COPY transaction이 rollback됐고 target은 0행이었다. 기존 adapter는 이를 확정 미적용으로 재실행하지 않고 UNKNOWN으로 반환했다. 현재 worker는 미해결 recovery lease release를 거부해 RecoveryRequired를 반환하며, owner mode는 reconcile이고 source route와 쓰기 봉쇄를 유지했다. Source의 기존 50행과 확인된 시험 주문 1개는 보존됐다.

두 경우의 HTTP reconciliation은 데이터 무결성 SATISFIED였지만 성공 범위는 다르다. 후자는 이전 성공이 아니며 source 쓰기도 다시 열리지 않았다. UNKNOWN의 명시적인 운영 보류 표시와 후속 승인·복구 절차는 별도 통합이 필요하다. 현재 경로를 SAFE_HOLD UI까지 완료했다고 표현하지 않는다.

## 회귀 결과와 원문

초기·관측 권한 보강·전체 데이터 보강의 세 묶음에서 각각 4건이 통과했다. 새 실제 원격 정리 사례 2개에 기존 SIGTERM 정상 drain·SIGKILL 후 proof 부재 차단 사례 2개를 함께 실행했다. 반복된 묶음을 12개의 독립 사례로 합산하지 않는다. ClockWatch는 각각 96·81·83개 표본이 모두 통과했다.

최종 묶음은 정상 판정 문서와 별도로 progress-only 파일을 finally에서 남긴다. Verification completed=false인 중간 기록을 최종 PASS 근거로 쓰지 않는다. API 프로세스는 실제 정상 shutdown 로그와 종료 코드를 확인했고, 의도한 worker SIGKILL과 구분했다.

제품 소스 `faa8897837c5fd2e909e908a7316202ddba66922`, security `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`, PG17.12 arm64 이미지 `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac`를 사용했다. 제품 이미지·Pod·실제 cloud/onprem은 실행하지 않았다. 기존 native 계획의 60초·120초 예산은 유지했고, 5초·8회 클라이언트 부하 자격으로 확대하지 않는다.

원문은 `local-dev-records/effect-settlement-20261119`에 보존했다. 공개 증거에 source hash·실제 effect/epoch·copy audit·원격 관측·HTTP 결과와 허용한 원문 해시를 연결한다. Private spec·DSN·키·인증서 파일은 포함하지 않는다. 전용 컨테이너·네트워크 6개가 모두 없음을 별도 읽기 전용으로 확인했다. 캐시 이미지는 유지했으며 runner에는 caffeinate -di를 사용했다.

## 다음 설계 경계

이 결과는 "워커가 죽었으니 요청도 끝났다"는 가정 대신 원격 정리 근거가 필요함을 보여준다. 다만 실제 Kubernetes verifier를 이 시험으로 우회했거나 운영 게이트가 뚫렸다고 주장하지 않는다. 현행 Pod 상태 관측과 여기서 사용한 privileged DB·relay 관측은 다르다.

다음 제품 단계는 provider별 요청 정리·외부 fencing 근거를 기존 quiescence 계약에 어떻게 연결할지 정하고, UNKNOWN이 명시적 보류로 남도록 운영 API/콘솔과 맞추는 것이다. 실제 provider 신원·DB 역할/세션 결속·proof issuer 권한을 갖춰야 하며 application_name만으로 일반화하지 않는다. 다른 세션의 worker 종료 변경이 정리된 뒤 source를 대조하고 별도 opt-in 변경으로 진행해야 한다.

제품 기본 연결과 pool 승격 보류를 유지한다. 임의 효과 재실행·권한 캐시·게이트 면제·클라이언트 시간 확대를 추가하지 않았다. 운영 proof issuer, 실제 Pod/계정, 독립 사이트, 무중단 SLO와 금융 보안 인증은 여전히 미검증이다.

[시험 계약](../effect-settlement-recovery.md) · [검증 증거](../evidence/effect-settlement-20261119.json).
