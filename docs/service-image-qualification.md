# 서비스 이미지 소스 대조와 실제 워커 CLI 시작 검사

2026-11-18 · 개인 `feature/migration-request-qualification`.

## 확인한 문제와 결과

로컬 `sova-service:dev`는 `sova-control-plane 0.1.0`·`sova-security 0.11.1`이라는 버전은 같았지만, 현재 고정한 Sova 코드와 달랐다. **필수 Python·SQL 파일 33개가 없고 23개가 달랐다.** `service/config.py`, `main.py`, `worker.py`도 달라 소스 체크아웃의 시험 결과를 이 이미지에 적용할 수 없었다. Security 패키지 파일은 일치했다.

| 대상 | 설치 소스 대조 | 실제 `sova-service run-worker` 시작 거부 시험 |
|---|---|---|
| 기존 캐시 이미지 `207bd22c20a0…` | Sova 불일치, Security 일치 | 4개 중 1개 통과, 3개 실패 |
| 고정 소스로 새로 빌드한 `117a14eadae2…` | Sova 317개·Security 122개 파일 모두 일치 | **4개 모두 통과** |

새 이미지의 소스는 `24e17165ac6ef657ba9fa79c4ab8f784e98f20eb`, Security 생산자는 `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`다. **arm64 로컬 이미지**만 확인했다. 단위 회귀는 신규 36개·기존 시작/catalog 85개를 합쳐 **121 PASS, 실패·오류·건너뜀 0**이다. 이전 PostgreSQL suite와 겹치는 시험이 있으므로 누적 숫자를 합산하지 않는다.

## 대조 방식

`python -m sova.qualification.image`는 태그 대신 Docker가 반환한 전체 SHA-256 이미지 ID와 명시한 아키텍처를 요구한다. 자동 pull이나 다른 이미지로의 대체는 없다.

1. 두 저장소의 전체 Git SHA를 받아 고정된 Python·SQL 파일 해시를 계산한다. 현재 소스 파일도 그 커밋과 같아야 한다.
2. 저장소의 기존 SQL 별칭은 **같은 고정 패키지 안의 일반 파일을 가리키는 Git 등록 링크**에 한해 설치 payload로 해석한다. 외부 링크·링크 체인·미등록 링크는 거부한다.
3. 이미지 안의 설치 파일을 읽어 누락·추가·변조, 버전과 서비스 진입점을 대조한다. 패키지 버전·OCI revision 라벨만으로 통과시키지 않는다.
4. 네트워크 없는 비루트 컨테이너에서 읽기 전용 파일시스템, capability 제거, 자원 제한으로 검사한다. 호스트 소스·자격 파일·Docker 소켓은 마운트하지 않는다.
5. 결과 파일은 새 파일로만 생성하며 권한은 `0600`이다. 결과가 MATCH여도 실행 승인과 운영 자격은 발급하지 않는다.

설치된 배포 패키지 버전 목록도 남기지만 전체 OS/native 공급망 SBOM이나 취약점 검사를 대신하지 않는다. 이 검사는 **신뢰하는 로컬 Docker와 빌드 환경**을 전제로 한 소스 대조다. 서명 검증, 악의적인 이미지의 안전성 증명 또는 레지스트리 발행자 인증이 아니다.

## 실제 CLI 시험

새 이미지의 기본 `ENTRYPOINT ["sova-service"]`를 그대로 실행했다. 호스트 Python·소스 overlay·시험용 worker 클래스로 대체하지 않았다.

| 입력 | 요구 결과 |
|---|---|
| health port `0` | 지정된 health port 계약 오류로 종료 |
| lease `301` | PostgreSQL 실행 원장 범위 오류로 종료 |
| poll `nan` | 유효한 poll 간격 계약 오류로 종료 |
| 기본 간격 + 빈 catalog | 실제 catalog의 필수 앱 검사에서 종료 |

단순 비정상 종료는 통과가 아니다. 종료 코드 `1`, `run-worker failed` JSON 오류, **해당 계약 오류**가 일치해야 한다. ImportError·다른 설정 오류·타임아웃으로 끝나면 실패다.

컨테이너 네트워크는 `none`이며, 생성한 비운영 fixture 값만 읽기 전용으로 마운트했다. Pod 이름·UID는 형식 입력일 뿐 실제 Pod를 주장하지 않는다. Pod 관측자나 정지 증명을 가짜로 주입하지 않았고, 실제 DB·Kubernetes·고객 자격은 사용하지 않았다. 종료 후 시험 컨테이너가 남지 않았음을 확인했다.

## 빌드와 재현

기존 `deploy/service/Dockerfile`을 사용했다. 별도 Git archive 빌드 폴더에 고정 커밋의 `src`, `pyproject.toml`, Dockerfile만 담았고, 고정 Security 생산자의 wheel을 공급했다. 기본 Python 이미지도 Dockerfile의 digest를 유지했다. 기존 `sova-service:dev` 태그를 덮어쓰거나 이미지를 레지스트리에 push하지 않았다.

```sh
PYTHONPATH=src python -m sova.qualification.image \
  --image sha256:117a14eadae2cc25b43a434dbf9a06e1497f8e1c96ebc5e9da62a4c0fb7591f6 \
  --architecture arm64 \
  --source /path/to/sova-checkout \
  --source-revision 24e17165ac6ef657ba9fa79c4ab8f784e98f20eb \
  --security-source /path/to/security-checkout \
  --security-revision 27e6e85514a7cbb3e43d216464b72d9bd78f3fcf \
  --output /path/to/new-private-image-report.json

PYTHONPATH=src:tests python tests/qualification_image_cli.py \
  --image sha256:117a14eadae2cc25b43a434dbf9a06e1497f8e1c96ebc5e9da62a4c0fb7591f6 \
  --architecture arm64 --artifacts /path/to/new-private-cli-directory
```

지정한 SHA와 소스 파일이 같은 checkout이 필요하며, 후속 커밋의 코드를 임의로 대신 사용하지 않는다.

## 未検証の境界

- 의존 패키지는 기존 Dockerfile의 범위 지정으로 해결했다. 실제 버전과 로그는 보존했지만 의존성 잠금·재현 가능 빌드·취약점·서명·amd64 시험은 미실시다.
- 로컬 Docker image ID를 기록했다. 실제 Pod의 `imageID`나 레지스트리 digest와의 대응을 관측한 것은 아니다.
- 정상 worker Ready, 실제 catalog의 업무 실행, 실제 Pod의 SA/UID/RBAC, 인증된 Pod 부재 확인, lease 만료 후 복구는 남아 있다.
- AWS→독립 VM, 보호된 네트워크·witness, 지속 운영 SLO·금융 인증은 이 시험으로 PASS 처리하지 않는다.

[공개 증거 요약](evidence/service-image-qualification-20261118.json) · [개발 기록](development/service-image-qualification-2026-11-18.md).
