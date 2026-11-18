# 제어면 연결 pool 실험과 승격 보류

2026-11-19 · 시작 `80e21d33eb0184321c4f776a5143566d1d267c4a`.

## 선택

직전 계측에서 9단계 안의 제어면 연결 생성 시도가 233회였다. 업무 접근 거부 시험은 새 연결이 필요하므로, 제어면 runtime role에 한정한 pool을 qualification 하네스에 연결했다. 제품 `_connect`, 배포 설정·의존성 기본값은 변경하지 않았다. 업무 DB·앱/배치 role·독립 probe 연결은 기존 새 연결을 유지한다.

자체 pool 대신 `psycopg_pool==3.3.3`을 사용했다. [공식 PyPI metadata](https://pypi.org/pypi/psycopg-pool/3.3.3/json)의 wheel SHA-256 `9b9cd6a4fcec47a410f7e82d408540e7f77b478509e91b44c1a5457a13e5ff37`을 requirements에 고정하고 `--require-hashes --only-binary=:all: --no-deps --target`으로 개인 작업 전용 폴더에 설치했다. 기존 가상환경과 다른 세션은 변경하지 않았다. 해시 고정은 취약점 검사나 서명된 릴리스를 뜻하지 않는다.

다른 세션은 Kubernetes volume adapter·probe·volume test를 작업 중이었다. 이번 helper·pool 시험·qualification runner와는 경로가 겹치지 않았다. 해당 worktree와 팀 저장소·공유 클러스터·AWS는 변경하지 않았다.

## 기능별 커밋

| 커밋 | 내용 |
|---|---|
| `e6a9500` | 표준 bounded pool, 대여 시 현재 role/CONNECT/TLS 확인, 반납 시 세션 초기화, native 비교 시험 |
| `b5953ca` | 잘못 구성한 다른 tenant fixture 수정, CONNECT·승인·서명자 신뢰 회수와 실행 epoch 회귀 추가 |
| `56e01e7` | reset callback 실패 시 폐기와 실제 종료 backend 교체 검증 |

풀은 최대 4개·대기열 4개·자원 대기 1초로 제한했다. 접속 생성에는 기존 서비스의 connect_timeout 10초를 사용한다. 새 라이브러리의 연결 재생성은 연결 준비이며 업무 명령이나 효과를 재실행하지 않는다. Pool을 닫은 뒤에는 새 연결로 fallback하지 않는다.

`with connect()` 계약에 맞는 context manager를 제공하며 naked handle을 반환하지 않는다. 대여 시 session/current user 일치·LOGIN·비관리자·NOBYPASSRLS·현재 DB CONNECT·TLS를 실제 조회한다. 기존 repository의 owner/bypass 검사·트랜잭션 단위 tenant 설정·승인·현재 신뢰·lease·epoch 검사는 유지한다.

정상 commit 또는 예외 rollback 후 idle 연결에 `DISCARD ALL`을 수행하고 Python 연결의 isolation/read_only/deferrable·row factory를 초기화한다. 자동 prepare는 비활성화해 서버 초기화와 client prepared cache가 엇갈리지 않도록 했다. 이 구성은 [Psycopg의 reset callback 계약](https://www.psycopg.org/psycopg3/docs/api/pool.html)과 [PostgreSQL의 transaction 밖 DISCARD ALL](https://www.postgresql.org/docs/17/sql-discard.html)을 따른다. Reset 실패는 표준 pool이 연결을 폐기한다.

## 첫 실패와 후속 회귀

첫 실행은 12건 중 11건 통과·1건 실패였다. 다른 tenant용 plan을 만들 때 plan.tenant_id만 바꾸고 step·obligation의 내부 scope를 바꾸지 않아 기존 계약이 `Plan scope crosses tenant or application`으로 거부했다. 동시성 시험이 시작되기 전 fixture 생성 단계 실패다. 제품 RLS가 우회됐거나 pool에서 데이터 유출이 재현됐다는 뜻이 아니다.

`b5953ca`에서 내부 scope까지 일관되게 변경했다. 기존 계약을 완화하지 않았으며 첫 코드 SHA·JUnit·로그·실행 결과를 보존했다. 추가 보안 시험을 포함한 두 번째 묶음 16건, reset/dead backend 시험을 추가한 최종 묶음 **18건 모두 통과**했다. 반복되는 시험이므로 12+16+18을 독립 사례 수로 합산하지 않는다.

| 최종 검증 | 실제 확인한 범위 |
|---|---|
| 세션 초기화 | 같은 backend PID를 다시 빌려도 session tenant·search_path·임시 테이블·prepared statement·advisory lock이 남지 않음 |
| rollback·취소 | SQL 오류·애플리케이션 예외·서버 statement_timeout에 의한 실제 query 취소 뒤 같은 backend에서 정상 쿼리 가능 |
| 연결 속성 | repeatable-read·read-only·deferrable이 다음 대여에 남지 않음 |
| RLS·동시성 | 두 tenant가 동시에 5회씩 무조건 SELECT를 해도 자기 tenant만 보임. 무범위 0행·다른 tenant 계획 조회 거부 |
| 현재 role 권한 | 이미 열린 backend가 있어도 NOLOGIN, BYPASSRLS 부여, DB CONNECT 회수 후 대여 거부 |
| 현재 승인 | warm pool로 읽었던 승인을 다른 연결에서 회수한 뒤 새로운 admission 거부 |
| 현재 신뢰 | 실제 Ed25519 서명을 검증한 뒤 서명자 신뢰를 회수하면 같은 verifier와 pool에서도 검증 거부 |
| epoch·소유권 | 활성 실행자와 충돌 거부, 만료 후 epoch 증가·token 변경, 옛 실행자의 renew/release 거부 |
| reset 실패 | callback 실패를 주입하면 기존 backend가 폐기되고 다음 PID가 달라짐. 오염 tenant 상태 없음 |
| 종료 backend | 실제 pg_terminate_backend 후 caller 쿼리 전에 다른 backend로 교체 |
| 상한·종료 | 1개 pool을 점유하면 다음 대여가 PoolTimeout. close 후 PoolClosed이며 fallback 없음 |

CONNECT 회수 시험은 이번 UUID 전용 DB 서버의 PUBLIC 및 시험 role 권한만 임시 변경하고 finally에서 복구했다. 클라우드·공유 DB 권한은 변경하지 않았다. Query 취소 시험의 20ms statement_timeout은 부정 시험 안에서만 사용하며 운영·클라이언트 제한을 완화하지 않는다.

## 실제 이전 결과

각 묶음은 새 fixture로 비계측 fresh-before→pooled→fresh-after 순서의 실제 네이티브 handoff를 수행했다. 등록·승인·admission API는 기존 실제 loopback HTTP 및 fresh 연결을 사용하고, 비교 대상 워커 제어면만 pool에 연결했다. API 전체가 pool을 사용한 검증으로 확대하지 않는다.

| 묶음 | fresh 이전 drive | pooled drive | fresh 이후 drive | 묶음 상태 |
|---|---:|---:|---:|---|
| initial · `e6a9500` | 7,387.426ms | 19,878.933ms | 6,097.588ms | 동시성 fixture 1건 실패 보존 |
| security · `b5953ca` | 6,843.907ms | 6,371.651ms | 6,789.939ms | 16건 통과 |
| final · `56e01e7` | 5,184.006ms | 2,677.016ms | 5,042.248ms | 18건 통과 |

첫 pooled drive는 양쪽 fresh보다 느렸다. 마지막 표본의 감소만 선택해 개선율을 발표하지 않는다. 반복 실행의 호스트 부하·캐시·스케줄링이 다르며 첫 장시간 구간의 원인도 분리해 측정하지 않았다.

| 묶음 | fresh 이전 pause 준비→route 확인 | pooled 같은 구간 | fresh 이후 같은 구간 |
|---|---:|---:|---:|
| initial | 4,749.176ms | 3,384.008ms | 3,470.674ms |
| security | 4,631.718ms | 2,905.725ms | 3,492.827ms |
| final | 3,493.071ms | 1,798.002ms | 3,135.419ms |

위는 같은 DB의 행 기록 시각이며 commit·정확한 쓰기 봉쇄 효력·최종 사용자 중단 시간이 아니다. 전체 drive와 범위가 달라 두 표의 값을 혼합하지 않는다.

세 pooled 실행 모두 실제 물리 연결 생성은 2회, 대여 요청은 258회, requests_errors는 0이었다. 최종 pool stats의 대기 누적은 7ms, usage는 1,070ms였다. 이 값은 표준 라이브러리의 집계 범위이며 reset·check·background 비용 전체를 뜻하지 않는다. 선행 cProfile의 233회는 `_step` 내부만의 값이므로 258회와 경계가 다르다.

모든 9개 handoff는 HANDOFF_COMPLETE, 효과 전이 21건·authority 이력 10건·검증 참조 16건을 남겼고 목적지 orders·work_items·idempotency_results 각각 50행을 보존했다. 실제 저장된 검사 결과는 모두 pass였다. 첫 묶음의 handoff 성공과 전체 시험 묶음 실패를 구분한다.

## 고정 실행 조건

- 제품 소스 `faa8897837c5fd2e909e908a7316202ddba66922`, security `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`. 제품 이미지나 Pod는 이번에 실행하지 않았다.
- PostgreSQL 17.12 arm64 이미지 `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac`, 전용 DB tmpfs 384MiB·메모리 512MiB.
- 동일 UUID fixture의 별도 DB들, 실제 certificate-verified TLS 및 client 인증서 없는 연결 거부. ClockWatch는 75·68·54개 표본 모두 통과.
- 기존 native fixture 예산 60초 쓰기 정지·120초 완료 대기. HTTP 클라이언트 부하는 없다. 이전의 5초·8회 기준과 소진 기록은 유지하며 이 실행으로 통과 처리하지 않는다.

## 판단: 실험 유지, 운영 기본 반영 보류

표준 pool을 붙여도 현재 확인한 보안·원장·데이터 계약을 유지할 수 있었고, 실제 연결 재사용도 확인했다. 그러나 표본 간 변동이 크고 첫 pooled drive는 느렸으므로 통계적 성능 개선이나 운영 적합성은 발급하지 않는다.

1초 제한은 자원 대기 한도다. 동기 check SQL의 네트워크 blackhole까지 1초 안에 끝나는 보장은 없다. 서버 query 취소와 즉시 종료 backend 시험이 모든 네트워크·사용자 취소 상황을 대체하지 않는다. 자격증명·인증서 교체/회수 때 기존 세션 drain, 역할 멤버십 변경, 회수 직후 경합, 여러 worker/Pod·사이트 장애·장시간 누수·shutdown 중 활성 작업 등은 추가 인수 대상이다.

따라서 제품 main·기본 의존성과 배포는 fresh 연결을 유지한다. 다음은 이 pool 계약을 임의로 완화하는 대신, 완전한 네트워크 대기/취소 경계와 secret rotation drain을 검증한 뒤 opt-in 제품 경로 및 실제 CLI Pod·HTTP 부하 비교를 별도 변경으로 준비하는 것이다. 현재 승인·신뢰 검증을 캐시하거나 5초 예산을 늘리지 않는다.

이 결과는 로컬 네이티브 기능 qualification이다. 합성 계획·IdP·일부 신뢰 입력·로컬 파일 witness의 한계와 AWS→독립 온프렘·금융 보안·무중단 SLO의 미검증 범위를 유지한다.

## 증거와 정리

`local-dev-records/control-pool-20261119`에 initial·security·final 원문을 보존했다. 공개 증거 JSON에는 20개 원문 파일의 SHA-256·크기·경로, 커밋별 소스 해시와 외부 pool 소스 해시, 실제 원장/수치와 첫 실패를 연결했다. 개인 키·인증서·DSN 설정은 제외했다. 원문 해시는 외부 보관·서명된 운영 증명을 대신하지 않는다.

정확한 시험 소유 컨테이너·네트워크 6개가 모두 없음을 별도 읽기 전용으로 확인했다. 캐시 이미지는 유지했다. 실행에는 `caffeinate -di`를 사용했으며 전역 잠자기 설정은 바꾸지 않았다.

[pool 실험 계약](../control-pool-qualification.md) · [실측 증거](../evidence/control-pool-20261119.json) · [선행 계측](worker-step-profile-2026-11-19.md).
