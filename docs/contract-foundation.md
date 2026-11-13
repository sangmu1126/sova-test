# 계약·능력 기반 인계 · 구현 안내

개발 기준은 [통합 명세 1.1](specifications/contract-driven-migration-platform-v1.1-integrated-notion.md)과 [채택 ADR](adr/0001-contract-driven-foundation.md)이다. 이 문서는 서비스 계약에서 인계 완료·failback·독립성 판정까지, 판정과 기록을 맡는 control plane 구현을 설명한다. 실제 환경 adapter(차단·복사·경로 변경)와 증거 생산자는 없으며, 시험은 합성 입력·로컬 키·일회용 PostgreSQL로 했다. 클라우드 이전 완료 보고서가 아니다.

**전체 흐름.**

1. 서명 등록된 계약·snapshot·자격을 인증한다.
2. 정합성 경계를 정한다(C1~C2).
3. 컴파일한다(C0~C11). 이어서 기존 `LifecyclePlan`으로 내리고 승인한다(C9).
4. 수용한다. 이때 재컴파일 확인(C12), 변경 영향, 보안 보류, 물리 키 잠금을 본다.
5. 단위별 권한 기록을 열고, 다음 단계에만 명령을 낸다. 효과는 기존 효과 원장에서, 증거는 서명 채널에서 받는다. 전이는 권한 DB 밖 witness에도 남긴다.
6. 감독으로 보안 보류와 검증 회귀를 처리한다.
7. 인계를 완료하고, 독립성을 판정한 뒤 원본 폐기 검토로 넘어간다.

실패 경로는 다음과 같다.

- 불명 효과는 보류 뒤 조정한다.
- 계약 위반은 활성화 전이면 보류·중단, 뒤면 failback 계획이다.
- 계획 입력 변경은 재확인하거나 새 계획을 만든다.
- 권한 DB 복원은 복구 incarnation으로 처리한다.

영역별 상태는 [준비 상태 조회](#준비-상태-조회-242-readiness-274)로 본다.

## 운영 절차 요약

한 단위를 인계할 때의 호출 순서다. 괄호는 구현 위치다.

1. **입력 등록.** 서비스 계약, 의존성 snapshot(관리 주체 선언 포함), 방향별 자격을 기존 서명 증거 저장소에 등록한다(`RegistryAuthenticator`가 확인).
2. **후보 탐색과 검토.** `search_plans`로 후보 전략을 모두 컴파일한다. 승인자는 `sova plan-explain <plan> --expect-digest <digest>`로 동결 계획을 검토한다.
3. **내리기와 승인.** `lower_plan`(검증자 버전 지정) → 기존 `PostgresLifecycleRepository.save_plan` → 독립 승인자의 `save_approval`.
4. **수용.** `HandoffAdmission.admit`이 한 트랜잭션에서 다음을 한다.
   - 재컴파일 확인, 변경 영향·수집 공백, 보안 보류 확인
   - 물리 키 잠금, 동결 계획 저장
5. **실행.** `HandoffCoordinator.open` 뒤에 단계마다 다음을 반복한다.
   - `next_command` → 기존 `EffectExecutor.execute`
   - 검증자의 서명 증거 수집
   - `advance_from_ledger`. 플랫폼 단계는 효과 없이 증거로만 진행한다.

   재시작한 실행 주체는 `PostgresCompiledPlans.for_plan`으로 같은 계획을 불러온다.
6. **감시.** 주기적으로 다음을 돌린다.
   - 변경 수집기 폴링
   - `supervise`: 보안 보류와 검증 회귀
   - 활성화 뒤 `SourceFenceWatch.check`
   - `HandoffReadiness.assess`로 상태 확인
7. **완료 뒤.** 다음을 차례로 한다.
   1. 독립성 시험을 실행하고 서명 증거를 남긴다(`ExitReadiness.assess`).
   2. 원본 폐기 검토는 별도 승인으로 한다.
   3. 원본 폐기 전까지 원본 차단 감시를 계속한다.

**실패 시.**

| 상황 | 처리 |
|---|---|
| 효과 불명 | 이전 리스 종료 → 정지 증명 → 복구 리스로 읽기 전용 조정 → `reconcile` |
| 검증 FAIL, 활성화 전 | 계약 위반 보류 → 원인 수정 → 재승인 → `resume_after_review`, 또는 `abort` → `source_restored` |
| 검증 FAIL, 활성화 뒤 | `FAILBACK_REQUIRED` → `compile_failback` → 승인 → 수용(잠금 인계) → 역방향 실행 |
| 보안 보류 | `SECURITY_VIOLATION` 보류 → 기존 `HoldRelease` → 보류 이후 재승인 → `resume_after_review` |
| 계획 입력 변경 | 자원 수준이면 새 snapshot으로 `PlanReaffirmation`. 아니면 새 계획 |
| 권한 DB 복원 | `AuthorityRollbackDetected` → `PostgresHandoffAuthority.recover` → 새 관측으로 조정 |

## 입력 계약

`ServiceContract`는 업무 요구, 접수/완료 의미, 데이터 범위, 가용성, 보안 통제, 위치, 복구·승인·탈출 요구를 보존한다. versioned JSON의 구조를 검사한 뒤 64 KiB 이하의 불변 정규 표현과 SHA-256을 만든다. 중복 키·모르는 필드·누락된 필수 구역·중복 통제·부정확한 숫자·다른 tenant를 거부한다.

수치는 IEEE-754에서 정확히 표현되는 비음수 정수로 제한하며 revision은 양수다. 정밀한 큰 수·금액은 후속 전용 문자열 규칙으로 정의한다. 이 정규화는 기존 `canonical_json`의 제한 프로필이며 RFC 8785/JCS 전체 준수나 업무 의미의 동등성을 주장하지 않는다. 사본을 수정해도 이미 만든 계약의 bytes/digest는 바뀌지 않는다.

정합성 분할은 `verified_partition`을 명시하고 분할 계약과 교차 분할 거래 거부를 요구한다. 이 선언의 사실성은 경계 분석·실측이 추가로 증명해야 한다. 현재 코드가 계약 작성자를 인증하거나 DB에 계약을 등록하지는 않는다.

Identity/사고 보류/증거 기한/사고 복구 확장은 원문과 digest에 보존한다. C0 검사는 의미를 판정하지 않으므로 결과에 `unevaluated_extensions`로 표시하고, 단독 C3 판정도 후보를 보류한다. 필드를 버리고 유리한 계약으로 바꾸지 않는다. 계획 컴파일러는 Identity 확장만 직접 평가한다([아래](#identity-확장-part-ii)). 나머지 확장이 있으면 컴파일 결과도 계속 `NEEDS_EVIDENCE`다.

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
| C4 (Part II) | Identity 확장의 v1 구조와 안전하지 않은 가정 | 아래 Identity 절의 차단 코드 |

실측이 필요한 계약 요구는 통과로 가정하지 않고 실행 게이트의 **검증 의무**로 결속한다.

| 의무 | 결속 위치 |
|---|---|
| 데이터 위치(`residency.<분류>`, 빈 분류 제외) | 목적지 준비. 데이터가 넘어가기 전 |
| 원본 쓰기 거부(등록된 모든 writer 경로, 독립 probe) | 원본 차단 |
| 보호 데이터셋 최종 비교, 업무 불변조건 | 최종 상태 검증 |
| 목적지 필수 보안 통제 | 활성화 의도 기록 전 |
| 단일 writer readback, 오래된 경로 쓰기 거부 | 목적지 활성화, 경로 확정 |
| 외부 효과 결과 조회 | 신규 접수 정지 |
| 기존 자동화(Terraform·Operator 등)의 일시 정지(§14.4) | 신규 접수 정지. 플랫폼의 첫 출발지 쓰기 전 |
| 쓰기 접수 정지·완료 대기 예산(ms), 복원 시험 | 안정화 |

전략 템플릿은 §15.2 인계 상태를 따른다. `offline-copy`는 원본 차단 뒤 복사하고, `snapshot-cdc`는 접수 정지 전에 동기화를 시작하고 차단 뒤 최종 변경분을 적용한다. 각 작업은 단위 구성원 기준 읽기/쓰기 집합, 전제·보장 조건, `tenant+execution+operation` 멱등 범위(같은 키에 다른 입력은 거부), 불확실 시 `observe_or_safe_hold`, 복구 경계를 가진다. 활성화 의도 기록은 adapter 명령이 아닌 플랫폼 단계이며, 그때부터의 모든 작업은 `post_activation_intent` 경계다. 활성화 가능성이 생긴 뒤 원본 자동 재개는 금지하고 failback은 별도 계획을 요구한다. 원본 폐기는 이 계획에 없고 별도 승인이다.

C10은 작업 ID 중복, 의존 순서, 단위 밖 자원 참조, 없는 작업을 가리키는 의무, 복구 경계 역전을 검사하며 위반은 상태가 아니라 컴파일러 결함으로 거부한다. C11은 평가 시각을 뺀 정규 문서를 동결한다. 같은 동결 입력이면 평가 시각이 달라도 같은 `plan_digest`다. 계획 유효기간은 자격 만료와 snapshot 유효기간 중 이른 쪽이다.

결과 상태는 `COMPILED`, `REQUIRES_REMEDIATION`, `NEEDS_EVIDENCE`, `UNSUPPORTED` 중 하나다(우선순위도 이 역순: 미지원 > 사전 변경 > 근거 부족). `COMPILED`이면 `approval_available=true`지만 `executable=false`, `authorizes_execution=false`다. C9·C12와 계획 간 충돌은 아래 절에서 다룬다.

**관리권 인수(§14.4).** 의존성 snapshot은 자원별로 그 자원을 쓰는 기존 자동화를 선택적으로 선언한다(`controllers`). 선언하지 않은 snapshot의 digest는 바뀌지 않는다. 단위 구성원에 관리 주체가 있으면 동결 계획이 `controllers.write_ownership`에 그 자동화와 대상 구성원을 명시한다. 따라서 승인자는 그 자동화를 멈추는 것을 계획과 함께 승인한다. 단위 밖 자원의 관리 주체는 넣지 않는다.

관리 주체마다 `controller.<id>.paused` 의무(PREFLIGHT)가 접수 정지 단계에 결속된다. 플랫폼은 첫 출발지 쓰기 전에, 그 자동화가 실제로 멈췄다는 서명 증거를 받아야 진행한다. FAIL이면 계약 위반 보류다. 기존 자동화가 원본 쓰기 차단 정책을 되돌리는 경쟁을 막기 위해서다. 인계 뒤 그 자동화를 원본에 다시 돌리지 않으며, 목적지로 돌리거나 퇴역시키는 것은 별도 승인이다. 관리 주체 목록이 빠짐없는지는 snapshot의 `control_inventory_complete`와 경계 분석이 맡는다.

**후보 탐색(§12.3).** `search_plans`는 고정된 후보 목록(전략별 manifest와 자격)을 같은 인증 입력으로 모두 컴파일한다. 필수 조건을 비용과 상쇄하지 않고 후보를 충족(`COMPILED`), 미충족(`REQUIRES_REMEDIATION`·`UNSUPPORTED`), 근거 부족(`NEEDS_EVIDENCE`)으로 나눈다. 순위는 충족 후보에만 매기며 규칙은 하나다. 쓰기 정지 예산(§16.4)에 비춰, 차단 구간 안에서 전체 데이터를 복사하는 계획을 최종 변경분만 적용하는 계획보다 뒤에 둔다. 추천은 참고일 뿐이며, 승인자는 정확한 계획 digest 하나를 골라 승인한다. 결과 상태는 다음과 같다.

- 충족 후보가 하나라도 있으면 `COMPILED`
- 모든 후보를 판정했는데 충족이 없으면 `NO_FEASIBLE_PLAN`. 주어진 후보 집합 안의 결론이며, 기술적으로 불가능하다는 주장이 아니다.
- 근거 부족·잘못된 입력·탐색 상한(16개) 초과로 판정하지 못한 후보가 있으면 `SEARCH_INCOMPLETE`. 충족 후보를 찾았어도 이때는 `search_complete=false`로 표시한다.

요구를 완화하지 않으며(`requirements_relaxed=false`), 완화하려면 계약 revision과 승인이 따로 필요하다.

**승인 전 검토.** 승인자는 승인하라고 받은 digest와 동결 계획의 정규 텍스트로 다음을 실행해 계획을 검토한다. 이 명령은 읽기 전용이며 아무것도 승인하지 않는다.

```sh
sova plan-explain plan.json --expect-digest <digest>
```

파일이 정규형이 아니거나(같은 내용이라도 들여쓴 JSON 등), digest가 다르거나, 크기 한도(256 KiB)를 넘으면 거부한다. 설명에는 다음이 들어간다.

- 단계별 행동 쪽(출발지·목적지·플랫폼·edge), 복구 경계, adapter 작업, 전제·보장, 필요한 증거와 게이트
- 되돌릴 수 없게 되는 첫 단계
- 물리 잠금 키
- 독립성 시험
- 복구 정책
- failback이면 정방향 계획·epoch·사유·원래 자원

## 기존 승인·명령 계약으로 내리기 (C9) 와 실행 직전 재확인 (C12)

컴파일러 밖에서 넘어온 계획 dict는 믿지 않는다. `compile_revision`이 동결 계획 객체와 결과를 함께 돌려주며, 이 객체만 내릴 수 있다. 동결 계획에는 단위 구성원의 종류·환경·자원 UID와 출발/목적지 프로필 ID도 들어간다.

`lower_plan`은 동결 계획을 기존 `LifecyclePlan`(security 계약)으로 결정적으로 바꾼다.

- 각 작업이 하나의 `PlanStep`이 된다. 범위는 `(tenant, service, 작업이 다루는 환경, unit-<digest>)`이고, 원본 차단·접수 정지는 출발지, 준비·복사·검증·활성화는 목적지, 경로 확정은 `edge`, 활성화 의도·준비 확인·안정화는 `sova-control-plane`이다. 플랫폼 단계의 adapter는 `sova.control-plane`이며 아직 실행 구현이 없다.
- 매개변수에는 컴파일 계획 digest, 작업 ID, 단계, adapter 작업과 manifest digest, 읽기/쓰기 집합, 전제·보장, 필요한 증거, 멱등 범위, 불확실 시 처리, 복구 경계가 그대로 들어간다. `expected_revision`은 계획이 근거한 snapshot digest다. 실제 자원 revision 대조는 실행 adapter가 다시 해야 한다.
- 검증 의무는 기존 `VerificationObligation`과 게이트로 옮긴다(위치 → REQUIREMENTS, 원본 차단·외부 효과 → PREFLIGHT, 데이터 → DATA, 업무 → BEHAVIOR, 보안 통제 → PROTECTION, 권한·경로·예산·복원 → POSTFLIGHT). 의무 종류마다 검증자 버전을 **반드시 이름으로** 지정해야 하며 기본값은 없다.
- 목적은 `MIGRATION_PHASE`, `origin_plan_digest`는 컴파일 계획 digest, revision 묶음은 계약·manifest·자격·snapshot digest다. 같은 입력이면 같은 `LifecyclePlan` digest가 나온다.

승인은 기존 `PlanApproval`을 그대로 쓴다. `verify_approved_plan`은 명령을 내기 전에 현재 인증된 입력으로 다시 컴파일하고 다시 내려서, 승인된 계획과 정확히 같을 때만 기존 `require_valid`(digest·목적·작성자≠승인자·기한)를 적용한다. 승인 뒤 계획이 바뀌었거나(AT-10), 같은 이름의 자원이 다시 만들어졌거나(AT-11), 검증자 키가 철회돼 입력이 인증되지 않으면(AT-13) 거부한다. 이 재확인은 아래 수용 단계의 첫 관문이다.

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

수용은 자신이 수용한 동결 계획을 digest 기준으로 그대로 저장한다(`postgres_compiled_plans_v1.sql`, 추가 전용). 실행 쪽은 재시작 뒤 저장된 내린 계획의 `origin_plan_digest`로 이 계획을 불러온다(`PostgresCompiledPlans.for_plan`). 다시 컴파일하면 그사이 바뀐 등록 입력 때문에 다른 계획이 될 수 있기 때문이다. 불러올 때 SHA-256과 문서 안의 tenant를 다시 확인하므로, 저장 행이 바뀌었으면 실행하지 않고 거부한다.

새 마이그레이션 `postgres_handoff_claims_v1.sql`은 테넌트별 강제 RLS를 쓰고 권한을 부여하지 않는다. 실행 역할에는 이 테이블의 `SELECT`·`INSERT`·`UPDATE(released_at,released_by)`와 승인 테이블의 `UPDATE(lock_guard)`만 준다. 수용 결과는 `commands_issued=false`, `authorizes_execution=false`다. 수용 자체는 명령을 내지 않는다. 명령은 아래 권한 기록을 거쳐서만 나오며, 권한 기록은 활성 잠금이 있는 수용된 계획에만 열린다. 해제는 인계가 검증된 종료 상태에 도달한 뒤에만 호출해야 하며, 해제가 복구 결정을 대신하지 않는다.

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

**보안 보류(Part IV).** 기존 security의 보안 보류 이력과 같은 트랜잭션에서 `lock_security_hold_set`으로 보류 집합을 잠그고 활성 보류를 확인한다. 같은 서비스에 활성 보류가 있으면 다음을 모두 거부한다: 계획 수용, 권한 기록 열기, 다음 명령 발급, 진행·중단·원본 복원 전이. `SAFE_HOLD`와 `FAILBACK_REQUIRED`로 가는 안전 쪽 전이만 기록한다. 침해가 의심되는 동안 원본 쓰기를 다시 여는 중단도 막는다. 인계 쪽 `SAFE_HOLD`와 보안 `SECURITY_HOLD`는 합치지 않으며, 보류 해제는 기존 `HoldRelease` 절차(독립 승인자, 재검증 증거)만 할 수 있다. 컴파일은 실행 효과가 없어 보류를 보지 않고, 수용 단계에서 막는다.

**권한 DB 복원 감지(AT-33, §15.6).** 권한 저장소는 커밋하는 모든 권한 기록을 DB 밖의 **witness**에도 남긴다. 같은 트랜잭션 안에서 커밋 직전에 추가하므로, witness 기록이 실패하면 전이도 실패한다. 읽기와 전이마다 저장된 행이 witness의 최신 기록과 같은지 확인한다. DB가 과거 시점으로 복원돼 행이 이전 revision이 되거나 사라지면 `AuthorityRollbackDetected`로 다음을 모두 멈춘다.

- 명령 발급
- 전이
- 같은 계획을 새로 여는 것

복구(`recover`)는 복원된 행을 믿지 않고 witness의 마지막 결정을 기준으로 새 incarnation을 연다.

- epoch를 양쪽보다 높여 이전 incarnation의 명령을 낡게 만든다.
- 진행 중이던 단계는 `SAFE_HOLD`(`AUTHORITY_ROLLBACK_RECOVERY`)로 둔다. 새 관측으로 조정해야만 재개한다. 이것이 "현장 대조 뒤 재개"다.
- 보안·계약 위반 보류의 사유는 낮추지 않는다. 종료 상태는 그대로 둔다.

로컬 구현 `FileAuthorityWitness`는 각 줄에 이전 줄의 SHA-256을 넣는 해시 체인 파일이다. 앞부분 수정·재배열·다른 tenant 기록은 읽을 때 감지한다. 다만 파일은 끝부분이 잘려 나간 것을 스스로 증명할 수 없다. 그래서 운영에서는 권한 DB와 다른 권한 경계의 보존 잠금 저장소(예: WORM 보존 객체 저장소)에 둬야 하며, 그 adapter는 아직 없다. witness 쓰기는 DB 커밋 직전이라, 그 사이 DB 커밋이 실패하면 witness가 앞서 복원으로 오인될 수 있다. 이 경우도 진행을 멈추는 안전한 쪽이며 같은 복구 절차로 해소한다.

**활성화 뒤 원본 차단 감시(AT-32, §15.4~15.6).** 활성화 가능성이 생긴 뒤에는 인계가 끝나도 원본 폐기 전까지 원본이 업무 쓰기를 다시 받으면 안 된다. 누군가 원본 DB를 과거 snapshot에서 복원해 이전 writer의 권한이 되살아나는 경우도 마찬가지다. 플랫폼은 그런 복원을 막을 수 없지만 알아채고 멈춰야 한다. `SourceFenceWatch.check`는 원본 쓰기 거부 의무의 최신 서명 관측을 기존 게이트 규칙으로 판정한다.

- PASS: 차단 유지
- FAIL: 이전 writer 재활성화로 보고 기존 보안 보류 저장소에 사고(`SOURCE_WRITER_REACTIVATED`)를 연다. 범위·사유가 같으면 한 번만 연다. 그 뒤 수용·명령·전이가 막힌다.
- 그 밖: 판단할 수 없음(UNKNOWN). 증거가 없다고 차단된 것으로 보지 않는다.

새 관측을 주기적으로 모으는 것은 호출자가 맡는다. 실제 PostgreSQL에서 인계 완료 뒤 원본 권한을 되살리자 probe가 실제 쓰기 성공을 관측했고, 감시가 사고를 열어 서비스가 보류됐다.

**효과 원장 밖의 결과(§19.1).** 실패는 두 경로로 권한 상태에 반영한다.

- **검증 실패.** 작업 효과가 확인됐어도, 그 작업의 의무에 대해 현재 유효한 서명 증거가 FAIL이면 "아직 미충족"이 아니라 `CONTRACT_BREACH`다. 활성화 전이면 `SAFE_HOLD`, 활성화 의도 뒤면 `FAILBACK_REQUIRED`다. 증거가 없거나 판단할 수 없는(UNKNOWN) 상태는 여전히 미충족일 뿐이다. 따라서 검증기는 아직 관측할 수 없는 상태(예: 경로 전파 대기)를 FAIL이 아니라 UNKNOWN으로 보고해야 한다.
- **감독(`supervise`).** 주기적으로 두 가지를 본다. 활성 보안 보류가 있으면 `SECURITY_VIOLATION`으로 `SAFE_HOLD`가 되며, 이미 다른 사유로 보류 중이면 사유를 보안 위반으로 올린다. 이미 끝낸 작업의 의무가 나중에 FAIL로 바뀌면(회귀) `CONTRACT_BREACH`로 처리한다.

두 사유 모두 자동 조정하지 않는다. 복구는 활성화 전이면 중단, 뒤면 failback 계획이다. 같은 계획을 이어 가려면 검토 뒤 재개(`resume_after_review`, Part V.3-7)를 쓴다. 재개 조건은 다음이 모두 참일 때다.

1. 보안 보류가 기존 `HoldRelease` 절차로 해제됐다. 독립 승인자와 재검증 증거가 필요하다.
2. 보류가 시작된 **뒤에** 같은 계획에 대해 철회되지 않은 새 승인이 있다(두 시각 모두 DB 시계). 그 승인이 기존 승인 규칙을 만족한다.
3. 계획 입력이 최신이다.
4. 보류된 단계를 새로 관측했다.

재개도 조정과 같이 새 관측으로만 단계를 결정한다. 종료된 인계는 감독하지 않는다. 완료 뒤의 위반은 운영 사고로 다룬다. 새 전이 함수 `escalate`와 `resume_after_review`도 유계 모델 검사에 사건으로 넣었다. 모델 검사는 세 템플릿 모두에 돈다. offline-copy는 상태 67·전이 454, snapshot-cdc는 상태 83·전이 574, failback-resync는 정방향 `FAILBACK_REQUIRED`에서 컴파일한 계획으로 상태 83·전이 574다. 보류에서 종료가 아닌 상태로 나가는 길은 조정, 검토 뒤 재개, 중단뿐이라는 속성도 추가했고, `escalate`가 보류를 풀도록 결함을 넣으면 검사가 실패하는 것을 확인했다. `PARTIAL_EFFECT`는 기존 효과 원장에 대응 상태가 없어 아직 원장에서 받지 않는다. 실제 환경 adapter와 실제 데이터 이전은 수행하지 않았다.

## Identity 확장 (Part II)

계약의 `identity` 확장은 고정된 v1 구조만 받는다. 모르는 필드, 빠진 필드, 잘못된 타입, 빈 이름은 상태가 아니라 입력 오류로 거부한다. 판정 원칙은 명세 Part II §1이다. 비밀번호 해시·MFA 비밀·refresh token·세션이 사용자 프로필 내보내기와 함께 옮겨진다고 가정하지 않는다. 운영자 로그인은 이 확장의 범위가 아니다.

**전략.** `remove_dependencies`가 비어 있으면 `RETAIN_SOURCE_IDP`다. 이 경우 계속 쓰는 IdP(`allowed_external_idps`)를 남는 의존으로 동결 계획에 적고, 독립성 증명 의무는 만들지 않는다. 남는 의존을 독립이라고 표시하지 않기 위해서다(IAM-AT-01). 제거할 의존이 있으면 `MIGRATE_IDENTITY`이며, 비밀번호 전환 정책에서 자격 증명 전략(`JUST_IN_TIME_ACCOUNT_BRIDGE`, `RESET_AND_REENROLL`)을 고른다.

**탈출 조항과의 대조.** 인계 뒤에도 의존하는 IdP는 모두 서비스 계약의 `exit.allowed_remaining`에 있어야 한다. 그래서 탈출 조항이 Cognito 제거를 요구하는데 Identity 확장이 Cognito를 유지하면 컴파일하지 않는다(IAM-AT-02). 이전할 때 제거하는 IdP는 `exit.remove`에도 있어야 하며, 나중에 독립성을 시험할 기준이 이 탈출 조항이기 때문이다.

**차단.** 다음 가정이 있으면 컴파일하지 않는다.

| 조건 | 코드 | 결과 |
|---|---|---|
| refresh token 이식을 가정 | `REFRESH_TOKEN_PORTABILITY_ASSUMED` | 사전 변경 |
| 교차 tenant 거부를 요구하지 않음 | `CROSS_TENANT_DENIAL_REQUIRED` | 사전 변경 |
| 이중 발급자 허용인데 기간이 없거나 168시간 초과(`PT<n>H`만 허용) | `DUAL_ISSUER_WINDOW_UNBOUNDED` | 사전 변경 |
| 이중 발급자를 쓰지 않는데 기간을 적음 | `DUAL_ISSUER_DURATION_WITHOUT_DUAL_ISSUER` | 사전 변경 |
| 이전인데 안정적 주체 매핑을 요구하지 않음 | `STABLE_SUBJECT_MAPPING_REQUIRED` | 사전 변경 |
| 알 수 없는 비밀번호 전환 정책(예: 해시 복사) | `CREDENTIAL_STRATEGY_UNSUPPORTED` | 미지원 |
| MFA 필수인데 재등록을 허용하지 않음(MFA 비밀 이전 가정) | `MFA_TRANSFER_UNSUPPORTED` | 미지원 |
| 재등록을 허용하지만 사용자 안내 승인이 없음 | `USER_COMMUNICATION_NOT_APPROVED` | 사전 변경 |
| 강제 재인증도 이중 발급자도 허용하지 않음 | `REAUTHENTICATION_FORBIDDEN_WITHOUT_DUAL_ISSUER` | 사전 변경 |
| 인계 뒤 쓸 IdP를 선언하지 않음 | `TARGET_IDP_UNDECLARED` | 사전 변경 |
| 인계 뒤 쓸 IdP가 `exit.allowed_remaining`에 없음 | `IDP_NOT_ALLOWED_BY_EXIT_CONTRACT` | 사전 변경 |
| 제거하는 IdP가 `exit.remove`에 없음 | `IDENTITY_REMOVAL_NOT_IN_EXIT_CONTRACT` | 사전 변경 |
| 제거하는 IdP를 인계 뒤 쓸 IdP로도 적음 | `REMOVED_IDP_STILL_ALLOWED` | 사전 변경 |

**Identity Gate 의무.** 통과하면 다음 의무를 계획 템플릿의 단계에 결속한다. 다른 계약 의무와 같이 서명 증거로만 충족된다.

| 의무 | 결속 단계 | 게이트 | 조건 |
|---|---|---|---|
| IAM-07 비밀·개인정보 누출 없음 | 목적지 준비 | REQUIREMENTS | 항상 |
| IAM-04 교차 tenant 거부 | 활성화 의도 기록 전 | PROTECTION | 항상 |
| IAM-02 새 로그인·토큰 검증, 새 토큰 갱신 | 활성화 의도 기록 전 | PROTECTION | 증거 요구 시 |
| IAM-03 MFA·복구 흐름 | 활성화 의도 기록 전 | PROTECTION | MFA 필수 시 |
| IAM-04 원본·목적지 권한 동등성 | 활성화 의도 기록 전 | PROTECTION | 이전이고 요구 시 |
| IAM-05 이중 발급자 수락 정책 | 경로 확정 | PROTECTION | 이중 발급자 사용 시 |
| IAM-01 주체 매핑 유일성, IAM-06 계정 상태 동등성 | 최종 상태 검증 | DATA | 이전 시 |
| IAM-09 감사·잠금·복구 경로, IAM-12 로그인 SLO | 안정화 | POSTFLIGHT | 항상 |

IAM-08(원본 IdP 차단 상태의 cold start), IAM-10(종료 뒤 이전 발급자 거부), IAM-11(브리지·이중 발급자 퇴역)은 인계 뒤의 독립성 시험이다. 이 계획의 의무가 아니라 동결 계획의 `deferred.exit_contract.identity`에 남기며, 탈출 계약 시험(§20)에서 다룬다.

C0 검사와 단독 C3는 Identity 의미를 판정하지 않는다. 컴파일러가 C4에서 직접 평가한 경우에만 C3가 Identity를 보류 사유에서 뺀다. 사고 보류·증거 기한·사고 복구 확장이 함께 있으면 여전히 `NEEDS_EVIDENCE`다. 실제 IdP 연결, 사용자 내보내기, 로그인 시험 실행은 하지 않는다. 이 단계는 계약이 안전한 가정만 담는지 판정하고, 실측해야 할 항목을 실행 게이트에 묶는다.

동결 계획에 `deferred.exit_contract.identity` 항목이 새로 생겼다(Identity 확장이 없으면 `null`). 그래서 같은 입력이라도 이전 컴파일러의 계획과 digest가 다르다. 그 전에 승인한 계획은 C12 재확인에서 "계획이 바뀜"으로 거부되며, 다시 승인해야 한다. 개인 개발 브랜치 밖에 저장된 승인은 없다.

## 변경 영향 (INV-08, §23.3)

계획과 그 의무를 충족한 증거는 관측한 시점의 세계를 기술한다. 그 뒤에 자원·정책·코드·어댑터가 바뀌면, TTL 안이어도 영향받는 판정과 승인을 다시 봐야 한다. `ImpactIndex`는 동결 계획과 내린 계획에서 무엇이 무엇에 의존하는지 색인한다. `assess_impact`는 기록된 변경 사건으로 세 가지를 판정한다.

1. **계획 입력이 낡았는가.** 계획 snapshot 관측 뒤에 다음이 바뀌면 계획이 낡은 것이다. 이 경우 새 명령을 내지 않으며, 새 snapshot·재컴파일·재승인이 필요하다.
   - 단위 구성원 자원의 재생성·구성·schema·자기 정책(writer 목록 등)
   - 계획이 쓰는 어댑터의 구현
   - 분류 불명 변경
   - 등록소의 계약·manifest·자격·환경 프로필: 최신 기록 revision이 컴파일한 digest와 다르면 낡은 것이다. 같은 시각에 digest가 둘이거나 대상이 불명이어도 낡은 것으로 본다.
2. **어느 의무의 증거가 무효인가.** 의무마다 그 검증이 실제로 관측하는 환경이 있다. 예를 들어 목적지 보안 통제 시험은 플랫폼 단계(활성화 의도 기록 전)에 결속되지만 목적지를 관측한다. 이 환경과, 결속 작업이 읽고 쓰는 단위 구성원으로 영향을 정한다. 영향받는 의무의 증거는 마지막 관련 변경 **이후**에 관측된 것만 인정한다. 변경 종류별 영향은 다음과 같다.
   - 환경 전체 정책 변경: 그 환경을 관측한 모든 증거. 원본이면 원본 쓰기 거부 probe도 포함되므로, 새로 생긴 writer 경로는 재검증에서 잡힌다.
   - 서비스 코드 변경: 계획의 모든 증거
   - 검증자 구현 변경: 그 검증자 버전의 증거
   - Identity 설정 변경: Identity 의무와 그 환경의 증거
3. **수집에 공백이 있는가.** 계획이 의존하는 각 수집 대상의 watermark가 없거나 허용 지연(기본·상한 15분)보다 오래되면 판단할 수 없는(UNKNOWN) 상태다. 수집 대상은 출발지 실제 환경, 목적지, 플랫폼, edge, 등록소다. 이 상태에서는 그 환경을 관측한 증거를 인정하지 않고 명령도 내지 않는다. "변경 없음"으로 보지 않는다.

이 계획 자신의 명령이 만든 변경은 제외한다. 사건의 원인이 이 계획의 **이미 발급된** 단계이고, 그 단계의 쓰기 범위 안이어야 한다(예: 접수 정지가 바꾼 API 접수 정책). 아직 발급하지 않은 단계로 귀속되거나, 쓰기 범위 밖이거나, 다른 계획의 원인을 단 사건은 외부 변경으로 본다. 정밀하게 귀속할 수 없으면 영향을 넓히고 버리지 않는다. 대상이 없는 원본 자원 변경, 모르는 의무 종류, 대상이 불명인 검증자·어댑터 변경이 이 경우다.

**집행 지점.**

- 수용: 재컴파일 확인 뒤, 잠금 전에 계획이 최신이고 수집이 완전해야 한다.
- `next_command`: 같은 조건이 아니면 `PlanReevaluationRequired`(사유 목록 포함)를 낸다.
- 플랫폼 단계 진행: 계획이 낡았거나 불명이면 거부한다. 활성화 의도 기록이 여기에 속한다.
- 어댑터 단계: 이미 일어난 효과이므로 결과는 기록한다. 다만 무효화되지 않은 증거가 있어야 진행한다.
- `HandoffCoordinator.impact`: 현재 판정을 문서로 돌려준다.

수집 저장소가 응답하지 않거나 기록이 상한(4096건)을 넘어도 판단할 수 없는(UNKNOWN) 상태로 처리한다.

**저장(PostgreSQL).** `postgres_change_feed_v1.sql`이 변경 사건과 watermark 두 테이블을 추가한다. 둘 다 강제 RLS이고 권한 부여는 없다.

- 사건은 추가만 한다. 같은 ID의 재전송은 무시하고, 내용이 다르면 거부한다.
- watermark는 환경마다 한 수집기에 속하며 뒤로 가거나 DB 시계보다 앞설 수 없다.
- 늦게 도착한 사건도 기록한다. 영향은 넓어지기만 하기 때문이다.

수집기 역할에는 사건 `INSERT`와 watermark 갱신만, 조정자 역할에는 읽기만 준다.

**한계.** 수집기의 귀속·watermark 주장은 DB 역할 분리로만 보호하며 서명하지 않는다. 귀속을 위조하면 변경을 숨길 수 있으므로, 서명된 수집기 증명은 후속이다. watermark 이후 허용 지연 안의 변경은 볼 수 없다. 그래서 §23.2대로 실행기의 revision 조건·fence와 함께 써야 한다. 실제 클라우드 변경 수집기는 만들지 않았고, 시험은 합성 사건으로 했다.

**변경 사건 규약.** 수집기는 사건을 다음 형태로 기록한다. 환경 ID는 계획이 아는 이름(출발지 실제 환경 또는 프로필 ID, 목적지 프로필 ID, `sova-control-plane`, `edge`, `sova-contract-registry`)이어야 한다. 모르는 환경의 사건은 그 계획에 영향이 없다. 대신 해당 수집 대상의 watermark가 없으면 판단할 수 없는(UNKNOWN) 상태가 되므로 설정 오류는 진행을 막는 쪽으로 드러난다.

| 종류 | environment | subject | digest | 비고 |
|---|---|---|---|---|
| `resource`, `schema` | 자원 환경 | 자원 UID(모르면 비움) | 없음 | 비우면 그 환경 전체로 넓힘 |
| `policy` | 환경 | 구성원 자원 UID 또는 비움 | 없음 | 구성원 정책이면 계획 낡음 |
| `identity` | 환경 | 선택 | 없음 | Identity 의무 포함 무효화 |
| `code` | 선택 | 선택 | 없음 | 서비스(application) 기준 |
| `vulnerability` | 환경 또는 비움 | 구성요소(선택) | 없음 | 그 환경의 보안 관련 증거만 무효화(보안 통제, 쓰기 거부, 경로, 권한 readback, Identity, 독립성 시험). 계획은 낡지 않음: 재진단이지 중단이 아님(§23.3, V.3-8). `postgres_change_feed_v3.sql` |
| `verifier` | 선택 | 검증자 버전(모르면 비움) | 없음 | |
| `adapter` | 선택 | adapter ID(모르면 비움) | 없음 | 계획 낡음, 재확인 불가 |
| `contract` | 선택 | 선택 | 새 계약 digest | 서비스 기준. 등록소 사건은 snapshot 이전 것도 읽어, 컴파일한 revision이 기록된 최신이어야 함 |
| `manifest`, `qualification` | 선택 | 전략 ID | 새 digest | |
| `environment_profile` | 선택 | 프로필 ID | 새 digest | |
| `unclassified` | 환경 또는 비움 | 선택 | 없음 | 넓혀서 무효화, 계획 낡음 |

환경을 비운 `resource`·`schema`·`policy` 사건이 구성원 UID를 가리키면 그 구성원 변경으로 본다. 아니면 모든 쪽으로 넓힌다. 환경을 비운 `identity` 사건은 모든 Identity 의무를 무효화한다.

자기 명령이 만든 변경이면 `cause`에 (내린 계획 digest, 단계 ID)를 넣는다.

**PostgreSQL 카탈로그 수집기.** `PostgresCatalogCollector`는 DB마다 테이블 구조(열·타입)와 접근 정책(테이블 권한, DB ACL)의 지문을 폴링마다 비교한다. 달라지면 `schema`·`policy` 사건을 기록한 뒤, 그 폴링의 DB 시각까지 watermark를 올린다. 사건을 먼저 쓰므로 watermark가 기록하지 않은 변경을 완전하다고 주장하지 않는다. 행 쓰기는 카탈로그 변경이 아니므로 보고하지 않는다.

귀속(`cause`)은 계획 단계의 자기 효과 직후에 한 폴링에만 붙인다. 그래도 그 단계의 쓰기 범위 밖이면 외부 변경으로 본다. 운영 수집기는 시점이 아니라 변경에 실린 상관관계 ID로 귀속해야 한다.

로컬 시험에서 인계 자신의 권한 변경(목적지 준비, 접수 정지, 차단, 활성화)은 각 단계에 귀속돼 인계가 끝까지 진행됐다. 인계 도중 다른 사람이 원본 테이블에 열을 추가하자 다음 명령이 `UNIT_RESOURCE_CHANGED:database`로 거부됐다.

**재평가(`PlanReaffirmation`).** 단위 자원·schema·구성원 정책 변경으로 낡은 계획은 더 새로운 서명 snapshot으로 다시 확인할 수 있다. 같은 계약에 대해 경계 분석을 다시 돌려 다음이 모두 같아야 재확인이다. 이때 계획 구조가 그 snapshot 시점까지 현재와 일치한다고 기록한다(`plan_reaffirmations`, `postgres_change_feed_v2.sql`).

- 경계가 `PARTITIONABLE`이고 구성원이 동결 계획과 같다.
- 구성원별 종류·환경·자원 UID가 같다.
- snapshot이 원래보다 새롭다.

그 시점 이전의 자원 수준 변경은 더 이상 계획을 낡게 만들지 않는다. 변경으로 무효화된 증거는 되살리지 않으므로 다시 검증해야 한다. 다음은 재확인되지 않고 새 계획이 필요하다.

- DB 재생성(UID 변화)
- 새 writer 등장: 경계가 `MERGE_REQUIRED`가 된다.
- 서명 없는 snapshot, 더 새롭지 않은 snapshot
- 어댑터·분류 불명·등록 문서 변경

동결 계획 `inputs`에 `snapshot_observed_at`이 추가됐다. 그래서 이전 컴파일러의 계획과 digest가 다르며, 그 전에 승인한 계획은 C12에서 거부된다.

## Failback 계획 (§15.4, §19.3)

활성화 의도 뒤의 계약 위반이나 운영 판단으로 정방향 인계가 `FAILBACK_REQUIRED`가 되면, 중앙 상태를 이전 단계로 되돌리지 않고 **별도로 승인하는 역방향 인계**를 만든다. `compile_failback`이 이 계획을 만든다.

- **전제.** 정방향 권한 기록이 그 컴파일 계획의 기록이면서 `FAILBACK_REQUIRED`일 때만 만들 수 있다. 같은 서비스의 계약이어야 한다. failback의 failback은 새 정방향 인계로 다룬다.
- **방향과 단위.** 출발지는 현재 권한을 가진 정방향 목적지이고 목적지는 정방향 출발지다. 방향이 정확히 반대가 아니면 `FAILBACK_DIRECTION_MISMATCH`(미지원)다. 경계 분석으로 닫힌 구성원이 정방향 단위와 다르면 `FAILBACK_UNIT_MISMATCH`(사전 변경)다. 방향별 자격(C3)은 역방향 `failback-resync` 전략으로 따로 받는다. 이 전략은 정방향 계획 없이 단독으로 컴파일되지 않는다.
- **템플릿 `failback-resync`.** 목적지를 새로 만들지 않고 `verify_return_target`으로 시작한다. 이 단계의 `failback.return_target_identity` 의무(PREFLIGHT)는 원래 출발지 자원이 정방향 계획과 **같은 UID**이고, 여전히 차단돼 있으며, 과거 snapshot에서 복원되지 않았음을 증거로 요구한다(AT-32). 이어서 역방향 동기화, 준비 확인, 접수 정지, 현재 권한 쪽 차단, 최종 변경 적용, 최종 상태 검증, 활성화 의도, 원래 출발지 활성화, 경로 확정, 안정화가 정방향과 같은 게이트로 진행된다. 정방향 목적지가 활성화 뒤 받은 쓰기는 동기화로 돌아오며, 데이터·업무 최종 비교가 통과해야만 회수된 것으로 본다(AT-30). 최종 비교 없이 무손실을 주장하지 않으며, 목적지 데이터에 접근할 수 없으면 이 계획은 진행하지 못하고 별도 DR이 필요하다(AT-31).
- **동결.** 계획의 `failback_of`에 정방향 컴파일 digest, 계획 digest, 단위, epoch, 방향, 원래 자원 목록, failback 사유를 고정한다. 잠금 집합에는 원래 출발지 자원을 물리 UID 기준 쓰기 키로 넣는다. 그래서 다른 계획이 failback 중인 원본 DB를 동시에 잡지 못한다.
- **수용과 잠금 인계.** 수용은 failback 계획에 대해 한 트랜잭션 안에서 다음을 한다.
  1. 정방향 권한 행을 잠그고 `FAILBACK_REQUIRED`인지 확인한다.
  2. 정방향 잠금을 해제하고 감사 기록(`handoff_superseded`)을 남긴다.
  3. failback 잠금을 기록한다.

  보호가 비는 순간은 없다.
- **권한 세대.** failback 권한 기록은 정방향 기록의 epoch+1에서 시작하고 `predecessor`로 정방향 컴파일 digest를 가리킨다(§15.6). 기록을 열 때 다음을 다시 확인한다.
  - 정방향이 아직 `FAILBACK_REQUIRED`인지
  - epoch가 더 높은지
  - 같은 정방향의 다른 failback이 `ABORTED` 외 상태로 있지 않은지

  그래서 한 정방향 인계는 최대 한 번만 권한을 되돌린다. 정방향 기록은 종료 상태로 남는다.

실제 PostgreSQL에서 이 흐름을 시험했다. 정방향 인계가 목적지 활성화 뒤 `FAILBACK_REQUIRED`가 되고, failback을 컴파일·승인·수용했다(잠금 인계 확인). epoch가 이어지는 권한 기록을 열고 서명 증거로 모든 단계를 진행했다. 끝에서 원래 출발지가 다시 권한을 가진 `HANDOFF_COMPLETE`가 됐다. 정방향이 다시 필요하다고 표시돼 있어도 두 번째 failback은 권한을 열지 못하는 것도 확인했다. 실제 역방향 동기화 adapter와 원본 자원 검증기는 없고, 효과는 기록용 대역과 합성 증거다.

## 인계 뒤 독립성 판정 (§20, Part II §9.2)

인계 완료와 독립성은 다른 주장이다. 완료는 목적지가 계획의 게이트를 통과해 업무 권한을 넘겨받았다는 뜻이다. 독립성은 탈출 조항이 제거하라는 의존을 **실제로 막아도** 서비스가 동작한다는 뜻이다. `exit_drills`가 동결 계획의 탈출 조항에서 필요한 시험을 만든다.

| 시험 | 판정하는 수준 |
|---|---|
| 원본 compute를 막고 새 목적지 인스턴스로 업무 수행 | `COMPUTE_INDEPENDENT` |
| `exit.remove`의 의존마다, 그것을 막고 새 기동·신규 업무(AT-35) | `DATA_IDENTITY_INDEPENDENT` |
| Identity 이전 시 원본 IdP를 막고 cold start(IAM-08) | `DATA_IDENTITY_INDEPENDENT` |
| 캐시 없이 새 인스턴스·연결·토큰·비밀 획득 | `RESTART_VERIFIED` |
| 배치·백업·복원·감사·신규 배포 | `OPERATIONS_VERIFIED` |
| 플랫폼 권한 회수 뒤 고객이 배포·비밀 회전·복원(AT-36) | `PLATFORM_EXIT_VERIFIED` |

`PROVIDER_EXIT_VERIFIED`는 앞의 네 수준(compute, 데이터·신원, 재기동, 운영)이 모두 통과해야 통과다. 계약이 제거를 요구하지 않는 수준은 `NOT_REQUIRED`다. 결과는 다음 규칙으로 정한다.

- 수준은 소속 시험이 **모두** 현재 증거로 통과해야 통과다. 하나라도 실패면 실패다.
- 증거가 없거나 만료됐거나 변경으로 무효화됐거나 수집 공백이 있으면 판단할 수 없음(UNKNOWN)이다. 예외 승인(NOT_APPLICABLE)도 독립성을 증명하지 않는다.
- 인계가 `HANDOFF_COMPLETE`가 아니면 모든 수준이 `NOT_STARTED`다.

Identity는 따로 보고한다.

| 상황 | 상태 |
|---|---|
| 확장 없음 | `NOT_APPLICABLE` |
| 원본 IdP 유지 | `SOURCE_IDP_RETAINED`(유지하는 IdP 목록) |
| 이전 중 | `IDENTITY_CUTOVER_IN_PROGRESS` |
| 이전 완료, IAM-08 미실시 | `IDENTITY_VALIDATED` |
| IAM-08 통과 | `SOURCE_IDENTITY_INDEPENDENT` |
| IAM-08 실패 | `IDENTITY_BLOCKED` |
| IAM-08 증거 불명 | `IDENTITY_UNKNOWN` |

IAM-10(종료 뒤 이전 발급자 거부)과 IAM-11(브리지·이중 발급자 퇴역)은 수준이 아니라 퇴역 확인 항목으로 따로 둔다. `exit.allowed_remaining`과 유지하는 IdP는 언제나 `residual_dependencies`로 표시한다(AT-35 잔존 의존 표시).

`ExitReadiness.assess`는 시험 의무를 계획의 목적지 단계와 같은 범위로 만든다. 증거는 인계 증거와 같은 생산자 채널과 기존 `evaluate_gate`로 판정하며, 서명·범위·계획 revision·검증자 버전·기한을 따진다. 시험 의무도 변경 영향 색인에 들어가므로, 시험 뒤 목적지 정책이나 단위 자원이 바뀌면 그 시험은 다시 해야 한다. 독립성이 모두 통과해도 원본 폐기는 `SEPARATE_APPROVAL_REQUIRED`다(INV-10). 결과의 `reviewable`은 폐기 검토를 시작할 수 있다는 표시일 뿐 승인이 아니다.

시험 자체(원본 의존 차단, 플랫폼 권한 회수)는 범위가 제한된 별도 승인 작업이다. 이 서비스는 그 결과만 판정하며 차단을 실행하지 않는다. 실제 환경의 시험 adapter는 없고, 시험은 합성 서명 증거로 했다.

## 준비 상태 조회 (§24.2 readiness, §27.4)

`HandoffReadiness.assess`는 한 단위의 상태를 하나의 "준비됨" 값으로 합치지 않고 영역별로 나눠 보여 준다.

| 영역 | 내용 |
|---|---|
| 실행 | 권한 상태, 소유 환경, epoch, 활성화 가능성, 다음 단계(플랫폼 단계 여부 포함), 보류 사유 |
| 명령 | 허용 여부와 막는 사유 전부 |
| 변경 영향 | 낡음·수집 공백·무효 증거·구조 재확인 시점 |
| 보안 | 활성 보류 |
| 잠금 | 활성 잠금 수 |
| 독립성 | 수준별 상태, Identity, 남는 의존 |
| 복구 | 지금 가능한 복구 |

명령을 막는 사유는 다음과 같으며 모두 나열한다.

- 종료 상태
- 보류·중단 중
- 보안 보류
- 계획 낡음
- 수집 공백
- 잠금 해제됨

복구 선택지는 규칙을 다시 적지 않는다. 권한 모델의 전이 함수(`available_recoveries`)를 실제로 적용해 받아들여지는 것만 고른다. 그래서 콘솔이 보여 주는 선택지와 모델이 받아들이는 전이가 어긋나지 않는다. 선택지는 다음 중에서 나온다.

- 활성화 전 중단, 원본 복원 확인, 조정(불명 효과 보류일 때만), failback 요구
- `FAILBACK_REQUIRED`면 failback 계획 컴파일. failback 계획 자체가 실패했으면 새 정방향 계획.
- 중단 완료 뒤 새 계획

유계 모델 검사의 모든 도달 상태에서 이 도출이 동작하고, 보류·failback 필요·중단 상태에는 항상 선택지가 있음을 검사한다. 보안 보류 중에는 저장소가 안전 쪽 전이만 받으므로 failback 요구만 즉시 가능으로 두고, 나머지는 `after_security_hold_release`로 나눈다. 독립성 판정이 허용할 때만 원본 폐기 검토를 보이며, 폐기는 언제나 별도 승인이다.

## 로컬 PostgreSQL 참조 adapter와 실측 검증 (§16.5)

`PostgresOfflineCopyAdapter`는 두 PostgreSQL DB 사이의 offline copy 인계에서 실제 효과를 내고, 각 단계의 사후조건을 adapter 메모리가 아니라 DB 자체에서 읽는다.

| 단계 | 효과 | 사후조건(readback) |
|---|---|---|
| 목적지 준비 | 테이블 생성, 앱 역할의 목적지 접속·쓰기 회수 | 테이블 존재, 앱 역할 접속·쓰기 불가 |
| 신규 접수 정지 | 원본 DB CONNECT 회수 | 앱 역할이 원본에 새로 접속 불가 |
| 원본 차단 | 등록된 모든 writer 역할(앱·배치 등)의 쓰기·접속 권한 회수, 기존 세션 종료(§15.5) | 모든 writer 역할의 테이블 쓰기 권한 없음, 세션 0, 남은 prepared transaction 없음 |
| 복귀 대상 확인(failback) | 없음(확인) | 원래 출발지 테이블 존재, 접속·쓰기 불가, 세션 0 |
| 동기화 시작 | 차단 전 초기 복사, 테이블별 표시 | 모든 테이블 동기화 표시 |
| 복사, 최종 변경 적용 | 목적지 한 트랜잭션에서 TRUNCATE 뒤 COPY, sequence 위치도 함께(AT-24). 참조 adapter는 CDC 없이 차단 구간 안의 전체 재복사로 최종 변경을 적용한다 | 테이블별 행 수 같음 |
| 최종 상태 검증 | 없음(측정) | 테이블별 행 수·순서 고정 SHA-256, sequence 위치 같음 |
| 목적지 활성화 | 목적지 CONNECT·쓰기 권한 부여 | 목적지 쓰기 가능, 원본은 여전히 불가 |
| 경로 확정 | 경로를 목적지로 | 경로가 목적지 |

모든 동작은 다시 실행해도 결과가 같다. 사후조건이 성립하지 않으면 `UNKNOWN`이다.

`PostgresHandoffProbes`는 adapter와 따로 측정한다. 쓰기 probe는 앱 역할로 접속해 실제 INSERT를 시도하고 항상 되돌린다. 접속 거부나 권한 거부를 거부로 본다. 측정하는 것은 다음과 같다.

- 원본 쓰기 거부, 단일 writer, 오래된 경로 쓰기 거부
- 테이블별 내용 해시 비교
- ORDER-01(고객·멱등 키당 확정 주문 하나와 그 결과 기록)
- 위치 라벨
- 서버의 실제 `ssl` 설정
- 다른 tenant DB 접속 거부
- 빈 DB로 복원한 뒤의 해시 비교
- 쓰기 정지·완료 대기 시간: 효과 원장의 관측 시각으로 잰다.

증거의 artifact digest는 각 관측 결과의 SHA-256이다.

일회용 클러스터에서 다음 시나리오를 시험했다.

1. **전송 암호화를 요구하지 않는 계약.** 원본 주문 50건과 작업·멱등 결과가 실제로 복사된다. 차단 때 열려 있던 앱 세션이 실제로 끊기고, 경로 확정 뒤 목적지에서 새 주문이 기록된다. 원본은 다시 열리지 않는다. 쓰기 정지 시간은 효과 원장 관측 시각으로 실측해 계약 예산과 비교한다. 로컬 시험 계약은 부하가 걸린 시험 기계가 결과를 가르지 않도록 예산을 넉넉히(60초) 둔다. 같은 측정으로 예산을 1ms로 두면 실제 정지 시간이 언제나 넘으므로, 활성화 뒤 검증에서 계약 위반이 되어 failback이 필요해지는 것도 확인했다. 기본 5000ms 예산은 전체 회귀가 도는 바쁜 기계에서 실제로 넘은 적이 있어, 성능 주장은 하지 않는다.
2. **failback(AT-30).** 정방향으로 목적지가 활성화되고 경로가 바뀐 뒤, 앱이 목적지에 새 주문 5건을 쓴다. 운영 판단으로 failback이 필요해지면 역방향 계획을 컴파일·승인·수용(잠금 인계)한다. 역방향으로 설정한 같은 adapter로 실행하면 새 주문이 원래 출발지로 돌아오고, 경로가 원래 출발지를 가리키고, 원래 출발지만 쓰기 가능하다. 권한 기록은 정방향 다음 세대로 이어진다.
3. **활성화 응답 유실(§29.2-8, AT-20).** 목적지 권한을 실제로 부여한 직후 실행기가 응답을 잃게 했다. 이어지는 흐름은 다음과 같다.
   - 효과 원장에 불명이 남고, 활성화 의도 뒤라 권한 기록은 활성화 가능 상태의 `SAFE_HOLD`가 된다.
   - 기존 복구 절차를 그대로 따른다. 이전 실행 리스가 끝나고, 미해결 효과 digest에 결속된 정지 증명으로 복구 리스를 받는다.
   - 같은 명령을 다시 실행하지 않고 DB를 다시 읽어 효과를 확인한다.
   - 단일 writer probe 증거로 조정해 재개하고 완료한다.
4. **snapshot-cdc.** 초기 동기화 뒤, 차단 전에 원본에 들어온 주문이 최종 변경 적용으로 목적지에 반영되고 내용 비교가 통과한다.
5. **독립성(AT-35).** 인계 완료 뒤 원본 DB를 실제로 접속 불가로 만들고, 앱 역할의 새 세션이 목적지에서 쓰기를 마치는지 잰다. 이 결과로 데이터·Identity 독립, 재기동, 운영(빈 DB 복원) 수준이 통과한다. 로컬에서 잴 수 없는 원본 compute 차단과 플랫폼 권한 회수는 증거가 없어 판단할 수 없음으로 남고, 그래서 사업자 탈출도 통과로 표시되지 않는다. 재기동 시험은 새 연결 기준이며, 토큰·비밀 갱신은 이 로컬 프로필에 없다.
6. **writer 목록 누락(§15.5).** 원본에 직접 접속·쓰기 권한을 가진 배치 역할을 adapter의 writer 목록에서 빠뜨린다. 독립 probe는 실제로 존재하는 모든 writer 경로를 시도하므로 배치의 실제 쓰기 성공을 잡는다. 원본 차단 의무가 FAIL이 되어 활성화 전에 계약 위반 보류가 된다. 목록이 완전하면 배치도 함께 차단된다.
7. **prepared transaction.** 접수 정지 직전 앱이 원본에서 `PREPARE TRANSACTION`을 남긴다. 세션을 끊어도 그 트랜잭션은 나중에 커밋될 수 있으므로, 차단 readback이 성립하지 않아 효과 불명 보류가 된다.
8. **플랫폼 중단(INV-07, AT-34).** 인계 완료 뒤 control plane DB 역할을 접속 불가로 만든다. 플랫폼은 권한 기록조차 읽지 못하지만, 앱은 확정된 경로로 목적지에 계속 주문을 기록하고 ORDER-01이 유지된다.
9. **sequence(AT-24).** 원본이 주문 번호 1~10을 발급한 뒤 인계하면, 목적지에서 다음 번호가 1이 아니라 11이다. 행만 복사하면 이미 쓴 번호를 다시 발급한다. 차단 때는 writer의 sequence 사용 권한도 회수한다.
10. **기본 계약.** 로컬 서버는 `ssl=off`라 활성화 의도 기록 전 전송 암호화 probe가 실제로 FAIL한다. 활성화 전 계약 위반 보류가 되고, 목적지 비활성 확인 → 원본 권한 복원 → probe로 원본 쓰기 확인 → `ABORTED`로 끝난다.

로컬 참조 프로필이며, 관리형 DB·네트워크 경로·실제 앱 진입점은 다루지 않는다.

## 완료 표현 (Part V.4)

| 기능 | 상태 | 근거 |
|---|---|---|
| 서비스 계약 C0·Identity 확장 판정 | 모의 시험 통과 | 단위 시험, 합성 계약 |
| 등록 문서 서명 인증 | 모의 시험 통과 | 로컬 Ed25519, 일회용 PostgreSQL |
| 정합성 경계(C1~C2) | 모의 시험 통과 | 합성 의존성 그래프 |
| 계획 컴파일·내리기·재확인(C0~C12) | 모의 시험 통과 | 단위, PostgreSQL 수용 |
| 계획 간 충돌·수용·동결 계획 저장 | 모의 시험 통과 | PostgreSQL 동시 수용 15회 반복 |
| 인계 권한 상태·효과 원장 연결 | 모의 시험 통과 | 유계 모델 검사, PostgreSQL E2E(기록용 adapter) |
| 보안 보류·검증 실패 결과·검토 뒤 재개 | 모의 시험 통과 | PostgreSQL, 기존 보류 해제 절차 |
| 변경 영향·재평가 | 모의 시험 통과 | 합성 변경 사건, PostgreSQL |
| failback 계획 | 모의 시험 통과 | PostgreSQL E2E(기록용 adapter) |
| 독립성 판정 | 모의 시험 통과 | 합성 서명 시험 증거 |
| 권한 DB 복원 감지·복구 | 모의 시험 통과 | 관리자 행 되돌림으로 복원 모사, 파일 witness |
| 준비 상태 조회·계획 검토 명령 | 모의 시험 통과 | 단위, PostgreSQL |
| 활성화 뒤 원본 차단 감시 | 모의 시험 통과 | 로컬 PostgreSQL에서 권한 복원 실측 |
| PostgreSQL 간 offline copy 참조 adapter·실측 probe | 모의 시험 통과 | 로컬 일회용 PostgreSQL에서 실제 권한·세션·데이터 변경과 실측 |
| 클라우드·관리형 DB·실제 앱 진입점용 adapter | 미검증 | 구현 없음 |
| PostgreSQL 카탈로그 변경 수집기 | 모의 시험 통과 | 로컬 PostgreSQL, 시점 기반 귀속 |
| 클라우드 변경 수집기·witness의 WORM 저장소 | 미검증 | 구현 없음 |
| 실제 IdP 이전·로그인 시험 | 미검증 | 판정과 의무 결속까지만 |
| 후보 전략 탐색(`NO_FEASIBLE_PLAN`·`SEARCH_INCOMPLETE`) | 모의 시험 통과 | 등록된 두 템플릿, 합성 자격 |
| 사고 보류·증거 기한·사고 복구 확장 | 지원하지 않음 | 명세가 스키마를 정하지 않음(S.9, ADR 전제). 있으면 `NEEDS_EVIDENCE` |
| 부분 효과(`PARTIAL_EFFECT`) 원장 연결 | 지원하지 않음 | 기존 원장에 상태 없음 |

어느 항목도 실환경 시험 통과가 아니다. 쓰기 정지 시간·복제 손실·세션 연속성은 측정하지 않았으므로 무중단·RPO=0·세션 무중단 이전을 주장하지 않는다.

## 기존 구현 재사용

- JSON decoder를 순수 domain 유틸리티로 옮기고 기존 `graph_codec.json_document` import를 유지했다. domain이 파일/네트워크 adapter에 의존하지 않도록 했다.
- 기존 보안 서명·현재 등록·보류 판정은 security가 소유한다. 등록 문서 인증과 의무 증거 판정은 그 서명 증거·검증자 신뢰 저장소와 `evaluate_gate`를 그대로 쓴다.
- 계획·승인·PostgreSQL 원장·outbox·실행권·효과 조정, 기존 실제 대응 실행기는 변경하지 않았다. 새 테이블(잠금, 권한·이력, 변경 사건·watermark, 구조 재확인)은 별도 마이그레이션이며 모두 강제 RLS이고 권한을 부여하지 않는다.
- 이 구현은 실제 all-writer fencing, 데이터 비교, 사고 복구, Cognito 사용자 이전, AI/Harbor E2E를 수행하지 않는다. 그런 실측은 adapter와 서명 증거 생산자가 맡고, 여기서는 그 결과를 요구하고 판정하는 쪽만 만든다.

## 원격 CI 상태 · 2026-11-14

개인 저장소의 Actions가 `enabled=false`여서 새 브랜치 push 뒤에도 실행이 생성되지 않았다. Python 3.11/3.14 workflow는 준비했지만 원격 통과로 표시하지 않는다. 현재 로컬 Python 3.12.16 시험 결과와 다음 인수 항목은 [개발 기록](development/contract-foundation-2026-11-14.md)을 따른다.
