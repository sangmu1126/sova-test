# ADR-0001 · 통합 명세를 개인 제품의 개발 기준으로 채택

- 날짜: 2026-11-14
- 상태: 개인 개발 채택. 팀 platform의 설계·API·운영 정책 변경 없음.
- 입력: [통합 상세 명세 원문](../specifications/contract-driven-migration-platform-v1.1-integrated-notion.md)과 [원문 hash](../specifications/manifest.json).

## 결정

개인 Sova의 다음 개발은 MIG-SPEC-001-CONSOLIDATED 1.1-draft를 목표로 한다. 원문은 사용자 제공 bytes 그대로 보존하며 내부의 예시 API·성능값·지원 표기를 현재 구현으로 받아들이지 않는다. Part I/II의 원문 보존과 Part III/IV의 확장 제안도 구분한다.

기존 v2와 여섯 영역 상세 설계는 이력과 구현 자산으로 유지한다. 충돌하는 프로토콜·스키마·권한 계약은 새 ADR과 명시적 버전으로 결정한다. 이번 채택만으로 기존 실행 API, package 경계, 지원 자격이나 신뢰 SHA를 바꾸지 않는다.

## 코드 경계

- `sova-test`: 서비스 요구 계약, 방향별 능력 카탈로그, 계획 컴파일, 실행·효과 원장과 기존 실행기 소비.
- `security`: 기존 scope·revision·서명·보안 보류·정책 판정과 서비스. 복제된 새 SecurityEvidence 정본이나 별도 승인 엔진을 만들지 않는다.
- `demo/local_response`: 기존 개인 실환경 검증 자산. 범용 제품 compiler의 권한 발급기로 사용하지 않는다.
- 팀 `platform`: 참조 계약과 향후 인수 접점. 현재 태스크·운영 자원·권한을 변경하지 않는다.

## 첫 단계

L0/C0의 불변 ServiceContract 입력과 L0/C3의 방향별 qualification 판정을 먼저 구현한다. 컴파일 결과·자격 표시는 실행 승인과 별도다. 입력 문서·manifest의 `supported` 문자열, 사용자 제출 boolean, 반대 방향의 성공을 운영 자격으로 승격하지 않는다. 필요한 증거가 없으면 NEEDS_EVIDENCE로 남긴다.

초기 입력은 versioned JSON이다. YAML·임의 객체 생성·플러그인 실행·클라우드 API 호출은 이 단계의 입력 처리에 포함하지 않는다. 서비스 계약은 정규화·범위 검증·변경 지문만 제공하며 등록자의 권한과 업무 의미의 진실성은 인증된 registry가 후속 검증한다.

## 이후 단계

1. L0/C1~C2: 증거 기반 의존성, 조사 공백, 업무 정합성 경계. 호출 SCC만으로 MigrationUnit을 확정하지 않는다.
2. C4~C11: 보안·위치·비용·복구 제약과 typed DAG·고정 입력 digest.
3. L1/C12: 기존 PostgreSQL 계획/승인·outbox·실행권·효과 원장과 연결.
4. L2: all-writer fencing, 활성화 intent, 신규 쓰기와 복구의 실제 인수.
5. V1~V5: 증거 영향 색인·독립 보안 사고 상태·Identity·패치와 독립 운영 자격 확대.

Temporal·OPA·SPIFFE·복제 엔진·PKI는 명세의 참조 선택이다. 이름만 맞추기 위한 재구축을 하지 않고 도입 필요와 실제 능력은 별도 자격으로 결정한다. Cognito는 고객 앱의 선택적 공급자 의존성으로 두며 플랫폼 필수 로그인으로 강제하지 않는다.
