# platform 대조표 (2026-11-19)

팀 platform 저장소의 과제(T1~T50)와 통제 20개를, MIG-SPEC v1.1과 이 저장소(sova-test)의 현재 구현에 하나씩 맞춘다. 무엇이 이미 대체됐고, 무엇이 결과로만 연결돼 있고, 무엇이 비어 있는지를 정해 흡수 순서를 고르기 위한 표다.

기준:

- platform: 팀 main `d569817e`(2026-11-13). 서비스 22개, 인프라 환경 40개. 과제 설명은 2026-11-10 작업 목록 사본.
- security: sova-test가 고정해 쓰는 `27e6e85`(보호 수준 비교, 서명 증거, 팀 통제 결과 수용 등).
- sova-test: `feature/contract-driven-foundation` `a3224b0`.
- 근거는 코드·문서에서 확인한 것만 적었다. 기능의 깊이(시험 범위)는 각 행의 근거 칸에 적은 만큼이다.
- **갱신 2026-11-20.** 흡수 1순위(목적지 보안 통제를 계약 의무로)를 반영했다. 바뀐 칸에는 "(11-20)"을 붙였다. 경위는 [개발 기록](contract-foundation-2026-11-20.md)에 있다.

## 상태 구분

| 상태 | 뜻 |
|---|---|
| 대체·확장 | sova-test가 같은 책임을 일반형으로 구현했다. 대상이 더 넓다 |
| 대체 | 같은 책임을 다른 방식이나 다른 공급자로 구현했다 |
| 결과 수용 | platform이 만든 결과를 security 패키지가 서명 검증해 받아 판정한다. 결과를 만드는 쪽은 sova-test에 없다 |
| 일부 | 책임의 일부만 있다(근거 칸에 무엇이 있고 무엇이 없는지) |
| 없음 | 제품 범위인데 sova-test에 없다 |
| 참조 기술 | 사양 §30의 참조 기술(실행 환경, IaC, 트래픽 장비, 격리 기술). 목적지·플랫폼 환경을 세우는 일이라 제품 핵심이 아니다. 시험 환경은 sova-test의 자체 스위치(`infra/test-env`, `infra/cloud-env`)가 맡는다 |
| 팀 운영·데모 | 팀 저장소 운영이나 해커톤 데모 전용. 대상 아님 |

## 요약

| 상태 | 과제 |
|---|---|
| 대체·확장 | T8, T10, T12, T13, T20, T25, T27, T47 |
| 대체 | T17, T19, T32, T37 |
| 결과 수용 | T5, T14(+일부), T33 |
| 일부 | T3, T9, T11, T15, T16, T18, T21, T24, T26, T31, T34, T35, T36, T46 |
| 없음 | T23, T42, T45 |
| 참조 기술 | T1, T2, T6, T7, T30, T43, T44, T48, T50 |
| 팀 운영·데모 | T4, T22, T28, T29, T38, T39, T40, T41, T49 |

이전을 실행하는 쪽(인계·데이터·경로·복귀·탈출·ID)은 sova-test가 platform보다 넓다. 비어 있는 것은 주로 **목적지 환경의 보안 통제를 직접 재는 쪽**(T14 시험기의 통제 대부분, T9의 무인증 거부)과 **AI 수정·리허설 실행 쪽**(T11, T15, T42)이다.

## 표 1. 과제별 대조

| 과제 | platform | 사양 | sova-test | 근거·빈칸 |
|---|---|---|---|---|
| T1 AWS 기반(권한·VPC·비용 태그) | `infra/envs/aws-foundation`, `cost-aws` | §26 | 참조 기술 | 시험용 AWS 자원은 `cloud-env` 임대 |
| T2 온프렘 RKE2(CIS) | `infra/envs/onprem`, `infra/shared/rke2` | §30 | 참조 기술 | |
| T3 리허설 namespace 격리 실측 | `services/rehearsal-runner` | §21.2, SEC-05 | 일부 | 자격 없는 sandbox 요청 모델(`domain/sandbox`)만. 격리 템플릿·격리 실측 없음 |
| T4 데모 앱 v0 | demo-chat 저장소 | - | 팀 운영·데모 | 시험 앱은 자체(게시판·게임 서버) |
| T5 통제 6개 설계 | `docs/controls` | §22.1 | 결과 수용 | security `team_controls`가 T5 프로필을 등록하고 ControlRun을 서명 수용 |
| T6 출발지 EKS·CNPG·PgBouncer·ESO | `infra/envs/source-*` | §30 | 참조 기술 | 시험용 EKS·RDS·Cloud SQL은 `cloud-env` |
| T7 플랫폼 클러스터·Actions OIDC | `infra/envs/platform-aws`, `platform-data` | §26 | 참조 기술 | sova-test 배포는 `deploy/kubernetes` |
| T8 복제 경로(논리 복제·첨부 동기화) | `operations/subscription`, `infra/envs/attachment-copy-reader` | §16 | 대체·확장 | PostgreSQL 논리 복제·객체 복사에 더해 MySQL·Kafka API·RabbitMQ·Redis 계열·MongoDB·Kubernetes 볼륨 |
| T9 OpenBao·ESO·SeaweedFS, 무인증 거부 | `infra/envs/target-workloads`, `target-secrets` | §22, §30 | 일부 | 비밀 이전은 Secrets Manager → Kubernetes Secret만(OpenBao 없음). 목적지 구성은 참조 기술. (11-20) 목적지 객체 저장소의 무인증·다른 tenant 거부를 `ObjectStoreProbes`가 잼. 팀 목적지와 같은 SeaweedFS 4.52를 기본값·잠금 두 상태로 실측 |
| T10 상태 머신·API·이벤트 | `services/platform-events` | §14, §24 | 대체·확장 | 계획·승인·실행권·효과 원장·워커·API. SSE 이벤트 스트림 없음 |
| T11 AI 수정 제안 | `services/agent-worker` | §21.1 | 일부 | security `ai_gateway`(데이터 위치 정책, 호출 경계)만. 제안서 생성 없음 |
| T12 Cutover Executor | `services/cutover-executor` | §15 | 대체·확장 | 저장소 10여 종과 Kubernetes 볼륨·세션의 인계, AWS·GCP·EKS·GKE 실측 |
| T13 무결성 비교·영수증 | `services/cutover-executor`(ADR-0013) | §16, §23 | 대체·확장 | 저장소별 `content_equal` probe, 행 digest, 서명 증거 |
| T14 통제 시험기 | `services/control-runner` | §22, §23 | 결과 수용 + 일부 | 결과는 security가 수용. 직접 재는 것은 단위별 probe(표 2). (11-20) 팀 시험기의 판정 방법(양성 대조로 감싼 측정)을 가져와 IAM-01.1, ISO-01.1(객체), ENC-01.1~01.3, SEC-01.1, NET-01.3을 단위 probe와 계약 의무로 둠 |
| T15 리허설 러너 | `services/rehearsal-runner` | §21.2~21.3 | 일부 | Harbor 결과 수용 경계(`sandbox_admission`)만. 리허설 실행 없음 |
| T16 의존성·설정 인벤토리 | `services/inventory` | §9, §10 | 일부 | Kubernetes 조사, 선언 의존성 → 의무, CloudTrail·GCP 감사 로그 변경 수집. 코드·IaC 정적 탐지 없음 |
| T17 Edge(Cloudflare 가중치) | `services/edge-control`, `public-entry`, `infra/envs/edge-*` | §18 | 대체 | Service·Gateway API·ALB·URL map·Route 53·Cloud DNS. Cloudflare 없음 |
| T18 부하 생성기·관측 | `services/loadgen` | §27, §28 | 일부 | 시험 안의 쓰기·쓰기 정지 실측(`tests/perf`). 부하 생성기 없음 |
| T19 원본 대조 게이트 | `services/control-gate` | §22.1 | 대체 | security 보호 수준 비교와 실행 전 게이트(다르거나 UNKNOWN이면 차단) |
| T20 failback | `services/cutover-executor` | §19 | 대체·확장 | failback 계획(§15.4·§19.3), adapter의 복귀 확인 |
| T21 콘솔 | `services/console`, `sova-mcp` | §27 | 일부 | `console/`은 팀 main(11-09) 것 그대로. 이 브랜치에서는 README 두 줄만 바뀜 |
| T22 데모 앱 v1 | demo-chat | - | 팀 운영·데모 | |
| T23 규제 매핑표 | `docs` | §22 | 없음 | |
| T24 감사 로그(S3 Object Lock) | `services/audit-log`, `infra/envs/platform-audit` | §23, §25, SEC-07 | 일부 | 효과 원장·서명 증거(위변조·재생 검출). 변경 불가 저장소 없음 |
| T25 반복 측정·장애 주입·SAFE_HOLD | T12 시험 | §28 | 대체·확장 | 경로·크기별 5회 분포, 결함 주입, SAFE_HOLD 시험, 전체 회귀 |
| T26 Harbor 평가 | - | §30 | 일부 | security Harbor 요청·서명 검증, `sandbox_admission` |
| T27 Exit Drill | `services/exit-drill` | §20 | 대체·확장 | `exit_readiness`, `platform_lockout`, AWS 전체 탈출 실측 |
| T28 데모 각본·reset | - | - | 팀 운영·데모 | `platform_t28` 포트 연결만 |
| T29 ADR·설계 문서 | `docs` | §31 | 팀 운영·데모 | sova-test는 자체 ADR·개발 로그 |
| T30 목적지 워크로드 | `infra/envs/target-workloads` | §30 | 참조 기술 | |
| T31 승인 반영 경로 | `services/apply-path` | §21, Part III | 일부 | Kubernetes 이미지 패치(승인된 digest 적용), security 패치 실행. 버킷 정책 반영 없음 |
| T32 이전 대상 추천 | `services/target-recommend` | §12.3~12.4 | 대체 | `plan_search`(충족·보완 필요·불가, 순위 규칙), 비용 조언. AI 추천 문안 없음 |
| T33 보안 탐지 통합 | `services/security-findings` | §23 | 결과 수용 | security `team_detection`(T33 OCSF 입력), Prowler OCSF. 수집기(Security Hub·SCC·Falco·Kyverno) 없음 |
| T34 감사 로그 수집 | `infra/envs/*` | SEC-07 | 일부 | CloudTrail·GCP 감사 로그는 변경 감지용. 감사 이벤트 검색 통제(AUD-01) 없음 |
| T35 CNPG 백업·복원 | `infra/envs/target-backup`, `source-backup` | SEC-08 | 일부 | 저장소별 `restore_drill`. 백업 구성과 삭제 제한은 없음 |
| T36 판정용 관측 계약 | `services/observability`, `docs/observability` | §18, §27 | 일부 | span 정규화(`telemetry`). SLI 창·SHIFT/WATCH 판정 없음 |
| T37 환경 횡단 지표 | `services/observability` | §27 | 대체 | Collector 구성 생성(`otel_collector`), OTLP 투영 |
| T38 구독 런타임 | `infra/envs/subscription-runtime` | - | 팀 운영·데모 | |
| T39 비밀 비노출 진단 | - | - | 팀 운영·데모 | 원칙은 sova-test에도 적용 |
| T40·T41 Merge Queue | - | - | 팀 운영·데모 | |
| T42 rehearsal.exec | `services/rehearsal-runner` | §21 | 없음 | |
| T43 플랫폼 GitOps | `infra` | §26 | 참조 기술 | |
| T44 목적지 IaC API | `services/iac`, `infra-plan` | §30, §11 | 참조 기술 | |
| T45 목적지 처음 구축 | `services/infra-plan` | §1.2 PREPARE | 없음 | 사양의 PREPARE 프로필에 해당. 참조 기술로 둘지 판단 필요 |
| T46 SQS → Lambda 호환 런타임 | `services/function-runtime` | §17 | 일부 | 큐 인계(RabbitMQ·Kafka API), SQS 운영 메시지. 함수 런타임 없음 |
| T47 Cognito → Keycloak | `infra/envs/source-identity` | Part II | 대체·확장 | Keycloak 계정 집합 인계, Cognito·Identity Platform 디렉터리, OIDC probe |
| T48 tailnet WIF | `infra/envs/tailnet`, `tailnet-relay` | - | 참조 기술 | |
| T49 무인 해피패스 | `services/migration-driver` | - | 팀 운영·데모 | |
| T50 GCP RKE2·GKE | `infra/envs/gcp-rke2`, `gcp-gke` | §30 | 참조 기술 | 시험용 GKE는 `cloud-env` |

## 표 2. 팀 통제 20개와 sova-test probe

platform의 T14 시험기는 아래 20개를 출발지와 목적지에서 같은 방법으로 잰다. sova-test는 단위마다 같은 probe 묶음(쓰기 거부, 단일 writer, 다른 tenant 거부, 전송 암호화, 복원 훈련, 내용 비교)을 갖는다. 그 probe가 겹치는 통제는 일부뿐이다.

| 통제 | 질문 | sova-test |
|---|---|---|
| SEC-01.1 | 다른 서비스 계정으로 앱 DB Secret을 읽을 수 있는가 | (11-20) 있음: `KubernetesSecretProbes`. 다른 계정의 토큰으로 get·list·Pod dry run을 시도하고, API 서버 판단(워크로드 생성·수정 권한, 그룹 포함)과 맞춤. 클라우드 비밀 저장소는 없음 |
| SEC-01.2 | Git에 평문 비밀이 있는가 | 없음 |
| SEC-01.3 | 로그에 비밀이 찍히는가 | 없음 |
| SEC-01.4 | 비밀을 회전해도 앱이 재시작 없이 동작하는가 | 없음 |
| ENC-01.1 | 앱 → 연결 지점 구간이 인증서 검증 TLS인가 | 일부: `transport_encrypted`는 플랫폼 자신의 연결을 잰다. (11-20) 끝점이 등록 CA·이름으로 검증되는지도 잰다(`tls_refusal`의 양성 대조) |
| ENC-01.2 | 틀린 CA·hostname이 거부되는가 | (11-20) 있음: `refuse_unverified_transport`. PostgreSQL·객체 저장소·Redis 계열·RabbitMQ·Kafka API·MongoDB |
| ENC-01.3 | 평문 다운그레이드가 거부되는가 | (11-20) 있음: 같은 통제. 프로토콜의 첫 메시지를 평문으로, 등록 포트와 `plaintext_ports`에. MySQL은 없음 |
| ENC-01.4 | 복제 구간이 TLS인가 | 일부: 클라우드 DB 구독을 검증된 TLS로 연결(시험). probe 없음 |
| ENC-01.5 | pooler → DB 구간이 TLS인가 | 없음 |
| ENC-02.1 | 비밀 저장소가 암호화돼 있는가 | 없음 |
| AUD-01.1~3 | 관리자 로그인·DDL·권한 변경이 감사 로그에서 검색되는가 | 없음 |
| NET-01.1 | 외부 스캔에 공개 진입점만 보이는가 | 없음 |
| NET-01.2 | 외부에서 DB에 붙을 수 있는가 | 없음 |
| NET-01.3 | 다른 namespace에서 DB에 붙을 수 있는가 | (11-20) 있음: `NetworkReachCheck`. 바깥 namespace의 클라이언트 Pod 연결과, 앱 클라이언트의 양성 대조. NetworkPolicy 집행을 실측 |
| IAM-01.1 | 인증 없이 첨부 저장소 객체를 읽을 수 있는가 | (11-20) 있음: `deny_anonymous_data_access`. 보호 집합마다 서명 없는 읽기·목록·조건부 쓰기 |
| IAM-01.2 | 앱 권한이 기준선보다 넓은가(Prowler·kube-bench) | 결과 수용(security의 Prowler OCSF). 시험기 없음 |
| ISO-01.1 | 다른 앱·역할로 이 앱의 버킷·DB에 접근할 수 있는가 | 있음: `cross_tenant_denied`(PostgreSQL·MySQL·Redis 계열·Kafka API·RabbitMQ·MongoDB·볼륨·OIDC). (11-20) 객체 저장소도 양방향·집합별로 |
| DR-01.1 | 백업을 새 환경으로 실제 복원할 수 있는가 | 있음: 저장소별 `restore_drill`(scratch에 복원해 비교) |

## 흡수 순서 제안

1. **목적지 보안 통제를 계약 의무로 (T14·T9의 빈칸).** 표 2의 "없음" 가운데 이전 단위에 묶이는 것부터 probe로 만든다. **(11-20) 아래 항목과 NET-01.3을 마쳤다.** 남은 것은 MySQL 평문 거부, 클라우드 비밀 저장소의 다른 주체 거부, GCS 무인증 거부, 객체 저장소 단위의 워커 엔진이다.
   - 객체 저장소 probe 모듈을 새로 둔다: 무인증 읽기·목록 거부(IAM-01.1), 다른 앱 자격 거부(ISO-01.1).
   - 저장소 연결의 평문·틀린 CA 거부(ENC-01.2·01.3), 비밀 저장소의 다른 주체 거부(SEC-01.1).
   - 시험 환경에 기본값이 열린 S3 호환 저장소(SeaweedFS)를 더해, 잠그지 않은 목적지에서 실제로 막히는지 확인한다.
2. **환경 수준 통제(AUD·NET·SEC-01.2~4·ENC-02).** 단위가 아니라 환경에 걸린 통제다. 사양 §23(Evidence Contract)의 생산자로 둘지, 플랫폼 시험기로 둘지 먼저 정한다.
3. **AI 수정·리허설(T11·T15·T42).** 사양 §21의 격리·검증 독립성 요구가 크다. 1·2 뒤에 설계부터 한다.
4. **판정용 관측(T36)과 Edge 가중치 판정(T17의 SLI 부분).** 경로 전환 뒤 오류율·p95로 되돌리는 판정.
5. 참조 기술(목적지 구축 T2·T30·T44·T45 등)은 흡수하지 않는다. 다만 T45(PREPARE 프로필)를 제품 범위로 볼지는 따로 정한다.
