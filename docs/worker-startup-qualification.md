# 워커 시작 설정과 실제 lease 계약 정합성

2026-11-18 · 개인 `feature/migration-request-qualification`. 배포 전 설정 검증의 수정·시험 기록이다.

## 수정한 문제

기존 `WorkerConfig`는 10~900초 lease를 받아들였지만 PostgreSQL 실행 원장은 1~300초만 허용했다. 실제로 **설정 600초·stall 700초는 생성되지만 원장의 duration 검사는 거부**하는 것을 재현했다. DB에 접속하지 않은 재현 결과도 보존했다.

서비스 설정에서 `PostgresExecutionOwnership.duration` 검사를 재사용한다. 서비스의 최소 10초 조건은 유지하므로 현재 배포 설정의 유효 범위는 **10~300초**다. 원장의 범위를 늘리거나 승인·실행 권한을 완화하지 않았다. 다른 business engine을 쓰더라도 제어 원장은 PostgreSQL이므로 같은 검사가 적용된다.

| 항목 | 현재 검사 | 기본값 |
|---|---|---|
| `SOVA_LEASE_SECONDS` | ASCII 숫자로 된 초 단위 문자열, 원장 duration 범위, 서비스 최소 10초 | 120 |
| `SOVA_WORKER_STALL_SECONDS` | ASCII 숫자로 된 초 단위 문자열, lease보다 크고 최대 86400초 | 600 |
| `--poll-seconds` | 유한한 양수이며 stall보다 작아야 함. NaN·무한대·bool 거부 | 5 |
| `--health-port` | 정수 1~65535. bool·0·범위 밖 값 거부 | 8081 |

초 단위 환경변수는 공백·부호·밑줄 등을 포함한 표기를 받지 않는다. 정상 기본값·최소 lease·기존 kind overlay의 15초 값은 범위 안이다.

폴링 간격이 0 이하이면 대기 없이 반복할 수 있고, stall 이상이면 정상 대기를 liveness가 정지로 볼 수 있다. 이를 시작 시 거부한다. 잘못된 값은 catalog 생성·DB 연결·Pod 관측·health listener 생성 전에 중단한다. 입력값이나 비밀 내용을 오류 메시지에 넣지 않는다.

## 검증

| 실행 | 범위 | 결과 |
|---|---|---|
| 전용 TLS PostgreSQL suite | 기존 83개, 신규 시작 검사 26개, 기존 설정 회귀 4개, 실제 lease 시험 3개 | **116 PASS, 실패·오류·건너뜀 0** |
| 의존성이 갖춰진 기존 환경의 단위 suite | 전체 service deployment/catalog 59개 + 신규 시작 검사 26개 | **85 PASS, 실패·오류·건너뜀 0** |

두 실행에는 겹치는 시험이 있으므로 숫자를 합산하지 않는다. 두 번째 환경은 설치·갱신 없이 읽기 전용으로 사용했고 bytecode·pytest cache 쓰기를 막았다.

전용 실행 `run-01`의 측정 소스 43개가 커밋 `638ec32`와 일치한다. 시각 감시 262개 샘플을 통과했고 생성한 DB·네트워크를 정리했다. 전체 시험 시간은 서비스 SLO 측정값이 아니다.

신규 단위 시험은 허용 경계·거부 값과 자원 생성 전 거부를 확인한다. 후자는 catalog·연결 생성·Pod 관측·health listener를 sentinel로 대체해 호출되지 않았음을 검사한다. 실제 배포 권한·네트워크 거부 시험으로 해석하지 않는다.

실제 DB 시험은 별도 승인·접수된 시험 계획에서 **10·120·300초**를 각각 발급했다. DB의 발급 전후 시각에 지정 시간을 더한 구간에 `valid_until`이 있는지 확인한 뒤 정상 해제했다. 만료까지 기다린 시험은 아니며, owner·valid_until이 비워지는 것까지 확인했다. 업무 효과나 컷오버는 실행하지 않았다.

## 재현

```sh
caffeinate -di python tests/qualification_postgres.py \
  --suite startup \
  --image sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac \
  --security-source /path/to/pinned-security/src \
  --artifacts /path/to/new-private-run-directory
```

별도 단위 실행은 이 저장소의 개발 의존성·선택 engine 의존성을 준비한 환경에서 한다.

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider \
  tests/unit/test_worker_startup.py tests/unit/test_service_deployment.py
```

[공개 실행 요약](evidence/worker-startup-qualification-20261118.json), [개발 기록](development/worker-startup-qualification-2026-11-18.md).

## 범위와 남은 일

기존 [워커 프로세스 시험](migration-worker-qualification.md)의 미검증 목록 중 설정 범위 불일치를 수정한 것이다. 후속 [이미지 소스·실제 CLI 시작 검사](service-image-qualification.md)에서 고정 이미지와 거부 조건을 확인했다. 정상 배포 worker의 catalog·Pod 신원·정지 증명과 AWS→VM 인수는 남아 있다. 이 설정 시험으로 운영 자격·무중단·부하/SLO 또는 금융 보안 인증을 발급하지 않는다.
