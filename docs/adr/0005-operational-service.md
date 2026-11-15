# ADR-0005 · 운영 서비스 형태: 상태 없는 API·워커, 인계마다 lease, Pod 신원으로 정지 증명

- 날짜: 2026-11-16
- 상태: **채택**, 구현됨. 로컬 kind에서 실측했다.
- 관련: [ADR-0001](0001-contract-driven-foundation.md), [구현 안내 · 운영 서비스](../contract-foundation.md#운영-서비스-api워커배포-2026-11-16), [배포 안내](../../deploy/kubernetes/README.md).

## 맥락

지금까지 control plane은 라이브러리였다. 등록, 컴파일, 승인, 수용, 실행은 시험 코드가 직접 호출했다. 운영하려면 다음이 필요하다.

- 여러 사람과 역할이 원격으로 쓰는 입구
- 등록 문서의 동시 수정 처리
- 실행 주체가 죽거나 교체되어도 인계가 이어지는 구조
- 이 모두를 배포하고 갱신하는 방법

조건은 기존 안전 규칙을 하나도 약하게 하지 않는 것이다.

- 효과 원장이 APPLIED 기록을 다시 적용하지 않는다.
- 정지 증명 없이 복구 lease를 받지 못한다.
- 증거는 서명된 것만 받는다.

## 결정 1 · 프로세스 셋, 상태는 DB와 witness에만

이미지 하나를 명령으로 나눈다. `migrate`는 Job, `serve-api`와 `run-worker`는 각각 Deployment다.

- replica는 상태를 갖지 않는다. 등록, 계획, 승인, authority, lease, effect ledger는 PostgreSQL에 있다(RLS, 실행 역할은 객체를 소유하지 않음).
- authority witness는 모든 replica가 함께 쓰는 Object Lock bucket이다. file witness는 RWX 볼륨이 필요해 다중 replica 배포에서는 쓰지 않는다.
- 스키마는 순서와 SHA-256으로 고정한 migration runner 하나가 적용한다. 시험 fixture도 같은 runner를 쓴다. 이 릴리스의 migration이 모두 적용되기 전에는 다음처럼 막힌다.
  - API readiness가 실패한다.
  - 워커는 일을 잡지 않는다.

## 결정 2 · 등록은 revision 비교 후 쓰기, 이력은 지우지 않는다

등록 문서 다섯 종(계약, snapshot, 환경 프로필, 능력 manifest, 전략 자격)은 정규화된 형태만 받는다. 저장은 다음 두 테이블로 한다.

- head 테이블: `WHERE revision = expected`로 바꾼다. 경합에서 진 쪽은 409를 받고, 현재 revision을 다시 읽는다.
- 이력 테이블: INSERT만 한다.

같은 문서를 다시 보내면 같은 revision을 돌려준다. 그래서 재시도가 안전하다.

## 결정 3 · 사람은 OIDC token과 역할 여섯으로

- 역할은 등록자, 증명자, 계획자, 승인자, 운영자, 감사자 여섯이다. IdP의 realm role `sova:<role>`로 주고, tenant는 token claim(`sova_tenant`)으로 준다.
  - 다른 tenant 경로는 403이다.
  - 계획을 만든 사람은 같은 계획을 승인할 수 없다.
- token은 RS256과 JWKS로 검증한다. 모르는 kid가 오면 JWKS를 다시 읽되, 1분에 한 번까지만 읽는다.
- JWKS는 HTTPS로만 받는다. 예외는 loopback 시험 발급자 하나다.

## 결정 4 · 검증자 신뢰 공개는 API에 넣지 않는다

어떤 키의 서명을 증거로 받을지는 두 사람의 관리 경로(`PostgresVerifierRegistry.publish`)로만 정한다. API에는 그 경로가 없다.

API 권한을 탈취한 사람이 자기 키를 신뢰시키면, 증거를 위조해 모든 판정을 통과시킬 수 있다. 신뢰 공개는 판정 자체를 바꾸는 행위라서, 등록이나 승인보다 높은 통제(두 사람, 별도 경로)를 둔다.

## 결정 5 · 워커는 인계마다 lease, 잃어버린 워커는 증명으로만 대체

- **lease 단위.** 워커는 몇 개든 돈다. 한 인계는 실행 lease(`handoff-<plan>`)를 가진 워커 하나만 진행한다. 나머지는 `ExecutionBusy`로 건너뛰고 다른 인계를 맡는다. 단일 활성 워커(리더 선출)는 택하지 않았다. 인계 수만큼 수평으로 늘릴 수 있고, 한 워커의 장애가 다른 인계를 멈추지 않는다.
- **owner ID.** `<pod 이름>.<pod UID>.<프로세스 instance>`. instance는 프로세스가 시작할 때마다 새로 정한다.
- **정지 증명.** 단계 도중 사라진 워커의 lease가 만료되면, 다음 둘 중 하나가 성립할 때만 증명을 낸다.
  - Kubernetes API에 그 이름과 UID의 Pod가 없거나 실행 중이 아니다.
  - 이전 owner가 같은 Pod의 이전 컨테이너다. Pod에는 워커 컨테이너가 하나뿐이고, kubelet은 컨테이너가 멈춘 뒤에만 다시 시작한다. 그래서 지금 프로세스가 살아 있으면 이전 프로세스는 살아 있을 수 없다.

  두 번째 조건이 없으면 같은 Pod 안에서 재시작한 워커가 자기 단계를 영영 복구하지 못한다. API로 보면 그 Pod가 계속 실행 중이기 때문이다.

  확실하지 않은 경우에는 증명을 내지 않고, 사람에게 남긴다. API에 닿지 않을 때와 owner ID가 Pod를 가리키지 않을 때가 그렇다.
- **복구.** 증명이 있으면 복구 lease를 받는다. 그 단계는 잃어버린 워커와 같은 epoch의 명령으로 readback만 한다. 다시 실행하지 않는다. readback이 APPLIED면 이어 가고, UNKNOWN이면 SAFE_HOLD다.
- **긴 단계.** 단계가 lease보다 길면 keeper thread가 lease를 갱신한다(대용량 복사 등).
  - 실측: keeper 없이 lease 2초에 5초짜리 단계를 돌리면 readback 기록이 `LeaseLost`로 거부됐고, 인계는 복구 대기로 빠졌다.
  - 갱신에는 상한(`SOVA_WORKER_STALL_SECONDS`)이 있다. 상한을 넘으면 갱신을 멈춰 lease가 만료되게 한다. 같은 상한으로 liveness도 실패해 kubelet이 워커를 재시작하고, 그 다음 instance가 위의 두 번째 조건으로 복구한다. 멈춘 워커가 lease를 영원히 쥐고 있지 않게 하려는 것이다.
- **종료.** SIGTERM을 받으면 현재 단계를 끝내고 lease를 놓는다. 다른 replica는 복구 없이 다음 단계부터 이어 간다.

## 결정 6 · 배포

- 컨테이너는 다음 조건으로 돈다.
  - 비 root
  - 읽기 전용 루트 파일 시스템
  - capability 모두 제거
  - seccomp RuntimeDefault
  - namespace는 Pod Security `restricted`
- rollout과 중단은 다음으로 다룬다.
  - API와 워커 모두 `maxUnavailable: 0`
  - API는 PDB `minAvailable: 1`, preStop 5초로 endpoint에서 먼저 빠진다
  - 워커는 PDB `maxUnavailable: 1`, 종료 유예 300초
- 권한은 최소로 둔다.
  - API는 service account token을 mount하지 않는다.
  - 워커는 자기 namespace에서 Pod `get`만 한다.
  - 애플리케이션 경로 권한은 그 namespace의 소유자가 따로 준다.
- 비밀은 파일로만 받는다. DSN, witness 키, 증거 키, 실행 파일이 모두 mount된 Secret이다.

## 검증 (로컬 kind, 2026-11-16)

| 시험 | 결과 |
|---|---|
| 단위: 설정·카탈로그·owner ID | 12 PASS |
| 워커(실 PostgreSQL) | 5 PASS: 완료, 다른 워커가 가진 인계는 건너뜀, 단계 도중 사라진 워커는 증명이 있을 때만 대체, 같은 Pod 재시작 복구, lease보다 긴 단계 |
| 배포(kind) | 2 PASS. 연속 두 번 통과 |

배포 시험이 확인한 것은 다음과 같다.

- 이미지와 base 매니페스트로 배포했다. migrate Job이 빈 DB에 스키마를 적용했다.
- Keycloak token으로 HTTP를 거쳐 등록 → 증명 → 계획 → 승인 → 승인 실행을 했다.
- 경로 전환 중에 lease를 가진 워커 Pod를 지웠다.
  - lease(30초)가 만료된 직후인 29.8초에 다른 워커가 복구 lease를 받았다.
  - 경로 전환 명령은 한 번만 발행됐고, readback으로 확정됐다.
  - 33.4초에 완료됐다.
- API `rollout restart` 중에 Service로 보낸 요청 실패는 0이었다.

## 한계

- 무중단 측정은 NetworkPolicy를 지우고 했다. kind의 정책 엔진은 Pod 전환 때 새 연결을 가끔 버린다. 정책이 있으면 재시작 약 절반에서 1건, 없으면 0건이었다. 운영 CNI에서는 따로 확인해야 한다.
- 실 클라우드 관리형 Kubernetes에는 아직 배포하지 않았다(EKS·GKE에서는 adapter 통제만 실측했다).
- 워커 카탈로그는 아직 PostgreSQL adapter만 구성한다. 경로 저장소는 Kubernetes Service만이다.
