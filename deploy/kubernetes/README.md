# Sova control plane 배포

control plane은 이미지 하나(`deploy/service/Dockerfile`)를 세 가지 명령으로 실행한다.

| 명령 | Kubernetes 객체 | 하는 일 |
|---|---|---|
| `sova-service migrate` | Job `sova-migrate` | 스키마 적용과 runtime role 권한 부여. 스키마 소유자 계정으로 실행 |
| `sova-service serve-api` | Deployment `sova-api` ×2, Service, PDB | HTTP API. OIDC bearer token으로 인증 |
| `sova-service run-worker` | Deployment `sova-worker` ×2, PDB | 승인된 handoff 실행 |

replica는 모두 상태가 없다. 등록, 계획, authority, lease, effect ledger는 DB에 있다. authority witness는 모든 replica가 함께 쓰는 Object Lock bucket이다.

## 구성

- `base/`: 환경에 무관한 매니페스트. namespace, 이미지, 설정값은 overlay가 정한다.
- `overlays/kind/`: 로컬 시험 환경(`infra/test-env`)용 overlay.
- 이미지 빌드: `SOVA_SECURITY_SRC=<고정 sova-security checkout> deploy/service/build-image.sh <image:tag> [--kind <cluster>]`.
  - 운영에서는 sova-security wheel을 사내 패키지 저장소에서 받는다.
  - 운영에서는 이미지를 digest로 고정한다.

## 운영자가 만드는 Secret

git에는 두지 않는다. 값은 파일로 mount되고, 환경 변수에는 경로만 들어간다.

| Secret | 키 | 내용 |
|---|---|---|
| `sova-database` | `runtime-dsn` | runtime role의 연결 문자열 (API, 워커) |
| | `migration-dsn` | 스키마 소유자의 연결 문자열 (migrate Job만 사용) |
| `sova-witness` | `config.json` | witness 저장소 설정. `endpoint`, `region`, `addressing_style`, `retention_seconds`, `mode`, 그리고 선택 항목 `access_key_id`·`secret_access_key` |
| `sova-worker-evidence` | `key` | 워커 증거 서명 키. Ed25519, 32바이트 hex |
| `sova-execution` | `execution.json` 등 | 워커 어댑터 카탈로그와 그 파일들. 형식은 `src/sova/service/catalog.py` |

`sova-witness`에 접근 키가 없으면 Pod의 workload identity를 쓴다(IRSA, Workload Identity).

runtime role은 운영자가 만든다. 조건은 다음과 같다.

- 객체를 소유하지 않는다.
- RLS를 우회할 수 없다: `NOSUPERUSER NOBYPASSRLS NOINHERIT`.

## 설정 (ConfigMap `sova-config`)

| 키 | 값 |
|---|---|
| `SOVA_OIDC_ISSUER` | IdP 발급자 |
| `SOVA_OIDC_AUDIENCE` | IdP 수신자 |
| `SOVA_OIDC_JWKS_URL` | JWKS 주소. HTTPS만 허용. loopback 시험 발급자일 때만 예외 |
| `SOVA_TENANT_CLAIM`, `SOVA_ROLES_CLAIM`, `SOVA_ROLE_PREFIX` | tenant claim과 역할 claim. 기본값은 `sova_tenant`, `realm_access.roles`, `sova:` |
| `SOVA_WITNESS` | `s3://<bucket>`. 모든 replica가 공유하는 Object Lock bucket |
| `SOVA_TENANTS` | 워커가 맡는 tenant 목록 |
| `SOVA_LEASE_SECONDS` | 실행 lease 길이 |
| `SOVA_WORKER_STALL_SECONDS` | 진척이 없을 때 liveness가 실패하기까지의 시간. 한 단계의 최대 실행 시간이기도 하다. lease보다 길어야 한다 |
| `SOVA_RUNTIME_ROLE` | migrate가 권한을 주는 role |

DSN은 시작할 때 읽는다. Secret을 바꾸면 rollout restart가 필요하다. role 비밀번호와 service account token은 쓸 때마다 다시 읽는다.

## 애플리케이션 경로 권한

워커는 애플리케이션의 트래픽을 Service selector로 옮긴다. 권한은 애플리케이션 소유자가 자기 namespace에서 준다.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: sova-route, namespace: orders}
rules:
  - {apiGroups: [""], resources: [services], resourceNames: [orders], verbs: [get, patch]}
  - {apiGroups: [""], resources: [pods], verbs: [list]}
  - {apiGroups: [discovery.k8s.io], resources: [endpointslices], verbs: [list]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: sova-route, namespace: orders}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: sova-route}
subjects: [{kind: ServiceAccount, name: sova-worker, namespace: sova-system}]
```

워커는 그 namespace를 Kubernetes 등록 문서로만 다룬다. 등록 문서에는 다음이 고정되어 있다.

- API 주소(`https://kubernetes.default.svc`)와 그 ClusterIP
- CA digest
- namespace UID

## 배포 순서

1. Secret을 만든다.
2. `kubectl kustomize overlays/<env> | kubectl apply -f -`
3. `sova-migrate` Job이 끝날 때까지 기다린다.
   - Job 이름에는 릴리스마다 suffix를 붙인다. Job template은 바꿀 수 없기 때문이다.
   - Job이 끝나기 전에는 API readiness와 워커가 대기한다. 이 릴리스의 스키마가 아직 없기 때문이다.

## 장애와 종료

| 상황 | 동작 |
|---|---|
| API `SIGTERM` | endpoint에서 빠진다(preStop 5초). 진행 중인 요청을 끝내고 종료한다 |
| 워커 `SIGTERM` | 현재 단계를 끝내고 lease를 놓는다. 다른 replica가 다음 단계부터 이어서 한다. 복구 절차는 필요 없다 |
| 워커가 단계 도중 사라짐 | lease가 만료되고, 사라졌다는 증명이 있어야 다른 워커가 복구 lease를 받는다. 그 단계는 다시 실행하지 않고 readback으로 결론을 낸다 |
| 워커가 같은 Pod 안에서 재시작 | 새 프로세스는 owner ID의 instance가 다르다. 자기 Pod의 이전 컨테이너가 멈췄음을 스스로 증명하고 복구한다 |
| 단계가 lease보다 김 | keeper thread가 lease를 갱신한다. `SOVA_WORKER_STALL_SECONDS`를 넘으면 갱신을 멈추고, liveness가 워커를 재시작한다 |

"사라졌다는 증명"은 둘 중 하나다.

- API에 그 이름과 UID의 Pod가 없다.
- 이전 owner가 같은 Pod의 이전 컨테이너다. kubelet은 컨테이너가 멈춘 뒤에만 다시 시작한다.

## 검증

`tests/integration/test_service_kubernetes.py`가 kind에 실제로 배포해 확인한다. 실행하려면 `SOVA_TEST_SERVICE_IMAGE`에 이미지를 지정한다.

- **배포**: 이미지와 base 매니페스트를 그대로 쓴다.
  - namespace는 Pod Security `restricted`다.
  - migrate Job이 빈 DB에 스키마를 적용한다.
- **인증**: Keycloak realm을 만든다. 역할은 `sova:<role>`, tenant는 사용자 속성(관리자만 수정 가능), audience mapper를 붙인다. 등록 → 증명 → 계획 → 승인 → 승인 실행을 HTTP로 한다.
- **장애 복구**: 워커들이 handoff를 경로 전환 단계까지 진행한다. 목적지 Pod가 아직 없어 전환이 대기하는 동안, lease를 가진 워커 Pod를 지운다.
  - 실측: lease(30초)가 만료된 직후 다른 워커가 복구 lease를 받았다(29.8초).
  - 경로 전환은 한 번만 발행됐고, readback으로 확정됐다.
  - handoff 완료까지 33.4초 걸렸다.
- **API 무중단**: 노드에서 Service로 요청을 계속 보내며 `rollout restart`를 한다. 실패한 요청은 없었다.

## 알려진 한계

- **kind의 NetworkPolicy 유실**: kind의 정책 엔진(kindnet)은 Pod가 시작하거나 멈출 때 새 연결을 가끔 버린다. 규칙이 Pod 상태를 늦게 따라가기 때문이다.
  - 측정: 정책이 있으면 재시작 약 절반에서 연결 시간 초과가 1건 났다. 정책이 없으면 5회 모두 0건이었다.
  - 그래서 무중단 측정은 정책을 지우고 한다.
  - 운영 CNI에서는 rollout 중 연결 유실 여부를 따로 확인해야 한다.
- **egress 정책은 overlay 몫**: DB, IdP, witness, 애플리케이션 API의 위치가 환경마다 다르다.
- **file witness는 다중 replica에 쓸 수 없음**: 모든 replica가 같은 파일을 봐야 해서 RWX 볼륨이 필요하다. 다중 replica 배포는 S3 Object Lock witness를 쓴다.
- **검증자 신뢰 공개는 API에 없음**: 공개는 두 사람의 관리 경로(`PostgresVerifierRegistry.publish`)로만 한다. API 권한을 탈취한 사람이 자기 키를 신뢰시키지 못하게 하려는 것이다.
