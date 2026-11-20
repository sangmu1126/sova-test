# 개인 AWS → 이 Mac의 Linux VM 인수

대상 계정은 `265233844540`, 프로필은 `default`로 고정한다. 기존 Sky·팀 VPC·DB·클러스터는 변경하지 않는다. 이 모듈은 새로운 실행 ID의 VPC, 수신 포트가 없는 EC2, 비공개 산출물 저장소, 불변 이미지 저장소와 값이 비어 있는 Secret 참조만 만든다. 비밀값은 Terraform에 전달하지 않는다.

## 순서

1. STS 신원, AMI의 Amazon 소유권·ARM64 아키텍처, 목적지 VM 마커·machine ID·클러스터 UID를 실제 조회한다.
2. 새로운 비공개 상태 폴더에 backend 경로와 tfvars를 만든다. `.terraform` 데이터·state·plan·tfvars는 Git에 넣지 않는다.
3. 고정 provider lock으로 `init`, `validate`, `plan`을 실행하고 계획에 새 실행의 자원만 있는지 검토한다. 검토한 plan만 apply한다.
4. 생성된 인스턴스의 run 태그와 SSM 인벤토리를 대조한다. 앱·DB 포트를 공개하지 않고 SSM 인증 터널을 사용한다. 목적지는 로컬 Linux VM이다. VM에서 AWS 서비스 계정이나 팀 tailnet을 사용하지 않는다.
5. 실제 앱·DB·목적지 보호·경로를 준비한 뒤 기존 네이티브 등록·계획·승인·실행 경로를 사용한다. 자원 생성이나 Pod Ready는 이전 성공이 아니다.

AMI는 [AWS의 공용 SSM 파라미터](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/finding-an-ami-parameter-store.html)를 실제 조회해 정확한 이미지 ID로 고정한다. 워크로드의 경로 제어에는 기존 Service/Gateway API 어댑터를 우선 재사용한다. 초기 v1.7 설치 예시는 현재 EOL이므로 사용하지 않는다. [공식 호환성 표](https://gateway.envoyproxy.io/news/releases/matrix/)와 [v1.10.0 릴리스](https://github.com/envoyproxy/gateway/releases/tag/v1.10.0)를 조회했으며, 실제 목적지 버전과 일치하는 고정 이미지·설치를 인수한다. 아직 배포 완료를 뜻하지 않는다.

## 비용과 보존

- 한 대의 `t4g.small`에 앞으로 최대 8시간 이내의 절대 정지 시각을 둔다. 타이머는 재부팅 시 수명을 연장하지 않는다. 이는 컴퓨트 시간 제한이며 청구액 상한이 아니다. 정지 상태와 타이머 동작은 별도 관측한다.
- root EBS는 암호화하고 인스턴스 종료 시 자동 삭제하지 않는다. EBS·이미지·산출물·Secret은 보존 비용이 발생한다.
- 버킷·ECR은 강제 삭제하지 않는다. 원본 데이터 보존·신규 쓰기 대조·복귀 증거를 확인한 뒤 실행 ID를 검증해 별도 정리한다.
- 단일 EC2·단일 로컬 VM은 이전 경로 시험 환경이다. HA·금융 운영 인증·인터넷 공개 서비스의 무중단 자격은 이 구성만으로 충족되지 않는다.


## 종류별 저장 위치 관측

`collect_placement_facts.py`는 이 경로의 고정된 소유 EC2/EBS와 VM/클러스터/namespace만 읽는다. 만료되지 않은 private 입력과 실제 TLS 연결 파일을 `--inputs`, `--connections`로 지정하고, 아직 없는 전용 출력 폴더를 `--artifacts`로 지정한다. AWS `default`의 실제 계정과 태그를 확인하며, SSM에서는 고정된 읽기 전용 projection만 실행한다. VM에서는 조회·파일 해시 계산만 수행한다. 앱·데이터·권한·경로·worker 설정은 변경하지 않는다.

DB의 실제 data directory와 컨테이너 mount, 원본 NVMe serial/EBS attachment, 목적지 Pod/PVC/PV/LUKS를 각각 대조한다. 보관 백업과 authority witness는 별도 파일·장치·SHA를 확인한다. 다른 장치·재생성된 namespace·다른 DB·바뀐 백업은 거부한다. 실제 파일의 크기·변경과 수집 코드의 변경도 검사한다.

이 결과는 독립 검증자나 실행 gate에 바로 넣는 PASS 문서가 아니다. 모든 사본·로그 수신처·국가 위치·AI 처리·보관 기간·복구가 확인된 것이 아니므로 `residency_obligation_satisfied`와 `migration_authorized`는 false로 남는다. [실제 측정과 한계](../../docs/evidence/personal-category-placement-20261120.json).


## 부분 변경 관측의 영속 저장

`prepare_observer_role.py`는 기존 강제 RLS의 소유 control DB에 **새 관측 역할 하나**를 만든다. 기존 역할을 재사용·초기화하거나 권한을 넓히지 않는다. private 입력·연결 파일과 새 출력 폴더가 필요하다. 정확한 DB system ID와 기존 RLS를 먼저 검사하며, 비밀번호를 출력하지 않는다. 이 역할에만 적용되는 restrictive 정책이 tenant/app/source/수집기/종류/기한을 고정한다. watermark·registry 주장·계획/효과·자체 변경 원인 주장·업무 DB 접근은 허용하지 않는다. 검증 실패 시 새 역할의 LOGIN을 닫고 원문은 보존한다.

`qualify_durable_change_observers.py`는 `--collector-directory`의 검증된 전용 역할을 필수로 사용한다. 운영자용 `--control-directory`는 전후 계획/작업/효과 조회에만 따로 사용한다. 새 scratch DB에서 실제 스키마 변경을 만들고 실제 AWS 이력과 함께 저장·재조회한다. **운영자 인수 하네스**이므로 scratch 생성용 source 관리자 자격도 사용하며, 일반 수집 서비스 배포를 뜻하지 않는다. 역할 생성 후의 인수 검사이며 운영 자동 실행 명령으로 노출하지 않는다.

`qualify_observer_expiry.py`는 기존 기한이 20분 이내일 때만 시작한다. 실제 TLS 세션에서 사전 관측 조회를 positive control로 확인한 뒤, 시계나 기한을 바꾸지 않고 기존 연결의 기한 이후 읽기/추가 거부를 시험한다. INSERT는 항상 rollback한다. 비밀번호 만료만으로 기존 연결이 닫힌다고 가정하지 않는다.

관측의 저장과 모든 변경의 수집 보증은 별개다. 이 도구들은 환경 watermark나 실제 이전 PASS를 발행하지 않는다. [실제 권한·저장·실패 기록](../../docs/development/personal-change-feed-integration-2026-11-20.md).
