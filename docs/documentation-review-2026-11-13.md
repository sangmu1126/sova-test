# README·문서 재점검 — 2026-11-13

## 기준과 변경 범위

- 점검 코드: `feature/local-onprem-response`의 `22ce74df1cb13293aeb24e36c90a51c2b0da4a15`.
- GitHub에서 확인한 팀·개인 sova-test `main`: `619fb6e396cca9139e219c2b9dd7e3bdfcea7a76`. 최신 대응 실행기가 있는 작업 브랜치와 다르다.
- Security 대조 기준: `5b5be0e32c03c109ce55b21090e863a6a9bac079`, package 0.11.1.
- 문서 수정은 별도 `docs/reconcile-readmes-20261113` 브랜치에서 수행했다. 문서 커밋은 검증 producer나 배포 image source를 대체하지 않는다. 이 기록은 병합·배포 완료 보고가 아니다.

이번 범위는 README·문서·기존 코드의 대조다. 클라우드 조회·변경, VM/클러스터 재실행, 서비스 운영 인수, 새 이미지 검사나 지속 시험을 수행하지 않았다.

## 발견과 수정

| 발견 | 수정 | 근거 |
|---|---|---|
| 공개 main README와 최신 로컬 대응 기능의 범위가 다름 | 코드 경로·브랜치·증거 producer·배포 digest를 분리 | README, `demo/local_response`, 과거 main 확인 |
| sibling security 설치 경로가 잘못됨 | `security/`와 `sova-test/`가 같은 상위 폴더인 배치를 명시 | 두 pyproject와 CLI |
| 초기 React·무상태 격리·정적 9개 시나리오·최신 실제 실행기 안내 혼재 | 화면별 진입점과 적용 범위, 문서 전체 목차 추가 | `console/`, `demo/onprem_response`, `demo/response_scenarios`, `demo/local_response` |
| P0·lifecycle·outbox 문서가 후속 PostgreSQL/SQS 구현을 미구현처럼 설명 | 초기 단계 기록임을 표시하고 후속 실행 원장·실험 기록 연결 | PostgreSQL 어댑터, `sqs_operations.py`, 날짜별 구현 현황 |
| 구현 현황의 WORM 미검증 문구와 후속 시험 기록이 충돌 | 개인 GOVERNANCE 1일 시험과 팀 T24·COMPLIANCE·장기 보존을 분리 | `demo/audit_shipper`, 감사 IaC, 실행 기록 |
| Terraform 상태의 상대 경로가 cd 뒤 다른 폴더를 가리킬 수 있음 | 절대 경로·고정 PYTHONPATH·서버/터널 별도 터미널 조건 명시 | 실제 CLI 인자와 실행기의 상태 로더 |
| 신규 PC에 checkout하면 실행 가능한 것으로 읽힐 수 있음 | 고정 VM·클러스터 UID·기존 상태·승인자/이미지 준비 조건 명시 | `demo/local_response/kube.py`, hybrid/provision 진입점 |
| 과거 Sky 링크와 추출된 시험 파일 경로가 깨짐 | 없는 Sky 문서는 출처 설명으로 보존, RDS 시험은 security 정본 링크로 수정 | 파일 존재·추출 후 경로 |
| PDF 파일명과 내부 버전이 다름 | 두 PDF가 v1.1 내부 표제를 가진 과거 스냅샷임을 목차에 명시 | PDFKit 텍스트 추출: 파일 v1은 25쪽, v1.1은 20쪽 |

과거 시험 수·생산자 SHA·증거·그림·제안 원문은 변경하지 않았다. 11/2의 929개 시험을 현재 전체 시험 수로 재표기하지 않는다.

## 다시 확인한 경계

| 항목 | 문서에서 유지할 표현 |
|---|---|
| 최신 9개 대응 경로 | 개인 EKS→이 Mac RKE2 / 같은 Mac RKE2의 제한된 전용 앱. 임의 고객 앱 대응 자격 아님 |
| 데이터 전환 | 2,000행 상한 stop-and-copy. CDC·무중단·일반 failback 자격 아님 |
| 패치 | 통제된 결함 수정·digest 교체와 이미지 취약점 해소는 별도. 81개 잔존 결과를 PASS로 바꾸지 않음 |
| Security 서비스 이미지 | 별도 산출물. 서비스의 특정 이미지 0건 결과를 대응 앱 이미지에 적용하지 않음 |
| 감사 보존 | 11/12 전용 GOV 1일 시험. 현재 잠금 유지·무기한 보존·팀 운영 인수로 확대하지 않음 |
| 팀 플랫폼 연결 | 고정 계약·로컬 소비 시험과 실제 계획 권한·SSE·운영 승인·T10/T12 실행 인수를 구분 |

## 문서만으로 아직 확인할 수 없는 것

1. 최신 9개 자격·콘솔 104회 반복·411개 업로드의 상세 원본은 비공개 상태에 보관돼 있다. 이 저장소에는 실행 요약이 있고 최신 `qualification-*`·`support-record-*` 원본은 없다. 이전 11/8 JSON을 이 원본으로 대체하지 않는다. 외부 검토에는 정제한 서명 증거와 공개 신뢰 anchor를 별도 인계해야 한다.
2. MIG-SPEC-001 통합 1.1-draft 전문과 고정 원문 링크가 없다. 요구 ID 추적표는 남기되 저장소 목표 명세 v2와 같은 번호 체계·자동 우선순위로 취급하지 않는다.
3. 현재 배포 imageID·서명 키 신뢰·자격 만료·실제 접근 거부·복구·운영 접속은 새 관측이 필요하다. 과거 문서의 Ready/PASS로 채우지 않는다.
4. 외부 URL 전체의 현재 응답, 인용 규정의 현행 효력, 장기 artifact 다운로드 가능성은 이번 점검으로 보증하지 않는다.

## 점검 방법과 결과

- root·중첩 README와 `docs/` 전체: 최종 105개 파일(그중 Markdown 53개)을 목록화하고 파일별 SHA-256을 기록했다. Markdown/HTML/SVG 링크의 저장소 내부 대상·헤딩을 검사했다.
- JSON/JSONL 구문, 텍스트 UTF-8, SVG/drawio XML, PNG 헤더·크기, ZIP CRC를 검사했다. PDF 2개는 PDFKit으로 열어 본문 텍스트와 페이지 수를 확인했다.
- 썸네일·캡처의 선언 hash/크기 2개가 실제 PNG와 일치했다. 공개 썸네일은 11/8 당시 시험 화면이며 현재 관측이 아니다.
- 2026-11-14 정정: 당시 기동 확인에 사용한 `python -m sova.interfaces.cli`는 실제 실행 진입점이 아니었다. 따라서 그 종료 코드 0만으로 README 명령이 수행됐다는 근거로 쓰지 않는다. 후속 개발에서 올바른 `python -m sova`의 JSON 출력과 실제 CLI 시험을 검증했다([개발 기록](development/contract-foundation-2026-11-14.md)). 패키지 설치·클라우드 호출·실행 승인은 이 기동 점검에서 수행하지 않았다.
- 실행 예시의 shell 6개 블록과 모듈 명령 11개의 CLI 인자를 검사해 통과했다. 상태 변경 명령 자체는 실행하지 않았다.
- 과거 증빙·제안·그림의 원본 bytes는 보존했다. 문서 링크 검사와 `git diff --check`에서 오류 0건을 확인했다.

전체 앱 회귀·실환경 자격 시험을 새로 통과했다는 기록이 아니다. [전체 문서 목차](README.md)에서 현재 안내와 날짜별 이력을 구분한다.

## 2026-11-14 후속

사용자가 통합 상세 명세 전문을 제공해 `specifications/`에 원문과 hash를 보존했다. 위의 원문 미포함 지적은 11/13 당시 상태이며 현재 개발 기준은 [채택 ADR](adr/0001-contract-driven-foundation.md)을 따른다. 새로 확보한 원문을 기존 실환경 시험의 소급 증거로 쓰지 않는다.
