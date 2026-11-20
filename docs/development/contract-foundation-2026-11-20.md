# 목적지 접근 통제의 실측 (2026-11-20)

[platform 대조표](platform-gap-map-2026-11-19.md)의 흡수 1순위를 진행했다. 팀 T14 시험기가 목적지에서 직접 재던 통제 가운데 이전 단위에 묶이는 것을 단위 probe와 계약 의무로 만들었다. 결함은 [결함 목록](defect-register.md)에, 기능 설명은 [구현 안내](../contract-foundation.md#목적지-접근-통제의-실측-2026-11-20)에 있다.

## 1. 무엇을 흡수했나

팀 시험기(`services/control-runner`, main `d569817e`)를 읽어 판정 방법을 가져왔다. 코드는 가져오지 않았다. 팀 것은 AWS 역할·고정 객체 하나를 전제로 한 수집기이고, 이 저장소는 저장소마다 probe를 두고 계약 의무에 연결하는 구조라서다.

**가져온 판정 방법.**

- 양성 대조로 감싼 측정이다. 앱이 자기 객체를 읽음 → 무인증·다른 주체의 시도 → 앱이 다시 읽음. 둘 다 성공해야 그 사이의 거부를 거부로 인정한다. 없는 객체도 403을 받을 수 있어서다.
- 거부는 HTTP 403 `AccessDenied`일 때만 센다. 시간 초과·도구 오류·예상 밖 응답은 판정 불가다.
- 객체 내용은 읽지도 남기지도 않는다. 관측에는 주체의 역할 이름과 digest만 남긴다.
- ENC-01.2·01.3은 정상 연결 → 틀린 CA·틀린 hostname·평문 → 정상 연결의 순서로 잰다.

**이 저장소의 원칙과 합친 것.**

- 쓰기 거부는 기존 ADR-0007대로 시도와 접근 모델의 판단이 둘 다 동의해야 한다.
- 접두사마다 접근이 다를 수 있다. 그래서 보호 집합마다 증인 객체를 둔다(팀은 등록 객체 하나).
- 활성화 전에는 앱이 목적지에서 일부러 막혀 있다. 그래서 양성 대조는 probe의 판독 주체가 한다.
- 데이터가 오기 전(offline-copy)에는 집합 접두사 안에 canary를 두었다가 지운다.

## 2. 만든 것

| 통제(계약 이름) | 팀 통제 | 측정 | 대상 |
|---|---|---|---|
| `deny_anonymous_data_access` | IAM-01.1 | `ObjectStoreProbes.anonymous_denied` | 객체 저장소 |
| `deny_cross_tenant_data_access` | ISO-01.1 | 객체 저장소는 양방향·집합별 읽기·목록·조건부 쓰기 | 기존 단위 + 객체 저장소 |
| `verified_transport_encryption` | ENC-01.1 | 객체 저장소는 끝점 핸드셰이크(등록 CA·이름) | 기존 단위 + 객체 저장소 |
| `refuse_unverified_transport` | ENC-01.2·01.3 | `tls_refusal.transport_refusals` | PostgreSQL·MySQL·객체·Redis·RabbitMQ·Kafka·MongoDB |
| `deny_cross_workload_secret_access` | SEC-01.1 | `KubernetesSecretProbes` | 실행 파일에 `secret_access`가 있는 모든 단위 |
| `deny_cross_namespace_network_access` | NET-01.3 | `NetworkReachCheck` | 실행 파일에 `network_reach`가 있는 모든 단위 |

- 평문 시도는 프로토콜의 첫 메시지만 보낸다. HTTP 요청, PostgreSQL 시작 메시지(비밀번호를 묻기 전에 멈춤), MySQL의 비밀번호 없는 로그인, Redis `PING`, AMQP 헤더, Kafka ApiVersions, MongoDB hello다. 자격은 보내지 않는다.
- 평문 시도는 등록 끝점의 포트와 `plaintext_ports`(서비스가 평문으로도 열어 둘 수 있는 포트)에 한다.
- Secret 접근은 다른 서비스 계정의 토큰으로 `get`·`list`·Pod 생성 dry run을 해 보고, API 서버의 판단(SubjectAccessReview, 그 계정의 그룹 포함)과 맞춘다.
  - Pod를 만들 수 있으면 같은 namespace의 어떤 Secret이든 마운트할 수 있다.
  - 그래서 Pod·Deployment·StatefulSet·DaemonSet·ReplicaSet·Job·CronJob의 `create`·`update`·`patch`도 본다.
- 네트워크 도달은 짧게 뜨는 클라이언트 Pod로 잰다. 앱 namespace에서 앱 클라이언트 레이블로 연결되는 것을 앞뒤로 확인하고, 등록된 바깥 namespace에서는 연결되지 않아야 한다.
- 워커 연결은 다음과 같다.
  - `data_measure`가 새 통제를 그 단위의 probe에 연결한다. probe에 없는 통제는 FAIL이다.
  - 실행 파일의 `secret_access`·`network_reach`는 어떤 엔진의 항목에도 붙는다.
  - PostgreSQL은 `tls_endpoints`로, Redis·RabbitMQ·Kafka·MongoDB는 TLS로 설정된 쪽의 host·port·CA가 그대로 등록 끝점이 된다.

## 3. 실측

**객체 저장소.** SeaweedFS 4.52(팀 목적지와 같은 고정 이미지)를 kind에 두 대 띄웠다.

| 저장소 | 무인증 | 다른 tenant | 전송 |
|---|---|---|---|
| 기본값으로 시작한 것 | 읽기·목록·쓰기 모두 허용 → FAIL | 양방향 허용 → FAIL | http → FAIL |
| 계정 파일 + TLS로 잠근 것 | 집합마다 모두 거부 → PASS | 양방향 거부 → PASS | 틀린 CA·hostname 거부, 평문에 Go의 "HTTPS 서버에 HTTP 요청" 400 → PASS |

- 이전 계획 전체: 출발지 versitygw → 목적지 SeaweedFS.
  - 잠근 목적지: 통제 다섯 개가 모두 통과하고 이전이 완료됐다.
  - 기본값 목적지: 쓰기 정지 전 단계에서 무인증·다른 tenant·전송 두 통제가 FAIL이다. SAFE_HOLD가 되고, 앱은 출발지에 계속 쓴다.
- **SeaweedFS의 정책 집행.** 버킷 정책이 주체를 맨 이름(`app`)으로 적으면, 저장소는 받아서 저장하고 읽을 때 그대로 돌려준다. 그런데 집행은 하지 않는다. `arn:aws:iam::…:user/app` 형식일 때만 집행한다.
  - 정책만 읽는 확인(adapter의 readback)은 "막혔다"고 판정한다.
  - probe는 시도가 허용됨을 보고 두 관점 불일치로 FAIL을 낸다. 이것을 실제 저장소에서 확인했다.

**전송 거부.**

| 끝점 | 결과 |
|---|---|
| 시험 환경 TLS PostgreSQL(`ssl = on`, 이미지 기본 접근 규칙 `host all all all trust`) | 신원 유지, 평문 시작 메시지가 인증 요청을 받음 → FAIL |
| `hostssl`만 허용하는 PostgreSQL(모듈 전용) | 평문은 "no encryption"으로 거부 → PASS |
| RabbitMQ(평문 리스너 없음) | PASS |
| Redpanda(TLS 리스너만) | PASS |
| Valkey TLS 포트 | 평문 거부 → PASS. 같은 서버의 6379를 `plaintext_ports`로 등록하면 그 포트가 평문을 받음 → FAIL |
| MongoDB `preferTLS` | TLS를 제공하지만 같은 포트에서 평문 hello에 답함 → FAIL |
| MySQL(TLS 제공, 요구하지 않음) | 비밀번호 없는 평문 로그인에 인증을 진행하고 서버 관점도 요구하지 않음 → FAIL. `require_secure_transport`를 켜면 PASS. 다만 MySQL은 그 요구를 인증이 성공한 뒤에 확인해서, 켠 상태에서도 시도는 인증 진행까지만 보인다(#44). 그래서 이 경우는 서버 관점이 판정한다 |

모든 끝점이 틀린 CA와 틀린 hostname을 거부했다.

**Secret 접근(kind).**

| 다른 서비스 계정 | 결과 |
|---|---|
| 권한 없음, 다른 namespace의 계정 | 모두 거부 → PASS |
| Secret `get` 권한 | FAIL |
| Deployment `create` 권한만(모든 시도는 거부) | API 서버 판단으로 FAIL |
| 다른 namespace의 계정 그룹에 준 읽기 | FAIL |

볼륨 단위의 이전 계획 전체로도 확인했다. 권한이 없으면 완료되고 통제가 PASS로 서명된다. 다른 워크로드에 읽기가 있으면 쓰기 정지 전에 SAFE_HOLD가 된다. 다른 계정의 토큰은 워커가 TokenRequest로 10분짜리로 받는다.

**네트워크 도달(kind, kindnetd).**

| 정책 | 결과 |
|---|---|
| 없음 | 어느 namespace든 도달 → FAIL |
| 앱 클라이언트만 허용 | 집행됨 → PASS |
| 모든 namespace 허용 | FAIL |

## 4. 시험 환경에서 생긴 일

- **venv 손상.** 자정의 임시 파일 정리가 시험용 venv의 오래된 파일(`pyvenv.cfg`, 59개 패키지의 RECORD 등)을 지웠다(T26). 같은 구성으로 다시 만들었다. security 사본(115개)은 온전했다.
- **kindnet의 정책 캐시.** 처음 측정에서는 "앱 클라이언트만 허용" 정책을 넣어도 바깥 namespace가 도달했다(T25).
  - kindnetd가 11-19 밤(Mac이 잠든 무렵)부터 컨트롤 플레인 이름 조회 시간 초과로 NetworkPolicy 재조회에 계속 실패했다. 그래서 오래된 캐시를 쓰고 있었다.
  - 정책을 써도 집행되지 않는 상태가 조용히 이어진 것이다. 이 통제가 재려는 바로 그 상황이고, probe는 FAIL로 보고했다.
  - kindnet Pod를 다시 띄워 동기화한 뒤 통과했다.
- **port-forward 종료.** Valkey의 TLS 포트는 평문에 RST로 답한다. `kubectl port-forward`(1.36)는 그 RST를 받으면 "lost connection to pod"로 끝난다(T24).
  - 제품 probe는 직접 연결하므로 상관없다.
  - 시험에서는 연결마다 port-forward를 살려 두는 중계기를 둔다.
- **같은 이름의 시험 모듈.** 단위·통합 시험 파일 이름이 겹쳐 두 번 수집 오류가 났다(T23, T2와 같은 원인).
  - 한 번은 결함 주입 결과가 전부 수집 오류였는데 "잡음"으로 보일 뻔했다. 실패 수를 보고 다시 돌렸다.
  - 이름 중복을 막는 배치 시험을 더했다.

## 5. 18차 전체 회귀와 재실행

71bc543(네트워크 도달까지)에서 시작한 18차 회귀는 쓸 수 없는 결과다(T30).

- 04:23~08:50, 4시간 24분이 걸렸다. 평문 2073 통과 / 14 실패 / 19 오류, TLS 단계 4 통과 / 2 실패 / 51 오류.
- 도중에 다른 작업 공간의 사용자 여정 검증이 같은 Docker VM에서 돌았다. kind 노드의 load average가 94, 남은 메모리가 116MB였고 스왑을 썼다. 오류 대부분은 공용 PostgreSQL이 연결을 끊은 것이다.
- 시작 전 확인이 `python -m pytest`만 찾아 그 시험(`python tests/…`)을 놓쳤다. 낯선 컨테이너도 보았지만 막지 않았다.

그 작업이 끝난 뒤(08:58) 환경을 되살렸다.

- 남은 namespace를 지우고 공용 Keycloak 두 대를 재시작했다.
- 공용 RabbitMQ가 25시간째 준비되지 않은 원인(T29)을 찾아 고쳤다.

실패·오류가 난 파일 12개(새 MySQL 시험 포함)를 다른 작업 없이 다시 돌렸다.

- 1차: 54 통과 / 5 실패. TLS 단계는 55 통과 / 2 건너뜀.
- 남은 5건과 처리:
  - Kafka 2건: 복원 훈련이 scratch 토픽을 남기는 제품 결함(#43). 쌓인 59개가 파티션 한도를 채웠다. 고치고 토픽을 지운 뒤 통과했다. 결함 주입도 잡았다.
  - MySQL 1건: 평문 판정의 설계 오류(#44). 서버 관점을 더한 뒤 통과했다.
  - NetworkPolicy 1건: 과부하 중 kindnet 감시가 다시 끊겼다(T25 재발). kindnet을 재시작한 뒤 통과했다.
  - 보안 대응 1건: 승인 만료 시각 경합으로 보인다. 단독 재실행에서 통과했다.

전체 회귀를 다시 돌릴 시간은 남지 않았다. 다음 작업의 첫 일로 19차 전체 회귀를 돌린다.

## 6. 남은 것

- 19차 전체 회귀(위). 시작 전에 낯선 컨테이너, 다른 작업 공간의 시험 프로세스, 노드 부하를 함께 본다.
- kind의 API 서버는 무거운 시험 중 과부하로 CNI의 감시를 끊을 수 있다. NetworkPolicy 시험이 실패하면 kindnet 로그를 먼저 본다.

- 객체 저장소 단위는 아직 워커 실행 파일 엔진이 아니다. 경로 저장소(앱이 읽는 버킷 위치)를 먼저 설계해야 한다.
- GCS의 무인증 거부는 서명 없는 GCS 클라이언트가 필요하다. 클라우드를 켜지 않아 재지 않았다.
- Secret 접근은 Kubernetes Secret만 잰다. Secrets Manager·Secret Manager의 다른 주체 거부(팀 SEC-01.1의 AWS 방식)는 클라우드 실측이 필요하다.
- 네트워크 도달은 클러스터 안의 namespace 사이만 잰다. 외부 노출(NET-01.1·01.2)은 환경 수준 통제로 대조표 2순위에 남는다.
