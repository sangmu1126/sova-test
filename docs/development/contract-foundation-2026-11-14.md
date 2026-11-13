# 통합 명세 기반 개인 개발 · 2026-11-14

## 기준

- 사용자 요청: 제공한 통합 명세로 개인 저장소에서 개발 시작.
- 정본: MIG-SPEC-001-CONSOLIDATED 1.1-draft, 작성 기준 2026-11-11.
- 보존 bytes SHA-256: `ffad2d1f49ea4878bf50b9e08d117e9f722e68197e4dde7e3e769c1f6d171d66`.
- 명세 채택 커밋: `4023ec503c43d00c2f58805628c1c5e2c7d80965`.
- 기존 Sova 코드: `22ce74df1cb13293aeb24e36c90a51c2b0da4a15`, 문서 정리 `50b8479`, 전체 콘솔 캡처 `cab8713`을 별도 개인 개발 브랜치에 병합(`6222305`).
- 고정 Security 생산자: `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`, package 0.11.1. 소비 manifest와 운영 신뢰 SHA를 변경하지 않았다.
- 개인 `sangmu1126/sova-test`·`sangmu1126/security`의 rulesets 조회 결과는 빈 목록이었다. root/해당 저장소의 추가 AGENTS.md는 없었다. 브랜치 push 대상은 personal로 명시했으며 팀 원본에 push하지 않는다.

## 구현과 이유

| 기능 | 코드 커밋 | 바꾼 이유 |
|---|---|---|
| 불변 서비스 요구와 C0 구조 검사·CLI | `9c43b5f` | 현재 관측과 고객 요구를 구분하고 정책·가용성 요구를 누락하거나 반올림하지 않도록 함 |
| 방향별 C3 자격 판정 | `602686b` | 제공자 이름·manifest·반대 방향 시험만으로 지원을 광고하지 않도록 함 |
| 기존 JSON decoder의 순수 domain 이동 | `9c43b5f` | domain이 파일 입력 adapter를 import하지 않도록 하며 기존 graph_codec 경로는 유지 |
| 개인 개발용 오프라인 CI | `602686b` | 고정 Security producer로 Python 3.11/3.14 구조·단위 검증. 클라우드 자격·상태 변경 없음 |
| Security 적용 경계 | 개인 security의 별도 문서 변경 | 기존 Evidence·현재 등록·신뢰·보류/해제·실행 승인 책임을 보존하고 신규 보안 정본 복제를 피함 |

새 ServiceContract의 NORMALIZED는 인증된 등록·업무 의미 검증·계획 생성이 아니다. 새 C3 결과 QUALIFIED_CANDIDATE도 해당 방향/operation/한도의 자격 대조일 뿐 전체 계약 충족이 아니다. 모든 결과에 실행·승인 권한 없음이 명시된다. 실제 qualification 인증 adapter는 미연결이다.

## 검증

실행 환경: Mac arm64, 기존 전용 Python 3.12.16 환경. 최종 단위 회귀는 위 **정확한 Security producer** 소스 사본을 사용했다. 처음 최신 5b5be0e 소스에서 한 결과와 고정 producer 재검증을 구분한다.

- `python -m pytest -q tests/unit -rs`: **454 PASS / 4 SKIP**.
- SKIP 1개: `test_consultation_assembly.py`의 `opentelemetry.sdk` 미설치.
- SKIP 3개: `test_otel_collector.py`의 OTLP protobuf 미설치. 새 계약/자격 시험은 SKIP하지 않았다.
- `ruff check src tests`: 통과.
- 실제 `python -m sova` subprocess: 새 계약 생성/검사에서 JSON kind·service ID·비등록·비실행을 검증. 기존 example/inspect/assess도 JSON 내용을 대조했다.
- 새 workflow: Ruby Psych로 YAML 구문 확인. 개인 브랜치 push 후 GitHub runs는 0건이었다. Actions permissions 읽기 전용 조회에서 `enabled=false`를 확인했다. 따라서 Python 3.11/3.14 원격 검증은 설정만 준비됐고 미실행이다. 저장소 Actions/권한 설정은 변경하지 않았다. 확인된 실행 결과는 위 Python 3.12.16 로컬 시험이며 원격 PASS로 합치지 않는다.
- 사용자 원문 파일과 저장소 보존본의 bytes·길이·SHA-256 일치.
- 문서 내부 상대 링크·git diff 형식 검사는 최종 문서 변경 뒤 수행한다.

회귀 반례는 계약 누락/중복/unknown 필드·부정확한 정수·교차 tenant, 익명 qualification·truthy 인증값·auth 예외, 반대 방향·다른 코드/프로필·만료/철회·한도 초과·실행 중 만료·시계 역행, 미평가 Identity 필드 누락 방지를 포함한다. 모든 자격 인증자는 테스트 double이다. LIVE 등급을 넣은 시험 객체는 실제 환경 측정을 뜻하지 않는다.

## 이어서 진행 · 경계 분석과 등록 문서 인증

Codex가 경계 분석 도중 사용량 한도로 멈춘 뒤 Claude Code가 같은 브랜치에서 이어받았다. 남은 미커밋 파일 5개를 그대로 검토·보완해 커밋했다.

| 기능 | 커밋 | 바꾼 이유 |
|---|---|---|
| C1~C2 정합성 경계 후보 | `4c94b34` | 호출 순환이 아니라 공유 쓰기·같은 물리 자원·원자 거래·공유 멱등 상태·통제 범위로 최소 단위를 닫아, 공유 재고나 DB 단위 `PAUSE`를 고객별로 나누지 못하게 함. 검토 중 경계를 넘는 `replicates` 관계가 차단 없이 통과하던 빈틈을 `CROSS_BOUNDARY_REPLICATION_UNRESOLVED`로 막음 |
| 등록 문서의 서명 인증 | 이 커밋 | 자격 판정·경계 분석이 요구하던 `authenticate`를 기존 security 서명 증거·검증자 신뢰 저장소에 연결. 새 서명 체계나 테이블 없이 문서 종류·서비스·프로필 쌍·자원 UID 목록에 결속하고, 검증 뒤 다시 읽어 변경·철회·만료를 놓치지 않게 함 |

| C0~C8·C10~C11 계획 컴파일 | `772017c` | 인증된 계약·경계·방향별 자격을 컴파일러가 직접 다시 판정하고, 실측이 필요한 요구는 실행 게이트 의무로 결속한 인계 DAG와 고정 digest를 만듦. 결과는 승인 제출 가능 여부까지만 표시 |

| C9 내리기·C12 재확인 | 다음 커밋 | 새 승인 체계를 만들지 않고 동결 계획을 기존 `LifecyclePlan`·`VerificationObligation`으로 결정적으로 내림. 실행 직전에 다시 컴파일·내리기를 해서 승인된 계획과 같을 때만 기존 승인 규칙을 적용 |

인증기를 만들면서 신뢰 조회 콜백의 일반 예외(예: DB 연결 오류 문자열)가 정제되지 않고 올라오는 점을 시험으로 발견해 정제된 오류로 바꿨다. 예제 snapshot 생성기는 계약을 인자로 받도록 했다. 생성 뒤 tenant만 바꾸면 그래프 검증이 거부하기 때문이다.

### 검증

- 단위: `tests/unit` **496 PASS / 4 SKIP**. SKIP 4개는 위와 같은 OpenTelemetry 의존성 미설치다.
- 실제 PostgreSQL: 이 Mac의 일회용 컨테이너 PostgreSQL 17.11(`cluster_name=sova-persistence-test`, 127.0.0.1 전용, 시험 뒤 삭제)에 기존 마이그레이션을 적용했다. 새 통합 시험 3개(서명 수집 후 재시작한 저장소에서 인증·경계 PARTITIONABLE, 재서명 모호성과 키 철회 거부, 다른 tenant의 RLS 차단)가 통과했다.
- 전체 회귀: 세션마다 새 DB에서 변경 전 `4c94b34`와 변경 후를 같은 조건으로 돌렸다. 변경 전 **1082 PASS / 37 FAIL / 74 SKIP**, 변경 후 **1098 PASS / 37 FAIL / 74 SKIP**, 새 실패 0건. 같은 DB를 재사용하면 마이그레이션 재적용 오류가 나므로 그 결과는 버렸다.
- 기존 37개 실패는 환경 원인이다. 33개는 이 가상환경의 psycopg 3.3.6에 `ConnectionInfo.ssl_in_use`가 없어 실패했고, 4개는 하위 프로세스를 `python -I`로 띄워 PYTHONPATH의 security 패키지를 찾지 못했다. 이번 변경으로 바뀌지 않았으며 원인 수정은 하지 않았다.
- `ruff check src tests` 통과.

## 이전 기록 정정

2026-11-13 기동 점검에서 사용한 `python -m sova.interfaces.cli`는 실제 entrypoint가 아니다. 종료 코드 0만으로 CLI 기능을 실행했다고 본 부분을 문서에서 정정했다. 올바른 `python -m sova` 경로와 JSON 출력 assertion을 이번에 검증하고 subprocess 회귀 시험을 추가했다. 이전 단위 시험의 `main()` 직접 호출과는 별도다.

## 미검증·다음 접점

- 인증·인가된 ServiceContract/qualification registry와 PostgreSQL CAS 저장은 아직 없다.
- §28.4의 부하·네트워크·failure model 전체 qualification schema와 실제 서명 소비 연결은 후속이다. 현 C3를 범용 운영 지원 행렬로 사용하지 않는다.
- 의존성 coverage·공유 쓰기·정합성 경계와 컴파일 C4~C12, typed 실행 계획 생성은 후속이다.
- 보안 사고 상태 머신·증거 영향 색인·Identity Gate·AI/Harbor·패치·고객별 정책·중앙 단절·HA/DR·범용 데이터 이전의 새 실환경 시험은 수행하지 않았다.
- 원본 제공자의 보안·법규·제품 관련 인용을 이번 원문 보존으로 독립 검증한 것은 아니다.
- 실제 클라우드·VM·팀 공용 앱/DB·운영 권한·승인 호스트는 변경하지 않았다.

[첫 구현 안내](../contract-foundation.md) · [채택 ADR](../adr/0001-contract-driven-foundation.md)
