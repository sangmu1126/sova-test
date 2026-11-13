# 계약·능력 기반 전방향 운영 인계 플랫폼 — 통합 상세 설계 명세

**통합 문서 ID:** MIG-SPEC-001-CONSOLIDATED  
**통합 버전:** 1.1-draft (2026-11-11)  
**기준 원문:** MIG-SPEC-001 1.0-draft (2026-11-06, 원문 보존)  
**포함하는 독립 확장:** MIG-EXT-IAM-001 1.0-draft (Cognito/Identity, 원문 보존)  
**신규 통합 제안:** SEC-GOV-EXT-001 / PATCH-EXT-001 (2026-11-11)  
**상태:** 목표 아키텍처 제안. 구현 여부와 실환경 검증 여부는 각각 따로 확인해야 함  
**목표:** Service Contract · Dependency Graph · Capability Qualification · Plan Compiler · Authority Handoff · Trusted Evidence · Security Governance · Continuous Revalidation

> 운영 서비스의 업무·데이터·보안·신원·가용성 계약을 유지하면서, 출발지·목적지의 검증된 능력에 맞춰 이전 계획을 만들고, 변경을 리허설·승인·실행·관측·검증한다. 이전 이후에는 실제 독립 운영 및 복귀 가능성을 주기적으로 재판정한다.

## 문서 구성 및 원문 보존 원칙

이 문서는 과거 원본을 임의로 축약·개작한 문서가 아니라 다음을 순서대로 합친 **통합본**이다.

1. **Part I — MIG-SPEC-001:** 2026-11-06 작성 기준 상세 명세 원문 전체. 내부 장 번호·참조·본문은 원본을 유지한다.
2. **Part II — MIG-EXT-IAM-001:** Cognito / Identity & Access Migration 상세 확장 원문 전체. 상위 스펙의 신원 계약을 명시적으로 보완한다.
3. **Part III — PATCH-EXT-001:** 별도 Patching as a Service 설계를 상위 계약·능력·증거·승인 모델에 반영하기 위한 신규 통합 설계. 기존 문서의 원문 전문을 그대로 붙인 것이 아니라 통합 관점에서 작성한 새 제안이다.
4. **Part IV — SEC-GOV-EXT-001:** 사고 대응 상태 머신, 신뢰 가능한 검증 증거, 권한 회수, 컷오버 보안, 테넌트·정책 수명주기 계약을 정리한 신규 확장안.
5. **Part V — 통합 적용·검증 로드맵:** 기존 플랫폼 태스크와의 접점을 제시한다. 실제 구현·운영 완료를 뜻하지 않는다.

**주의:** Part I/II의 각 절에 기재된 기능은 목표 아키텍처의 요구사항이며, Part III~V는 추가 제안이다. 문서 내 `지원`은 검증을 통과한 구체적인 실행 프로필에 한해서만 선언한다. 서로 다른 파트의 용어가 충돌할 경우 자동으로 덮어쓰지 않고 ADR 및 계약 버전 승격 후 확정한다.

---

# Part I. MIG-SPEC-001 — 원본 상세 명세 (원문 보존)

# 계약·능력 기반 전방향 운영 인계 플랫폼 — 상세 설계 명세

**문서 ID:** MIG-SPEC-001  
**버전:** 1.0-draft  
**작성 기준일:** 2026-11-06  
**상태:** 목표 아키텍처 / 제안 / 구현 및 실환경 검증 미확정  
**영문명:** Contract-Driven, Capability-Based Migration & Operations Platform  
**핵심 구성:** Service Contract · Dependency Graph · Migration Unit · Plan Compiler · Authority Handoff · Independent Verification

> 운영 중인 서비스가 지켜야 할 업무·데이터·보안·가용성 조건을 계약으로 정의하고, 출발지와 목적지의 검증된 능력에 따라 실행 가능한 이전 계획을 생성한다. 승인된 계획으로 업무 범위의 데이터·작업·처리 권한을 안전하게 인계하고, 이전 이후에도 계약과 독립 운영 가능성을 재검증한다.

---

## 0. 문서의 지위와 근거

### 0.1 공식 요구와 제안의 구분

사용자 제공 본선 PDF 「[Final Term｜Kickoff] SoftBank Hackathon 2026 powered by KOREC (3).pdf」의 다음 내용을 상위 문제 정의로 사용한다.

| 공식 자료 | 확인되는 방향 |
|---|---|
| 14쪽 | 이미 프로덕션에서 운영 중인 시스템을 다른 환경으로 안전하게 이전하고 보호 |
| 17·19쪽 | 데이터 동기화·트래픽 전환·복귀, 환경 간 보안·거버넌스, 목적지 운영 기능 재현, AI·데이터 위치, 샌드박스, 비용·관측 |
| 21쪽 | 실제 이전, 여러 환경의 보안 상태 시각화, AI 샌드박스 점검·수정 시연 |
| 23쪽 | 모든 테마 구현보다 선택한 영역의 프로덕션 수준 완성도 중시 |
| 40쪽 | 실제 동작, 적절한 추상화, 운영 설계, 독창성, AI 활용, 팀 개발과 의사결정 과정 평가 |

**이하의 서비스 계약, IR, 인계 프로토콜, API, 데이터 모델, 불변조건과 시험 기준은 이 문서의 설계 제안이다. 공식 PDF에 이 구현 방식이 지정되어 있지는 않다.** 현재 저장소의 구현·완료 상태를 나타내지 않으며, 특정 PR·작업 번호·담당자와 결합하지 않는다. 공개 기술 문서에서 가져온 제약은 부록 R의 출처를 표시한다.

### 0.2 규범 용어

- **MUST / 필수:** 지원을 주장하려면 충족해야 하는 요구다.
- **MUST NOT / 금지:** 안전성 또는 권한 경계를 깨뜨리는 동작이다.
- **SHOULD / 권고:** 다른 선택을 한다면 대안·위험·검증 근거를 남겨야 한다.
- **MAY / 선택:** 지원 프로필에 따라 추가할 수 있다.
- **예시 값:** 설명을 위한 가정이며 실제 성능, 제품 보장 또는 검증 결과가 아니다.

### 0.3 ‘전방향’의 의미

전방향은 **환경의 출발·목적지 역할을 제품 구조에 고정하지 않는다**는 의미다. 임의의 앱을 임의의 환경으로 무조건 이전한다는 의미가 아니다.

`Support(A → B)`는 `Support(B → A)`나 `Failback(B → A)`를 함의하지 않는다. 각 경로는 버전·권한·네트워크·데이터 의미·복구 방식까지 포함해 별도로 자격 시험을 통과해야 한다. 동일 사업자의 리전·계정·실행 방식 간 이전도 같은 모델에서 다룬다.

---

## 1. 제품 범위

### 1.1 제품이 책임지는 것

이전 전에는 서비스와 의존성, 필수 보호 요구, 정합성 경계, 환경 능력과 미확인 영역을 조사한다. 이전 중에는 승인된 변경, 데이터 동기화, 작업 정리, 처리 권한과 경로 전환, 실패 복구를 통제한다. 이전 후에는 업무·보안·복구 요구의 충족 여부, 증거의 유효성, 잔존 의존성과 복귀 가능성을 관리한다.

계획 컴파일러는 제품의 핵심 모듈이지만 제품 전체는 아니다. **컴파일러 + 실행 통제 + 검증 + 지속 관리**를 하나의 제품 경계로 둔다.

### 1.2 지원 영역과 지원 프로필

| 프로필 | 의미 | 지원을 위한 조건 |
|---|---|---|
| OBSERVE | 조사·비교·위험 설명만 수행 | 읽기 권한, 조사 범위·누락 표시 |
| PREPARE | 목적지 준비·격리 리허설 | 별도 승인 범위, 운영 쓰기 미활성화 |
| PLANNED_SINGLE_AUTHORITY | 계획된 단일 변경 권한 인계 | 모든 업무 쓰기 경로의 강제 차단·확인, 최종 데이터 검증 |
| PARTITIONED_HANDOFF | 검증된 DB·샤드·셀별 인계 | 분리된 데이터·권한·작업·라우팅 경계 |
| REVERSE_MIGRATION | 현재 상태에서 과거 환경 또는 제3환경으로 재이전 | 새 출발지 상태를 기준으로 재컴파일·재승인 |
| FAILBACK | 사전에 준비한 복귀 경로 실행 | 목적지 신규 데이터 회수, 복귀 계약·단일 권한 유지 |
| DISASTER_RECOVERY | 접근 불능·손상에 대한 복구 | 별도 RPO/RTO, 데이터 손실·업무 보상·승인 계약 |

첫 일반 실행 프로필은 `PLANNED_SINGLE_AUTHORITY`로 정의한다. 다중 쓰기를 기본 동작으로 하는 시스템은 해당 의미를 보존하는 별도 프로필과 자격 시험 없이는 지원하지 않는다.

### 1.3 비목표

임의의 프로그램 의미를 AI가 완전히 증명하는 것, CPU·프로세스 메모리와 열린 트랜잭션의 투명한 실시간 이동, 모든 클라우드의 기능을 최소 공통 기능으로 평준화하는 것, 외부 결제·메일의 정확히 한 번 실행을 일괄 보장하는 것, 접근 불가능한 원본의 쓰기 차단을 확인하지 않고 무손실 승격을 수행하는 것은 보장 범위가 아니다.

기존 애플리케이션을 반드시 Kubernetes·Wasm·특정 SDK로 변경하지 않는다. 변경이 필요하면 독립된 사전 현대화 계획으로 드러내고 비용·영향·검증을 승인받는다.

---

## 2. 개념과 용어

| 용어 | 정의 |
|---|---|
| Environment | 계정·리전·네트워크·클러스터·VM·관리형 자원으로 구성되는 관리 대상 환경 |
| Provider | AWS·Google Cloud·Azure·온프레미스 등 자원 제공 주체 |
| Backend | ECS·EKS·VM·관리형 DB 등 실제 실행·상태 관리 방식. Provider와 별도 축 |
| Service Contract | 서비스가 보존해야 할 업무·데이터·보안·가용성·위치·운영 조건 |
| Observed State | 실제 수집된 구성·상태. 요구사항과 구분 |
| Dependency Graph | 구성요소와 읽기·쓰기·인증·작업·복제·외부 효과 관계의 증거 기반 모델 |
| Migration Unit | 함께 인계해야 하는 정합성 경계. 물리 노드나 배포 이름과 동일하지 않음 |
| Capability | 특정 버전·조건에서 제공하고 검증할 수 있는 동작과 제약 |
| Strategy | 출발지·목적지 능력을 조합하는 방향성 있는 이전 방법 |
| Migration IR | 계획의 중간 표현. 작업·의존·효과·검증·승인·복구 조건 포함 |
| Business Authority | 업무 변경을 확정할 권한을 가진 환경. 물리 프로세스 하나를 뜻하지 않음 |
| Execution Lease | 관리 명령을 진행하는 실행자에게 주는 한시적 실행권 |
| Epoch | 오래된 실행·권한을 구분하기 위한 단조 증가 세대 번호 |
| Fence | 오래된 실행자가 실제 업무 효과를 확정하지 못하게 하는 집행 장치 |
| Evidence | 특정 주체·자원·버전·방법·시점·조건에 연결된 관측 증거 |
| Receipt | 실행자 또는 검증기가 남긴 결과 기록. 인증·검증 이전에는 신뢰하지 않음 |
| SAFE_HOLD | 관련 범위에서 충돌할 수 있는 추가 변경을 보류하는 상태 |
| Exit Contract | 제거할 의존성, 남길 의존성, 독립 운영 시험과 적용 범위를 명시한 계약 |

`tenant_id`는 플랫폼 고객 조직 식별자이고, `partition_key`는 고객 애플리케이션 안의 업무 분할 키다. 두 식별자를 혼용하지 않는다.

---

## 3. 신뢰·장애 모델

### 3.1 가정하는 장애

프로세스 중단·재시작, 요청·응답 유실, 지연·중복·순서 역전, 네트워크 분할, 리더 변경, 부분 성공, 취소와 실행의 경합, 외부 관리자 변경, 클라우드 권한·쿼터 부족, 시계 오차, 관측 누락, 소스·목적지 장애를 다룬다.

### 3.2 불신 입력

사용자 소스·README·로그·도구 응답·AI 출력·외부 스캐너 결과는 실행 권한을 부여할 수 없다. 임의 어댑터와 서명되지 않은 실행기도 신뢰하지 않는다. 등록된 실행기라도 그 영수증만으로 실제 데이터 정합성을 확정하지 않는다.

### 3.3 보장의 전제

단일 처리 권한 보장은 **계약에 포함된 모든 쓰기 경로를 차단할 수 있고, 그 차단이 지속되며, 새 경로의 변경도 통제할 수 있는 지원 프로필**에 한정한다. 고객 root·DB superuser가 통제를 우회하거나, 비등록 시스템이 새 쓰기 경로를 만들거나, 효과 지점 자체가 침해된 상황까지 일반 분산 장애 보장으로 포함하지 않는다. 해당 상황은 탐지·격리·사고 대응 대상으로 분리한다.

네트워크 분할과 상태 불확실성에서 안전성과 즉시 가용성을 동시에 무조건 보장하지 않는다. 원본 변경 차단을 입증하지 못하면 무손실 인계 프로필의 목적지 활성화를 금지한다.

---

## 4. 핵심 불변조건

| ID | 필수 조건 | 집행·검증 지점 |
|---|---|---|
| INV-01 | 단일 권한 프로필에서 동일 업무 범위의 변경을 확정할 권한은 최대 한 환경에 존재 | DB·Worker·외부 효과 경계 |
| INV-02 | 계약상 성공 응답한 업무 결과는 해당 계획의 보존 조건을 충족 | 요청 원장·최종 데이터·업무 검증기 |
| INV-03 | 동일 멱등 키·동일 입력의 재시도는 지원 범위 내에서 중복 업무 효과를 만들지 않음 | DB 제약·처리 이력·외부 API |
| INV-04 | 효과 불명 상태에서 충돌하는 실행·반대 명령을 추측으로 진행하지 않음 | 조정기·효과 원장·실행기 |
| INV-05 | 실행 대상·변경·산출물·권한·정책은 승인된 계획과 일치 | 실행 직전 및 효과 지점 |
| INV-06 | 미확인·만료·서명 불일치 증거를 PASS로 처리하지 않음 | 검증기·정책 게이트 |
| INV-07 | 이미 확정된 업무 경로는 중앙 관리 서버의 매 요청 응답을 요구하지 않음 | 진입점·현장 실행 환경 |
| INV-08 | 코드·스키마·권한·어댑터 변경은 영향받는 기존 증거·승인을 재평가 | 증거 의존성 인덱스 |
| INV-09 | AI·샌드박스에 운영 권한 발급·최종 승인·통과 판정 권한을 부여하지 않음 | 신원·도구 게이트·정책 |
| INV-10 | 원본 폐기와 복구 경로 종료는 이전 성공과 별개의 승인 | 보존·폐기 정책 |
| INV-11 | 고객 조직 간 데이터·실행권·증거·비밀 경계를 유지 | API·DB·큐·자격증명 |
| INV-12 | 데이터 복제·검증용 쓰기와 업무 변경 권한을 구분 | 별도 역할·허용 작업 |

INV-01에서 한 환경에 여러 API·Worker·DB 복제본이 존재할 수 있다. 제한하는 것은 프로세스 수가 아니라 **서로 충돌할 수 있는 독립 업무 변경 권한**이다.

---

## 5. 사용자·권한 모델

| 역할 | 허용 | 금지·추가 조건 |
|---|---|---|
| Viewer | 계약·현재 상태·정제된 증거 조회 | 비밀 원문·운영 변경 |
| Service Owner | 업무 불변조건·SLO·분할 경계 선언 | 자기 선언만으로 검증 PASS 생성 |
| Operator | 계획·리허설 요청, 승인된 실행 시작 | 승인 범위 확장 |
| Security Reviewer | 보호 요구·예외·격리 검토 | 단독으로 업무 손실 허용 |
| Approver | 자신에게 위임된 범위 승인 | 고위험 변경의 자기 승인 |
| Adapter Maintainer | 어댑터 개발·자격 시험 제출 | 자기 어댑터에 운영 신뢰 자동 부여 |
| Executor | 발급된 작업만 수행 | 계획·정책·승인 수정 |
| Verifier | 독립 시험·영수증 발급 | 임의 운영 변경 |
| Platform Admin | 플랫폼 유지·보수 | 감사·고객 데이터 무제한 접근을 기본 부여하지 않음 |

고위험 인계·보존 종료·원본 삭제는 직무 분리를 적용한다. `break-glass`는 만료·사유·범위·사후 검토를 기록하며, 미확인 증거를 통과로 바꾸거나 이중 쓰기를 허용하는 기능이 아니다.

---

## 6. 기능 요구사항

| ID | 요구사항 | 완료 조건 |
|---|---|---|
| FR-01 | 환경 등록·읽기 권한 확인 | 자원 UID·계정·리전·권한·누락 범위 기록 |
| FR-02 | 서비스 계약 등록·버전 관리 | 필수 조건·SLO·책임자·변경 이력 |
| FR-03 | 코드·IaC·런타임 의존성 조사 | 선언·관측·추정·미확인 구분 |
| FR-04 | 정합성 경계 후보 산출 | 경계 결합 이유·미확인 연결·승인자 |
| FR-05 | 목적지 후보와 환경 능력 비교 | 지원·미지원·검증 필요·정보 부족 구분 |
| FR-06 | 방향별 전략 자격 관리 | 정방향·역방향·failback 각각의 증거 |
| FR-07 | 계획 컴파일 | 버전 고정 IR·위험·권한·검증·복구·근거 생성 |
| FR-08 | 코드 패치와 실행 계획 일관성 검사 | 실제 빌드 산출물과 설정·권한 가정 일치 |
| FR-09 | AI 격리 수정·리허설 | 비밀 미제공·독립 시험·예산·정리 증거 |
| FR-10 | 목적지 구성·보안 기능 검증 | 허용·거부·감사·복원 시험 |
| FR-11 | 승인 결속 | revision·digest·대상·정책·만료 검증 |
| FR-12 | 제한 실행권 발급 | 작업·자원·권한·기한·epoch·audience 결속 |
| FR-13 | 초기 복사·변경분 동기화 | 데이터 범위·시작점·적용 위치·지연 기록 |
| FR-14 | 요청·진행 작업 정리 | 새 요청·진행 트랜잭션·작업·외부 효과 상태 분류 |
| FR-15 | 출발지 실제 변경 차단 | 기존 연결·우회 경로 포함 거부 확인 |
| FR-16 | 최종 검증·목적지 활성화 | 경계별 정합성·업무·보안 조건 충족 |
| FR-17 | 요청 경로 인계 | 인증된 범위·경로 버전·잔존 연결 검증 |
| FR-18 | 효과 불명 처리 | 맹목 재실행 없이 실제 상태 조회 |
| FR-19 | 취소·안전한 보류·복구 | 단계·실제 효과에 따른 허용 경로 |
| FR-20 | 미완료 업무 인계 | 영속 작업·중복 방지·외부 결과 확인 |
| FR-21 | 출발지 독립성 시험 | 새 기동·연결·비밀 갱신·신규 업무 시험 |
| FR-22 | 복귀 가능성 관리 | 현재 조건·검증 시각·만료·복귀 경로 |
| FR-23 | 지속 재검증·패치 | 변경 영향에 따라 증거 무효화·수정안 생성 |
| FR-24 | 플랫폼 자체 탈출 | 설정·운영 인계, 권한 회수 후 배포·복원 시험 |
| FR-25 | 감사·증거 보존 | 삭제 권한 분리·무결성·검색·보존 시험 |
| FR-26 | 비용·용량 평가 | 전송·병행 운영·복귀 보존·AI·인력 가정 표시 |
| FR-27 | 다중 서비스 변경 조정 | 공유 자원·버전·변경 창 충돌 검출 |
| FR-28 | 미지원 설명 | 부족 능력·필요 변경·대안·재평가 조건 반환 |

## 7. 논리 아키텍처와 책임

```text
운영자·서비스 책임자·승인자
             │
       Console / API
             │
   계약·환경·의존성·증거 모델
             │
       계획 컴파일러 ───── AI 제안 / 격리 리허설
             │                        │
       독립 검증 / 정책 게이트 ◀───────┘
             │
       승인·실행권·효과 원장
             │
       영속 실행 조정기
             │
       제한 명령 / 상태 조회
          ┌──┴─────────┐
     출발지 실행기    목적지 실행기
          │              │
   앱·DB·작업·신원 ──복제── 앱·DB·작업·신원
          └──── 안정 진입점 ────┘
                    │
                사용자 요청

실제 관측 → 독립 검증 → 증거 유효성 평가 → 필요한 재컴파일
```

### 7.1 구성요소 계약

| 구성요소 | 소유하는 책임 | 소유하지 않는 책임 |
|---|---|---|
| Inventory / Discovery | 읽기 수집·관측·범위·증거 | 업무 의미의 독자적 확정 |
| Contract Registry | 요구사항·버전·책임자 | 현재 환경 설정을 요구로 자동 채택 |
| Graph & Boundary Analyzer | 관계·공유 상태·경계 제약 | 관측 부재를 의존성 부재로 단정 |
| Capability Catalog | 어댑터·전략·버전·자격 시험 | 제품 이름만으로 호환 판정 |
| Plan Compiler | 후보·제약·IR·위험·검증 의무 | 운영 API 직접 호출 |
| AI Workbench | 조사·패치 후보·설명 | 운영 실행권·최종 PASS |
| Independent Verifier | 실제 외부 상태·업무·권한 시험 | 자기 시험 대상의 임의 수정 |
| Policy Engine | 허용·거부·필요 승인 판단 | 실제 자원 변경의 자동 강제 |
| Authority Service | 승인·소유권 전이·명령 의도·실행권 | 실제 DB 쓰기 차단의 대체 |
| Durable Orchestrator | 단계·대기·재조회·재개 | 독립적인 권한 원장 |
| Environment Executor | 제한된 효과 실행·관측 | 계획 변경·범위 확대 |
| Evidence Service | 서명·출처·유효성·무효화 | 모든 실측 사실의 무오류 보증 |
| Observability / Cost | 측정·관측·비용 가정 | 샘플링 로그만으로 무손실 인증 |

### 7.2 권위의 분리

**계약·승인·명령 의도는 플랫폼 원장이 기준이고, 실제 자원 효과는 외부 환경 관측이 기준이다.** 서로 어긋나면 어느 한쪽을 임의로 덮어쓰지 않고 `RECONCILE_REQUIRED`로 처리한다.

워크플로 엔진의 단계 상태는 운영 편의를 위한 진행 상태다. 작업 완료 이벤트만으로 업무 변경의 성공을 판정하지 않는다. Temporal Activity는 성공 효과를 만들고도 완료 응답을 남기지 못하면 재시도될 수 있으므로 어댑터별 멱등성과 결과 조회가 필요하다.[R2]

### 7.3 관리 영역과 업무 영역

기존에 확정된 업무는 중앙 플랫폼의 매 요청 조회 없이 실행되어야 한다. 라우팅 테이블·현재 업무 권한 세대·검증된 정책의 지역 사본을 사용할 수 있다. 하지만 지역 캐시를 근거로 **새로운** 처리 권한을 자율 발급하지 않는다.

중앙 연결 단절 시 새 인계·권한 승격·정책 변경을 제한한다. 이미 활성인 환경의 정상 업무와 진행 중 인계의 보류는 별도로 다룬다. 비밀·인증서 갱신, 재시작, 이미지 수급이 중앙에만 의존한다면 그 장애 한계를 계약에 기록하고 독립 운영을 주장하지 않는다.

---

## 8. 핵심 데이터 모델

### 8.1 공통 메타데이터

모든 객체는 `tenant_id`, 불변 `id`, `revision`, `schema_version`, 생성 주체·시각, 관련 객체의 ID·revision, 정규화 digest를 가진다. 자원 경로만으로 신원을 판단하지 않고 가능한 경우 공급자 자원 ID·UID·계정·리전·클러스터를 결속한다.

역할이 다른 값을 구분한다. `revision`은 모델 변경 버전, `resource_version`은 외부 자원 버전, `epoch`는 권한 세대, `attempt_id`는 한 실행 시도, `operation_id`는 재시도에도 유지되는 논리 작업 ID다.

### 8.2 엔터티

| 모델 | 핵심 필드 |
|---|---|
| Environment | provider, backend, account/region, architecture, endpoints_ref, identity_ref, capabilities_ref, observed_at |
| ServiceContract | owner, business_invariants, acknowledgment_semantics, datasets, SLO, security, residency, operations, cost, approval_policy |
| DependencySnapshot | nodes, edges, coverage, source_revisions, observation_window, unresolved_edges, digest |
| MigrationUnit | member_ids, dataset_selectors, partition_contract, effect_boundaries, writers, shared_dependencies, freeze_scope |
| CapabilityManifest | adapter, version, build_digest, roles, operations, preconditions, semantics, limits, qualification_refs |
| StrategyQualification | source_profile, target_profile, direction, data_features, workload_limits, tests, expiry, exclusions |
| PlanRevision | immutable inputs, unit_refs, strategies, patch_refs, operation_graph, evidence_requirements, recovery, locks, digest |
| Approval | plan_digest, scope, approver, policy_digest, artifact_digests, expires_at, revocation_state |
| Execution | plan_revision, state, active_step, authority_epoch, recovery_incarnation, deadline, operation_ids |
| OperationRecord | operation_id, input_hash, intent, attempt_ids, state, target_identity, expected_effect, observations |
| AuthorityRecord | unit, owner_environment, epoch, phase, activation_intent, last_verified_effect, fence_refs |
| Evidence | subject/version, predicate, measurement, method, provenance, scope, freshness, artifacts, signature |
| ReversibilityContract | retained_source, reverse_strategy, compatible_versions, RPO/RTO, evidence, expires_at, termination_approval |
| ExitContract | removed_dependencies, allowed_remaining, runtime/build/recovery tests, handover_package |
| Exception | failed_requirement, risk, compensating_controls, approvers, expiry, allowed_operations |

### 8.3 서비스 계약 예시

다음 YAML은 제안 형식이며 특정 외부 제품의 공식 API가 아니다. 수치와 이름은 예시다.

```yaml
apiVersion: migration.platform/v1alpha1
kind: ServiceContract
metadata:
  tenant_id: org-example
  id: orders-service
  revision: 7
spec:
  owner: team-orders
  acknowledgment_semantics:
    accepted: durable_request_record
    completed: transaction_committed_and_result_recorded
  business:
    invariants:
      - id: ORDER-01
        description: 같은 고객·멱등 키의 주문 효과는 한 번만 확정한다.
      - id: ORDER-02
        description: 주문과 재고 예약의 관계가 업무 규칙을 만족한다.
    external_effects:
      - id: shipment-request
        idempotency: required
        result_lookup: required
        on_unknown: hold
  migration:
    profile: PLANNED_SINGLE_AUTHORITY
    units:
      mode: verified_partition
      partition_key: customer_id
      partition_contract_ref: partition-contract-r3
      reject_cross_partition_transactions: true
  data:
    protected_sets:
      - orders
      - inventory_reservations
      - work_items
      - outbox
      - consumer_inbox
      - idempotency_results
      - attachment_manifest
    preserve_completed_operations: true
    comparison_contract_ref: order-consistency-v2
  availability:
    max_write_admission_pause_ms: 5000
    max_client_completion_wait_ms: 15000
    latency_budget_ref: orders-slo-v3
    read_consistency_during_handoff: read_your_writes_or_explicit_retry
  security:
    required_controls:
      - authenticated_tenant_routing
      - deny_cross_tenant_data_access
      - deny_cross_workload_secret_access
      - verified_transport_encryption
      - searchable_tamper_protected_audit
      - tested_restore
  residency:
    business_data: [approved-zone-a]
    backups: [approved-zone-a]
    audit: [approved-zone-a]
    ai_inputs: [approved-ai-processing-zone]
  operations:
    restore_test_required: true
    source_retirement_requires_separate_approval: true
  approval:
    separation_of_duties: true
    high_risk_handoff: human_approval
  exit:
    remove: [source-db, source-secret-service, source-object-store]
    allowed_remaining: [customer-owned-idp, customer-owned-image-registry]
```

`5000ms`를 충족할 수 없는 실측 결과가 나오면 계약을 조용히 낮추지 않는다. 다른 전략·범위·유지보수 창 또는 권한 있는 계약 변경을 제안한다. `accepted`와 `completed`를 구분하므로 `202`를 받은 요청을 최종 업무 완료로 집계하지 않는다.

---

## 9. 의존성 그래프와 증거

### 9.1 그래프 의미

노드는 API·Worker·DB·큐·객체 저장소·Secret·IdP·키·진입점·레지스트리·배치·외부 AI·백업·라이선스 서비스를 포함할 수 있다.

연결에는 `READS`, `WRITES`, `AUTHENTICATES_WITH`, `PUBLISHES_TO`, `CONSUMES_FROM`, `REPLICATES_TO`, `ENCRYPTED_BY`, `CALLS_EXTERNAL_EFFECT`, `DEPENDS_ON_BUILD`, `DEPENDS_ON_RECOVERY` 등 의미를 부여한다. 단순 네트워크 통신 한 번을 업무상 필수 의존성으로 자동 승격하지 않는다.

연결마다 호출 주체, 데이터 분류, 허용 작업, 트랜잭션 경계, 전달 의미, 순서·중복 규칙, 지역, 근거, 관측 창, 불확실성을 기록한다.

### 9.2 분류 축

| 축 | 값 |
|---|---|
| 근거 종류 | DECLARED, STATIC, RUNTIME_OBSERVED, OWNER_CONFIRMED, TESTED |
| 근거 상태 | VALID, STALE, UNKNOWN, CONFLICTING |
| 변환 유형 | CONFIG_ONLY, ADAPTER_CHANGE, CODE_CHANGE, DATA_IDENTITY_CHANGE, SEMANTIC_GAP |
| 가능성 판정 | CANDIDATE, REQUIRES_CHANGE, UNSUPPORTED, NEEDS_EVIDENCE |

한 의존성이 정적 분석과 실행 관측 근거를 동시에 가질 수 있다. `UNKNOWN`은 “의존성 없음”도, 독립적인 변환 유형도 아니다.

### 9.3 조사 범위와 미관측

야간 배치·오류 복구·토큰 갱신·자동확장·신규 사용자 가입·재시작 경로는 짧은 정상 트래픽 관측만으로 확인되지 않을 수 있다. 계약에는 조사한 경로와 조사하지 못한 경로를 기록한다. 보안·데이터·처리 권한에 영향을 줄 수 있는 미확인 경로가 남으면 관련 실행 게이트를 차단한다.

---

## 10. Migration Unit 산출

### 10.1 경계 폐쇄 규칙

단순 호출 그래프의 연결 성분 전체를 무조건 한 단위로 만들지 않는다. 읽기 API 의존성은 계약이 있으면 잠시 환경을 가로질러 유지할 수 있다. 반면 원자적 갱신과 공유 쓰기·재고 같은 불변조건은 여러 노드를 하나로 묶는 강한 제약이다.

분석 순서는 다음과 같다.

1. 서비스 책임자가 이전할 업무·분할 기준을 제안한다.
2. 실제 데이터·작업·권한·외부 효과 관계를 수집한다.
3. 원자적 트랜잭션·공유 쓰기·분할 불가능한 중복 방지 상태를 결합 제약으로 추가한다.
4. 결합 제약의 전이적 폐쇄로 최소 안전 후보 범위를 계산한다.
5. 경계를 가로지르는 관계마다 `유지`, `브리지`, `변환`, `경계 병합`, `미지원`을 판정한다.
6. 실제 차단·복제·라우팅의 통제 단위와 후보 범위가 일치하는지 검사한다.
7. 책임자 확인과 독립 리허설 뒤 경계를 승인한다.

서로 다른 DB에 걸친 강한 불변조건이 있으면 단순 barrier 하나로 같은 시점을 얻었다고 주장하지 않는다. 협력적 quiesce나 공통 업무 체크포인트가 없으면 경계를 확장하거나 지원을 거부한다.

### 10.2 고객사 단위 이전 허용 조건

데이터 선택자, 권한, 큐 소비자, 배치, 외부 효과의 멱등 범위, 라우팅을 고객사별로 구분할 수 있어야 한다. A·B가 전역 재고를 함께 변경한다면 고객사 A의 DB만 옮기는 계획을 그대로 허용하지 않는다. 공용 재고를 유지하는 계획은 `부분 이전·잔존 의존성 있음`으로 표시하고, 경계 간 의미를 별도로 검증한다.

PgBouncer `PAUSE [db]`는 DB 단위이므로 공유 DB에서 고객사 A만 멈추는 기능으로 간주하지 않는다.[R4] PostgreSQL 행 필터의 복제 범위와 업무 쓰기 권한의 범위도 별개다.[R5]

### 10.3 결과

`PARTITIONABLE`, `MERGE_REQUIRED`, `MODERNIZATION_REQUIRED`, `NEEDS_EVIDENCE`, `UNSUPPORTED` 중 하나를 반환한다. 여러 서비스의 호출 순환은 검토 사유이지만, 순환 자체가 항상 원자적 이전 필요성을 뜻하지는 않는다.

---

## 11. 능력 카탈로그와 환경 어댑터

### 11.1 분리해야 할 축

`Provider`, `ComputeBackend`, `DataEngine`, `IdentityMechanism`, `NetworkTopology`, `Architecture`, `Version`을 독립적으로 기록한다. AWS라는 이유만으로 Kubernetes API를 사용할 수 있다고 가정하지 않는다. Kubernetes를 쓴다고 CNPG·PgBouncer가 존재한다고 가정하지 않는다.

### 11.2 어댑터 종류

| 종류 | 제공 작업 예 |
|---|---|
| Discovery | inspect_resources, inspect_permissions, inspect_runtime |
| Compute | stage_artifact, start_readonly, check_health, drain, stop |
| Data | snapshot, start_sync, checkpoint, verify_applied, fence_writes, activate_writer |
| Queue / Work | pause_claims, inspect_inflight, export_work_state, resume_claims |
| Object | enumerate_versions, transfer, verify_content, reconcile_references, fence_uploads |
| Identity / Secret | establish_identity, map_policy, deliver_secret_ref, rotate, revoke, test_access |
| Traffic | prepare_route, commit_route, inspect_effective_route, drain_connections |
| Observability / Recovery | emit_evidence, test_audit, backup, restore, verify_recovery |
| AI Service | inspect_api_contract, evaluate_quality, enforce_residency, check_tool_permissions |

### 11.3 능력 명세 예

```yaml
kind: CapabilityManifest
metadata:
  adapter_id: postgresql-logical-handoff
  adapter_version: 1.0.0-proposed
spec:
  operations:
    - id: fence_business_writes
      scopes: [database]
      confirmation: independent_write_denial_probe
      survives_executor_restart: required
      restore_behavior: quarantined_until_authority_verified
    - id: synchronize_changes
      semantics:
        ddl_replication: false
        sequence_state_replication: false
        business_idempotency_state: must_be_included
  direction_qualification:
    source_profile_ref: pg-source-profile
    target_profile_ref: pg-target-profile
    evidence_ref: required-before-advertising-support
  fallback:
    on_missing_scope_isolation: reject_partitioned_handoff
```

이 명세는 능력 선언이지 자격 인증서가 아니다. 해당 manifest와 코드 digest, 환경 버전, 실험 조건에 일치하는 qualification이 있어야 실행 후보로 인정한다.

### 11.4 어댑터 ABI

모든 명령은 구조화 입력과 출력 스키마를 가진다. 최소 메서드는 `inspect`, `validate_preconditions`, `prepare`, `execute`, `observe`, `verify_postconditions`, `cancel_if_safe`, `cleanup_owned_resources`다. 복구는 허용 조건을 가진 별도 작업이며 모든 동작에 역연산이 있다고 가정하지 않는다.

일반 어댑터가 임의 shell·SQL·Terraform을 자유 실행하는 인터페이스는 금지한다. 필요한 사용자 정의 변환은 별도 리뷰·서명·시험을 거친 제한 플러그인으로 등록한다.

### 11.5 지원 행렬

지원 키는 `(source profile, target profile, direction, strategy version, contract class, tested limits)`다. 공급자마다 플러그인 하나를 구현했다고 모든 조합이 해결되지는 않는다. 특히 데이터 의미 변환과 failback은 조합별 자격 시험이 필요하다.

새 어댑터 등록으로 가능한 작업은 늘어날 수 있지만, 기존 계획의 어댑터 버전은 자동 교체하지 않는다. 보안 결함으로 자격을 철회한 버전은 새 실행을 중지하고 진행 중 계획은 안전한 체크포인트에서 재평가한다.

---

## 12. 계획 컴파일러

### 12.1 입력

불변 ServiceContract revision, DependencySnapshot, MigrationUnit, 출발지 관측, 목적지 후보, CapabilityManifest·qualification, 정책 bundle, 비용·용량 snapshot, 변경 창·기존 관리권 정보를 입력으로 고정한다.

### 12.2 단계

| 단계 | 동작 | 출력·차단 사유 |
|---|---|---|
| C0 Normalize | 스키마·ID·신원·입력 버전 검증 | 입력 오류, 범위 밖 자원 |
| C1 Discover | 선언·정적·런타임·소유자 근거 결합 | 관측 snapshot, 조사 공백 |
| C2 Boundary | 정합성 경계·공유 효과·우회 쓰기 판정 | 단위, 병합·사전 변경 필요 |
| C3 Capability | 방향·버전·범위·권한 지원 검사 | 능력 부족, 자격 만료 |
| C4 Constraints | 보안·위치·데이터·가용성 필수 조건 검사 | 후보 제외 및 구체 사유 |
| C5 Strategy | 가능한 실행·데이터·신원 전략 조합 | 후보 계획·의미 차이 |
| C6 Remediation | 필요한 코드·설정 수정 명세 및 AI 작업 | PatchPlan, 별도 리허설 요청 |
| C7 Schedule | 순서·read/write sets·배타 범위·승인 경계 계산 | 실행 DAG와 조건부 분기 |
| C8 Obligations | 검증 의무·증거 유효성·복구 조건 생성 | VerificationPlan, RecoveryPlan |
| C9 Lower | 환경별 typed operation·IaC·매니페스트 생성 | 실제 실행기용 명세 |
| C10 Consistency | 코드·이미지·환경·권한·계획 상호 일치 검사 | 불일치 차단 |
| C11 Freeze | 정규화·digest·근거·지원 범위 확정 | 불변 PlanRevision |
| C12 Authorize | 독립 검증·정책·사람 승인 | 실행 가능 여부. 컴파일과 별도 |

AI 호출은 C6 등 제안 단계에서 수행한다. **동일한 동결 입력·AI 제안 산출물·컴파일러 버전에서 결정적 IR을 생성하는 것**이 재현성 목표다. AI를 다시 호출해도 같은 문장이 나온다는 보장은 아니다.

### 12.3 후보 선택

필수 조건을 비용 점수와 상쇄하지 않는다. 먼저 `충족 가능`, `미충족`, `근거 부족`을 나눈 후, 충족 후보에서 비용·운영 부담·성능·잔존 의존성을 비교한다. 요구를 완화하면 별도 계약 revision과 승인이 필요하다.

모든 후보를 탐색하지 못했으면 `SEARCH_INCOMPLETE`를 반환한다. 카탈로그 안에서 계획을 못 찾은 것을 모든 기술적 방법의 불가능성으로 표현하지 않는다.

### 12.4 결과 상태

| 상태 | 의미 |
|---|---|
| COMPILED | 지원된 규칙으로 계획 생성. 운영 실행 허가는 아님 |
| REQUIRES_REMEDIATION | 사전 코드·설정·데이터 변경 필요 |
| NEEDS_EVIDENCE | 중요한 사실·권한·성능·경계 증거 부족 |
| UNSUPPORTED | 현재 등록·자격 확인된 전략으로 지원하지 않음 |
| NO_FEASIBLE_PLAN | 고정한 후보·제약 집합에서 충족 계획 없음 |
| SEARCH_INCOMPLETE | 탐색 예산·정보 부족으로 결론 미확정 |
| CONFLICT | 기존 관리권·동시 계획·변경 창과 충돌 |

### 12.5 계획 간 충돌

계획은 예상 읽기·쓰기·권한 변경·공유 기반 자원 집합을 선언한다. 동일 자원 ID만 비교하지 않고 부모·자식 범위, DB 전체와 특정 샤드, 공용 인증·네트워크 정책의 겹침도 검사한다. 영향받는 자원만 잠그되, 불확실한 범위는 보수적으로 확대한다. lock 획득 순서를 고정해 교착 가능성을 줄이고, 긴 리허설 동안 운영 lock을 무한 점유하지 않는다.

---

## 13. IR과 계획 결속

### 13.1 계획 묶음

`MigrationPlan`은 `PatchPlan`, `InfrastructurePlan`, `DataPlan`, `IdentityPlan`, `TrafficPlan`, `VerificationPlan`, `RecoveryPlan`, `ExitPlan`을 묶는다. 코드 수정과 목적지 설정이 별개로 생성되더라도 최종 이미지·환경변수·Secret 참조·권한이 서로 일치해야 한다.

계획 그래프는 유한 DAG와 명시적 조건부 분기로 구성한다. 재시도·재조회는 별도 실행 상태 머신의 제한된 반복이며 AI가 실행 중 임의 단계를 추가하지 않는다.

### 13.2 작업 스키마

```yaml
operation_id: op-unit-a-activate-target
kind: activate_business_authority
adapter_ref: pg-handoff@qualified-digest
scope:
  tenant_id: org-example
  migration_unit_id: orders-customer-a
  target_environment_id: env-target
  target_resource_uid: verified-target-db-uid
read_set: [authority/orders-customer-a, evidence/final-checkpoint]
write_set: [authority/orders-customer-a, target-db/write-policy]
requires:
  - source_fence_verified_and_latched
  - final_changes_applied
  - business_consistency_verified
  - destination_access_controls_verified
  - approval_matches_plan_revision
  - activation_intent_durable
ensures:
  - destination_business_writes_enabled_for_current_epoch
  - source_business_writes_remain_fenced
idempotency:
  key_scope: tenant+execution+operation
  same_key_different_input: reject
on_uncertain: observe_or_safe_hold
recovery_boundary: post_activation_intent
required_evidence: [source-fence, data-final, security-final]
```

이는 논리 예시다. 실행기 서버가 실제 scope와 evidence를 공급·검증한다. 클라이언트가 `source_fence_verified: true`를 보냈다는 이유로 허용하지 않는다.

### 13.3 digest 규칙

JSON 표현은 스키마 검증 뒤 정규화한다. JCS를 사용할 수 있으나 지원 숫자·문자 표현을 제한해야 한다.[R9] 금액·LSN·큰 epoch 등 정밀도가 중요한 값은 정의된 문자열 형식으로 저장한다. 정규화는 업무 의미의 동등성 증명이 아니다.

`plan_digest`에는 계약·그래프·정책·어댑터·작업·산출물·대상·허용 예외·복구 조건의 참조와 digest를 포함한다. 계획 digest 자체와 실행 중 측정값은 순환 없이 외부 envelope에 둔다. `compile_id`는 실행 이력 식별자이며 그 값이 달라도 동결된 의미 입력이 같다면 계획 내용 digest는 동일할 수 있다.

동적 상태는 계획 digest에 매번 덮어쓰지 않고 사전조건·버전 guard·유효한 증거로 확인한다. 승인 후 의미 있는 계획 변경은 새 revision을 만든다.

---

## 14. 승인·실행권·효과 원장

### 14.1 승인 범위

리허설 준비, 목적지 운영 자원 준비, 처리 권한 인계, 복귀, 원본 폐기를 서로 다른 승인 scope로 둔다. 하나의 승인으로 모든 삭제·추가 권한을 허용하지 않는다.

승인은 `tenant`, `plan revision/digest`, `unit`, 대상 자원 UID, 코드 diff·이미지 digest, 정책 digest, 필요 증거 집합, 유효기간, 승인자·직무 분리, 예외 집합에 결속한다. 제안서 텍스트만 승인하지 않는다.

### 14.2 제한 실행권

실행권은 서명된 envelope로 전달하며 audience, command ID, operation ID, input hash, target UID, executor identity, deadline, epoch, recovery incarnation, allowed effects를 포함한다. 서버 측 신뢰 시간·시간 오차 허용 범위·서명 키 회전 정책을 둔다. 만료는 **새 변경 금지**를 뜻하며 자동으로 원본 쓰기를 재개하는 신호가 아니다.

### 14.3 승인과 효과의 원자성 범위

동일 플랫폼 DB 트랜잭션 안에서 승인 유효성 확인, 충돌 lock, 실행 의도, 발송 outbox를 기록한다. 외부 DB·클라우드 API 호출은 이 트랜잭션과 원자적이라고 주장하지 않는다.

```text
권한·승인 확인 + intent + outbox commit
    → 실행기가 명령 수신·중복 확인
    → 외부 효과 수행
    → 외부 결과 관측
    → 영수증 검증 및 효과 원장 갱신
```

기록 전송은 중복될 수 있다. 같은 operation ID와 input hash는 동일 논리 작업이며, input이 다르면 거부한다. 공급자가 idempotency token을 지원하지 않으면 자원 UID·태그·외부 작업 ID로 조회 가능한지 자격 시험한다. 안전한 재시도와 결과 식별이 모두 불가능한 작업은 무인 자동 실행에서 제외한다.

### 14.4 관리권 인수

처음 등록한 Terraform·Operator·기존 자동화의 자원은 관찰만 한다. 동일 자원을 여러 조정기가 경쟁적으로 변경하지 않도록 필드·자원 단위 소유권을 선언한다. 기존 자동화의 일시 정지·인계·복귀 절차를 승인받기 전에는 플랫폼이 write owner가 되지 않는다.

## 15. 처리 권한 인계 프로토콜

### 15.1 업무 권한과 실행 리스

`ExecutionLease`는 조정 명령 수행자의 독점 진행권이다. `BusinessAuthority`는 어느 환경이 업무 변경을 확정할 수 있는지에 대한 상태다. 리스 하나를 획득했다는 사실만으로 기존 DB 연결·Worker를 차단했다고 보지 않는다. 리스 기반 조정에서는 프로세스 정지와 지연으로 이전 리더의 작업이 뒤늦게 발생할 수 있으므로 실제 효과 지점의 통제가 필요하다.[R3]

정상 운영 권한을 중앙 리스의 주기적 갱신에 무조건 종속시키지 않는다. 인계 중 쓰기 차단은 명시적으로 해제할 때까지 유지되는 fence로 집행한다. 차단이 단순 TTL 만료로 사라져서는 안 된다.

### 15.2 상태 모델

계획 준비 상태와 인계 상태를 분리한다.

```text
DRAFT → DISCOVERED → COMPILED → REHEARSING → VERIFIED → APPROVED
```

각 Migration Unit의 인계 상태:

```text
SOURCE_ACTIVE
    → TARGET_PREPARED
    → SYNCING
    → CUTOVER_READY
    → QUIESCING
    → SOURCE_FENCED
    → FINAL_STATE_VERIFIED
    → TARGET_ACTIVATION_INTENT
    → TARGET_ACTIVE_OBSERVED
    → ROUTE_CONFIRMED
    → STABILIZING
    → HANDOFF_COMPLETE
```

`SAFE_HOLD`, `ABORTING_PRE_ACTIVATION`, `RESTORING_SOURCE`, `FAILBACK_REQUIRED`, `FAILED`는 별도 전이다. 경과 시간만으로 다음 단계로 승격하지 않는다.

### 15.3 단계별 계약

| 단계 | 작업 | 다음 단계 필수 조건 | 실패 시 |
|---|---|---|---|
| TARGET_PREPARED | 대상 인프라·데이터 구조·신원·관측·백업 준비 | 운영 업무 쓰기 비활성, 리허설 통과 | 승인 범위의 대상 정리 또는 보류 |
| SYNCING | 초기 복사와 변경분 적용 | 복제 범위·시작점·수신/적용 위치·보존 여유 확인 | 출발지 운영 유지, 대상 복구 |
| CUTOVER_READY | 계획·승인·자원·증거·용량 재확인 | 모든 필수 조건 유효, 지원된 budget | 전환 미시작 |
| QUIESCING | 새 요청 대기/명시적 재시도, 작업 claim 중지, 진행 작업 분류 | 모든 진입점·Worker·배치 확인 | 목적지 비활성 확인 후 안전 재개 검토 |
| SOURCE_FENCED | 실제 업무 변경 차단·기존 연결 정리 | 우회 경로 포함 거부, 차단 지속성 | 입증 불가 시 활성화 금지 |
| FINAL_STATE_VERIFIED | 최종 체크포인트 적용·업무·데이터·객체·작업 비교 | 같은 일관성 기준과 모든 필수 비교 통과 | 목적지 비활성 확인 후 원본 복원 또는 보류 |
| TARGET_ACTIVATION_INTENT | 새 세대와 활성화 의도를 영속화하고 명령 발급 | 원본 차단 증거 유효, 명령 중복 보호 | 이후 목적지 상태 불명은 post-activation 취급 |
| TARGET_ACTIVE_OBSERVED | 실제 대상 쓰기·권한·원본 차단 재확인 | 새 권한에서 업무 가능, 이전 권한 거부 | 별도 failback 또는 SAFE_HOLD |
| ROUTE_CONFIRMED | 인증된 범위별 경로 변경·잔존 연결 처리 | 실제 경로·재접속·멱등 재시도 확인 | 원본 쓰기 자동 재개 금지 |
| STABILIZING | SLO·업무·보안·감사·미완료 작업 확인 | 계약 측정 구간·조건 충족 | 승인된 대상 복구·failback |
| HANDOFF_COMPLETE | 결과·잔존 의존성·복귀 상태 기록 | 완료 조건·필수 증거 충족 | 원본 삭제는 별도 승인 |

### 15.4 활성화 확정 경계

`TARGET_ACTIVATION_INTENT`가 내구성 있게 기록되고 명령이 외부에 전달될 수 있는 시점부터, 응답이 없어도 목적지가 활성화됐을 가능성을 고려한다. “아직 첫 쓰기 ACK를 못 봤다”는 사실은 쓰기가 없었다는 증거가 아니다.

출발지 재활성화를 하려면 목적지 비활성·미확정 효과의 해소를 증명하거나, 목적지 신규 데이터를 포함한 별도 failback 계획을 수행해야 한다. 단순히 중앙 상태를 이전 단계로 되돌리지 않는다.

### 15.5 실효 fence 요구

DB 어댑터는 새 로그인 금지뿐 아니라 기존 세션·prepared transaction·공용 역할·직접 접근·배치를 검토해야 한다. Worker는 새로운 claim을 막는 것과 이미 claim한 작업이 늦게 효과를 만드는 것을 구분해야 한다. 외부 효과는 상대 시스템의 멱등 키·조회·중앙 customer-owned gateway 등 해당 프로필이 보장하는 경로로 통제한다.

경로 변경은 fence를 대체하지 않는다. 오래된 요청이 원본으로 도착하더라도 원본은 변경을 확정하지 못해야 한다. 반대로 목적지 읽기 트래픽을 먼저 보내면서 업무 쓰기를 원본 DB로 유지하는 전략은 허용할 수 있으나, 그 상태를 데이터 인계 완료·출발지 독립 완료로 표시하지 않는다.

### 15.6 재시작·스냅샷 복원

원본 DB나 실행기를 과거 스냅샷에서 복원해 과거 권한이 되살아나는 경우를 시험한다. 재기동·복원 경로는 `QUARANTINED` 상태에서 시작해 현재 권한 세대·자원 신원·경로를 확인하기 전 업무 쓰기를 열지 않는 자격 요건을 둔다.

플랫폼 권한 DB 자체가 과거 시점으로 복원되면 기존 epoch 값만으로 계속 명령을 발급하지 않는다. 독립된 복구 절차로 현장에 발급된 권한과 상태를 대조하고, 새로운 recovery incarnation과 권한 갱신을 완료한 후 재개한다. 오래된 클러스터·키·명령을 격리할 수 없으면 변경을 보류한다.

---

## 16. 데이터 이전과 정합성 검증

### 16.1 데이터 전략

| 전략 | 적합 조건 | 한계·필수 검사 |
|---|---|---|
| Snapshot + CDC | 변경분 적용·실제 차단·최종 비교 가능 | CDC 범위·시작점·적용 위치·DDL·복구 |
| Offline Copy | 승인된 정지 창 존재 | 정지 시간·최종 일관성·재개 절차 |
| Backup / Restore | 복구 포맷·버전·키 접근 호환 | 복원 시간·새 변경분·권한·확장 |
| Application Replay | 업무 이벤트와 재실행 의미가 정의됨 | 순서·중복·외부 효과·스키마 진화 |
| Data Transform | 변환 의미·검증 oracle 존재 | 비가역성·정밀도·인코딩·역변환 |

“RDS”와 “자체 PostgreSQL”은 같은 엔진 계열일 수 있어도 관리 권한·복제 옵션·확장·백업 책임이 다르므로 방향별 qualification이 필요하다. 엔진이 같다는 이유로 동일 계획을 자동 선택하지 않는다.

### 16.2 체크포인트와 비교

복제 지연 수치 하나가 0이라는 이유로 전환하지 않는다. 같은 publisher·subscription·단위·적용 세대에 속하는 최종 체크포인트가 목적지에 적용됐음을 확인한다. 소스 WAL 위치와 목적지 자체 WAL 위치를 같은 좌표로 수치 비교하지 않는다.

여러 DB·객체 저장소·큐를 포함하면 `checkpoint vector`와 그 벡터를 같은 업무 경계로 만드는 절차를 정의한다. 각자 최신 값만 모은 벡터가 원자적 업무 시점을 보장하지는 않는다. 공통 quiesce·업무 완료 기록·참조 manifest 등 별도 근거가 필요하다.

### 16.3 검증 수준

| 수준 | 확인 내용 | 허용 주장 |
|---|---|---|
| Connectivity | 연결·권한·기본 쿼리 | 연결 가능 |
| Schema | 테이블·타입·제약·확장·collation 등 | 정의된 호환성 충족 |
| Structural | 행 수·키 집합·객체 수 | 구조상 비교 결과 |
| Content | 정해진 snapshot·직렬화 규칙의 내용 비교 | 비교 범위의 내용 일치 |
| Business | 주문·재고·작업·첨부 불변조건 | 계약의 업무 규칙 충족 |
| Outcome | 클라이언트 결과와 최종 확정 효과 대조 | 시험 범위의 유실·중복 측정 |

해시·행 수만으로 업무 의미를 전부 보장하지 않는다. 표본 검증은 표본임을 표시한다. 전체 비교는 같은 논리 시점·타입 직렬화·NULL·시간대·정렬 규칙을 사용해야 한다.

### 16.4 전환 시간 예산

```text
write_pause_budget
 ≥ admission_quiesce
 + in_flight_resolution
 + source_fence_confirmation
 + final_sync
 + final_consistency_checks
 + target_activation
 + route_and_connection_confirmation
```

전체 해시 비교를 짧은 정지 구간에 넣을 수 있는지는 데이터 크기·부하·네트워크를 포함해 실측한다. 사전 일관 snapshot 검증 + 이후 변경분 검증을 사용하려면 그 사이 변경을 빠짐없이 추적하고 누락을 감지할 수 있어야 한다. 그렇지 않으면 정지 시간을 늘리거나 지원 범위를 줄인다. 변경 후 계약 기준을 낮춰 측정을 통과한 것처럼 보이지 않는다.

### 16.5 PostgreSQL 프로필

공식 PostgreSQL 논리 복제 문서의 DDL·sequence state·large object 제약을 지원 명세에 반영한다.[R1] 초기 스키마 준비, 스키마 변경 통제, replica identity, extension·권한·collation, sequence 보정, 장기 트랜잭션, 복제 슬롯·WAL 잔량, subscriber 적용 상태를 검사한다.

역방향 복제는 명령 한 줄로 전제하지 않는다. 시작 위치, 이미 존재하는 데이터, initial copy 여부, replication origin·루프·충돌, 시퀀스, 신규 테이블·DDL, 운영 권한과 실패 복구를 시험한다. DB 대규모 버전 업그레이드가 포함되면 역방향 호환성을 별도로 평가한다.

### 16.6 DB와 객체 저장소

첨부 manifest에 업무 참조 ID, 객체 key·version, 크기, 명시적 checksum 알고리즘·값, content type, 암호화·권한·보존 속성, 상태를 기록한다. DB 참조가 가리키는 실제 객체가 목적지에 있고 읽을 권한도 맞는지 확인한다.

객체 수만 같다고 통과하지 않는다. 대형 객체·multipart 업로드·삭제 표식·버전·미완료 업로드·signed URL·서버 측 암호화 키 의존성을 다룬다. checksum 의미는 공급자 문서와 검증 계약에 따라 고정한다.[R11] 파일 이전을 지원하지 않는 경로는 신규 첨부를 통제하거나 명시적으로 부분 이전으로 제한해야 한다.

---

## 17. 미완료 업무·이벤트·외부 효과

### 17.1 업무 재개 의미

프로세스 메모리를 이동하지 않는다. 다음 영속 상태를 인계한다.

`business_id`, `step_id`, 입력 해시, 현재 업무 상태, 완료 단계, 외부 작업 ID, 재시도 정책, claim 소유권·세대, idempotency result, outbox·consumer inbox, 미확정 효과 목록.

업무 API가 완료했다고 응답한 단계는 재시작 후에도 조회 가능해야 한다. 접수만 확인한 단계는 이후 처리 의무를 가진 내구성 있는 작업으로 보존한다.

### 17.2 큐 이전

큐 깊이 0은 모든 작업이 처리됐다는 충분조건이 아니다. 이미 소비자가 가져갔지만 ACK하지 않은 작업·지연 메시지·DLQ·재전달·배치·scheduler를 포함한다.

지원 전략은 `drain_then_switch`, `durable_work_ledger_replay`, `validated_bridge`, `broker_native_transfer` 등으로 분리한다. 메시지 ID, ordering key, delivery·ack 의미, 중복 방지 TTL, consumer offset, inflight visibility, DLQ 처리를 명시한다. 재전달 의미가 다른 큐를 단순 endpoint 치환하지 않는다.

### 17.3 Transactional Outbox

지원되는 업무 모델에서는 업무 상태와 발행 대기 이벤트를 같은 DB 트랜잭션에 기록한다. 발행과 소비는 중복 가능성을 전제로 한다. 소비자는 멱등 키·입력 해시·완료 결과를 보관한다. 이 패턴은 DB와 이벤트의 이중 쓰기 위험을 다루지만 외부 전송의 정확히 한 번 실행을 일반적으로 보장하지 않는다.[R6]

### 17.4 외부 효과

| 관측 상태 | 처리 |
|---|---|
| 효과 미실행이 확인됨 | 승인된 재시도 가능 |
| 효과 실행이 확인됨 | 같은 결과로 기록·재사용 |
| 외부 결과 조회 가능 | business_id / provider_operation_id로 확인 |
| 조회 불가·중복 피해 가능 | 자동 재실행 금지, 업무 보류·사람 확인 |
| 취소·반환 가능한 효과 | 독립 승인된 보상 계획 사용 |
| 비가역 효과 | 완료 기록·감사 유지, 원복으로 표현하지 않음 |

플랫폼의 Temporal workflow history와 고객 업무의 작업 원장을 혼동하지 않는다. 플랫폼이 워크플로 엔진을 사용한다고 고객 앱의 모든 상태가 자동으로 이전되는 것은 아니다.

---

## 18. 라우팅·네트워크·세션

### 18.1 라우팅 계약

업무 범위는 서버가 인증·인가한 신원에서 결정한다. 클라이언트가 임의 header로 다른 고객사 경로를 선택해서는 안 된다. 매핑에는 `unit_id`, `owner_environment`, `epoch`, `route_revision`, endpoint identity를 포함한다.

동일 업무의 변경 요청을 서로 독립된 쓰기 환경으로 무작위 분배하지 않는다. 10→50→100% 트래픽 전환은 같은 권한/DB를 공유하는 준비 구간이나 분리된 단위에 한해서만 사용한다. 라우팅이 오래되어도 원본 fence가 업무 쓰기를 거부해야 한다.

### 18.2 연결과 읽기

기존 WebSocket·장기 요청은 drain·종료·재연결을 계약으로 처리한다. 세션 재개 토큰과 마지막 수신 위치는 실제 commit 순서·전달 의미에 맞춰 정의한다. 단순 증가 ID가 항상 commit 순서를 나타낸다고 가정하지 않는다.

전환 중 목적지에 새 쓰기가 발생한 후 원본 read replica에서 오래된 값을 읽을 수 있으므로, read-your-writes가 필요한 세션의 경로·checkpoint를 관리하거나 명시적 재시도를 반환한다. 목적지 활성화 뒤 기존 source read 경로를 안전성 검증 없이 유지하지 않는다.

### 18.3 연결 조사

방화벽·NAT·DNS·mTLS·CA·MTU·대역폭·직접/중계 경로·패킷 손실·egress 제한을 조사한다. 운영 데이터 경로, 복제 경로, 관리 명령 경로, AI egress를 분리한다. 연결이 된다거나 tunnel이 있다는 사실만으로 데이터 위치·접근 통제가 충족된다고 판단하지 않는다.

---

## 19. 취소·실패·복귀

### 19.1 작업별 실패 분류

| 코드 | 의미 | 기본 행동 |
|---|---|---|
| PRECONDITION_FAILED | 실행 전 필수 상태가 아님 | 변경 미실행·재조사 |
| EFFECT_NOT_APPLIED | 외부 미실행 확인 | 승인된 재시도 또는 안전 취소 |
| EFFECT_CONFIRMED | 사후조건 확인 | 다음 게이트 진행 |
| EFFECT_UNKNOWN | 실행 여부·범위 불명 | 조회·보류, 맹목 재실행 금지 |
| PARTIAL_EFFECT | 일부 자원에만 효과 | 부분 원장 기록·범위별 복구 |
| SECURITY_VIOLATION | 비인가 실행·격리 위반 | 권한 회수·증거 보존·범위 격리 |
| CONTRACT_BREACH | 데이터·SLO·업무 조건 위반 | 완료 표시 금지·승인된 복구 |

### 19.2 단계별 취소

준비 단계에서는 소유가 확인된 임시 자원만 삭제할 수 있다. 원본 fence 이후에는 목적지 활성화 가능성을 먼저 확인한다. 활성화 의도 발급 후 취소는 단순 중단 플래그가 아니라, 실제 목적지 상태 확인과 별도 복구 요구다.

namespace·VM·버킷 정리는 생성 UID·소유 태그·plan/run scope에 결속한다. 이름만 같다는 이유로 대체 자원을 삭제하지 않는다. 정리 결과가 미확인인 경우 자원·비밀 잔존을 기록하고 완료 상태와 구분한다.

### 19.3 복귀 유형

| 유형 | 조건 | 보장 |
|---|---|---|
| Abort before activation | 목적지 업무 활성화가 없음을 확인 | 안전한 원본 복원 가능성 평가 |
| Clean failback | 목적지 접근·원본 동기화·최종 차단 가능 | 검증된 신규 데이터까지 회수 |
| Reverse migration | 현재 목적지에서 새 계획으로 복귀 | 현재 호환성과 계약 기준으로 재평가 |
| Disaster recovery | 원본 또는 목적지 접근 불가·손상 | 별도 복구 지점·손실 승인·업무 보상 |
| SAFE_HOLD | 권한·효과·데이터 상태 불명 | 관련 충돌 변경 보류 |

DNS 원복을 데이터 원복으로 표현하지 않는다. 목적지 장애 직전 ACK된 데이터가 원본에 복제됐다는 증거가 없으면 무손실 failback을 주장하지 않는다.

### 19.4 복귀 가능성 계약

복귀 가능 여부는 기간만이 아니라 원본 상태, 역방향 동기화, 앱·스키마·파일 호환성, 신원·키, 마지막 복원 시험, 현재 데이터 보존 조건의 조합이다. 신규 배포가 이 계약을 깨면 차단 또는 복귀 보장 종료 승인을 요구한다. 보안 패치와 복귀 호환성이 충돌하면 위험을 비교해 새 복구 경로를 마련하거나 보장 종료를 승인받는다. 취약한 원본을 계속 살려두는 것을 기본 해법으로 삼지 않는다.

---

## 20. 출발지와 플랫폼 자체에서의 독립 운영

### 20.1 출발지 독립성

단일 `independent=true` 대신 범위를 나눈다.

| 상태 | 검증 범위 |
|---|---|
| COMPUTE_INDEPENDENT | 원본 compute 없이 실행 |
| DATA_IDENTITY_INDEPENDENT | 계약의 원본 DB·객체·Secret·신원 의존 제거 |
| RESTART_VERIFIED | 새 인스턴스·연결·토큰·비밀 갱신 성공 |
| OPERATIONS_VERIFIED | 배치·백업·복원·감사·신규 배포 시험 |
| PROVIDER_EXIT_VERIFIED | 계약상 해당 사업자 필수 의존 제거 |

출발지 접근 차단 시험은 승인된 범위에서 수행한다. 기존 메모리·연결·cached Secret에 기대지 않도록 새 신원 세션·새 앱 인스턴스를 사용한다. 원본 복제 채널을 복귀용으로 유지한다면 “일상 업무 경로의 독립성”과 “완전한 사업자 탈출”을 구분한다.

### 20.2 플랫폼 탈출

실제 적용된 IaC·매니페스트·이미지·SBOM·비밀 참조·권한 소유권·백업·복구·운영 절차를 고객 소유 형식으로 인계한다. 플랫폼이 소유한 IdP·키 발급·프록시가 필수 경로라면 대체 경로를 먼저 준비한다.

플랫폼 권한 회수·에이전트 종료 후 신규 배포·새 연결·비밀 회전·데이터 저장·복구 시험을 수행한다. 이미 떠 있는 Pod가 계속 HTTP 응답하는 것만으로 플랫폼 독립성을 판정하지 않는다. 검증된 운영 설정을 동결해 남길 수 있지만 고객이 직접 수정·배포·복구할 수 있어야 한다.

---

## 21. AI 수정·패치·리허설

### 21.1 권한

AI는 의존성 후보, 변경안, 코드 diff, 테스트 후보, 원인 설명을 생성한다. 정책·승인·최종 보안 판정·운영 credential 발급·실제 데이터 전환 권한은 없다. OPA 등 정책 엔진은 판단점이고 도구 harness·실행기·Admission 등 별도 집행점이 거부를 강제해야 한다.[R7]

### 21.2 격리 실행

신뢰하지 않는 소스·빌드 스크립트·AI 생성 코드에는 운영 환경과 분리된 일회성 VM 또는 자격 시험을 통과한 sandbox runtime을 사용한다. namespace·RBAC·NetworkPolicy는 유용한 계층이지만 공유 노드의 모든 보안 경계를 대신하지 않는다.[R8]

원본은 읽기 전용 snapshot, 데이터는 합성·승인된 비식별 데이터, 비밀은 시험용 참조로 제한한다. 패키지·모델·도구 egress는 필요한 경로만 별도 프록시로 허용한다. CPU·메모리·스토리지·네트워크·실행 시간·토큰·비용을 제한한다. 금지 shell 문자열 목록만으로 격리를 주장하지 않는다.

### 21.3 검증 독립성

AI가 작성한 테스트와 플랫폼이 관리하는 수락 시험을 구분한다. 비공개 테스트를 사용하더라도 비공개라는 사실만으로 시험 품질이나 격리가 입증되지는 않는다. 테스트 코드·runner 이미지·권한·결과 저장 경로를 AI 수정 권한 밖에 두고 실제 결과를 독립 수집한다.

실행·기능 통과와 보안 통과를 분리한다. 기존 Secret 접근을 고치면서 권한을 넓힌 수정은 업무가 동작해도 거부한다. AI가 만든 변경과 실제 build artifact가 달라지면 재빌드·재검증·재승인을 요구한다.

### 21.4 지속 패치

취약점·설정 드리프트·지원 종료·정책 변경은 remediation plan을 만든다. 패치가 목적지 호환성·복귀 가능성·데이터 의미에 미치는 영향을 검사한다. 패치와 이전은 같은 권한·실행·증거 체계를 재사용하되 위험·수락 조건은 작업별로 다르게 둔다. 자동 탐지와 자동 운영 적용을 동일 기능으로 취급하지 않는다.

## 22. 보안·거버넌스 통제

### 22.1 보호 요구의 비교

보안 게이트는 `원본과 결과가 같음`이 아니라 **절대 필수 요구 충족 + 승인된 비퇴행 조건 + 잔여 위험 확인**으로 판정한다. 원본과 목적지가 모두 취약한 경우 PASS가 아니다. 목적지가 더 강한 통제를 제공해도 필수 업무가 깨지면 성공이 아니다.

| 통제 ID | 보호 요구 | 필수 시험 예 |
|---|---|---|
| SEC-01 | 고객·서비스별 최소 권한 | 다른 고객·앱·자원에 대한 실제 거부 |
| SEC-02 | 승인된 실행만 허용 | 대상·이미지·정책·권한·만료 변조 거부 |
| SEC-03 | 비밀 원문 보호 | 코드·로그·state·AI 입력·산출물 누출 검사 |
| SEC-04 | 신원·전송 보호 | 상대 신원·CA·hostname·허용 경로·토큰 audience 검증 |
| SEC-05 | 실행 격리 | sandbox 밖 접근·노드 권한·운영 credential 차단 |
| SEC-06 | 신뢰된 공급망 | digest·출처·서명 identity/issuer·SBOM·검사 결과 |
| SEC-07 | 감사·증거 보존 | 위변조·삭제 거부, 누락·재생·교차 run 사용 검출 |
| SEC-08 | 보호된 복구 자산 | 실행 역할의 백업 삭제 거부, 실제 복원 |
| SEC-09 | 안전한 API·콘솔 | 객체별 인가·입력 검증·세션·CSRF·출력 처리 |
| SEC-10 | 비용·가용성 남용 제한 | 조직별 요청·AI·VM·동시 작업·egress quota |
| SEC-11 | 데이터 위치 | 업무·백업·로그·AI 입력 경로 모두 확인 |
| SEC-12 | 사고 격리·회복 | 영향 범위·권한 회수·깨끗한 재구축·재검증 |

이 표의 ID는 본 명세 내부 ID다. 기존 문서의 동명 ID와 자동 동일시하지 않으며 통합 시 대응표를 만든다.

### 22.2 신원과 자격증명

클라우드는 단기 위임 신원을 우선 사용하고 고객·서비스·작업별 권한을 제한한다. 온프레미스는 customer-owned identity와 mTLS 등 검증된 경로를 사용한다. SPIFFE/SPIRE는 워크로드 신원 선택지이며, 신원 인증과 어떤 업무·자원에 접근할 수 있는지의 인가는 별도다.[R10]

장기 키가 필수인 외부 시스템은 예외로 등록하고 secret store·회전·노출 범위·철회 한계를 기록한다. 단기 토큰도 이미 열린 세션·외부 API 권한을 즉시 회수한다고 가정하지 않는다. 회수 효과와 지연을 실제 확인한다.

### 22.3 공격 입력과 공급망

소스 저장소·URL 조사 기능은 외부 입력을 통해 내부 metadata·사설 관리 주소·비밀을 읽지 못하도록 목적지 검증·egress·redirect 제한을 적용한다. 로그·diff·AI 리포트는 안전하게 렌더링한다. Tenant ID를 요청 본문에서 믿지 않고 인증 주체와 서버 측 자원 소유권으로 결정한다.

아티팩트는 digest·빌드 출처·승인된 서명 주체에 결속한다. Sigstore 검증은 기대한 identity와 issuer를 제한하는 방식으로 사용할 수 있다.[R12] 서명은 출처·무결성 근거이지 코드가 악성이 아니라는 보증이 아니다.

### 22.4 사고 대응

침해 의심 환경을 목적지로 그대로 복제하는 것을 복구로 취급하지 않는다. 데이터·백업·이미지 오염 여부를 검토하고, 원본에서 빌드한 아티팩트를 재사용할지 별도 판정한다. 격리 범위는 의존성 그래프와 실제 권한 경계로 계산한다. 잘못된 격리가 데이터 회수 경로를 끊을 수 있으므로 자동 대응 범위·예외·복구를 사전 승인한다.

---

## 23. Evidence Contract와 지속 재검증

### 23.1 필수 필드

```yaml
kind: Evidence
metadata:
  id: ev-target-writer-denial
  tenant_id: org-example
  schema_version: '1'
spec:
  subject:
    environment_id: env-source
    logical_resource_id: orders-db-a
    resource_uid: source-db-verified-uid
    resource_revision: observed-revision
  binding:
    plan_revision: 12
    plan_digest: sha256:example-only
    execution_id: run-example
    attempt_id: attempt-3
    migration_unit_id: orders-customer-a
    authority_epoch: '42'
  assertion:
    predicate: stale_source_writer_cannot_commit
    observed: true
    decision: PASS
  provenance:
    verifier_identity: independent-verifier
    verifier_image_digest: sha256:example-only
    test_definition_digest: sha256:example-only
    challenge_id: fresh-run-scoped-challenge
  freshness:
    observed_at: '2026-11-06T00:00:00Z'
    expires_at: '2026-11-06T00:05:00Z'
    invalidated_by: [writer-policy-change, identity-change, resource-recreation]
  coverage:
    included_paths: [api-db-role, worker-db-role]
    unresolved_paths: []
  artifacts:
    - protected-audit-object-reference
  signature_ref: evidence-signature-reference
```

예시 시각·digest는 실측 결과가 아니다. 실제 프로토콜은 canonical encoding, trusted signer, key validity, audience, nonce, 중복 수신 정책과 저장 원본을 정의해야 한다.

### 23.2 판정 규칙

`PASS`, `FAIL`, `UNKNOWN`, `STALE`을 구분한다. 서명·신원·plan/unit/resource binding 검증에 실패하면 증거를 거부한다. 서명이 맞아도 measurement가 검증 요건을 충족하지 못하면 PASS가 아니다.

증거 TTL 안에서도 관련 자원·정책·이미지가 바뀌면 무효다. 동적 상태는 조회 직후 바뀔 수 있으므로 실행기 guard·version 조건·fence와 결합해야 한다. 전후 UID 조회만으로 조회 사이의 변경을 원자적으로 막았다고 주장하지 않는다.

검증기의 서명은 “누가 무엇을 보고했는지”를 뒷받침할 뿐, 손상된 verifier가 절대 거짓말하지 않는다는 보장은 아니다. 중요한 권한·데이터 결과는 독립 관측과 서로 다른 근거를 교차 확인한다.

### 23.3 변경 영향

증거마다 코드·스키마·자원·정책·어댑터·환경 프로필의 의존성을 인덱싱한다. 관련 변경이 발생하면 기존 판정·승인을 재평가한다. 영향 분석이 불완전하면 더 넓은 범위의 증거를 무효화한다. 이벤트 수집 장애로 변경 여부를 알 수 없으면 그 관측에 의존하는 동적 안전 조건을 UNKNOWN으로 둔다.

정상 운영의 새 취약점 발견과 진행 중 인계의 안전성 위반은 대응이 다르다. 새 변경을 차단하는 것과 현재 서비스를 즉시 끄는 것을 동일시하지 않는다. 정책에 명시된 영향·우선순위·긴급 대응에 따라 처리한다.

### 23.4 감사 보존

중요 명령·승인·권한 전이·증거를 별도 권한 경계의 append-only/WORM 저장소에 보존한다. S3 Object Lock은 선택 가능한 WORM 수단이다.[R13] 보존 모드·기간·버전·삭제 및 우회 권한을 실제 시험한다.

암호화 키 삭제, 로그 생성 자체의 누락, 고객 원문 과다 수집은 WORM만으로 해결되지 않는다. 키 보존·복구 권한·수집 실패 경보·개인정보 최소화를 함께 정의한다. 감사 저장 장애 시 실행 의도 기록을 보장하지 못하면 새 고위험 변경을 시작하지 않는다. 이미 발생한 효과는 현장 내구성 기록과 재조정 절차로 회수한다.

---

## 24. 외부 API 명세

### 24.1 공통 규칙

API는 OIDC 등 사용자 인증과 객체별 인가를 요구한다. 플랫폼 서비스 간 호출은 별도 workload identity를 검증한다. 모든 비동기 상태 변경 요청은 `Idempotency-Key`를 요구한다. 같은 key·같은 입력은 기존 작업 ID를 반환하고, 같은 key·다른 정규화 입력은 `409 IDEMPOTENCY_CONFLICT`다.

가변 모델 변경은 `If-Match`/revision을 요구하며 오래된 revision은 `412 REVISION_MISMATCH`다. 승인된 PlanRevision은 불변이고 수정 시 새 revision을 생성한다. 실행 상태·Evidence의 PASS를 클라이언트가 직접 설정하는 API는 제공하지 않는다.

### 24.2 엔드포인트

| Method / Path | 입력 | 응답·의미 |
|---|---|---|
| POST /v1/environments | profile, connection_ref | environment_id, 연결 점검 job |
| POST /v1/environments/{id}/observations | 조사 scope | observation_id, 조사 job |
| POST /v1/services | 계약·소유자·소스 참조 | service_id, revision |
| PATCH /v1/services/{id}/contracts/{revision} | 변경 요구·If-Match | 새 계약 revision |
| POST /v1/services/{id}/discoveries | 코드/환경/관측 창 | discovery_id |
| GET /v1/discoveries/{id} | — | graph snapshot, coverage, unknowns |
| POST /v1/services/{id}/units:analyze | 업무·분할 후보 | units, 합침·변경·보류 사유 |
| POST /v1/candidates:assess | 계약·snapshot·후보 | 가능성·근거·비용 범위 |
| POST /v1/plans:compile | 불변 input refs | compile_id, 비동기 결과 |
| GET /v1/plans/{id}/revisions/{rev} | — | IR, blockers, explanation, digest |
| POST /v1/plans/{id}/rehearsals | revision, trial scope | rehearsal_id |
| POST /v1/plans/{id}/approvals | revision, scope, expires | approval_id, 결속 값 |
| POST /v1/approvals/{id}:revoke | 사유 | 이후 명령 제한·진행 효과 재조정 |
| POST /v1/plans/{id}/executions | revision, approvals, request_id | execution_id, 202 |
| GET /v1/executions/{id} | — | 단계·효과·권한·증거·보류 사유 |
| POST /v1/executions/{id}:pause | 사유 | 안전 체크포인트 정지 요청 |
| POST /v1/executions/{id}:cancel | 사유 | 단계별 취소/복구 필요 판정 |
| POST /v1/executions/{id}:reconcile | 조회·복구 scope | 재조정 job; 강제 성공 아님 |
| POST /v1/executions/{id}/failback-plans | 현재 상태·복귀 조건 | 별도 plan revision |
| POST /v1/services/{id}/exit-drills | exit contract, approval | 범위 제한 독립성 시험 |
| GET /v1/services/{id}/readiness | — | 실행·업무·보안·독립성·복귀 분리 상태 |
| GET /v1/executions/{id}/events | Last-Event-ID | SSE·순번·재접속 |

### 24.3 컴파일 요청·실패 응답 예

```json
{
  "service_contract_ref": "orders@7",
  "dependency_snapshot_ref": "graph@24",
  "source_environment_ref": "env-source@9",
  "target_environment_ref": "env-target@4",
  "migration_units": ["orders-customer-a@3"],
  "profile": "PLANNED_SINGLE_AUTHORITY",
  "constraints": {"permit_requirement_relaxation": false},
  "qualification_policy_ref": "qualified-only@2"
}
```

```json
{
  "compile_id": "compile-example",
  "status": "NEEDS_EVIDENCE",
  "executable": false,
  "blockers": [
    {
      "code": "FENCE_SCOPE_UNPROVEN",
      "unit_id": "orders-customer-a",
      "requirement_id": "INV-01",
      "observed": "shared database role also used by customer-b",
      "required_action": "prove partition-scoped enforcement or merge units"
    }
  ],
  "approval_available": false
}
```

### 24.4 오류 코드

`UNAUTHORIZED`, `FORBIDDEN`, `TENANT_SCOPE_MISMATCH`, `REVISION_MISMATCH`, `IDEMPOTENCY_CONFLICT`, `CAPABILITY_UNSUPPORTED`, `QUALIFICATION_EXPIRED`, `BOUNDARY_UNPROVEN`, `EVIDENCE_UNKNOWN`, `EVIDENCE_EXPIRED`, `APPROVAL_MISMATCH`, `RESOURCE_DRIFT`, `OWNERSHIP_CONFLICT`, `STALE_EPOCH`, `ACTIVATION_UNCERTAIN`, `RECOVERY_REQUIRED`, `QUOTA_EXCEEDED`를 사용한다.

SSE는 UI 전달 수단이지 권한 원장이 아니다. 이벤트 누락·보존 기간 초과 시 snapshot 재조회와 gap 표시를 제공한다. 고객·계획 간 cursor를 재사용해 정보가 노출되지 않게 한다.

---

## 25. 저장소·일관성·원장 설계

### 25.1 테이블 그룹

| 그룹 | 주요 테이블 |
|---|---|
| 조직·접근 | tenants, principals, role_bindings, access_policies |
| 모델 | environments, service_contracts, graph_snapshots, migration_units |
| 능력 | adapters, capabilities, strategy_qualifications |
| 계획 | plan_revisions, operations, operation_dependencies, artifact_refs |
| 권한 | approvals, exceptions, authority_records, execution_grants, scope_locks |
| 실행 | executions, operation_intents, operation_attempts, effect_observations, command_outbox |
| 검증 | evidence_metadata, evidence_dependencies, gate_decisions, invalidations |
| 운영 | recovery_contracts, exit_contracts, cost_estimates, audit_index |

대형 로그·정합성 manifest·SBOM·원본 증거는 보호된 객체 저장소에 두고 DB에는 digest·보존·소유권·색인을 저장한다. Secret 원문은 별도 비밀 저장소에만 둔다.

### 25.2 제약

`tenant+logical_operation_id`는 고유하고, 해당 키에 입력 digest를 결속한다. approval은 immutable plan_revision을 참조한다. evidence는 subject·run·attempt와 연결한다. authority_record는 unit별 하나의 현재 조정 상태를 갖고 CAS·트랜잭션 잠금으로 수정한다.

단순 UNIQUE(unit_id)는 겹치는 두 unit의 안전성을 해결하지 않는다. 부모/자식·공유 데이터·역할·네트워크 정책의 범위를 정규화한 충돌 검사와 함께 lock을 획득해야 한다. 권한 DB의 원자성은 실제 두 외부 DB의 원자성을 대신하지 않는다.

### 25.3 보존·삭제

작업 멱등 기록은 최대 재시도·복구·failback 기간보다 짧게 지우지 않는다. 정리와 원본 삭제는 이력·백업·법적 보존·고객 요구를 확인한 별도 계획이다. tenant 삭제는 운영 자원 접근 회수·미완료 명령·키·감사 보존까지 포함하며, 단순 DB cascade로 처리하지 않는다.

---

## 26. 배치·고가용성·규모 확장

### 26.1 기준 배치

중앙 API·compiler worker·reconciler·workflow worker·evidence collector는 책임별로 분리한다. 논리 컴포넌트 모두를 처음부터 독립 마이크로서비스로 배포할 필요는 없지만, 신뢰 경계가 다른 API·운영 실행기·AI sandbox는 권한과 실행 환경을 분리해야 한다.

중앙 state DB는 복구·이중 리더 방지·백업 시험을 포함하는 HA 구성을 사용한다. 고객 업무 DB와 공유 관리 권한으로 묶지 않는다. 지역별 executor와 verifier를 배치해 대규모 데이터가 중앙 플랫폼을 경유하지 않게 한다.

### 26.2 확장 단위

조직·지역·신뢰 요구별 control cell을 둘 수 있다. 각 unit은 한 권한 cell에 소속되며 cell 자체의 소유권 이동도 별도 프로토콜을 필요로 한다. 조직별 queue·worker 동시성·비밀·증거 경계를 분리한다. 무작정 다중 리전 active-active 권한 DB를 채택하지 않는다.

### 26.3 장애별 기본 동작

| 장애 | 기본 동작 |
|---|---|
| API/Console 장애 | 기존 업무 지속, 새 조작 제한 |
| Compiler 장애 | 운영 효과 없음, 동결 입력으로 재실행 |
| Workflow worker 장애 | intent·실제 상태 기반 재개 |
| Authority DB 접근 불가 | 새 권한 발급·인계 승격 금지 |
| Executor 장애 | 현장 기록·effect 조회, 중복 변경 제한 |
| Verifier 장애 | 필수 검증 UNKNOWN, 운영 게이트 미통과 |
| 관측 백엔드 장애 | UI 지연 표시, 관련 증거 신뢰성 재평가 |
| Secret/identity 발급 장애 | 갱신 한계 내 운영, 서비스 계약에 따라 보류·경보 |
| 출발지 단절 | 실제 차단·데이터 확인 불가 시 무손실 활성화 금지 |

### 26.4 비기능 목표의 수립

플랫폼 API 가용성, 권한 원장 RPO/RTO, 계획 생성 지연, 증거 수집 지연, 지원 자원 수·그래프 크기·동시 실행 수를 qualification profile로 관리한다. 본 명세는 실측 없이 특정 TPS나 가용성 숫자를 제품 보장으로 확정하지 않는다.

쓰기 정지·요청 실패·p95/p99·복구 시간은 **관리 대상 서비스 계약**과 **실행 경로 자격 시험**에 결속한다. 시연 조건에서 5초가 나왔다고 모든 데이터 크기·리전·작업에 적용하지 않는다.

---

## 27. 관측·비용·콘솔

### 27.1 공통 상관관계

`tenant_id`, `service_id`, `unit_id`, `plan_id/revision`, `execution_id`, `operation_id`, `attempt_id`, `business_id`, `trace_id`, `authority_epoch`, source/target resource identity를 연결한다. 모든 ID를 metrics label로 넣지 않는다. 고카디널리티 업무 ID는 trace·log·전용 검증 원장에 둔다.

OpenTelemetry는 signal 수집·처리·export 계층으로 사용할 수 있다. 저장·검색·보존·불변성은 별도 설계다.[R14] sampling된 trace만으로 업무 유실 0을 증명하지 않는다.

### 27.2 지표

| 지표 | 정의 |
|---|---|
| write_admission_pause_ms | 새 업무 요청을 정상 접수하지 못하는 측정 구간 |
| business_completion_delay | 계약상 완료 응답까지 지연 분포 |
| logical_failure_count | 요청 ID 기준 최종 실패. 개별 재시도 오류와 구분 |
| completed_write_loss | 완료 ACK된 요청 중 최종 효과 누락 |
| duplicate_business_effect | 하나의 논리 요청에 중복 업무 효과 발생 |
| unknown_outcome_count | 완료 여부를 확인하지 못한 요청·외부 효과 |
| source_fence_latency | 차단 요청부터 실제 거부 증거 확보까지 |
| replication_apply_lag | 계약의 publisher 좌표에 대한 적용 지연 |
| in_flight_work_count | 인계 범위의 진행 작업·미확정 외부 효과 |
| security_regression_count | 필수 보호 요구의 실패·미확인 |
| control_plane_dependency | 중앙 단절 상태에서 운영 시험별 성공·실패 |

`0건`은 전체 검증 집합·측정 기간·미확인 수와 함께 표시한다. 미확인 요청을 표본에서 제외해 0건을 만들지 않는다.

### 27.3 비용

사전 조사, sandbox·LLM, 초기 전송·증분 전송, 병행 운영, 복귀용 원본 보존, 목적지 관리·패치·백업·라이선스·네트워크·인력 가정, 최종 정리 비용을 나눈다. 온프레미스를 무료로 보지 않는다. 금액은 출처·통화·기간·가격 snapshot·불확실성 범위를 표시한다.

배치 최적화는 필수 조건 통과 후 수행하고, 최소 유지 기간·이전 비용·재이전 위험·관측 안정 구간을 둬 작은 가격 변동마다 이동하지 않도록 한다.

### 27.4 콘솔

상태·보안·비용·AI 리소스 화면은 동일한 서비스 모델을 사용한다. 이전 애니메이션은 실제 execution event에 연결하고 예측·모의·실측을 구분한다.

최소 화면은 서비스 요구와 의존성, 환경별 능력 차이, 차단 사유, AI diff·검증 결과, 승인된 범위, unit별 권한 소유자·epoch, 복제·업무 지표, 효과 불명과 복구, 독립성·복귀 가능성, 증거 만료·잔존 의존성을 포함한다.

---

## 28. 자격 시험·수락 기준

### 28.1 검증 단계

1. 스키마·컴파일러 결정성·정책 단위 시험.
2. 어댑터 계약 시험: 실행·조회·멱등·권한·실효 차단·복구.
3. 실제 데이터/네트워크 통합 시험.
4. 분산 실패 모델 검사.
5. 단계별 fault injection.
6. 외부 업무 검증기 기반 E2E 시험.
7. 보안 경계·멀티테넌시·플랫폼 독립성 시험.

모의 시험과 실제 환경 시험은 서로 다른 증거 등급으로 기록한다. 하나의 방향이 통과해도 반대 방향 자격을 자동 발급하지 않는다.

### 28.2 수락 시험 목록

| ID | 시나리오 | 통과 조건 |
|---|---|---|
| AT-01 | 같은 고정 입력 재컴파일 | 같은 의미의 IR·digest, 변경 이유 추적 가능 |
| AT-02 | 출발·목적지 역할 교환 | 경로별 capability 재평가, 무근거 대칭 지원 없음 |
| AT-03 | 미지원 목적지 | 실행 계획 미발급·부족 능력 설명 |
| AT-04 | 관측 공백 | 의존성 없음으로 승격하지 않음 |
| AT-05 | A/B 공유 재고 | 단독 부분 이전 거부 또는 적절한 경계 확대 |
| AT-06 | 공유 DB의 고객별 PAUSE 요청 | 범위 미지원 검출 |
| AT-07 | 고객 A 인계 | B/C 영향 측정과 계약 유지 |
| AT-08 | 재시도·동시 같은 key | 중복 업무 없음·같은 결과 |
| AT-09 | 같은 key 다른 payload | 충돌 거부 |
| AT-10 | 승인 후 image/diff 변경 | 실행 거부·재검증 |
| AT-11 | 대상 이름 같고 UID 변경 | 기존 승인·증거 무효 |
| AT-12 | evidence 만료·재생·다른 run 사용 | 게이트 거부 |
| AT-13 | 권한·정책 변경 | 관련 승인·증거 재평가 |
| AT-14 | AI의 운영 Secret 조회 | 차단·민감값 미노출 |
| AT-15 | malicious dependency 행동 모사 | sandbox 밖 credential·egress 차단 |
| AT-16 | verifier 결과 조작 | 서명·binding·독립 관측으로 거부 또는 경보 |
| AT-17 | 원본 기존 DB 세션 | fence 이후 업무 commit 불가 |
| AT-18 | 늦게 살아난 Worker | 이전 권한으로 업무 효과 확정 불가 |
| AT-19 | source 차단 확인 불가 | 목적지 활성화 금지 |
| AT-20 | 활성화 요청 응답 유실 | 조회·SAFE_HOLD, source 자동 재개 없음 |
| AT-21 | 활성화 후 조정기 중단 | 재시작 시 실제 상태 재조정 |
| AT-22 | 순서 역전·중복 명령 | stale epoch·입력 불일치 거부 |
| AT-23 | 복제 수신 완료·적용 미완료 | 컷오버 거부 |
| AT-24 | DDL·sequence·extension 차이 | 대응 검증 또는 미지원 |
| AT-25 | 객체 참조 누락·내용 변조 | 인계 완료 차단 |
| AT-26 | 큐 inflight·지연·DLQ | 누락·중복 없이 지원 전략 처리 |
| AT-27 | 외부 효과 성공 후 로컬 응답 유실 | 조회·멱등 또는 보류, 맹목 재실행 없음 |
| AT-28 | 미완료 업무 인계 | 같은 business_id로 필요한 단계만 재개 |
| AT-29 | stale route·WebSocket 재연결 | 원본 쓰기 거부·지원된 세션 복구 |
| AT-30 | 새 목적지 쓰기 후 failback | 신규 데이터 회수·단일 권한·업무 검사 |
| AT-31 | 목적지 데이터 접근 불가 | 무손실 복귀 주장 금지·별도 DR |
| AT-32 | 원본 snapshot 복원 | 과거 writer가 자동 재활성화되지 않음 |
| AT-33 | 권한 DB 과거 복원 | 복구 incarnation·현장 대조 전 명령 금지 |
| AT-34 | 중앙 플랫폼 중단 | 확정된 경로의 신규 업무·연결 동작 확인 |
| AT-35 | 출발지 비밀 차단 후 새 기동 | 해당 exit contract 충족·잔존 의존 표시 |
| AT-36 | 플랫폼 권한 회수 | 고객 경로 신규 배포·백업·복원 가능 |
| AT-37 | 백업·감사 삭제 시도 | 실행 역할에서 거부·증거 보존 |
| AT-38 | 두 plan의 공유 권한 변경 충돌 | 겹치는 scope 동시 실행 거부 |
| AT-39 | failback 호환성을 깨는 신규 배포 | 차단 또는 보장 종료 승인 |
| AT-40 | 예산·용량 한도 초과 | 새 고비용 작업 제한, 안전 정리·복구는 보존 |

### 28.3 모델 검사

모델은 source/target 권한, unit, epoch, activation intent, fence, 메시지 중복·유실·지연, 조정기 crash, 지연 Worker, 승인 만료, 외부 효과 확인 상태를 포함한다.

안전 속성은 `at_most_one_effective_authority`, `no_activation_without_valid_fence`, `no_conflicting_step_after_unknown`, `no_unapproved_effect`, `no_automatic_source_reopen_after_possible_target_activation`로 둔다. 진행 속성은 통신·저장소·수리 가능성·공정한 재시도라는 명시 가정 아래 완료 또는 안전 복구에 도달하는지 검사한다.

TLA+ 모델 검사는 실제 구현 전체의 정확성 증명이 아니다.[R15] 테스트한 모델·범위·가정·발견 반례와 수정 이력을 제출하고, 어댑터가 모델의 가정을 실제 환경에서 지키는지 별도로 검증한다.

### 28.4 지원 선언에 필요한 qualification record

소스·타깃 버전, 데이터 기능, 데이터량·행수·객체 크기, 트래픽, 네트워크 지연·손실, 권한, replica 설정, 앱의 요청·멱등 의미, failure model, 시험 스크립트 digest, 이미지·어댑터 digest, raw evidence, 알려진 한계, 만료·재시험 조건을 기록한다.

---

## 29. 참조 시나리오: 업무 범위별 계획된 인계

### 29.1 가정

가상 주문 앱은 고객사 A/B/C가 DB·쓰기 역할·작업 ledger·라우팅 범위로 분리되어 있다. 전역 공유 재고를 각 고객사 DB에 분할해 놓았다는 가정을 명시한다. 분리되지 않은 실제 앱을 이 데모와 동일하게 처리한다고 주장하지 않는다.

출발지는 AWS의 앱 실행 환경(ECS 또는 EKS 중 별도 검증한 프로필), PostgreSQL 계열 DB, 객체 저장소, 비밀 서비스다. 목적지는 온프레미스 VM 또는 Kubernetes의 별도 검증 프로필이다. RDS 지원·자체 PostgreSQL 지원은 각각 qualification을 요구한다. 이 설명은 지원 완료 목록이 아니다.

### 29.2 흐름

1. 실제 주문 요청·완료 ACK·작업 ID·재고 결과를 외부 검증기가 수집한다.
2. A의 목적지 구성을 준비하되 업무 쓰기는 비활성으로 둔다.
3. 비밀 접근·보안 정책 차이를 발견하고 AI가 격리 환경에서 수정한다.
4. 업무가 동작하더라도 과도한 권한이면 거부한다.
5. 검증된 산출물·계획을 승인하고 A의 동기화를 수행한다.
6. A의 신규 요청·작업 claim을 제어하고, 진행 작업을 완료·영속 인계·미확정으로 나눈다.
7. 실제 원본 쓰기·지연 Worker를 차단하고 최종 데이터·작업·객체를 비교한다.
8. activation intent를 기록한 뒤 목적지를 활성화한다. 명령 응답 유실을 주입하고 실제 상태를 확인한다.
9. 경로를 전환하고 A의 미완료 업무를 같은 업무 ID로 재개한다. B/C 영향도 측정한다.
10. 출발지 의존성 차단 후 새 앱 인스턴스·새 연결·신규 첨부·권한 거부를 확인한다.
11. 복귀 가능 조건과 잔존 의존성을 기록한다. 원본 삭제는 하지 않는다.

### 29.3 비교 반례

A/B가 공유 역할·재고를 사용하는 변형 앱에서는 A 단독 인계를 거부한다. source 접근을 끊어 차단 여부를 모르게 하면 무손실 목적지 승격을 거부한다. AI가 보안 시험을 느슨하게 수정하려 하면 독립 검증으로 통과시키지 않는다.

---

## 30. 참조 기술과 제품 핵심의 구분

| 책임 | 참조 선택 | 명세상 요구 |
|---|---|---|
| API·모델·원장 | PostgreSQL 기반 서비스 | 원자적 intent·승인·scope 충돌·복구 절차 |
| 영속 조정 | Temporal 등 | 재개·타이머·상태 조회, 효과 멱등성은 별도 |
| 정책 | OPA | 서버 입력 신뢰·집행점 강제 |
| 워크로드 신원 | SPIFFE/SPIRE·클라우드 위임 | 신원과 인가 분리·갱신·회수 효과 검증 |
| 실행 환경 | Kubernetes·VM·managed API | 제품의 필수 고객 런타임으로 강제하지 않음 |
| IaC | 기존 Terraform 또는 별도 controller | 자원 소유권 충돌·승인·state 비밀 관리 |
| 트래픽 | Envoy·클라우드 LB·기존 edge | 범위별 경로·실제 전환·stale route 안전성 |
| 격리 | 일회성 VM·자격 검증된 runtime | 운영 credential 미제공·자원·네트워크 제한 |
| 공급망 | OCI digest·SBOM·Cosign 등 | 승인 산출물·기대 주체·빌드 출처 검증 |
| 관측 | OpenTelemetry·교체 가능한 backend | 원장·감사와 sampling telemetry 분리 |
| 검증 | 독립 업무 검증기·TLA+·fault injection | 모델·실제 환경·수락 시험의 연결 |

Harbor 등 특정 AI 평가 프레임워크는 선택 가능한 시험 어댑터로만 둔다. 제품 실행 오케스트레이터나 sandbox의 안전성을 해당 이름으로 대체하지 않는다. 특정 프레임워크와 microVM의 연결은 별도의 구현·자격 시험 대상이다.

직접 개발의 중심은 서비스 계약·경계 모델, capability·strategy qualification, 계획 컴파일러, 처리 권한 인계, 효과 재조정, 독립 검증·증거 유효성 관리다. 데이터 복제 엔진·PKI·분산 workflow 자체를 새로 구현하는 것은 제품 핵심으로 삼지 않는다.

---

## 31. 구현 순서와 설계 결정 기록

이 순서는 본선 일정에 맞춘 범위 축소가 아니라 위험을 검증하며 확장하는 제품 개발 순서다.

| 단계 | 결과물 | 다음 단계 조건 |
|---|---|---|
| L0 모델·읽기 조사 | 계약·그래프·환경·미지원 판정 | 허위 지원·숨은 미확인 없음 |
| L1 typed 실행·원장 | 제한 명령·효과 조회·멱등·감사 | crash·응답 유실·중복 명령 시험 |
| L2 단일 단위 인계 | 실제 DB·작업·권한 전환 | 단일 쓰기·데이터·복구 시험 |
| L3 코드 수정·독립 검증 | AI patch·sandbox·artifact 결속 | 악성 지시·권한 확대·증거 위조 거부 |
| L4 방향 확장 | 다른 source/target 전략 | 경로별 qualification |
| L5 부분·다중 단위 | 경계 분석·충돌 조정 | 공유 상태 반례·범위별 차단 |
| L6 지속 운영 | 재검증·복귀 계약·플랫폼 탈출 | 변경 무효화·독립 복구 시험 |

필수 ADR 주제는 권한 원장과 workflow의 책임, 정합성 경계 판정, adapter ABI, fence 강제 방식, source restore 차단, 데이터 정합성 수준, external effects, audit failure 동작, AI 격리, source/플랫폼 exit, 지원 프로필·예외다.

---

## 32. 미결정 사항과 승인 전 필수 결정

| 항목 | 필요한 결정 | 미결정 상태의 제한 |
|---|---|---|
| 업무 분할 계약 | 실제 transaction·공유 자원·writer 범위 | 부분 인계 금지 |
| 실제 fence | 프로필별 강제·재시작·복원·우회 차단 | 무손실 활성화 금지 |
| 데이터 비교 | snapshot·delta·hash·업무 oracle | 동등·무손실 주장 금지 |
| 외부 효과 | provider 멱등성·조회·보상 | 해당 단계 자동 재개 금지 |
| Qualification 범위 | 버전·데이터·부하·네트워크·장애 | 지원 완료 표시 금지 |
| 승인 정책 | 직무 분리·사전 승인·예외 | 고위험 운영 실행 금지 |
| 권한 DB 복구 | incarnation·키·현장 대조·격리 | 재해 복구 후 신규 명령 금지 |
| 비밀/PKI 운영 | 고객 소유·갱신·회수·중앙 장애 | 독립 운영 보장 제한 |
| 비용·SLO | 고객 계약·실측 가능한 budget | 수치 보장 금지 |

---

## 부록 R. 공식 근거와 기술 참고

**F1.** 사용자 제공 「[Final Term｜Kickoff] SoftBank Hackathon 2026 powered by KOREC (3).pdf」, 14·17·19·21·23·40쪽. 공식 요구 해석의 근거다. 나머지 상세 명세는 설계 제안이다.

**R1. PostgreSQL — Logical Replication Restrictions**  
https://www.postgresql.org/docs/current/logical-replication-restrictions.html  
DDL·sequence state·large object 등 제약. 실제 구현은 qualification 당시 문서 버전을 고정한다.

**R2. Temporal — Activity Definition**  
https://docs.temporal.io/activity-definition  
Activity 멱등성과 재실행 가능성.

**R3. Amazon Builders’ Library — Leader Election in Distributed Systems**  
https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/  
리스·지연·기존 리더의 작업에 대한 고려.

**R4. PgBouncer — Usage**  
https://www.pgbouncer.org/usage.html  
DB 단위 PAUSE와 연결·pooling 동작.

**R5. PostgreSQL — Logical Replication Row Filters**  
https://www.postgresql.org/docs/current/logical-replication-row-filter.html  
행 필터와 제약. 복제 선택자는 업무 차단 수단이 아니다.

**R6. AWS Prescriptive Guidance — Transactional Outbox Pattern**  
https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html  
업무 데이터·이벤트 이중 쓰기 문제와 소비자 멱등성.

**R7. Open Policy Agent — Deployment / Policy Enforcement**  
https://www.openpolicyagent.org/docs/deploy  
정책 판단점과 실제 집행점의 분리.

**R8. Kubernetes — Multi-tenancy**  
https://kubernetes.io/docs/concepts/security/multi-tenancy/  
namespace·control/data plane·공유 노드 격리 고려.

**R9. RFC 8785 — JSON Canonicalization Scheme**  
https://www.rfc-editor.org/rfc/rfc8785.html  
계획 표현의 정규화 참고. 업무 의미 일치를 보증하는 규격은 아니다.

**R10. SPIFFE — Overview**  
https://spiffe.io/docs/latest/spiffe-about/overview/  
워크로드 신원과 SVID.

**R11. Amazon S3 — Checking Object Integrity**  
https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html  
객체 checksum 검증.

**R12. Sigstore — Verifying Signatures**  
https://docs.sigstore.dev/cosign/verifying/verify/  
서명·인증 identity/issuer 검증.

**R13. Amazon S3 — Object Lock**  
https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html  
WORM 보존 수단.

**R14. OpenTelemetry — Signals**  
https://opentelemetry.io/docs/concepts/signals/  
관측 signal의 수집·처리·export.

**R15. Leslie Lamport — A High-Level View of TLA+**  
https://lamport.azurewebsites.net/tla/high-level-view.html  
모델 수준 검토·안전성과 진행 속성·구현 검증의 구분.

---

## 부록 S. 최종 제품 정의

**출발지·목적지를 고정하지 않고, 지원 가능한 환경 조합에 대해 업무·데이터·보안 조건을 보존하는 계획을 컴파일한다. 실제 처리 권한 인계와 외부 효과를 검증하며, 근거가 부족하거나 안전하게 인계할 수 없는 경우 실행을 거부한다. 이전 이후에도 계약·복귀 가능성·독립 운영의 유효성을 관리한다.**

이 문서는 구현 완료나 안전성 증명서가 아니다. 지원 선언은 본 문서의 acceptance test와 방향별 qualification을 실제 코드·환경·증거로 통과한 범위에서만 가능하다.


---

# Part II. MIG-EXT-IAM-001 — Identity & Access / Cognito 확장 (원문 보존)

# Contract-Driven, Capability-Based Migration Platform — Identity & Access Migration 확장 명세

**문서 ID:** MIG-EXT-IAM-001  
**버전:** 1.0-draft  
**작성일:** 2026-11-06  
**상태:** Proposed / Not Implemented  
**상위 문서:** `contract-driven-migration-spec-v1.0.md` (MIG-SPEC-001)  
**범위:** 플랫폼에 등록된 **고객 운영 애플리케이션**의 인증·인가 의존성과 Identity Provider(특히 Amazon Cognito User Pools) 이전  
**우선순위:** Identity & Access 계약 및 검증은 플랫폼의 기본 필수 능력. **Cognito 전용 어댑터는 권장 확장**. IdP 실제 교체 실행은 서비스 계약에서 요구하는 경우에만 필요.

---

## 1. 결정 요약

이 확장은 AWS Cognito를 플랫폼의 필수 로그인 공급자로 채택하는 제안이 **아니다**. 기존 플랫폼의 `ServiceContract`, `DependencySnapshot`, `CapabilityManifest`, `IdentityPlan`, `ExitContract`, `Evidence`, `Approval`과 안전한 실행 프로토콜에 **사용자 인증·세션·인가 정책 이전**을 명시적으로 반영하는 제안이다.

- **필수:** 운영 서비스가 의존하는 신원 공급자, 토큰 검증, 권한 정책, 비밀·워크로드 신원을 발견하고, 목표 환경에서 계약이 충족되는지 판단한다.
- **선택:** 기존 Cognito를 목적지에서도 유지할 수 있다. 허용된 잔존 의존성으로 기록한다.
- **권장:** Cognito User Pool의 조사 및 정합성 시험용 Provider Adapter를 구현한다.
- **조건부:** `Cognito → Keycloak/기업 OIDC/다른 CIAM` 실제 사용자 이전은 독립성 요구·지원 범위·검증 근거가 있을 때만 계획한다.
- **금지:** 프로필 복사를 로그인 연속성의 보장으로 간주하거나, 비밀번호·MFA·기존 JWT 세션이 자동 이전된다고 주장하지 않는다.

## 2. 문제와 범위

### 2.1 문제

서비스가 EKS/ECS와 RDS를 떠났더라도 새 로그인, 액세스 토큰 갱신, MFA, 관리자 계정 관리가 Cognito에 의존하면 완전한 사업자 독립이 아니다. 반대로 계약에서 Cognito 잔존 의존성을 허용한다면 해당 경로는 적법한 **부분 이전**일 수 있다. 플랫폼은 이 상태를 구분한다.

### 2.2 세 종류의 신원 분리

| 영역 | 예시 | 책임 |
|---|---|---|
| Workforce Identity | 플랫폼 운영자 SSO, 관리자 OIDC | Control Plane 자체 인증·권한. 특정 IdP 필수 아님 |
| Customer Identity / CIAM | Cognito User Pools, Keycloak, Auth0 | 이전 대상 앱의 사용자·그룹·인증 정책 |
| Workload Identity | IAM Role/STS, K8s SA, SPIFFE/SPIRE | 앱·Worker의 자원 접근 신원 및 권한 |

**Cognito User Pools**(사용자 인증)와 **Cognito Identity Pools**(AWS 자격증명 연결)는 구분하여 각각 발견·분류한다. Identity Pool을 사용하는 앱은 federated identity와 IAM role/credential 경로를 별도로 이전해야 한다.

### 2.3 범위/비목표

- 범위: 사용자 프로필·고정 사용자 ID 매핑, 계정 상태, 클라이언트·리디렉션, JWT 검증, 권한·그룹·커스텀 클레임, MFA/패스키/외부 페더레이션 **지원 여부 분석**, 세션 전환, 감사·로그인 회귀, 복구 계획.
- 비목표: 모든 사용자 비밀번호·MFA 등록정보·Refresh Token의 투명한 이전, 임의 IdP 간 완전 자동 호환, 전 사용자 무중단 재인증 보장, 규제 적합성 자동 인증.

## 3. 기존 MIG-SPEC-001에 대한 최소 변경

| 기존 요소 | 확장 사항 | 호환성 |
|---|---|---|
| `ServiceContract` | `identity`, `session`, `authorization` 불변조건 | 선택 필드 추가, 필수 검사 항목은 계약에 따름 |
| `DependencySnapshot` | `IdP`, `JWKS`, OAuth client, trigger, identity pool, federation, authz decision edge | 기존 증거 모델 재사용 |
| `CapabilityManifest` | Identity/CIAM 하위 능력과 방향·제약·시험 정의 | Provider Adapter 플러그인 |
| `MigrationPlan.IdentityPlan` | 계정·토큰·세션·권한의 단계별 전환 계획 | 기존 계획의 하위 계획 |
| `Approval/AuthorityRecord` | 신원·권한 변경의 별도 고위험 승인·scope lock | 기존 승인/원장 재사용 |
| `Evidence` | 로그인·인가·MFA·토큰·독립성 검증, 신선도·무효화 | 기존 증거 형식 재사용 |
| `ExitContract` | `identity_independence` 상태와 잔존 인증 의존성 | 기존 exit 시험 확장 |
| `ReversibilityContract` | 원래 IdP로 복귀 가능한 기간·세션·계정 drift | 별도 복귀 계획 |

단일 쓰기 인계 프로토콜을 인증 전환에 그대로 대입하지 않는다. **사용자 신원 매핑·토큰 수용 정책·기존 세션 만료·인가**가 별도 전환 경계이며, 활성 로그인과 계정 변경의 충돌을 관리해야 한다.

## 4. 서비스 계약 확장

```yaml
kind: ServiceContract
metadata:
  id: customer-portal
  revision: 12
spec:
  identity:
    principal_namespace: organization-scoped-stable-id
    stable_subject_mapping_required: true
    allowed_external_idps: [target-oidc]
    remove_dependencies: [aws-cognito-user-pool]
    allow_dual_issuer_during_transition: true
    dual_issuer_max_duration: "PT24H" # 예시, 실제 값은 서비스 책임자가 결정
    password_transition_policy: reset-or-verified-bridge
    mfa:
      required: true
      allowed_methods: [totp]
      reenrollment_allowed: true
      user_communication_approved: true
    sessions:
      forced_reauthentication_allowed: true
      refresh_token_portability_assumed: false
    authorization:
      deny_cross_tenant_access: true
      source_target_permission_equivalence_required: true
    evidence:
      fresh_login_required: true
      fresh_token_refresh_required: true
      denied_access_test_required: true
      source_idp_blocked_cold_start_required: true
```

서로 다른 IdP의 `sub` 값이 같은 사용자임을 뜻하지 않는다. 앱 도메인의 안정적 내부 사용자 ID와 `(issuer, subject) → internal_user_id` 매핑을 분리하고, 충돌·중복·탈퇴·재가입 정책을 정의한다.

## 5. 의존성 조사(Discovery)

수집 항목: User Pool ID·리전·도메인, App Client ID, OAuth/OIDC 플로, Redirect/Logout URI, issuer/JWKS, 토큰 수명·검증 방식, 커스텀 claim, 그룹·역할, Lambda Trigger 의존성, 외부 OIDC/SAML federation, MFA 및 사용자 복구 정책, 사용자 수·계정 상태, Identity Pool, 연계 IAM 역할, 이메일/SMS 발신 경로, 로그인 감사 및 알림, 앱 코드의 Cognito SDK·환경 변수·JWKS 캐시·API Gateway authorizer·ALB authentication 설정.

- 정적 코드/설정, 실제 배포 상태, 로그인·갱신 테스트, 담당자 선언을 각각 다른 근거로 기록.
- 사용자 데이터의 수집에는 PII 최소화, 암호화, 접근 제어, 보존 기간을 적용. 민감 자격증명을 AI 입력에 제공하지 않음.
- `발견하지 못한 로그인 경로`는 `NONE`이 아닌 `UNKNOWN`으로 기록.
- 서버 측 인증뿐 아니라 모바일 앱, SPA, API Gateway/ALB, 백그라운드 M2M 토큰 소비자를 조사.

## 6. Cognito Capability Adapter

### 6.1 기본 작업

```text
inspect_user_pool
inspect_clients_and_issuer
inspect_users_and_groups
inspect_triggers_and_federation
inspect_identity_pool_and_iam_roles
export_supported_user_profile_fields
stage_target_principal_mapping
verify_oidc_metadata_and_jwks
verify_login_and_token_validation
verify_authorization_matrix
verify_source_idp_independence
reconcile_identity_changes
```

### 6.2 능력 선언 예시

```yaml
kind: CapabilityManifest
adapter: aws.cognito.user-pools
version: 0.1.0
roles: [source, dependency-preserved]
operations:
  inspect: supported
  user_attributes_export: supported-with-permissions
  group_membership_export: supported-with-permissions
  plaintext_password_export: unsupported
  password_hash_export_from_cognito: unsupported
  live_session_transfer: unsupported
  mfa_secret_transfer: unsupported-unless-qualified
  account_migration_out: strategy-dependent
  token_issuer_transition: strategy-dependent
  identity_pool_iam_role_mapping: separately-qualified
qualification_required: true
```

지원 표시만으로 실제 경로를 승인하지 않는다. AWS API 권한, 계정 규모·속도 제한, 사용자 속성·상태의 의미 차이, 방향별 자격 시험 결과가 필요하다.

## 7. 마이그레이션 전략

| 전략 | 설명 | 적용 조건 | 완료 판정 |
|---|---|---|---|
| `RETAIN_SOURCE_IDP` | 목적지 앱에서 Cognito 계속 사용 | 잔존 AWS 종속 허용 | 로그인·권한·갱신 검증, AWS 의존성 잔존 표시 |
| `RECONFIGURE_EXISTING_FEDERATION` | 기업 소유 상위 IdP/페더레이션 재구성 | 실제 신원·클레임 의미 호환 | 신규 로그인·권한·세션 시험 |
| `BULK_PROFILE_MIGRATION` | 검증된 사용자 속성·그룹 이전 | 소스에서 내보낼 수 있는 항목만 | 사용자 매핑·계정 상태·로그인 복구 경로 검증 |
| `JUST_IN_TIME_ACCOUNT_BRIDGE` | 최초 로그인 시 기존 IdP 인증 결과를 검증해 계정 연결 | 합법적 자격증명 검증 브리지와 보안 설계 | 점진 이전율·실패율·브리지 철회 확인 |
| `RESET_AND_REENROLL` | 암호 재설정·MFA 재등록 | 사용자 안내·복구 채널과 승인 | 새 인증 수단으로 로그인 성공 |
| `DUAL_ISSUER_TRANSITION` | 제한 기간 구·신 JWT 수용 | 좁은 issuer/audience/client 매핑, 운영 허가 | 수용·거부·종료 시각·최종 폐기 검증 |
| `BLOCK` | 필수 요건 불충족 | 계정·MFA·인가·복구·규제 조건 미충족 | 이전 실행 불가 |

**방향 주의:** AWS Cognito `User Migration Lambda`는 외부 디렉터리에서 Cognito User Pool로 이전하는 기능이다. 이를 Cognito → 외부 IdP의 자동 내보내기·암호 이전 도구라고 해석하지 않는다. Cognito 비밀번호를 복원 가능한 형태로 읽어낼 수 없으며, 프로필·그룹 export가 인증수단·MFA까지 이동시키는 것은 아니다.

## 8. 신원·세션·인가 전환 프로토콜

1. **Inventory** — Cognito 및 실제 모든 토큰 소비자를 조사, 대상 `issuer`, `audience`, 사용자 ID 연결 전략 정의.
2. **Target Provisioning** — 새 IdP·OAuth client·권한·로그인 감사·복구 경로 구성.
3. **Account Mapping** — 기존 `(issuer, sub)`와 대상 `(issuer, sub)`를 앱 내부 영속 ID로 매핑. 이메일 주소만으로 계정 병합 금지.
4. **Credential Strategy** — 기존 암호 재사용을 가정하지 않고 브리지/재설정/재등록 중 자격 시험된 전략 선택.
5. **Authorization Qualification** — 그룹·custom claims·역할·리소스 권한 매핑과 `deny`/`allow` 회귀 시험.
6. **Session Transition** — 앱·API·모바일·배치의 기존 토큰·세션 처리, Refresh Token 중단/만료/재인증 결정.
7. **Rehearsal** — 합성 사용자·테스트용 IdP에서 신규 로그인, 로그아웃, 갱신, MFA, 복구, 비인가 거부 시험.
8. **Approval Binding** — 계획 revision, 변경된 auth configuration, IdP UID, JWKS/issuer, app image digest, 정책 bundle, 검증 증거에 결속.
9. **Canary & Cutover** — 지원 전략에 맞춰 고객·클라이언트 범위를 제한해 전환. 구버전 앱·API Gateway/ALB authorizer가 남아 있는지도 확인.
10. **Independent Operation** — 출발지 인증 서비스에 연결하지 않는 새 앱 인스턴스·새 로그인·토큰 갱신·MFA·계정 복구를 시험.
11. **Observation & Retire** — 로그인 실패·권한 우회·세션 문제 관측 후 기존 issuer 수용·브리지·Cognito 의존성 제거를 별도 승인.

### 8.1 이중 issuer 보안 조건

- 허용 가능한 `iss`, `aud`/client, 토큰 목적(access vs ID), `exp`, 서명 알고리즘과 JWKS 출처를 명시. `kid`만으로 키를 신뢰하지 않음.
- 구 issuer와 신 issuer의 동일 `sub`를 동일 사용자로 해석하지 않음. 서버 측 신원 매핑이 필요.
- 서로 다른 issuer의 권한·그룹 claim을 자동으로 합치거나 상위 권한을 취하지 않음.
- 만료된 구 issuer는 거부하고 허용 범위 밖에서 fallback하지 않음.
- 구 토큰을 받는 모든 소비자(API, 게이트웨이, 앱, Worker)를 추적.
- 종료 시각과 종료 후 실제 거부 시험을 필수로 둠.

### 8.2 계정 변경 동시성

사용자가 이전 도중 이메일·MFA·권한을 변경할 수 있다. 초기 프로필 복사만으로 완료되지 않는다. 소스·목적지 중 **누가 계정 변경의 권위를 갖는가**를 전환 단계별로 정하고, 변경 이벤트 또는 최종 차이 대조를 통해 동기화한다. 불확실하면 계정 변경을 제한하거나 해당 사용자의 전환을 보류한다.

### 8.3 인증 이전과 업무 Writer 인계의 관계

앱의 데이터 쓰기 권한 이전과 IdP 전환은 독립적인 작업이지만 연관된 제약을 가진다. 로그인 성공만으로 목적지 업무 Writer를 열지 않는다. 반대로 데이터 전환이 완료돼도 사용자 인증 회귀가 실패했다면 서비스 계약 완료로 판정하지 않는다.

## 9. 정책·증거·완료 판정

### 9.1 Identity Gate

| ID | 검증 항목 | 실패 시 |
|---|---|---|
| IAM-01 | 기존 사용자 → 새 내부 사용자 ID 매핑의 유일성 | BLOCK |
| IAM-02 | 신규 로그인·토큰 검증·갱신 | BLOCK |
| IAM-03 | 필수 MFA/복구 흐름 유지 또는 승인된 재등록 | BLOCK |
| IAM-04 | 다른 고객·권한·리소스 접근 거부 | BLOCK |
| IAM-05 | 구·신 JWT 검증 정책과 만료 | BLOCK |
| IAM-06 | 사용자·그룹·계정 상태 일치 또는 승인된 차이 | BLOCK |
| IAM-07 | 비밀·토큰·PII의 로그·AI 입력 유출 없음 | BLOCK |
| IAM-08 | 새 앱 인스턴스에서 출발지 IdP 차단 후 로그인·운영 | `SOURCE_IDENTITY_INDEPENDENT` 불가 |
| IAM-09 | 감사·잠금·탈퇴·계정 복구 경로 | BLOCK |
| IAM-10 | 기존 issuer 종료 이후 토큰 거부와 권한 회수 효과 | 완료 보류 |
| IAM-11 | 브리지·이중 issuer 종료 조건과 사용자 미전환 잔량 | 종료 승인 보류 |
| IAM-12 | 로그인 실패율·인증 지연·재인증·MFA 실패 측정 | SLO 위반 시 진행 차단 |

증거는 `service_contract_revision`, `identity_plan_revision`, `source/target IdP identity`, `client_id`, 검증기 digest, 정책 revision, 수집 시각·유효기간·무효화 트리거에 결속한다. 사용자 비밀번호·인증 토큰 원문은 영수증이나 감사 로그에 포함하지 않는다.

### 9.2 판정 상태

```text
SOURCE_IDP_RETAINED
IDENTITY_MIGRATION_PREPARED
IDENTITY_CUTOVER_IN_PROGRESS
IDENTITY_VALIDATED
SOURCE_IDENTITY_INDEPENDENT
IDENTITY_PARTIAL
IDENTITY_UNKNOWN
IDENTITY_BLOCKED
```

`SOURCE_IDENTITY_INDEPENDENT`는 출발지 User Pool을 포함한 **계약상 제거 대상**을 실제 차단한 상태에서 새 로그인·토큰 검증·갱신·필수 복구가 시험됐을 때만 부여한다. 복귀용으로 기존 IdP를 보존하는 상태와 완전 폐기 상태를 구분한다.

## 10. 복귀·거버넌스·운영

- 기존 Cognito issuer를 다시 허용하는 것만으로 새 IdP에 등록된 계정 변경·새 사용자·MFA 상태가 되돌아오지 않는다. 복귀 시 사용자·권한·계정 변경 차이를 별도 검증.
- 새 issuer 토큰을 구 issuer로 바꿔 재서명하거나 이전 Refresh Token을 억지로 재사용하지 않는다.
- 로그인 실패·권한 상승·계정 연결 오류가 발생하면 영향 사용자·클라이언트 범위를 격리하고, 이미 활성화된 신원 구성을 안전하게 재조회한다.
- 운영·보안 책임자를 분리하고 MFA 변경·인증정책 약화·구 issuer 재허용은 고위험 변경 승인 대상으로 지정.
- 기존 IdP 폐기 전에 사용자 미전환율, 복구 경로, 불변 감사, 세션 최대 수명, API 소비자 잔존 여부와 되돌리기 가능 기간을 확인.
- 코드, OAuth client, 인증 정책, 사용자 디렉터리, Lambda Trigger, JWKS, 역할 매핑, 토큰 소비자 변경 시 관련 증거 무효화.
- 관리 플랫폼 자체의 OIDC와 고객 애플리케이션의 IdP 이전은 자격증명·테넌트·정책 저장소를 분리.

## 11. API 및 예시

```http
POST /v1/services/{service_id}/identity/discoveries
POST /v1/services/{service_id}/identity-plans:compile
GET  /v1/identity-plans/{plan_id}/revisions/{revision}
POST /v1/identity-plans/{plan_id}/rehearsals
POST /v1/identity-plans/{plan_id}/approvals
POST /v1/identity-plans/{plan_id}/executions
GET  /v1/identity-plans/{plan_id}/evidence
GET  /v1/services/{service_id}/identity-independence
```

위 경로는 기존 `/v1/plans`에 하위 IdentityPlan으로 통합할 수 있는 **논리적 API 후보**다. 별도 실행 엔진이나 별도 권한 원장을 만들지 않는다. 서버 측 조직·서비스 소유권 검사, 수정 요청의 Idempotency-Key, revision precondition, 읽기 가능한 비밀값 최소화를 적용한다.

## 12. 수락 시험 (추가)

| ID | 시험 | 합격 기준 |
|---|---|---|
| IAM-AT-01 | Cognito 사용 중인데 외부 AWS 의존성 허용 | 잔존 의존성 표시, 지원하는 유지 계획 제공 |
| IAM-AT-02 | 완전 AWS 탈출 요구 + Cognito 미이전 | 컴파일 결과 BLOCK/REQUIRES_REMEDIATION |
| IAM-AT-03 | User Pool 프로필 export 성공, 암호 미이전 | 프로필 복사를 인증 완료로 처리하지 않음 |
| IAM-AT-04 | 사용자 ID/email 충돌 | 계정 자동 병합 금지, 검토 대기 |
| IAM-AT-05 | issuer만 바뀌고 API audience 불일치 | 잘못된 JWT 거부 |
| IAM-AT-06 | ID Token을 Access Token 대신 사용 | 목적 외 토큰 거부 |
| IAM-AT-07 | 구 issuer와 신 issuer의 같은 sub | 동일 사용자로 자동 결합하지 않음 |
| IAM-AT-08 | 비인가 사용자에게 그룹 상향 매핑 | 권한 회귀 실패, BLOCK |
| IAM-AT-09 | MFA 미이전, 대체 인증 없음 | 실제 이전 완료 불가 |
| IAM-AT-10 | 리허설 데이터에 실제 비밀번호·토큰 포함 | 시험 시작 차단·감사 |
| IAM-AT-11 | 이중 issuer 만료 후 기존 토큰 제출 | 명시적 거부 |
| IAM-AT-12 | 앱 재기동 시 기존 JWKS 캐시 제거 | 목적지 IdP로 신규 검증 성공 |
| IAM-AT-13 | 출발지 User Pool 접근 차단 후 신규 로그인 | 계약 범위 내 독립성 PASS 또는 실패 근거 |
| IAM-AT-14 | 이전 중 그룹·MFA 변경 | 상태 재조회·충돌 확인·필요 시 보류 |
| IAM-AT-15 | 인증 cutover 직후 응답 유실 | 원본 복귀 추측 실행 금지, 실제 상태 재조회 |
| IAM-AT-16 | 목적지 IdP 장애 | 계약상 복구·재인증 정책에 따라 처리, 임의 보안 약화 금지 |

## 13. 단계별 도입 제안

| 단계 | 구현 범위 | 목표 |
|---|---|---|
| I0 — 계약 | Identity/CIAM 분류, 서비스 계약·증거·ExitContract 필드 | 출발지 신원 의존성의 유무와 요구 구분 |
| I1 — 조사·검증 | Cognito Read-only Discovery, OAuth/OIDC 검증기 | 기존 사용자 로그인·권한을 잃을 위험 탐지 |
| I2 — 선택적 실제 이전 | 지원되는 Cognito→목적지 IdP 경로 하나 | 계정·세션·인가의 검증된 전환 |
| I3 — 운영 확장 | MFA/복구·페더레이션·대규모 계정·장기 재검증 | 사업자 독립성·복귀 계약의 지속 관리 |

기존 `MigrationPlan`과 `Approval/Effect Ledger`를 재사용하고, 코드/데이터/권한의 실제 운영 인계 기능을 Cognito 작업에 종속시키지 않는다.

## 14. 위험 및 공개 제약

- 계정 프로필 export와 로그인 인증수단 이전은 별개다.
- 사용자 비밀번호를 Cognito에서 추출하거나 기존 Refresh Token을 새 IdP에서 그대로 수용할 수 있다고 가정하지 않는다.
- Cognito 사용자 풀 트리거·그룹·커스텀 클레임·외부 federation 설정은 기능 이름이 같아도 의미가 다를 수 있다.
- MFA·계정 복구·사용자 ID 식별은 이전 실패와 계정 탈취 위험이 높은 구간이다.
- 이중 issuer 전환은 공격 면적을 늘릴 수 있으므로 짧은 기간·제한된 client·독립 권한 검증이 필요하다.
- 고객 계정 데이터·PII를 AI 입력으로 보내지 않고, 실험은 합성 데이터로 수행한다.
- Cognito가 유지되는 경로에서는 `출발지 AWS 독립`을 허위로 PASS 표시하지 않는다.
- Cognito 인증에 성공하는 것과 AWS 자격 증명 발급(Identity Pools), AWS IAM 역할 접근, 애플리케이션 권한 허용은 별도로 검사한다.

## 15. 기존 상위 스펙 반영 지점

- `§8 공통 모델`: Cognito/CIAM 및 사용자 mapping 추가.
- `§10 서비스 계약`: `identity`, `sessions`, `authorization` 계약 추가.
- `§11 Dependency/Capability`: 사용자 인증, JWT, 인가 정책, 연계 IAM 역할 능력 추가.
- `§12 Plan Compiler`: 인증 이전 지원 여부·위험·세션·재인증 계획 검사.
- `§13 Migration IR`: 기존 `IdentityPlan` 상세화, 승인·복구·증거 결속.
- `§19~20 Safety/Handoff`: 계정 변경 주체, issuer 전환, 기존 세션의 처리 계약 추가.
- `§25 Exit/Continuous`: `SOURCE_IDENTITY_INDEPENDENT` 시험·증거 재검증.
- `§26 Security`: 사용자 인증·MFA·인가 회귀 테스트 추가.
- `§28 API`: IdentityPlan 논리 API 추가(별도 실행 엔진은 만들지 않음).
- `§31 Acceptance`: IAM-AT-01~16 검증 확장.

## 16. 공식 참고자료

- [AWS — Cognito user profile/group export guidance](https://docs.aws.amazon.com/solutions/latest/cognito-user-profiles-export-reference-architecture/overview.html): 프로필·그룹 내보내기 예시, 비밀번호·MFA 등 제한.
- [AWS — Cognito password and recovery policies](https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users-passwords.html): 기존 비밀번호 원문 조회 불가.
- [AWS — Cognito user migration trigger](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-import-using-lambda.html): 외부 디렉터리 → Cognito 방향의 로그인 시 이전.
- [AWS — Cognito user pool migration/import options](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-import-users.html): Cognito로 들어오는 데이터 가져오기 방식. Cognito 탈출 API와 구분.
- [AWS — Cognito Lambda triggers](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-working-with-lambda-triggers.html): 커스텀 클레임·인증·페더레이션 후크의 이전 대상 분석.
- [AWS — Cognito User Pools](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-identity-pools.html): User Pools와 Identity Pools의 역할 구분.

---

**결정:** Cognito를 플랫폼 필수 인프라로 도입하지 않는다. **Identity & Access의 발견·요구 계약·검증은 공통 필수 능력**, Cognito 전용 조사 어댑터는 **권장**, 실제 Cognito→목적지 IdP 이전은 **서비스 계약에 의해 선택되는 방향별 검증 전략**으로 둔다.


---

# Part III. PATCH-EXT-001 — 지속적 보안 개선 및 Patching as a Service 통합 설계

**버전:** 1.0-draft · **상태:** 신규 통합 제안 / 기능 전체 E2E 검증 미확정  
**상위:** MIG-SPEC-001 / MIG-EXT-IAM-001  
**참고 기존 설계:** `docs/security/continuous-secure-modernization.md` — 「Patching as a Service 기반 지속적 보안 개선 및 멀티클라우드 마이그레이션 아키텍처」

## P.1. 목표 및 비목표

보안 개선을 별도 자동 패치 제품으로 분리하지 않고 **서비스 계약을 지속적으로 유지하기 위한 운영 변경의 한 종류**로 모델링한다. 취약점 탐지와 수정 제안은 자동화할 수 있지만, AI가 임의로 운영 권한·계정·대상·승인을 변경할 수 없다.

- 목표: 취약점·정책 드리프트 탐지 → 위험과 영향 분석 → 변경 계획 → 격리 리허설 → 독립 검증 → 권한 있는 승인 → 점진 반영 → 운영 재검증 → 증거 갱신.
- 비목표: 취약점이 발견될 때마다 AI가 운영 시스템을 직접 패치하거나, 모든 CVE를 자동 수정할 수 있다고 보장하는 것.
- 구현 구분: 현재 `security`의 판정, Sova의 실행·감시, `platform`의 AI·리허설·승인·반영 기능이 존재할 수 있어도 전체 자동 운영 E2E와 동의어로 취급하지 않는다.

## P.2. 공통 계약에 추가할 객체

| 객체 | 필수 정보 | 활용 |
|---|---|---|
| Finding | 탐지 원천, CVE/설정 문제, 영향 자원, 신뢰도, 최초·최근 관측 | 위험 추적 |
| RemediationIntent | 변경 목표, 금지 행동, 허용 대상·권한 범위 | AI 제안 범위 제한 |
| ChangeImpact | 업무·데이터·가용성·비용·규제·복귀 영향 | 계획 승인 입력 |
| PatchPlan | 버전 고정 diff, 이미지 digest, 마이그레이션 순서, 실행 제약 | Plan Compiler 출력 |
| RehearsalEvidence | 격리 환경 및 계약, 취약점 재현과 수정 확인, 비기능 시험 | 독립 검증 |
| ApprovalBinding | 계획 해시, 검증 증거 ID, 승인자·권한·정책 버전, 만료 | TOCTOU 차단 |
| DeploymentReceipt | 실행 효과, 실제 리비전, 롤아웃·중단·복귀 상태 | 결과 판정 |
| RevalidationResult | 취약점 해소, 회귀, 남은 위험, 만료된 증거 | 지속 운영 |

모든 객체는 `organization_id`, `application_id`, `environment_id`, `resource_uid`, `operation_id`, `schema_version`, `issued_at`을 지원해야 한다. 사용 범위가 다른 증거를 혼합하지 않는다.

## P.3. 실행 흐름

```text
Finding 수집·정규화
    ↓
실제 자원 및 서비스 계약에 결속
    ↓
위험 우선순위 + 변경 영향 분석
    ↓
AI 또는 규칙 기반 PatchPlan 제안
    ↓
정책 사전 검증 · 허용 능력 조회
    ↓
자격증명 없는 격리 리허설
    ↓
보안·기능·데이터·성능 독립 검증
    ↓
계획·증거·정책 버전 결속 → 승인
    ↓
제한된 실행 권한으로 점진 배포
    ↓
새 이미지·설정·실제 환경 재조회
    ↓
취약점 재스캔·기능 회귀 검사
    ↓
계약 유효성·복귀 가능성 재판정
```

## P.4. 정책 및 게이트

- **P-G0 자산 결속:** 탐지 결과가 실제 소유·관리 대상인지 검증되지 않으면 자동 적용 금지.
- **P-G1 계획 능력:** 변경할 리소스 타입, 정합성 경계, API를 Capability Catalog가 검증한 경우만 실행 계획 발급.
- **P-G2 리허설:** AI 생성 diff와 실제 산출물 digest를 일치시키고 독립 시험으로 악성 의존성·비밀 노출·기능 회귀 확인.
- **P-G3 승인:** 수정안과 승인 당시 자원 상태, 적용 환경, 만료되지 않은 증거를 결속. 작성자와 승인자를 고위험 변경에서 분리.
- **P-G4 실행:** 승인된 범위 밖 작업·삭제·권한 확대 거부. 외부 효과 UNKNOWN 시 반복 실행하지 않고 원장과 실제 상태 확인.
- **P-G5 재검증:** 운영에서 실제 취약점이 사라졌는지 확인. 성공한 배포만으로 `RESOLVED` 금지.
- **P-G6 지속성:** 새 CVE, 이미지 변경, 정책·키·신원·네트워크 변경 시 관련 증거 STALE 전환.

## P.5. AI 권한 경계

AI는 정형 `RemediationIntent`와 계획 초안만 생성한다. 운영 변경 자격증명, 고객 Secret 원문, 임의 IAM 권한 확대, 증거 서명 키, 승인 기능은 AI 런타임에 제공하지 않는다. 격리 환경에서 생긴 결과는 독립 검증기가 실제로 시험해야 한다. AI의 자체 PASS 설명은 증거로 채택하지 않는다.

## P.6. 패치와 마이그레이션의 결합

- 원본 AWS에서 수정한 애플리케이션과 목적지에서 실행할 애플리케이션의 digest 차이를 구분하고 각각 검증한다.
- 패치가 DB 스키마·시퀀스·이벤트 포맷을 바꾸면 Migration Unit의 원자성·복귀 전략을 재계획한다.
- 소스와 타깃의 보안 동등성은 같은 제품 사용 여부가 아니라 **동일 통제 목적의 실제 시험 통과**로 판정한다.
- 인증 라이브러리 패치나 OIDC Issuer 변경이 포함되면 Part II Identity Gate를 추가한다.
- Cutover 직전 승인 diff·이미지·구성·대상 UID가 변하면 이전 계획과 패치 승인 모두 재평가한다.

## P.7. 수락 시험

| ID | 시험 | 기대 결과 |
|---|---|---|
| P-AT-01 | CVE 탐지 후 자원 결속 실패 | 패치 계획 실행 불가 |
| P-AT-02 | AI가 운영 Secret·승인 키 요청 | 권한 거부·원문 비노출 |
| P-AT-03 | 패치 diff가 승인 후 변경 | 배포 차단·재승인 |
| P-AT-04 | 이미지 배포 성공, 취약점 미해소 | RESOLVED 판정 거부 |
| P-AT-05 | 보안 PASS, 업무 회귀 FAIL | 운영 승격 거부 |
| P-AT-06 | 리허설 성공 후 대상 UID 변경 | 기존 증거 STALE·재시험 |
| P-AT-07 | 외부 API 응답 유실 후 같은 작업 재전송 | 실제 효과 조회 및 멱등 중복 방지 |
| P-AT-08 | 승인된 패치가 데이터 복제 스키마를 변경 | Migration Unit 및 복귀 능력 재검토 |
| P-AT-09 | 소스와 목적지 보안 도구가 다름 | 통제 목적·실측 시험으로 동등성 판정 |
| P-AT-10 | 신규 취약점 공개 후 기존 PASS 증거 | 영향 있는 자원·통제만 무효화·재검증 |

## P.8. 도입 순서

P0: Finding의 실제 자원 결속, 검증 독립성, T10 승인 재사용, 배포 후 재검증.  
P1: 변경 영향 분석과 자동 증거 무효화.  
P2: 여러 공급자 지원, 복잡한 정합성 경계, 고급 자동 수정 운영.


---

# Part IV. SEC-GOV-EXT-001 — 보안·거버넌스 강화 계약

**버전:** 1.0-draft · **상태:** 신규 통합 제안 / 구현 미확정  
**기존 근거:** 사고 기반 보안 아키텍처 `CTRL-01~09`, `POL-01~12`, T5/T14/T19/T24/T33, MIG-SPEC-001 불변조건과 수락 시험을 계승한다.

## S.1. 보안·거버넌스 설계 원칙

1. 인증된 요청이더라도 승인된 `organization/application/resource/operation` 범위 밖은 거부한다.
2. AI·에이전트·고객 코드·Probe의 출력은 승인 권한이 아니다.
3. 검사 누락, 원격 응답 불명, 증거 만료, 출처 불명은 `PASS`로 취급하지 않는다.
4. 정책 해석과 실제 원격 효과를 분리해 기록한다.
5. 복구 권한, 감사 증거 수정 권한, 일반 운영 실행 권한을 분리한다.
6. 보안 사고 중에는 복구가 가능해 보인다는 이유만으로 자동 failback을 시작하지 않는다.
7. 동일한 통제라도 공급자별 검증 능력이 다르면 `PARTIAL` 또는 `UNSUPPORTED`를 명시한다.
8. `SECURITY_HOLD`는 일반 마이그레이션 진행·승인과 별도로 해제해야 한다.

## S.2. Security Incident & Hold Contract

### S.2.1 상태 정의

| 상태 | 의미 | 운영 변경 허용 |
|---|---|---|
| NORMAL | 통제 유효, 관측 범위 확인 | 기존 승인 조건을 충족할 때 가능 |
| SUSPECTED | 침해 의심·검증 진행 | 고위험 새 변경 보류, 최소 진단 허용 |
| SECURITY_HOLD | 침해 또는 중요 통제 불확실 | 새 cutover·patch·비인가 권한 발급 금지 |
| ISOLATING | 키·역할·에이전트·네트워크 격리 수행 | 허용 목록의 사고 대응 작업만 |
| RECOVERING | 신뢰 가능한 환경 재구축·복원·재검증 | 목적이 한정된 복구만 |
| REQUALIFYING | 보안·데이터·업무·신원 재시험 | 새 업무 쓰기 권한은 별도 승인 전 금지 |
| RELEASED | 독립 검토와 재승인 종료 | 새 승인 계획 기준으로 정상 실행 |

### S.2.2 필수 전이 조건

- 탐지 원천, 발생 시각, 영향 자원, 신뢰도, 증거 ID, 사고 담당자를 기록한다.
- `SECURITY_HOLD` 진입은 활성 실행기에게 전달하되, 원격 작업 중단의 실제 성공을 ACK로 확인한다.
- 실행 중인 T12의 `SAFE_HOLD`와 보안 사고 `SECURITY_HOLD`는 서로 다른 상태다. 하나로 합치거나 다른 하나를 자동 해제하지 않는다.
- 격리 전에 가능한 한 증거를 보존하고, 지속적 피해가 우려되면 차단을 우선한 뒤 예외를 기록한다.
- 격리 중에도 DB를 양쪽에서 열지 않는다. 복구 가능한 Writer 선택은 해당 컷오버 상태·복제 위치·신원 건전성이 확인된 경우에만 수행한다.
- 해제 승인자는 침해된 자격증명이나 사고 대상 운영자 신원만으로 승인할 수 없다.
- 복구 이후 기존 승인·기존 Security PASS 증거의 재사용은 영향 분석 및 재검증을 통과해야 한다.

### S.2.3 최소 사고 기록

`incident_id`, `tenant_id`, `affected_resource_uids`, `detector_identity`, `first_seen`, `severity`, `hold_scope`, `revocation_receipts`, `remote_effects`, `evidence_refs`, `recovery_plan_digest`, `reviewer_identity`, `release_approval_digest`, `closed_at`.

## S.3. Trusted Security Evidence Contract

### S.3.1 구조

```yaml
kind: SecurityEvidence
schema_version: 1
organization_id: org-example
application_id: app-example
environment_id: target-onprem
resource_uid: stable-provider-uid
operation_id: migration-run-id
test_id: IAM-01.1
test_spec_version: fixed-version
probe:
  identity: attested-runner-identity
  artifact_digest: sha256:verified-probe-image
  execution_id: unique-execution-id
observation:
  outcome: DENIED
  observed_at: 2026-11-11T00:00:00Z
result:
  verdict: PASS
  qualified_scope: anonymous-object-read
issued_at: 2026-11-11T00:00:00Z
expires_at: 2026-11-11T01:00:00Z
plan_digest: sha256:approved-plan
integrity:
  envelope_signature_ref: signed-evidence-reference
  storage_receipt_ref: immutable-receipt-reference
```

이는 목표 스키마 예시이며 현행 T14/T19 JSON 계약을 대체하지 않는다. 실제 도입 시 기존 `ControlRun`, `Evidence`, 서명·해시·승인 계약과 필드 매핑을 확정해야 한다.

### S.3.2 검증 원칙

- SHA-256 해시만으로 진짜 Probe의 실제 측정을 증명할 수 없으므로 신원·실행 환경·로그·관측의 독립 검증을 결합한다.
- 결과는 `test_id + spec_version + environment_id + resource_uid + plan_digest + probe_digest + time_window`에 결속한다.
- 같은 이름의 리소스를 다른 UID로 교체하면 기존 증거는 유효하지 않다.
- Probe 또는 검증기 이미지, 권한 정책, 연결 경로, 보안 대상, 관련 Secret 변경 시 영향받는 증거를 STALE로 전환한다.
- 필수 시험이 `UNKNOWN`, `NOT_RUN`, `STALE`, `EXPIRED`, `OUT_OF_SCOPE`이면 해당 프로필 게이트를 통과시키지 않는다.
- 불변 저장소에 기록됐다는 사실과 실제 대상에서 관측된 사실은 서로 다른 검증 항목이다.
- 도구 어댑터가 지원하지 않는 통제는 가짜 PASS를 만들지 않고 미지원 상태로 기록한다.

## S.4. Revocation & Credential Compromise Contract

- IAM/STS/WIF/OIDC/mTLS/OpenBao별로 `new_issue_blocked`, `existing_credential_rejected`, `active_session_terminated`, `downstream_access_denied`를 별도 검증한다.
- 단기 토큰 회수는 공급자 기술 제약을 반영한다. 회수 API 성공을 이미 발급된 모든 토큰의 즉시 폐기라고 주장하지 않는다.
- 고객·테넌트·애플리케이션·자원·작업별 신원 소유 경계를 유지한다.
- 권한 회수 후 새 에이전트가 침해된 이전 신원을 재사용하는 경우 등록 거부한다.
- 인증서/Issuer/JWKS 변경이 있을 경우 Part II의 Identity Gate와 운영자 SSO를 별개로 재검증한다.

## S.5. Security-Aware Cutover Contract

| 컷오버 단계 | 침해 또는 상태 불명 발견 시 필수 조치 |
|---|---|
| PRECHECK 전 | 새 실행 불가·진단 증거 수집 |
| ENTRY/PAUSE 이전 | 권한·증거 무효화 후 승인부터 재평가 |
| SOURCE FENCE 이후, TARGET 활성화 이전 | 기존 DB 무단 재개 금지·상태 조사·가능하면 SAFE_HOLD |
| COMMIT 이후 | 이미 발생한 외부 효과와 새로운 Writer를 조사; 일방적인 이전 환경 복귀 금지 |
| TARGET 활성화 이후 | 침해 범위와 최신 업무 쓰기를 보존한 상태에서 긴급 차단·복구를 별도 승인 |
| FAILBACK 중 | 역방향 복제·신원·원본 정합성 확인 없으면 진행 금지 |

원칙: 클러스터 합의 알고리즘이 있다는 사실만으로 DB Split-Brain 방지나 무중단을 입증할 수 없다. 실제 출발지 Fencing, 복제 Barrier, 목적지 활성화 조건, 중복 요청 방지, 전환 시퀀스를 측정하고 증명한다.

## S.6. 거버넌스: 분리해야 하는 권한

| 역할 | 할 수 있는 일 | 추가 안전 장치 |
|---|---|---|
| Developer/Planner | 계획 제안·리허설 요청 | 운영 쓰기 권한·승인 금지 |
| Security Reviewer | 위험 평가·증거 검증·Security Hold 권고 | 자신의 운영 변경 승인 제한 |
| Approver | 특정 digest·환경·기간의 작업 승인 | 작성자와 분리, 승인 회수 가능 |
| Operator | 이미 승인된 실행 수행 | 범위 밖 자원·권한 변경 금지 |
| Incident Commander | 사고 범위·긴급 차단 지시 | 사고 대응 작업 기록, 보류 해제 단독 금지 |
| Recovery Approver | 복구 및 보류 해제 독립 승인 | 침해 신원 사용 금지 |
| Audit Custodian | 감사 기록 보존 정책 관리 | 일반 운영 수정·삭제와 분리 |

비상 접근(Break-glass)은 인명·중대 장애 대응 등의 조건에서만 사전 정의된 제한 기간, 최소 권한, 별도 감사, 사후 독립 리뷰로 허용한다. 일반 승인 우회 수단으로 사용하지 않는다.

## S.7. 테넌트·데이터·정책 수명주기

1. 신규 고객 등록 시 소유 검증·리소스 UID 결속·기본 Deny·전용 권한 범위 확인.
2. 고객별 데이터 레지던시·보존·복구·백업 격리 요구를 Service Contract에 기록.
3. 인계 중 공유 DB/객체 저장소/네트워크를 쓰는 타 고객의 영향 범위를 검증.
4. 고객 해지 시 애플리케이션 자격·클라우드 연동·에이전트·백업·증거·복제 잔존 의존성 별도 처리.
5. 정책/권한/이미지/리소스 UID 변경 시 관련 승인·증거·능력 Qualification의 유효성 재평가.
6. 삭제 의무와 법적 보존 의무가 충돌하는 경우 자동 삭제하지 않고 정책에 따라 승인·예외 기록.

## S.8. 공격 및 사고 대응 수락 시험

| ID | 시나리오 | 기대 판정 |
|---|---|---|
| SG-AT-01 | 침해된 에이전트가 이전 실행 요청 | SECURITY_HOLD·실행 거부 |
| SG-AT-02 | 신원 회수 API 성공, 기존 토큰으로 API 호출 가능 | 회수 완료 판정 금지 |
| SG-AT-03 | Probe의 JSON PASS 변조 | 출처·서명·실행 범위 불일치로 차단 |
| SG-AT-04 | 다른 환경의 유효한 ControlRun 재생 | 환경 UID 결속 실패·차단 |
| SG-AT-05 | 리허설 후 버킷 ACL 변경 | 이전 PASS 증거 STALE·게이트 거부 |
| SG-AT-06 | SOURCE FENCE 후 원격 응답 유실 | TARGET 활성화 금지·SAFE_HOLD 또는 안전한 상태 재확인 |
| SG-AT-07 | TARGET 새 쓰기 직후 보안 사고 | AWS로 무조건 자동 failback 금지 |
| SG-AT-08 | 운영자 한 명이 계획·승인·보류 해제 전부 수행 | SoD 정책 위반·차단 |
| SG-AT-09 | 고객 A 해지 후 권한·Secret·복제 경로 잔존 | 해지 완료 판정 거부 |
| SG-AT-10 | 감사 증거 삭제 시도 | 독립 보관·권한 거부 검증 |
| SG-AT-11 | 다중 고객 공유 DB에서 일부만 PAUSE | 계약 미지원이면 컷오버 계획 거부 |
| SG-AT-12 | 보안 검증 실행기 버전 교체 | 관련 통제 증거 재검증·영향 색인 갱신 |

## S.9. 기존 스펙과의 결합 규칙

- `ServiceContract`에는 `security_hold_policy`, `evidence_freshness`, `incident_recovery_objectives`를 추가 제안한다.
- `DependencySnapshot`은 워크로드 신원·토큰 Issuer·감사 경로·위험한 공유 자원을 명시한다.
- `CapabilityManifest`는 사고 대응과 실제 회수 가능성을 방향별 지원 능력으로 기술한다.
- `Plan Compiler`는 Security Hold, 사고 범위, 미지원 통제, 만료 증거를 사전조건으로 거부한다.
- `Approval`에는 `security_review_receipt`와 긴급 복구·승인 취소 효과를 연결한다.
- `Effect Ledger`에는 원격 권한 회수·차단·복구 효과와 불확실성을 별도 기록한다.
- `ExitContract`에는 출발지 신원·DNS·비밀·감사 저장소·복구 자산의 잔여 의존성을 포함한다.

**도입 전제:** 위 필드 추가는 기존 클라이언트·게이트·감사 스키마 호환성 확인, ADR 승인, 단계별 마이그레이션이 필요하다. 본 문서만으로 현행 운영 API가 바뀌지 않는다.


---

# Part V. 현재 platform 통합 방향과 검증 로드맵

## V.1. 기존 구현을 보존하는 원칙

- **T5/T14/T19:** 기존 6개 보안 통제 및 Evidence/ControlRun/Gate를 재사용. 새 SecurityEvidence는 별도 중복 구현하지 말고 검증된 확장 필드로 통합한다.
- **T10:** 승인·상태 머신·실행 원장 계약을 보존. 사고 보류는 별도 보안 상태로 추가 검토한다.
- **T12/T13/T20:** Fencing, Barrier, Fingerprint, Failback을 재사용. 클러스터 합의 알고리즘을 새로 도입한다는 가정은 하지 않는다.
- **T15/T31:** 리허설·이미지 승격·실행 검증을 패치와 이전의 공통 경로로 사용한다.
- **T24/T33/T34/T35:** 감사 증거, 탐지 수집, 백업·복구 시험을 사고 대응 및 지속 검증에 연결한다.
- **T47:** Cognito→Keycloak을 특정 프로필의 조건부 Identity Migration 경로로 둔다. 일반 이전을 해당 기능에 종속시키지 않는다.

## V.2. 도입 단계

| 단계 | 범위 | 수락 조건 |
|---|---|---|
| V0 | 기존 AWS→온프렘 프로필의 계약·관측·결과 정리 | 현재 구현/모의/실환경 증거를 구분 |
| V1 | 증거 UID·출처·만료·영향 색인 공통화 | 변조·재생·드리프트 시 게이트 차단 |
| V2 | 보안 사고 상태·자격 회수·승인 분리 | 보류 중 Cutover·Patch 실행 불가 |
| V3 | Cognito Discovery·Identity Gate·조건부 실제 이전 | 로그인·권한·원본 독립성 검증 |
| V4 | 패치 계획→격리 시험→승인→적용→재스캔 | 실제 보안 수정 및 비기능 회귀 검증 |
| V5 | 방향별 Capability Qualification·Exit Drill 확대 | 지원하지 않는 조합을 PASS로 표기하지 않음 |

## V.3. 필수 통합 E2E 시나리오

1. AWS EKS의 업무 메시지 생성, Mac mini 온프렘 RKE2 복제 확인.
2. 목적지 익명 읽기가 가능한 시험용 객체 스토리지에서 T14 실패 → T19 차단 → T10 이전 실행 거부 확인.
3. 승인된 정책 수정과 새 Probe 결과로 재검증. 기존 PASS 증거 재사용이 아닌 새 Run인지 확인.
4. 실제 Writer Fencing + Barrier + 정합성 + 목적지 Writer 인계. 전환 중 응답 유실과 상태 불명은 SAFE_HOLD.
5. 출발지 앱 중지 후 새 메시지 생성·기존 데이터 조회·외부 의존성 검사.
6. Cognito를 그대로 유지하는 프로필과 교체하는 프로필을 구분. 교체 프로필에서 실제 신규 로그인·그룹 정책·토큰 갱신 시험.
7. 보안 사고 시 SECURITY_HOLD → 권한 회수 실효 시험 → 재승인 후 재개.
8. 완료 후 취약점·설정 변화가 발생하면 기존 증거를 무효화하고 재진단.

## V.4. 완료 기준과 표현

`설계 완료 / 구현 중 / 모의 시험 통과 / 실환경 시험 통과 / 미검증 / 지원하지 않음`을 구분한다. 특정 경로에서 통과한 시험을 임의의 클라우드·서비스·DB에 일반화하지 않는다. 실제 쓰기 정지 시간을 측정하지 않았다면 '완전 무중단', 복제 무손실을 측정하지 않았다면 'RPO=0', IdP 별 실증이 없으면 '세션 무중단 이전'을 주장하지 않는다.

## V.5. 연관 원문

- 원본 상세 명세: `contract-driven-migration-spec-v1.0.md` / MIG-SPEC-001.
- Cognito 확장 원문: `identity-access-cognito-extension-v1.0.md` / MIG-EXT-IAM-001.
- 보안 사고 기반 설계: https://github.com/Softbank-Hackathon-Finals/platform/blob/develop/docs/security/incident-based-architecture.md
- 지속적 보안 개선 설계: https://github.com/Softbank-Hackathon-Finals/platform/blob/develop/docs/security/continuous-secure-modernization.md
- 공식 작업 상태: https://github.com/Softbank-Hackathon-Finals/platform/blob/develop/docs/tasks.md
- 컷오버 실행기: https://github.com/Softbank-Hackathon-Finals/platform/blob/develop/services/cutover-executor/README.md

---

# 통합 문서 최종 정의

**서비스가 지켜야 할 업무·데이터·보안·신원·가용성 계약을 출발지와 목적지의 검증된 능력에 맞춰 운영 인계 계획으로 컴파일하고, 승인된 단일 권한 인계와 독립 검증으로 실행한다. 이전 후에도 외부 의존성·복귀 가능성·보안 취약점·정책 드리프트를 지속 확인하며, 침해·불확실성·증거 부족 시 변경을 보류하고 신뢰할 수 있는 복구 계약을 적용하는 플랫폼이다.**

**이 문서는 설계 통합본이지 구현 완료 보고서가 아니다.**
