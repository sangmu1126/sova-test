# 계약 계획의 읽기 전용 복구 보류

현재 관측 형식은 **v2**다. [거버넌스 보류 연결 기록](development/governance-recovery-status-2026-11-19.md).
이전 v1은 operation 플래그만 읽었으며 별도 거버넌스 원장의 활성 보류는 포함하지 않았다.

## 목적과 책임 경계

실행 프로세스가 종료되면 권한 원장은 `SOURCE_FENCED`인데 효과 원장에는
`in_flight` 또는 `unknown`이 남을 수 있다. 권한 상태만 보여주면 실행을 계속할
수 있다고 오해할 수 있으므로, **관측 보류를 별도로 표시**한다.

저장된 권한 상태를 `SAFE_HOLD`로 바꾸거나 원격 결과를 추정하지 않는다.
워커·효과 CAS·복구 증명·인가·DB 스키마는 수정하지 않았다.
재승인만으로 불확실성을 해소하거나 명령을 다시 발행하는 기능도 없다.

## 상태 API

기존 `GET /v1/tenants/{tenant}/applications/{app}/units/{unit_digest}`의 인증·
테넌트·역할 검사와 기존 응답 필드를 유지하고 다음 필드를 추가한다.

| 필드 | 의미 |
| --- | --- |
| `observation_version` | `handoff-recovery-status-v2` |
| `scope` | 현재 tenant·app·unit·plan digest·operation ID |
| `observed_at`, `expires_at` | DB 관측 시각, 5초 유효 기한 |
| `recovery.status` | `clear` / `required` / `unavailable` |
| `recovery.required` | `false` / `true` / `null`; 미확인은 false가 아님 |
| `recovery.reason` | 서버가 계산한 고정 사유 코드 |
| `recovery.pending_effects` | 명령 지문·action·상태·revision |
| `recovery.security_holds` | 거버넌스의 활성 보류 개수·ID, operation 플래그. 작업을 확정하지 못하면 플래그는 null |
| `recovery.next_steps` | 운영자 확인 순서. 실행 명령이 아님 |
| `automatic_replay_allowed`, `authorizes_execution` | 항상 `false` |

권한 행과 외부 witness는 기존 공유 advisory lock을 잡은 뒤 확인한다.
READ COMMITTED를 유지하여 커밋 직전의 witness를 오래된 스냅샷과 비교하지 않는다.
트랜잭션은 `READ ONLY`이며 SQL 실행 제한 2초, lock 대기 제한 1초다.
이 값은 HTTP·외부 witness I/O를 포함하는 종단 지연 보장이 아니다.

현재 계획의 operation·미확정 효과·해당 operation의 실행권과 앱의 거버넌스 보류를
한 SQL 스냅샷으로 읽는다. 다른 작업의 실행권을 현재 실행권으로 가져오지 않는다. 같은 앱의 다른
작업에 미확정 효과가 남으면 별도로 보류한다. 명령 원문은 기존 계약으로 무결성과
scope·계획 결속을 검사하되 응답에 포함하지 않는다.
operation이 없거나 둘 이상이거나 결과 상한 256건을 초과하면 `unavailable`이다.
누락·절삭된 목록을 정상 빈 목록으로 취급하지 않는다.
거버넌스 원장의 최신 이벤트가 열린 보류만 포함하며 기존 보류 검증기를 재사용해
문서 해시·tenant·app·ID·revision을 확인한다. 잘못된 문서나 256개를 넘는 활성 보류는
조회 실패로 남긴다. 보류의 reason·actor·release 문서 등 원문은 응답으로 내보내지 않는다.

## 주 사유 판정 순서

| 조건 | 사유 | 관측 상태 |
| --- | --- | --- |
| `unknown` 효과 존재 | `EFFECT_RESULT_UNKNOWN` | required |
| 복구 실행권과 미확정 효과 존재 | `RECOVERY_PENDING` | required |
| 미확정 효과 존재, 유효 실행권 없음 | `OWNER_EXPIRED_UNSETTLED` | required |
| 완료·중단 상태인데 미확정 효과 존재 | `TERMINAL_WITH_PENDING_EFFECTS` | required |
| 같은 앱의 다른 작업에 미확정 효과 존재 | `APPLICATION_PENDING_EFFECTS` | required |
| operation의 보안 보류 플래그 또는 앱의 활성 거버넌스 보류 | `SECURITY_HOLD` | required |
| 권한 상태 SAFE_HOLD / FAILBACK_REQUIRED | `AUTHORITY_HOLD` | required |
| 위 보류가 없는 현재 관측 | 사유 없음 | clear |
| 미등록·모호·만료·응답 불일치 | `OBSERVATION_UNAVAILABLE` 또는 조회 거부 | unavailable |

UNKNOWN과 보안 보류가 함께 있으면 주 사유는 UNKNOWN을 유지하되 활성 보류를
별도 표시한다. 보안 보류가 있으면 확인 순서의 첫 항목은 `REVIEW_SECURITY_HOLD`다.
보안 보류만 있으면 원격 요청 정지나 명령 readback을 불필요하게 안내하지 않는다.

`clear`는 실행 허가·전체 보안 PASS·이전 완료가 아니다. 이 조회는 모든
통제·변경 피드·승인·verifier 신뢰를 다시 판정하는 게이트가 아니다.
실행 시에는 기존 정책·실행권 검사와 인증된 원격 정지·readback 증명이 필요하다.

## 콘솔과 로컬 연결

React 콘솔의 기존 화면을 유지하고 **계약 계획** 메뉴를 추가했다.
보류와 저장된 권한 상태, 계획 결속, 미확정 효과, 확인 순서를 함께 표시한다.
2초 간격 GET으로 읽고, 5초 관측 기한이 지나거나 조회가 실패하면 현재 결과를
철회한다. 새로고침 외에 복구·재실행·승인·중단·쓰기 재개 버튼은 없다.

브라우저는 기존 로컬 운영자 인증 뒤에 `/api/native/status`를 호출한다.
upstream JWT는 서버에 보관한다. 설정과 자격 파일은 소유자 전용 일반 파일만
허용하며 심볼릭 링크·다른 소유자·그룹/전체 권한을 거부한다. 매 요청마다 자격
파일을 읽고, 거부된 요청에는 과거 성공 결과를 반환하지 않는다.

아래는 **설정 형식 예시**이며 실제 관측·배포 결과가 아니다.

```json
{
  "origin": "http://127.0.0.1:8910",
  "tenant_id": "registered-tenant",
  "application_id": "registered-application",
  "unit_digest": "실제 등록된 64자리 SHA-256",
  "token_file": "/absolute/private/path/auditor.jwt"
}
```

설정·JWT 파일은 `0600`으로 준비하고 가능한 한 tenant의 auditor 전용 JWT를 쓴다.
값은 Git·브라우저 저장소·채팅에 넣지 않는다. origin은 사용자 정보·경로·query가
없는 명시적 `http://127.0.0.1:포트`만 허용한다. 고정한 scope만 GET하며 redirect·
임의 upstream·클라이언트 query·POST는 허용하지 않는다.

```sh
# 기존 보안 콘솔의 소유자 전용 session-root가 준비된 상태
python demo/security_console.py \
  --session-root /absolute/private/session \
  --ui-root console/dist \
  --native-status-config /absolute/private/native-status.json
```

이 명령은 native API·계획·DB를 만들거나 새 이미지를 배포하지 않는다.
새 상태 API와 실제 등록된 계획이 먼저 필요하다. 이전 이미지가 신규 관측 형식을
제공하지 않으면 미확인으로 남는다.
v2 API·브리지·콘솔은 함께 갱신한다. v1 관측에 보류가 없다고 추정하는 fallback은
없다. 형식이 맞지 않는 조합은 변경을 실행하지 않고 조회 미확인으로 남는다.

**운영 한계:** 같은 PC의 신뢰된 운영자와 명시적 loopback upstream을 전제로 한다.
원격 TLS, 사용자별 위임·인가 매핑, 기업 로그인, 클라우드 배포 또는 금융 보안
자격을 완료한 구현이 아니다. 워크로드 대응 화면·기존 무상태 이전과도 별도 경로다.

## 검증과 후속 인수

[개발 기록](development/recovery-status-2026-11-19.md)과
[기계 판독 증거](evidence/recovery-status-20261119.json)에 코드 SHA·실패 이력·
실행 결과·원본 해시를 남겼다. 실제 격리 PostgreSQL·API·워커 종료와 원격 요청
정지 후 readback을 사용한다. 로컬 DB·브라우저 시험을 Kubernetes·클라우드 운영
복구 인수나 무중단 자격으로 승격하지 않는다.

남은 운영 작업은 인증된 원격 정지 증명, 실제 운영 인증·전송 경로, 감사·보존·
알림, 운영자 복구 runbook 및 별도 변경 승인이다.
