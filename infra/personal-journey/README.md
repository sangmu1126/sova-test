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
