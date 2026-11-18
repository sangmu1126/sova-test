# 실제 TLS 지연과 pool 세대 교체 경계 검증

2026-11-19 · 시작 `137e92f836e8316c7fc038666450b3a48d1c35a0`.

## 목적과 변경 경계

직전 표준 pool 시험에서 남긴 네트워크 대기와 이전 세션 drain을 실제로 재현했다. 제품 코드·배포·기본 연결과 기존 pool 구현은 변경하지 않았다. 기존 worker control-plane pool의 대여·reset·종료 경계를 조사하는 qualification 전용 시험이다.

다른 세션은 `9003c9e3f3576080bc2212e2652220e8eded0e22` 이후 volume adapter 및 worker execution 종료 경로를 작업 중이었다. 이번에는 별도 relay·child·lifecycle 시험과 qualification runner·문서만 변경했으며 경로는 겹치지 않았다. 해당 세션의 작업 트리나 미커밋 코드를 가져오지 않았다. 실행 기준은 이번 worktree의 동결 소스다.

## 기능별 커밋과 검증 단계

| 커밋 | 내용 |
|---|---|
| `eef980e` | TLS를 종료하지 않는 loopback relay와 안전한 child 프로토콜, 실제 checkout/reset 지연, 두 역할의 drain·긴급 fencing 시험 |
| `4d37caa` | 새 연결의 실제 쿼리 대조군, relay 시작 전 전용 DB 신원 확인 |
| `615183d` | 지연 중인 자식 종료와 소켓 정리 후 원격 DB backend 소멸 확인 |

세 묶음은 각각 24·25·26건 모두 통과했다. 기존 pool 보안·데이터 회귀 18건을 유지하고 최종적으로 새 lifecycle 사례 8건을 추가했다. 반복되는 시험을 합산해 75개의 독립 사례로 부르지 않는다. 이 중 지연 시험은 **기대했던 제한의 부족을 실제로 관측했기 때문에 통과**한 것이며 네트워크 deadline을 제품에서 해결했다는 뜻이 아니다.

## 실제 네트워크 구성

전용 PG17.12 arm64 이미지 `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac`와 기존 fixture TLS를 사용했다. Relay는 certificate-verified loopback DSN만 받고, 최종 수집기부터 `cluster_name=sova-persistence-test`와 `current_database=sova_test`를 실제 조회한 뒤 시작한다. 임의 로컬 DB나 외부 주소를 대상으로 삼지 않는다.

Relay는 암호화된 바이트를 전달하다 서버→client 방향만 잠시 정지한다. payload·SQL·TLS 평문은 읽거나 기록하지 않는다. 소켓·thread는 문맥 종료 시 정리하며 살아 있는 thread가 남으면 실패한다. Child는 DSN·역할을 private stdin으로 받고, stdout에는 고정 이벤트·PID·시간·오류 class만 출력한다. 키·token·DSN은 command line이나 출력에 넣지 않는다.

공유 cluster·AWS·팀 저장소·실제 고객 Secret은 사용하지 않았다. Pool은 이전과 같은 `psycopg_pool==3.3.3`이며 작업 전용 설치 바이트의 해시를 다시 기록했다.

## 최종 네트워크 관측

| 사례 | 실제 결과 | 해석 |
|---|---|---|
| 정상 TLS relay 대조군 | checkout 및 SELECT 완료 4.445ms | 중계기 자체가 정상 연결을 막지 않음 |
| pool checkout 응답 지연 | 1,302.788ms 동안 완료 없음, 회복 후 1,329.315ms에 대여 완료 | queue timeout 1초는 동기 check의 전체 네트워크 기한이 아님 |
| fresh 연결의 SELECT 응답 지연 | 1,305.977ms 동안 완료 없음, 회복 후 1,326.760ms에 완료 | 동기 I/O 대기는 pool에만 생기는 문제가 아님 |
| reset 응답 지연 | 다음 대여는 1,006.114ms에 PoolTimeout | 초기화가 끝나지 않은 backend를 다시 대여하지 않음 |

응답이 지연된 동안 실제 backend 상태는 `idle in transaction`이었다. Relay가 보류한 TLS 바이트는 checkout 39·fresh query 88·reset 45바이트였고, 내용은 기록하지 않았다. 회복 시험의 child는 모두 자연 종료했으며 시험 guard로 강제 종료하지 않았다.

1.3초는 이 시험의 관측 구간이다. 새로운 제품 timeout이나 5초 SLO 위반 횟수로 사용하지 않는다. 특히 fresh 연결에 1초 query timeout이 선언된 것은 아니다. 정상 연결과 지연 연결을 함께 남겨 동기 check/쿼리 대기에 전체 operation 기한이 필요하다는 점을 확인했다.

## 별도 프로세스 guard 검증

별도 사례는 네트워크를 회복시키지 않고, 지연 상태를 확인한 다음 부모가 1초를 기다린 뒤 전용 child를 종료했다. 종료된 child의 반환 코드는 -15였다. 그 후 relay 소켓을 정리하고 실제 pg_stat_activity에서 해당 PID가 사라졌는지 확인했다.

**지연 확인 후 대기→프로세스 종료→소켓 정리→원격 backend 부재 확인까지 1,054.034ms**였다. 5초 시험 상한 안에 들어왔지만, child 시작·최초 연결부터의 전체 사용자 완료 시간이 아니다. 프로세스가 죽었다는 사실만으로 원격 세션 종료를 추정하지 않았다.

이 사례는 의도적인 guard 동작이다. 일반 회복 시험의 강제 종료 여부와 구분해 증거에 남겼다. Child가 업무 명령에 들어가기 전의 읽기 전용 checkout만 대상이었고 업무 mutation·효과 재시도는 없었다. 제품 watchdog을 배포하거나 mid-effect commit 유실·미확정 결과 복구를 검증한 것은 아니다. 실제 단절된 원격망에서 프로세스 종료만으로 서버 세션이 사라지는지도 이 시험으로 보장하지 않는다.

## 역할 세대 교체

UUID로 만든 이전·새 NOBYPASSRLS runtime role을 사용했다. 두 역할은 같은 전용 fixture client 인증서를 사용하므로 password·인증서·Secret 교체 시험과 구분한다. 순서는 테스트가 직접 조정하며 제품 rotation manager를 만들지 않았다.

| 사례 | 검증한 결과 |
|---|---|
| 이전 pool에 대여 중인 연결 + queued 요청 | NOLOGIN 적용 및 pool.close 뒤 queued 요청은 PoolClosed. 이미 대여된 세션은 계속 SELECT 가능 |
| 정상 drain 후 새 역할 | 이전 연결 반납·실제 backend 부재 확인 후 새 pool을 열고 새로운 역할·PID·실제 schema 조회를 확인 |
| 새 역할의 runtime grant 누락 | 실제 schema 조회가 거부됨. 닫힌 이전 pool을 다시 쓰는 fallback 없음 |
| 긴급 이전 세션 fencing | 해당 세션에서 직접 얻은 PID만 pg_terminate_backend. 이전 연결의 쿼리 거부와 새 역할 연결 확인 |

`pool.close()`가 사용 중인 연결을 반납 시까지 남겨 두는 것은 [공식 계약](https://www.psycopg.org/psycopg3/docs/api/pool.html)과 일치한다. NOLOGIN과 pool 종료만으로 즉시 세션 권한을 없앴다고 보고하면 안 된다. 정상 종료를 기다릴지 기존 세션을 fencing할지 정책을 구분해야 한다.

긴급 fencing에도 업무 mutation을 넣지 않았다. 실제 쓰기 중 세션을 끊으면 commit 여부가 불명확해질 수 있으므로 단순 재실행하면 안 된다. 운영 연결에는 기존 승인·epoch/lease·quiescence·effect 원장·조회 전용 복구 경로를 우선 재사용해야 한다.

## 기존 회귀와 실제 이전

각 묶음의 fresh-before·pooled·fresh-after 네이티브 handoff도 모두 완료됐다. 최종 drive 시간은 각각 4,622.017ms·3,119.555ms·4,708.666ms였다. Pooled 사례는 물리 연결 2개로 258회 대여했으며 기존 데이터 orders·work_items·idempotency_results 각각 50행을 보존했다. 모든 handoff는 효과 전이 21건·authority 이력 10건·검증 참조 16건을 유지했다.

이번 수치는 네트워크 회복 능력의 성능 비교나 통계적 향상이 아니다. [직전 pool 실험](control-pool-2026-11-19.md)의 느린 pooled 실행과 최초 fixture 실패도 계속 보존한다. 기존 18건에서 확인한 RLS·LOGIN/CONNECT/승인·서명자 신뢰 회수·epoch·rollback·실패 reset 폐기 등의 검증을 이 묶음에서도 유지했다.

제품 소스는 `faa8897837c5fd2e909e908a7316202ddba66922`, security는 `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`다. 제품 이미지·Pod나 HTTP 부하를 실행한 것은 아니다. Native fixture의 합성 가용성 예산 60초·120초는 그대로이며 실제 클라이언트의 5초·8회 기준을 통과 처리하지 않는다. ClockWatch는 묶음별 65·73·64개 표본 모두 통과했다.

## 결정과 다음 경계

운영 pool 승격은 계속 보류한다. 풀 자원 대기, 현재 callback/쿼리의 I/O 기한, 프로세스 종료, 원격 세션 소멸, 진행 중인 효과의 복구는 서로 다른 조건이다. 이번에 확인한 guard와 세대 분리를 운영 기능으로 표시하지 않는다.

다음 작업은 현재 워커의 프로세스 감독·quiescence·복구 경로에 전체 단계 기한을 연결하는 설계를 고정하고, 실제 효과 실행 중 응답 유실/정지에서 결과를 조회해 복구하는 경로를 검증하는 것이다. 다른 세션이 수정 중인 worker 종료 코드와 겹치지 않도록 최신 변경과 먼저 맞춰야 한다. 별도 권한 캐시·임의 효과 재시도·시간 예산 확대·게이트 생략으로 해결하지 않는다.

자격증명·인증서의 실제 갱신/회수와 trust-store reload, 여러 worker/Pod·사이트 장애·장시간 누수, 실행 중 commit 모호성, AWS→독립 온프렘·금융 보안·무중단 SLO는 추가 인수 대상이다. 합성 IdP·계획·일부 신뢰 입력·로컬 파일 witness의 한계도 유지한다.

## 증거와 정리

원문은 `local-dev-records/pool-lifecycle-20261119`의 initial·final·guarded에 있다. 증거 JSON은 실행별 소스 해시·case 관측·기존 원장/데이터 결과 및 허용한 원문 해시를 연결한다. 키·인증서·접속 설정·TLS payload는 제외했다.

회복 사례 child는 정상 종료, guard 사례는 의도한 -15 종료를 확인했다. Relay는 모든 전용 소켓·thread를 정리했고, 전용 컨테이너·네트워크 6개의 부재도 별도 읽기 전용으로 확인했다. 캐시 이미지는 유지했다. 시험에는 `caffeinate -di`를 사용했으며 전역 잠자기 설정은 바꾸지 않았다.

[시험 계약](../pool-lifecycle-faults.md) · [실측 증거](../evidence/pool-lifecycle-20261119.json).
