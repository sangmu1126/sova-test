# 로컬 통합 시험 환경

통합 시험에 필요한 서비스를 kind 클러스터 하나(`sova-test-env`)에 모두 올린다.

| 서비스 | 호스트 주소 | 용도 |
|---|---|---|
| PostgreSQL 17.11 | `127.0.0.1:55432` | control plane 저장소, 원본·목적지 DB. `cluster_name=sova-persistence-test`, prepared transaction 허용, `wal_level=logical`(논리 복제 adapter 시험) |
| PostgreSQL 17.11 (검증된 TLS) | `localhost:55433` | 읽기·쓰기 게이트 시험용. `ssl=on`, 시험용 CA가 서명한 `localhost` 인증서. 연결은 `sslmode=verify-full` |
| S3 호환 저장소(versitygw) 원본 | `127.0.0.1:57070` | 객체 저장소 원본. 계정 `appuser`·`batchuser`·`platformuser` |
| S3 호환 저장소(versitygw) 목적지 | `127.0.0.1:57071` | 객체 저장소 목적지. 같은 계정, 별도 저장소·IAM |
| Keycloak 26.8.1 원본 | `127.0.0.1:58080` | 원본 IdP |
| Keycloak 26.8.1 목적지 | `127.0.0.1:58081` | 목적지 IdP |
| MySQL 8.4.13 | `127.0.0.1:53306` | MySQL 인계 adapter의 원본·목적지 DB. 행 기반 binlog, GTID |
| RabbitMQ 4.1.8 | `127.0.0.1:55672`(AMQP), `127.0.0.1:55673`(관리 API) | 큐 인계 adapter |
| Redpanda 26.2.5 (Kafka API) | `127.0.0.1:59092`(Kafka), `127.0.0.1:59644`(관리 API) | 스트림 인계 adapter. SASL/SCRAM과 ACL 강제 |
| Kubernetes API | kind 기본 | 경로 전환·관리권 멈춤·Secret·플랫폼 탈출·볼륨·세션 시험의 namespace. 볼륨 시험은 기본 StorageClass(local-path)를 쓴다 |
| Valkey 9.1.3 두 대 | 시험 모듈마다 namespace에 띄우고 `kubectl port-forward`로 접속 | Redis 계열 키 공간 adapter. default 사용자는 끄고 관리자 사용자만 켠다. 고정 포트가 없어 클러스터를 다시 만들 필요가 없다 |
| MySQL 8.4.13 두 대, MongoDB 8.0.34 두 대 | 시험 모듈마다 namespace에 띄우고 `kubectl port-forward`로 접속 | MySQL 변경 캡처(서버 사이 복제)와 MongoDB adapter. 자체 CA의 TLS를 제공한다(강제하지 않음) |
| Envoy Gateway v1.9.3 (선택) | 클러스터 안 | Gateway API 진입점 시험. `test-env.sh gateway`로 추가한다. 설치 파일 SHA-256과 컨트롤러 이미지 digest를 고정한다. 없으면 그 시험만 건너뛴다 |

모든 주소는 `127.0.0.1`에만 열린다. 계정과 비밀번호는 이 일회용 저장소 전용의 고정 시험값이다. 상태는 모두 휘발성이라 Pod를 다시 시작하면 비워진다.

## 사용

```sh
infra/test-env/test-env.sh up          # 클러스터 생성, 모든 서비스 준비까지 대기(약 2~3분)
eval "$(infra/test-env/test-env.sh env)" # 시험이 읽는 환경 변수
infra/test-env/test-env.sh reset-db    # 전체 회귀 전에 빈 DB로(시험은 새 DB를 전제)
python -m pytest tests                 # PYTHONPATH에 src와 고정 security 패키지 포함
infra/test-env/run-suite.sh            # 위 두 줄 + TLS 게이트 시험까지 한 번에(아래)
infra/test-env/test-env.sh down        # 클러스터와 그 안의 모든 것 삭제
```

kubeconfig는 `~/.kube/sova-test-env.yaml`(또는 `SOVA_TEST_KUBECONFIG`)에만 쓴다. `~/.kube/config`와 현재 kubectl context는 바꾸지 않는다. 시험의 `kubectl` 호출은 `SOVA_TEST_KUBECONFIG`로 이 파일을 쓴다.

## 전체 회귀

`run-suite.sh`는 두 PostgreSQL을 비운 뒤 두 번 돈다.

1. 평문 서버(`SOVA_TEST_POSTGRES_DSN`)로 전체 시험. 읽기·쓰기 게이트 시험(`postgres_tls` 표식)은 검증된 TLS나 private Unix socket만 받으므로 여기서는 건너뛴다.
2. TLS 서버(`SOVA_TEST_POSTGRES_TLS_DSN`)로 `postgres_tls` 표식 시험만.

평문 서버는 "전송 암호화가 없으면 활성화 전에 막힌다"는 시험에 필요하다. 그래서 TLS를 평문 서버에 켜지 않고 서버를 둘로 나눴다.

시험용 CA와 서버 키는 `up` 때 `~/.sova-test-env/tls`(또는 `SOVA_TEST_ENV_STATE`)에 만든다. 이 기기 밖으로 나가지 않는다. 서버 Pod는 CA를 호스트와 같은 절대 경로에 mount한다. 그래서 서버 안에서 만든 subscription도 시험의 연결 문자열 그대로 TLS 검증을 한다.

자식 프로세스를 `python -I`(PYTHONPATH 무시)로 띄우는 시험이 있다. 이 시험들 때문에 이 저장소용 venv에 고정 security 패키지(extra 포함)와 이 저장소(editable)를 설치해 둔다. `SOVA_TEST_PYTHON`으로 그 python을 넘긴다.

## 주의

- 같은 Docker VM 위에서 돌므로 호스트와 DB 시계가 수 밀리초 어긋날 수 있다. 시험은 DB에 쓰는 시각에 1초 여유를 둔다.
- Keycloak은 JVM 힙을 512MB로 제한한다. 시험 규모에는 충분하다.
- 논리 복제 시험의 subscription은 서버가 자기 자신의 로컬 socket으로 원본 DB에 접속한다(`SOVA_TEST_POSTGRES_REPLICATION_CONNINFO`, 기본 `user=postgres`). slot이 남아도 `max_slot_wal_keep_size=1GB`가 WAL 보존을 제한한다.
- 성능 측정(`tests/perf`)의 10만 행 크기는 MacBook Air M1에서 Docker Desktop VM을 두 번 비정상 종료시켰다. 이 기기에서는 기본 크기(1천·1만 행)만 쓴다.
- 플랫폼 탈출 시험의 고객 백업은 `SOVA_TEST_POSTGRES_EXEC`(이 환경에서는 `kubectl exec deploy/postgres`)로 DB의 `pg_dump`·`pg_restore`를 쓴다.
- 실제 클라우드 실측(`aws_live`, `gcp_live`)은 이 환경과 별개로, 명시적으로 켤 때만 돈다. 클라우드 쪽 자원은 [cloud-env](../cloud-env/README.md) 스위치로 AWS·GCP를 함께 켜고 끄며, 정한 시간이 지나면 저절로 꺼진다.

## 목적지 접근 통제 시험의 서버 (2026-11-20)

목적지 접근 통제(무인증·다른 tenant·전송 거부·Secret·네트워크) 시험은 Redis·MongoDB 시험처럼, 시험 모듈이 kind에 자기 namespace를 만들어 서버를 띄우고 `kubectl port-forward`로 붙는다. 끝나면 namespace를 지운다. 클러스터 구성(`kind-config.yaml`)은 바꾸지 않았다.

- **SeaweedFS 두 대**(`tests/seaweedfs_fixtures.py`, 팀 목적지와 같은 4.52 digest 고정).
  - 기본값 그대로 시작한 것: 계정 파일이 없어 누구나 읽고 씀.
  - 계정 파일과 TLS로 잠근 것: 관리자, 앱·배치는 `orders-*`, 다른 tenant는 `other-*`.
  - 텔레메트리는 끈다.
  - 버킷마다 볼륨을 하나씩만 늘리게 한다(`WEED_MASTER_VOLUME_GROWTH_COPY_1=1`). 기본값은 7개씩이라 시험 버킷이 늘면 상한에 걸린다.
- **`hostssl`만 허용하는 PostgreSQL**(`test_postgres_transport_refusals.py`): 평문 거부가 통과하는 쪽이다.
  - 공용 TLS 서버(55433)는 접근 규칙이 `trust`라 평문을 받는다. 이 시험은 그것을 실패로 보고하는 것을 확인한다. 공용 서버는 다른 시험이 쓰므로 그대로 둔다.
  - 준비 확인은 TCP로 한다. 이미지의 초기화용 서버는 로컬 소켓으로만 뜬다.
- **port-forward 중계기**(`tests/forward_relay.py`).
  - Pod가 연결을 RST로 끊으면 `kubectl port-forward`(1.36)는 "lost connection to pod"로 끝난다. Valkey의 TLS 포트에 평문을 보내면 그렇다.
  - 그런 포트는 연결마다 port-forward를 다시 살리는 중계기로 잰다.
- **NetworkPolicy**: kind의 kindnetd가 집행한다.
  - 정책 감시가 끊기면(예: Mac이 잠든 뒤 컨트롤 플레인 이름 조회 실패) 오래된 캐시로 새 정책을 집행하지 않는다.
  - 네트워크 도달 시험이 이유 없이 실패하면 `kubectl -n kube-system logs ds/kindnet`에서 `Failed to watch *v1.NetworkPolicy`를 확인하고, kindnet Pod를 다시 띄운다.
- **시험용 venv.** 세션 작업 폴더에 두면 자정의 임시 파일 정리가 오래된 파일을 지운다(security 사본과 같다). 회귀 전에 `pyvenv.cfg`가 있는지 본다.
