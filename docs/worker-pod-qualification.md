# 실제 Pod 워커 신원·정지 확인·복구 검증

> **2026-11-20 안전 조건 변경:** 아래 기록은 명시한 과거 이미지의 시험 결과다. 현재 기본 워커는 Pod 종료와 별도로 원격 요청의 종료를 확인하는 관측자가 없으면 자동 복구 권한을 발급하지 않는다. 과거 PASS를 현재 코드의 복구 자격으로 재사용하지 않는다.

2026-11-18 · 개인 `feature/migration-request-qualification`.

## 확인한 범위

고정 이미지의 **기본 `sova-service run-worker` 진입점**을 새로운 로컬 kind 클러스터에서 두 Pod로 실행했다. 시험용 worker 클래스·가짜 Pod 상태·소스 overlay를 사용하지 않았다. 기존 공유 클러스터 두 개와 AWS는 변경하지 않았다.

핵심은 **lease가 만료됐다는 사실만으로 다른 워커에게 실행을 넘기지 않는 것**이다. 실제로 기존 워커의 프로세스를 멈춰 lease를 만료시켰지만 Kubernetes에는 Running Pod로 남겼다. 이때 복구는 차단됐다. 이후 kubelet에 의한 컨테이너 종료와 Pod 부재를 확인한 뒤 다른 워커가 진행 중 효과를 조회해 이전 작업을 완료했다.

| 확인 항목 | 실제 근거 | 최종 결과 |
|---|---|---|
| 정상 CLI 시작 | 두 Pod의 health Ready, 실제 catalog로 SQL 효과 진행 | 확인 |
| 실행 코드 | Pod 안의 Sova 317개·Security 122개 설치 파일 대조 | 고정 소스와 일치 |
| 소유자 신원 | DB owner의 Pod 이름·UID·프로세스 instance와 실제 Pod/SA UID | 일치 |
| 살아 있는 소유자 | 실제 runtime task pause, DB lease 만료, SA로 Pod 조회 200 | 복구 0건·효과 `in_flight` 유지 |
| 권한 없는 Pod 관측 | 별도 SA의 실제 조회 403 및 해당 SA UID의 감사 로그 | 정지 여부 `unknown` |
| 워커의 Secret 조회 | 실제 워커 토큰으로 Secret GET | 403 거부 |
| 기존 워커 정지 | 일반 Pod 삭제, kubelet의 종료, CRI `CONTAINER_EXITED`, 인증된 Pod 조회 404 | 정지 확인 |
| 인계 | 다른 실제 Pod UID를 가진 소유자가 recovery lease 획득 | `HANDOFF_COMPLETE` |
| 변경 재실행 방지 | API 서버 감사 로그의 성공한 Service PATCH 횟수 | **1회** |
| 데이터 | 과거 주문 50건과 목적지의 신규 SQL 쓰기 | 보존·쓰기 확인 |

완결 통합 시나리오 **1 PASS, 실패·오류·건너뜀 0**이다. 시험 안에서 위 조건을 함께 검사했다. 여러 개의 독립 운영 자격이나 이전의 단위 시험 수와 합산하지 않는다. 호스트 시험 모듈 import에서 발생한 Starlette의 TestClient 사용 중단 예고 경고 1건도 원문에 보존했다.

## 실행 흐름

```text
고정 이미지 로드 → HTTP 계획 등록·승인·접수 → 두 CLI 워커 Ready
→ 실제 SQL 복사·쓰기 권한 전환 → 실제 Service selector 변경
→ 효과 확인 전 소유자 runtime pause
→ 실제 lease 만료 + Running Pod 조회 200 → 복구 차단
→ 권한 없는 Pod 조회 403 → unknown
→ 일반 Pod 삭제 + 실제 runtime 종료 확인
→ 다른 워커의 Pod 부재 조회 404
→ recovery lease → 진행 중 효과 readback → 완료
```

target endpoint를 늦게 준비해 route 효과가 진행 중인 구간을 만들었고, 소유자를 멈춘 상태에서 endpoint를 준비한 뒤 정지·복구를 검증했다.

Pod API 객체를 강제로 지우는 것만으로 프로세스가 죽었다고 간주하지 않았다. `--force`·0초 grace 삭제를 사용하지 않았고, 전용 Pod의 1초 종료 유예 후 실제 컨테이너가 종료된 상태를 별도로 확인했다. 이 시험은 노드가 정상 응답하는 로컬 환경이며, 노드 분할·원격 runtime 정지 불명 상태의 복구 자격을 증명하지 않는다.

## 이미지와 신원 연결

- 서비스 빌드 소스: `24e17165ac6ef657ba9fa79c4ab8f784e98f20eb`.
- Security 생산자: `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`.
- Docker 이미지 ID: `sha256:117a14eadae2cc25b43a434dbf9a06e1497f8e1c96ebc5e9da62a4c0fb7591f6`.
- 두 Pod와 CRI에서 관측한 이미지 ID: `sha256:9532154cc628884b35cc49ed6c8f0c9a6ed89f1eb13ac763644396c247a9cc07`.
- 최종 시험 코드: `237eee26cc432b24c53e1acf20990994fe36b642`.

Docker가 보존한 상위 이미지 ID와 CRI가 반환하는 ID를 같은 문자열이라고 가정하지 않는다. run 전용 태그를 생성해 고정 이미지를 load했고 `imagePullPolicy: Never`를 적용했다. 두 Pod의 imageID를 로드한 CRI ID와 대조했으며, 한 Pod에서는 설치 파일 439개도 직접 읽어 고정 소스와 대조했다. 레지스트리 발행·서명 검증을 한 것은 아니다.

Pod 신원은 Downward API의 이름·UID와 매 시작마다 새로 생성되는 instance를 사용했다. 감사 기록의 SA UID·토큰에 결속된 Pod UID를 실제 Kubernetes 객체와 대조했다. 토큰·Secret 값은 공개 증거에 넣지 않았다.

## 격리와 재현

`tests/qualification_postgres.py --suite pods`는 label로 소유권을 확인하는 전용 TLS DB·네트워크와 UUID 이름의 kind 클러스터를 만든다. 전용 kubeconfig만 사용하며 `~/.kube/config`를 변경하지 않는다. 테스트에서 노드를 추가 DB 네트워크에 연결한 뒤, 종료 시 소유권을 다시 확인하고 클러스터·전용 이미지 태그·DB·네트워크를 정리한다.

```sh
caffeinate -di python tests/qualification_postgres.py \
  --suite pods \
  --image sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac \
  --security-source /path/to/pinned-security/src \
  --artifacts /path/to/new-private-run-directory
```

이 suite는 명시된 arm64 서비스·node 캐시 이미지가 있어야 한다. 다른 버전을 자동으로 pull하거나 대신 선택하지 않는다. Docker 메모리가 부족하면 기존 자원을 비우거나 줄이지 않고 시험을 거부한다. 실제 workload Pod에는 restricted Pod Security와 비루트·읽기 전용 root filesystem을 적용했다. SA에는 이 시험 namespace의 Pod 관측과 지정 Service 변경 권한만 주었고 Secret 읽기는 주지 않았다.

PostgreSQL은 `verify-full`과 생성한 클라이언트 인증서를 사용했다. 서버의 localhost 인증서 이름과 전용 Docker IP를 libpq의 `host`·`hostaddr`로 구분해 사용했으며 hostname 검사를 끄지 않았다. DB·호스트 시각 감시 184개 샘플이 통과했다. 이는 시각 오차의 관측 하한을 검사한 결과이며 정밀 시각 동기화 인증은 아니다.

## 운영 자격으로 확대하지 않는 부분

- 하나의 로컬 kind 노드와 하나의 PostgreSQL 서버 안의 논리 DB들이다. AWS→독립 온프렘 이전이 아니다.
- 실행기는 실제 제품 코드지만 계획 inventory·attestation·IdP·change-feed watermark는 생성한 시험 자료다. API는 호스트의 기존 TCP 시험 서버이며 배포된 다중 API·기업 SSO를 검증하지 않았다.
- endpoint Pod는 라벨·EndpointSlice 관측용 Python sleep 프로세스다. 실제 HTTP 주문 앱이나 안정 진입점의 업무 응답을 검사하지 않았다. **`HANDOFF_COMPLETE`는 이 계획의 상태이며 고객 서비스 무중단 판정이 아니다.**
- witness는 전용 폴더를 PV로 공유하는 시험용 파일이다. WORM·독립 보존·분리 권한·다중 노드 저장소를 검증하지 않았다. 합성 witness 파일의 시험 쓰기 권한도 운영 설정으로 재사용하지 않는다.
- DB fixture 역할들은 인증서를 공유하고 업무 어댑터는 전용 서버의 관리자 접속을 쓴다. 고객별 최소 DB 권한·운영 workload identity의 자격을 부여하지 않는다.
- NetworkPolicy·외부 통신 거부·노드 장애·기존 세션·부하/SLO·다중 노드 HA·생산 Deployment/PDB·amd64·공급망 취약점/서명 검증은 범위 밖이다.

후속 [실제 HTTPS 업무 요청 시험](worker-pod-http-qualification.md)에서 고정 시험 Service와 실제 앱의 지속 요청·오류·지연·멱등 결과를 대조했다. 데이터는 복구됐지만 최초 요청 오류가 있어 무중단으로 판정하지 않았다. EndpointSlice Ready만으로 업무 가용성을 PASS 처리하지 않아야 한다.

[공개 증거 요약](evidence/worker-pod-qualification-20261118.json) · [개발 기록](development/worker-pod-qualification-2026-11-18.md).
