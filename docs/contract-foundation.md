# 계약·능력 기반 개발 · 첫 구현

개발 기준은 [통합 명세 1.1](specifications/contract-driven-migration-platform-v1.1-integrated-notion.md)과 [채택 ADR](adr/0001-contract-driven-foundation.md)이다. 이 문서는 최초 L0/C0·C3 구현만 설명한다. 전체 compiler·등록 API·클라우드 이전 완료 보고서가 아니다.

## 입력 계약

`ServiceContract`는 업무 요구, 접수/완료 의미, 데이터 범위, 가용성, 보안 통제, 위치, 복구·승인·탈출 요구를 보존한다. versioned JSON의 구조를 검사한 뒤 64 KiB 이하의 불변 정규 표현과 SHA-256을 만든다. 중복 키·모르는 필드·누락된 필수 구역·중복 통제·부정확한 숫자·다른 tenant를 거부한다.

수치는 IEEE-754에서 정확히 표현되는 비음수 정수로 제한하며 revision은 양수다. 정밀한 큰 수·금액은 후속 전용 문자열 규칙으로 정의한다. 이 정규화는 기존 `canonical_json`의 제한 프로필이며 RFC 8785/JCS 전체 준수나 업무 의미의 동등성을 주장하지 않는다. 사본을 수정해도 이미 만든 계약의 bytes/digest는 바뀌지 않는다.

정합성 분할은 `verified_partition`을 명시하고 분할 계약과 교차 분할 거래 거부를 요구한다. 이 선언의 사실성은 경계 분석·실측이 추가로 증명해야 한다. 현재 코드가 계약 작성자를 인증하거나 DB에 계약을 등록하지는 않는다.

Identity/사고 보류/증거 기한/사고 복구 확장은 원문과 digest에 보존하지만 현재 의미 판정은 미구현이다. 결과에 `unevaluated_extensions`로 표시하고 C3 후보도 보류한다. 필드를 버리고 유리한 계약으로 바꾸지 않는다.

## 오프라인 사용

루트 README의 설치 절차로 기존에 고정한 `sova-security==0.11.1`과 Sova를 준비한다. 아래 예제는 합성 요구이며 실제 앱·SLO의 측정값이 아니다.

```sh
sova service-contract-example > .local-notes/service-contract.json
sova service-contract-inspect .local-notes/service-contract.json --tenant org-example
```

소스 개발 환경에서는 같은 가상환경과 source 경로로 `python -m sova`를 사용할 수 있다. `python -m sova.interfaces.cli`는 실행 진입점이 아니며 종료 코드만으로 CLI 수행을 확인하면 안 된다. [검토 가능한 예제](../examples/contracts/orders-service.json)

`NORMALIZED`는 구조 검사와 동결 성공이다. 출력의 `registered`, `live_verified`, `executable`은 모두 false다. `--tenant`는 오프라인 범위 대조값이며 실제 사용자 인증이 아니다. 호스팅 registry는 인증된 주체로 tenant를 정하고 객체별 인가·revision CAS·등록 원장을 별도로 제공해야 한다.

## 방향별 능력과 자격

`EnvironmentProfile`은 profile ID/revision, provider, compute backend, data engine, identity, network, architecture, component versions를 독립 축으로 가진다. AWS라고 Kubernetes를 가정하지 않는다. 같은 기술 조합도 profile ID/revision이 바뀌면 기존 결속을 재평가한다.

`CapabilityManifest`는 adapter/strategy의 버전·code digest·지원 operation/profile을 동결한다. manifest에 지원이라고 적는 것만으로 qualification이 생기지 않는다.

`StrategyQualification`은 tenant·service·정확한 계약 digest, manifest digest, 순서 있는 source/target profile, operation 집합, 행 수 상한, 증거 등급, 발급/만료, verifier와 시험 코드·원본 증거 지문을 결속한다. 이는 일반 자격 기록의 **첫 제한 모델**이다. 현재 부하·지연·손실·스토리지·failure model의 모든 축을 구현한 §28.4 전체 자격 레코드로 광고하지 않는다. 추가 축은 profile/contract에 명시적으로 동결하거나 후속 스키마를 정의하고 재자격해야 한다.

`assess_strategy`는 신뢰된 호스트의 `authenticate(record)`와 시계를 요구한다. 인증 경계는 전체 record의 현재 서명·신뢰된 issuer·원본·등록·철회를 검증하고 정확한 True만 반환해야 한다. 이 연결은 아직 운영 registry에 구현하지 않았다. `lambda _: True`나 사용자 JSON을 운영 인증으로 쓰면 안 된다. 오류는 정제된 코드로 반환하며 원격 예외·자격값을 출력하지 않는다.

| 결과 | 의미 |
|---|---|
| QUALIFIED_CANDIDATE | 해당 C3 방향·operation·한도·등급·시점의 자격 인증을 확인. 전체 서비스 계약 충족이나 실행 가능을 뜻하지 않음 |
| NEEDS_EVIDENCE | 자격 누락·만료·다른 방향/계약/이미지·한도 초과·인증 실패·미평가 확장 |
| UNSUPPORTED | 선택한 manifest가 요청 profile 또는 operation을 제공하지 않음. 모든 기술적 대안이 불가능하다는 뜻은 아님 |

모든 결과는 `contract_satisfied=false`, `executable=false`, `authorizes_execution=false`다. `COMPILED` 상태나 실행 명령은 아직 만들지 않는다. 정방향 자격으로 역방향·failback을 추론하지 않으며 local/unit 등급을 live로 바꾸지 않는다.

## 의존성 snapshot과 정합성 경계 후보 (C1~C2)

`DependencySnapshot`은 계약 digest, 기존 `SystemGraph`, 모든 노드의 자원 UID, 보호 데이터셋→노드 매핑, 조사한 경로와 미조사 경로, writer·통제 목록의 완전성, 명시적 결합 제약을 묶어 정규 digest를 만든다. 디코딩한 snapshot은 인증된 것이 아니다.

`analyze_boundary`는 호출 그래프의 순환이 아니라 다음 결합만으로 최소 후보를 전이적으로 닫는다.

| 결합 | 근거 |
|---|---|
| `WRITES` 관계 | writer와 대상 자원을 같은 경계로 묶는다. 두 서비스가 같은 DB에 쓰면 셋이 함께 묶인다 |
| 같은 환경·같은 자원 UID | 이름이 다른 논리 구성요소라도 물리 자원이 같으면 따로 옮길 수 없다 |
| 명시적 제약 | `atomic_transaction`·`shared_writer_authority`·`shared_idempotency_state`·`control_scope`. DB 단위 `PAUSE`처럼 실제 통제 단위가 더 넓으면 `control_scope`로 함께 묶는다 |

읽기 관계의 순환은 묶지 않는다. 대신 경계를 가로지르는 관계를 `boundary_links`로 돌려주고, `calls`·`uses`는 효과 의미가 확인되지 않았으므로 `CROSS_BOUNDARY_EFFECT_SEMANTICS_UNKNOWN`, `replicates`는 복제 채널의 유지·차단·역전이 정해지지 않았으므로 `CROSS_BOUNDARY_REPLICATION_UNRESOLVED`로 보류한다.

| 결과 | 의미 |
|---|---|
| PARTITIONABLE | 요청한 노드가 위 결합으로 닫혀 있고 차단 사유가 없음. 경계 간 관계 정책·방향별 능력·출발지 차단 자격·데이터와 업무 검증은 남은 결정으로 표시 |
| MERGE_REQUIRED | 결합 때문에 요청 밖의 노드가 함께 와야 함. `additional_members`와 `coupling_reasons`로 이유를 돌려줌 |
| NEEDS_EVIDENCE | snapshot 만료·시계 역행, writer/통제 목록 미완료, 미조사 경로, 보호 데이터셋 매핑 누락, 경계 간 효과·복제 미확정, snapshot 인증 실패, `verified_partition` 선언의 실제 집행 미자격 |

조사 공백은 "의존성 없음"으로 승격하지 않는다. snapshot 인증은 다른 차단 사유가 없을 때만 호출하고, 정확한 `True`만 인정하며 예외 내용은 출력하지 않는다. 같은 이름의 자원이 다시 만들어져 UID가 바뀌면 snapshot digest가 바뀌어 기존 인증이 적용되지 않는다. `unit_digest`는 계약·snapshot digest와 정렬한 요청·구성원에 결속한다. 모든 결과는 `candidate_only=true`, `executable=false`, `authorizes_execution=false`다. `MODERNIZATION_REQUIRED`·`UNSUPPORTED`와 경계 간 관계별 유지/브리지/변환 판정은 아직 만들지 않았다.

```sh
sova dependency-example > .local-notes/dependency-snapshot.json
sova boundary-analyze .local-notes/service-contract.json .local-notes/dependency-snapshot.json --tenant org-example --node api-a
```

예제 snapshot은 공유 쓰기 반례이며 일부러 미조사 경로와 미완료 목록을 남긴다. CLI는 인증기를 연결하지 않으므로 결과는 항상 `NEEDS_EVIDENCE`다. 이는 의도된 동작이다.

## 등록 문서의 서명 인증

C3 자격 판정과 C1~C2 경계 분석은 `authenticate(record)`가 정확히 `True`를 돌려줄 때만 다음 상태로 간다. 이 인증기를 새 서명 체계 없이 **기존 security 서명 증거 경로**에 연결했다.

- `registry_binding(document)`: 서비스 계약, 의존성 snapshot, 전략 자격 각각에 대해 서명이 담아야 할 범위·의무·digest를 하나로 정한다. 범위는 `(tenant, service, sova-contract-registry, 문서 종류)`, 의무는 `sova.registry.<종류>.v1`, `artifact_digest`는 문서의 정규 digest다. revision 묶음에는 계약 digest, manifest digest 또는 그래프 digest, 의미 정책 digest, 프로필 쌍 또는 자원 UID 목록 digest를 넣는다. 따라서 한 종류·서비스·프로필 쌍·자원 목록에 대한 서명을 다른 문서에 재사용할 수 없다.
- `attestation_record(...)`: 신뢰된 검증자가 자기 검사를 마친 뒤 서명할 PASS 기록을 만든다. 기록을 만드는 것은 검증이 아니다. 서명 기한은 문서 자체 기한(자격 만료, 그래프 유효기간)을 넘을 수 없다.
- `RegistryAuthenticator`: 문서 digest로 서명 봉투를 정확히 찾고, 결속을 대조하고, Ed25519 서명과 현재 검증자 신뢰(범위·버전·기한·철회)를 확인한다. 그 뒤 봉투와 신뢰를 **다시 읽어** 검증 도중 바뀐 기록, 철회된 키, 기한 만료, 시계 역행이 남지 않게 한다. 캐시하지 않는다. 결과는 정확한 `True` 또는 정제된 오류뿐이며 저장소·네트워크 오류 내용은 밖으로 내보내지 않는다.
- `postgres_registry_authenticator(...)`: 운영 연결. 기존 `PostgresSignedEvidence.find_envelope`와 `PostgresVerifierRegistry.resolve`를 쓰며 새 테이블을 만들지 않는다. 서명 저장은 기존 `SignedEvidenceAuthority.ingest`가 서명·신뢰 확인 뒤에만 수행한다.

같은 문서 digest에 두 번째 서명이 들어오면 정확한 조회가 모호해져 인증이 실패한다. 개별 문서 철회는 이 성질로 닫힌 쪽으로 동작하며, 검증자 키 철회는 기존 신뢰 revision으로 처리한다. 인증된 등록 문서도 실행 권한이 아니다. 운영 registry API, 인증된 등록 주체 확인, 검증자 신뢰를 등록할 관리 절차 연결은 아직 없다.

## 기존 구현 재사용

- JSON decoder를 순수 domain 유틸리티로 옮기고 기존 `graph_codec.json_document` import를 유지했다. domain이 파일/네트워크 adapter에 의존하지 않도록 했다.
- 기존 보안 서명·현재 등록·보류 판정은 security가 소유한다. 새 자격 인증용 host adapter는 기존 신뢰 경계를 소비하도록 후속 구현한다.
- 계획·승인·PostgreSQL 원장·outbox·실행권·효과 조정, 기존 실제 대응 실행기는 변경하지 않았다.
- 첫 후보 조회는 compiler의 전체 C0~C12, all-writer fencing, 데이터 비교, 사고 복구, Cognito 이전, AI/Harbor E2E를 완성하지 않는다.

다음 단계는 C4~C11 계획 컴파일(보안·위치·가용성 제약 검사, typed DAG, 고정 입력 digest)이다. 그 뒤 기존 승인·원장을 연결한다.

## 원격 CI 상태 · 2026-11-14

개인 저장소의 Actions가 `enabled=false`여서 새 브랜치 push 뒤에도 실행이 생성되지 않았다. Python 3.11/3.14 workflow는 준비했지만 원격 통과로 표시하지 않는다. 현재 로컬 Python 3.12.16 시험 결과와 다음 인수 항목은 [개발 기록](development/contract-foundation-2026-11-14.md)을 따른다.
