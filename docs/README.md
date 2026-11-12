# Sova 문서 목차

이 저장소에는 서로 다른 시기에 만든 실행 경로가 함께 있다. **설계 목표, 구현 코드, 특정 실행의 검증 기록, 현재 운영 상태**를 구분한다. 아래 문서의 과거 PASS나 화면을 현재 접속·권한·가동 증거로 쓰지 않는다.

## 처음 읽는 순서

| 목적 | 먼저 읽을 문서 | 해석 범위 |
|---|---|---|
| 최신 전용 대응 화면·실행기 | [실행 안내](workload-response-runtime.md) | 개인 EKS→이 Mac RKE2 및 로컬 기준선, 9개 대응 유형 |
| 무엇을 실제로 확인했는가 | [명세 추적표](spec-traceability.md), [날짜별 구현 현황](implementation-status.md) | producer·프로필·방향·기한이 같은 기록만 사용 |
| 목표 제품·6개 영역 설계 | [명세 v2](sova-production-spec-v2.md), [상세 설계](design/README.md) | 목표 계약이며 전체 운영 완료 선언이 아님 |
| 팀 platform과 보안 서비스의 연결 | [보안 인계](platform-security-handoff.md), [T28 인수](platform-t28-mac-intake.md) | 고정 계약 소비 시험과 실제 팀 운영 인수를 구분 |

## 화면·실행 경로 선택

| 코드 | 화면·실행 위치 | 주의할 점 |
|---|---|---|
| `demo/response_scenarios` | 워크로드 대응 화면 | 정적 화면 단독 실행은 가상 진행. 실제 연결은 다음 행의 서버가 제공 |
| `demo/local_response` | 최신 실제 대응 서버 | 고정된 Lima VM·클러스터 UID·소유 상태 필요. 다른 PC에 checkout만으로 실행되지 않음 |
| `demo/onprem_response` | 11/7~11/8 전용 무상태 시험 | API/API+작업자 격리 및 사전 승인 자동 대응. 최신 9개 실행기와 별도 |
| `console` | 초기 React 콘솔 | 프로세스 실험·초기 k3s·AWS/VM 프리뷰. 최신 Studio와 별도 |
| `demo/onprem_response/native_bridge.py` 등 계획 콘솔 경로 | 고정 platform 계약의 계획·SSE 소비 | 로컬 합성 계획, 실제 운영 계획 접근, 실제 실행을 구분 |

최신 대응 앱은 통제된 시험 워크로드다. 팀 demo-chat, 임의 고객 앱, 일반 침투시험이나 금융 보안 인증을 대신하지 않는다. SSM 하이브리드 시험 기록을 팀 Tailscale 접속의 검증으로 바꾸지 않는다.

## 저장소·구성 요소별 안내

- [Sova](<../README.md>) · `README.md`
- [Sova 운영 콘솔](<../console/README.md>) · `console/README.md`
- [실제 온프렘 워크로드 대응 시험](<../demo/onprem_response/README.md>) · `demo/onprem_response/README.md`
- [입력 예제](<../examples/README.md>) · `examples/README.md`
- [워크로드 대응 감사 보존 (S3 Object Lock)](<../infra/aws-response-audit/README.md>) · `infra/aws-response-audit/README.md`
- [워크로드 대응 하이브리드 출발지 (EKS)](<../infra/aws-response-source/README.md>) · `infra/aws-response-source/README.md`

## 별도 UI·기존 시험 경로

- [팀 demo-app 연결과 앱 등록](<app-deployment.md>) · `docs/app-deployment.md`
- [전용 시험의 사전 승인 자동 격리](<automatic-containment.md>) · `docs/automatic-containment.md`
- [실제 채팅 앱과 환경 이전 화면 연결](<chat-migration-connection.md>) · `docs/chat-migration-connection.md`
- [운영 이전·대응 UI 정리](<live-journey-ui.md>) · `docs/live-journey-ui.md`
- [실제 운영 계획 콘솔 연결](<native-plan-console.md>) · `docs/native-plan-console.md`
- [Sova](<preview-guide.md>) · `docs/preview-guide.md`
- [보안 거버넌스·워크로드 대응 검증 현황](<security-workload-validation.md>) · `docs/security-workload-validation.md`
- [실제 워크로드 대응: 지원 범위와 운영 인수](<workload-response-acceptance.md>) · `docs/workload-response-acceptance.md`

## 날짜가 고정된 개발·화면 검증 기록

- [README 워크로드 지도 · 실제 검증 범위](<assets/workload-verification.md>) · `docs/assets/workload-verification.md`
- [사전 승인 자동 대응 개발 기록 — 2026-11-08](<development/automatic-containment-2026-11-08.md>) · `docs/development/automatic-containment-2026-11-08.md`
- [실제 온프렘 워크로드 대응과 시각화 검증](<development/onprem-workload-response-2026-11-07.md>) · `docs/development/onprem-workload-response-2026-11-07.md`
- [T14·T33 맥 인수 도구 소비](<development/security-intake-2026-11-07.md>) · `docs/development/security-intake-2026-11-07.md`
- [터미널 요청과 실제 온프렘 워크로드 대응](<development/terminal-workload-response-2026-11-08.md>) · `docs/development/terminal-workload-response-2026-11-08.md`
- [워크로드 대응 확장 · 실행 기록](<development/workload-response-expansion-2026-11-08.md>) · `docs/development/workload-response-expansion-2026-11-08.md`
- [워크로드 대응 유형별 목업 — 2026-11-09](<development/workload-response-preview-2026-11-09.md>) · `docs/development/workload-response-preview-2026-11-09.md`
- [Sova 사이드바와 실제 관측 연결](<development/workspace-navigation-2026-11-08.md>) · `docs/development/workspace-navigation-2026-11-08.md`

## 제어면·보안·데이터 어댑터 계약

- [RDS 읽기 전용 보안 조사](<aws-rds-security.md>) · `docs/aws-rds-security.md`
- [등록된 Kubernetes 환경의 읽기 전용 조사](<kubernetes-discovery.md>) · `docs/kubernetes-discovery.md`
- [검증된 이미지의 제한적 Kubernetes 교체](<kubernetes-image-patch.md>) · `docs/kubernetes-image-patch.md`
- [단계별 이전 실행 계약](<migration-phase-execution.md>) · `docs/migration-phase-execution.md`
- [P0 실행 계약과 프리뷰 연결](<p0-contracts.md>) · `docs/p0-contracts.md`
- [PostgreSQL 실행 원장과 앱 소유권](<postgres-execution.md>) · `docs/postgres-execution.md`
- [Lifecycle의 PostgreSQL 저장](<postgres-lifecycle.md>) · `docs/postgres-lifecycle.md`
- [PostgreSQL 작업 접수와 outbox](<postgres-outbox.md>) · `docs/postgres-outbox.md`
- [출발지·목적지 보호 수준 비교](<protection-parity.md>) · `docs/protection-parity.md`
- [보안 거버넌스 실행 계약](<security-governance.md>) · `docs/security-governance.md`
- [등록된 보안 감시 워커](<security-monitoring.md>) · `docs/security-monitoring.md`

## 목표 설계와 이전 명세

- [Sova 상세 설계](<design/README.md>) · `docs/design/README.md`
- [AI 리소스 관리와 데이터 레지던시](<design/ai-residency.md>) · `docs/design/ai-residency.md`
- [이전 대상에서의 기능과 보호 재현](<design/destination-parity.md>) · `docs/design/destination-parity.md`
- [앱·데이터 이전과 복구](<design/migration.md>) · `docs/design/migration.md`
- [비용 통합과 OpenTelemetry 횡단 관측](<design/observability-cost.md>) · `docs/design/observability-cost.md`
- [AI 에이전트와 Harbor 샌드박스](<design/sandbox.md>) · `docs/design/sandbox.md`
- [보안·거버넌스와 워크로드 시각화](<design/security-governance.md>) · `docs/design/security-governance.md`
- [Sova 프로덕션 설계 명세 v1.1](<sova-production-spec-v1.md>) · `docs/sova-production-spec-v1.md`
- [Sova 프로덕션 목표 명세 v2](<sova-production-spec-v2.md>) · `docs/sova-production-spec-v2.md`

## 현재 구현과 후속 작업

- [구현·연결·운영 자격 현황](<implementation-status.md>) · `docs/implementation-status.md`
- [Sova 구현 순서와 수용 기준](<roadmap.md>) · `docs/roadmap.md`
- [통합 명세 추적표 (워크로드 대응 실행기)](<spec-traceability.md>) · `docs/spec-traceability.md`
- [담당 경계와 보안 근거 연동](<team-boundaries.md>) · `docs/team-boundaries.md`
- [워크로드 대응 실제 실행기](<workload-response-runtime.md>) · `docs/workload-response-runtime.md`

## Security·팀 platform 계약과 인수

- [두 보안 설계의 개인 검증과 platform 인계](<platform-security-handoff.md>) · `docs/platform-security-handoff.md`
- [T28 Mac 인수: 검증된 포트와 실제 플랫폼 연결의 차이](<platform-t28-mac-intake.md>) · `docs/platform-t28-mac-intake.md`
- [T28 재시작 경계: 기존 실행 원장에 연결](<platform-t28-restart.md>) · `docs/platform-t28-restart.md`
- [보안 서비스 소비 개발 기록](<security-service-development.md>) · `docs/security-service-development.md`

## 문서 점검 기록

- [2026-11-13 재점검](documentation-review-2026-11-13.md): 기준 SHA·수정 이유·검증 범위·미확인 사항.

## PDF·그림·공개 증빙

- [v1 PDF](sova-production-spec-v1.pdf), [v1.1 PDF](sova-production-spec-v1.1.pdf)는 과거 명세 스냅샷이다. 두 PDF의 내부 표제는 모두 v1.1이며 파일명의 `v1`만으로 본문 버전을 판단하지 않는다. 최신 구현이나 v2의 PDF로 해석하지 않는다.
- [목표 아키텍처 drawio](diagrams/sova-target-architecture.drawio)는 설계 그림이다.
- [팀 문서 보완 그림](diagrams/team-design-supplement/index.html) · [보완 4개 그림](diagrams/four-supplements/index.html): 같은 폴더의 SVG·PNG·drawio·manifest를 함께 사용한다. 복사본 `team-design-supplement 2/`는 과거 배포본으로 보존하며 최신 구현의 별도 근거로 세지 않는다.
- [워크로드 격리 원문](evidence/workload-response-20261108.json) · [자동 격리 원문](evidence/automatic-containment-20261108.json) · [메뉴 검증 원문](evidence/workspace-navigation-20261108.json)은 11/8 시험의 공개 요약이다. 최신 9개 실행기의 자격 원본과 합산하지 않는다.
- [썸네일 출처](assets/workload-verification.md)와 [이미지 메타데이터](assets/thumbnail.json)는 당시 화면의 제작·검증 범위를 설명한다. 정적 그림은 실시간 상태가 아니다.

외부 통합 초안 MIG-SPEC-001의 전문은 이 저장소에 포함돼 있지 않다. 추적표의 외부 요구 ID와 저장소 자체 명세 v2의 절 번호를 같은 계약으로 취급하지 않는다.
