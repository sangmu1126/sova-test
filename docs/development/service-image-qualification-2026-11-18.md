# 서비스 이미지·워커 진입점 개발 기록

2026-11-18 · 개인 `feature/migration-request-qualification`.

## 범위와 선택

시작 SHA는 `afcab339e4bd6c23e242dd758ee1e379797f2834`다. 다른 개인 작업 트리는 `d664566`에서 깨끗한 상태였고, 세션·볼륨 어댑터를 진행한 것을 확인했다. 이번 소유 범위는 `qualification/image.py`, 이미지 전용 시험·문서로 제한했다. `SESSION-SCOPES.json`은 작업 공지이며 상대 세션의 동의나 잠금은 아니다.

바로 공유 kind에 배포하는 대신, **시험한 코드와 실행 이미지가 같은지** 먼저 검사했다. 두 기존 kind 클러스터·팀 저장소·AWS·공용 DB·kubeconfig는 변경하지 않았다. 제품의 worker/catalog/정지 증명 구현도 변경하지 않았다.

## 구현과 발견

- `c0f9b2ae0946f35b1942b79859d146a79cef24e1`: 두 고정 Git 소스와 캐시 이미지의 설치 payload 대조, 실제 이미지 CLI 시작 거부 시험.
- `24e17165ac6ef657ba9fa79c4ab8f784e98f20eb`: 기존 SQL 별칭을 고정 패키지 내부의 일반 파일에 한해서 해석. 외부 링크·미등록 링크는 계속 거부.

첫 대조는 기존 저장소의 SQL symlink를 전부 거부해 `source_symlink_or_special_file`로 중단했다. 잘못된 PASS를 내지 않았으며 `cached-image.json`을 보존했다. 원인을 확인해 내부 링크 규칙을 구현하고 단위 회귀를 추가했다. 두 번째 대조에서는 **Sova 파일 누락 33·변경 23개**를 확인했다. 이 오류를 이미지 소스 불일치와 혼동하지 않는다.

기존 이미지에서도 CLI 네 가지를 실행했다. 빈 catalog 거부만 통과했고 health port·lease·poll의 최신 계약 오류는 나오지 않았다. 비정상 종료 자체를 성공으로 보지 않는 시험이 실제 구버전 차이를 잡았다.

## 빌드와 검증

고정 Git archive로 별도 빌드 폴더를 만들었다. `.env`·로컬 키·원문 증거·작업 트리의 다른 파일은 Docker context에 넣지 않았다. 기존 production Dockerfile과 고정 Python digest를 그대로 썼다. Security 생산자 `27e6e855…`의 wheel SHA-256은 `a40e4925e4f70f0c62daf7149e87eb0b8e01ca50386c741caddc8c5fc610e8ce`다.

새 로컬 이미지 ID는 `sha256:117a14eadae2cc25b43a434dbf9a06e1497f8e1c96ebc5e9da62a4c0fb7591f6`이며, 기존 `sova-service:dev`는 유지했다. 이미지 빌드 과정의 `pip check`도 통과했다. 의존성은 기존 범위 지정으로 해결했고 잠긴 release로 주장하지 않는다.

| 검증 | 결과 |
|---|---|
| 신규 이미지 대조·거부 단위 시험 | 36 PASS |
| 기존 시작·catalog 단위 회귀 포함 전체 | **121 PASS, 실패·오류·건너뜀 0** |
| 새 이미지 설치 Python·SQL | Sova 317개 + Security 122개 전부 일치 |
| 새 이미지의 실제 워커 CLI | 네 가지 모두 지정된 계약 오류로 정상 거부 |
| Ruff·공백 검사 | 통과 |
| 시험 컨테이너 | 전부 종료·삭제 확인 |

단위 환경은 기존 의존성이 갖춰진 가상환경을 패키지 설치·갱신 없이 사용했다. bytecode와 pytest cache 쓰기를 막았다. 121개에는 과거 실행과 겹치는 회귀가 있으므로 과거 숫자와 합산하지 않는다. CLI 시험은 정상 기동 성공 시험이 아니다.

## 증거와 다음 경계

원문은 `local-dev-records/service-image-qualification-20261118/`에 보존했다. 최초 거부, 구이미지 불일치·CLI 실패, 새 이미지 대조·CLI, 빌드 로그·wheel·빌드 입력·JUnit을 남겼다. [공개 요약](../evidence/service-image-qualification-20261118.json)에 원문 해시와 소스·이미지 식별자를 연결한다. 생성한 이미지와 wheel은 로컬에 보존했고 레지스트리에 발행하지 않았다.

앞선 계획에서 실제 worker CLI·Pod 인수 전체를 한 번에 다루지 않고, 발견된 **캐시 이미지 소스 불일치**를 먼저 닫았다. 다음은 이 이미지의 정상 `run-worker`를 전용 Kubernetes 공간에서 실행하고 실제 SA/Pod UID·catalog·lease·인증된 정지 증명을 확인하는 것이다. Ready만으로 복구 PASS를 발급하지 않고, 구 worker가 살아 있거나 관측이 불확실하면 보류되는 경우도 확인해야 한다.
