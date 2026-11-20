# 실제 변경 관측의 영속 저장과 권한 분리 — 2026-11-20

## 완료한 범위

기존 CloudTrail·PostgreSQL 관측기를 기존 PostgreSQL 효과/승인 원장과 같은 DB의 변경 이력에 연결했다. 실제 AWS EC2/SG 이력과 **새로운 source 시험 DB**의 스키마 변경을 저장하고, 새 수집기·새 DB 연결에서도 같은 기록이 읽히는지 확인했다. 원래 앱의 DB나 진입점은 변경하지 않았다.

CloudTrail 이벤트 하나가 여러 자원/환경에 걸릴 때 첫 자원 뒤의 관측을 누락하던 수집기 동작을 수정했다. `(AWS event ID, 환경, 자원 UID)`별 안정적인 v2 기록 ID를 사용한다. 같은 자원의 별칭은 중복 발행하지 않는다. 이전 v1 기록은 수정하거나 지우지 않는다. DB 저장 후 응답 유실과 재시작 재호출도 같은 기록으로 복원한다.

## 실제 권한 경계

[전용 역할 검증 원본](../evidence/personal-observer-authority-20261120.json)

- 새 관측 역할은 비관리자·다른 역할 membership 없음·연결 4개 상한이다. 기존 역할의 권한을 넓히지 않았다.
- 기존 강제 RLS 위에 이 역할에만 적용되는 restrictive 정책을 추가했다. tenant·app·원본 환경·수집기 ID·허용 변경 종류·기한을 동시에 검사한다.
- 관측 이력의 SELECT/INSERT만 허용한다. 삭제·수정, 완전성 watermark INSERT/UPDATE, 계획 변경, 효과 조회, 스키마 생성, 업무 DB 접속은 허용하지 않는다.
- 다른 tenant/app/environment·미등록 수집기·registry 주장·미래 관측·계획의 자체 변경이라는 `cause` 위조도 거부한다. 실제 기존 관측의 조회와 멱등 append는 허용된다.
- **실제 경계 18개 통과.** 거부 시험의 모든 INSERT는 허용됐더라도 rollback되도록 했다. 검증 실패 시 새 역할만 NOLOGIN으로 닫는다. 비밀번호/DSN은 private 로컬 파일에만 둔다.

[PostgreSQL restrictive 정책](https://www.postgresql.org/docs/17/sql-createpolicy.html)은 기존 permissive 조건과 AND로 결합한다. [VALID UNTIL](https://www.postgresql.org/docs/17/sql-createrole.html)은 비밀번호 기한이며 기존 연결의 종료를 뜻하지 않는다. 그래서 행 접근 정책에도 DB clock 기반 기한을 넣었다. 기존 시험 기한 **11/20 22:20 KST**를 연장하지 않는다.

## 실제 저장·재조회

[최종 코드의 실제 실행 원본](../evidence/personal-durable-observers-20261120.json)

- 전용 역할을 통한 **12개 검사 통과**: 실제 schema/AWS 관측 저장·새 인스턴스 조회·멱등 재호출·재시작 중복 방지·이력 삭제 거부·테넌트 분리 등이다.
- 첫 시험은 기존 host runtime 역할로 기록 경로를 확인했다. 뒤 시험은 새 관측 역할로 다시 확인했다. 두 결과의 producer SHA와 권한 범위를 합치지 않는다. 최종 CLI는 넓은 runtime 자격으로 fallback하지 않는다.
- AWS에서 새로 조회했더라도 이미 원장에 저장한 이벤트는 새 삽입으로 세지 않는다. 스키마 비교 polling과 CloudTrail 조회의 지연·누락 한계는 남아 있다.
- 실제 native 계획·작업·효과는 전후 **0/0/0**, 원래 주문/작업/멱등 행은 **1/1/1**로 같다.

## 판정의 한계

관측의 영속 저장과 **빠짐없는 수집의 증명**은 다르다. 이 역할에는 후자를 발행할 DB 권한도 주지 않았다. 환경 watermark는 만들지 않았으며, 실제 원장에 저장한 관측으로 만든 **진단용 index**는 `plan_stale=true`, `plan_unknown=true`, `commands_allowed=false`였다. 이것은 실제 접수·승인된 원래 앱의 계획이 아니다.

운영 수집 daemon을 배포한 것도 아니다. 이 실행기는 운영자 인수 하네스이며 새 scratch DB 준비와 별도 감사용 조회를 위해 운영자 자격도 사용한다. 실제 수집 서비스에는 관측 전용 credential만 제공해야 한다. AWS/SQL 수집 신원 최소화와 전체 변경 채널 보증은 후속 인수 범위다.

## 현재 코드 재측정의 실패

[실제 실패와 원본 보존 근거](../evidence/personal-directional-refusal-20261120.json)

수집기 변경으로 native/security 전체 소스 지문은 `acc368a20daa6643ae336a4792d142cbe0d61d6a345617a746e276f7b69faad5`가 됐다. 과거 `5ad880ee…` 자격을 현재 코드의 자격으로 재사용하지 않았다.

새 PKCE 세션에서 별도 시험 주체의 검증자 승인 revision 3과 같은 요구사항의 계약 revision 3 등록은 끝났다. 그러나 실제 분리 DB/Service 방향별 재측정은 15단계 중 14단계 뒤의 역방향 경로 재조회에서 BrokenPipe로 중단됐다. **새 자격은 발행하지 않았다.** 원래 앱 내용 SHA·시퀀스·권한·진입점은 전후 같고, 시험 writer는 모두 NOLOGIN으로 닫혔다. API·IdP도 정리됐다.

소유 VM은 RKE2 active이지만 4 CPU에서 load 약 33, `/readyz` 5초 조회 실패가 관측됐다. pinned SSH 경로의 API projection도 실패했다. 이것만으로 단일 근본 원인을 확정하지 않는다. 사용자 보류인 VM 우선순위 변경과 다른 세션 컨테이너 중지는 수행하지 않았다. 새 자격뿐 아니라 전체 writer/controller·변경 채널·위치 보증과 원래 앱 계획의 실행/연속성도 남아 있다.

## 코드 검증

- 수집기·변경 영향·비밀 비노출 회귀 31개 통과.
- `6b9ba0a` 원격 Python 3.11/3.14 각각 **1,393개 통과·제외 0개**, 콘솔 **77개·타입·빌드·포맷 통과**. [실제 CI](https://github.com/sangmu1126/sova-test/actions/runs/44740746883).
- 마지막 진단 비밀 비노출 시험은 위 CI 이후 추가됐다. 최종 SHA의 CI를 별도로 확인한다.
- 실제 모델 호출·VM 우선순위 변경 보류, 개인 `default` 계정만 사용, 팀/Sky/다른 세션 자원 변경 금지를 유지했다.
