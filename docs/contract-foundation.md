# 계약·능력 기반 개발 · 첫 구현

개발 기준은 [통합 명세 1.1](specifications/contract-driven-migration-platform-v1.1-integrated-notion.md)과 [채택 ADR](adr/0001-contract-driven-foundation.md)이다. 이 문서는 최초 L0/C0·C3 구현만 설명한다. 전체 compiler·등록 API·클라우드 이전 완료 보고서가 아니다.

## 입력 계약

`ServiceContract`는 업무 요구, 접수/완료 의미, 데이터 범위, 가용성, 보안 통제, 위치, 복구·승인·탈출 요구를 보존한다. versioned JSON의 구조를 검사한 뒤 64 KiB 이하의 불변 정규 표현과 SHA-256을 만든다. 중복 키·모르는 필드·누락된 필수 구역·중복 통제·부정확한 숫자·다른 tenant를 거부한다.

수치는 IEEE-754에서 정확히 표현되는 비음수 정수로 제한하며 revision은 양수다. 정밀한 큰 수·금액은 후속 전용 문자열 규칙으로 정의한다. 이 정규화는 기존 `canonical_json`의 제한 프로필이며 RFC 8785/JCS 전체 준수나 업무 의미의 동등성을 주장하지 않는다. 사본을 수정해도 이미 만든 계약의 bytes/digest는 바뀌지 않는다.

정합성 분할은 `verified_partition`을 명시하고 분할 계약과 교차 분할 거래 거부를 요구한다. 이 선언의 사실성은 경계 분석·실측이 추가로 증명해야 한다. 현재 코드가 계약 작성자를 인증하거나 DB에 계약을 등록하지는 않는다.

Identity/사고 보류/증거 기한/사고 복구 확장은 원문과 digest에 보존하지만 현재 의미 판정은 미구현이다. 결과에 `unevaluated_extensions`로 표시하고 C3 후보도 보류한다. 필드를 버리고 유리한 계약으로 바꾸지 않는다.

## 오프라인 사용

루트 README의 설치 절차로 기존에 고정한 `sova-security==0.11.1`과 Sova를 준비한다. 아래 예제는 합성 요구이며 실제 앱·SLO의 측정값이 아니다.

```sh
sova service-contract-example > .local-notes/service-contract.json
sova service-contract-inspect .local-notes/service-contract.json --tenant org-example
```

소스 개발 환경에서는 같은 가상환경과 source 경로로 `python -m sova`를 사용할 수 있다. `python -m sova.interfaces.cli`는 실행 진입점이 아니며 종료 코드만으로 CLI 수행을 확인하면 안 된다. [검토 가능한 예제](../examples/contracts/orders-service.json)

`NORMALIZED`는 구조 검사와 동결 성공이다. 출력의 `registered`, `live_verified`, `executable`은 모두 false다. `--tenant`는 오프라인 범위 대조값이며 실제 사용자 인증이 아니다. 호스팅 registry는 인증된 주체로 tenant를 정하고 객체별 인가·revision CAS·등록 원장을 별도로 제공해야 한다.

## 방향별 능력과 자격

`EnvironmentProfile`은 profile ID/revision, provider, compute backend, data engine, identity, network, architecture, component versions를 독립 축으로 가진다. AWS라고 Kubernetes를 가정하지 않는다. 같은 기술 조합도 profile ID/revision이 바뀌면 기존 결속을 재평가한다.

`CapabilityManifest`는 adapter/strategy의 버전·code digest·지원 operation/profile을 동결한다. manifest에 지원이라고 적는 것만으로 qualification이 생기지 않는다.

`StrategyQualification`은 tenant·service·정확한 계약 digest, manifest digest, 순서 있는 source/target profile, operation 집합, 행 수 상한, 증거 등급, 발급/만료, verifier와 시험 코드·원본 증거 지문을 결속한다. 이는 일반 자격 기록의 **첫 제한 모델**이다. 현재 부하·지연·손실·스토리지·failure model의 모든 축을 구현한 §28.4 전체 자격 레코드로 광고하지 않는다. 추가 축은 profile/contract에 명시적으로 동결하거나 후속 스키마를 정의하고 재자격해야 한다.

`assess_strategy`는 신뢰된 호스트의 `authenticate(record)`와 시계를 요구한다. 인증 경계는 전체 record의 현재 서명·신뢰된 issuer·원본·등록·철회를 검증하고 정확한 True만 반환해야 한다. 이 연결은 아직 운영 registry에 구현하지 않았다. `lambda _: True`나 사용자 JSON을 운영 인증으로 쓰면 안 된다. 오류는 정제된 코드로 반환하며 원격 예외·자격값을 출력하지 않는다.

| 결과 | 의미 |
|---|---|
| QUALIFIED_CANDIDATE | 해당 C3 방향·operation·한도·등급·시점의 자격 인증을 확인. 전체 서비스 계약 충족이나 실행 가능을 뜻하지 않음 |
| NEEDS_EVIDENCE | 자격 누락·만료·다른 방향/계약/이미지·한도 초과·인증 실패·미평가 확장 |
| UNSUPPORTED | 선택한 manifest가 요청 profile 또는 operation을 제공하지 않음. 모든 기술적 대안이 불가능하다는 뜻은 아님 |

모든 결과는 `contract_satisfied=false`, `executable=false`, `authorizes_execution=false`다. `COMPILED` 상태나 실행 명령은 아직 만들지 않는다. 정방향 자격으로 역방향·failback을 추론하지 않으며 local/unit 등급을 live로 바꾸지 않는다.

## 기존 구현 재사용

- JSON decoder를 순수 domain 유틸리티로 옮기고 기존 `graph_codec.json_document` import를 유지했다. domain이 파일/네트워크 adapter에 의존하지 않도록 했다.
- 기존 보안 서명·현재 등록·보류 판정은 security가 소유한다. 새 자격 인증용 host adapter는 기존 신뢰 경계를 소비하도록 후속 구현한다.
- 계획·승인·PostgreSQL 원장·outbox·실행권·효과 조정, 기존 실제 대응 실행기는 변경하지 않았다.
- 첫 후보 조회는 compiler의 전체 C0~C12, all-writer fencing, 데이터 비교, 사고 복구, Cognito 이전, AI/Harbor E2E를 완성하지 않는다.

다음 단계는 계약에 결속한 의존성 snapshot·조사 공백·강한 정합성 경계와 인증된 qualification 저장/조회다. 그 뒤 typed 계획과 기존 승인·원장을 연결한다.
