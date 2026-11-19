# 실제 HTTPS 업무 요청과 Pod 워커 복구 대조

> **2026-11-20 안전 조건 변경:** 아래 기록은 명시한 과거 이미지의 시험 결과다. 현재 기본 워커는 Pod 종료와 별도로 원격 요청의 종료를 확인하는 관측자가 없으면 자동 복구 권한을 발급하지 않는다. 과거 PASS를 현재 코드의 복구 자격으로 재사용하지 않는다.

2026-11-19 · 개인 `feature/migration-request-qualification`.

## 결과

실제 주문 API를 출발지·목적지 Pod로 실행하고, **같은 HTTPS 주소와 Kubernetes Service**에 지속 요청을 보냈다. 실제 워커를 중단·복구하는 동안에도 요청을 계속 기록하고, 끝난 뒤 목적지 DB를 독립 조회했다.

최종 실행에서 **논리 주문 100건이 모두 한 번씩 처리·확인**됐다. 과거 주문 50건도 보존됐다. 다만 첫 시도에는 오류가 있었다. 이 결과는 **데이터 보존·복구 시험 통과이며, 오류 없는 무중단 운영 통과가 아니다.**

| 관측 | 결과 |
|---|---|
| 최초 논리 주문 | 100건 |
| 첫 시도 성공 | 75건 |
| 첫 시도 실패 | **25건: HTTP 503 16건 + 연결 오류 9건** |
| 명시적 복구 | 조회 25회(404) 후 같은 키로 POST 25회 |
| 전체 HTTP 시도 | 150회: 최초 100 + 복구 조회 25 + 복구 POST 25 |
| 최종 확인 | **100/100 CONFIRMED**, 미확인·미완료·중복 업무 효과 0 |
| DB 확인 | 주문·멱등 결과·완료 작업 각각 대응, 금액 일치 |
| 최대 논리 완료 지연 | **21,537.259ms** |
| 시도별 지연 | p50 38.507ms · p95 134.247ms · 최대 1,012.031ms |
| HTTP TLS | 올바른 CA·이름 성공, 잘못된 CA·이름 거부 |
| 워커 복구 | live Pod이면 lease 만료 후에도 차단, 종료 확인 후 다른 Pod가 완료 |
| 실제 route 변경 | API 서버 감사 로그의 성공한 Service PATCH **1회** |

복구 조회의 404는 최초 실패 요청이 DB에 없다는 확인이며, 성공 응답을 받았던 데이터가 사라졌다는 의미가 아니다. 이후 원래 주문·멱등 키로 다시 제출했다. 첫 실패 기록은 그대로 남았다.

최대 논리 완료 지연은 최초 의도 기록부터 최종 확인까지의 값이다. **이 시험은 최초 부하 중 자동 재시도를 하지 않고 마지막에 명시적으로 복구**하므로, 이 값에는 의도적으로 미룬 재시도 대기도 포함된다. 실제 서비스의 불가용 시간이나 즉시 재시도 정책의 복구 시간으로 해석하지 않는다.

## 실제 연결 구조

```text
별도 HTTPS 부하 클라이언트 컨테이너
  ├─ 전송 전 SQLite 요청·시도 기록
  ├─ CA·hostname 검증
  └─ https://orders.fixture:30443 (고정)
       → 같은 NodePort Service (UID 유지)
       → 출발지 주문 Pod / 목적지 주문 Pod
       → 각각 고정된 DB에 실제 transaction 수행

실제 sova-service run-worker Pod 2개
  → 기존 승인·효과 원장·SQL adapter
  → source fencing·복사·target 활성화·Service 변경
  → runtime pause·lease 만료·실제 Pod 정지 확인·readback 복구

요청 종료 → 독립 READ ONLY / REPEATABLE READ DB 조회
          → 요청·영수증·금액·멱등 결과·work item 대조
```

이전 시험의 sleep endpoint를 **실제 FastAPI·Uvicorn 주문 API**로 교체했다. 기존 `tests/qualification_orders.py`의 업무 처리를 재사용했다. source 앱은 source DB, target 앱은 target DB에만 연결하도록 구성했고, 클라이언트나 화면이 DB 포인터를 임의로 바꾸지 않았다.

Pod 안의 앱은 worker의 키·catalog·관리자 DSN을 마운트하지 않았고 Kubernetes SA 토큰 자동 마운트도 끄고 실행했다. DB 인증서는 기존 fixture처럼 공유하므로 고객별 DB 신원·최소 권한의 운영 자격을 주장하지 않는다.

같은 로컬 Service·NodePort와 URL을 유지했다. 클라이언트는 전용 Docker 네트워크에서 이 클러스터에만 접근했고, 포트포워드나 응답 목업을 사용하지 않았다. 주소의 DNS 매핑은 이 실행만의 고정 호스트 매핑이다. 외부 DNS·운영 Edge·AWS→온프렘 경로를 검증한 것은 아니다.

## 요청 측정과 오류 보존

- 최초 요청은 논리 요청마다 새 키를 사용한다. 실제 전송 전에 기존 `RequestJournal`의 SQLite FULL transaction에 기록한다.
- 매 시도는 새 TLS/TCP 연결을 사용한다. 기존 장기 연결·WebSocket·세션 이전과 구분한다.
- 단일 클라이언트가 응답 또는 1초 timeout 후 0.2초를 기다리는 **closed-loop** 부하다. 고정 5 RPS·고동시성·독립 도착률 시험으로 표현하지 않는다.
- 최초 부하 중 자동 재시도는 없다. 종료 후 실패한 의도를 같은 프로세스·clock domain에서 조회하고, 404인 경우에만 같은 내용·키로 POST한다.
- 모든 시도를 SQLite와 fsync하는 JSONL에 남긴다. HTTP 상태·연결 오류·출발/목적지 표시·시각·지연을 보존하며 토큰·원문 예외·인증 헤더는 기록하지 않는다.
- 최종 판정은 HTTP 200 개수만 세지 않는다. 독립 DB 조회에서 각 주문의 ID·금액·멱등 결과와 정확히 한 개의 완료 작업을 확인한다.

`reconcile`의 `service_continuity`는 계속 `NOT_QUALIFIED`이며 실행 승인·운영 자격을 발급하지 않는다. 시험용 파일은 소유자가 수정할 수 있어 WORM 또는 서명된 감사 증거도 아니다.

## 기준과 재현

- 실행기 최종 코드: `cb7d92144a1d9a90e7ecfa8454a1a83c0f61c525`.
- worker 이미지의 제품 소스: `24e17165ac6ef657ba9fa79c4ab8f784e98f20eb`.
- Security 생산자: `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf`.
- [전용 프로필](../examples/qualification/orders-pod-https-postgres-v1.json): 실제 HTTPS·Service·Pod·부하·복구 범위를 기록한 DRAFT. 운영 SLO 한도는 미정이다.

기존 `/private/tmp` Security checkout이 사라져 동일 SHA를 개인 로컬 Git에서 별도 `qualification-sources/security-27e6e855` 경로로 복구했다. 임의의 최신 버전이나 다른 생산자로 대체하지 않았다. worker 이미지의 설치 소스 대조도 다시 통과했다.

```sh
caffeinate -di python tests/qualification_postgres.py \
  --suite pod-http \
  --image sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac \
  --security-source /path/to/security-at-27e6e855/src \
  --artifacts /path/to/new-private-run-directory
```

전용 kind·DB·네트워크와 client를 새로 만들고 정리한다. 기존 두 kind 클러스터·공용 앱·사용자 kubeconfig·AWS는 변경하지 않는다. 캐시 이미지가 없으면 다른 버전을 자동 선택하지 않는다.

## 검증과 다음 경계

단위 시험은 신규 측정 회귀 6개와 기존 요청 원장 회귀 56개, **62 PASS**다. 최종 실제 클러스터 통합 시나리오는 **1 PASS**, 실패·오류·건너뜀 0이다. Starlette TestClient 사용 중단 예고 경고 1건은 남아 있다. 통합 시험 PASS를 무중단 PASS로 바꾸어 표시하지 않는다.

이전의 [Pod 신원·정지·복구 시험](worker-pod-qualification.md) 조건을 그대로 재사용하면서 HTTP 업무를 추가했다. fixture inventory·IdP·file witness, 공유 DB 인증서, 단일 PostgreSQL 서버·단일 kind 노드, 운영 NetworkPolicy·배포 release·기업 인증 미검증은 남는다.

다음에는 target을 먼저 준비하고 연결·쓰기 대기·재시도 정책을 구분해, 오류와 완료 지연을 실제로 줄이는 방법을 검증해야 한다. 그 전에 운영 업무가 허용하는 지연·최종 실패율·부하 범위를 정해야 한다. 이번에 관측한 25% 최초 실패를 지우거나 사후 기준을 완화해 무중단으로 처리하지 않는다.

[공개 증거 요약](evidence/worker-pod-http-qualification-20261119.json) · [개발 기록](development/worker-pod-http-qualification-2026-11-19.md).

## 후속 비교

[목적지 사전 준비와 한도 있는 주문 완료](bounded-order-completion.md)에 후속 구현·측정과 실패 기록을 분리했다. 위 25건 오류·최대 21.537초는 기존 고정 실행의 값이며 후속 결과로 대체하지 않는다. 이 문서의 기존 실행을 재현하려면 증거의 시험 스냅샷 `cb7d92144a1d9a90e7ecfa8454a1a83c0f61c525`와 당시 고정 이미지를 사용해야 한다. 최신 브랜치의 fixture와 이미지로 실행하면 별도 결과다.
