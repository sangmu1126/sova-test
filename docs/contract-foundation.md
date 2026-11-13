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

## 계획 컴파일 (C0~C8, C10~C11)

`compile_plan`은 첫 일반 실행 프로필 `PLANNED_SINGLE_AUTHORITY`만 컴파일한다. 다른 결과 dict를 믿지 않고 같은 인증기와 신뢰된 시계로 다음을 직접 다시 판정한다.

| 단계 | 내용 | 막히면 |
|---|---|---|
| C0 | 서비스 계약 자체의 등록 서명 | `CONTRACT_REGISTRATION_REQUIRED` |
| C1~C2 | 서명된 snapshot의 경계. 요청 밖 구성원이 필요하면 추가할 구성원을 돌려줌 | `BOUNDARY_MERGE_REQUIRED`(사전 변경), 경계 차단 사유 |
| C3 | 템플릿이 요구하는 adapter 작업 전체에 대한 방향별 자격 | 빠진 작업 목록, 자격 사유 |
| C4 | 판단 가능한 제약: 외부 효과가 멱등 키·결과 조회·불명 시 보류를 모두 갖추지 않으면 무인 실행에서 제외 | `EXTERNAL_EFFECT_NOT_RECONCILABLE` |

실측이 필요한 계약 요구는 통과로 가정하지 않고 실행 게이트의 **검증 의무**로 결속한다.

| 의무 | 결속 위치 |
|---|---|
| 데이터 위치(`residency.<분류>`, 빈 분류 제외) | 목적지 준비. 데이터가 넘어가기 전 |
| 원본 쓰기 거부(등록된 모든 writer 경로, 독립 probe) | 원본 차단 |
| 보호 데이터셋 최종 비교, 업무 불변조건 | 최종 상태 검증 |
| 목적지 필수 보안 통제 | 활성화 의도 기록 전 |
| 단일 writer readback, 오래된 경로 쓰기 거부 | 목적지 활성화, 경로 확정 |
| 외부 효과 결과 조회 | 신규 접수 정지 |
| 쓰기 접수 정지·완료 대기 예산(ms), 복원 시험 | 안정화 |

전략 템플릿은 §15.2 인계 상태를 따른다. `offline-copy`는 원본 차단 뒤 복사하고, `snapshot-cdc`는 접수 정지 전에 동기화를 시작하고 차단 뒤 최종 변경분을 적용한다. 각 작업은 단위 구성원 기준 읽기/쓰기 집합, 전제·보장 조건, `tenant+execution+operation` 멱등 범위(같은 키에 다른 입력은 거부), 불확실 시 `observe_or_safe_hold`, 복구 경계를 가진다. 활성화 의도 기록은 adapter 명령이 아닌 플랫폼 단계이며, 그때부터의 모든 작업은 `post_activation_intent` 경계다. 활성화 가능성이 생긴 뒤 원본 자동 재개는 금지하고 failback은 별도 계획을 요구한다. 원본 폐기는 이 계획에 없고 별도 승인이다.

C10은 작업 ID 중복, 의존 순서, 단위 밖 자원 참조, 없는 작업을 가리키는 의무, 복구 경계 역전을 검사하며 위반은 상태가 아니라 컴파일러 결함으로 거부한다. C11은 평가 시각을 뺀 정규 문서를 동결한다. 같은 동결 입력이면 평가 시각이 달라도 같은 `plan_digest`다. 계획 유효기간은 자격 만료와 snapshot 유효기간 중 이른 쪽이다.

결과 상태는 `COMPILED`, `REQUIRES_REMEDIATION`, `NEEDS_EVIDENCE`, `UNSUPPORTED` 중 하나다(우선순위도 이 역순: 미지원 > 사전 변경 > 근거 부족). `COMPILED`이면 `approval_available=true`지만 `executable=false`, `authorizes_execution=false`다. 환경별 명령으로 내리는 C9와 승인 C12, `NO_FEASIBLE_PLAN`·`SEARCH_INCOMPLETE`·`CONFLICT`(계획 간 잠금 충돌)는 아직 없다.

## 기존 승인·명령 계약으로 내리기 (C9) 와 실행 직전 재확인 (C12)

컴파일러 밖에서 넘어온 계획 dict는 믿지 않는다. `compile_revision`이 동결 계획 객체와 결과를 함께 돌려주며, 이 객체만 내릴 수 있다. 동결 계획에는 단위 구성원의 종류·환경·자원 UID와 출발/목적지 프로필 ID도 들어간다.

`lower_plan`은 동결 계획을 기존 `LifecyclePlan`(security 계약)으로 결정적으로 바꾼다.

- 각 작업이 하나의 `PlanStep`이 된다. 범위는 `(tenant, service, 작업이 다루는 환경, unit-<digest>)`이고, 원본 차단·접수 정지는 출발지, 준비·복사·검증·활성화는 목적지, 경로 확정은 `edge`, 활성화 의도·준비 확인·안정화는 `sova-control-plane`이다. 플랫폼 단계의 adapter는 `sova.control-plane`이며 아직 실행 구현이 없다.
- 매개변수에는 컴파일 계획 digest, 작업 ID, 단계, adapter 작업과 manifest digest, 읽기/쓰기 집합, 전제·보장, 필요한 증거, 멱등 범위, 불확실 시 처리, 복구 경계가 그대로 들어간다. `expected_revision`은 계획이 근거한 snapshot digest다. 실제 자원 revision 대조는 실행 adapter가 다시 해야 한다.
- 검증 의무는 기존 `VerificationObligation`과 게이트로 옮긴다(위치 → REQUIREMENTS, 원본 차단·외부 효과 → PREFLIGHT, 데이터 → DATA, 업무 → BEHAVIOR, 보안 통제 → PROTECTION, 권한·경로·예산·복원 → POSTFLIGHT). 의무 종류마다 검증자 버전을 **반드시 이름으로** 지정해야 하며 기본값은 없다.
- 목적은 `MIGRATION_PHASE`, `origin_plan_digest`는 컴파일 계획 digest, revision 묶음은 계약·manifest·자격·snapshot digest다. 같은 입력이면 같은 `LifecyclePlan` digest가 나온다.

승인은 기존 `PlanApproval`을 그대로 쓴다. `verify_approved_plan`은 명령을 내기 전에 현재 인증된 입력으로 다시 컴파일하고 다시 내려서, 승인된 계획과 정확히 같을 때만 기존 `require_valid`(digest·목적·작성자≠승인자·기한)를 적용한다. 승인 뒤 계획이 바뀌었거나(AT-10), 같은 이름의 자원이 다시 만들어졌거나(AT-11), 검증자 키가 철회돼 입력이 인증되지 않으면(AT-13) 거부한다. 이 재확인은 명령 발급 경로에 아직 연결하지 않았다.

## 계획 간 충돌 (§12.5, AT-38)

`lock_set`은 동결 계획의 기호 읽기/쓰기 집합을 실제 신원 키로 바꾼다. 출발지 자원은 `(환경, 자원 UID)`, 목적지 배치는 `(목적지 프로필, tenant, service, 노드)`, 단위 권한은 구성원 출발지 자원마다, 경로는 `(tenant, service)`다. 같은 키에는 더 강한 모드(쓰기)를 남기고 정렬한 순서를 잠금 획득 순서로 돌려준다.

`detect_conflicts(후보, 활성 계획들)`은 한쪽이 쓰고 다른 쪽이 읽거나 쓰는 키가 있으면 `CONFLICT`다. 이름이 다른 두 서비스가 같은 물리 DB UID를 쓰면 충돌하고, 읽기끼리는 충돌하지 않는다. 해석할 수 없는 참조는 빈 잠금이 아니라 충돌(`SCOPE_UNRESOLVED`, `ACTIVE_SCOPE_UNRESOLVED`)이며, 같은 계획이 이미 활성이면 `SAME_PLAN_ALREADY_ACTIVE`다.

이것은 판정 함수이지 잠금 서비스가 아니다. 호출자는 후보의 잠금 기록과 같은 트랜잭션 안에서 판정해야 하며, 읽고 나서 따로 쓰면 경합이 생긴다. DB 전체와 샤드 같은 부모·자식 범위, 공용 인증·네트워크 정책 겹침은 아직 키로 표현하지 않는다. 그런 겹침은 경계 분석의 `control_scope` 제약으로 단위에 먼저 묶어야 한다.

## 승인된 인계 계획의 수용 (PostgreSQL)

`HandoffAdmission.admit`은 기존 승인과 명령 발급 사이에 들어간다.

1. 저장된 승인과 계획을 기존 `PostgresLifecycleRepository`에서 읽는다. 철회된 승인은 거부한다.
2. `verify_approved_plan`으로 현재 인증된 입력에서 다시 컴파일·내리기를 해 승인된 계획과 같은지 확인한다.
3. 동결 계획의 잠금 집합을 만든다. 해석할 수 없는 범위가 있으면 거부한다.
4. `PostgresHandoffClaims.acquire`가 **한 트랜잭션** 안에서 승인 행을 `FOR SHARE`로 잠가 철회와 경합하지 않게 하고, revision·계획 digest·미철회를 다시 확인한다. 이어서 정렬된 키 순서로 트랜잭션 advisory lock을 잡고, 활성 잠금과 겹치는지 보고, 없을 때만 잠금 행과 감사 기록을 넣는다.

같은 키를 기다리면 상대 트랜잭션이 커밋한 잠금이 다음 문장에서 보이므로, 충돌하는 두 계획이 동시에 수용되지 않는다. 실제 PostgreSQL에서 두 스레드가 동시에 수용을 시도하는 시험을 새 DB로 15번 반복해 매번 하나만 수용됐다. 한 계획은 한 번만 수용되며, 해제한 뒤에도 같은 계획은 다시 수용하지 않는다. 다음 시도는 새 계획이어야 한다.

새 마이그레이션 `postgres_handoff_claims_v1.sql`은 테넌트별 강제 RLS를 쓰고 권한을 부여하지 않는다. 실행 역할에는 이 테이블의 `SELECT`·`INSERT`·`UPDATE(released_at,released_by)`와 승인 테이블의 `UPDATE(lock_guard)`만 준다. 수용 결과는 `commands_issued=false`, `authorizes_execution=false`다. 실행권 발급은 기존 실행 원장이 맡으며 아직 이 수용 결과와 연결하지 않았다. 해제는 인계가 검증된 종료 상태에 도달한 뒤에만 호출해야 하며, 해제가 복구 결정을 대신하지 않는다.

## 인계 권한 상태 (§15)

`AuthorityRecord`는 한 이전 단위의 업무 변경 권한이 어느 환경에 있는지를 기록한다. 기존 실행 원장의 `ExecutionLease`(명령을 수행하는 쪽의 독점 진행권)와는 다른 개념이다.

- `start`는 수용된 계획에 대해 `SOURCE_ACTIVE`, 소유 환경=출발지, epoch 1로 연다.
- `complete_operation`은 계획 순서의 **다음 작업**에 대한 인증된 관측만 받는다. `EFFECT_CONFIRMED`이고 그 작업의 필요 증거가 모두 충족될 때만 해당 단계로 간다. 원본 차단을 마치면 `fence_verified`가 켜진다. 활성화 의도를 기록하면 `activation_possible`이 켜지고 epoch가 오른다. 목적지 활성화를 관측해야 소유 환경이 목적지로 바뀐다. 마지막 작업을 마치면 `HANDOFF_COMPLETE`다.
- `EFFECT_NOT_APPLIED`는 상태를 바꾸지 않는다. `EFFECT_UNKNOWN`·`PARTIAL_EFFECT`·`SECURITY_VIOLATION`, 그리고 활성화 전의 `CONTRACT_BREACH`는 `SAFE_HOLD`로 멈춘다. 활성화 의도 뒤의 `CONTRACT_BREACH`는 `FAILBACK_REQUIRED`다.
- `reconcile`은 보류된 작업을 새로 관측해 확인되면 진행하고, 미적용이면 보류 전 상태로 돌아가며, 여전히 불명이면 보류를 유지한다. 보안 위반·계약 위반 보류는 자동 조정하지 않는다.
- `abort_before_activation`은 활성화 의도가 없고 목적지 비활성이 검증된 경우에만 시작한다. 원본 쓰기 재개가 검증돼야 `ABORTED`다. 활성화 의도 뒤에는 중단과 원본 복원이 불가능하고 `require_failback`만 가능하다.
- 기록 자체가 구조 불변조건을 검사한다. 활성화 의도 없이 목적지가 소유할 수 없고, 검증된 원본 차단 없이 활성화 가능성이 생길 수 없으며, 활성화 가능성과 원본 복원 상태는 함께 있을 수 없다.

**유계 모델 검사.** 시험은 한 컴파일 계획에서 시작해 모든 사건(작업 완료×6가지 결과×증거 유무, 조정, 중단, 원본 복원, failback)을 도달 가능한 모든 상태에 적용한다. 현재 계획에서 상태 67개, 전이 267개다. 각 전이에서 §28.3의 안전 속성을 확인한다: 권한은 최대 하나, 차단 없는 활성화 금지, 불명 뒤 진행 금지, 활성화 가능성 뒤 원본 자동 재개 금지, 모든 작업 확인 후에만 완료, 종료 상태 흡수. 또 모든 도달 상태에서 종료 상태로 가는 길이 있는지(진행 속성) 확인한다. 일부러 결함 두 개(보류 중 진행 허용, 활성화 가능성 뒤 중단 허용)를 넣었을 때 각각 검사가 실패하는 것을 확인했다. 이것은 계획 수준 상태 머신의 검사이며, adapter가 실제 환경에서 이 가정을 지키는지와 TLA+ 수준의 동시성 모델은 별도다.

**저장과 증거 판정.** `PostgresHandoffAuthority`는 단위마다 현재 기록 한 행과 전이 이력을 저장한다(`postgres_handoff_authority_v1.sql`, 강제 RLS, 권한 부여 없음). 모든 변경은 같은 트랜잭션에서 revision CAS와 이력 행을 함께 쓰므로 오래된 조정자가 새 결정을 덮어쓰지 못한다. 활성 잠금이 있는 수용된 계획만 기록을 열거나 바꿀 수 있다. 이전 계획이 종료 상태가 아니면 같은 단위에 새 계획을 열 수 없고, 끝난 계획은 다시 열 수 없다.

`HandoffCoordinator`는 작업이 요구하는 증거를 호출자에게 받지 않는다. `SignedObligationEvidence`가 검증 의무마다 등록된 생산자 채널의 최신 서명 관측을 읽고, Ed25519 서명과 현재 발급자 신뢰를 확인한다. 그 결과를 기존 `evaluate_gate`의 결속 규칙(의무·범위·계획 revision 묶음·검증자 버전·기한)으로 판정하고 PASS만 인정한다. 최신 관측이 FAIL이면 예전 PASS는 쓰이지 않고, 다른 계획 입력에 결속된 증거도 충족하지 않는다. 실제 PostgreSQL에서 다음을 시험했다: 서명 증거로 끝까지 완료, 불명 효과의 보류 저장과 조정, 동시 조정자 중 하나만 성공, 잠금 해제 뒤 변경 거부.

**효과 원장과 명령 발급.** `next_command`는 권한 기록이 가리키는 **다음 adapter 작업**에 대해서만 기존 `LifecyclePlan.command`로 명령을 만든다. 보류·중단·종료 상태와 플랫폼 단계에서는 거부한다. `advance_from_ledger`는 효과 결과를 호출자가 아니라 기존 효과 원장의 그 작업 기록에서 받는다. 이때 다음을 확인한다.

- 기록의 명령이 이 계획 digest·다음 작업 ID·동작·adapter에 결속돼 있는지
- `confirmed_applied`에는 그 명령의 readback 관측이 붙어 있는지
- `prepared`·`in_flight`처럼 끝나지 않은 효과가 아닌지

`confirmed_applied`는 확인, `confirmed_not_applied`는 미적용, `unknown`은 불명으로 대응한다. 활성화 의도·준비 확인·안정화 같은 플랫폼 단계는 외부 효과가 없어 효과 기록을 받지 않고 증거만으로 진행한다.

실제 PostgreSQL에서 기존 운영 작업 등록, 실행 소유권 리스, `EffectExecutor`, 효과 원장을 그대로 써서 승인 → 수용 → 명령 → 원장 readback → 권한 전이로 인계를 끝까지 실행했다(adapter는 기록용 대역). 원본 차단 readback을 불명으로 바꾸면 기존 실행기가 `UncertainEffect`를 내고 원장에 `unknown`이 남는다. 그 기록으로 권한은 `SAFE_HOLD`가 되고, 활성화 가능성은 생기지 않으며, 다음 명령은 발급되지 않는다.

`PARTIAL_EFFECT`·`SECURITY_VIOLATION`·`CONTRACT_BREACH`는 효과 원장 상태가 아니라 검증·사고 경로에서 와야 하며 그 연결은 아직 없다. 실제 환경 adapter와 실제 데이터 이전은 수행하지 않았다.

## 기존 구현 재사용

- JSON decoder를 순수 domain 유틸리티로 옮기고 기존 `graph_codec.json_document` import를 유지했다. domain이 파일/네트워크 adapter에 의존하지 않도록 했다.
- 기존 보안 서명·현재 등록·보류 판정은 security가 소유한다. 새 자격 인증용 host adapter는 기존 신뢰 경계를 소비하도록 후속 구현한다.
- 계획·승인·PostgreSQL 원장·outbox·실행권·효과 조정, 기존 실제 대응 실행기는 변경하지 않았다.
- 첫 후보 조회는 compiler의 전체 C0~C12, all-writer fencing, 데이터 비교, 사고 복구, Cognito 이전, AI/Harbor E2E를 완성하지 않는다.

다음 단계는 수용된 계획만 기존 실행 원장(실행권·epoch·효과)에서 명령을 받을 수 있게 연결하고, 인계 상태 머신(§15.2)의 단계 전이를 원장에 기록하는 일이다.

## 원격 CI 상태 · 2026-11-14

개인 저장소의 Actions가 `enabled=false`여서 새 브랜치 push 뒤에도 실행이 생성되지 않았다. Python 3.11/3.14 workflow는 준비했지만 원격 통과로 표시하지 않는다. 현재 로컬 Python 3.12.16 시험 결과와 다음 인수 항목은 [개발 기록](development/contract-foundation-2026-11-14.md)을 따른다.
