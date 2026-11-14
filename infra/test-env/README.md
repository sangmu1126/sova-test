# 로컬 통합 시험 환경

통합 시험에 필요한 서비스를 kind 클러스터 하나(`sova-test-env`)에 모두 올린다.

| 서비스 | 호스트 주소 | 용도 |
|---|---|---|
| PostgreSQL 17.11 | `127.0.0.1:55432` | control plane 저장소, 원본·목적지 DB. `cluster_name=sova-persistence-test`, prepared transaction 허용 |
| S3 호환 저장소(versitygw) 원본 | `127.0.0.1:57070` | 객체 저장소 원본. 계정 `appuser`·`batchuser`·`platformuser` |
| S3 호환 저장소(versitygw) 목적지 | `127.0.0.1:57071` | 객체 저장소 목적지. 같은 계정, 별도 저장소·IAM |
| Keycloak 26.8.1 원본 | `127.0.0.1:58080` | 원본 IdP |
| Keycloak 26.8.1 목적지 | `127.0.0.1:58081` | 목적지 IdP |
| Kubernetes API | kind 기본 | 경로 전환·관리권 멈춤·Secret·플랫폼 탈출 시험의 namespace |

모든 주소는 `127.0.0.1`에만 열린다. 계정과 비밀번호는 이 일회용 저장소 전용의 고정 시험값이다. 상태는 모두 휘발성이라 Pod를 다시 시작하면 비워진다.

## 사용

```sh
infra/test-env/test-env.sh up          # 클러스터 생성, 모든 서비스 준비까지 대기(약 2~3분)
eval "$(infra/test-env/test-env.sh env)" # 시험이 읽는 환경 변수
infra/test-env/test-env.sh reset-db    # 전체 회귀 전에 빈 DB로(시험은 새 DB를 전제)
python -m pytest tests                 # PYTHONPATH에 src와 고정 security 패키지 포함
infra/test-env/test-env.sh down        # 클러스터와 그 안의 모든 것 삭제
```

kubeconfig는 `~/.kube/sova-test-env.yaml`(또는 `SOVA_TEST_KUBECONFIG`)에만 쓴다. `~/.kube/config`와 현재 kubectl context는 바꾸지 않는다. 시험의 `kubectl` 호출은 `SOVA_TEST_KUBECONFIG`로 이 파일을 쓴다.

## 주의

- 같은 Docker VM 위에서 돌므로 호스트와 DB 시계가 수 밀리초 어긋날 수 있다. 시험은 DB에 쓰는 시각에 1초 여유를 둔다.
- Keycloak은 JVM 힙을 512MB로 제한한다. 시험 규모에는 충분하다.
- 플랫폼 탈출 시험의 고객 백업은 `SOVA_TEST_POSTGRES_EXEC`(이 환경에서는 `kubectl exec deploy/postgres`)로 DB의 `pg_dump`·`pg_restore`를 쓴다.
- 실제 AWS 실측(`aws_live`)은 이 환경과 별개로, 명시적으로 켤 때만 돈다.
