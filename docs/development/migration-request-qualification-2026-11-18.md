# MQ-01/02 개발 기록 · 다른 세션과 범위를 분리한 HTTP 업무 대조

2026-11-18 · 개인 `sova-test`의 `feature/migration-request-qualification`.

## 요청과 소유 범위

사용자는 프로덕션 자격 검토를 바탕으로 개발을 요청했고, 다른 세션과 겹치지 않도록 범위를 확인하도록 했다. 이 세션은 `src/sova/qualification`과 전용 시험·프로필·새 문서만 소유한다. 관측한 상대 작업은 `feature/contract-driven-foundation`의 `11f9018`이며 볼륨·MongoDB·Kafka·클러스터 경로다. 현재 파일 경로의 교집합은 없었다. 외부 세션의 전체 계획을 확인하거나 합의했다고 표현하지 않는다.

새 개발 작업 트리를 별도로 만들었다. 기존 어댑터·공통 원장·service·infra·다른 작업 트리는 변경하지 않고 소비했다. `personal-development/SESSION-SCOPES.json`은 로컬 공용 안내이며 잠금이 아니다.

## 선택과 구현

1. 기존 PostgreSQL 주문·멱등 결과·작업 fixture를 재사용하고 실제 HTTP 표면을 추가했다. 새 범용 주문 제품이나 복제 엔진을 만들지 않았다.
2. SQLite FULL 커밋으로 제출 의도와 전송 시도부터 보존했다. 응답·실패는 별도 append-only 레코드다. 로컬 파일을 WORM이나 서명된 운영 증거로 표현하지 않는다.
3. 최종 DB를 별도의 읽기 전용 snapshot으로 관측한다. 성공 응답 누락·중복·다른 금액·업무 효과·영수증 불일치를 검출한다.
4. timeout 뒤 저장된 주문은 `UNRESOLVED_COMMIT`로 남긴다. 조회로 확인하고 같은 요청을 재시도했을 때 한 주문에 수렴한다. 미해결 요청은 CLI도 성공 종료하지 않는다.
5. 다른 request ID로 같은 업무를 중복 집계하지 않도록 `(customer,idempotency_key)`도 고정했다. 고객이 다르면 같은 키를 별개 업무로 허용한다.
6. 프로세스 재개·clock domain 변경·시계 역행을 구분한다. 재시도에서 대기 시간을 초기화하거나 다른 프로세스 시각을 빼지 않는다.

## 실제로 찾아 고친 문제

- **금액 정밀도 손실:** 시험 앱과 독립 조회의 소수 둘째 자리 포맷이 `10.001`을 `10.00`으로 보이게 할 수 있었다. DB 수치를 문자열로 그대로 보존하고 기존 주문의 금액 비교는 Decimal의 정확한 값으로 했다. 실제 DB 변조 대조를 추가했다.
- **기존 주문을 실행 성과로 집계할 가능성:** 제출 없이 lookup만 허용하면 기존 주문을 이번 요청 처리로 표시할 수 있었다. 이전 submit 시도 기록을 요구했다.
- **시계 역행:** 재시도 시작이 요청 생성보다 늦어도 이전 응답보다 이른 경우가 있었다. 같은 프로세스의 마지막 관측 시각까지 검사했다.
- **결과 불명의 종료 코드:** DB 결과가 존재한다는 이유만으로 CLI가 0을 반환할 수 있었다. 모든 요청이 CONFIRMED여야 0으로 끝내도록 고쳤다.

## 시험 환경 분리와 실패 기록

이번 범위 확인 이후 실행은 공유 kind PostgreSQL 대신 run 전용 Docker DB를 썼다. 캐시 이미지의 정확한 ID·arm64·실제 PostgreSQL 17.12를 기록했다. 호스트의 새 loopback 포트와 run 전용 서버/클라이언트 인증서를 사용했다. 클라이언트 개인 키는 DB 컨테이너에 마운트하지 않는다.

초기 setup 두 번은 `--internal` 네트워크에서 이 Mac Docker가 published port를 주지 않아 시험 시작 전에 실패했다. 두 번째 실행은 DB Running·OOM 없음·빈 port binding을 기록했다. 전용 bridge와 loopback 바인딩으로 고치고 TLS 클라이언트 인증서를 요구했다. 초기 실패와 자원 정리 기록을 보존했다. 기존 클러스터·서버 설정은 바꾸지 않았다. 전용 bridge의 egress 차단을 입증했다고 쓰지 않는다.

## 최종 검증

- 단위 56개 + 실제 HTTP/PostgreSQL 통합 3개: **59 PASS, 0 FAIL, 0 SKIP**.
- 정상 전환·503 재시도, 커밋 후 응답 timeout·이전 후 조회·재시도, 잘못된 토큰/tenant 거부.
- 실제 DB에서 주문 삭제와 `10.00→10.001` 변경을 검출했다. 각 부정 대조를 예상 VIOLATION으로 기록했다.
- Ruff 통과. 공유 시험 환경 미사용. 소유 컨테이너·네트워크 정리 확인.
- 최종 run-05에서 측정한 소스 10개 SHA-256이 커밋된 파일과 모두 일치함을 확인했다.
- 공개 가능한 [결과 요약](../evidence/http-request-qualification-20261118.json), [사용·범위 문서](../migration-http-qualification.md).

기능 커밋은 `7be427f`(요청 원장·대조·CLI)와 `0a89d78`(실제 HTTP·전용 TLS DB 시험·초안 프로필)로 나눴다. raw 자료는 로컬 `local-dev-records/migration-qualification-20261118/run-05`에 있고, 공개 요약은 원문 해시와 기대 결과만 제공한다.

## 아직 하지 않은 것

운영 HTTP API·OIDC·배포 워커 경유 이전, 클라우드·독립 두 서버·실제 Edge·복귀, 부하/SLO·독립 운영 서명·운영 자격 발급은 이 단계에 포함하지 않는다. 모든 측정 보고서는 `qualification_issued=false`, `authorizes_execution=false`, `service_continuity=NOT_QUALIFIED`를 유지한다. 원격 CI 통과도 이 로컬 실행으로 대신하지 않는다.

MQ-01은 업무 프로필 초안, MQ-02는 제한된 구성 요소 통합 검증 상태다. 기존 계획 표의 “계획”을 전체 운영 완료로 바꾸지 않는다. 다음 MQ-03 접점은 서비스 API/워커 소유 범위를 다시 확인한 뒤 연결한다.
