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

provider는 등록된 값만 받는다: `aws`, `gcp`, `azure`, `ncp`(NAVER Cloud Platform), `on_prem`, `local`. 자유 문자열이면 `amazon`·`AWS` 같은 표기 차이가 서로 다른 방향을 만들어 자격이 갈라진다. 등록은 이름일 뿐이다. 해당 provider를 다루는 adapter와 방향별 자격이 없으면 그 방향은 여전히 `UNSUPPORTED`나 `NEEDS_EVIDENCE`다.

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

**후보 탐색(§12.3).** `search_plans`는 고정된 후보 목록(전략별 manifest와 자격)을 같은 인증 입력으로 모두 컴파일한다. 필수 조건을 비용과 상쇄하지 않고 후보를 충족(`COMPILED`), 미충족(`REQUIRES_REMEDIATION`·`UNSUPPORTED`), 근거 부족(`NEEDS_EVIDENCE`)으로 나눈다. 순위는 충족 후보에만 매기며 규칙은 하나다. 쓰기 정지 예산(§16.4)에 비춰, 차단 구간 안에서 전체 데이터를 복사하는 계획을 최종 변경분만 적용하는 계획보다 뒤에 둔다. 추천은 참고일 뿐이며, 승인자는 정확한 계획 digest 하나를 골라 승인한다.

차단 구간에 `apply_final_changes` 단계가 있다는 것만으로는 "최종 변경분만 적용"으로 보지 않는다. adapter가 그 단계를 전체 재복사로 구현할 수 있기 때문이다. 이 저장소의 참조 adapter가 실제로 그렇고, [로컬 측정](#로컬-쓰기-정지-측정-참조값)에서 snapshot-cdc의 정지 시간은 데이터 크기에 비례했다. 그래서 다음 두 가지가 모두 있을 때만 앞에 둔다.

- manifest가 `properties`에 `incremental_final_changes`를 선언한다.
- 자격 기록도 같은 속성을 담는다. 자격 서명은 fingerprint 전체를 덮으므로, 이 주장도 서명된 증거에 결속된다.

어느 하나라도 없으면 차단 구간 안 전체 복사로 센다. 후보마다 판단 근거를 `fenced_window_basis`로 보여 준다.

| 근거 | 뜻 |
|---|---|
| `FULL_COPY_STEP_IN_FENCED_WINDOW` | 차단 구간에 `copy_content`가 있다 |
| `FINAL_CHANGES_NOT_QUALIFIED_INCREMENTAL` | 최종 변경 단계는 있지만 증분이라는 선언·자격이 없다 |
| `QUALIFIED_INCREMENTAL_FINAL_CHANGES` | 선언과 자격이 모두 있다 |
| `FENCED_WINDOW_COPY_UNDECLARED` | 차단 구간에 어느 복사 단계도 없다(전체 복사로 셈) |

속성을 선언하지 않은 manifest·자격의 fingerprint는 속성 도입 전과 같다. 기존 서명은 그대로 유효하다. 속성 어휘는 닫혀 있어 모르는 이름은 거부한다.

결과 상태는 다음과 같다.

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

로컬 구현 `FileAuthorityWitness`는 각 줄에 이전 줄의 SHA-256을 넣는 해시 체인 파일이다. 앞부분 수정·재배열·다른 tenant 기록은 읽을 때 감지한다. 다만 파일은 끝부분이 잘려 나간 것을 스스로 증명할 수 없다. 그래서 운영에서는 권한 DB와 다른 권한 경계의 보존 잠금 저장소에 둬야 한다. witness 쓰기는 DB 커밋 직전이라, 그 사이 DB 커밋이 실패하면 witness가 앞서 복원으로 오인될 수 있다. 이 경우도 진행을 멈추는 안전한 쪽이며 같은 복구 절차로 해소한다.

`S3ObjectLockAuthorityWitness`는 그 보존 잠금 저장소 구현이다. 권한 기록마다 객체 하나를 쓴다. 쓰기는 `If-None-Match`로 한 번만 하고, Object Lock 보존 기간을 건다(기본 compliance 모드). 보존 기간 안의 버전은 버킷 소유자도 지우거나 기간을 줄일 수 없다.

남는 위험은 숨기기다. 버전 없는 DELETE는 삭제 표식을 남겨 일반 목록에서 항목을 숨기고, 조건 없는 PUT은 같은 키에 버전을 하나 더 만든다. 그래서 witness는 일반 목록이 아닌 버전 목록으로 읽는다. 다음 중 하나라도 있으면 답하지 않는다(fail closed).
- 삭제 표식
- 버전이 둘 이상인 키
- 순번의 빈자리
- 깨진 해시 체인
- 잠금 없는 항목
- 잠금이 이미 끝난 최신 항목

권한 저장소는 그때 멈춘다. 자신을 지키는 증거가 변조됐을 때 의도한 반응이다.

쓰고 나면 저장된 버전이 요청한 잠금을 실제로 가졌는지 다시 읽는다. 잠금이 켜지지 않은 버킷은 witness로 받지 않는다.

시험은 로컬 versitygw의 Object Lock 버킷에서 실제로 한다.
- 버전 삭제·보존 단축 시도는 거부된다.
- 삭제 표식·덮어쓰기·동시 추가·잠금 없는 항목·만료된 잠금은 모두 감지한다.
- 권한 DB 복원 시나리오(AT-33)를 이 witness로 다시 통과한다.

결함 5종을 일부러 넣으면 모두 잡는다. AWS S3 Object Lock에서는 아직 시험하지 않았다.

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

C0 검사와 단독 C3는 Identity 의미를 판정하지 않는다. 컴파일러가 C4에서 직접 평가한 경우에만 C3가 Identity를 보류 사유에서 뺀다. 사고 보류·증거 기한·사고 복구 확장이 함께 있으면 여전히 `NEEDS_EVIDENCE`다. 이 판정 단계는 계약이 안전한 가정만 담는지 보고, 실측해야 할 항목을 실행 게이트에 묶는다. 실제 IdP 사이의 계정 이전과 실측은 아래 참조 구현이 한다.

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
| 목적지 준비 | 테이블 생성, writer 역할의 목적지 접속·쓰기 회수 | 테이블 존재, 원본과 열·타입·제약·트리거 동일, 원본이 쓰는 extension 설치(AT-24), writer 접속·쓰기 불가 |
| 신규 접수 정지 | 원본 DB CONNECT 회수 | 앱 역할이 원본에 새로 접속 불가 |
| 원본 차단 | 등록된 모든 writer 역할(앱·배치 등)의 쓰기·접속 권한 회수, 기존 세션 종료(§15.5) | 모든 writer 역할의 테이블 쓰기 권한 없음, 세션 0, 남은 prepared transaction 없음 |
| 복귀 대상 확인(failback) | 없음(확인) | 원래 출발지 테이블 존재, 접속·쓰기 불가, 세션 0 |
| 동기화 시작 | 차단 전 초기 복사, 테이블별 표시 | 모든 테이블 동기화 표시 |
| 복사, 최종 변경 적용 | 목적지 한 트랜잭션에서 TRUNCATE 뒤 COPY, sequence 위치도 함께(AT-24). 참조 adapter는 CDC 없이 차단 구간 안의 전체 재복사로 최종 변경을 적용한다 | 테이블별 행 수 같음 |
| 최종 상태 검증 | 없음(측정) | 테이블별 행 수와 행 순서에 무관한 SHA-256(행 텍스트를 바이트 순으로 정렬해 길이와 함께 해시), sequence 위치 같음 |
| 목적지 활성화 | 목적지 CONNECT·쓰기 권한 부여 | 목적지 쓰기 가능, 원본은 여전히 불가 |
| 경로 확정 | 경로를 목적지로 | 경로가 목적지 |

모든 동작은 다시 실행해도 결과가 같다. 사후조건이 성립하지 않으면 `UNKNOWN`이다.

`PostgresHandoffProbes`는 adapter와 따로 측정한다. 쓰기 probe는 앱 역할로 접속해 실제 INSERT를 시도하고 항상 되돌린다. 접속 거부나 권한 거부를 거부로 본다. 측정하는 것은 다음과 같다.

- 원본 쓰기 거부, 단일 writer, 오래된 경로 쓰기 거부
- 테이블별 내용 해시 비교: 행 순서와 무관한 다중집합 비교. 선언된 키가 고유하지 않아도(예: `idempotency_results`는 고객 하나로 선언) 같은 내용은 같게 판정한다. 처음에는 선언된 키로 정렬해 해시해서, 같은 고객의 행 순서가 실행마다 달라지면 같은 내용도 다르게 보였다
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
10. **DDL 차이(AT-24).** 원본에만 열이나 CHECK 제약이 하나 더 있으면 목적지 준비가 확인되지 않는다. 그래서 첫 단계에서 효과 불명 보류가 되고, 원본은 접수 정지·차단 없이 업무를 계속한다.
11. **기본 계약.** 로컬 서버는 `ssl=off`라 활성화 의도 기록 전 전송 암호화 probe가 실제로 FAIL한다. 활성화 전 계약 위반 보류가 되고, 목적지 비활성 확인 → 원본 권한 복원 → probe로 원본 쓰기 확인 → `ABORTED`로 끝난다.

로컬 참조 프로필이며, 관리형 DB·네트워크 경로·실제 앱 진입점은 다루지 않는다.

### 로컬 쓰기 정지 측정 (참조값)

`tests/perf/test_handoff_pause.py`는 인계를 끝까지 돌리고, 효과 원장 관측 시각으로 두 구간을 잰다.

- 쓰기 정지: 접수 정지부터 목적지 활성화까지. 계약 예산과 같은 정의다.
- 경로 확정까지: 접수 정지부터 경로 확정까지.

단계별로 접수 정지 뒤 몇 ms에 효과가 확인됐는지도 기록한다. 세 테이블(주문·멱등 결과·작업)에 같은 행 수를 넣는다. `SOVA_TEST_PERF=1`일 때만 돈다. 크기는 `SOVA_TEST_PERF_SIZES`(기본 `1000,10000`)로 고른다.

비교하는 세 가지는 다음과 같다.
- offline-copy: 참조 adapter. 차단 구간 안에서 전체를 복사한다.
- snapshot-cdc(참조): 같은 참조 adapter. 최종 변경을 전체 재복사로 반영한다.
- snapshot-cdc(논리 복제): [논리 복제 adapter](#postgresql-논리-복제-adapter-snapshot-cdc). 차단 구간에서는 남은 변경분만 반영한다.

측정 환경(2026-11-15):
- MacBook Air M1(메모리 16GiB, 팬 없음)의 Docker Desktop VM(8GiB) 위 [시험 환경](../infra/test-env/README.md)
- PostgreSQL 17.11 서버 하나에 원본·목적지 DB를 둘 다 둔다. `wal_level=logical`이다.
- Lima VM은 멈춘 상태였다.

| 방식 | 테이블당 행 | 횟수 | 쓰기 정지 ms (최소 / 중앙 / 최대) | 경로 확정까지 ms (중앙) |
|---|---:|---:|---|---:|
| offline-copy | 1,000 | 3 | 1,150 / 1,176 / 1,228 | 1,372 |
| snapshot-cdc(참조) | 1,000 | 3 | 1,202 / 1,221 / 1,236 | 1,452 |
| snapshot-cdc(논리 복제) | 1,000 | 3 | 1,168 / 1,206 / 1,213 | 1,408 |
| offline-copy | 10,000 | 3 | 1,851 / 1,876 / 1,889 | 2,079 |
| snapshot-cdc(참조) | 10,000 | 3 | 1,757 / 1,787 / 1,823 | 1,981 |
| snapshot-cdc(논리 복제) | 10,000 | 3 | 1,703 / 1,760 / 1,882 | 1,973 |
| offline-copy | 100,000 | 3 | 7,461 / 7,797 / 8,064 | 8,001 |

단계별 분해(중앙값, 접수 정지 뒤 ms):

| 방식 | 행 | 차단 확인 | 복사·최종 반영 확인 | 최종 상태 검증 확인 | 활성화 확인 |
|---|---:|---:|---:|---:|---:|
| offline-copy | 10,000 | 172 | 718 | 931 | 1,876 |
| snapshot-cdc(참조) | 10,000 | 164 | 676 | 874 | 1,787 |
| snapshot-cdc(논리 복제) | 10,000 | 185 | 481 | 724 | 1,760 |
| offline-copy | 100,000 | 176 | 2,696 | 3,332 | 7,797 |

각 시각은 adapter의 readback이 끝난 때다. 복사·최종 반영과 최종 상태 검증의 readback은 양쪽 전체 내용 digest를 계산하므로 그 시간도 들어 있다.

읽는 법:
- **논리 복제는 차단 구간의 데이터 이동을 줄였다.** 1만 행에서 최종 반영이 접수 정지 뒤 481ms에 끝났다. 전체 재복사(676~718ms)보다 약 200ms 빠르다.
- **그러나 규모가 커지면 정지 시간은 활성화 전 검증이 지배한다.** 10만 행 offline-copy에서 복사 확인까지 2.7초, 최종 상태 검증 확인부터 활성화까지 4.5초였다. 그 구간에서 재는 것은 다음이다. 모두 목적지 전체 데이터에 비례한다.
  - 데이터 집합별 전체 내용 비교
  - 업무 불변식(ORDER-01)
  - 보안 통제 `tested_restore`: 목적지를 빈 DB로 복원해 비교한다.

  §16.4가 "전체 해시 비교를 짧은 정지 구간에 넣을 수 있는지 실측하라"고 한 지점이다. 차단 구간을 더 줄이려면 다음과 같은 별도 설계가 필요하다. 아직 하지 않았다.
  - 복원 시험을 차단 전 동기화 상태로 옮기기
  - digest를 서버 쪽에서 계산하기
  - §16.4 조건(변경 누락 감지)을 갖춘 증분 검증
- **1천 행에서도 약 1.2초다.** 이 규모에서는 데이터와 무관한 고정 비용이 대부분이다.
- **참조 snapshot-cdc는 정지 시간을 줄이지 못한다.** 최종 반영을 전체 재복사로 하기 때문이다. 이 결과에 따라 [후보 탐색](#계획-컴파일-c0c8-c10c11)은 단계 이름만으로 snapshot-cdc를 앞에 두지 않는다.
- 같은 서버 안 복사라 네트워크 경로가 없다. 운영 환경의 정지 시간을 예측하는 값이 아니다. 성능 주장이나 SLO 근거로 쓰지 않는다.

측정하지 못한 것:
- snapshot-cdc 10만 행(참조·논리 복제 모두). 10만 행 측정 중 Docker Desktop VM이 두 번 비정상 종료했다("virtualization process exited without being asked to").
  - 첫 번째는 offline-copy, 두 번째는 참조 snapshot-cdc를 돌리던 중이었다.
  - 두 번째 종료 뒤 오류 창에서 공장 초기화가 선택되어 Docker 안의 모든 컨테이너·이미지가 지워졌다.
  - 그래서 10만 행은 기본 크기에서 뺐다. 이 기기에서는 다시 돌리지 않는다.
- 같은 날 더 이른 묶음(Lima VM 실행 중, `wal_level=replica`)에서는 참조 snapshot-cdc 10만 행 2회가 12,963·13,199ms였다. 조건이 달라 위 표와 섞지 않는다.

## adapter 공통 적합성 시험

인계 adapter가 어떤 자원을 옮기든 control plane의 안전은 provider와 무관한 성질에 기댄다. `tests/adapter_conformance.py`가 그 성질을 한 번 정의하고, provider별 시험은 *world* 하나만 제공해 같은 시험을 통과해야 한다.

| 성질 | 시험 |
|---|---|
| 삭제 반영 | 초기 복사 뒤 원본에서 지운 기록이 최종 복사 뒤 목적지에도 없다 |
| 재조회는 상태를 바꾸지 않는다 | 모든 동작의 재조회 전후로 내용·권한·남은 쓰기·경로가 같다 |
| 재조회는 실제 시스템만 본다 | 동작을 실행하지 않은 새 인스턴스가 같은 판정을 낸다(응답 유실·실행기 재시작과 같은 조건) |
| 멱등 | 두 번 적용해도 한 번 적용한 상태와 같다 |
| 외부 되돌림 감지 | 차단 해제·목적지 내용 변경·경로 이동을 계획 밖에서 하면 `UNKNOWN` |
| 남은 쓰기 경로 | 차단 전에 열린 쓰기 경로(prepared transaction, 미완료 multipart upload)가 있으면 차단이 확인되지 않고, 운영자가 해소하면 확인된다 |
| 앱만 활성화 | 활성화 뒤 앱만 목적지에 쓰고, 다른 writer는 목적지에서 거부, 원본은 계속 거부 |
| failback 복귀 대상 | 정방향 완료 뒤 원래 출발지가 차단된 채라면 역방향 복귀 확인이 통과, 다시 열리면 `UNKNOWN` |

두 템플릿(offline-copy, snapshot-cdc)의 adapter 단계 순서를 모두 걷는다. world는 쓰기 시도를 데이터를 남기지 않는 방식으로 해야 한다(PostgreSQL은 항상 되돌리는 트랜잭션).

PostgreSQL adapter와 S3 호환 객체 저장소 adapter가 통과한다. 결함을 일부러 넣으면(PostgreSQL 4종, S3 5종) 각각 시험이 실패한다. S3에서 삭제 누락은 처음에 S3 전용 시험만 잡았기에, provider와 무관한 성질로 보고 공통 시험에 올렸다.

## PostgreSQL 논리 복제 adapter (snapshot-cdc)

`PostgresLogicalReplicationAdapter`는 snapshot-cdc를 이름 그대로 구현한다. 데이터는 차단 전에 옮기고, 차단 구간에서는 아직 반영되지 않은 변경분만 적용한다. 그 밖의 단계는 참조 adapter와 같다(목적지 준비, 접수 정지, 차단, 최종 상태 검증, 활성화, 경로 확정).

**동기화 시작(`start_sync`).** 먼저 다음 두 가지를 확인한다. 하나라도 어긋나면 아무것도 만들지 않고, readback이 이유를 보고한다.
- 원본 서버가 `wal_level=logical`이다.
- 단위의 모든 테이블이 UPDATE·DELETE를 발행할 수 있다(primary key, replica identity 인덱스, `REPLICA IDENTITY FULL` 중 하나). 그렇지 않은 테이블을 발행하면 원본 앱의 UPDATE·DELETE가 실패한다.

확인을 통과하면 차례로 만든다.
1. 원본에 단위 테이블만 담은 publication과 logical slot. slot을 초기 복사보다 먼저 만들어야 그 사이 변경이 빠지지 않는다. slot 생성은 진행 중인 트랜잭션을 기다리므로 시간 상한을 둔다.
2. 목적지에 초기 복사 뒤 변경을 계속 따라가는 subscription.

모든 테이블이 준비(`r`) 상태가 되면 끝난다. readback은 publication 테이블 집합·slot·subscription 설정·테이블별 상태를 서버에서 읽는다.

**최종 변경 적용(`apply_final_changes`).** 차단 뒤 다음 순서로 한다.
1. 원본의 현재 WAL 위치를 읽는다.
2. 같은 slot의 `confirmed_flush_lsn`이 그 위치에 이를 때까지 기다린다. §16.2가 요구하는 "같은 publisher·subscription의 최종 체크포인트가 목적지에 적용됨"을 확인하는 것이다. 지연 수치나 양쪽 WAL 위치를 비교하지 않는다.
3. sequence 위치를 옮긴다. 논리 복제는 sequence를 옮기지 않는다.
4. subscription·slot·publication을 지운다.

시간 안에 확인되지 않으면 아무것도 지우지 않는다. 그래서 운영자가 고친 뒤 같은 동작을 다시 실행할 수 있다.

readback은 다음이 모두 성립할 때만 APPLIED다.
- 테이블별 내용 digest와 sequence 위치가 같다.
- 복제 객체가 하나도 남지 않았다. 활성화 때 목적지에 두 번째 writer(apply worker)가 없어야 하고, 원본에 WAL을 붙잡는 slot이 없어야 한다.

**중단.** `start_sync` 뒤 중단하면 `release_replication()`을 호출해야 한다. `replication_released()`가 남은 것이 없음을 서버에서 읽어 확인한다. 그 전까지 slot이 원본 WAL을 붙잡는다. 상한은 서버의 `max_slot_wal_keep_size`뿐이다.

**연결 정보.** `source_conninfo`는 목적지 서버가 원본에 접속하는 정보다. 목적지의 subscription catalog에 저장되므로 비밀번호를 넣지 말고 password file이나 인증서를 쓴다. 증거에는 넣지 않는다.

**시험(로컬, 2026-11-15).** 14개가 첫 실행에 통과했다.
- adapter 공통 적합성 시험 전체: 두 템플릿, 읽기만 하는 readback, 새 인스턴스의 같은 판정, 멱등, 외부에서 되돌린 차단, 보류 중인 prepared transaction, failback 목적지. 업무 쓰기 뒤에는 world가 같은 slot 기준으로 따라잡기를 기다린다. 그래서 비동기 반영이 관측과 경합하지 않는다.
- 차단 전 쓰기·삭제가 목적지에 반영되고, 최종 적용 뒤 복제 객체가 남지 않으며, sequence가 이어진다.
- replica identity 없는 테이블: 아무것도 만들지 않고, 원본 앱의 INSERT·DELETE가 계속 된다.
- 차단 전에 subscription이 멈춘 경우: 최종 적용이 확인되지 않아 복제를 그대로 두고 UNKNOWN이다. 다시 켜면 같은 동작이 완료된다.
- 중단 뒤 slot 해제.
- control plane을 거친 전체 인계: 초기 동기화 뒤 들어온 주문이 반영되고, 단일 writer가 확인된다.

시험이 끝난 뒤 서버에 slot·subscription·walsender가 0개임을 확인했다.

이 adapter의 manifest가 [후보 탐색](#계획-컴파일-c0c8-c10c11)의 `incremental_final_changes`를 선언할 근거는 이 시험과 [측정](#로컬-쓰기-정지-측정-참조값)이다. 실제 자격 기록 발급은 자격 등록 경계(검증자 서명)의 일이며, 여기서는 하지 않았다.

로컬 서버 하나 안의 두 DB 사이에서만 시험했다. 다음은 다루지 않았다.
- 서버 간 네트워크 경로
- 관리형 DB의 논리 복제 권한(RDS의 `rds.logical_replication` 등)
- DDL 변경 중 동기화. 원본 DDL이 바뀌면 적용이 멈추고, 최종 적용 확인이 시간 초과로 UNKNOWN이 된다.
- large object

## S3 호환 객체 저장소 참조 adapter

`S3OfflineCopyAdapter`는 한 버킷의 객체를 다른 버킷으로 옮기는 offline copy 인계다. 출발지와 목적지가 서로 다른 provider일 수 있다. 각 쪽이 자기 S3 API client(엔드포인트·자격 증명)와 자기 접근 제어 구현(`BucketAccess`)을 가진다. 서버 측 복사에 기대지 않으므로 같은 코드가 AWS S3, S3 호환 API를 가진 다른 클라우드, 온프레미스 gateway 사이에서 동작한다. 접근 모델이 다른 provider는 `BucketAccess`만 새로 구현하면 된다.

| 단계 | 효과 | 사후조건(readback) |
|---|---|---|
| 목적지 준비 | 버킷 생성(원본과 같은 버전 관리·Object Lock), 모든 writer 거부 | 버킷 존재, 설정 같음, 모든 writer 거부 |
| 신규 접수 정지 | 원본에서 모든 writer의 모든 요청 거부 | 모든 writer 거부 |
| 원본 차단 | 차단 전용 거부 문장 | 그 문장이 모든 writer의 객체 쓰기를 무조건 거부, 진행 중 multipart upload 없음 |
| 복귀 대상 확인(failback) | 없음(확인) | 원래 출발지 존재, 모든 writer 거부, 진행 중 upload 없음 |
| 동기화 시작 | 초기 복사, 목적지 버킷 태그 표시 | 표시 있음 |
| 복사, 최종 변경 적용 | 다른 객체만 다시 쓰고, 원본에 없는 목적지 객체 삭제 | 모든 현재 객체의 SHA-256·내용 헤더·사용자 metadata·태그 같음 |
| 최종 상태 검증 | 없음(측정) | 위와 같고, 현재 상태 복사로 잃을 버전 이력(이전 버전·삭제 표식)이 없음. 손실을 계약 수준에서 받아들인 adapter만 예외 |
| 목적지 활성화 | 앱 principal에 대해서만 handoff 거부를 풀고 객체 읽기·쓰기 허용 | 앱만 목적지 쓰기 가능, 다른 writer는 목적지에서 거부, 원본은 계속 거부 |
| 경로 확정 | 경로를 목적지 버킷으로 | 경로가 `엔드포인트/버킷` |

`S3BucketPolicyAccess`는 버킷 정책의 `SovaHandoff`로 시작하는 문장만 소유하고, 고객이 쓴 다른 문장은 건드리지 않는다. 판정은 지금 provider가 돌려주는 정책으로만 한다. 거부는 조건 없이 모든 키의 객체 쓰기를 덮어야 인정한다. 허용은 조건 없는 허용이 있고 일부 키라도 막을 수 있는 거부가 없어야 인정한다. S3는 버킷 정책에 조건부 쓰기가 없어, 그 사이 다른 편집이 사라질 수 있다. 그래서 쓰기 자체를 믿지 않고 readback이 handoff 문장을 다시 찾는다.

진행 중 multipart upload는 PostgreSQL의 prepared transaction에 대응한다. 차단 뒤에는 완료할 수 없지만, 원본이 다시 열리면 완료될 수 있는 쓰기 경로다. 그 부분이 업무 데이터일 수 있으므로 adapter가 버리지 않는다. 운영자가 완료하거나 중단할 때까지 차단은 확인되지 않는다.

한계는 다음과 같다.
- 현재 버전만 옮긴다.
- 객체별 보존 기간·legal hold는 비교만 하고 복사하지 않으므로, 이를 쓰는 원본은 검증을 통과하지 않는다.
- 서버 측 암호화는 각 저장소의 기본값을 따르며 비교하지 않는다.
- 객체 ACL은 옮기지 않는다. 접근은 `BucketAccess`가 버킷 단위로 다룬다.
- 비교할 때마다 모든 객체를 이 프로세스로 읽는다(`max_objects` 상한).

시험은 versitygw(posix backend, 내부 IAM) 컨테이너 두 개로 한다. 각각 따로 root·writer 계정·저장소·정책을 가진 독립 저장소다. writer 쓰기 시도는 성공할 수 없는 조건부 쓰기(없는 키에 맞지 않는 If-Match)로 한다. 허용된 writer는 "키 없음", 거부된 writer는 접근 거부를 받고 아무것도 써지지 않는다. 시험 fixture는 loopback 엔드포인트만 받아 실제 클라우드 계정에 닿을 수 없다(`SOVA_TEST_S3_ENDPOINTS`, `SOVA_TEST_S3_ROOT`, `SOVA_TEST_S3_WRITERS`).

공통 적합성 시험 9개와 S3 전용 시험 7개가 통과한다. S3 전용 시험은 다음을 확인한다.
- 고객 정책 보존
- 9 MiB 객체·태그·metadata 보존
- 원본 삭제 반영
- 버전 이력 손실 차단
- writer 목록 누락을 probe가 잡음
- 잘못된 구성 거부
- 객체 수 상한 초과 시 재조회는 `UNKNOWN`, 복사는 아무것도 옮기기 전에 거부

결함 5종을 일부러 넣으면 모두 시험이 잡는다.

## 실제 IdP 사이 계정 이전 참조 (Keycloak, Part II)

`KeycloakIdentityMigration`은 `RESET_AND_REENROLL` 전략으로 한 realm의 계정을 다른 서버의 realm으로 옮긴다. 옮기는 것은 다음과 같다.
- 프로필, 활성·이메일 확인 상태
- 최상위 그룹 소속
- 원본 subject

자격 증명은 옮기지 않는다. 비밀번호와 MFA 비밀은 IdP 사이에 옮길 수 없다(§1). 그래서 옮긴 계정마다 비밀번호 재설정을 요구하고, 계약이 MFA를 요구하면 MFA 재등록도 요구한다.

원본 subject는 관리자만 보고 고칠 수 있는 목적지 속성 `sova_source_subject`에 둔다. 사용자가 이 값을 고칠 수 있으면 다른 사람의 원본 신원을 자기 것으로 주장할 수 있다. 그래서 목적지 user profile이 사용자 편집을 허용하면 readback이 확인되지 않는다.

readback은 두 realm을 다시 읽고 다음이 모두 성립할 때만 확인한다.
- 활성 원본 계정마다 목적지 계정이 정확히 하나이고, 두 목적지 계정이 같은 원본 subject를 주장하지 않는다(IAM-01).
- 사용자 이름·이메일·활성·이메일 확인·그룹이 같다(IAM-06, IAM-04의 그룹 부분).
- 원본에서 사라진 계정은 목적지에서 비활성이다.
- 같은 사용자 이름의 매핑 없는 목적지 계정이 없다. 이런 계정은 자동으로 합치지 않는다. 합치면 기존 계정을 원본 계정 주인에게 넘기는 셈이기 때문이다. 운영자가 해결한다.

중첩 그룹은 지원하지 않으며, 있으면 확인되지 않는다. 계정 목록은 `max_users`로 상한을 둔다.

`OidcProbes`는 이전 adapter와 따로, 실제로 로그인한다. ID token은 RS256 서명을 발급자가 공개한 키로 검증하고, 발급자·대상·기한도 확인한다. 측정하는 의무는 다음과 같다.
- 새 로그인(IAM-02)
- 목적지 refresh token으로 갱신(IAM-02)
- 원본 refresh token을 목적지가 거부함: 계약이 이식성을 가정할 수 없는 이유의 실측
- 다른 테넌트 계정의 로그인 거부(IAM-04)
- 같은 사람의 그룹 claim이 양쪽에서 같음(IAM-04)
- 원본 IdP가 로그인을 거부하는 동안 목적지 로그인(IAM-08)

닿지 않는 발급자를 거부로 착각하지 않는다. 관측에는 토큰·비밀번호를 담지 않는다. 시험 계정은 resource-owner password grant로 로그인하며, 운영 IdP에서 실사용자에게 켤 방식은 아니다.

시험은 서로 독립된 Keycloak 26.8.1 두 대로 한다. 발급자·키·저장소가 각각 따로다. fixture는 loopback 서버만 받는다(`SOVA_TEST_KEYCLOAK_URLS`, `SOVA_TEST_KEYCLOAK_ADMIN`). 실제 로그인 시험 11개가 통과한다. 원본 비밀번호는 목적지에서 `invalid_grant`이고, 재설정 뒤에는 목적지 서명 토큰이 검증된다.

올바른 IdP는 만들지 않는 토큰(서명은 맞지만 발급자·대상·기한이 틀린 토큰, 서명 없는 토큰, 모르는 키)은 직접 만든 키와 모의 발급자로 단위 시험한다. 결함 5종을 일부러 넣으면 모두 잡는다. 처음에는 발급자 확인 제거를 놓쳤다. 다른 발급자의 토큰이 키 조회에서 먼저 거부되어, 시험이 엉뚱한 이유로 통과했기 때문이다. 이 단위 시험을 추가한 뒤 잡는다.

`IdentityMappingProbes`는 IAM-01·06을 이전 adapter와 다른 방식으로 따로 판정한다. adapter는 양쪽 계정을 통째로 나열해 맞춰 보지만, probe는 원본 계정마다 목적지에서 그 원본 subject를 주장하는 사용자만 속성 검색으로 찾는다.
- 정확히 한 명이어야 한다(IAM-01).
- 그 사용자의 이름·이메일·활성·이메일 확인·그룹이 원본과 같아야 한다(IAM-06).
- 코드를 공유하지 않아서 adapter의 나열·매칭 결함이 여기서 반복되지 않는다.
- 이전 직후에는 통과한다. 다른 사용자가 같은 원본 subject를 주장하거나 목적지 상태가 바뀌면 실패한다.
- 두 번 주장된 경우를 놓치는 결함을 넣으면 시험이 잡는다.

측정하지 않은 것은 다음과 같다.
- MFA·복구 흐름의 실제 시험(IAM-03)
- 앱 쪽 이중 발급자 수락 정책(IAM-05·10·11)
- 비밀·개인정보 유출 검사(IAM-07)
- 감사·잠금·복구 경로(IAM-09)
- 로그인 SLO(IAM-12)
- `JUST_IN_TIME_ACCOUNT_BRIDGE` 전략
- Cognito 같은 운영 IdP

이 adapter는 계정 복사만 한다. 계정 집합 전체를 인계 단위로 옮기는 것은 [아래 절](#계정-집합을-독립-인계-단위로-adr-0002)의 `KeycloakIdentityHandoff`가 하고, 계정 복사는 이 adapter에 맡긴다. 의존성 그래프에 IdP 노드가 없는 계약에서는 이 adapter를 계획 밖에서 실행하고 IAM 의무를 증거로 확인한다(ADR-0002의 A).

## 객체 저장소 단위의 control plane 전체 인계

같은 컴파일 계획과 승인·수용·권한 기록·효과 원장·서명 증거를 그대로 두고, adapter만 S3 호환 offline copy로 바꾸고 의무별 측정만 객체 저장소에 맞게 대응시켰다.

| 의무 | 객체 저장소에서의 측정 |
|---|---|
| `data.<표>` | 같은 이름 접두어(`orders/` 등) 아래 모든 객체의 내용 비교 |
| ORDER-01 | 주문 객체가 고객·멱등 키당 하나이고 결과 객체가 있음 |
| 위치(residency) | 목적지 버킷의 zone 태그(환경 소유자가 버킷을 미리 만들어 붙임) |
| 원본 차단·오래된 경로 | 원본 쓰기 거부 |
| 단일 writer | 목적지만 쓰기 가능 |
| 다른 테넌트 거부 | 권한이 없는 계정의 접근 거부 |
| 복원 시험 | 목적지를 새 버킷으로 복원해 비교 |
| 예산 | 기존처럼 효과 원장 관측 시각으로 측정 |

로컬의 독립된 S3 호환 저장소 두 개 사이에서 인계가 완료되고, 앱만 목적지에 쓴다. 원본 데이터에서 결과 기록 하나를 빼면 복사는 충실해도 ORDER-01이 FAIL한다. 그러면 활성화 전에 계약 위반 보류가 되고 경로는 원본에 남는다. 데이터의 종류가 바뀌어도 권한 프로토콜은 그대로임을 보인다. 실행 루프가 의무별 측정을 외부에서 받게 한 덕분이다.

## Kubernetes 경로 전환과 관리권 인수 실측

데이터 adapter의 경로 확정은 경로 저장소(`get`/`set`)에 쓴다. `KubernetesServiceRoute`는 이 경로 저장소를 실제 Service로 구현한다. 그래서 PostgreSQL·S3 adapter의 경로 확정이 그대로 실제 트래픽 전환이 된다. EKS·GKE·AKS·온프레미스 등 표준 Kubernetes면 같다.
- `set`은 Service selector를 그 쪽 Pod로 바꾸고, 엔드포인트가 다시 만들어질 때까지 상한 시간 안에서 기다린다.
- `get`은 트래픽이 실제로 닿는 곳에서 경로를 읽는다. selector가 그 쪽 것이어야 하고, Service 뒤의 모든 엔드포인트가 그 쪽 Pod여야 한다.
- 엔드포인트가 아직 수렴 중이거나, 다른 Pod나 손으로 만든 엔드포인트가 섞여 있거나, 준비된 Pod가 없으면 어느 쪽도 아니다(`None`). 그래서 경로 확정 readback은 `UNKNOWN`이다.

`controller_paused`는 CronJob 형태의 기존 자동화(배치 writer)에 대한 `controller.<id>.paused` 의무를 측정한다. 멈춤 판정에는 두 조건이 모두 필요하다.
- CronJob이 suspend 상태다.
- 그 CronJob이 시작한 Job 중 아직 도는 것이 없다.

suspend는 새 실행만 막는다. 그래서 이미 도는 Job이 있으면 그것이 끝나거나 운영자가 멈출 때까지 FAIL이다.

모든 요청은 기존의 고정 HTTPS 전송을 쓴다. 등록된 CA·주소와 bearer token으로, 등록된 namespace 안의 정확한 경로만 보낸다. 변경은 먼저 `resourceVersion`을 시험하는 JSON Patch다. 그래서 그 사이 다른 사람이 바꾼 객체는 덮어쓰지 않고 거부된다.

시험은 일회용 kind 클러스터(노드 `kindest/node:v1.34.0`)에서 한다.
- 시험 대상 코드는 namespace 범위의 최소 권한 ServiceAccount token만 쓴다. 권한은 Service·EndpointSlice·Pod·CronJob·Job 읽기와 Service·CronJob patch뿐이다.
- Pod는 노드에 이미 있는 pause 이미지로 띄워 외부에서 아무것도 받지 않는다.
- fixture는 loopback API 서버의 kind context만 받는다(`SOVA_TEST_KUBE_CONTEXT`).

7개 시험이 확인하는 것은 다음과 같다.
- 실제 트래픽 전환과 멱등
- 목적지 Pod가 없을 때 경로를 주장하지 않고, 생길 때까지 기다림
- PostgreSQL adapter의 경로 확정이 Service를 실제로 바꾸고, 계획 밖에서 되돌리면 `UNKNOWN`
- 손으로 만든 엔드포인트 감지
- 그 사이 변경 거부
- 실행 중 Job이 남은 배치는 멈춤이 아님
- namespace·동사 밖 요청이 403

결함 5종을 일부러 넣으면 모두 잡는다. 수렴 대기 제거는 kind의 빠른 갱신 때문에 처음에 놓쳤다. 목적지 Pod를 늦게 올리는 결정적 시험을 추가한 뒤 잡는다.

`deployment_paused`는 Deployment로 도는 자동화(operator, 동기화 agent)의 멈춤을 잰다. 다음 셋이 모두 맞아야 멈춤이다.
- 원하는 replica가 0이다.
- 실제 replica가 0이다.
- 그 selector에 걸리는 살아 있는 Pod가 없다.

단순 라벨 일치가 아닌 selector는 추측하지 않고 거부한다. 멈춘 뒤 누군가(예: GitOps 동기화) 다시 늘리면 다음 측정이 실패한다. kind에서 확인했고, Pod 조건은 가짜 controls 단위 시험으로 결정적으로 확인했다. Pod 조건을 빼는 결함을 넣으면 잡는다.

GitOps 도구 자체의 동기화 중지 API, Ingress·Gateway 수준의 전환, 관리형 클러스터의 IAM 기반 인증(IRSA·Workload Identity)은 다루지 않았다.

## 계정 집합을 독립 인계 단위로 (ADR-0002)

[ADR-0002](adr/0002-identity-migration-in-the-handoff-plan.md)의 C를 채택해 구현했다. 계정 집합은 데이터와 같은 템플릿·권한 프로토콜·효과 원장·서명 증거로 인계된다. 승인·잠금·불명 효과 복구도 데이터 단위와 같다.

**단위와 순서.** 의존성 그래프의 `identity` 노드(예: 서비스가 `uses`로 쓰는 로그인 IdP)만 요청하면 계정 단위다.
- 계정 단위의 의무
  - 데이터 내용 비교·업무 불변식·외부 효과 의무는 없다.
  - IAM-01·06(계정 매핑·상태 비교)이 최종 검증에 걸린다.
  - 목적지 로그인은 이 단위의 활성화에서야 열린다. 그래서 IAM 흐름 시험(IAM-02·03·04)은 안정화 단계에서 확인한다.
- 데이터 단위와의 순서
  - 계약이 원본 IdP를 제거하고(`MIGRATE_IDENTITY`) 그래프에 IdP 노드가 있으면, 데이터 단위 계획에 `identity.unit_handoff_complete` 의무가 생긴다.
  - 컴파일러가 같은 계약·snapshot으로 계정 단위의 `unit_digest`를 계산해 이 의무에 박아 둔다.
  - 활성화 의도 기록 때 `UnitCompletion`이 그 단위의 현재 권한 기록이 `HANDOFF_COMPLETE`인지 읽는다. 아니면 데이터 단위는 목적지 활성화 전에 보류된다.
- 경계 분석
  - 서비스→IdP `uses`·`calls`가 단위 경계를 넘는 것은 이 프로토콜이 의미를 정하므로 막지 않는다.
  - IdP를 남기는 계약(`RETAIN_SOURCE_IDP`)에서는 이전처럼 미정 효과로 막는다.
- 잠금
  - 계정 단위의 경로 키는 앱이 신뢰하는 발급자다. 한 서비스의 두 단위는 서로 막지 않는다.
  - 같은 IdP를 옮기는 두 계획은 충돌한다.
- failback
  - 계정 단위의 failback은 UNSUPPORTED다. 인계 뒤 목적지에서 생긴 계정을 되돌릴 수단이 없다.

**Keycloak adapter(`KeycloakIdentityHandoff`).** 계정을 만들거나 바꾸는 경로가 writer다.
- 앱: 셀프 가입, 사용자의 본인 프로필(사용자명·이메일·이름) 수정
- 다른 writer: 사용자 관리 권한을 가진 백오피스 client

| 단계 | 서버에서 다시 읽어 확인하는 것 |
|---|---|
| 목적지 준비 | 목적지 realm이 로그인·가입·본인 수정을 거부하고, 원본 subject 속성은 관리자 전용이며, 백오피스 client에 사용자 관리 권한이 없다 |
| 동기화 시작 | 원본이 열린 채 한 번 복사됐다(목적지 realm의 표시) |
| 접수 정지 | 원본이 가입을 거부한다 |
| 원본 차단 | 원본이 가입·본인 수정을 거부하고 백오피스 권한이 없다. 다음 셋이 하나도 없어야 확인된다(어댑터가 사용자의 동작을 대신 취소하지 않는다). ① 계정을 만드는 다른 경로(IdP 중개 로그인, 사용자 연합) ② 프로필이나 이메일 확인 상태를 바꿀 미완료 필수 동작(`UPDATE_PROFILE`·`VERIFY_EMAIL`·`UPDATE_EMAIL`)을 가진 활성 계정 |
| 복사·최종 반영 | 계정 이전(`KeycloakIdentityMigration`)의 전체 비교 |
| 활성화 | 목적지가 로그인·필요한 가입·본인 수정을 받고, 백오피스는 여전히 권한이 없으며, 원본은 차단 상태다 |
| 경로 확정 | 앱의 발급자 설정이 목적지 realm이다 |

차단 전 설정은 원본 realm에 남긴다. 중단 때 `restore_source()`가 되돌리고, `source_restored()`가 서버에서 확인한다.

**독립 probe(`KeycloakWriterProbes`).** adapter와 코드를 공유하지 않는다.
- realm의 셀프 가입 폼을 실제로 제출한다. Keycloak의 세션 쿠키는 Secure라서 평문 loopback에서는 명시적으로 보낸다.
- 백오피스 client가 자기 자격으로 사용자를 만든다.
- 만든 계정은 지운다.
- 결과는 생성(True), 거부(False), 판단 불가(None)다. False만 거부로 센다.

**시험(로컬 Keycloak 두 대 + PostgreSQL, 2026-11-15).**
- 컴파일·잠금 단위 시험 5개.
- adapter 시험 12개: 공통 적합성 시험 전체, 중단 뒤 원본 writer 복원, 켜진 IdP 중개 로그인 때문에 차단이 확인되지 않음, 독립 probe와 adapter 판정 일치.
- control plane 두 단위 시험 2개: 계정 단위 미완료 때 데이터 단위 보류, 계정 → 데이터 순서로 둘 다 완료.

Cognito 원본은 아직 독립 단위가 아니다. Cognito에는 "계정 변경 차단"이 정확히 없어서, 근사 차단을 자격으로 인정할지 정해야 한다. 그때까지 Cognito는 계획 밖 이전 + 증거 게이트(A)로 다룬다.

## Cognito에서 옮기기 (Part II §5~§7)

이전 adapter의 원본 쪽은 계정 목록 연결부(`accounts(bound)`)로 분리했다. Keycloak realm과 AWS Cognito user pool이 그 구현이고, 목적지는 Keycloak realm이다.

`CognitoUserPoolDirectory`는 `ListUsers`와 `AdminListGroupsForUser` 두 호출만으로 읽는다. 원칙은 다음과 같다.
- Cognito는 비밀번호·해시·MFA 비밀을 내주지 않는다. 그래서 Cognito에서 나가는 이전은 항상 목적지에서 새 자격 증명을 만드는 방식(`RESET_AND_REENROLL`)이다. Cognito의 사용자 이전 트리거는 반대 방향(Cognito로 들어오는 이전)이라 쓰지 않는다.
- 안정 식별자는 `sub` 속성이다. 사용자 이름이나 이메일로 계정을 합치지 않는다. 발급자(`https://cognito-idp.<region>.amazonaws.com/<pool>`)와 함께 원본 신원을 정한다.
- 외부 IdP로 페더레이션된 사용자(`EXTERNAL_PROVIDER`)는 비밀번호 재설정으로 옮길 수 없다. 만들지 않고, readback이 `federated_accounts`로 보고하며 확인되지 않는다. 페더레이션 계획이 따로 다룬다.

실제 AWS Cognito user pool(Lite 등급, 실행 범위, 이메일 발송 끔)에서 로컬 Keycloak으로 4개 시험이 통과했다.
- 계정은 Cognito `sub`로 매핑되고 그룹이 함께 옮겨진다. 비활성 계정은 만들지 않는다.
- Cognito 비밀번호로는 목적지에 로그인할 수 없다(`invalid_grant`). 재설정 뒤 목적지 로그인이 되고, 목적지 토큰의 그룹 claim이 Cognito ID 토큰의 `cognito:groups`와 같다. Cognito ID 토큰도 Cognito가 공개한 키로 같은 검증기가 서명을 확인했다.
- Cognito refresh token은 목적지가 거부한다.
- Cognito 앱 클라이언트를 지워 Cognito 로그인을 막아도 목적지 로그인은 계속된다(IAM-08).
- 이전 뒤 Cognito에서 계정 비활성화·그룹 변경이 일어나면 readback이 `UNKNOWN`이고, 다시 동기화하면 반영된다.

Identity Pool·IAM role 연계, 앱 클라이언트·redirect URI·Lambda trigger 조사, MFA 사용자, 페더레이션 사용자의 실계정 시험은 하지 않았다.

## 비밀 이전: Secrets Manager → Kubernetes Secret

`SecretsManagerToKubernetes`는 등록된 원본 비밀(AWS Secrets Manager)의 현재 값(`AWSCURRENT`)을 등록된 Kubernetes Secret의 키로 옮긴다.
- 목적지 Secret은 환경 소유자가 미리 만든다. adapter의 token은 그 이름들에 대한 `get`·`patch`만 가진다. 쓰기는 resourceVersion을 시험하는 JSON Patch다.
- readback은 양쪽을 다시 읽어, 모든 목적지 값이 원본의 현재 값과 같을 때만 확인한다. 복사 뒤 원본이 회전되면 목적지는 낡은 자격 증명을 내주게 되므로 `UNKNOWN`이다.
- 값은 호출 동안 메모리에만 있다. 관측·오류에는 비밀 이름, 원본 버전 ID, 일치 여부만 담는다. 값, 값의 인코딩, 값의 해시는 담지 않는다. 짧은 비밀은 해시로부터 역산될 수 있기 때문이다. provider 오류 메시지도 전달하지 않는다.
- 원본 비밀 뒤의 자격 증명을 폐기·회전해 클라우드에 남은 사본을 무효로 만드는 일은 별도 승인 단계다. 이 adapter는 그것을 주장하지 않으며, 읽을 수 있는 원본은 여전히 의존성이다.

실제 Secrets Manager 비밀 두 개(JSON 문자열, 바이너리)에서 kind의 Secret으로 3개 시험이 통과했다.
- 증거 어디에도 값·base64·비밀번호 문자열이 없다.
- 원본 회전 뒤 낡은 사본이 감지되고, 다시 복사하면 확인된다.
- 같은 namespace의 등록되지 않은 Secret은 403이다.
- 없는 원본의 오류에는 provider 메시지가 없다.

결함 3종을 일부러 넣으면 모두 잡는다: 존재만 비교, 값을 증거에 넣음, provider 메시지 전달.

## 원본 compute 중지와 AWS 탈출 측정 (§20.1)

원본 앱이 아직 돌 수 있으면 그 작업이 연결을 쥐거나 큐를 비우거나 다른 곳에 쓸 수 있으므로, 인계는 원본에서 독립했다고 할 수 없다. `EcsServiceCompute`는 등록된 ECS 서비스 하나를 멈추고 그 상태를 ECS에서 다시 읽는다.
- 원하는 작업 수를 0으로 하고(멱등), 서비스의 작업이 모두 `STOPPED`가 될 때까지 상한 시간 안에서 기다린다.
- readback은 원하는 작업 수가 0이고, ECS가 실행·대기 중인 작업을 하나도 세지 않고, 내려가는 중인 작업도 없을 때만 확인한다. 원하는 수가 0이어도 작업이 아직 내려가는 중이면 확인하지 않는다.
- 서비스·작업 정의·이미지 삭제는 퇴역이며 별도 승인 단계다.

`exit.compute.source_compute_blocked` 의무는 다음 셋을 함께 잰다.
- ECS가 원본 서비스를 멈췄다고 보고함
- 예전 앱 주소가 그 실행의 고유 표식으로 응답하지 않음. 작업이 멈춘 뒤 AWS가 그 공인 IP를 다른 고객에게 줄 수 있어서, 남의 서버 응답을 원본으로 착각하지 않도록 표식으로 확인한다.
- 원본 DB에 닿지 못하는 상태에서 새 앱 세션이 목적지에서 업무를 처리함

로컬에서는 원본 compute가 없어 이 의무가 판단 불가였다. 실제 AWS에서 데이터 원본은 RDS, compute 원본은 ECS Fargate 서비스(실행 범위, 이 기기 IP만 허용)로 두고 측정했다.
1. control plane 전체 흐름으로 RDS → 온프레미스 PostgreSQL 인계를 완료했다.
2. 멈추기 전에는 같은 측정이 FAIL이었다. 예전 앱이 응답하고 실행 중인 작업이 1개였다.
3. adapter가 ECS를 멈추고 재조회로 확인했다.
4. RDS의 원본 DB가 접속을 받지 않게 했다.
5. 의무별로 측정해 서명 증거로 넣었다.

결과는 `COMPUTE_INDEPENDENT`, `DATA_IDENTITY_INDEPENDENT`, `RESTART_VERIFIED`, `OPERATIONS_VERIFIED` 모두 PASS다. 그래서 규칙대로 `PROVIDER_EXIT_VERIFIED`도 PASS다. 측정하지 않은 `PLATFORM_EXIT_VERIFIED`는 판단 불가로 남는다.

이 PASS는 이 시험의 프로필에만 해당한다. 주문 50건 규모의 한 단위, RDS 한 대와 ECS 서비스 한 개, 객체 저장소·비밀·Identity를 계약에 넣지 않은 경우다. 객체·비밀·Cognito까지 한 계약으로 묶은 탈출은 아직 한 번에 시험하지 않았다(각각은 따로 시험했다). 데이터 원본을 로컬 DB로 둔 첫 시도에서도 사업자 탈출이 PASS로 나왔는데, 원본이 AWS가 아니었으므로 그 결과는 주장하지 않고 RDS 원본으로 다시 측정했다.

## 플랫폼 탈출 (§20.2, AT-36)

인계 뒤에도 플랫폼은 목적지 환경의 자격 증명을 쥐고 있다. 플랫폼 탈출은 그것들이 모두 실제로 거부되고, 고객이 자기 자격 증명만으로 운영할 수 있을 때 통과다. 이미 떠 있는 Pod가 응답하는 것은 근거로 쓰지 않는다.

`platform_lockout`은 플랫폼이 쥔 자격 증명을 하나씩 실제로 써 보고, 환경 자신에게 아직 통하는지 묻는다.
- 결과는 셋이다: 거부됨(`True`), 아직 통함(`False`), 판단 불가(`None`).
- 닿지 않음·시간 초과·예상 밖 오류는 회수의 증거가 아니므로 거부로 세지 않는다.
- 다루는 자격 증명은 넷이다: Kubernetes token(401·403), PostgreSQL 역할(로그인 불가·역할 없음·인증 실패), IdP 관리자(토큰 요청 거부), 객체 저장소 계정(접근 거부·키 무효).
- `platform_exit`은 모든 자격 증명이 거부되고 고객 작업이 모두 성공할 때만 `exit.platform.customer_operates_after_revocation`을 통과시킨다. 판단 불가가 하나라도 있으면 통과가 아니다.

로컬 시험은 실제 인계 뒤 다음 순서로 진행한다.
1. 플랫폼 자격 증명 넷이 아직 통하는지 확인한다(모두 `False`).
2. 고객이 자기 도구로 모두 회수한다.
   - RoleBinding·ServiceAccount 삭제
   - 역할 `NOLOGIN`
   - Keycloak 관리자 삭제
   - 버킷 정책에 고객 소유의 거부 문장 추가
3. 그 뒤 고객 자격 증명만으로 다음을 한다.
   - API 키 Secret을 회전하고 새 버전을 배포한다. 새 Pod가 새 키를 갖고 있는지 확인한다.
   - 새 연결로 데이터를 저장한다.
   - DB 컨테이너의 `pg_dump`·`pg_restore`로 백업·복원하고 내용이 같은지 본다.
   - 사용자 비밀번호를 재설정하고 로그인한다.

모두 통과해 `PLATFORM_EXIT_VERIFIED`가 PASS다. 고객이 Keycloak 관리자 회수를 빠뜨리면 그 자격 증명이 통해서 실패함을 확인했다. 에이전트 종료 중 업무 지속은 AT-34 시험이 따로 보인다.

## DNS 수준 트래픽 전환 (Route 53)

`Route53Route`는 호스팅 존의 레코드 하나로 클라이언트가 쓰는 이름을 한쪽 주소로 가리키는 경로 저장소다. 데이터 adapter의 경로 확정이 그대로 DNS 전환이 된다.
- **전환:** 레코드를 읽은 그대로 지우는 동작과 새 레코드 생성을 한 변경 묶음으로 보낸다. 그 사이 다른 사람이 레코드를 바꿨으면 묶음 전체가 거부되고, 덮어쓰지 않는다. Route 53에 조건부 쓰기가 없어도 원자적 비교 후 교체가 된다. 변경이 전파 완료(`INSYNC`)될 때까지 상한 시간 안에서 기다린다.
- **재조회:** 이 인스턴스가 낸 마지막 변경이 전파 완료이고, 설정된 레코드와 Route 53의 실제 응답(`TestDNSAnswer`)이 둘 다 한쪽 주소와 정확히 같을 때만 그쪽을 경로로 인정한다. 변경이 전파 중이면(다른 사람의 변경 포함) 둘이 달라서 어느 쪽도 아니다. 처음 구현은 응답만 봤는데, 외부 변경 직후 아직 전파 전인 예전 응답을 경로로 인정하는 것을 실측에서 발견해 고쳤다.
- **TTL 캐시:** 클라이언트는 전환 뒤에도 TTL 동안 예전 주소를 쓸 수 있다. 이 창은 숨기지 않고 TTL로 보고하며, 인계는 원본 차단으로 그런 클라이언트를 원본에서 거부한다.

실제 Route 53(실행 범위의 공개 호스팅 존, 위임하지 않고 바로 삭제, 요금 없음)에서 2개 시험이 통과했다.
- PostgreSQL adapter의 경로 확정이 레코드를 원본 주소에서 목적지 주소로 바꾼다.
- 그 존의 권한 네임서버 넷 모두에 `dig`로 직접 물어도 목적지 주소를 답한다.
- 다시 적용하면 바꾸지 않는다.
- 계획 밖 변경은 `UNKNOWN`이다.
- 예전 읽기로 준비한 전환은 거부된다.

주소는 문서용 범위를 써서, 경로 제어를 시험하며 도달성은 시험하지 않는다. 로드밸런서 가중치 전환은 다루지 않았다.

## AWS 전체 탈출 실측 (Part V.3)

계약 하나가 RDS(`source-db`), S3(`source-object-store`), Secrets Manager(`source-secret-service`), Cognito(`aws-cognito-user-pool`)의 제거를 선언하고, Identity 확장으로 Cognito에서 Keycloak으로 옮기며, compute는 ECS에서 돈다. 모든 원본은 실제 AWS 계정의 실행 범위 자원이고, 목적지는 온프레미스(PostgreSQL, S3 호환 저장소, kind의 Kubernetes Secret, Keycloak, 새 앱 세션)다.

진행 순서는 다음과 같다.
1. **신원:** Cognito 계정을 Keycloak으로 옮기고 사용자가 재설정한다. 활성화 의도 단계에서 Identity 의무를 확인하므로 데이터 인계보다 먼저 한다.
2. **비밀과 객체:** 비밀을 Kubernetes Secret으로, S3 객체를 온프레미스 저장소로 옮긴다(각 adapter, offline-copy 순서).
3. **데이터:** control plane 전체 흐름으로 인계한다. 계약이 더한 Identity 의무 9개도 이 흐름 안에서 측정해 서명 증거로 넣는다.
   - IAM-01·06: 계정 매핑·상태 재조회
   - IAM-02: 새 로그인·갱신
   - IAM-04: 테넌트 간 거부, Cognito 토큰과 목적지 토큰의 그룹 동등성
   - IAM-07: 이전 시각 이전의 자격 증명이 목적지에 없음
   - IAM-09: 감사 이벤트·무차별 대입 잠금·비밀번호 복구 설정
   - IAM-12: 목적지 로그인 시간이 계약의 클라이언트 완료 대기 예산 안. 명세가 로그인 SLO 값을 따로 정하지 않아 이 예산으로 해석했다.
4. **원본 차단:** 모든 AWS 원본을 실제로 막는다.
   - ECS 서비스 중지(재조회 확인)
   - RDS 원본 DB 접속 거부
   - S3 원본은 인계의 차단 정책으로 앱 거부
   - Secrets Manager는 모든 주체의 값 조회를 거부하는 자원 정책
   - Cognito 앱 클라이언트 삭제
5. **탈출 의무 측정:** 의무마다 측정해 서명 증거로 넣는다.
   - compute: ECS 중지, 예전 주소 무응답, 새 세션 업무
   - 제거 대상 넷: 원본은 앱을 거부하고, 목적지는 새 세션에 데이터·객체·비밀·로그인을 내줌
   - IAM-08: Cognito 차단 중 목적지 로그인
   - IAM-10: Cognito 토큰을 목적지 발급자 검증이 거부
   - 재기동: 새 세션이 DB 쓰기, 새 토큰, 목적지 비밀을 모두 캐시 없이 얻음
   - 운영: 빈 DB 복원

결과(실행 번호 `930b87`, 첫 실행 통과, 4분)는 다음과 같다.
- `COMPUTE_INDEPENDENT`, `DATA_IDENTITY_INDEPENDENT`, `RESTART_VERIFIED`, `OPERATIONS_VERIFIED`, `PROVIDER_EXIT_VERIFIED`: 모두 PASS
- `PLATFORM_EXIT_VERIFIED`: 측정하지 않아 UNKNOWN
- Identity: `SOURCE_IDENTITY_INDEPENDENT`. 예전 발급자 거부 통과, 이중 발급자 철회는 해당 없음

같은 시험에 두 가지를 더해 다시 측정했다(실행 번호 `81fb39`, 첫 실행 통과, 4분 42초).
- **DNS 전환:** 클라이언트가 쓰는 실제 Route 53 이름을 처음에 ECS 원본 주소로 두었다. 데이터 인계의 경로 확정이 이를 온프레미스 주소로 바꾸고, 권한 네임서버 넷이 모두 그 주소를 답한다.
- **플랫폼 탈출:** 다른 탈출 의무를 모두 잰 뒤 마지막에 측정했다. 플랫폼이 측정을 마칠 때까지는 자격 증명이 필요하기 때문이다. 처음에는 플랫폼의 자격 증명 넷(비밀 namespace의 Kubernetes token, 목적지 DB 역할, Keycloak 관리자, 목적지 버킷 계정)이 모두 통했다. 고객이 회수한 뒤에는 모두 거부됐고, 고객 혼자 다음을 해냈다.
  - DB 자격 Secret 회전과 새 배포
  - 새 연결 저장
  - `pg_dump` 백업·복원
  - 비밀번호 재설정

그 결과 여섯 수준이 모두 PASS다(`PLATFORM_EXIT_VERIFIED` 포함).

이 결과의 범위는 다음과 같다.
- 시험 규모의 한 단위다. 주문 50건, 객체 11개(9 MiB 하나 포함), 비밀 2개, Cognito 사용자 3명이다.
- 데이터 인계는 offline-copy이며, 쓰기 정지 시간 등 성능은 주장하지 않는다.
- 다음은 시험하지 않았다: Identity Pool·IAM 연계, 페더레이션·MFA 사용자, 로드밸런서 수준의 트래픽 전환, EKS
- 다른 계약·규모·리전·계정으로 일반화하지 않는다.

## 실제 AWS 계정 실측 (2026-11-14)

개인 AWS 계정(ap-northeast-2)에서 사용자 승인을 받아 시험했다. 자원 이름은 모두 `sova-test-…-<실행번호>`이고 태그를 붙였으며, 시험 직후 실행 번호로 찾아 지웠다. 기존 자원은 읽기만 했다. 시험은 `aws_live` 표시가 있고, `SOVA_TEST_AWS_LIVE=1`과 실행 번호를 명시해야만 돈다.

| 경로 | 결과 | 확인한 것 |
|---|---|---|
| AWS S3(클라우드 원본) → 로컬 S3 호환 저장소(온프레미스 목적지) | 공통 적합성 시험 9개 통과 | 쓰기 주체는 AWS 쪽 IAM role, 온프레미스 쪽 gateway 계정. 같은 논리 writer를 각 저장소의 주체 이름으로 차단·활성화. AWS 정책 평가가 실제로 쓰기를 거부·허용 |
| AWS RDS PostgreSQL 17.11 → 로컬 PostgreSQL | 공통 적합성 시험 8개 통과, 1개 건너뜀 | superuser 없이도 기존 앱 세션 종료, 내용·sequence·ORDER-01 동일, 목적지에서 업무 지속. RDS는 prepared transaction이 기본으로 꺼져 있어 그 시험은 해당 없음 |
| 전송 암호화 probe | RDS에서 통과 | TLS는 RDS CA로 `verify-full` 검증. 로컬 참조 서버는 계속 FAIL |
| 권한 witness, S3 Object Lock | 2개 통과 | governance 모드 1시간 보존. 버전 삭제·보존 단축이 거부되고, 삭제 표식은 감지 |
| CloudTrail 변경 수집기 | 1개 통과 | 실제 버킷 정책 변경이 91초 뒤 이벤트 이력에 보여 `policy` 사건으로 한 번 기록. watermark는 15분 지연 원칙 유지 |
| control plane 전체 흐름, RDS → 온프레미스 | 통과 | 승인·수용·권한 기록·효과 원장 명령·서명 측정 증거로 인계 완료. 실측 쓰기 정지 시간 **4501ms**(접수 정지 관측 → 활성화 관측) |
| 권한 witness, compliance 모드 | 통과 | 버킷 소유자도 보존 우회 삭제·기간 단축·governance로 강등 모두 거부. 버킷은 1시간 보존이 끝난 뒤 지움 |
| 기존 RDS 보안 설정 수집기(읽기 전용) | 통과 | 그동안 가짜 응답으로만 시험한 수집기를 실제 인스턴스에 실행. 공개 접근·삭제 보호·Multi-AZ·암호화 없음은 FAIL, 백업 보존은 PASS로 실제와 일치 |
| 실제 snapshot 복원으로 되살아난 원본(AT-32) | 통과 | 앱이 쓸 수 있던 때의 RDS snapshot을 인계 완료 뒤 새 인스턴스로 복원. 복원된 DB에 예전 쓰기 권한이 그대로 있어 probe가 FAIL, 원본 차단 감시가 `SOURCE_WRITER_REACTIVATED` 보안 사고 보류를 열고 서비스를 멈춤. 복원본은 `DbiResourceId`가 달라 다른 DB로 식별됨 |

실측 중 드러난 것은 다음과 같다.
- **provider마다 같은 writer를 다르게 부른다.** AWS는 IAM role ARN, 온프레미스 gateway는 계정 이름이다. 그래서 adapter는 논리 이름만 다루고, 각 쪽 `BucketAccess`가 자기 주체 이름으로 바꾼다. 매핑에 없는 writer는 조용히 넘기지 않고 오류다(처음에는 거부 문장이 없을 때 조용히 넘겼다).
- **AWS는 같은 버킷 정책을 읽을 때마다 주체 목록의 순서를 다르게 돌려준다.** adapter는 원래 집합으로 판정해 영향이 없었다. 시험 world의 상태 비교가 텍스트였기에 집합 비교로 고쳤다. 증거나 상태를 정책 텍스트 그대로 비교하면 안 된다.
- 한 클라이언트에서 버킷 정책을 쓴 직후 읽기에는 낡은 값이 보이지 않았다(각 20초, 38회).
- **로그 내보내기 판정의 빈틈.** AWS는 로그 내보내기가 하나도 없으면 `EnabledCloudwatchLogsExports` 필드를 아예 빼고 보낸다. 기존 수집기는 필드가 없으면 `unknown`으로 둔다. 통과로 잘못 보고하지는 않지만, 내보내기가 없는 인스턴스를 실패로 드러내지도 못한다. 수집기는 고정된 security 패키지의 코드라 이 저장소에서 고치지 않고 발견 사항으로 남긴다.
- **쓰기 정지 시간.** 4501ms는 접수 정지부터 활성화까지를 효과 원장 관측 시각으로 잰 값이다. 소형 RDS 한 대, 주문 50건 규모, 서울 리전과 이 기기 사이 인터넷 경로에서의 한 번 측정이다. 성능 주장이나 SLO 근거로 쓰지 않는다.

**CloudTrail 변경 수집기.** `AwsCloudTrailCollector`는 등록한 자원마다 이벤트 이력에서 쓰기 호출을 찾아 변경 사건으로 기록한다.
- 권한을 바꾸는 호출(버킷 정책·ACL·공개 차단·보안 그룹 규칙 등)은 `policy`다.
- 자원 자체를 바꾸는 호출(생성·삭제·버전 관리·암호화·복원·재시작 등)은 `resource`다.
- 그 밖의 쓰기는 버리지 않고 `unclassified`다. 예를 들어 `ModifyDBInstance`는 크기 변경·네트워크 변경·역할 변경이 섞여 있다.
- 읽기 호출과 AWS가 거부한 호출은 변경이 아니다.
- CloudTrail은 호출 몇 분 뒤에 이력에 넣는다(최대 15분). 그래서 watermark는 `지금 - 15분`을 넘지 않는다. 매번 watermark부터 다시 찾으므로 늦게 들어온 사건도 한 번 기록된다. 재시작하면 피드의 watermark에서 이어 간다.
- 이벤트 이력은 한 리전의 90일 관리 이벤트이며, 객체 쓰기 같은 데이터 이벤트는 없다. 운영에서는 조직 단위 trail이 원천이어야 한다.

하지 않은 것은 다음과 같다.
- RDS의 IAM DB 인증, 다른 엔진(Aurora 등)
- 처리량·대용량 이전 측정, 반복 측정에 의한 쓰기 정지 시간 분포
- EKS 등 다른 앱 실행 환경, 로드밸런서 수준의 트래픽 전환
- 다른 리전·다른 계정 사이의 경로
- Azure·NAVER Cloud (GCP는 아래 절)

## 실제 GCP 프로젝트 실측 (2026-11-15)

사용자 승인을 받아 시험 전용 GCP 프로젝트(asia-northeast3, 서울)에서 시험했다. 자원은 AWS 때와 같은 규칙을 따른다.
- 이름은 모두 `sova-test-…-<실행번호>`이고, 라벨이 있는 서비스에는 `sova-test` 라벨을 붙인다.
- 시험은 `gcp_live` 표시가 있고 `SOVA_TEST_GCP_LIVE=1`일 때만 돈다.
- 두 클라우드의 기반 자원은 [클라우드 시험 환경 스위치](../infra/cloud-env/README.md)로 함께 켜고 끈다. 끝나는 시각(이번엔 8시간)이 지나면 저절로 꺼진다.

GCP adapter는 각 서비스의 공개 REST API를 `google-auth` 인증 세션으로 부른다(`gcp` extra). 오류에는 HTTP 상태와 Google의 이유 코드만 남기고 응답 본문은 옮기지 않는다. 본문이 요청 내용을, 경우에 따라 비밀값을 되풀이할 수 있기 때문이다.

| 경로 | 결과 | 확인한 것 |
|---|---|---|
| Cloud SQL PostgreSQL 17 → 로컬 PostgreSQL | 공통 적합성 시험 8개 통과, 1개 건너뜀. 전체 인계·평문 거부 통과 | 관리자 계정(`cloudsqlsuperuser`)은 superuser가 아니다. 인스턴스는 암호화 연결만 받는다(평문 접속은 인스턴스가 거부). 인증서는 인스턴스 DNS 이름으로 `verify-full` 검증 |
| control plane 전체 흐름, Cloud SQL → 온프레미스 | 통과 | 승인·수용·권한 기록·효과 원장·서명 측정 증거. 실측 쓰기 정지 **5405ms**(1회) |
| Cloud SQL에서 논리 복제(snapshot-cdc) | 통과. 쓰기 정지 **5965ms**(1회) | 로컬 서버가 Cloud SQL을 검증된 TLS로 구독. 데이터는 차단 전에 옮기고, 차단 안에서는 남은 변경만 적용. 끝난 뒤 Cloud SQL에 slot·publication이 남지 않음 |
| 복원한 백업으로 되살아난 원본(AT-32) | 통과 | 쓰기 가능하던 때의 on-demand 백업을 인계 뒤 새 인스턴스에 복원. 예전 권한이 살아 있어 원본 차단 감시가 보안 사고 보류를 엶 |
| Cloud Storage → 로컬 S3 호환 저장소 | 공통 적합성 시험 9개와 측정 2개 통과 | 같은 객체 저장소 adapter. 쓰기 주체는 실행 전용 서비스 계정 둘, 버킷 권한만 가진다 |
| Secret Manager → Kubernetes Secret | 3개 통과 | 값은 증거 어디에도 없음. 원본에 새 버전이 생기면 사본이 낡았다고 UNKNOWN. 등록하지 않은 Secret은 403 |
| Cloud Run 원본 compute 중지 | 2개 통과 | 수동 확장 0으로 멈추고 예전 URL이 앱 응답을 내지 않음을 실측. 밖에서 자동 확장으로 되돌리면 UNKNOWN |
| Cloud DNS 경로 전환 | 2개 통과 | 원자적 교체. 존의 권한 네임서버 넷이 모두 새 주소를 답함. 사이에 바뀐 레코드는 교체를 거부 |
| Cloud Audit Logs 변경 수집기 | 1개 통과 | 실제 IAM 변경과 비밀 버전 추가가 19초 뒤 관리 활동 로그에 보여 각각 `policy`·`resource` 사건으로 한 번씩 기록 |
| Identity Platform → Keycloak | 3개 통과 | 실제 로그인과 ID 토큰 검증(원본은 Google 공개키). UID가 주체로 유지되고, 그룹 일치, 비밀번호는 옮기지 않음. 원본 로그인을 막아도 목적지 로그인 지속 |
| GCP 전체 탈출 | 통과(첫 실행, 2분 55초). 여섯 수준 모두 PASS | Cloud SQL·Cloud Storage·Secret Manager·Identity Platform을 한 계약으로 제거하고, compute는 Cloud Run. 각각 온프레미스로 옮긴 뒤 원본을 실제로 막음(DB 접속 금지, 버킷 권한 회수, 비밀 버전 비활성화, tenant 비밀번호 로그인 끔, 수동 확장 0). 경로 단계가 Cloud DNS 이름을 Cloud Run 앞단 주소에서 온프레미스 주소로 옮김. 마지막에 플랫폼 자격 증명을 회수하고 고객 단독 운영까지 측정 |

**실측에서 드러난 것**

- **Cloud SQL 인증서 이름 끝의 점.** 공유 CA 모드의 서버 인증서는 SAN에 인스턴스 DNS 이름을 `…sql.goog.`처럼 끝점까지 넣는다. 끝점 없는 이름으로 접속하면 libpq가 이름 불일치로 거부한다. 스위치와 시험은 API가 주는 이름을 끝점째 그대로 쓴다.
- **Cloud SQL 라벨.** gcloud의 Cloud SQL 생성 명령에는 라벨 옵션이 없다. 라벨은 Admin API로 붙인다.
- **Cloud Run에는 "멈춘 상태"가 따로 없다.** 자동 확장 서비스는 요청이 오면 인스턴스를 새로 띄운다. 그래서 "지금 도는 인스턴스가 없다"는 원본이 멈췄다는 근거가 못 된다. 수동 확장 0으로 두면 어떤 리비전도 인스턴스를 띄우지 않고 모든 요청에 503을 준다(실측). adapter는 이 상태와 반영 완료(세대 일치, 재조정 끝, 성공 조건)를 함께 확인한다. gcloud 명령은 0을 받지 않아 API로 바꾼다.
- **감사 로그의 자원 이름.** 비밀에 새 버전을 더한 기록은 `…/secrets/<이름>/versions/<n>`처럼 하위 경로로 남는다. 이름이 정확히 같은 기록만 찾으면 비밀값 교체를 모두 놓친다. 수집기는 등록 자원과 그 아래 경로를 함께 찾는다. Secret Manager 기록의 자원 이름에는 프로젝트 ID가 아니라 프로젝트 번호가 들어간다. 로그는 호출 뒤 약 20초 안에 들어왔지만 상한이 문서화돼 있지 않아, watermark는 10분 뒤에 둔다.
- **GCS 쓰기 차단은 IAM이다.** 버킷 정책에 Deny가 없어서 차단은 버킷 수준 권한을 빼는 것이다.
  - 쓸 수 있는지는 직접 계산하지 않고 Policy Troubleshooter에 묻는다. 프로젝트·폴더 권한, Deny 정책, 조건까지 반영한 Google 자신의 판정이다. `CANNOT_ACCESS`만 차단으로, `CAN_ACCESS`만 쓰기 가능으로 본다.
  - 프로젝트 수준 권한을 따로 가진 writer는 버킷에서 빼도 쓸 수 있다. 그래서 그 권한을 주인이 회수할 때까지 차단이 확인되지 않는다(시험).
- **회수한 권한이 잠시 살아 있다.** Google은 IAM 변경이 보통 2분 안, 경우에 따라 7분 이상 걸리고, 그동안 회수한 권한을 쓸 수 있다고 문서화한다([Access change propagation](https://cloud.google.com/iam/docs/access-change-propagation)).
  - 실측: 버킷 권한을 회수한 뒤 194회 탐침 중 6회가 여전히 허용됐고, 마지막은 회수 4.4초 뒤였다. 권한 부여 직후에도 일부 요청이 거부되다가 맞춰지는 것을 봤다.
  - 그래서 GCS 접근층은 정책을 바꿀 때마다 시각과 정책 etag를 버킷 라벨에 남기고 전파 시간만큼 기다린다. 차단·쓰기 가능은 그 시간이 지났고 정책이 그대로일 때만 인정한다. 그 사이 다른 사람이 정책을 바꾸면 시각을 알 수 없으므로 확인하지 않는다.
  - 기본 대기는 Google이 말한 7분이다. 이 대기는 IAM으로 차단하는 GCS의 실제 비용이고 첫 차단 단계(접수 정지)에 실린다. 시험은 60초로 둔다.
  - 처음에는 "정책이 그대로인지"를 정책 etag로 확인했다. 그런데 GCS의 버킷 정책 etag는 버킷 메타데이터를 따라가서, 인계가 자기 표식 라벨을 쓰는 순간 etag가 바뀌었다. 그래서 차단이 영원히 확인되지 않았다(첫 실측에서 발견). 지금은 권한 바인딩 내용의 digest와 버킷 메타데이터 세대(metageneration)를 기록한다. 라벨은 `ifMetagenerationMatch`를 걸고 써서, 기록한 세대가 정확히 자기 쓰기의 결과가 되게 한다.
  - 권한 부여 쪽도 고르지 않다. 버킷 권한을 준 뒤 몇 분 동안 같은 주체의 요청이 서버에 따라 허용되기도 하고 거부되기도 했다. 시험 world는 자기가 바꾼 권한이 실제로 적용될 때까지 기다린 뒤에 다음 단언으로 간다.
- **재개 가능 업로드 세션은 차단을 깨지 못한다.** 세션 URI 자체가 인증이라 나열할 수 없다. 하지만 GCS는 완료 시점에 업로더 권한을 다시 확인해, 차단 전에 연 세션도 차단 뒤에는 403으로 거부했다(실측). 나열할 수 있는 XML API 멀티파트 업로드만 S3와 같은 규칙으로 차단을 미확인으로 둔다.
- **GCS 바이트는 저장된 그대로 읽는다.** `Content-Encoding: gzip` 객체는 그냥 받으면 압축을 풀어 다른 바이트를 준다. 그래서 `Accept-Encoding: gzip`으로 저장된 바이트를 받고, 메타데이터를 읽은 세대(generation)로 고정한다. GCS 객체에는 태그가 없다(빈 태그 집합이 사실이다). 보존·hold는 S3 객체 잠금처럼 비교만 하므로, 그런 원본은 검증되지 않는다.
- **Identity Platform 켜기는 되돌릴 수 없다.** 프로젝트를 Identity Platform으로 올리면 Firebase Authentication으로 돌아갈 수 없다. 시험 전용 프로젝트에서만 켰다. 원본의 비밀번호 해시(변형 scrypt)는 목적지가 검증할 수 없으므로 받는 즉시 버리고, 목적지에서는 새 자격 증명을 쓴다(`RESET_AND_REENROLL`).

하지 않은 것은 다음과 같다.
- Cloud SQL 보안 설정 수집기(RDS 수집기는 고정된 security 패키지의 코드다), IAM DB 인증, 다른 엔진
- GKE 등 다른 실행 환경, 부하 분산기 수준의 전환
- 조직 단위 감사 로그 sink, 데이터 접근 로그
- Identity Platform의 MFA·페더레이션 사용자, Firebase 프로젝트 수준(테넌트 없는) 사용자 이전

## 클라우드 사이 직접 인계 (AWS ↔ GCP, 2026-11-15)

두 클라우드를 같은 시간 창에 켜 두고, 온프레미스를 거치지 않는 클라우드 간 인계를 같은 adapter·적합성 시험·control plane으로 측정했다. 데이터는 이 기기의 프로세스를 거쳐 두 사업자의 서울 리전 사이 인터넷 경로로 옮겨진다.

| 경로 | 결과 | 확인한 것 |
|---|---|---|
| RDS → Cloud SQL, Cloud SQL → RDS | 공통 적합성 시험 16개 통과, 2개 건너뜀(두 사업자 모두 prepared transaction 꺼짐) | 양쪽 관리자 모두 superuser가 아님. 양쪽 모두 검증된 TLS라 계약의 전송 암호화 통제를 빼지 않음 |
| control plane 전체 흐름, 양방향 | 통과(첫 실행) | 쓰기 정지 AWS→GCP **7598ms**, GCP→AWS **7884ms**(각 1회, 분포는 아래) |
| AWS S3 → GCS, GCS → AWS S3 | 공통 적합성 시험 18개 통과(양방향) | 각 쪽을 그 사업자 방식으로 차단·개방(버킷 정책과 IAM role / 버킷 IAM과 서비스 계정) |
| S3 객체 태그 → GCS | 통과 | GCS 객체에는 태그가 없다. 태그 달린 S3 객체는 태그를 버리고 옮기지 않고 복사를 오류로 멈춤 |

**쓰기 정지 분포.** 경로·크기마다 5회 control plane 전체 인계를 돌렸다(`tests/perf/test_cloud_handoff_pause.py`, `SOVA_TEST_PERF=1`). 기본 데이터는 주문 50건 규모이고, "+1만 행"은 주문·결과·작업을 1만 행씩 더한 것이다. 원본 RDS는 db.t4g.micro, Cloud SQL은 공유 코어 db-f1-micro이고, 이 기기는 가정용 인터넷이다. 성능 주장이나 SLO 근거가 아니다.

| 경로 | 기본: 최소 / 중앙값 / 최대 (ms) | +1만 행: 최소 / 중앙값 / 최대 (ms) |
|---|---|---|
| RDS → 로컬 | 5279 / 5944 / 15897 | 6114 / 6365 / 12072 |
| Cloud SQL → 로컬 | 5075 / 5510 / 6203 | 4991 / 5187 / 5742 |
| Cloud SQL → 로컬, 논리 복제 | 5454 / 6634 / 7354 | 7959 / 11477 / 14581 |
| RDS → Cloud SQL | 7533 / 7846 / 10649 | 13556 / 20226 / 21222 |
| Cloud SQL → RDS | 9728 / 10034 / 12267 | 12386 / 13998 / 17607 |

단계별 중앙값을 보면(접수 정지부터 누적), 클라우드 사이 경로는 복사보다 차단 구간 안의 검증이 지배한다. 예를 들어 RDS → Cloud SQL +1만 행에서 복사는 4.6초에 끝나지만, 최종 검증(6.3초) 뒤 활성화까지 14초가 더 걸린다. 이 사이에 목적지 쪽 의무를 실측하는데, 복원 훈련이 목적지 표를 이 기기를 거쳐 같은 클라우드의 다른 DB로 다시 옮기기 때문이다. 로컬 측정에서 본 결론("검증이 정지 시간을 지배")이 원격 목적지에서는 더 크게 나타난다. 차단 구간 밖으로 옮길 수 있는 검증을 가려내는 것이 다음 개선 대상이다. 최대값의 튄 값(RDS → 로컬 15.9초)은 1회뿐이며, 원인은 확인하지 못했다.

**검증을 정지 구간 밖으로(ADR-0003).** 위 분포에서 드러난 원인을 고쳤다. 목적지 통제 시험(교차 tenant 거부, 전송 암호화, 감사, 라우팅, 복원 훈련)이 활성화 의도 단계, 즉 쓰기가 멈춘 구간 안에서 측정되고 있었다. 명세(FR-10, §15.3)는 이 시험을 목적지 준비에 두고, 컷오버 직전에는 증거가 유효한지 재확인하라고 한다.
- 컴파일러 v2는 이 의무를 쓰기 정지 직전 단계에서 측정한다. 활성화 의도에서는 같은 증거가 아직 유효한지만 판정하고 다시 재지 않는다.
- 복원 훈련은 정지 전에 데이터가 목적지에 닿는 템플릿(snapshot-cdc)에서만 앞으로 나간다.
- 1만 행 중앙값: RDS → Cloud SQL 20.2초 → 11.5초, Cloud SQL → RDS 14.0초 → 12.4초, Cloud SQL → 로컬 논리 복제 11.5초 → 6.0초.
- 목적지 통제가 실패하면 이제 쓰기를 멈추기 전에 보류된다. 자세한 근거와 수치는 [ADR-0003](adr/0003-target-controls-before-the-pause.md)에 있다.

**내용 digest는 서버에서(ADR-0004).** 내용 비교(복사 확인, 최종 검증, 보호 데이터 의무)는 이제 각 DB가 자기 행으로 digest를 계산하고 해시만 넘긴다.
- digest 정의는 그대로다. 행 텍스트를 바이트 순으로 정렬하고 길이를 붙여 SHA-256을 낸다. 서버 값과 클라이언트 값이 모든 값 종류에서 같음을 시험으로 고정했다.
- 행의 텍스트 형태를 정하는 세션 설정(시간대, 날짜·간격 형식, 부동소수 자릿수, bytea 형식)을 고정했다. 기본값이 다른 두 서버에서 같은 데이터가 다르다고 판정될 수 있던 잠재 결함이다.
- 표가 256MiB를 넘으면 같은 digest를 클라이언트에서 낸다.
- 효과는 클라우드 간 경로에서 0.6~1.5초로 작았다. 남은 원인(복원 훈련, 원격 연결 반복)과 복원 훈련 선택지 비교는 [ADR-0004](adr/0004-verification-inside-the-window.md)에 있다.

**관리자 연결 재사용(ADR-0004 결정 3).** 정지 구간 안에서 원격 DB 연결을 32번 새로 맺고 있었다(합계 4~8초). 플랫폼 관리자 연결만 DB마다 하나씩 재사용하고, 쓸 때마다 세션을 처음 상태로 되돌린다. 역할로 접속하는 접근 시험은 계속 새 연결이다. 정지 구간 안 원격 연결은 5번이 됐다. 1만 행 쓰기 정지 중앙값은 RDS → Cloud SQL 8.4초, Cloud SQL → RDS 9.1초다(오늘 처음 20.2초·14.0초).

RDS에서도 논리 복제(snapshot-cdc)를 실측했다. 실행 전용 매개변수 그룹에 `rds.logical_replication=1`을 두었다(정적 매개변수라 재부팅 필요. 스위치는 이제 처음부터 이 그룹으로 인스턴스를 만든다). 로컬 서버가 RDS를 검증된 TLS로 구독해 인계를 마쳤고, 쓰기 정지는 6432ms(1회)였다. 끝난 뒤 RDS에 slot·publication이 남지 않았다.

**측정 중 드러난 것**
- **전송 암호화 probe가 서버 설정을 보고 있었다.** `SHOW ssl`은 서버에 TLS가 켜졌는지만 알려 준다. 그래서 TLS가 켜진 서버에 평문으로 붙거나 `sslmode=require`(검증 없음)로 붙어도 PASS였다. 지금은 서버가 보는 이 세션의 채널(`pg_stat_ssl`의 자기 backend 행)이 암호화돼 있고, 클라이언트가 인증서 체인과 호스트 이름까지 검증했을 때(`verify-full`)만 PASS다. 로컬 평문 서버, RDS, Cloud SQL에서 다시 확인했다. 클라우드 간 경로에서 처음으로 이 통제를 실제로 켜고 측정하려다 발견했다.
- **GCS의 "아무도 못 쓰는 버킷"은 차단된 상태가 아니다.** IAM은 허용만 있어서, 아무 권한도 받지 않은 주체도 쓸 수 없다. 그래서 미리 만들어 둔 빈 GCS 목적지가 인계가 아무것도 하기 전에 "목적지 준비 완료"로 읽혔다(공통 적합성 시험이 첫 클라우드 간 실행에서 잡음). 지금은 인계 자신의 차단 표식이 있는 곳에서만 차단을 인정한다.

## 관리형 Kubernetes 실측 (EKS·GKE, 2026-11-15)

로컬 kind에서만 돌던 Kubernetes 시험을 실행 전용 관리형 클러스터 두 개에 그대로 겨눴다. 클러스터는 EKS 1.37(관리형 노드 1대)과 GKE Autopilot이고, 스위치의 `kubernetes` 명령으로 같은 임대에 넣었다. 두 API 서버 모두 이 기기 IP에서만 받는다. 시험 코드는 하나도 바꾸지 않았다. 시험 도우미만 kind 말고도 이 실행의 관리형 클러스터를 받게 넓혔고, 그 API 서버의 공개 주소를 등록에 고정한다. 코드는 여전히 namespace 범위의 최소 권한 token만 가진다.

| 대상 | EKS | GKE Autopilot |
|---|---|---|
| Service 경로 전환, 수작업 endpoint가 경로 판정을 막음, 사이에 바뀐 변경 거부 | 통과 | 통과 |
| CronJob 관리권 멈춤(돌던 Job이 남으면 멈춤 아님), operator Deployment 멈춤 | 통과 | 통과 |
| token이 자기 namespace·동사 밖에 닿지 않음 | 통과 | 통과 |
| 비밀 이전: 같은 클라우드의 비밀 저장소 → 클러스터 Secret | 통과(Secrets Manager → EKS) | 통과(Secret Manager → GKE) |
| 비밀 이전: 다른 클라우드 → 클러스터 Secret | 통과(Secret Manager → EKS) | 통과(Secrets Manager → GKE) |

모두 첫 실행에 통과했다(통제 8개, 비밀 6개, 클러스터마다). 드러난 것은 다음과 같다.
- **EKS 노드는 공개 엔드포인트를 막으면 합류하지 못한다.** API 공개 접근을 이 기기 IP로만 묶고 사설 접근을 꺼 두었더니, 노드가 API 서버에 닿지 못해 노드 그룹이 `CREATE_FAILED`가 됐다. 사설 접근을 켜서 노드는 VPC 안에서 붙고, 공개 접근은 계속 한 주소로만 둔다.
- **API 서버 주소 고정.** EKS 공개 엔드포인트는 이름 하나에 IP가 둘이다. 전송 계층은 원래 등록된 첫 IP로만 붙었다. 지금은 등록된 IP를 같은 마감 시간 안에서 차례로 시도한다. 등록되지 않은 주소로는 여전히 가지 않는다.

클러스터는 시험을 마친 뒤 `kubernetes-down`으로 클러스터만 지웠다. DB는 임대 끝까지 둔다.

## 부하 분산기 수준 트래픽 전환 (ALB·URL map, 2026-11-15)

DNS 수준 전환(Route 53·Cloud DNS)에 더해, 데이터 adapter의 경로 단계(`commit_route`)가 쓰는 경로 저장소를 실제 부하 분산기로 두고 측정했다. 두 부하 분산기 모두 이 기기 주소만 받는다. AWS는 보안 그룹으로, GCP는 Cloud Armor 정책으로 막는다. 각 쪽은 실행 고유 표식을 돌려주는 작은 서버리스 백엔드다. AWS는 대상 그룹 뒤 Lambda, GCP는 서버리스 NEG 뒤 Cloud Run(부하 분산기 경유만 허용)이다.

| 경로 저장소 | 결과 | 확인한 것 |
|---|---|---|
| `AlbListenerRoute` (AWS ALB 리스너) | 통과(첫 실행) | 리스너 기본 동작을 목적지 대상 그룹으로 바꾸고, 부하 분산기를 거친 실제 요청이 모두 목적지에서 답할 때만 경로로 인정. 밖에서 원본으로 되돌리면 UNKNOWN. 리스너가 고정 응답 같은 다른 일을 하면 덮어쓰지 않고 거부 |
| `UrlMapRoute` (GCP URL map) | 통과(첫 실행). 바꾼 설정이 모든 응답에 반영되기까지 219초 | URL map의 기본 백엔드를 fingerprint 조건부로 바꿈. 사이에 바뀐 map에는 쓰지 않고 거부. 호스트·경로 규칙이 있는 map은 건드리지 않음 |

ALB는 조건부 쓰기가 없다. 읽은 뒤 쓰기 전에 다른 사람이 바꾼 내용은 덮어쓸 수 있다. 그래서 확인은 설정과 실제 응답이 둘 다 맞을 때만이다. GCP URL map은 fingerprint로 그 경합을 막는다. 이미 열린 연결은 닫힐 때까지 예전 대상에 남을 수 있으므로, 원본은 그동안 차단된 채로 둔다.

## 데이터 저장소 확대: MySQL·Kafka API·RabbitMQ (2026-11-16)

PostgreSQL·S3 말고도 세 저장소의 참조 adapter를 만들었다. 세 adapter 모두 [공통 적합성 시험](#adapter-공통-적합성-시험)을 그대로 통과한다. 시험 환경은 로컬 kind에 MySQL 8.4.13, Redpanda 26.2.5(Kafka API, SASL/SCRAM과 ACL 강제), RabbitMQ 4.1.8(shovel·federation 포함)을 더한 것이다. 저장소마다 PostgreSQL과 다른 점이 있고, 다를 때는 차단을 더 엄격하게 하는 쪽을 택했다.

| 저장소 | 결과 | PostgreSQL과 다른 점, 그에 따른 처리 |
|---|---|---|
| MySQL (`MySqlOfflineCopyAdapter`) | 적합성 9 + 고유 5 = **14 PASS** | 아래 MySQL 항목 |
| Kafka API (`KafkaTopicHandoffAdapter`) | 적합성 9 + 고유 3 = **12 PASS** | 아래 Kafka API 항목 |
| RabbitMQ (`RabbitMqQueueHandoffAdapter`) | 적합성 9 + 고유 2 = **11 PASS** | 아래 RabbitMQ 항목 |

**MySQL.**

- **DB 단위 접속 권한이 없다.** 그래서 권한 회수가 곧 차단이 되어 버린다. 승인 중지(pause)는 계정 잠금(`ACCOUNT LOCK`)으로 한다. 활성화 때 목적지 서버의 앱 계정만 잠금을 푼다. 원본과 목적지가 같은 서버라도, 원본에는 권한이 없으니 차단은 유지된다.
- **세션이 권한을 캐시하고, 한 DB에 묶이지 않는다.** 원본에 쓴 뒤 다른 DB로 옮긴 세션도 차단 뒤에 commit할 수 있다. 그래서 차단은 writer 계정의 세션을 모두 끊는다. 이 경우를 시험으로 재현했다.
- **XA PREPARE는 어느 DB를 건드렸는지 알려 주지 않는다.** 그래서 원본 서버에 XA PREPARE 트랜잭션이 하나라도 있으면 차단을 확정하지 않는다.
- **역할과 전역 권한으로 남는 쓰기 권한은 보고만 한다.** 자동으로 회수하지 않고, 차단을 미확인으로 둔다. 공유 역할을 지울지는 운영자가 정한다.
- **`AUTO_INCREMENT`가 sequence 역할을 한다.** 카운터를 복사하고 비교한다. 원본에서 발급했다가 철회한 번호(9, 10)를 목적지가 다시 내지 않는 것을 시험했다.
- **구조는 `SHOW CREATE TABLE`과 트리거로 비교한다.** 트리거 본문의 `END IF`, `CASE ... END CASE`, 레이블 LOOP를 mysql 클라이언트처럼 나누는 DDL 분리기를 넣었다.
- **적합성 시험이 실제 결함을 잡았다.** `DEFAULT CURRENT_TIMESTAMP` 열은 EXTRA가 `DEFAULT_GENERATED`라서, 생성 열로 잘못 걸러져 복사되지 않았다. 목적지에 발급 시각 대신 복사 시각이 들어간 것이다. 생성 열 판정을 `GENERATION_EXPRESSION` 기준으로 고쳤다.
- **control plane 전체 흐름도 통과했다.** MySQL probe(쓰기 거부, 단일 writer, 내용 비교, 업무 불변식, 테넌트 격리, 복원 훈련)를 만들었고, 워커 카탈로그가 `engine: mysql` 애플리케이션을 구성한다. MySQL 단위가 등록 → 컴파일 → 승인 → 수용 → 워커 실행까지 HANDOFF_COMPLETE로 끝났다. 계획의 모든 의무는 MySQL probe로 측정·서명되어 pass였다.

**Kafka API.**

- **단위 구성.** 토픽 하나와 그 producer, consumer, consumer group 위치가 단위다. 권한은 ACL로 판정한다(literal·prefixed·wildcard, `User:*`, ALL, DENY 우선).
- **차단 조건.** 차단은 producer 권한, 단위의 트랜잭션 ID 권한, consumer 권한을 모두 회수한다. 그리고 열린 트랜잭션이 없어야 한다(last stable offset = high watermark). 열린 트랜잭션은 차단 뒤에도 commit될 수 있기 때문이다. 시험에서 앱이 열어 둔 트랜잭션이 차단을 미확인으로 두었고, 운영자가 그 트랜잭션 ID로 fencing하자 확정됐다.
- **복사.** 목적지 레코드를 지운 뒤(DeleteRecords), 커밋된 레코드를 파티션마다 한 트랜잭션으로 다시 쓴다. 그래서 반복해도 같고, 중간에 끊겨도 반만 보이지 않는다.
- **consumer group 위치.** "앞선 레코드 수"로 옮긴다. consumer는 목적지에서, 원본에서 다음에 읽었을 바로 그 레코드부터 읽는다(30개 중 처리한 12개를 뺀 18개, 한 번씩).
- **클러스터 단위 자원.** group과 트랜잭션 ID는 토픽이 아니라 클러스터 단위 자원이다. 둘 다 토픽 권한 없이는 그 토픽에 쓰거나 커밋할 수 없다. 그래서 원본 차단과 목적지 활성화에서만 판정한다. 첫 구현은 목적지를 준비하면서 이를 회수해, 같은 클러스터에 있던 원본 앱의 트랜잭션까지 막았다.

**RabbitMQ.**

- **연결 종료와 그 근거.** 채널은 권한 검사를 캐시하므로, 차단은 단위 사용자의 연결을 모두 닫는다. 관리 API의 일반 연결 목록은 통계 주기만큼 늦다(실측: 새 연결이 3.8초 뒤에 나타났고, 그 전에는 이름으로 닫기가 404였다). 그 사이 차단이 확정되고, 캐시된 권한으로 차단 뒤에도 발행된 것을 시험이 잡았다. 그래서 연결은 연결 추적(`/connections/username/<user>`)으로 조회하고 닫는다.
- **응답하지 않는 클라이언트.** 브로커는 종료 제한시간(30초)이 지나야 연결을 놓는다. 차단은 그때까지 기다린다(실측 30.3초).
- **클라이언트 없이 쓰는 경로.** shovel이나 federation이 원본에 쓰고 있으면 차단을 확정하지 않는다.
- **복사와 확인 방식.** 메시지는 읽으면 바뀐다(재전달 표시, quorum 큐의 전달 횟수). 그래서 복사는 원본 메시지를 확인 없이 받아 목적지에 confirm으로 쓴 뒤, 원본에 순서대로 되돌린다. 읽기 확인은 정의와 개수만 비교한다. 목적지는 원본에 남은 22개를 순서대로, 첫 전달로, 속성까지 같게 내놓았다.

**한계.**

- MySQL TLS probe는 뒤에 시험용 MySQL 두 대(자체 CA)에서 실측했다([아래 변경 캡처 절](#mysql-변경-캡처-서버-복제-기반-snapshot-cdc-2026-11-17)).
- MySQL 위치(zone)는 DB에 붙일 표식이 없어, 제공자나 운영 인벤토리에서 읽는다.
- MySQL 변경 데이터 캡처는 뒤에 서버 복제 기반으로 추가했다([아래](#mysql-변경-캡처-서버-복제-기반-snapshot-cdc-2026-11-17)).
- Kafka 복사는 삭제 정책 토픽만 받는다. 압축 전용 토픽은 DeleteRecords를 쓸 수 없어서, 준비 단계에서 아무것도 바꾸기 전에 거부한다(시험함).
- Kafka와 RabbitMQ는 한 클러스터·한 브로커 안의 두 토픽·두 vhost로만 시험했다.
- Kafka와 RabbitMQ의 control plane 연결은 뒤에 추가했다([아래](#kafka-apirabbitmq의-control-plane-연결-2026-11-17)).

## 운영 서비스 (API·워커·배포, 2026-11-16)

지금까지 시험 코드가 직접 부르던 등록·컴파일·승인·수용·실행을 운영 서비스로 묶었다. 구조와 이유는 [ADR-0005](adr/0005-operational-service.md), 배포 방법은 [배포 안내](../deploy/kubernetes/README.md)에 있다.

| 구성 | 구현 | 하는 일 |
|---|---|---|
| 등록 저장소 | `PostgresRegistrations`, `registry_codec` | 등록 문서 다섯 종을 정규화된 형태로만 받는다. revision 비교 후 쓰기(경합에서 진 쪽은 409), 이력은 INSERT만, 같은 문서 재전송은 같은 revision |
| control plane | `ControlPlane` | 등록, 증명 수집(서명 확인), 컴파일(요청을 함께 저장), 승인(계획자는 자기 계획을 승인 못 함), 수용(저장된 요청으로 재컴파일 확인), 상태 조회, 중단 |
| HTTP API | `create_app`, `BearerAuthenticator` | OIDC RS256 token. 역할은 IdP realm role `sova:<role>`, tenant는 claim. 401/403/409/422 구분, 본문 1MiB 상한, 요청 ID |
| 워커 | `HandoffWorker`, `LeaseKeeper`, `KubernetesWorkerQuiescence` | 인계마다 실행 lease를 잡는다. 단계마다 효과 원장을 거쳐 실행하고, 의무를 probe로 측정해 서명 증거로 낸다. 사라진 워커는 정지 증명이 있을 때만 readback으로 대체한다 |
| 실행 설정 | `catalog.py`, `config.py` | 애플리케이션별 DB, writer role, 경로 저장소(Kubernetes Service). 비밀은 mount된 파일로만 받는다 |
| 프로세스 | `sova-service migrate`·`serve-api`·`run-worker` | Job 하나, Deployment 둘. JSON 로그. SIGTERM을 받으면 하던 일을 마치고 내려간다 |

**검증자 신뢰 공개는 API에 없다.** 어떤 키의 서명을 증거로 받을지는 두 사람의 관리 경로로만 정한다. API를 탈취해 자기 키를 신뢰시키는 길을 막으려는 것이다.

**워커 장애.** owner ID는 `<pod>.<uid>.<instance>`다. 단계 도중 사라진 워커의 lease가 만료되면, 다음 둘 중 하나일 때만 증명을 낸다.

- API에 그 Pod가 없다.
- 이전 owner가 같은 Pod의 이전 컨테이너다.

증명이 있으면 같은 epoch의 명령으로 readback만 한다. 단계가 lease보다 길면 keeper가 갱신하되, 정체 상한까지만 갱신한다. 이 둘은 설계 중에 실제 결함을 찾아 넣었다.

- 같은 Pod에서 재시작한 워커는 자기 단계를 복구하지 못했다. API로 보면 그 Pod가 계속 실행 중이기 때문이다.
- lease보다 긴 단계는 readback 기록이 `LeaseLost`로 거부됐다(실측).

### 로컬 kind 실측

`tests/integration/test_service_kubernetes.py`, 연속 두 번 통과했다.

- **배포.** 서비스 이미지(비 root, 기반 이미지 digest 고정)와 base 매니페스트를 Pod Security `restricted` namespace에 올렸다. 구성은 API ×2, 워커 ×2, migrate Job, PDB, RBAC, NetworkPolicy다. migrate Job이 빈 DB에 스키마를 적용했다.
- **인증과 수용.** Keycloak realm에서 역할(`sova:<role>`), 사용자 속성 tenant(관리자만 수정), audience mapper를 설정했다. 그 token으로 HTTP를 거쳐 등록 6건 → 증명 3건 → 계획 → 승인 → 승인 실행을 했다. witness는 Object Lock bucket이다.
- **워커 장애 복구.** 워커들이 인계를 경로 전환 단계까지 진행했다. 전환이 목적지 Pod를 기다리는 동안 lease를 가진 워커 Pod를 지웠다.

  | 시점 | 경과 |
  |---|---|
  | 다른 워커가 복구 lease를 받음 | 29.8초(lease 30초) |
  | 인계 완료 | 33.4초 |

  경로 전환 명령은 한 번만 발행됐고, readback으로 확정됐다. 완료 뒤 애플리케이션 role이 목적지에 쓴다.
- **API 무중단.** 클러스터 노드에서 Service로 요청을 계속 보내며 `rollout restart`를 했다. 실패한 요청은 0건이었다. 단, 이 측정은 NetworkPolicy를 지우고 했다. kind의 정책 엔진은 Pod 전환 때 새 연결을 가끔 버린다. 정책이 있으면 재시작 약 절반에서 1건이었고, 없으면 5회 모두 0건이었다.

## 파일 상태·세션·애플리케이션 단위 (2026-11-17)

흔한 웹 앱은 상태를 DB 말고도 볼륨 위 파일(SQLite, 업로드 폴더)과 프로세스 메모리(WebSocket 세션)에 둔다. 둘 다 회수할 권한 목록이 없다. 그래서 쓰기 차단을 "그것을 쓰는 Pod가 없음"으로 바꿔 구현했다. 그리고 한 앱의 여러 상태를 한 단위로 묶었다. 결정과 이유는 [ADR-0006](adr/0006-file-state-and-session-units.md)에 있다. 시험 앱은 데모와 같은 구조로 새로 만들었다(팀 코드는 가져오지 않았다).

| 단위 | 구현 | 결과 |
|---|---|---|
| 볼륨(SQLite·업로드) | `KubernetesVolumeHandoffAdapter`, `volume_agent`, `KubernetesVolumeProbes` | 적합성 9 + 고유 3 + probe 실패 시험 3, **15 PASS**. 결함 주입 2건 검출 |
| 메모리 세션(WebSocket) | `KubernetesSessionHandoffAdapter`, `KubernetesSessionProbes` | **3 PASS**. 결함 주입 1건 검출 |
| 애플리케이션 한 단위 | `CompositeHandoffAdapter`, `composite_execution` | control plane 전체 흐름 **2 PASS**(볼륨 단독, 게시판 + 게임 서버) |

**볼륨.**

- **복사.** 짧게 사는 agent Pod 둘이 한다. 원본에는 읽기 전용, 목적지에는 읽기·쓰기로 마운트하고, 워커는 API 서버의 Pod 프록시로 통신한다. 그래서 클러스터 사이에도 등록된 API 서버만 있으면 된다. 복사는 차이만 쓴다.
- **안전 장치.**
  - 파일은 원본 SHA-256 확인 뒤 rename으로만 최종 이름을 얻는다.
  - 경로는 구성 요소마다 `O_NOFOLLOW`로 연다. 볼륨 안 심볼릭 링크로 볼륨 밖을 읽거나 쓰는 시험이 거부됐다.
  - 최종 복사 중 원본이 바뀌면 차단이 안 된 것이므로 실패한다.
- **비교.** 모든 항목의 manifest를 비교한다(경로, 종류, mode, 소유자, 크기, SHA-256). agent는 root로 돌지 않으므로, 다른 사용자 소유 파일은 "다름"으로 읽힌다(시험함).
- **readback.** 바인딩되지 않은 claim을 건드리지 않고, agent를 남기지 않는다(시험함).
- **probe(adapter와 다른 방법).**
  - 쓰기 거부: 앱 자신의 쓰기 probe로 실제로 쓰고 되돌려 본다. 등록되지 않은 읽기·쓰기 마운트와, 관측 구간 동안의 트리 변화도 확인한다.
  - SQLite 내용: scratch로 복사한 뒤 논리 비교한다(무결성, 스키마, 테이블별 행 집합). WAL에만 있는 행도 읽고, 볼륨의 파일은 한 바이트도 바뀌지 않았다(시험함).
  - 업무 불변식: "모든 게시글의 첨부 파일이 있음" 같은 등록 질의로 확인한다.
  - 복원 훈련: 새 scratch claim으로 복원해 비교한 뒤 지운다.
  - 교차 테넌트: 다른 테넌트 볼륨을 넘겨받은 claim이 없는지 본다.
  - probe가 실패해야 할 때 실패하는 시험: 바뀐 행, 지워진 업로드, 사람이 띄운 Pod.

**메모리 세션.**

- **멈춤.** 원본 Pod를 경로에서 빼서(Pod가 없는 "paused" selector) 새 연결을 즉시 거부한다. 그다음 열린 세션이 끝나기를 기다린다. 명세에 동시 서비스 프로필이 없어 단일 권한 안에서 처리한다(ADR-0006 결정 4).
- **세션 수.** 원본 Pod에 붙인 ephemeral container가 커널 TCP 테이블에서 센다. 앱의 자기 보고는 쓰지 않는다. kubelet probe 연결은 제외하고, 오래된 값은 "모름"이다.
- **drain 상한.** 넘기면 `hold`(미확인 유지) 또는 `terminate`(차단이 끊을 세션 수를 증거로 남김)를 따른다. 둘 다 시험했다.
- **실측.** 진행 중이던 경기 3개가 원본에서 정상 종료됐다. 멈춤 동안의 새 연결은 거부됐고, 전환 뒤 새 경기는 목적지 Pod로 갔다. drain을 무시하게 바꾸는 결함을 넣자 시험이 잡았다.

**애플리케이션 한 단위.**

- **동작.** 각 행동을 모든 구성 요소에 수행하고, 모두 APPLIED일 때만 APPLIED다.
- **측정.** 쓰기·권한·위치 의무는 모든 구성 요소가 성립해야 한다. 내용·불변식·복원은 데이터 구성 요소가 답한다.
- **E2E.** 게시판(SQLite + 업로드)과 게임 서버(메모리 경기)가 등록 → 컴파일 → 승인 → 수용 → 워커 실행을 거쳐 HANDOFF_COMPLETE로 끝났다.
  - 진행 중이던 경기는 원본에서 끝났다.
  - 게시판의 행·무결성·업로드는 목적지에서 같았고, 새 글 ID는 이어서 발급됐다.
  - 새 경기는 목적지로 갔다.
  - 모든 의무가 측정·서명되어 pass였다.
  - 쓰기 정지는 32.5초(경기 drain 15초 포함)였다. 볼륨 단독은 27.5초였다.
- **실행 설정.** 워커 실행 설정에 `kubernetes-volume`, `kubernetes-session`, `composite` 항목을 추가했다.

**함께 찾아 고친 기존 결함.** `EffectObservation`은 증거를 8KiB까지만 받는데, 모든 adapter가 64KiB 한도로 증거를 만들었다. 그래서 많이 본 readback(다른 파일·테이블·파티션이 많을 때)은 판정 대신 오류로 끝났다(9000바이트로 재현). 이제 증거를 줄이고(긴 목록부터, 무엇을 줄였는지와 전체 digest를 남김), 적합성 시험이 실제 한도를 확인한다.

**한계.**

- 볼륨 비교는 매번 양쪽의 모든 바이트를 읽는다.
- 블록 볼륨, 하드 링크, 특수 파일은 다루지 않는다.
- ReadWriteOncePod 볼륨은 start_sync를 쓸 수 없다.
- API 서버와 agent Pod 사이는 평문이다. 그래서 검증된 전송 암호화를 요구하는 계약은 이 단위에서 실패로 측정된다.
- 세션 drain은 이미 맺어진 연결이 유지되는 것에 기댄다. kube-proxy(iptables)와 Envoy Gateway에서 확인했고, 다른 Gateway 구현체·서비스 메시에서는 따로 확인해야 한다.
- 관리형 클러스터에서는 실측하지 않았다. 클러스터 사이 복사는 아래처럼 로컬 kind 두 대로 실측했다.
- 진입점은 Service selector와 Gateway API(아래 절)다. Ingress는 다루지 않는다.

**쓰기 정지의 구성(실측, 2026-11-17).** 워커 수준에서 단계별로 재 보면, 볼륨 단위의 정지 구간(약 27초)은 대부분 Pod 수명 주기다.

- 멈춤·차단: 앱 workload를 내리고 Pod가 없어지기를 기다리는 데 각각 2~3초가 든다.
- probe: 독립성을 위해 각자 reader agent Pod를 따로 띄우고 지운다. 쓰기 거부 3.7초, 내용 비교 집합마다 약 2초, 복원 훈련 5.3초가 든다.

probe끼리 agent를 공유하면 줄일 수 있지만, probe의 독립성과 정리 보장을 다시 설계해야 한다. 그래서 이번에는 바꾸지 않았다.

### 클러스터 사이 볼륨 인계 (로컬 kind 두 대)

볼륨 설계의 전제는 "두 클러스터 사이에 등록된 API 서버 두 곳 말고는 필요한 것이 없다"였다. 이를 확인하려고 시험 환경에 두 번째 kind 클러스터를 추가했다(`test-env.sh second`). 두 번째 클러스터는 API 서버와 kubeconfig를 따로 가진다.

- **인계.** 원본 클러스터의 게시판을 두 번째 클러스터의 정지된 게시판으로 옮겼다. 원본 reader agent는 첫 클러스터에서, 목적지 writer agent는 두 번째 클러스터에서 돌았다. 바이트는 워커를 거쳐 갔다.
- **결과.** 두 번째 클러스터에서 앱이 같은 행·무결성·업로드로 시작했고, 원본 writer는 둘 다 쓸 수 없었다.
- **probe.** 두 클러스터에 걸쳐 다섯 가지가 통과했다: 양쪽 쓰기 거부, DB 논리 비교, 업로드 비교, 목적지 클러스터 안 복원 훈련.
- **복사 처리량**(128MiB 파일 하나, opt-in perf 시험):
  - 청크를 하나씩 보낼 때 17.5MiB/s였다.
  - 청크를 병렬(기본 4개)로 보내도록 바꾸자 26.6~28.4MiB/s가 됐다. 각 파일의 첫 청크가 먼저 도착해야 하므로, 첫 청크 → 나머지 → 확인 커밋의 세 단계로 나눴다.
  - 8스트림이나 8MiB 청크는 더 빠르지 않았다(27.3, 24.9MiB/s). 병목은 스트림 수가 아니라 API 서버 프록시 쪽이다.
  - 양쪽 검증(전체 SHA-256)은 1.4~2.3초였다.
- **오프라인 복사의 다운타임.** 대략 `볼륨 크기 / 처리량`이다. 이 환경에서 10GiB면 약 6분이다. 큰 볼륨은 snapshot-cdc 템플릿(`start_sync`로 미리 복사, 차단 안에서는 바뀐 파일만)으로 줄인다.

## Gateway API 진입점 전환 (2026-11-17)

Kubernetes 진입점의 표준은 Ingress에서 Gateway API로 옮겨 갔다(ingress-nginx는 지원이 끝났다). `GatewayHttpRoute`는 HTTPRoute의 규칙 하나를 경로 저장소로 쓴다. 데이터 adapter의 `commit_route`가 여기에 쓴다. ALB·URL map과 같은 원칙이다. 다음 셋이 모두 같은 쪽을 가리킬 때만 그 쪽으로 읽는다.

- **설정.** 규칙에 weight 있는 backend가 정확히 하나이고, 그것이 등록된 쪽의 Service와 포트다. 필터(미러링 등)가 붙은 규칙은 어느 쪽도 아니다.
- **수락.** 등록된 모든 상위 Gateway가 이 route의 **현재 세대**에 대해 `Accepted`와 `ResolvedRefs`를 보고한다. 구현체가 바로 이 버전을 받아들였다는 뜻이다.
- **실제 요청.** Gateway를 거친 표본 요청이 모두 그 쪽에서 답했다. 응답의 쪽 표식을 읽고, 추측하지 않는다. 운영에서는 공개 URL로 HTTPS 요청을 보내 응답 헤더를 읽는다(`http_side_observer`, 인증서 검증, 평문은 loopback에서만).

전환은 다음처럼 한다.

- 규칙이 등록된 쪽의 단순 backend가 아니면 덮어쓰지 않는다. 다른 사람의 분할 배포 같은 경우다.
- route의 `resourceVersion`을 먼저 확인하는 JSON Patch로 바꾼다.
- 셋이 맞을 때까지 기다린다(상한 있음).

**시험.** 시험 환경에 Envoy Gateway v1.9.3을 선택 사항으로 추가했다(`test-env.sh gateway`, 설치 파일 SHA-256과 이미지 digest 고정). 시험 요청은 API 서버의 Service 프록시를 거쳐 Envoy 프록시로 들어간다. 그래서 HTTPRoute의 설정이 아니라 Envoy가 실제로 내보내는 경로를 본다.

- kind 실측 4 PASS:
  - 설정·수락·응답이 모두 맞을 때만 이동한다.
  - 50:50 분할은 어느 쪽도 아니고, 덮어쓰지 않는다.
  - Gateway가 수락하지 않은 route는 어느 쪽도 아니다.
  - 데이터 adapter의 경로 단계가 Gateway에서 실제로 트래픽을 옮기고, 계획 밖에서 되돌리면 UNKNOWN이 된다.
- 클러스터 없는 판정 시험 17 PASS(규칙 모양, 세대, 표본, 실행 설정).
- 판정 조건 두 가지(현재 세대의 수락, 응답 표본)를 각각 빼는 결함을 넣자 시험이 잡았다.

워커 실행 설정에서 경로 저장소는 `kubernetes_service`와 `gateway_httproute` 중 하나다(`sova.service.route_stores`).

**Gateway를 거친 세션 drain.** 두 경로 저장소 모두 멈춤(pause), 멈춤 확인(paused), 원본에서 벗어남(left)을 제공하고, 세션 adapter는 이것만 쓴다.

- **Gateway에서 멈추는 방법.** 규칙의 backend를 비운다. 그러면 Gateway가 새 요청에 오류로 답하고, 이미 열린 스트림은 상류 연결을 유지한다.
- **멈춤 확인.** backend가 비었고, 상위 Gateway가 이 상태를 현재 세대로 수락했고, 어느 쪽도 표본 요청에 답하지 않아야 한다.
- **실측(Envoy Gateway, WebSocket 경기 2개).**
  - 경로를 멈추자 새 연결은 거부됐다.
  - 열린 경기는 원본에서 끝까지 갔다. 원본 Pod의 커널이 Envoy에서 온 연결을 세다가 0이 되자 멈춤이 확정됐다.
  - 전환 뒤 새 경기는 목적지로 갔다.

**한계.**

- Envoy Gateway 하나로만 시험했다. 다른 구현체(Istio, Cilium, NGINX Gateway Fabric 등)의 수락 보고 방식은 같은 표준이지만 실측하지 않았다.
- Ingress(구 API)는 다루지 않는다.

## probe 관측값 보존 (2026-11-17)

워커의 서명 증거에는 관측값의 SHA-256만 들어간다. 그래서 지금까지는 "pass"가 무엇을 측정한 결과인지 볼 수 없었다. 이제 관측값을 보존한다.

- **저장.** 워커가 서명하기 전에 관측값을 정규화 JSON 그대로 `evidence_observations`에 넣는다. 테넌트 RLS가 걸려 있고 INSERT만 된다.
- **무결성.** DB 제약이 `digest = sha256(내용)`을 강제한다. 다른 내용을 같은 digest로 넣거나 바꾸는 시험이 거부됐다.
- **조회.** 감사자는 API로 애플리케이션의 최신 서명 결과 목록을 받는다(의무, 결과, 관측 digest, 보존 여부). digest로 관측값을 받아 서명 기록과 직접 대조할 수 있다. 없는 관측값은 404이고, 다른 테넌트에게는 아무것도 보이지 않는다.
- **크기.** 관측값은 60KB 안으로 줄여 보존한다(증거 한도와 같은 방식).

## Redis 계열 키 공간 (Valkey·Redis, 2026-11-17)

세션, 장바구니, 순위표, 이벤트 스트림처럼 많은 웹 앱이 Redis 프로토콜 서버에 두는 상태를 옮긴다. 시험은 BSD 라이선스인 Valkey 9.1.3(digest 고정)으로 했고, 같은 프로토콜이라 Redis에도 적용된다.

- **단위와 writer.** 단위는 앱의 키 접두어(한 논리 DB)다. writer는 ACL 사용자다.
- **멈춤.** 앱 사용자를 `off`로 한다. 새 로그인만 막고, 이미 열린 연결은 차단 때 끊는다(시험함).
- **차단.** writer를 `off`로 하고, 키 접근을 모두 없애고(`resetkeys`), 연결을 끊는다(`CLIENT KILL USER`). 다음까지 성립해야 확정한다.
  - 다른 켜진 사용자 중 이 키에 쓸 수 있는 사용자가 없다. 판정은 서버 자신의 권한 검사(`ACL DRYRUN`)로 한다. 모든 키에 쓰는 `default` 사용자가 켜져 있으면 미확인이다(시험함).
  - 원본이 primary다. replica는 primary에서 ACL 밖의 쓰기를 받기 때문이다.
- **준비·활성화.** 원본의 writer ACL 규칙(비밀번호 해시, 키 패턴, 명령)을 목적지에 꺼진 채로 만든다. 활성화는 앱 사용자만 켠다.
- **복사.** `DUMP`/`RESTORE ABSTTL`이라 만료 시각이 그대로 간다. 12:00에 만료될 세션은 목적지에서도 12:00에 만료된다. 원본에 없는 키는 지운다.
- **비교.** 타입별 논리 내용과 만료 시각으로 비교한다. 같은 값이라도 서버마다 인코딩이 다를 수 있기 때문이다.
- **probe(adapter와 다른 방법).**
  - 쓰기 거부: 두 관점이 모두 동의해야 한다(ADR-0007). 하나는 각 writer로 새로 로그인해 실제 `SET`을 시도하는 것(트랜잭션 안, 버림)이고, 다른 하나는 primary와 등록된 모든 replica에서 본 서버의 권한 검사(`ACL DRYRUN`)다. 처음 구현은 시도만 봐서 ADR과 어긋나 있었다(아래 replica 절).
  - 집합별 논리 비교, 참조 불변식(모든 세션의 장바구니가 있음).
  - scratch DB 복원 훈련, 다른 테넌트 키 접근 거부, 연결의 실제 TLS 검증 여부.
- **시험이 잡은 결함.** 트랜잭션·PING 권한이 없는 writer(`+@read +@write`만)를 시도하던 probe가 판정 대신 예외로 끝났다. 이제 첫 명령으로 로그인 실패와 권한 거부를 구분한다. MULTI가 막힌 사용자는 1초 뒤 사라지는 고유 키에 실제 `SET`을 시도한다. 이 사용자도 쓸 수 있는 writer로 판정되는 것을 시험으로 고정했다.

| 시험 | 결과 |
|---|---|
| 적합성 키트 + 고유(만료 시각 보존, 열린 연결은 차단이 끊음, 켜진 default 사용자) | 12 PASS. 결함 주입 3건(다른 사용자 무시, 지운 키 잔존, 만료 시각 누락) 검출 |
| probe 실패 시험(살아 있는 writer, 바뀐 값, 사라진 장바구니, 다른 테넌트 키 허용, 비어 있지 않은 scratch DB, 평문 전송) | 2 PASS |
| control plane 전체 흐름 | 1 PASS. 모든 의무 pass, 쓰기 정지 10.1초(로그인 재시도 제거 뒤 1.75초), 복원 훈련 세션 20·장바구니 4 일치 |
| 워커 실행 설정(`engine: redis`) | 단위 시험 |

**한계.**

- **TLS(추가 실측).** 시험용 Valkey 두 대에 자체 CA의 TLS 포트를 열었다. 워커 실행 설정의 `ssl_ca`로 만든 연결이 서버 인증서와 호스트 이름을 검증하고, 전송 probe가 이를 pass로 잰다. 그 연결로 인계 전체를 수행했고, 서버를 보증하지 않는 CA는 거부됐다.
- Redis Cluster(슬롯 분산)는 다루지 않는다. replica는 아래 절처럼 등록해서 다루고, Sentinel의 자동 발견은 쓰지 않는다.
- `DUMP` 형식은 같은 계열·호환 버전 사이에서만 복원된다. 예를 들어 Redis 7.4 이후 형식을 Valkey가 읽는지는 버전마다 다르다.
- 해시 필드 단위 만료(HEXPIRE)는 복사되지만 비교에는 넣지 않았다.

**쓰기 정지 단축(추가).** 워커 수준에서 단계별로 재 보니, 쓰기 거부 probe 한 번이 약 8초였고 정지 구간에 세 번 들어 있었다. 원인은 redis-py 8의 기본 재시도다. 연결 오류를 10번까지 점점 길게 기다리며 다시 시도하는데, 로그인 거부(`AuthenticationError`)도 연결 오류의 하위 클래스다. 그래서 차단된 writer의 로그인 시도마다 몇 초씩 기다렸다. 워커 실행 설정이 만드는 writer 클라이언트도 똑같았다. 이제 writer 자신의 로그인은 한 번만 시도한다. 관리자 클라이언트의 재시도는 그대로 둔다. 쓰기 정지는 10.1초 → 1.75초, 인계 전체는 26초 → 2.8초가 됐다. Redis 시험 20 PASS(177초 → 45초).

### replica가 있는 서버 (2026-11-17)

Redis 계열 서버는 ACL 사용자를 노드마다 따로 가진다. 복제는 데이터만 나르고 ACL은 나르지 않는다. 그래서 차단이 primary에만 걸리면, 장애 조치로 replica가 승격되는 순간 차단된 writer가 다시 쓸 수 있었다. replica를 둔 운영 구성에서 실제로 생기는 빈틈이다.

- **모든 노드에 적용.** ACL 변경(준비, 멈춤, 차단, 활성화)은 primary와 등록된 각 replica(실행 설정 `replicas`)에 모두 간다. 접근 조건(writer 꺼짐·키 접근 없음, 다른 사용자 없음, writer 연결 없음, 활성화된 앱 사용자)도 모든 노드에서 성립해야 확정한다.
- **등록되지 않은 replica.** primary가 보고하는 replica 수가 등록된 수와 같아야 한다. 또 각 replica가 그 primary를 따라야 한다(같은 복제 ID, 링크 정상). 등록되지 않은 replica가 있으면 차단은 미확인으로 남는다. 차단이 닿지 않은 replica가 승격될 수 있기 때문이다.
- **probe.** 쓰기 거부의 서버 관점을 모든 노드에서 본다.
- **함께 고친 것.** 쓰기 거부 probe가 시도만 보고 있었다. 로그인은 거부되지만 키 권한이 남은 writer(이미 열린 연결은 여전히 씀)를 거부로 셌다. 이제 두 관점이 필요하고, 이 경우가 거부로 판정되지 않는 것을 시험으로 고정했다. 같은 시험에서 열린 연결이 실제로 쓰는 것도 확인했다.

| 시험(Valkey 9.1.3, 쪽마다 primary + replica 하나) | 결과 |
|---|---|
| 차단이 모든 replica에 닿고, 원본 replica를 승격해도 writer가 로그인하지 못함. 활성화는 목적지 replica에도 닿음 | PASS |
| replica의 ACL이 writer를 다시 들이면 차단 미확인, probe도 그 replica를 가리킴 | PASS |
| 등록되지 않은 replica가 있으면 차단 미확인(그 replica에서 writer가 여전히 쓸 수 있음을 함께 확인) | PASS |
| 결함 주입: primary에만 적용 / replica 수 확인 제거 | 각각 2건·1건 실패로 검출 |
| Redis 시험 전체 | 20 PASS |

## MySQL 변경 캡처: 서버 복제 기반 snapshot-cdc (2026-11-17)

MySQL은 지금까지 offline copy만 있어서, 쓰기 정지가 데이터 크기에 비례했다. `MySqlReplicationAdapter`는 서버 자신의 복제로 그 비례를 없앤다. writer, 차단, 활성화, 비교는 offline copy adapter와 같고, 데이터가 언제 움직이는지만 다르다.

- **`start_sync`.** 원본의 일관 스냅샷과 binlog 위치를 함께 잡는다. mysqldump처럼 전역 읽기 잠금을 스냅샷을 열고 위치를 읽는 순간만 건다. 원본이 계속 서비스하는 동안 그 스냅샷으로 복사한다. 그다음 목적지 서버가 바로 그 위치부터 단위 전용 복제 채널로 받는다. 채널은 원본 DB만 받고, 이름을 목적지 DB로 바꾼다. 행 이벤트로 받으므로 트리거가 두 번 돌지 않는다.
- **`apply_final_changes`(차단 뒤).** 원본의 현재 위치를 읽고, 채널이 거기까지 적용하기를 기다린다(상한 있음). 그다음 채널을 지우고 `AUTO_INCREMENT` 카운터를 맞춘다. 채널이 없어지고 내용·카운터가 같아야 완료로 읽는다. sync를 하지 않은 계획이면 여기서 전체를 복사한다.
- **중단.** `release_replication`이 채널을 지운다. 잊힌 채널은 원본의 binlog를 붙잡고, 목적지에 계속 쓰기 때문이다.
- **실측(서버 두 대, 2만 행 + 업무 쓰기).** 쓰기 정지 동안 목적지 서버가 쓴 행 수는 복제 방식에서 **0**이었다(바뀐 행은 정지 전에 이미 복제로 도착). offline copy는 **20,158**이었다. control plane 전체 흐름(snapshot-cdc 템플릿)에서 쓰기 정지는 1.69초였고, 모든 의무가 pass였다. 복원 훈련은 데이터가 정지 전에 목적지에 있으므로 정지 전에 측정됐다(ADR-0003).
- **시험이 잡은 것.**
  - 복제 필터가 바꾸기 전 이름을 가리키는 결함을 넣자 2건이 실패했다.
  - 마지막 카운터 복사를 빼는 결함은 처음에는 잡지 못했다. 커밋된 삽입은 복제가 카운터를 올리기 때문이다. 카운터 복사가 실제로 필요한 경우는 원본에서 **롤백된** 삽입이다(번호는 소비되지만 binlog에는 남지 않음). 그래서 시험을 그 경우로 바꿨고, 이제 잡는다.
  - 서버 두 대에서 기존 MySQL 복원 훈련 probe의 결함이 드러났다. scratch DB를 목적지 서버에 만들고, 읽을 때는 scratch DB 이름이 매핑된 서버로 접속했다. 서버가 하나일 때는 보이지 않았다. 만든 서버에서 읽게 고쳤다.
- **실행 설정.** `engine: mysql`, `mode: replication`. 다음을 함께 준다: 목적지 서버가 보는 원본 주소, 운영자가 원본에 만든 복제 계정(`REPLICATION SLAVE`), 그 비밀번호 파일, 선택적 TLS.

| 시험 | 결과 |
|---|---|
| 적합성 키트(서버 두 대, 두 템플릿) + 고유 4 | 13 PASS |
| control plane 전체 흐름(snapshot-cdc) | 1 PASS, 기존 단일 서버 MySQL E2E도 PASS |

**한계.**

- **TLS 실측(추가).** 시험용 MySQL 두 대에 자체 CA가 서명한 인증서를 넣었다(SAN: 127.0.0.1과 각 서버의 클러스터 DNS 이름). TLS는 제공하되 강제하지 않는다.
  - 전송 probe는 검증된 TLS 1.3 연결에서만 pass다. 서버가 TLS를 제공하면 인증서를 검증하지 않는 클라이언트도 암호화는 되지만, 그 경우는 pass가 아니다(시험함).
  - TLS를 요구하는 복제 채널은 목적지 서버의 CA 파일(`replication_ca_path`)로 원본 인증서와 호스트 이름을 검증한다. 원본을 보증하지 않는 CA로는 sync가 시작되지 않고 UNKNOWN으로 남는다(시험함).
  - 함께 찾은 결함: 워커 카탈로그가 MySQL CA를 `ssl={"ca": ...}`와 `ssl_verify_cert=True`로 함께 넘겼다. PyMySQL은 `ssl_*` 인자가 있으면 `ssl` 딕셔너리를 그 인자들로 바꾸므로 CA 파일이 버려지고, 시스템 CA 저장소로 검증한다. 그래서 사설 CA(예: RDS CA)로는 접속하지 못했을 것이다. `ssl_ca`·`ssl_verify_cert`·`ssl_verify_identity`(기본 켬)로 넘기게 고치고, 드라이버에 CA가 실제로 전달되는지 단위 시험으로 고정했다.
- **채널 연결 대기(추가).** `START REPLICA`는 채널이 원본에 닿기 전에 돌아온다. 부하가 걸리면 TLS 연결이 몇 초 걸려, 바로 뒤의 readback이 "연결 중" 채널을 보고 UNKNOWN을 냈다. 워커는 그 단계를 불확실로 멈추고 나중에 다시 확인한다. 4차 전체 회귀에서 이 경합이 드러났다. 이제 `start_sync`가 채널이 돌거나 오류를 보고할 때까지 상한을 두고 기다린다.
- **재연결(추가).** 6차 회귀에서 같은 시험이 다시 실패했다. 실패 시 readback 증거를 보이게 하자 원인이 나왔다. 막 닿게 된 원본이 첫 연결을 거부했고(errno 111), MySQL은 기본 60초 뒤에야 다시 연결한다. 운영에서는 잠깐의 네트워크 단절 뒤에 같은 일이 생긴다. 이제 다음과 같이 한다.
  - 채널은 5초마다 다시 연결한다(`SOURCE_CONNECT_RETRY`).
  - `start_sync`의 대기는 다시 연결하면 낫는 오류(호스트에 닿지 못함·이름 해석 전·연결 끊김)면 계속 기다린다.
  - 낫지 않는 오류(TLS 검증 실패, 로그인 거부)면 바로 돌아온다.
  - 고치기 전에는 단독 실행 4회 중 2회 실패했고, 고친 뒤 6회 연속 통과했다.
- **잠금 없는 스냅샷(추가, `replication.snapshot: lockless`).** 기본 스냅샷은 순간적인 전역 읽기 잠금을 건다. 이 잠금에는 `RELOAD`나 `FLUSH_TABLES` 권한이 필요한데(8.0.31부터 `FLUSH_TABLES`로도 됨, 실측), 관리형 서비스는 흔히 이 권한을 주지 않는다. 잠금 없는 경로는 다음 순서로 동작한다.
  1. binlog 위치를 먼저 읽고, 이미 기록된 커밋이 엔진에서 끝나도록 잠깐(1초) 기다린다.
  2. 일관 스냅샷을 열고 위치를 한 번 더 읽는다.
  3. 두 위치 사이의 변경은 복사본에도 있고 채널로도 다시 온다. 그래서 그 구간만 목적지가 멱등으로 적용한다(`replica_exec_mode=IDEMPOTENT`). 행 이벤트는 행 전체를 담으므로, 순서대로 적용하면 원본과 같아진다.
  4. 구간을 지나면 목적지 설정을 원래대로 되돌린다. readback이 구간이 닫혔는지 확인한다.
  - **조건과 대비.** 모든 단위 테이블에 기본 키가 있어야 한다. 없는 테이블은 중복 행이 남으므로 거부한다. 기다림이 덮지 못한 트랜잭션이 있으면 최종 비교가 인계를 멈춘다. 조용히 빠지는 경우는 없다.
  - **찾은 것.** 처음 구현은 sync가 끝나지 않았다. 적용 위치를 보이게 하고 재현하니, 다중 스레드 적용기가 이미 적용된 트랜잭션마다 약 1초씩 걸렸다(60건에 69초, 단일 스레드는 0.4초). 그래서 그 구간은 단일 스레드로 적용한다.
  - **목적지 설정의 보존.** 두 설정(`replica_exec_mode`, `replica_parallel_workers`)은 목적지 서버 전역 값이라 그동안 다른 채널에도 적용된다. 바꾸기 전 값을 목적지 DB에 저장했다가 sync, 최종 단계, 중단 때 거기서 되돌린다. 워커가 중간에 죽어도 운영자 설정이 남지 않는다.
  - **실측.** `RELOAD`·`FLUSH_TABLES`가 없는 원본 계정에서 기본 스냅샷은 거부됐다(1227). 잠금 없는 sync는 진행 중 쓰인 1,944건을 옮겼고, 운영자 설정도 되돌렸다. 기본 키 없는 테이블은 거부됐다. 결함 주입(멱등 구간 없음, 설정 미복원)은 모두 시험이 잡았다. 두 서버 MySQL 시험 20 PASS.
- 복제 중 원본의 DDL은 받는다. 다만 계획은 구조가 같다는 확인 뒤에만 활성화한다.

## Kafka API·RabbitMQ의 control plane 연결 (2026-11-17)

두 adapter는 있었지만 워커와 control plane에 연결되지 않아 서비스에서 쓸 수 없었다. 이제 독립 probe, 워커 실행, 실행 설정(`engine: kafka`, `engine: rabbitmq`)이 붙어, 다른 저장소처럼 등록 → 컴파일 → 승인 → 수용 → 워커 실행을 거친다.

**Kafka API probe.**

- **쓰기 거부.** 두 관점이 모두 동의해야 거부로 인정한다.
  - 실제 시도: 각 producer principal이 자기 자격 증명으로, 등록된 probe용 트랜잭션 ID 접두어 아래에서 레코드 하나를 보내고 트랜잭션을 버린다.
  - 브로커의 권한 판단: 그 principal로 물어 본 허용 작업(KIP-430)에 WRITE가 없다.
- **내용 비교와 불변식.** 내용은 probe 자신의 `read_committed` consumer로 레코드와 group 위치를 비교한다. 불변식은 등록 검사로 본다(모든 값이 필드를 가진 JSON, 또는 키 중복 없음).
- **복원 훈련.** scratch 토픽으로 복원해 비교하고 지운다.
- **다른 테넌트·전송.** 다른 테넌트 토픽은 쓰기·읽기 시도가 모두 거부되어야 한다. 전송은 이 클라이언트 설정의 TLS·호스트 검증 여부를 본다.
- **설정 파일 제한.** 연결 관련 키만 받는다. 코드를 불러오는 설정(`plugin.library.paths`)은 누가 썼든 거부한다(시험함).

**처음 구현이 잡힌 결함.** probe의 트랜잭션 ID가 principal에게 허용되지 않아, 쓸 수 있는 principal도 거부되었는데 이를 "쓰기 거부"로 셌다. 차단 확인이 잘못 통과할 수 있는 결함이다. 다음 두 가지로 고쳤다.

- probe 접두어를 등록값으로 받는다.
- 토픽 권한 거부와 로그인 거부만 거부로 센다.

그다음에도 토픽 거부가 트랜잭션 내부 오류로만 남고 전달 보고에는 abort의 `_PURGE_QUEUE`만 오는 경우가 있었다. 그래서 브로커의 권한 판단을 두 번째 관점으로 더했다. "probe가 돌 수 없지만 토픽 쓰기 권한은 남은 writer"를 거부로 판정하지 않는 것을 시험으로 고정했다.

**RabbitMQ probe.** 메시지는 읽으면 바뀌므로, 읽지 않고 잴 수 있는 것만 잰다.

- **쓰기 거부.** 실제 발행 시도(트랜잭션 안, 롤백)와 브로커의 권한 기록이 모두 동의해야 한다.
- **내용과 불변식.** 내용은 정의와 대기 메시지 수로 비교한다. 불변식은 등록된 개수 검사만 잰다(`same_count`: 인계로 작업이 사라지거나 늘지 않음, `max_ready`). 계약 스키마가 서비스마다 불변식을 최소 하나 요구하기 때문에 필요하다.
- **복원 훈련.** 정의만 scratch vhost에 복원해 비교한다. 메시지는 처리 중인 작업이라 복원 대상이 아니며, 관측값에 그렇다고 적는다.

| 시험 | 결과 |
|---|---|
| Kafka: control plane 전체 흐름, probe 실패 시험, 판정 불가 writer 시험 | 3 PASS. 쓰기 정지 8.9초(읽기 대기 제거 뒤 2.63초), 복원 훈련 30건 일치 |
| RabbitMQ: control plane 전체 흐름, probe 실패 시험 | 2 PASS. 쓰기 정지 1.68초 |
| 실행 설정 단위 시험(Kafka·RabbitMQ 항목, 위험 설정 거부) | 통과 |

**TLS 실측(추가).** 공유 시험 브로커는 평문이라, 시험 모듈마다 TLS만 받는 브로커를 따로 띄워 워커 실행 설정 경로로 실측했다(ADR-0007 결정 4). 전송 의무를 면제하지 않은 control plane 전체 흐름이 그 위에서 끝났다.

- **RabbitMQ.** AMQP는 5671, 관리 API는 https만 연다(자체 CA).
  - 찾아 고친 결함: 실행 설정의 `ssl_ca`가 AMQP에만 쓰이고, 같은 관리자 자격 증명이 오가는 관리 API 호출에는 쓰이지 않았다. 평문 http도 받아들였고, https라도 시스템 CA로 검증해 사설 CA 브로커에는 접속하지 못했을 것이다. 이제 `ssl_ca`가 있으면 두 경로가 같은 검증 context를 쓰고, 관리 API 주소가 `https://`가 아니면 거부한다. 전송 probe도 두 경로를 모두 본다.
  - 함께 찾은 결함: pika는 TLS 핸드셰이크 실패를 AMQP 연결 오류로 감싸지 않고 `ssl` 오류 그대로 올린다. 그래서 쓰기 시도 probe가 판정 대신 예외로 끝났다. 이제 판정 불가로 기록한다(실패 쪽으로 셈).
  - 실패해야 할 때 실패하는지: 평문 설정, CA 옆의 평문 관리 API 주소, 브로커를 보증하지 않는 CA를 시험했다.
- **Kafka API.** Redpanda의 Kafka 리스너를 TLS 하나만 연다(안에서 SASL/SCRAM). port-forward를 고정 포트로 열고 `localhost`로 광고한다. 서버 인증서에 `localhost`가 있다.
  - 바꾼 것: 전송 probe가 설정만 보고 판정했다. 이제 그 설정으로 메타데이터 요청까지 성공해야 통과한다. 설정이 요구한 검증을 실제 연결이 통과했다는 뜻이다.
  - 실패해야 할 때 실패하는지: 평문 설정, 호스트 검증을 끈 설정, 브로커를 보증하지 않는 CA를 시험했다.
  - 로컬 Redpanda의 쓰기 정지(당시 약 9초, 아래 개선 뒤 약 7초)가 예시 계약의 예산(5초)을 넘는다. 그래서 TLS 시험은 전송 의무는 그대로 두고, 평문 시험과 같은 로컬 예산만 쓴다.

| 시험 | 결과 |
|---|---|
| RabbitMQ: TLS 전체 흐름, 검증 안 되는 경로 시험 | 2 PASS. 기존 RabbitMQ 시험 13 PASS |
| Kafka: TLS 전체 흐름, 검증 안 되는 설정 시험 | 2 PASS. 기존 Kafka control plane 시험 3 PASS |

**Kafka 최종 단계의 증분 복사(추가).** snapshot-cdc 계획에서 `apply_final_changes`(차단 뒤)도 목적지를 비우고 토픽 전체를 다시 썼다. 그래서 쓰기 정지가 여전히 토픽 크기를 날랐다.

- 이제 목적지의 레코드가 파티션마다 원본 커밋 레코드의 앞부분과 정확히 같은지 내용 digest로 확인한다. 같으면 그 뒤 레코드만 파티션당 트랜잭션 하나로 덧붙인다.
- 원본의 보존 기간·compaction이 그사이 레코드를 지웠거나 목적지가 바뀌었으면 전체를 다시 복사한다.
- sync의 읽기는 마지막 안정 오프셋(열린 트랜잭션이 없는 지점)에서 멈춘다. 그래서 뒤에 커밋되는 레코드는 항상 그 뒤에 온다.
- 실측(630 레코드): 쓰기 정지 동안 목적지에 쓰인 오프셋은 증분 **11**, offline copy **636**이다. 11은 레코드 5개와, 파티션마다 commit 마커 1개와 fence 배치 1개다. fence 배치는 Redpanda가 트랜잭션이 파티션을 처음 건드릴 때 쓴다. 목적지가 앞부분이 아니면 전체 복사로 맞춰지는 것도 시험했다.

**서로 다른 두 클러스터 사이(추가).** 지금까지는 한 브로커의 두 토픽·두 vhost 사이만 시험했다. 이제 원본은 kind 클러스터 A의 공유 브로커(평문), 목적지는 클러스터 B에 띄운 모듈 전용 브로커(TLS만)로 control plane 전체 흐름을 돌렸다. 목적지에는 운영자가 하듯 단위의 계정을 미리 만들었다. Kafka는 probe용 트랜잭션 ID 접두어 권한도 함께 만들었다.

| 시험 | 결과 |
|---|---|
| Kafka: 클러스터 사이 전체 흐름 | PASS. 오프셋이 다른 클러스터에서 consumer group이 읽지 않은 18건부터 이어 읽음. 쓰기 정지 8.96초 |
| RabbitMQ: 브로커 사이 전체 흐름 | PASS. 정의와 대기 메시지 22건. 쓰기 정지 1.56초 |

adapter는 바꾸지 않았다.

**Kafka 쓰기 정지 단축(추가).** 정지 구간을 단계별로 재 보니 adapter 동작은 다 합쳐 약 2초였다. 가장 큰 단일 항목은 probe의 레코드 읽기(두 토픽 약 2초)였다.

- 원인: 빈 파티션까지 모든 파티션의 끝 이벤트를 기다렸고, 그때마다 fetch가 데이터를 최대 0.5초 기다렸다. 내용 비교, 불변식, 복원 훈련이 모두 이 방식으로 읽는다.
- 고친 것: 시작 오프셋과 마지막 안정 오프셋을 먼저 묻는다. 빈 파티션은 읽지 않고, 마지막 안정 오프셋에서 멈춘다. 마지막 오프셋이 트랜잭션 마커이면 끝 이벤트로 멈춘다.
- 결과: control plane 전체 흐름의 쓰기 정지가 한 브로커 8.9초 → 6.7초, 클러스터 사이 8.96초 → 7.09초로 줄었다. Kafka 시험 21 PASS.
- 두 번째 단계: 워커 수준에서 동작·readback·probe마다 시간을 재니, 토픽 전체 읽기마다 약 0.5초 대기가 두 가지 더 있었다.
  - copy가 쓴 파티션은 트랜잭션 마커로 끝나서, 끝 이벤트를 받으려면 fetch 대기(기본 500ms)가 걸린다. adapter의 읽기에도 짧은 대기를 줬다.
  - probe의 읽기는 `OFFSET_BEGINNING`으로 시작했는데, 클라이언트가 이 논리 오프셋을 따로 해석하느라 0.5초를 쓴다(실측 0.56초 대 0.07초). 이미 물어 둔 시작 오프셋으로 시작한다.
  - 결과: adapter readback 0.58초 → 0.10초, 내용 probe 1.16초 → 0.15초, 복원 훈련 1.86초 → 0.89초. 쓰기 정지는 한 브로커 **2.63초**, 클러스터 사이 **2.93초**로 예시 계약의 5초 예산 안에 들어왔다. Kafka 시험 21 PASS.

**한계.**

- RabbitMQ의 내용 비교는 개수 수준이다. 메시지 단위 동일성은 복사 시점의 confirm으로 확보한다(adapter 설명 참고).

## MongoDB (2026-11-17)

MERN 계열처럼 많은 웹 앱이 쓰는 MongoDB를 추가했다. 시험은 MongoDB 8.0.34(2주 이상 지난 판, digest 고정)와 pymongo 4.19.0으로 했다. 단위는 앱 DB의 등록된 컬렉션이고, writer는 MongoDB 사용자(`user@authdb`)다.

- **멈춤.** MongoDB에는 계정 잠금이 없다. 대신 앱 사용자의 로그인을 아무도 쓰지 않는 주소(TEST-NET-1)로 제한한다(`authenticationRestrictions`). 실측해 보니 서버가 바뀐 사용자를 열린 연결에서도 다시 확인한다. 그래서 이미 열린 연결도 다음 명령에서 인증을 잃는다. 다른 저장소의 계정 잠금보다 엄격하다.
- **차단.** 원본 DB에 대한 writer의 역할을 회수하고, 로그인을 제한하고, 실행 중인 작업을 끝낸다(`killOp`). 사용자 단위 세션 종료는 `impersonate` 권한이 필요한데, `root`에도 없다. 다음까지 성립해야 확정한다.
  - 다른 사용자 누구도 원본 DB에 쓸 수 없다(서버가 보고하는 상속 권한 포함). 전체 사용자 목록 조회는 권한 상세를 주지 않아, 한 명씩 정확히 조회한다.
  - writer의 작업이 남아 있지 않다.
- **준비·활성화.** 목적지 컬렉션을 원본의 옵션·인덱스와 같게 만든다. 계정은 MySQL처럼 운영자가 만들어 두고, 활성화는 앱 사용자에게만 `readWrite`를 주고 제한을 푼다.
- **복사·비교.** 원시 BSON으로 `_id` 순서대로 복사하고 비교한다. 필드 순서와 타입까지 같다.
- **probe(adapter와 다른 방법).**
  - 쓰기 거부: 일치하는 문서가 없는 update 시도(권한 검사만 받고 바뀌는 것 없음)와 서버가 그 사용자에게 보고하는 권한(`connectionStatus`)이 모두 동의해야 한다.
  - 내용: 정규 확장 JSON으로 비교한다(adapter는 원시 BSON).
  - 불변식: 등록 검사로 본다(키 고유성, 또는 결과가 없어야 하는 집계).
  - 복원 훈련은 scratch DB로, 다른 테넌트 DB 읽기는 거부되어야 하고, 전송은 TLS 검증 여부를 본다.
- **TLS.** 시험용 서버에 자체 CA로 TLS를 제공했다. 워커 실행 설정의 `tls_ca`로 만든 연결이 서버 인증서와 호스트를 검증하고, 그 연결로 인계 전체를 수행했다. 서버를 보증하지 않는 CA는 거부됐다.

| 시험 | 결과 |
|---|---|
| 적합성 키트 + 고유(인덱스 동반, 열린 연결의 권한 상실) | 11 PASS. 결함 주입 3건(다른 사용자 무시, 목적지 기존 문서 잔존, 인덱스 누락) 검출 |
| control plane 전체 흐름, probe 실패 시험 | 2 PASS. 쓰기 정지 1.21초 |
| 실행 설정의 검증된 TLS | 1 PASS |

### 변경 캡처: change stream 기반 snapshot-cdc

offline copy는 쓰기 정지가 데이터 크기에 비례한다. `MongoChangeStreamHandoffAdapter`(실행 설정 `mode: change_stream`)는 원본 자신의 change stream으로 그 비례를 없앤다. writer, 차단, 활성화, 비교는 offline copy adapter와 같다.

MySQL과 달리 서버끼리 복제 채널을 걸 수 없다. 그래서 상태를 두지 않는 워커에 맞춰, 두 단계에서 변경을 따라잡는다.

- **`start_sync`.**
  1. 원본의 과반수 커밋 시점을 읽는다.
  2. 원본이 계속 서비스하는 동안 문서를 복사한다(과반수 읽기). 읽는 데이터는 모두 그 시점 이후의 것이다.
  3. change stream을 그 시점부터 열어, 지금의 과반수 커밋 시점까지 변경을 적용한다.
  4. resume token을 목적지의 표시 컬렉션(단위 밖)에 둔다.
- **적용 규칙.** 문서 키 기준으로 멱등이라, 순서대로 적용하면 각 문서가 원본의 마지막 판으로 끝난다.
  - insert·replace: 이벤트의 문서를 쓴다.
  - update: 원본이 지금 가진 문서를 쓴다(`updateLookup`). 원본에서 사라졌으면 지운다.
  - delete: 지운다.
  - 문서는 원시 BSON을 유지한다.
- **`apply_final_changes`(차단 뒤).**
  1. 원본의 마지막 쓰기가 과반수 커밋되기를 기다린다(상한 `catch_up_seconds`). change stream은 과반수 커밋된 변경만 보여 주기 때문이다.
  2. 보관한 token부터 stream을 재개해, 그 쓰기를 지날 때까지 적용한다.
  3. 그사이 원본에 생긴 인덱스를 만든다.
  내용과 정의가 같아야 완료로 읽는다.
- **전체 복사로 대체.** sync를 하지 않았거나, 원본이 그 token으로 재개를 거부하면 여기서 전체를 복사한다. 거부되는 경우는 oplog가 token을 더 갖고 있지 않거나, 이 stream의 token이 아닐 때다. 원본은 이미 차단되어 있어 전체 복사가 정확하다. readback에 어느 쪽으로 맞췄는지와 이유를 적는다.
- **구조 변경.** sync 중 단위 컬렉션의 drop·rename이나 DB drop은 옮길 변경이 아니므로 오류로 끝낸다.
- **BSON 한도를 넘는 이벤트(추가).** 큰 문서를 갱신하면 이벤트가 지금의 문서 전체와 변경 내용을 함께 담아 16MB를 넘을 수 있다. 그러면 stream이 오류로 끝나 sync가 끝나지 않았다. 이제 stream 끝에 `$changeStreamSplitLargeEvent`를 두어 그런 이벤트를 조각(최상위 필드 단위)으로 받고, 합쳐 한 이벤트로 적용한다. 조각 사이에서는 적용·token 보관·멈춤을 하지 않으므로, 다시 돌려도 온전한 이벤트에서만 재개한다. 실측: sync 중 9MiB 문서를 갱신한 18.9MB 이벤트를 옮겼고, 분할 단계를 빼면 같은 시험이 서버의 크기 오류로 실패한다.
- **"지났다"의 판정.** token이 나타내는 클러스터 시각을 token에서 읽는다(Debezium 등이 쓰는 방식). 한 트랜잭션의 이벤트는 commit 시각을 공유한다. 그래서 이벤트는 그 시각을 넘어야, 빈 배치는 그 시각 이상이어야 지난 것으로 본다.

**시험이 잡은 결함 둘.**

- **계속 쓰이는 원본을 끝없이 쫓음.** 처음에는 빈 배치를 받았을 때만 도달 여부를 봤다. 원본이 계속 쓰이면 빈 배치가 오지 않아, 목표 시점을 지나서도 실시간 변경을 쫓았다. 이벤트가 있으면 마감 시간도 확인하지 않았다. 이벤트마다 도달을 판정하고 마감을 늘 확인하게 고쳤다. 지속 쓰기, 트랜잭션 공유 시각, 뒤처진 빈 배치, 마감을 단위 시험으로 고정했다.
- **복사 중 변경을 잃음(간헐적).** 처음에는 복사 전에 stream을 열어 그 token을 시작점으로 썼다. 그런데 바쁜 원본에서는 stream을 여는 aggregate의 첫 배치에 이미 이벤트가 담겨 와, pymongo가 시작 token을 주지 않는다. 그러면 복사 뒤 따라잡기가 "지금"부터 시작해 복사 동안의 갱신·삭제가 빠졌다. 4회 중 2회 재현했고, 빠진 문서를 직접 대조해 원인을 확인했다. 시작점을 복사 전에 읽은 과반수 커밋 시각(`startAtOperationTime`)으로 바꾼 뒤 6회 연속 통과했다.

| 시험 | 결과 |
|---|---|
| 적합성 키트(두 템플릿) + 고유 7 | 16 PASS |
| 쓰기 정지 동안 목적지에 쓴 문서(2만 건 + 업무 쓰기, 2회) | change stream **3건**·0.15~0.18초, offline copy **20,080건**·0.27~0.29초 |
| 복사 중 동시 쓰기(삽입·갱신·삭제·교체) | 6회 연속 PASS |
| 재개 거부 시 전체 복사 대체(서버의 실제 거부로) | PASS |
| control plane 전체 흐름(snapshot-cdc) | PASS. 쓰기 정지 1.21초(같은 실행의 offline copy 전체 흐름 1.65초) |
| BSON 한도를 넘는 갱신(9MiB 문서, 18.9MB 이벤트) | PASS. 분할 단계 제거 시 실패로 검출 |

작은 문서 2만 건에서는 정지 시간 차이가 작다. 차이는 정지 동안 쓰는 양이며, 데이터가 클수록 시간 차이로 커진다. 시험용 MongoDB는 이제 단일 구성원 레플리카셋(keyFile 내부 인증)으로 띄운다. 기존 MongoDB 시험도 모두 그 위에서 다시 통과했다(MongoDB 전체 30 PASS).

**한계.** 다음은 다루지 않았다.

- 샤딩 클러스터. change stream adapter는 레플리카셋 원본만 받는다.
- 실제 oplog 순환으로 인한 이력 유실. 새 레플리카셋의 oplog는 순환한 적이 없어, 서버의 다른 재개 거부(이 stream의 token이 아님)로 대체 경로를 시험했다.
- TTL 인덱스가 차단 뒤에 지우는 문서. 양쪽이 같은 시각에 지우지만, 비교 시점에 따라 일시적으로 다를 수 있다.

## probe 설계 원칙 (ADR-0007)

하루 사이 일곱 저장소에 probe를 붙이면서 같은 종류의 결함이 거듭 나왔다. 그래서 원칙을 [ADR-0007](adr/0007-probe-design-principles.md)로 정했다.

1. "쓸 수 없다"는 두 관점이 동의할 때만 인정한다: 남지 않는 실제 시도와, 서버 자신의 권한 판단.
2. writer에게 허용되는 명령을 가정하지 않는다.
3. 읽으면 바뀌는 것은 읽지 않는다.
4. TLS는 워커가 실제로 쓰는 설정 경로로 실측한다.
5. 관측값은 서명 digest와 함께 보존한다.

**전체 점검(같은 날).** 원칙을 정한 뒤 모든 저장소의 probe를 다시 읽었다. 원칙과 어긋난 곳은 다음과 같았다.

- **PostgreSQL.** 쓰기 시도에서 어떤 연결 오류든 거부로 셌다. 네트워크 단절이나 시간 초과도 "쓰기 차단됨"이 될 수 있었다. 다른 테넌트 접근 probe도 같은 방식으로 통과했다.
  - psycopg는 연결 단계 오류에 SQLSTATE를 주지 않는다(실측). 그래서 관리자 연결이 같은 서버에 지금 닿는지로 거부와 장애를 가른다. 닿지 않으면 판정 불가로 실패한다.
  - 서버 관점을 더했다: 단위 테이블에 쓸 권한과, 세션을 얻을 수 있는지(로그인·CONNECT, 또는 열린 세션).
- **MySQL.** 시도는 오류 코드로 엄격했지만 시도만 봤다. MySQL은 잠긴 계정의 열린 세션과 `REVOKE` 뒤 열린 세션의 전역 권한을 남긴다. 그래서 그 계정의 열린 세션 수를 서버 관점으로 더했다.
- **Redis 계열.** 시도만 봤다(위 replica 절).

세 저장소 모두 "로그인은 막혔지만 열린 세션은 실제로 쓰는 writer"를 거부로 판정하지 않는 것을 실측 시험으로 고정했다. 그 세션이 실제로 쓰는 것도 같은 시험에서 확인했다. PostgreSQL은 닿지 않는 서버가 판정 불가인 것도 고정했다. 결함 주입(서버 관점 무시, 닿지 않음을 거부로 셈)은 모두 시험이 잡았다.

## 로컬 통합 시험 환경

통합 시험에 필요한 서비스를 kind 클러스터 하나에 모아 `infra/test-env/test-env.sh up`·`down`으로 올리고 내린다. 들어 있는 것은 다음과 같다.
- PostgreSQL
- S3 호환 저장소 두 대
- Keycloak 두 대
- Kubernetes 시험용 namespace

호스트 주소는 기존 시험이 쓰는 loopback 주소 그대로라 시험 코드는 바꾸지 않았다.

검증된 TLS만 받는 PostgreSQL을 하나 더 둔다(`localhost:55433`). 이 저장소의 읽기 전용 경계와 쓰기 게이트는 인증서·호스트명이 검증된 TLS나 허가된 private Unix socket만 받기 때문이다. `run-suite.sh`가 평문 서버로 전체를, TLS 서버로 게이트 시험(`postgres_tls`)을 돌린다.

이 TLS 서버를 붙이다가 실제 결함을 찾아 고쳤다. 두 게이트가 TLS 사용 여부를 `connection.info.ssl_in_use`로 읽었는데, psycopg 3에는 그 속성이 없다(`connection.pgconn.ssl_in_use`가 맞다). 그래서 TCP 연결에서는 TLS 검사가 늘 `AttributeError`로 끝났다. 검증된 TLS로도 게이트를 지날 수 없었고, 평문 연결도 계약 오류가 아닌 예외로 끝났다. 그동안 "기준선 실패"로 분류된 37건 가운데 33건이 이 결함이었다. 고친 뒤 단위 시험이 검증된 TLS 통과와 평문·검증 없는 TLS·허가 없는 socket 거부를 확인한다. kubeconfig는 별도 파일에만 써서 사용자의 kubectl 현재 context를 건드리지 않는다. 고객 백업용 DB 도구 실행 방식은 `SOVA_TEST_POSTGRES_EXEC`로 바꿀 수 있게 했다. 이 환경에서 전체 회귀가 이전 개별 컨테이너 환경과 같은 결과(새 실패 0)를 낸다. 사용법은 [infra/test-env](../infra/test-env/README.md).

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
| 권한 DB 복원 감지·복구 | 모의 시험 통과 | 관리자 행 되돌림으로 복원 모사, 파일 witness와 Object Lock witness |
| 준비 상태 조회·계획 검토 명령 | 모의 시험 통과 | 단위, PostgreSQL |
| 활성화 뒤 원본 차단 감시 | 모의 시험 통과 | 로컬 PostgreSQL에서 권한 복원 실측(실제 snapshot 복원은 아래 AWS 행) |
| PostgreSQL 간 offline copy 참조 adapter·실측 probe | 모의 시험 통과 | 로컬 일회용 PostgreSQL에서 실제 권한·세션·데이터 변경과 실측 |
| AWS RDS PostgreSQL → 로컬 PostgreSQL offline copy | 실환경 시험 통과 | 개인 AWS 계정, RDS 17.11 db.t4g.micro → 로컬 17.11. control plane 전체 흐름 포함, 쓰기 정지 4501ms 1회 측정. 이 방향·프로필에 한함 |
| 활성화 뒤 원본 차단 감시, 실제 snapshot 복원(AT-32) | 실환경 시험 통과 | RDS snapshot 복원본에서 예전 writer 감지 |
| 기존 RDS 보안 설정 수집기 | 실환경 시험 통과 | 읽기 전용. 로그 내보내기 미설정은 `unknown`으로만 보고(빈틈) |
| adapter 공통 적합성 시험 | 모의 시험 통과 | PostgreSQL·S3 adapter, 결함 주입 9종 검출 |
| S3 호환 객체 저장소 간 offline copy 참조 adapter | 모의 시험 통과 | 로컬 versitygw 두 개(독립 계정·저장소·정책)에서 실제 정책 판정·복사·실측 |
| 객체 저장소 단위의 control plane 전체 인계 | 모의 시험 통과 | 같은 계획·승인·권한 기록·서명 증거, 객체용 측정. 업무 불변식 위반 시 활성화 전 보류 |
| AWS S3 → 로컬 S3 호환 저장소 offline copy | 실환경 시험 통과 | 개인 AWS 계정, IAM role writer. GCS·S3↔GCS는 아래 GCP·클라우드 간 절 |
| Kubernetes Service 경로 전환·CronJob 관리권 멈춤 실측 | 실환경 시험 통과 | 로컬 kind와 관리형 EKS·GKE Autopilot, 최소 권한 token |
| 클라우드 IAM 기반 쓰기 차단(GCS) | 실환경 시험 통과 | 버킷 권한 회수 + Policy Troubleshooter 판정 + 전파 대기. 프로젝트 수준 권한이 남은 writer는 미확인으로 둠 |
| 부하 분산기 수준 진입점 adapter | 실환경 시험 통과 | AWS ALB 리스너, GCP URL map. Kubernetes Gateway API는 아래 행, Ingress는 미구현 |
| Kubernetes Gateway API 진입점(HTTPRoute) | 모의 시험 통과 | 로컬 kind + Envoy Gateway v1.9.3. 설정·수락(현재 세대)·실제 응답이 모두 맞을 때만 그 쪽 |
| PostgreSQL 카탈로그 변경 수집기 | 모의 시험 통과 | 로컬 PostgreSQL, 시점 기반 귀속 |
| 권한 witness의 Object Lock 저장소 | 실환경 시험 통과 | AWS S3 governance·compliance 모드 모두 실측 |
| AWS CloudTrail 변경 수집기 | 실환경 시험 통과 | 이벤트 이력 조회, 실제 정책 변경 91초 뒤 기록. 조직 trail 미검증 |
| GCP Cloud Audit Logs 변경 수집기 | 실환경 시험 통과 | 관리 활동 로그, 실제 IAM 변경 19초 뒤 기록, 하위 경로(비밀 버전) 포함. 조직 sink 미검증 |
| Keycloak 간 계정 이전·OIDC 실측 probe | 모의 시험 통과 | 로컬 Keycloak 두 대에서 실제 로그인·서명 검증. 계획 템플릿 미연결 |
| AWS Cognito → Keycloak 계정 이전 | 실환경 시험 통과 | 실제 Cognito user pool에서 로컬 Keycloak으로. 페더레이션·MFA 사용자, Identity Pool은 미검증 |
| AWS Secrets Manager → Kubernetes Secret 이전 | 실환경 시험 통과 | 실제 비밀 두 개에서 kind Secret으로. 원본 자격 증명 폐기는 별도 단계 |
| ECS 원본 compute 중지·compute 독립성 실측 | 실환경 시험 통과 | 실제 ECS Fargate 서비스 중지와 재조회, 예전 주소 무응답 실측 |
| 사업자 탈출(`PROVIDER_EXIT_VERIFIED`), RDS+ECS → 온프레미스 | 실환경 시험 통과 | 네 수준을 모두 실측해 판정. 이 단위·프로필에 한함 |
| AWS 전체 탈출(RDS·S3·Secrets Manager·Cognito·ECS를 한 계약으로) | 실환경 시험 통과 | 계약의 Identity 의무 9개와 탈출 의무 10개(플랫폼 탈출 포함)를 실측, DNS 이름 전환 포함. 여섯 수준 모두 PASS. 시험 규모의 한 단위에 한함 |
| 플랫폼 탈출(`PLATFORM_EXIT_VERIFIED`) | 모의 시험 통과 | 로컬 환경에서 플랫폼 자격 증명 넷의 실제 거부와 고객 단독 운영 실측 |
| DNS 수준 트래픽 전환(Route 53) | 실환경 시험 통과 | 실제 호스팅 존에서 원자적 교체·권한 네임서버 응답 실측. 로드밸런서 전환 미시험 |
| DNS 수준 트래픽 전환(Cloud DNS) | 실환경 시험 통과 | 원자적 교체, 권한 네임서버에 DNS로 직접 질의 |
| Cloud SQL PostgreSQL → 로컬 PostgreSQL (offline-copy·snapshot-cdc) | 실환경 시험 통과 | 시험용 GCP 프로젝트, db-f1-micro → 로컬 17.11. 쓰기 정지 offline-copy 5405ms, snapshot-cdc 5965ms 각 1회 측정 |
| Cloud Storage → 로컬 S3 호환 저장소 | 실환경 시험 통과 | 같은 객체 저장소 adapter에 GCS 접근층 |
| Secret Manager·Cloud Run·Identity Platform 이전과 중지 | 실환경 시험 통과 | 비밀 3, compute 2, 계정 3개 시험 |
| GCP 전체 탈출 | 실환경 시험 통과 | 데이터·객체·비밀·계정·compute를 한 계약으로 |
| 운영 서비스: 등록 API·승인·수용·워커 | 모의 시험 통과 | 실 PostgreSQL·Keycloak·Object Lock witness. 역할 여섯, 계획자 자기 승인 금지, revision 비교 후 쓰기 |
| control plane 이중화(API ×2, 워커 ×2)·워커 장애 복구 | 모의 시험 통과 | 로컬 kind에 실제 배포. 경로 전환 중 워커 Pod 삭제 뒤 증명·readback으로 완료. 관리형 클러스터 배포는 미시험 |
| MySQL 간 offline copy adapter·probe | 모의 시험 통과 | 로컬 MySQL 8.4.13. 적합성 키트와 control plane 전체 흐름. TLS probe는 시험용 서버 두 대에서 실측 |
| MySQL 변경 캡처(서버 복제, snapshot-cdc) | 모의 시험 통과 | 시험용 MySQL 8.4.13 두 대. 적합성 키트, 정지 구간 쓰기 0행(offline 20,158행), control plane 전체 흐름 |
| Kafka API 토픽 인계 adapter | 모의 시험 통과 | 로컬 Redpanda(SASL/SCRAM, ACL). 한 브로커의 두 토픽과, 서로 다른 kind 클러스터의 두 브로커 사이. control plane 전체 흐름 연결(2026-11-17). TLS 리스너만 연 Redpanda에서 검증된 TLS로 전체 흐름. snapshot-cdc 최종 단계는 sync 이후 레코드만 씀(정지 중 오프셋 11, offline 636) |
| RabbitMQ 큐 인계 adapter | 모의 시험 통과 | 로컬 RabbitMQ 4.1.8. 한 브로커의 두 vhost와, 서로 다른 kind 클러스터의 두 브로커 사이. 개수 비교만(메시지 내용은 읽으면 바뀜). control plane 전체 흐름 연결(2026-11-17). TLS만 받는 브로커에서 AMQP·관리 API 모두 검증된 TLS로 전체 흐름 |
| Kubernetes 볼륨(파일 상태) 인계 adapter·probe | 모의 시험 통과 | 로컬 kind, SQLite·업로드 시험 앱. 적합성 키트, probe 실패 시험, control plane 전체 흐름, kind 두 대 사이 인계(128MiB 26.6~28.4MiB/s). 관리형 미실측 |
| 메모리 세션 drain adapter | 모의 시험 통과 | 로컬 kind, WebSocket 시험 서버. 커널 TCP 테이블로 세션 계수, drain 상한 hold·terminate |
| 애플리케이션 한 단위(볼륨 + 세션) | 모의 시험 통과 | 로컬 kind, 게시판 + 게임 서버를 control plane 전체 흐름으로. 쓰기 정지 32.5초 1회 |
| Redis 계열(Valkey) 키 공간 adapter·probe | 모의 시험 통과 | 로컬 kind의 Valkey 9.1.3 두 대. 적합성 키트, probe 실패 시험, control plane 전체 흐름, 실행 설정의 검증된 TLS. 등록된 replica까지 차단(승격 뒤에도 유지 실측). Cluster·Sentinel 자동 발견 미시험 |
| MongoDB 데이터베이스 adapter·probe | 모의 시험 통과 | 시험용 MongoDB 8.0.34 두 대. 적합성 키트, probe 실패 시험, control plane 전체 흐름, 실행 설정의 검증된 TLS. 단일 구성원 레플리카셋. 샤딩 미시험 |
| MongoDB 변경 캡처(change stream, snapshot-cdc) | 모의 시험 통과 | 시험용 MongoDB 8.0.34 레플리카셋 두 대. 적합성 키트, 정지 구간 쓰기 3건(offline 20,080건), 복사 중 동시 쓰기, 재개 거부 시 전체 복사, control plane 전체 흐름 |
| probe 관측값 보존·조회 | 모의 시험 통과 | 서명 digest와 일치를 DB 제약으로 강제, 테넌트 격리, API 조회 |
| MFA·이중 발급자·유출·SLO 실측 | 미검증 | 구현 없음 |
| 후보 전략 탐색(`NO_FEASIBLE_PLAN`·`SEARCH_INCOMPLETE`) | 모의 시험 통과 | 등록된 두 템플릿, 합성 자격 |
| 사고 보류·증거 기한·사고 복구 확장 | 지원하지 않음 | 명세가 스키마를 정하지 않음(S.9, ADR 전제). 있으면 `NEEDS_EVIDENCE` |
| 부분 효과(`PARTIAL_EFFECT`) 원장 연결 | 지원하지 않음 | 기존 원장에 상태 없음 |

인계 기록(권한 이력, 변경 사건, watermark 소유, 동결 계획, 구조 재확인, 감사)은 실행 역할로 DELETE·UPDATE·TRUNCATE가 실제로 거부됨을 시험했다(AT-37). 보존 기간·WORM은 별도 저장소의 몫이다.

실환경 시험 통과는 위 표에 적은 AWS 경로와 그 방향·프로필에만 해당한다. 다른 클라우드·방향·관리형 서비스로 일반화하지 않는다. 쓰기 정지 시간·복제 손실·세션 연속성은 측정하지 않았으므로 무중단·RPO=0·세션 무중단 이전을 주장하지 않는다.

## 기존 구현 재사용

- JSON decoder를 순수 domain 유틸리티로 옮기고 기존 `graph_codec.json_document` import를 유지했다. domain이 파일/네트워크 adapter에 의존하지 않도록 했다.
- 기존 보안 서명·현재 등록·보류 판정은 security가 소유한다. 등록 문서 인증과 의무 증거 판정은 그 서명 증거·검증자 신뢰 저장소와 `evaluate_gate`를 그대로 쓴다.
- 계획·승인·PostgreSQL 원장·outbox·실행권·효과 조정, 기존 실제 대응 실행기는 변경하지 않았다. 새 테이블(잠금, 권한·이력, 변경 사건·watermark, 구조 재확인)은 별도 마이그레이션이며 모두 강제 RLS이고 권한을 부여하지 않는다.
- 이 구현은 실제 all-writer fencing, 데이터 비교, 사고 복구, Cognito 사용자 이전, AI/Harbor E2E를 수행하지 않는다. 그런 실측은 adapter와 서명 증거 생산자가 맡고, 여기서는 그 결과를 요구하고 판정하는 쪽만 만든다.

## 원격 CI 상태 · 2026-11-14

개인 저장소의 Actions가 `enabled=false`여서 새 브랜치 push 뒤에도 실행이 생성되지 않았다. Python 3.11/3.14 workflow는 준비했지만 원격 통과로 표시하지 않는다. 현재 로컬 Python 3.12.16 시험 결과와 다음 인수 항목은 [개발 기록](development/contract-foundation-2026-11-14.md)을 따른다.
