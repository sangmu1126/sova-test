# 개인 환경 사용자 흐름 통합 — 2026-11-19

## 범위와 실행 기준

기준은 `5a6d625c719cd1cad18a5a4d779096181662ece3`이다. 기존 보안 보류의 읽기 전용 콘솔 연결 다음으로, 같은 앱·환경·계획을 선택해 등록부터 승인·실행·검증까지 연결한다.

사용자는 팀 개발 종료와 개인 AWS 전환을 지시했다. AWS는 개인 `default` 프로필의 계정 `265233844540`만 사용한다. 팀 SSO 로그인은 중단했다. 팀 저장소·공유 클러스터와 Sky는 변경하지 않는다. 개인 remote에 기능별로 커밋하고 검증 뒤 푸시한다. 다른 세션의 작업 트리는 수정하지 않는다.

## 완료 기준

| 순서 | 범위 | 완료를 인정할 근거 | 현재 상태 |
|---|---|---|---|
| 1 | 같은 앱·환경·계획 선택 | 메뉴 간 같은 서버 계획·digest를 유지하고 잘못된 범위를 거부 | 새 UI 로컬 E2E 통과. 운영 인벤토리 연결 별도 |
| 2 | 등록·계획·독립 승인·접수 | 실제 IdP 로그인, 작성자와 다른 승인자, 기존 API의 서버 권한 검사 | 실제 Keycloak 두 계정 PKCE·자기 승인 403 통과. 조직 SSO 인수 별도 |
| 3 | HTTP 앱·PG 사용자 흐름 | UI에서 접수한 같은 계획이 실제 HTTP·PG 효과와 연결 | 실제 로컬 API·폴링 프로세스·PG·HTTP 흐름 통과. 공유 파일 경로는 fixture |
| 4 | 보류·UNKNOWN·프로세스 사망·안전 복구 | 불확실한 효과 재실행 차단, 신규 데이터 보존, 복귀 근거 | 실제 SIGKILL·원장 보류·재실행 차단·별도 승인 복귀 통과. 원격 settlement/운영 복구 인수 별도 |
| 5 | 개인 AWS → 독립 Linux VM | 실제 실행 위치·신규 쓰기·원본 차단·데이터 대조·복구 기록 | 실제 AWS/VM 앱·DB 기동, 엄격한 TLS·인증·업무 쓰기 사전 시험 14개 통과. 네이티브 계획·승인·데이터 이전·전환 미완료 |
| 6 | AI·데이터 위치·목적지 보호·비용·OTel | 같은 실행에 정책·독립 검증·실제 지표·비용 출처 연결 | 기존 계약 재사용 검토 필요 |

로컬 PostgreSQL의 실제 트랜잭션 시험, 실제 브라우저 로그인, 실제 AWS/VM 배포는 서로 다른 검증이다. 로컬 시험의 등록·자격·신뢰 부트스트랩이나 파일 경로 fixture를 운영 인수로 승격하지 않는다. 화면이 PASS·서명·승인 권한을 만들어 내지 않는다.

## 변경과 검증

- `ea8f47d`: 인증된 등록 목록·동결 계획 조회와 선택적 네이티브 UI 호스팅. 기존 API를 재사용한다. 스트리밍 본문 상한, 안전한 issuer URL, 서명 키 캐시 만료를 보완했다.
- `8252fa1`: 공식 `oidc-client-ts` 3.5.0의 Authorization Code + PKCE를 사용한다. 토큰은 메모리에만 보관한다. 화면의 승인·실행 확인은 앱·계획·digest에 결속된다.
- 최초 API 회귀: 90개 통과, 신규 시험 3개는 공통 `service` fixture 연결 누락으로 수집 실패했다. fixture import를 보완하고 전체 service 회귀를 다시 실행한다. 실패한 실행은 삭제하지 않는다.
- 실제 IdP E2E는 공식 Keycloak 26.8.2 arm64 이미지 `sha256:923a431c08971d49727a857e64870d8d63b5bf294a730e5dde729b5138c26c9e`로 준비한다. 로컬 전용 IdP이며 원격 TLS·기업 MFA·운영 인증 인수의 근거로 사용하지 않는다.

원본 증거는 Git에 넣지 않는 `local-dev-records/sova-user-journey-20261119/`에 보관한다. 비밀값·토큰·개인 키는 문서나 공개 증거에 싣지 않는다. 후속 결과와 남은 제약은 실제 실행 후 갱신한다.

## 고정 코드의 사용자 흐름 검증

`836f1caf7a1eaea9fb428bb6a9230cd9fc12c318`에서 세 브라우저 E2E가 연속 통과했다. 실행 `workspace-journey-06`은 201.990초, source hash 328개를 사후 대조했다. 별도 service 회귀 `workspace-api-final`은 104개 통과했다. 로컬 경계 69개 통과·실서비스 전용 18개 제외, TypeScript 53개 통과와 빌드를 확인했다. IdP·PG 컨테이너와 전용 네트워크 제거를 실제 조회했다.

정상 경로는 논리 요청 3개/전송 4회/503 1회, 복귀 경로는 신규 대상 쓰기를 포함해 논리 요청 4개를 보존했다. 사망 경로는 복사 후 실제 SIGKILL, DB 보안 보류 1개, 원본 쓰기 거부와 후속 워커의 재실행 차단을 관측했다. `service_continuity=NOT_QUALIFIED`, `operational_qualification=false`를 유지한다.

앞선 `workspace-journey-05`에서는 정상 경로의 세션 관측 연결이 `ConnectionTimeout`으로 실패하고 리스를 잃었다. 다른 두 사례는 통과했다. 원인은 확정하지 않았고 한도·타임아웃을 늘리거나 자동 재시도해 통과시키지 않았다. 이 실패를 보존하며, 후속 독립 실행 통과만으로 원인 해결·안정성 자격을 선언하지 않는다. 최초 fixture 연결·Docker 네트워크·접근성 실패도 [증거 요약](../evidence/workspace-user-journey-20261119.json)에 남긴다.

## 개인 물리 경로 준비

- 팀 SSO와 팀 tailnet은 사용하지 않는다. 사용자의 질문에 따라 개인 AWS→이 Mac의 VM에는 Tailscale이 필수가 아님을 설명했고, SSM 인증 터널과 로컬 VM 전달 경로로 준비한다.
- 기존 `sova-target`은 조회 후 원래의 정지 상태로 복원했다. 실제 목적지는 별도 `sova-rke2-target`이다. ARM64 RKE2 v1.35.9+rke2r1, 클러스터 UID `9de35591-57a3-4759-bd73-6340e2de0b61`과 VM 마커·machine ID를 대조했다. 호스트 공유 mount가 없으며 기존 namespace·앱·권한은 변경하지 않았다.
- Terraform을 실제 적용해 새 자원 18개를 생성했다. 기존 자원 변경·삭제는 0개다. 계정 `265233844540`, 실행 `b0be270e62`, 인스턴스 `i-04946f0e0c0548c3b`다. 수신 규칙 없음, 발신 443, IMDSv2 hop-limit 1, 암호화·보존 EBS, 비공개 버킷·불변 ECR을 조회했다. SSM Online과 ARM64, 정지 타이머의 절대 시각 `2026-11-19 05:46:31 UTC`를 실제 확인했다. 14:46:31 KST이며 청구액 상한은 아니다.
- 최초 SSM 부팅은 새 역할의 자격을 읽지 못했다. 역할·정책·프로필 연결을 조회하고 아직 앱·DB가 없는 전용 인스턴스만 재부팅한 뒤 Online을 확인했다. IAM·네트워크 범위는 넓히지 않았다. Terraform에도 정책·경로 연결 뒤 인스턴스를 시작하도록 순서를 명시했다. 최초 오류의 모든 원인을 재부팅 성공만으로 확정하지 않는다.
- 공식 nerdctl-full 2.4.1 ARM64의 GitHub 자산 digest·archive 해시를 대조해 표준 containerd·nerdctl·runc·필요한 CNI만 설치했다. BuildKit은 설치하지 않았다. source 마커·개인 계정·타이머를 먼저 검사하고 기존 runtime은 덮어쓰지 않는다. 앱·DB 물리 배포는 별도다.
- 서울 리전 Bedrock의 Amazon 텍스트 모델 목록은 실제 조회했으며, 조회된 채팅 모델은 `INFERENCE_PROFILE` 방식이었다. 이 사실만으로 한국 내 처리나 모든 데이터 단계의 위치를 인증하지 않는다. 실제 profile 대상 리전·로그·보존 근거를 검증하기 전 제한 데이터의 호출은 허용하지 않는다.

남은 순서는 실제 canary 앱 산출물·개인 클라우드/VM 준비 → 실제 등록·자격 → 같은 네이티브 사용자 흐름의 물리 경로 인수 → AI·목적지 보호·비용·OTel 연결이다. 작업을 계속하며 팀·다른 작업 트리에는 반영하지 않는다.

## 같은 산출물의 이미지·런타임 인수

로컬 서비스 회귀는 `a22c59b` 기준 124개 통과, 소스 해시 49개 대조, clock 검사·전용 컨테이너/네트워크 정리 확인이다. [회귀 증거](../evidence/canary-service-regression-20261119.json)는 AWS/VM 이전 성공과 구분한다.

이미지는 처음부터 취약점 0건이 아니었다. 첫 Python slim 이미지 HIGH 103·CRITICAL 3, 새 slim 패치 HIGH 48, Alpine 기반 HIGH 16을 발견했다. 필터나 예외 없이 기반 이미지 교체·정확한 vendor 패치·불필요한 pip 제거로 보완했다. PostgreSQL upstream도 사용하지 않는 gosu의 오래된 Go 코드 때문에 차단됐다. 항상 UID70으로 시작하도록 바꾸고 해당 도구를 실제 제거했다. [각 산출물·검사 기록](../evidence/canary-image-preflight-20261119.json)에 실패와 최종 결과를 함께 남긴다.

- 최종 앱 manifest: `sha256:f80bc08328b0f962151b949706589bd5db3a12803dfa3096fb700bd3fad5e208`, UID/GID 10001.
- 최종 DB manifest: `sha256:01f80835db480f8403d638051eff0ace599264a908bad70a214b8952173363c5`, UID/GID 70.
- 두 산출물 모두 Trivy 0.76.2의 모든 심각도·전체 패키지 검사에서 발견 0건이다. 이 로컬 preflight는 registry에 결속된 정식 보안 admission이나 운영 자격을 대신하지 않는다.
- 같은 실제 앱·DB 산출물로 13개 검사를 통과했다. HTTP 주문·재전송·조회, 업무 효과 1회, 정상 TLS/SCRAM, 잘못된 CA·호스트명·비밀번호/평문/타 DB 거부, 예상하지 않은 역할 멤버십과 NOLOGIN 차단, 비루트·읽기 전용 앱·capability 제거를 확인했다. [런타임 증거](../evidence/canary-runtime-acceptance-20261119.json)는 AWS/VM 이전 증거가 아니다.
- 첫 인수는 제한된 초기화 도우미에서 실패했다. 두 번째는 임시 초기화 서버의 socket 준비를 실제 TCP 준비로 잘못 읽어 SQL이 종료 시점과 겹쳤다. 실제 TCP 서버를 기다리도록 고친 세 번째 실행이 통과했다. 단순 sleep·자동 재시도로 통과시키지 않았고 실패와 자원 정리 기록을 보존했다.
- 물리 source/target용으로 DB별 다른 SCRAM 비밀번호와 TLS leaf를 준비했다. CA 개인 키는 Mac에만 둔다. source 전용 Secret과 정확한 OCI 산출물을 전달해 앱·DB를 실제 기동했다. 네이티브 등록·승인·이전은 아직 별도다.

## 실제 개인 AWS·VM 사전 시험 — 이전 완료와 구분

전용 목적지 namespace는 `sj-b0be270e62`, UID는 `565b91c4-4f6e-4d06-94c6-9f8803afa6c9`다. 새 19개 Kubernetes 자원만 생성했고 기존 namespace·앱은 변경하지 않았다. 두 워크로드는 비루트·읽기 전용 rootfs·capability 제거·자원 상한을 사용한다. NetworkPolicy 선언만으로 실제 통신 거부를 PASS 처리하지 않는다.

이 Mac의 FileVault는 꺼져 있었다. 전체 Mac 설정을 바꾸지 않고 목적지용 새 파일 한 개에 LUKS2 AES-XTS 512비트/Argon2id 볼륨을 준비했다. 포맷 전 새 backing file과 mapping을 대조했고 잘못된 키 거부를 확인했다. 키는 표준 `keyring` 25.7.0의 macOS Keychain에만 저장·재조회했다. VM 디스크나 평문 키 파일·AWS에는 남기지 않는다. 이는 VM 데이터 볼륨의 통제이며, 개발 PC·제어면의 모든 비밀정보가 저장 시 보호됐다는 주장은 아니다. PVC는 Retain이고 DB 시작은 암호화 볼륨의 실행 마커를 확인한다. 재부팅 후 unlock·운영 복구 인수는 별도다.

실제 검사에서 두 설정 문제를 수정했다.

- 내부 CNI bridge에는 호스트 IPv4 경로가 없어 SSM에서 앱·DB에 닿지 않았다. 해당 bridge의 호스트 주소만 추가하고, 컨테이너가 새로 시작하는 host-directed 요청 및 외부 forwarding을 먼저 차단했다. 공개 수신 포트·컨테이너 default route는 추가하지 않았다. 방화벽·경로는 재부팅 후 재인수가 필요하다.
- 새 인증서에 엄격한 X509 검증에 필요한 식별자 확장이 빠져 있었다. 검증을 끄지 않고 SKI·AKI를 추가해 새 입력 revision으로 재발급했다. 키·비밀번호·원래 만료 시각은 유지했다. source의 인증서만 원자 교체하고, 데이터 0건·목적지 writer NOLOGIN을 확인한 뒤 전용 워크로드의 새 불변 Secret 참조만 CAS 변경했다. 기본 HTTP 헬스체크도 실제 HTTPS·신원 검증으로 고쳤다.

[실제 사전 시험 14개 증거](../evidence/personal-aws-vm-preflight-20261119.json): 양쪽 DB의 다른 system identifier와 TLS, 앱 HTTPS의 엄격한 CA·호스트명 검증, writer 권한·초기 활성 상태, 무인증 요청 거부, 출발지 주문·재요청·조회, 잘못된 DB 호스트명·평문 시작 거부를 확인했다. 동일 키 두 번 요청의 업무 효과는 source `(1,1,1)`, target `(0,0,0)`이다. target의 NOLOGIN과 HTTP 503을 함께 관측했지만, 실제 활성화 후 정상 쓰기는 아직 양성 대조군으로 남아 있다.

연결은 개인 `default`의 AWS SSM과 local VM의 호스트 키를 고정한 SSH다. Tailscale이나 공개 앱·DB ingress 없이 Mac loopback 네 개를 사용한다. 최초 listener 준비를 실제 목적지 준비로 오인하지 않았고, 실패 실행은 timeout·인증서 오류로 보존했다. 플러그인 누락 실패의 소유 프로세스를 정리하고 AWS 서명·공증·ARM64·버전이 확인된 Session Manager plugin 1.2.896.0을 명시했다.

다음은 표준 HAProxy의 안정 진입점 → 실제 자원에 결속된 등록·자격 → 같은 네이티브 API/계획·독립 승인·워커 실행 → 데이터·새 쓰기·복귀 검증이다. 현재 `migration_complete=false`, `operational_qualification=false`, 무중단 미자격을 유지한다. 실제 위치 대신 DB 주석만 읽는 기존 placement probe를 운영 위치 증거로 승격하지 않는다. 원격 효과 settlement와 제한된 복원 시험의 운영 한계도 유지한다.

### 개인 AWS → VM 실제 진입점과 호스트 워커 준비

- 전용 네임스페이스의 HAProxy 3.4.8 비루트 이미지 두 실행본을 사용한다. 진입점은 아직 AWS source만 가리킨다. 재시도는 0이며 앱의 side/digest 헤더를 프록시가 만들지 않는다.
- 로컬 SSH 고정 연결을 거쳐 실제 진입점의 HTTPS `/healthz`가 200을 반환했다. 엄격한 X509 체인·호스트명 검증과 앱이 반환한 source 및 고정 digest를 확인했다. 이것은 데이터 이전·전환 성공 근거가 아니다.
- 준비 중 기존 SSM 세션 두 개가 유휴 만료된 것을 관측했다. 원래 개인 EC2·포트만 재연결한 뒤 HAProxy source 준비 상태가 돌아왔고 실제 HTTPS 응답을 검증했다. 터널 수명도 실행 전 재검증 대상이다.
- 네이티브 `run-worker`에 명시적인 `SOVA_WORKER_IDENTITY=host` 구성을 추가했다. 실제 PID와 시작별 nonce를 사용하며 Pod 신원을 함께 주면 거부한다. 기존 Kubernetes 기본값은 유지한다.
- 호스트 워커는 Kubernetes API를 조회하지 않고 quiescence 제공자를 두지 않는다. 로컬 프로세스 종료만으로 원격 SQL 완료를 주장하거나 불명확한 효과를 자동 재실행할 수 없다. 상태 조회용 health listener는 loopback에만 바인딩한다.
- 관련 설정·시작·서비스 시험: **73 통과, 실환경 조건부 18 제외**, Ruff 통과. 첫 실행의 5건 실패는 시험 실행의 PyMySQL 경로 누락이었으며, 기존 격리 의존 경로를 포함해 재실행했다. 제외를 실환경 통과로 합산하지 않는다.
- 남은 범위: 실제 개인 환경의 원장·등록·독립 승인·자격 프로필·증거 수집·워커 설정, 데이터 권한 전환과 반대 방향 복귀. 기존 로컬 fixture 신뢰 등록·watermark를 개인 AWS 실측으로 바꾸어 표시하지 않는다.
- 특히 현재 PG의 DB comment 기반 zone 확인은 실제 위치 증명이 아니다. 이를 근거로 사업 데이터·백업·감사 기록 위치를 모두 통과시켜서는 안 된다. 확인되지 않은 위치 통제는 차단 상태를 유지한다.

#### 네이티브 실제 경로 조회와 권한 거부

기준 `6749cf7`의 `observe_entry.py`로 실제 개인 VM의 API CA·namespace UID·Service UID를 대조했다. 메모리에서만 사용하는 10분짜리 전용 ServiceAccount 토큰으로 네이티브 Service 경로를 읽고 실제 HTTPS 요청 3회의 side와 비교했다. 모두 `source_orders`였다. Secret 읽기·다른 Service 읽기·다른 namespace 조회는 실제 403으로 거부됐다. [공개 검증 결과](../evidence/personal-native-entry-20261119.json).

이 검증에서는 경로·SQL·앱 데이터·기존 정책을 변경하지 않았다. Service 경로의 선택적 실제 HTTP observer를 추가했으며, 다른 side·HTTP 503·외부 Endpoint가 포함된 경우 성공으로 판단하지 않는 회귀 시험을 포함했다. 관련 99개 통과, 실환경 조건부 18개 제외. 최초 sandbox 실행의 loopback bind 실패는 권한이 필요한 동일 시험을 승인된 환경에서 재실행해 확인했다. `migration_complete=false`와 `operational_qualification=false`는 유지한다.

### 개인 네이티브 등록·워커·실측 후속 결과

- `6b64990` 기준 실제 개인 AWS·VM PG 17.12를 다시 관측하고, 실제 브라우저 Authorization Code + PKCE(S256)로 앱 계약·출발 프로필·목적지 프로필을 네이티브 API에 등록했다. VM 암호화 볼륨 위 별도 제어 DB에 등록 3건, 계획 0건을 확인했다. 토큰은 브라우저 영속 저장소에 남지 않았다. [결과](../evidence/personal-native-registration-20261119.json) · [실제 화면](../evidence/personal-native-registration-20261119.png).
- 이 IdP는 실제 Keycloak을 실행한 **로컬 시험용 IdP**다. 기업 SSO·MFA·실제 두 사람의 승인을 증명하지 않는다. 두 번의 100초 시작 실패는 보존했다. 초기 Quarkus 빌드가 약 135초 걸린 로그를 확보한 뒤 별도 300초 콜드 시작 예산에서 로그인까지 확인했다. 앱 이전 SLO를 늘린 결과가 아니다. 시험 API·IdP 컨테이너는 종료·정리했다.
- 어댑터·의존성 조사·방향별 자격이 없는 요청은 실제 네이티브 컴파일러가 `422 / Nothing registered as capability_manifest not-qualified`로 거부했다. 일반 404를 성공적인 보안 거부로 셈하지 않는다. 신뢰 등록·서명·watermark·자격을 fixture로 주입하지 않았다.
- `35c6b35` 기준 실제 네이티브 호스트 워커와 VM 원장을 연결했다. 실제 PID 기반 소유자, health 3회, 안전 종료, 승인/효과 0건 유지를 확인했다. 런타임의 이력 삭제·마이그레이션 기록 추가·스키마 생성·업무 데이터 읽기는 **실제 SQLSTATE 42501**로 거부됐다. [검증 결과](../evidence/personal-native-host-worker-20261119.json).
- 준비 과정의 slotted 정본 객체 직렬화 오류와 `offline` 모드 오기는 실제 시작 전에 거부됐다. 각각 `asdict`, 정본 `offline_copy` 및 실제 catalog parser 사전 검증으로 수정했고 실패 출력과 새 초안 리비전을 보존했다. 승인된 계획·업무 데이터·경로를 수정한 것은 아니다.
- API의 OIDC 설정과 워커의 원장·증거 신원을 분리했다. 워커가 실제 사용하지 않는 가짜 IdP 주소를 요구하지 않으며, API는 여전히 실제 OIDC 설정 검증을 요구한다.
- `a75e486`에서 선택적으로 Service UID를 고정하고, PATCH가 UID와 resourceVersion을 함께 검사하도록 했다. 자원이 같은 이름으로 재생성되거나 HTTP 표본 수집 중 바뀌면 성공으로 인정하지 않는다. 개인 워커 설정은 이 UID를 포함한다. **교체 자원·CAS 회귀는 단위 시험으로 검증했으며, 실제 잘못된 UID PATCH 시험은 자격 만료로 접속 전에 차단되어 미실행**이다. 앞선 실제 경로 조회 3회·403 검증과 합산해 live mutation guard PASS로 표시하지 않는다.
- 최종 관련 경계 회귀: **169 통과, 실환경 조건부 18 제외**, Ruff 통과. 새 설정 분리 이후 기업 로그인·전체 데이터 전환을 다시 실측한 것으로 표시하지 않는다.

### 6번: 비용·AI·관측의 실제 확인과 미완료

[개인 환경 관측](../evidence/personal-environment-20261119.json)에서 공식 AWS Price List의 서울 `t4g.small` Linux/Shared 단가는 시간당 USD 0.0208이었다. 청구액이 아니며 저장소·네트워크·백업·운영·공유 비용과 로컬 VM TCO는 미확인이다. 누락을 0으로 채우거나 더 저렴한 목적지로 판정하지 않는다.

Bedrock `apac.amazon.nova-lite-v1:0`의 실제 모델 리전은 서울·도쿄·오사카·뭄바이·싱가포르·시드니를 포함했다. 계정 호출 로깅 미설정은 공급자 보관·지원 접근 위치가 없다는 근거가 아니다. 모델 호출은 0회이며 위치 정책 승인은 미완료다. Harbor는 실제 실행 환경에 연결하지 않았다.

기존 Collector 0.164.0은 HIGH 2·UNKNOWN 1·MEDIUM 2를 발견했다. [공식 0.165.0 릴리스](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.165.0)의 정확한 ARM64 digest 후보도 검사했으나 HIGH 0·UNKNOWN 1·MEDIUM 1이 남았다. 두 이미지 모두 **배포 승인 BLOCK**이다. 무시 목록·심각도 제외·임의 예외를 넣지 않았고 새 Collector를 기동하지 않았다. 현재 앱에 실제 OTel SDK/Collector가 연결됐다는 근거가 아니다. [검사 기록](../evidence/collector-preflight-20261119.json).

### 17:31 KST 시점과 남은 실제 경로

[시험 창 종료 확인](../evidence/personal-trial-window-closed-20261119.json): 개인 AWS source는 자동 중지됐고 EBS는 연결·보존 상태이며 삭제 설정은 꺼져 있다. 원래 자격 만료를 연장하거나 검사 우회를 하지 않았다. 이미지·실제 앱·PG 연결·진입점·콘솔 등록·원장·워커 기동 근거는 해당 관측 시점의 기록이다. 현재 서비스가 가동 중이거나 데이터 이전이 끝났다는 뜻이 아니다.

다음 준비 범위는 (1) 실제 source/target 프로필에 대한 방향별 기능 자격 및 검증자 신뢰 등록, (2) 모든 writer/controller를 포함한 현재 의존성·변경 수집, (3) 사업 데이터·백업·감사별 검증된 위치 근거, (4) 독립 승인과 네이티브 원장/워커를 통한 실제 권한·데이터·경로 전환 및 신규 쓰기·복귀, (5) 자격을 갖춘 관측 이미지·실제 AI/Harbor·완전한 비용 근거다. DB comment, 테스트용 신뢰·계정, 성공한 health만으로 이 빈칸을 채우지 않는다. **여섯 단계 전체는 미완료**다.
