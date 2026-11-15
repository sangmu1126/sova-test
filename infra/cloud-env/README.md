# 클라우드 시험 환경 스위치 (AWS·GCP)

실제 클라우드 실측(`aws_live`, `gcp_live`)에 쓰는 자원을 AWS와 GCP에서 한 번에 켜고 끈다. 한 번 켠 것을 "실행(run)"이라 하고, 실행은 끝나는 시각이 정해진 임대(lease)다. 정한 시간이 지나면 저절로 꺼진다.

```sh
infra/cloud-env/cloud-env.sh configure --aws-profile <프로필> --aws-region <리전> \
    --gcp-project <프로젝트> --gcp-region <리전>     # 한 번만. 로컬 상태 디렉터리에 저장
infra/cloud-env/cloud-env.sh up                    # 둘 다 8시간(--hours, 최대 24)
infra/cloud-env/cloud-env.sh up --clouds gcp       # 한쪽만
infra/cloud-env/cloud-env.sh status                # 남은 시간, 지금 클라우드에 있는 이 실행의 자원
eval "$(infra/cloud-env/cloud-env.sh env)"         # 실측 시험이 읽는 환경 변수
infra/cloud-env/cloud-env.sh extend --hours 2      # 끝나는 시각을 미룸(클라우드 쪽 예약도 함께)
infra/cloud-env/cloud-env.sh down                  # 이 실행의 자원을 모두 지우고 남은 것이 없는지 확인
```

## `up`이 만드는 것

| 클라우드 | 자원 | 설정 |
|---|---|---|
| AWS | RDS PostgreSQL 17 `sova-test-pg-<run>` | db.t4g.micro, 이 기기 IP /32만 허용하는 보안 그룹, 백업 보존 0일, 삭제 보호 없음 |
| GCP | Cloud SQL PostgreSQL 17 `sova-test-pg-<run>` | db-f1-micro, 이 기기 IP /32만 허용, 암호화 연결만(`ENCRYPTED_ONLY`), 공유 CA(인스턴스 DNS 이름으로 인증서 검증), `cloudsql.logical_decoding=on`, 최종 백업 없음 |
| 둘 다 | 클라우드 쪽 만료 삭제 | 임대 종료 30분 뒤 DB 인스턴스를 지우는 예약. AWS는 EventBridge Scheduler(그 인스턴스 하나만 지울 수 있는 role), GCP는 Cloud Scheduler(그 인스턴스 하나로 조건을 건 서비스 계정) |

나머지 자원(버킷, 비밀, user pool, compute, DNS 존 등)은 각 시험이 만들고 지운다. 시험이 중간에 멈춰 남긴 것도 `down`이 찾아 지운다.

## 안전 규칙

- **이 실행의 것만 지운다.** 이름이 `sova-test`로 시작하고 실행 번호를 독립된 토큰으로 담은 자원만 대상이다(`sova-test-src-<run>-…`, `sova-test/<run>/…`). 다른 실행 번호, 다른 이름의 기존 자원은 조회만 하고 건드리지 않는다. 이 규칙은 단위 시험(`tests/unit/test_cloud_env.py`)이 고정한다.
- **임대가 자원보다 먼저 생긴다.** `up`은 아무것도 만들기 전에 실행 번호를 기록한다. 중간에 실패하거나 끊겨도 `down`이 찾아낸다. 실패한 쪽은 `up`을 다시 실행하면 같은 실행 번호로 이어서 만든다.
- **목록 조회가 실패하면 "비었다"로 보지 않는다.** `down`은 지운 뒤 다시 조회해 두 클라우드 모두 비었을 때만 임대를 닫는다. 아직 못 지운 것(보존 기간이 남은 객체 등)이 있으면 임대를 열어 두고 다음 `down`이나 만료 작업이 다시 시도한다.
- **비밀은 출력하지 않는다.** DB 비밀번호는 여기서 만들어 상태 디렉터리의 0600 파일에만 둔다. 명령줄 인자로 넘기지 않는다(AWS는 요청 파일, GCP는 API 본문). `env`는 DSN에 passfile을, 비밀번호에는 `$(cat 파일)`을 쓴다.

## 자동 종료는 두 겹이다

1. **이 기기의 launchd 작업**(`com.sova-test.cloud-env-expiry`): 임대가 있는 동안 10분마다 `expire`를 실행한다. 끝나는 시각이 지났으면 `down`을 실행한다. 잠자기 등으로 놓친 실행은 깨어났을 때 돈다. 임대가 닫히면 스스로 내려간다.
2. **클라우드 쪽 예약 삭제**: 이 기기가 꺼져 있어도 임대 종료 30분 뒤 두 DB 인스턴스를 지운다. 시간당 요금이 의미 있는 자원은 DB 인스턴스뿐이라 이것만 맡긴다. 그 밖의 남은 자원은 다음 `down`이 정리한다.

## 상태 디렉터리

`~/.sova-test-env/cloud`(또는 `SOVA_TEST_ENV_STATE/cloud`): 설정, 임대, 비밀번호(0600), 서버 CA, 끝난 실행의 기록(`history/`), 만료 작업 로그. 저장소에는 계정·프로젝트 식별자를 넣지 않는다.

표준 라이브러리와 `aws`·`gcloud` 명령만 쓴다.
