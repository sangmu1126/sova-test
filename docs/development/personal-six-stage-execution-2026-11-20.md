# 개인 AWS·로컬 VM 사용자 흐름 실행 기록 — 2026-11-20

## 범위와 현재 판정

개인 AWS 계정과 이 Mac의 Lima/RKE2만 사용한다. 팀 계정·Sky·팀 저장소는 변경하지 않는다. 개인 저장소의 `feature/migration-request-qualification`에서 작업한다. **여섯 단계의 완료 보고가 아니다.** 사전 검사, 구성 요소 시험, 실제 계획 실행 및 운영 자격을 각각 구분한다.

| 단계 | 현재 확인한 것 | 완료 전 필요한 것 |
|---|---|---|
| 1. 실제 환경 | 개인 EC2·보존 EBS·소유 VM 유지, 브리지 복구, 실제 앱·DB TLS 4경로, 미허용 포트 3개 거부, 진입점 TLS만 갱신 | 현재 시험 창은 **11/20 22:20 KST**까지. 실제 이전 완료와 별도 |
| 2. 실행 전제 | 실제 PKCE 신뢰/계약 revision 3, 부분 관측의 실제 원장 저장과 전용 역할. `5ad880ee…` 당시 방향별 자격 2개는 역사적 기록 | 현재 `acc368a2…` 방향별 재측정은 14단계 뒤 API readback에서 실패. 새 자격 미발행. 전체 writer/controller·변경 채널·항목별 위치·실행 verifier 인수 필요 |
| 3. 실제 계획 실행 | 기존 계획·독립 승인·원장·호스트 워커 경로 유지. 실제 제어 DB의 계획/작업/효과 **0/0/0** 확인 | 원래 앱의 동일 실제 계획에서 승인→워커→봉쇄→복사→검증→활성화→경로 전환 |
| 4. 새 쓰기·복귀·실패 | `5ad880ee…`의 별도 실제 DB/Service 15단계: 새 쓰기 보존 복귀·단일 writer. `acc368a2…` 재측정 실패 후 시험 writer 닫음·원래 앱 불변 | 원래 앱 계획의 회수·불확실 결과·프로세스 종료·복귀와 실제 연속 서비스 측정 |
| 5. 나머지 기능 | 실제 Harbor Oracle+별도 verifier/egress 경계, 원본/후보 이미지 검사, 실제 HTTPS 6회→OTel→독립 수신, 실제 태그 비용과 `fresh` 콘솔 표시, 별도 warm 복원 | 실제 모델 호출은 사용자 보류. 앱 패치 승격·Harbor 전체 운영 인수, 항목별 위치·복구 범위, 앱 내부 전체 관측·완전한 비용 비교 |
| 6. 콘솔 흐름 | 실제 로그인→키 승인→계약/방향별 자격/조사 등록→실제 422 보류와 읽을 수 있는 사유, 최신 관측 화면. 임시 IdP/API 정리·토큰 비영속 확인 | 같은 실제 앱·계획·승인·실행·대응·복귀의 전체 브라우저 E2E |


## 시험 시간과 인증서

최신 갱신 창은 **11/20 22:20 KST**이다. 아래 오전 창과 만료·실패 기록은 보존한다.

11/19의 만료·중지 기록은 그대로 보존했다. 11/20 오전에는 **10:20 KST까지의 새 시험 창**을 명시적으로 만들었다. 만료된 자격을 유효하다고 간주하거나 검사 기한을 없애지 않았다. CA/서버 인증서를 재발급했으며, 기존 개인 키·비밀번호를 유지했으므로 **키 회전으로 표현하지 않는다.** 새 CA 개인 키는 Mac 밖으로 내보내지 않았다.

갱신 전 실제 앱의 계획·작업·효과는 0/0/0이었다. 대상 Deployment와 Secret 참조를 UID/resourceVersion으로 제한해 변경했고 기존 주문 데이터·DB 비밀번호·이미지 digest·원본 진입점 방향은 유지했다. 원본은 주문/작업/멱등 결과 각 1건, 대상은 각 0건이다.

## 검증자 공개 키의 신뢰 경계

- 일반 계획 역할과 다른 `trust_requester`·`trust_approver` 권한을 추가했다. 요청자의 실제 인증 주체는 서버가 기록한다.
- 다른 인증 주체가 화면에서 검토한 요청 digest를 제출해야 승인된다. 공개 키 발행과 승인 기록은 같은 PostgreSQL 트랜잭션으로 저장한다.
- 만료·다른 digest·자기 승인·같은 revision의 경합·승인 ID 재사용·응답 유실을 거부하거나 원래 결과로 복원한다. 재시도로 요청의 기한을 늘리지 않는다.
- 공개 키만 화면/원장에 보낸다. 개인 검증자 서명 키는 지정된 macOS Keychain 항목으로 관리한다.
- 키 발행 승인 기록은 현재 실행 권한이나 전략 자격 PASS가 아니다. 실제 서명을 소비할 때 신뢰·범위·폐기·기한을 다시 확인한다.

실제 로컬 Keycloak 두 테스트 계정으로 PKCE 인수를 진행한다. 이는 두 개의 인증 주체를 확인하는 시험이며, 서로 독립된 사람 두 명이나 기업 SSO/MFA를 증명하지 않는다. 첫 기동은 300초 내 준비되지 않아 실패 기록과 로그를 남겼다. 재실행은 시험 IdP 기동 예산을 명시적으로 늘렸으며 인증·승인·증거 유효성은 완화하지 않는다.

## 실제 전략 시험과 발견한 결함

실제 AWS PG와 VM PG에 새 이름의 별도 시험 DB·역할을 만든다. 원래 주문 DB나 `orders-entry`를 변경하는 시험이 아니다. 실제 Kubernetes Service·endpoint·HTTPS 응답을 읽는 별도 진입점으로 경로를 확인한다. 메모리의 Route 값만 바꾸고 실제 전환이라고 기록하지 않는다.

첫 실행은 초기 Kubernetes API 읽기 중 연결이 닫혀 완료하지 못했다. **복사·전환 성공으로 처리하지 않았다.** 원본 데이터·역할 권한·Service UID/resourceVersion/selector가 동일함을 확인했고 시험 역할은 모두 NOLOGIN으로 닫았다. 원인 조사용으로 시험 DB와 식별자가 있는 전용 자원을 보존했다.

별도로, 대상 역할이 NOLOGIN인데 DB 권한만 있으면 활성화 readback이 성공할 수 있던 결함을 고쳤다. 활성화 관측은 역할의 실제 LOGIN 가능 여부를 함께 확인한다. 어댑터가 관측 과정에서 클러스터 전체 역할을 LOGIN으로 바꾸지는 않는다. 실제 새 연결·쓰기 시험은 여전히 독립 검증의 책임이다.

## 확인한 범위와 한계

[현재 증거 요약](../evidence/personal-resumed-qualification-20261120.json)에 원본 자료의 SHA-256을 연결했다. 실제 PostgreSQL 검증자 경계 시험 9개, 네이티브 역할/설정 단위 시험 63개가 통과했다. 선택한 설정 시험 중 별도 선택 의존성이 없는 18개는 실행되지 않았으며 전체 제품 통과로 합산하지 않는다.

현재 원본 진입점은 AWS를 계속 가리킨다. 실제 Service UID 오류 PATCH는 422, 다른 자원·Secret·namespace 요청은 403으로 확인했다. 이 증거는 가용성·복구·전체 이전을 대신하지 않는다. DB 주석 하나로 업무/백업/감사 위치를 동일하게 증명하거나, 단순 조회 이후 완전한 변경 수집 watermark를 넣지 않는다. 기존 OTel 후보의 취약점 BLOCK도 예외 없이 유지한다.

다른 세션의 작업 트리는 수정하지 않는다. 전달된 커밋에 필요한 선행 코드가 없어 충돌한 선택 병합은 중단하고 원래 브랜치를 복원했다. 미완료 코드를 추정해 채우거나 다른 세션의 미커밋 파일을 가져오지 않았다.

## 04:11 KST — 실제 브라우저와 별도 전략 시험 통과

[실행 증거](../evidence/personal-native-verifier-strategy-20261120.json)와 [실제 브라우저 화면](../evidence/personal-native-verifier-20261120.png)을 보존했다. 화면은 실제 PKCE·네이티브 HTTP API·VM PostgreSQL 결과이며, 이전 성공 화면이 아니다.

- Keycloak의 실제 cold start는 512.252초였다. 별도 주체 두 명의 PKCE, 보안 승인 역할 없는 요청 403, 다른 검토 digest 409, 앱 선택 해제 시 승인 상태 초기화, 토큰 비영속 저장을 확인했다. 승인 revision 1이 실제 VM 원장에 남았다. 시험 IdP·API는 정상 종료했다.
- 두 번째 전략 시험은 하네스가 HTTP 측면 이름을 임시 DB의 route 이름과 잘못 대응시켜 실패했다. 실제 응답을 받았더라도 불일치로 중단했으며, 매핑을 바로잡았다. 원본은 미변경이었다.
- 세 번째 시험 `d2b00c7`에서 실제 순방향 7단계, 역방향 8단계가 통과했다. 새로운 어댑터 인스턴스로 각 readback을 다시 확인했다. 목적지에 쓴 주문·작업·멱등 결과를 복귀 시 보존했다. 원본 앱의 내용 해시·건수·시퀀스·권한과 진입점 UID/resourceVersion/selector를 전후 대조했다.
- 이 시험의 HTTP backend는 기존 앱의 health 응답을 제공하는 별도 Service이고, 데이터 이동은 별도 시험 DB다. **기존 앱의 업무 요청이 그 시험 DB로 이전됐다고 합치지 않는다.** 실제 앱의 동일 계획 인수는 여전히 남아 있다.
- 각 단계 시간은 중복 readback과 검증을 포함한다. 이를 5초 쓰기 정지 SLO 충족이나 무중단 증거로 해석하지 않는다. signed 전략 자격은 아직 발행하지 않았다.

## 05:05 KST — 방향별 실측 자격과 실제 등록 인수

[실측·등록 증거](../evidence/personal-measured-registry-20261120.json)와 [실제 콘솔](../evidence/personal-measured-registry-20261120.png)을 추가했다. 앞의 04:11 시점의 “서명 미발행”은 당시 상태이며, 아래 결과로 갱신한다.

- 순·역방향 모두 실제 catalog v2를 거쳐 15단계를 수행했다. 매 단계 재호출과 새 인스턴스 조회를 확인했다. 테이블 3개·쓰기 경로 2개·prepared transaction 0의 한정된 범위이며, 순방향 150행·복귀 153행을 실측했다. 원래 앱 데이터·경로는 그대로다.
- 현재 네이티브/Security 전체 구현 해시와 시험 코드 해시를 묶어 Keychain의 사전 승인된 검증자 키로 두 방향의 자격과 계약을 서명했다. 실제 PKCE 콘솔/API로 등록 4건·서명 3건을 저장했다. 등록 합계 7건, 실제 앱 계획은 0건이다.
- 실제 원장의 인증된 C3 판정은 두 방향 모두 QUALIFIED_CANDIDATE였다. 실측 행 수 상한을 넘기면 QUALIFICATION_LIMIT_EXCEEDED로 차단했다. C3는 전략 후보 자격이며 실행 승인이나 계약 전체 충족이 아니다.
- 등록 변경의 원자적 outbox, 테넌트별 발행 barrier, 실제 snapshot/strategy 식별자 결속을 추가했다. 실제 PostgreSQL 구성 요소 17개와 컴파일러·변경 영향 단위 36개가 통과했다. barrier는 native registry에만 해당하며 외부 AWS/VM 변경 수집을 대신하지 않는다.
- 워커는 방향별 환경·자격·구현 해시를 확인하고, 읽기 전용 코드 사본으로 실행한다. 실제 기동 7개를 확인했지만 당시 코드 사본 이후 SQL/컴파일러 변경이 있어 최종 실행 전 다시 만든다. 실제 앱 효과는 아직 실행하지 않았다.
- 실제 앱 의존성 수집은 source 컨테이너 2개, target Pod 4개, 양쪽 SQL 역할·권한·식별자와 제한된 앱 코드 해시를 얻었다. 전체 제어 경로·쓰기 경로를 모두 찾았다는 판정과 서명은 아직 하지 않았다.
- OTel 공식 0.165.0 후보도 취약점 차단을 유지한다. 공식 OCB와 검증한 Go 1.27.4로 필요한 upstream 구성 요소만 빌드하는 후보를 준비한다. 검사 통과 전 배포하지 않는다.

남은 핵심 전제는 실제 의존성 범위 판정, 외부 변경 수집, 항목별 데이터 위치 및 실행 검증자 권한이다. 이 전제를 거짓 PASS로 바꾸지 않고 실제 앱 계획→승인→이전→새 쓰기→복귀→브라우저 인수를 이어간다.

## 05:27 KST — 분리된 위치 검증·실제 백업·VM 증인·Collector 후보

- 공통 데이터 검증이 DB 라벨 하나로 백업·감사·AI 위치까지 통과시키지 않도록 수정했다. 컴파일러 v4가 category를 고정하며, 항목별 관측자가 없으면 거부한다. 관련 단위 시험 52개 통과. 현재 개인 catalog의 항목별 관측 연결은 아직 남았다.
- [실제 백업/복원](../evidence/personal-backup-restore-20261120.json): AWS 원본의 표준 pg_dump custom archive 4,546바이트를 실제 VM LUKS 볼륨에 저장하고, 새 DB q_backup_dfb52dbac535에 pg_restore했다. 내용·시퀀스 일치, 양쪽 기존 앱 DB 미변경, 앱 계정의 복원 DB 접근 거부를 확인했다. 첫 시도는 대상 연결 시간 초과로 복원 전에 실패했다. 가동 중 VM에서의 복원이며 VM 소실·원격 DR·장기 보존 자격은 아니다.
- [VM 증인](../evidence/personal-vm-witness-20261120.json): 기존 파일 증인을 암호화 VM 볼륨으로 제공하는 forced-command SSH 경로를 추가했다. 전용 비sudo 계정, 명시한 사용자만 상위 경로 통과, root 소유 불변 코드/공개 authorized_keys, host key 고정, tenant 결속, 만료형 전용 agent를 사용한다. 개인 키는 지정 Keychain 항목과 agent 메모리만 사용한다. 실제 append→새 연결 조회와 다른 tenant 거부를 확인했다. 첫 설치는 상위 디렉터리 권한 때문에 거부됐고 같은 공개키/코드만 명시적으로 재개했다. 아직 실제 계획의 공통 witness 설정을 전환하지 않았고, 파일 증인의 tail 삭제 한계·동일 VM 장애 한계도 유지된다. WORM으로 주장하지 않는다.
- SSH 증인 프로토콜/파일 증인 경계 27개 통과. 별도로 암호화 mapper가 보이지 않으면 파일에 쓰지 않도록 검사한다. 자동 append 재시도로 불확실 결과를 숨기지 않는다.
- [Collector 후보](../evidence/personal-otel-candidate-20261120.json): 공식 OCB의 필요한 upstream 구성 요소만 Go 1.27.4로 빌드했다. 위험한 OpenPGP 패키지가 연결되지 않는지 확인했고, 갱신된 Trivy DB로 이미지 전 심각도 발견 0건이다. 실제 고정 컨테이너의 mTLS·토큰·범위 교체·민감정보 제거 시험 1개가 통과했다. 실제 앱의 텔레메트리와 운영 backend 연결은 아직 남았다. 이전 후보의 BLOCK은 해제하지 않았다.

네이티브 코드가 변경됐으므로 앞의 bd497… 구현 자격을 현 코드의 실행 자격으로 재사용하지 않는다. 현재 코드로 방향별 재측정·새 자격 등록이 필요하다. 실제 앱 계획·작업·효과는 여전히 0이며 원래 트래픽은 AWS를 가리킨다.

## 후속 — 실제 거부 경계 확인

실제 VM 증인에서 [6개 추가 경계](../evidence/personal-vm-witness-boundaries-20261120.json)를 확인했다. 임의 셸 명령은 강제 프로토콜로 대체되고, Kubernetes로의 SSH stdio 포워딩은 administratively prohibited로 거부됐다. 전용 계정은 실제 kubeconfig·업무 pgdata를 읽지 못했다. 다른 암호화 mapper를 준 서버 실행은 파일 생성 전에 거부됐고, 네이티브 LedgerConfig도 같은 실제 기록을 읽었다. 이 시험은 전용 시험 앱 식별자만 사용했으며 실제 업무 authority는 변경하지 않았다.

전체 단위 회귀는 1,221개 통과, 선택 의존성 미설치 22개 제외였다. 첫 실행의 3개 실패는 샌드박스의 loopback bind 거부로 재현됐고, 필요한 로컬 리스너 권한으로 코드·조건을 바꾸지 않고 재실행했다. 제품 오류를 재시도로 숨긴 것이 아니다.


## 후속 — 실제 개인 VPN 통신과 현재 인수 제한

- AWS 소유 EC2와 개인 VM에 표준 WireGuard를 구성했다. 현재 NAT IPv4 하나의 `/32`에서 UDP 51820만 허용하고, 터널 안에서는 원본 앱·DB의 TLS 두 포트만 허용한다. 기본 경로·공개 TCP·IP forwarding은 추가하지 않았다. 양쪽 전용 인터페이스는 기존 시험 기한 10:20 KST에 종료되도록 예약했다. 애플리케이션 데이터와 이전 Service 선택자는 바꾸지 않았다.
- 최초 HAProxy 시작은 프런트엔드/백엔드 이름 중복으로 설정 검사에서 거부됐다. 부분 적용 영수증과 거부된 설정을 보존하고, 소유 키·기존 설정 해시·이미지·보안 그룹을 다시 대조한 뒤 이름만 수정해 시작했다. 불확실한 전체 적용을 재실행하지 않았다.
- [실제 통신 증거](../evidence/personal-wireguard-preflight-20261120.json): 앱·DB 네 경로의 CA/호스트명 검증과 실제 응답, VPN 비허용 포트 3개 거부, 앱 인증·멱등·대상 쓰기 차단 등 기존 14개 검사를 통과했다. 원본 1/1/1행, 대상 0/0/0행이다. 이는 이전 완료가 아니다.
- SSH 증인은 같은 Keychain 키와 기존 기록을 유지하고 서버에서도 시험 기한을 검사하는 코드로 갱신했다. 실제 append/새 연결 조회와 다른 tenant 거부를 다시 확인했다. 관련 단위 시험 22개 통과.
- 프록시 두 개의 이미지/경로 갱신 중, RKE2의 controller-manager와 cloud-controller-manager가 CrashLoopBackOff이며 Deployment의 새 generation을 처리하지 않는 것을 확인했다. 새 소스 Deployment 선언은 저장됐으나 당시 실행 Pod는 이전 이미지였다. 이 부분을 완료로 기록하지 않고 제어기 오류와 VM 자원 상태를 조사 중이다. 이전 trial-06의 Ready endpoint 0건 실패 역시 보존한다.


## 후속 — 재시작 안전 조건·이미지·의존성·비용의 실제 결과

[검증 체크포인트](../evidence/personal-six-stage-checkpoint-20261120.json)는 아래 결과와 원문 해시를 연결한다. 여섯 단계 완료 보고가 아니다.

- 복구: Kubernetes Pod 종료만으로 원격 SQL 요청이 끝났다고 간주하지 않도록 수정했다. 등록된 원격 종료 관측자가 없거나 오류·불명확한 응답이면 복구 증거를 발급하지 않는다. 같은 operation·owner·epoch·pending 지문에 결속하고 만료 시각부터 거부한다. 정상 이전과, 소유자 상실 후 복구를 구분한다. 현재 기본 배포에는 운영 원격 종료 관측자가 없다. 과거 이미지의 Pod 복구 PASS를 현재 자격으로 재사용하지 않는다는 안내를 관련 문서에 추가했다.
- 회귀: 현재 단위 시험 1,264개 통과, 기존 선택 의존성 시험 22개 제외, 기존 Starlette 사용 중단 예고 1건이다. 실제 DB 복구 인수로 합산하지 않는다.
- 공급망: 기존 앱·PG 이미지에도 최신 DB 기준 OpenSSL 3.5.9-r0 관련 MEDIUM 10건씩이 있었다. 수정 버전 r1로 새 이미지를 빌드했고 새 앱 518b6f…와 PG 934224…는 전 심각도 발견 0건이다. proxy 36cbd2… 역시 0건이다. 억제·ignore를 추가하지 않았다.
- 실행 자격: 패치 앱의 전용 Docker 실행 시험은 20초 안에 HTTP 준비가 되지 않아 실패했다. 원인 수집을 위한 두 번째 동일 기준 시험도 실패했다. 캡처 당시 앱 프로세스는 Running, OOM=false였고 시작 로그는 없었다. 전용 컨테이너·볼륨·네트워크는 정리했다. 기준을 늘리거나 실패를 지우지 않았으며, 후보 앱·PG를 기존 실환경에 배포하지 않았다. 자원 압력이 관측됐지만 단일 원인으로 확정하지 않는다.
- 의존성: 실제 source 2컨테이너, target 4Pod, SQL 2서버의 최신 사실을 수집했다. source 역할 30개·target 역할 32개, SECURITY DEFINER 함수와 외부 테이블은 양쪽 0개였다. LOGIN에서 SET ROLE로 접근 가능한 관계도 수집했다. 실제 ID 3개를 기존 DependencySnapshot으로 변환하고 부분 문서를 서명했지만, 쓰기/자동화 완전성은 false와 미확인 경로 5개를 유지한다. 아직 API에 이 문서를 등록하지 않았다.
- Harbor: 공식 0.26.0의 런타임 준비와 별개로, 공개 코드의 알려진 한 줄 결함을 고치는 시험을 준비했다. 에이전트/검증기 이미지를 분리할 컨텍스트를 만들고, 검토한 패치의 바이트와 다른 코드는 실행 전에 거부한다. 5개 검증기 경계 시험 통과. 아직 Harbor agent 실행·실제 모델 호출·제품 승격을 한 것은 아니다. 원본 코드가 실제 공개 저장소의 현재 게시 파일과 같은 SHA임을 확인했다.
- 비용: 개인 RunId에 대한 AWS Cost Explorer 조회에서 비용 배분 태그가 비활성 상태였다. 실제 귀속 비용은 null이며 0원으로 바꾸지 않았다. 비용 수집기와 기존 비용 비교 계약 30개 시험 통과. 로컬 VM 전력·장비·운영·공유 비용은 아직 미확인이고 저렴한 대상 판정은 하지 않는다.

현재 주된 실환경 병목은 로컬 RKE2 제어기의 리더 상실·재시작과 미반영 Deployment 세대다. Mac의 다른 두 kind 시험이 높은 CPU를 쓰는 상태도 관측했다. 다른 세션 자원은 변경하지 않았으며 현재 사용 여부를 질문하고 독립 작업을 계속했다. 실제 앱 계획→승인→이전→새 쓰기→복귀→콘솔 인수는 아직 끝나지 않았다.

## 07:2x KST — 개인 AWS의 패치 후보 런타임 시험

[실제 실행 근거](../evidence/personal-patched-runtime-20261120.json)를 추가했다. 기존 Docker 시험 실패는 보존하고, 별도 호스트인 개인 AWS의 새 containerd namespace에서 같은 앱·PG 후보를 확인했다. 새 자격·새 TLS·새 임시 데이터만 사용하고, 두 컨테이너는 외부 인터페이스 없이 loopback을 공유했다.

- 실제 HTTPS 신원, 무인증 401·다른 tenant 403, 동일 요청의 주문/작업/멱등 결과 각 1건, DB TLS, 관리자 권한 없음, 잘못된 CA/호스트명/비밀번호·비밀번호 없는 평문·다른 DB 거부, 메타데이터 주소로의 통신 불가 등 13개 통과.
- 기존 AWS 앱·DB 컨테이너와 원본 1/1/1 데이터가 그대로임을 전후 대조했다. 시험 컨테이너·임시 비밀 파일·정확한 S3 입력 버전을 정리했다. 후보를 기존 앱에 배포하거나 실제 앱의 이전 계획을 실행하지 않았다.
- 첫 AWS 시험은 버전 객체 조회 권한 부족, 두 번째는 시험 파일 이름 허용 목록 오류로 이미지 실행 전에 실패했다. IAM을 넓히지 않고 조건부 현재 객체 조회의 반환 VersionId·AES256·본문 SHA를 검증하게 고쳤다. 파일은 정규식 확대 대신 정확한 허용 집합으로 제한했다. 두 실패의 원문·정리 영수증을 보존했다.
- OCI 스캔은 런타임이 manifest ID를 반환하는 경우와 config ID를 반환하는 경우를 구분한다. 실제 config blob SHA와 모든 DiffID를 대조하고 기존 원문을 보존한 파생 영수증으로 결속했다. 관련 6개 단위 시험 통과. 파생 시점의 DB 메타데이터를 원래 스캔 시점 관측으로 바꾸어 쓰지 않는다.
- 원격 명령 종료가 불분명하면 정리나 새 시험을 자동 재실행하지 않는다. 확인된 종료·원본 보존·시험 컨테이너 제거가 있어야 비밀 입력을 회수한다. 후보 공급망/런타임 시험 통과는 무중단·금융 규정 준수·실제 이전 완료를 대신하지 않는다.

## 07:30–07:58 KST — 변경 수집·등록 기한·실제 연결 인수

- 실제 AWS/VM의 두 연결 프록시 갱신을 확인했다. 동일한 새 이미지와 WireGuard 설정, 기존 Service UID/resourceVersion/selector 보존을 대조했다. 원래 앱의 경로는 계속 source다. 앞서 실패한 갱신 영수증은 보존했다.
- 별도 전략 시험 07은 실제 EndpointSlice 조회의 BrokenPipe로 중단했다. 원래 앱의 데이터·권한·진입점이 같고 시험 역할들이 NOLOGIN임을 종료 처리에서 확인했다. 성공으로 재분류하지 않았다. Mac의 기본 Lima API와 별도 pinned SSH API를 비교한 시점에는 양쪽 모두 실제 TLS/동일한 읽기 권한으로 5회씩 통과했다. 짧은 관측이며 지속 안정성 자격은 아니다. 새 경로를 다음 시험에 명시적으로 결속한다.
- Kubernetes HTTPS 전송 계층이 등록 기한을 매 요청에 직접 검사하지 않던 부분을 수정했다. GET/PATCH가 자격을 얻기 전에, 자격을 얻은 뒤에, 응답을 읽은 뒤에 기한을 확인한다. 네트워크 대기 한도도 남은 등록 시간으로 줄인다. 실제 loopback TLS의 과거/만료, 자격 획득 중 만료, 응답 도중 만료와 기존 경계 15개가 통과했다. 실제 RKE2의 공격/복구 시험으로 합산하지 않는다.
- [부분 변경 수집 실측](../evidence/personal-partial-change-observers-20261120.json): 새 AWS 시험 DB의 실제 DDL·ACL 차이, 조회 사이에 바뀌었다가 돌아온 DDL, 실제 등록된 개인 EC2의 CloudTrail 이벤트와 중복 조회를 검증했다. 7개 통과, 원래 앱 미변경. 순간적인 복원 변경을 스냅샷으로 검출했다고 표현하지 않는다. 확인한 변경은 기록하지만 완전 수집 watermark는 발행하지 않는다.
- 기존 PostgreSQL·AWS·GCP 조회 수집기가 조회 시각/고정 지연만으로 완전 수집을 선언하던 동작을 제거했다. 관측 37개 단위 시험 통과. AWS/GCP의 미확정 이벤트 보관은 10,000개에서 거부해 무한 메모리 증가를 막는다. 실제 Sova 환경 전체의 완전한 변경 수집은 아직 없다. 따라서 실행 판정의 UNKNOWN을 우회하지 않는다.
- 당시 전체 단위 회귀는 1,276개 통과·선택 의존성 22개 제외·기존 Starlette 경고 1건이었다. 앞선 샌드박스 실행의 loopback 바인드 거부 8개는 필요한 권한에서 동일 코드·시험으로 재실행했다. 이후 비용 추정 0 방어와 관측 상한의 추가 시험도 별도로 통과했다.
- 개인 AWS의 RunId 비용 키를 실제로 활성화했다. 키의 모든 값에 적용되지만 조회는 b0be270e62/개인 계정에 제한한다. 예산·청구 한도를 바꾼 것이 아니다. 실제 첫 응답은 estimated=true, 0 USD였으므로 귀속 반영 전의 불확실한 0으로 처리하도록 수정했다. amount=null/UNKNOWN을 유지하며 무료 또는 절감으로 주장하지 않는다. [AWS 공식 비용 태그 설명](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html)은 태그 표시와 활성화가 각각 지연될 수 있다고 명시한다.
- 공개 코드 전용 Harbor의 agent와 별도 verifier 이미지는 최신 DB의 전 심각도 발견 0건, 개인 불변 ECR의 실제 manifest 및 로컬 runtime digest를 확인했다. 임시 registry 인증 파일은 제거했다. 실제 모델 호출·제품 승격은 아직 없다. Oracle 첫 실행은 분리한 HOME에서 공식 Docker Buildx 플러그인을 찾지 못해 시작 전에 실패했다. CLI exit=0만으로 통과시키지 않고 TrialResult의 예외와 verifier reward를 별도로 확인한다.
- VM 제어기 우선순위를 3분 바꾸려던 시험은 자동 승인 검토가 구체적 승인 부족과 안정성 영향을 이유로 거부했다. 실제 변경은 실행하지 않았고 별도 승인 응답 전에는 보류한다. 다른 세션의 kind 컨테이너도 계속 보존한다.


### 08:17–08:45 KST · 실측 관측·비용의 인증 콘솔 연결

[실제 측정 근거](../evidence/personal-directional-observability-20261120.json) / [첫 실제 화면](../evidence/screenshots/personal-operational-20261120.png)

- 실제 AWS/VM 앱 상태 HTTPS 6회 → 공식 OTel SDK → 검사한 upstream Collector → 별도 mTLS/토큰 수신 → native projection 6건을 확인했다. 게이트웨이 SERVER span이며 앱 내부 계측·전체 트래픽·SLO를 주장하지 않는다. 수집기와 게이트웨이는 시험 뒤 정상 종료했다.
- 앱별 private 0600 파일과 고정 코드 SHA·개인 AWS 계정/RunId를 운영자가 등록하는 읽기 모델을 추가했다. 요청자가 파일·URL·계정·collector를 선택할 수 없다. 감사 조회 권한과 등록된 앱을 먼저 확인하며, 원문 경로·자격은 응답에 없다. 관측은 실행 게이트 증거로 사용하지 않는다.
- 실제 PKCE 로그인 → 실제 VM 제어 DB의 앱 조회 → 관측 화면을 확인했다. 표본 유효기간이 지난 경우 과거 기록을 표시했다. 다른 tenant 403·미등록 앱 404, native mutation 0·계획 0·토큰 영속 저장 없음. API/테스트 IdP와 네트워크 정리를 확인했다. 이 첫 시험은 당시 Git commit만 기록했으므로 작업 중인 전체 API와 콘솔 소스의 완전한 버전 증거로 사용하지 않는다. 후속 시험은 API 소스 복사본과 콘솔 bundle SHA를 별도로 고정한다.
- 개인 RunId 비용은 활성 태그에서도 estimated zero다. 0원·절감·TCO로 해석하지 않았다. 첫 rolling-day 조회는 생성일을 포함하지 않았으므로 원본을 남기고 실제 생성일을 포함하는 04 기록으로 바로잡았다.
- 고정 worker `38bdd57c…`는 실제 기동·권한 거부·정상 종료 7개 통과, 접수·효과는 0개다. 현재 API 코드와 고정 worker의 소스는 다르며, 새 API 코드가 이전 전략 자격으로 검증됐다고 주장하지 않는다.
- K8s HTTPS는 실제 TLS handshake 뒤에도 등록 기한을 다시 검사한다. GET/PATCH에서 만료 후 bearer가 전송되지 않는 실제 loopback TLS 회귀까지 17개 통과. 실제 클러스터 거부 시험과 구분한다.
- 읽기 모델·권한 경계 25개, 콘솔 68개 및 타입/번들 빌드 통과. 단계별 별도 커밋으로 남긴다.

### Harbor 공급자 추가 검사 · 전체 보안 판정은 보류

- Harbor 0.26.0 기본 egress helper 실제 이미지에 UNKNOWN 2 / LOW 33 / MEDIUM 44 / HIGH 44 / CRITICAL 0이 확인됐다. agent/verifier 이미지의 0건 검사만으로 provider 전체가 통과했다고 하지 않는다.
- 공식 GOST v3.3.3 arm64 이미지도 별도 digest로 고정해 검사했으나 UNKNOWN 1 / MEDIUM 8 / HIGH 3이 남았다. 단순 최신 태그 변경으로 해소됐다고 처리하지 않는다. 공식 소스와 보안 수정 의존성의 격리된 재빌드 후보를 준비한다. 운영 provider는 변경하지 않았다.
- 공식 Harbor 기본 Dockerfile: https://github.com/harbor-framework/harbor/blob/main/src/harbor/environments/docker/harbor-docker-egress-control-sidecar/Dockerfile
- 공식 GOST 후보: https://github.com/go-gost/gost/releases/tag/v3.3.3

6단계 전체 완료는 아니다. 완전한 writer/controller 조사·외부 변경 수집 보증, 항목별 위치 관측, 실행 검증자 신뢰 인수, 원래 앱의 native 승인/이전/신규 쓰기/복귀/연속성, 실제 AI 실행과 전체 공급자 격리, 앱 내부 OTel/운영 backend/TCO는 별도로 남아 있다. 모델 호출은 이전 보류 때문에 자동 승인 검토가 거절했고, 현재 일회성 공개 소스 시험 승인 답변을 기다린다. 이를 우회하지 않는다.


### 18:03 KST 재개 · 사용자 보류와 시험 창 구분

사용자는 실제 모델 호출과 제어기 우선순위 변경을 계속 보류했다. 다른 세션 컨테이너의 사용 여부도 미확정이므로 중지·제한하지 않는다. 재개 시각의 CPU는 이전 약 660%와 달리 두 컨테이너 합계 약 81%로 읽혔으며 다른 세션 작업 완료를 의미하지 않는다.

- 이전 10:20 시험 창은 실제 종료됐다. 개인 EC2는 stopped, 기존 암호화 EBS는 그대로였다. 소유 VM은 실행 중이며 샌드박스 안의 Broken 표시는 boot-session sysctl 접근 제한 때문이었다. 외부 읽기 확인에서는 Running이었다.
- 새 창은 **오늘 22:20 KST**까지다. 키·비밀번호·이미지·업무 데이터를 보존하고 새 인증서/앱 만료를 명시했다. source 갱신은 데이터 각 1건 유지, target의 v3→v4 Secret 교체 2개가 성공했다. 실제 원장 계획·작업·효과 0개를 먼저 검사했다. CA 개인 키는 재사용했으므로 키 회전이라고 하지 않는다.
- 새 `caffeinate -di -t28800` PID 6971을 시작했고 화면/잠자기 방지 assertion을 확인했다. 이 assertion은 외부 종료·절전·입력 대기 없이 작업이 계속된다는 보증이 아니다. 이전 만료 후 시간을 무중단 작업 시간으로 계산하지 않는다.
- 옛 VPN은 VM에서 10:20:01에 인터페이스·private key를 정리했고 공개 상태만 남았다. EC2 재기동 후에도 인터페이스·전용 iptables 흔적은 없었고 연결 컨테이너는 stopped였다. 과거 계획과 완전히 일치하는 만료 UDP /32 규칙 1개만 회수하고 0개 readback을 확인했다. 옛 자료·컨테이너·이미지·데이터는 삭제하지 않았다.
- 새 VPN 준비는 만료/제거가 확인된 공개 상태만 별도 보존한다. 시간대별 타이머·연결 컨테이너 이름과 정확한 만료 binding으로 이전 타이머가 새 연결에 작용하지 않도록 한다. 실제 변경 전 새 proposal과 코드 SHA를 다시 고정한다.

### 18:11–18:20 · 실제 Harbor 컨테이너 경계와 별도 verifier

[검증 근거](../evidence/personal-harbor-boundary-20261120.json)

- 공식 GOST v3.3.3 고정 commit에서 Go 1.27.4, x/crypto v0.59.0, x/net v0.63.0을 사용하고 Go 모듈 무결성 검사를 통과했다. 고정 Alpine base와 vendor 보안 버전으로 만든 helper `a53697aa…`는 전체 심각도 0건이다. 코드·go.mod·go.sum·원본 scan의 SHA를 보존했다.
- 설치된 Harbor wheel은 수정하지 않았다. 공개 custom environment 인터페이스에서 고정 helper만 선택하고 커널 probe도 같은 검사된 이미지로 실행한다. private config·scan/hash/DB 기한·실제 이미지/레이어를 재검사한다. public 또는 allowlist로의 확대는 이 개인 자격 범위에서 거부한다.
- 실제 guest UID 10001, effective capabilities 0, no-new-privileges, Docker socket/클라우드·모델 키 부재, loopback TCP positive control, 외부 활성 링크/IPv4 경로 부재를 확인했다. metadata IP와 외부 HTTPS IP로의 실제 TCP 연결은 ENETUNREACH였다. 악성코드 침투시험이나 VM 격리를 수행했다고 하지 않는다.
- 별도 verifier는 검토한 패치만 실행하고 10개 동작 시험 통과, Harbor reward 1.0과 exception 없음까지 확인했다. **Oracle의 정해진 패치 시험이며 실제 AI 모델/에이전트 판정은 아니다.** 운영 승격·앱 패치 배포는 수행하지 않았다.
- 첫 실행의 의존성 경로 누락과, 두 번째 실행에서 DOWN 상태인 커널 터널 placeholder까지 실패로 본 시험 오류를 보존했다. 기존 task 원문은 이전 hash와 대조해 복원했고 새 task 복사본에서 조건을 바로잡았다. CLI exit 0만으로 PASS 처리하지 않았다.

### 19:24 이후 · 재시작 이후 실제 연결과 원장 보존

[실제 갱신 근거](../evidence/personal-window-renewal-20261120.json)

- 개인 EC2 재개 뒤 내부 CNI 브리지에는 의도적으로 host IPv4가 없었다. 이전 시험에서 추가한 host-only 운영 연결 주소와 제한 규칙은 재부팅 후 사라졌다. 컨테이너 정상 종료·기동만으로 해결되지 않은 첫 실패를 보존했다. 전역 라우트나 방화벽 초기화 대신 기존 `source_operator_bridge.py`의 네트워크 ID·두 컨테이너·정확한 subnet/미적용 상태 검사를 재사용해 주소와 발신 차단을 함께 복원했다. DB system ID와 1/1/1행은 보존됐다.
- 실제 AWS/VM 앱·DB TLS 네 경로와 VPN 미허용 세 포트 거부를 재확인했다. 제어 DB의 기존 두 계정에서 28개 migration 이름·SHA, TLS, 비superuser·RLS 우회 불가, 계획·작업·효과 0건을 확인했다. 비밀번호·스키마를 재설정하지 않고 새 private DSN만 만들었다.
- 진입점 두 Deployment의 TLS Secret만 v3→v4로 갱신했다. 원본·대상 backend ConfigMap, 이미지, NetworkPolicy, Service UID/selector를 보존했다. 실제 HTTPS 세 번은 모두 source_orders였고 Secret/다른 Service/다른 namespace 조회는 403이었다. 인증서 갱신을 이전 완료로 세지 않는다.
- 강제 명령 증인은 기존 키·기록을 보존해 재개했다. 실제 기록 추가/조회 외에 셸 대체, API forwarding 거부, kubeconfig·PGDATA 접근 거부, 다른 mapper 거부, 원장에서 같은 기록 조회의 여섯 경계가 통과했다. WORM·VM 장애 생존을 주장하지 않는다.
- 사후 갱신 코드는 내부 bridge 주소가 없을 때에만 기존 검사 도구를 재사용한다. 기존 주소나 방화벽 상태가 예상과 다르면 거부한다. 전체 갱신 shell은 문법/거부 시험이며, 복구 helper 자체와 후속 TLS는 실환경에서 실행했다. 새 전체 갱신 명령을 운영 자격으로 합치지 않는다.
- 소유권·세대·Secret 범위·두 Deployment 사전 검사·다른 bridge 주소 거부 시험 10개 통과. 별도 계약 갱신 시험 3개 통과. 기존 도구 경계 19개와 별개 결과다.
- 기동 전 image 참조 형식 오류와, 기존 Keycloak의 관리자 인증 timeout으로 실패한 실제 브라우저 준비 기록을 보존했다. 후자는 API 미기동이며 실제 승인/등록 성공이 아니다. 해당 실행의 컨테이너·네트워크 정리를 확인했다.
- 사용자가 보류한 실제 모델 호출과 VM nice/우선순위 변경은 수행하지 않았다. 다른 두 세션 컨테이너는 조회만 했다. 현재 EC2·인증서·VPN의 유효기한은 11/20 22:20 KST이고 자동 연장하지 않는다.

### 19:44–19:54 · 실제 신뢰 갱신·방향별 자격·차단 화면과 최신 관측

[실제 readiness 근거](../evidence/personal-readiness-20261120.json) / [당시 콘솔](../evidence/screenshots/personal-readiness-refusal-20261120.png) / [최신 관측 근거](../evidence/personal-fresh-operational-20261120.json) / [최신 관측 화면](../evidence/screenshots/personal-fresh-operational-20261120.png)

- 기존 IdP의 API provisioning은 관리자 인증 또는 realm 생성 응답이 시간 초과됐다. 사전 build만으로 후자까지 해결되지는 않았다. 실패한 POST를 재시도하거나 응답 제한을 늘리지 않고 해당 일회용 IdP를 정리했다.
- 공식 고정 Keycloak 26.8.2 레이어에 외부 네트워크 없이 `kc.sh build`만 추가한 로컬 시험 이미지를 고정했다. 새 이미지의 비root 사용자·원본 레이어를 재검사했다. startup realm import를 명시적으로 선택하며, 무작위 두 계정의 비밀번호 포함 JSON은 stdin으로 소유 컨테이너의 tmpfs에만 쓴다. master 관리자 계정·관리자 토큰은 생성하지 않았다. 실제 PKCE S256·JWT 검증·역할 분리 검사는 유지한다. 기업 SSO/MFA나 운영 IdP 자격은 아니다.
- 실제 두 시험 subject로 검증자 신뢰 revision 2 발행, 자기 승인 403·다른 digest 409·앱 선택 해제 시 이전 승인 제거를 확인했다. 요구사항은 같은 서비스 계약 metadata revision 2를 CAS 등록하고 새 digest에 새 서명을 제출했다. 만료된 문서나 서명의 기한을 수정하지 않았다.
- 실제 분리 DB·분리 Service에서 현재 native/security 코드 `5ad880ee…`의 forward 7·reverse 8, 총 15단계를 통과했다. 매 단계 재호출·새 인스턴스 조회, 대상 신규 쓰기 보존·단일 writer·실제 경로 복귀를 확인했다. 원래 앱 데이터·권한·진입점은 그대로다. 독립된 conformance 문서를 실제 접수 계획으로 세지 않는다. 새 방향별 자격 2개를 서명해 실제 native API로 등록했다.
- 실제 부분 조사 결과도 서명해 등록하고 같은 브라우저에서 계획 생성을 요청했다. 결과는 422/NEEDS_EVIDENCE다. writer·controller·coverage의 세 미완성 코드가 표시됐다. 이 경우 boundary 분석은 snapshot 서명 확인을 생략하고 AUTHENTICATION_REQUIRED도 남기므로, 서명 envelope 제출을 해당 의무 PASS로 세지 않는다. 계획·작업·효과는 후속 실제 조회에서도 모두 0건이다.
- 위 실행을 시작할 때 로드된 이전 harness의 aggregate `strategy_qualification_issued`는 false로 남았다. 실제 두 package 문서·서명·native 등록 영수증과는 다른 보고 코드 누락이며 원본은 수정하지 않았다. 후속 reporter를 바로잡고 근거에 이 한계를 적었다. API의 실제 전체 소스와 콘솔 bundle hash는 별도로 고정해 보존했다.
- 이어서 실제 새 HTTPS 6회 → OTel Collector → 독립 mTLS 수신 → native projection을 수집한 뒤 실제 PKCE 콘솔에 `fresh`로 표시했다. 실제 AWS 생성일부터 현재까지 RunId 귀속 비용은 USD 0.0007146056(추정치)로 읽혔다. 원래 앱 내부 전체 계측·SLO나 모든 청구 비용·로컬 VM TCO·최저 비용 판단은 아니다. 다른 tenant 403·미등록 앱 404·native mutation 0·계획 0·토큰 영속 저장 없음과 API/IdP 정리를 확인했다.
- 공식 실행 방식: [Keycloak 컨테이너 사전 build](https://www.keycloak.org/server/containers), [startup realm import](https://www.keycloak.org/server/importExport).

**현재 완료 경계:** 실제 접속·등록/신뢰/서명·분리 방향별 DB 시험·차단 판정·최신 관측 콘솔까지다. 원래 앱의 승인/실행/복귀가 완료된 E2E는 아니다. 전체 writer/controller/변경 feed·항목별 위치 보증과 실행 검증자 인수가 남아 있다.

### 20:00 이후 · 서버 보류 판정을 운영자가 읽을 수 있는 화면으로 표시

[실제 최신 화면](../evidence/screenshots/personal-actionable-hold-20261120.png)

- 원래 Sova 화면을 유지하면서 raw JSON 대신 writer·설정 변경 경로·조사 누락·미완료 서명 확인을 각각 설명하는 보류 카드를 추가했다. 원래 판정 코드와 추가 근거는 펼쳐서 확인한다. 알 수 없는 새 코드를 통과로 바꾸지 않고, 앱/subject가 바뀌면 이전 범위의 사유를 표시하지 않는다.
- 실제 새 조사·서명 등록·PKCE 브라우저로 다시 확인했다. 결과는 동일한 422/NEEDS_EVIDENCE이고, 읽기 쉬운 보류 이유가 보였다. 등록 head 13개·계획 0개, API/IdP 정리·토큰 비영속을 확인했다. 이 UI 개선으로 게이트 조건을 완화하지 않았다.
- 콘솔 77개·타입 검사·번들 빌드 통과. 임시 IdP 입력의 stdin/경로/역할 경계와 갱신·Harbor·VPN 경계는 별도 38개 통과했다. 서로 다른 버전의 시험을 하나의 전체 PASS 숫자로 합치지 않는다.

### 실제 SQL 수집기 강화 및 거부 시험

[실제 SQL 조사·거부 근거](../evidence/personal-bounded-sql-inventory-20261120.json)

- 양쪽 DB의 catalog 조회를 read-only/repeatable read, statement timeout 5초·lock timeout 1초로 제한하고 실제 세션 설정과 DB system ID를 먼저 대조한다. 역할/함수/권한 결과 건수에도 상한을 둔다. 원격 projection은 시간·출력 byte 상한을 두며 변경 중인 collector 코드는 결과로 내보내지 않는다.
- 역할마다 테이블별 질의를 보내던 방식을 하나의 범위 제한 catalog 질의로 합쳤다. SET ROLE 경로·함수 호출권뿐 아니라 시퀀스 USAGE/UPDATE와 DB 접근권·membership의 inherit/set 옵션을 수집한다. 함수 정의 지문은 SHA-256으로 바꿨다. 실제 소스 36개·대상 38개 역할과 양쪽 실제 TLS를 확인했다. ACL에 권한이 있다는 관측은 실제 로그인·쓰기 성공이나 모든 외부 credential 소유자를 찾았다는 뜻이 아니다.
- 설정 선언만으로 끝내지 않고 기존 별도 conformance DB 두 개에서 실제 영구 DDL/DML 거부(25006), 질의 취소(57014, 약 5초), 잠금 거부(55P03, 약 1초)를 확인했다. 원래 앱 테이블은 잠그거나 수정하지 않았고 주문/작업/멱등 결과의 전후 건수도 같다. 침투시험이나 금융 인증으로 확대하지 않는다.
- 실제 원본에는 postgres와 pg_write_all_data 같은 관리 권한도 관측된다. 이 경로를 실행 제어의 신뢰 경계와 어떻게 묶는지 입증하기 전에는 writer/controller 완전성을 true로 바꾸지 않는다.
- 전체 unit 1342개 통과·22개 조건부 제외. 처음 8개 실패는 sandbox가 loopback 소켓을 거부한 기록이며, 같은 코드의 전체 시험을 필요한 소켓 권한으로 다시 실행한 결과다. 삭제·skip·조건 완화로 통과시키지 않았다. Starlette 의존성 deprecation 경고 1개는 보존했다.

- 추가로 실제 원본의 소유 systemd 단위 6개와 해당 EC2의 SSM association 상태를 수집했다. ExecStart·환경변수·명령 본문은 내보내지 않고 상태와 root 소유 단위 파일 SHA만 남긴다. 첫 실행은 기존 `/run/systemd/transient` 타이머를 허용 경계 밖으로 보고 거부했다. 실제 파일 경로·UID·권한을 별도 조회한 뒤, 이 run의 정확한 unit/파일명과 안전한 권한만 허용해 재검증했다. cron·외부 IAM/세션·모든 제어 동작을 포괄한다고 주장하지 않는다.

### 20:24 · 현재 자격 코드와 실제 호스트 워커의 일치

- 방향별 실제 자격과 같은 `5ad880ee…` 전체 native/security 소스를 새 read-only worker snapshot으로 고정했다. 여섯 DB 연결과 실제 증인 연결을 확인했다. 원래 앱의 작업 접수·효과가 0건일 때만 실제 프로세스를 기동해 권한 거부·health 3회·정상 종료 등 7개 검사를 통과했다.
- 같은 고정 소스의 route loader와 새 설정·토큰으로 실제 Service UID·Ready endpoint·HTTPS 세 응답이 모두 source_orders임을 확인했다. 설정이 로드됐다는 사실만으로 route 권한이 동작했다고 주장하지 않는다.
- 이 signing key는 실행용 신뢰로 승인하지 않았다. 새 호스트의 기동과 읽기 경로 인수이며, 원래 앱의 실행·복구 인수가 아니다. 준비한 Kubernetes 등록/토큰은 10분으로 제한되고 이를 넘겨 재사용하지 않는다.


### 원본 근거 공개 범위와 개인 CI

- 사용자의 2026-11-20 명시 승인으로 실제 AWS/VM 식별정보·시험 주체·태그 비용·스크린샷이 포함된 근거를 공개한다. 원본 증거 파일은 보존된 로컬 commit과 byte 단위로 대조했다. 비밀번호·토큰·개인 키·원본 DSN은 공개하지 않는다. 이전의 공개 보류는 이 승인으로 해제됐으며, 검증 범위나 PASS 조건이 바뀐 것은 아니다.
- 개인 저장소 Actions가 꺼져 있어 신규 CI가 실행되지 않았음을 확인했다. 저장소 CI를 활성화하고 검토한 checkout/setup-python/setup-uv/setup-node의 고정 SHA 네 개만 허용했다. 토큰 기본 권한은 read, PR 승인 권한 없음으로 유지했다. 팀 저장소·자동 배포·모델 호출은 변경하지 않았다. 원격 결과는 실행 완료 뒤 별도로 기록한다.

### 20:43 이후 · 실제 개인 CI에서 발견한 의존성 누락

- 원본 공개 후 첫 원격 실행에서 콘솔은 통과했으나 Python 3.11은 PyMySQL 미설치로 5개 실패했고, matrix의 3.14는 취소됐다. 로컬의 누적 설치 상태와 CI의 producer 전용 설치가 달랐다. 실패 기록을 보존하고 시험을 삭제하거나 새 skip으로 통과시키지 않는다.
- Sova 자체 `uv.lock`과 검증용 dependency group을 추가한다. security 생산자는 기존 계약과 같은 개인 저장소·정확한 SHA로만 해석한다. CI는 dev/service/observability와 producer 시험 의존성을 고정 설치하고, security 코드는 별도 SHA checkout에서 import한다. lock·source manifest·import 대상이 다르면 검사에서 거부한다.
- CI와 같은 uv 0.13.6은 공식 PyPI arm64 wheel의 SHA-256을 대조했다. 로컬 검증은 새 전용 가상환경을 사용하며, 원래 실제 실행의 소스/환경을 덮어쓰지 않는다. 두 Python 버전의 최종 결과는 재실행 후 기록한다.

- 새 lock의 Python 3.12.16 전용 환경에서 전체 단위 **1,365개 통과·제외 0개**, Ruff 통과를 확인했다. 이전 환경의 조건부 제외 22개가 의존성 설치 뒤 실행됐다. Starlette deprecation 경고 1개는 남았다. 이는 이전 실제 브라우저/DB 시험의 의존성을 소급 변경하지 않는다.
- Trivy 0.76.2의 실제 `uv.lock` 분석에서 전체 심각도 발견 0건·비밀값 0건이다. 사용 DB UpdatedAt은 2026-11-19 19:04:59 UTC이며 NextUpdate 전임을 확인했다. 처음 빈 기본 캐시로 거부된 시도는 보존하고, 기존 검증 DB 경로를 명시했다. 원격 Python 3.11·3.14 결과와는 별도다.

### 20:52 이후 · 종류별 실제 저장 경로 관측

[종류별 관측 근거](../evidence/personal-category-placement-20261120.json)

- 기존 DB 라벨만으로 업무 데이터·백업·감사·AI 위치를 합격시키지 않는다. 별도 읽기 전용 수집기가 실제 원본 컨테이너의 DB mount → 호스트 filesystem → NVMe serial → 정확한 개인 EBS attachment와 암호화를 연결했다. 실제 TLS PostgreSQL system ID와 `data_directory`도 양쪽에서 대조했다.
- 목적지는 실제 DB Pod의 volumeMount → PVC → PV claim UID → 소유 VM의 LUKS mapper를 연결했다. 별도 보관 백업과 authority witness는 각각 실제 경로·동일 암호화 장치·내용 SHA를 확인했다. 백업은 기존 archive SHA와 동일하며, 이번 관측으로 복원을 다시 수행했다고 하지 않는다.
- 잘못된 EBS/장치/클러스터/namespace/PVC 상태, 백업 바꿔치기, 감사 기록의 다른 장치, 다른 DB system ID·평문·다른 data directory를 거부하는 단위 시험 21개가 통과했다. 실제 읽기 관측도 완료했다. 코드 SHA와 관측 시각을 보존했다. 앱 데이터·원장·경로·자격은 변경하지 않았다.
- 이 관측은 등록한 세 종류의 자산 연결을 확인한다. 모든 사본·로그 수신처·국가 위치·AI 처리 경로·보관 기간·운영 복구를 증명하지 않는다. `all_copies_enumerated`, `geographic_residency_verified`, `residency_obligation_satisfied`, `migration_authorized`는 모두 false다. 실행 게이트에 PASS로 주입하거나 계약의 allowed zone을 낮추지 않았다.
- 원격 CI의 소비자 lock 커밋 `0d1ab6e`는 Python 3.11·3.14 각각 **1,365개 통과·제외 0개**, 콘솔 **77개·타입·빌드·포맷 통과**다. [실제 CI](https://github.com/sangmu1126/sova-test/actions/runs/44730675419). 새 위치 수집기는 뒤 커밋의 CI에서 따로 확인한다.

- 위치 수집기를 포함한 최종 코드 `bea359a`의 [원격 CI](https://github.com/sangmu1126/sova-test/actions/runs/44731343605)는 Python 3.11·3.14 각각 **1,386개 통과·제외 0개**, 콘솔 77개와 타입·빌드·포맷 통과다. [구조화한 CI 근거](../evidence/personal-locked-ci-20261120.json)에 첫 실패·lock·scanner·환경 구분을 보존했다. native 실행 소스 지문 `5ad880ee…`는 바뀌지 않았으며 CI·위치 수집을 원래 앱의 실제 계획/이전 완료로 세지 않는다.


### 21:44 이후 · 실제 변경 이력 영속 저장과 수집 권한 분리

[구현·권한·실측 상세](personal-change-feed-integration-2026-11-20.md)

CloudTrail의 다중 자원 누락을 수정하고 실제 AWS/시험 DB 변경을 원장에 저장했다. 전용 append-only 역할의 DB 경계 18개와 그 역할의 실제 저장/재시작 인수 12개가 통과했다. 완전성 watermark 발행 권한은 없으며 환경 수집이 불완전한 진단은 UNKNOWN/실행 차단을 유지한다. 원래 앱 계획·작업·효과는 0/0/0이다.

현재 native 소스 지문은 `acc368a2…`이다. 새 신뢰/계약 revision 3 등록 뒤 현재 코드로 방향별 재측정했으나 역방향 경로 readback에서 실패해 새 C3 자격을 발행하지 않았다. 이전 `5ad880ee…` 결과는 역사적 기록이다. 원래 앱 SHA·시퀀스·권한·진입점 보존과 시험 writer NOLOGIN, API/IdP 정리를 확인했다. VM API 상태와 부하는 상세 문서에 기록했다. 22:20 KST 시험 종료는 그대로다.
