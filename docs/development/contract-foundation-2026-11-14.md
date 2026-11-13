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
