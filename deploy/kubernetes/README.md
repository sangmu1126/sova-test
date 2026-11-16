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

### 볼륨·세션 단위의 추가 권한

파일 상태(볼륨) 단위와 메모리 세션 단위는 위 경로 권한에 더해 다음 권한이 필요하다([ADR-0006](../../docs/adr/0006-file-state-and-session-units.md)). 이 권한도 애플리케이션 소유자가 자기 namespace에서만 준다.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: sova-units, namespace: board}
rules:
  # writer 워크로드를 멈추고 다시 올린다
  - {apiGroups: [apps], resources: [deployments, statefulsets], verbs: [get, patch]}
  - {apiGroups: [batch], resources: [cronjobs], verbs: [get, patch]}
  - {apiGroups: [batch], resources: [jobs], verbs: [list]}
  # 볼륨: 목적지·복원용 claim, 짧게 사는 agent Pod와 그 프록시
  - {apiGroups: [""], resources: [persistentvolumeclaims], verbs: [get, list, create, patch, delete]}
  - {apiGroups: [""], resources: [pods], verbs: [get, list, create, delete]}
  - {apiGroups: [""], resources: [pods/proxy], verbs: [get, create, update]}
  # 세션: 원본 Pod에 붙이는 관찰 컨테이너와 그 출력
  - {apiGroups: [""], resources: [pods/ephemeralcontainers], verbs: [patch]}
  - {apiGroups: [""], resources: [pods/log], verbs: [get]}
```

- **agent Pod.** 고정된 Python 이미지에서 표준 라이브러리만으로 돈다. 다음 조건을 지키므로 restricted Pod Security를 통과한다.
  - non-root로 돈다(`run_as`).
  - capability를 모두 뺀다.
  - root 파일 시스템은 읽기 전용이다.
  - 서비스 계정 token을 마운트하지 않는다.
  - 한 시간이 지나면 스스로 끝난다.
- **이미지 가져오기.** 이미지가 노드에 없으면 받아 와야 한다. 사설 레지스트리를 쓰는 곳에서는 같은 digest를 미러링해 `agent_image`로 지정한다.
- **agent 키.** 워커의 Secret에 hex로 넣고 `agent_key_file`로 가리킨다. 어느 워커 replica든 같은 키로 agent와 통신한다.
- **NetworkPolicy.** API 서버의 Pod 프록시는 agent Pod의 8080 포트로 들어온다. 애플리케이션 namespace에 기본 거부 정책이 있다면, `app.kubernetes.io/managed-by: sova` Pod의 8080 수신을 허용해야 한다.
- **API 서버 → agent 구간.** 이 구간은 클러스터 네트워크의 평문 HTTP다. 검증된 전송 암호화를 요구하는 계약에서 이 단위는 실패로 측정된다.

### 저장소별 실행 설정 항목

워커의 실행 설정 파일(`SOVA_EXECUTION_FILE`)은 애플리케이션마다 `engine`과 그 엔진의 항목을 둔다. 자격 증명은 모두 mount된 파일 경로로만 받는다. 실행 설정 파일 자체에는 비밀을 넣지 않는다. 항목의 정확한 형식은 각 모듈 설명에 있다.

| engine | 모듈 | 비밀 파일 | 비고 |
|---|---|---|---|
| `postgresql` | `sova.service.catalog` | DB DSN, 역할 비밀번호 | `mode: offline_copy` 또는 `logical_replication` |
| `mysql` | 같음 | 연결 설정(JSON), 계정 비밀번호, 복제 계정 비밀번호 | `mode: replication`은 서버 복제 변경 캡처. TLS는 `ssl_ca`(호스트 검증 기본 켬) |
| `redis` | `sova.service.redis_execution` | 서버 설정(JSON), 사용자 비밀번호 | TLS는 `ssl_ca` |
| `kafka` | `sova.service.kafka_execution` | 관리자·principal별 클라이언트 설정(JSON) | 연결 관련 키만 받는다. probe용 트랜잭션 ID 접두어(`probe_prefix`)를 각 producer에 허용해야 한다. TLS는 `security.protocol: SASL_SSL`과 `ssl.ca.location` |
| `rabbitmq` | `sova.service.rabbitmq_execution` | 브로커 설정(JSON), 사용자 비밀번호 | TLS는 `ssl_ca`. 관리자 자격 증명이 오가는 두 경로(AMQP, 관리 API)에 모두 쓰이므로, 관리 API 주소는 `https://`여야 한다 |
| `mongodb` | `sova.service.mongodb_execution` | 서버 설정(JSON), 사용자 비밀번호 | `mode: change_stream`은 change stream 변경 캡처(원본이 레플리카셋이어야 함, `catch_up_seconds`). TLS는 `tls_ca` |
| `kubernetes-volume` | `sova.service.volume_execution` | agent 키(hex), 다른 클러스터의 token | 앱 namespace 권한은 위 절 참고 |
| `kubernetes-session` | `sova.service.composite_execution` | 다른 클러스터의 token | 경로는 Service selector 또는 Gateway API HTTPRoute |
| `composite` | 같음 | 구성 요소의 것 | 볼륨·세션 구성 요소를 한 단위로 묶는다 |

경로 저장소(`route`)는 `kubernetes_service` 또는 `gateway_httproute`다(`sova.service.route_stores`). Gateway 경로의 표본 요청은 앱이 응답 헤더로 내는 쪽 표식을 HTTPS로 읽는다.

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
