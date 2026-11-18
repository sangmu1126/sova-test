# 실제 HTTP·PostgreSQL 이전 검증용 앱

로컬 사용자 E2E와 개인 AWS→Linux VM 경로가 같은 `sova.qualification.order_app` 코드를 사용한다. 실제 주문·작업·멱등 결과를 PostgreSQL 트랜잭션에 기록하고 HTTP 응답을 돌려준다. 고객용 인증·결제·재고 시스템은 아니며, 생성한 canary 데이터만 사용한다.

## 구성

`/run/sova`에 아래 파일만 읽기 전용으로 마운트한다. 값은 이미지·환경변수·Git에 넣지 않는다.

| 파일 | 내용 |
|---|---|
| `config.json` | version 1, tenant_id, application_id, run_id, source/target side, 실제 OCI deployment_digest, 최대 8시간의 expires_at |
| `database.dsn` | 해당 환경의 앱 역할만 가진 단일 DB conninfo. password·verify-full·마운트 CA·1~10초 connect timeout 필수 |
| `database-ca.pem` | 해당 DB 서버를 검증할 공개 CA |
| `client.token` | 실행별로 새로 생성한 무작위 canary 클라이언트 자격 |

앱은 관리자 자격·경로 변경·승인·클라우드 자격을 받지 않는다. 일반 쓰기·조회 연결에서 실제 DB 역할의 superuser, 역할·DB 생성, replication, RLS 우회와 역할 멤버십을 거부한다. 명시적인 직접 테이블 권한만 쓰는 제한된 canary 프로필이다.

`POST /orders`에는 Bearer 자격, `X-Qualification-Tenant`, `X-Qualification-Run`, `Idempotency-Key`가 필요하다. `GET /orders`로 업무 키를 재조회한다. 최초 응답 유실 시 같은 키의 조회·재요청으로 커밋 여부를 대조한다. `/healthz`는 side·앱 신원과 프로세스 수명만 확인하며, DB 준비·데이터 무결성을 대신하지 않는다. body는 파싱 전에도 4096바이트로 제한한다.

## 산출물

`infra/personal-journey/build_canary.py --output <새 비공개 폴더>`가 허용한 소스 파일과 해시 고정 dependency lock만 임시 context로 복사해 Docker Buildx OCI 산출물을 만든다. 저장소 전체나 `.env`, AWS 설정, Git 정보는 context에 넣지 않는다. Linux ARM64, UID/GID 10001, image manifest·config·모든 layer·archive 해시를 검증해 `image.json`에 기록한다.

OCI manifest digest와 런타임이 보고하는 image/config ID를 혼동하지 않는다. 실제 반입 뒤 같은 manifest와 config를 읽어 확인해야 같은 산출물 배포라고 판정한다. 빌드 성공은 취약점 정책·실제 배포·이전 성공이 아니다.

소스·목적지 DB의 writer 비밀번호는 서로 다르게 생성한다. 네이티브 worker 설정에는 PostgreSQL의 `database_role_password_files`를 사용해 모든 등록 DB와 writer의 파일을 지정한다. legacy 공통 `role_password_files`와 혼용하지 않는다. 누락·미등록 writer는 네트워크 요청 전 거부하고, 새 연결 때 파일을 다시 읽는다. 런타임 권한 회전과 실제 양쪽 DB 인수는 별도 시험한다.

루트 파일시스템 읽기 전용, 권한 상승 금지, capability 제거, 자원 상한, 외부 발신 차단은 배포 계층에서 실제 적용·검증한다. 이 문서가 그 결과를 미리 PASS로 만드는 것은 아니다.
