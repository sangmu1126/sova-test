# 전환 단계 지연의 읽기 전용 실측 기록

2026-11-19 · 시작 `068860d6f96678046dffa3675a1a8529dbdca85f`.

## 목적과 작업 경계

직전 backpressure 비교에서는 두 모드 모두 100건 중 1건이 호출 한도를 소진했다. 다음 성능 변경을 선택하기 위해 실제 원장 시각과 클라이언트 결과를 연결했다. 이번에는 제품 코드·이미지·승인·fencing·필수 검증·시간 및 재시도 한도를 바꾸지 않았다.

별도 개인 worktree의 `feature/migration-request-qualification`에서 진행했다. 다른 세션의 `feature/contract-driven-foundation`은 `c344934717de6ce52279ad06c28c796f8e85b729`로 깨끗했고, 해당 세션의 최근 두 문서 변경과 이번 소유 경로는 겹치지 않았다. 로컬 범위 공지는 상호 승인이나 잠금을 대신하지 않는다. 다른 worktree·팀 저장소·AWS·공유 클러스터는 변경하지 않았다.

## 구현과 수집기 수정

| 커밋 | 내용 |
|---|---|
| `be9e383` | 실제 admission에 묶인 원장·authority 이력과 호출 결과의 읽기 전용 timing export, 정상 Pod 전환의 종료 후 수집, 합성 14건·실제 TLS DB/API 4건 |
| `ad7a168` | 동일 관측 본문 digest를 여러 obligation이 참조할 때 각 서명 envelope의 obligation을 유지 |
| `1ac775992c0405922ab62e321ad72c9ad1d5c520` | 현재 계획의 정확한 obligation scope 및 authority 시작 이후 저장 시각으로 metadata 범위를 제한 |

DB는 tenant RLS를 유지한 `REPEATABLE READ / READ ONLY` snapshot을 사용한다. tenant·app·unit·plan·operation·canonical plan digest를 대조한다. 효과 7개의 준비·dispatch·확인 전이 21건과 authority 이력 10건이 정확해야 한다. SQLite 원장은 읽기 전용으로 열어 run·tenant·profile과 최종 reconciliation 범위를 확인한다. 불완전·모호한 기록은 측정 완료로 처리하지 않는다.

첫 실행에는 단계 시각이 모두 있었지만 검증 참조가 13건만 나왔다. content-addressed 본문 행의 최초 obligation을 기준으로 연결해서 동일 본문을 공유한 residency 참조 3건이 누락된 것이다. 서명 envelope의 obligation으로 연결하도록 수정했고 이후 16건을 유지했다. 최초 13건 출력과 단계 실측은 지우지 않았다.

같은 revisions만으로 다른 unit의 metadata가 섞이지 않도록 정확한 scope 및 저장 하한 시각을 추가했다. 실제 DB 시험에 같은 revisions지만 다른 unit scope의 서명 metadata를 직접 넣고 출력에서 제외되는지 확인했다. 이는 부정 fixture 주입 시험이며, 실제 생산자/API의 서명 인증을 통과했거나 제품 게이트 우회가 입증됐다는 뜻은 아니다.

Exporter는 서명 본문·token·DSN·명령 인자·승인 문서를 출력하지 않는다. 서명 metadata 조회는 현재 issuer 및 서명 인증이나 실행 허가가 아니다. 테스트 하네스의 내부 진단이며 운영자용 API로 공개하지 않았다. 실행 종료 후 수집하므로 측정 대상 전환 경로에 새 조회를 추가하지 않는다.

## 고정 실행 기준

| 항목 | 값 |
|---|---|
| 최종 하네스 | `1ac775992c0405922ab62e321ad72c9ad1d5c520` |
| 제품 소스 | `faa8897837c5fd2e909e908a7316202ddba66922` |
| 로컬 제품 이미지 | `sha256:0999b268f7246efe4f2b0e7f5ae65fcd8ee88e7328d9eaaff8684986388a559c` |
| security 소스 | `27e6e85514a7cbb3e43d216464b72d9bd78f3fcf` |
| PostgreSQL 17.12 arm64 | `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac` |
| 실행 모드 | `pod-http / ready_backpressure`, 기존 v4 |
| 클라이언트 한도 | 논리 주문 100건, 5초·조회 포함 8회, 네트워크 1초, 기존 jitter·seed 유지 |
| 전용 DB 자원 | tmpfs 384MiB·메모리 512MiB, 실행마다 UUID container/network |

제품 이미지를 새로 빌드하지 않았다. 실제 설치 소스 440개와 고정 소스의 일치는 각 실행에서 확인했다. 동일 로컬 arm64 kind 노드의 두 실제 CLI worker Pod, 전용 TLS PostgreSQL의 별도 DB, 실제 Kubernetes Service PATCH를 사용했다. 목표 앱은 사전 Ready였지만 쓰기는 SQLSTATE 42501로 거부됐으며, 출발지 정상 쓰기를 대조군으로 확인했다. owner 장애는 주입하지 않았다.

기존 이미지 감사는 [직전 기록](lookup-backpressure-2026-11-19.md)에 있다. 설치 바이트 일치는 공급망 서명·새 취약점 검사·전이 의존성 lock·레지스트리 게시·운영 릴리스 자격을 의미하지 않는다.

## 검증 결과

구성 요소 묶음은 각 수집기 버전에서 18건 모두 통과했다. 최종 18건은 합성 단위 14건과 실제 TLS DB/API 4건이다. 반복된 세 묶음은 같은 시험이므로 54개의 독립 사례로 합산하지 않는다. 다른 tenant·app·operation, 누락/중복 효과·시각 역전·시간대 누락·다른 원장 범위·불완전 부하를 거부하고, 동일 digest의 서로 다른 세 참조와 다른 unit scope의 배제를 확인했다.

| 실행 | 수집기 | pause 준비→route 확인 | 준비→dispatch 합 | dispatch→확인 합 | 효과 사이 합 | 호출 내 결과 / 최초 실패 | 최대 호출 시간 |
|---|---|---:|---:|---:|---:|---|---:|
| hinted | `be9e383` | 3,164.716ms | 100.809ms | 936.168ms | 2,127.739ms | CONFIRMED 100 / 503 1 | 3,343.360ms |
| measured | `ad7a168` | 2,063.804ms | 88.132ms | 404.588ms | 1,571.084ms | CONFIRMED 100 / 503 1 | 2,162.982ms |
| final | `1ac7759` | 2,486.157ms | 110.527ms | 585.362ms | 1,790.268ms | CONFIRMED 100 / 503 1 | 3,203.083ms |

최종 실행의 최대 부하 완료 시간은 별도 reconciliation 기준 3,200.541377ms다. 호출 원장의 최대 elapsed와 기록 경계가 같다고 가정하지 않는다. DB UTC와 클라이언트 monotonic 시각을 서로 빼서 지연을 만들지 않았다.

각 실행은 HANDOFF_COMPLETE, 실제 경로 PATCH 1회, recovery 0회, SQL 주문 103/103 및 기존 주문 50건 보존을 확인했다. 103에는 별도 사후 probe 3건이 포함되며 최초 부하 성공 100건과 섞지 않는다. 최종 실행에서는 부하 중 target 성공이 87건이었고 늦은 target 활성화는 0회였다. Secret GET 403, 권한 없는 Pod 관측 GET 403, 잘못된 CA·호스트명 거부도 유지했다. ClockWatch는 실행별 188·185·201개 표본이 모두 통과했다.

## 가장 긴 구간의 실제 문맥

최종 실행에서 효과 사이 합은 전환 기록 구간의 약 72%였다. 가장 긴 구간은 `verify_final_state` 확인→`activate_target` 준비의 **945.899ms**였다. 같은 DB 시간 구간에 다음 관측 저장과 authority 갱신이 있었다.

| 관측 또는 전이 | 확인한 내용 |
|---|---|
| `business.ORDER-01` | 업무 효과 1회 확인 관측 저장 |
| `data.idempotency_results`·`data.orders`·`data.work_items` | 세 데이터 집합 비교 관측 저장 |
| `security.tested_restore` | 활성화 의도 기록 전 복원 시험 관측 저장 |
| `FINAL_STATE_VERIFIED`→`TARGET_ACTIVATION_INTENT` | 다음 효과로 진행하기 위한 실제 authority 이력 |

그 밖의 효과 사이 구간은 pause→fence 167.451ms, fence→copy 294.041ms, copy→verify 140.260ms, activate→route 242.617ms였다. 실제 copy dispatch→확인 envelope는 57.142ms, route dispatch→확인은 369.351ms였다. stage_target 확인→pause 준비 928.834ms는 사전 준비 구간이므로 위 전환 합계에 넣지 않았다.

이 값은 **DB 행 기록 시각**의 구간이다. commit 시각·정확한 봉쇄 효력 시각·순수 SQL/API 시간·개별 probe 시간은 아니다. 효과 사이에는 검증·서명·저장·authority·lease·다음 준비가 함께 포함된다. 복원 시험 하나나 TLS 연결이 945.899ms 전체의 원인이라고 단정하지 않는다.

## 결론과 다음 개발 경계

이번 세 실행은 100건 모두 호출 내 완료했지만 제품·이미지·예산을 변경하지 않았다. 실행별 도착 시각과 호스트 부하가 다른 단일 closed-loop 표본이므로 성능 최적화나 SLO 개선을 입증하지 않는다. [직전 두 실행](../evidence/lookup-backpressure-20261119.json)의 99 CONFIRMED·1 EXHAUSTED와 다른 실패를 그대로 보존한다. 최초 503도 없애지 않았다. `complete=true`는 관측 완전성이지 무중단 자격이 아니다.

다음 후보는 이 효과 사이 구간의 연결·트랜잭션·검증별 소요를 추가 계측한 뒤, 제한된 연결 재사용 실험을 따로 수행하는 것이다. 풀을 도입한다면 tenant RLS 및 트랜잭션 초기화·반납, 권한 회수, 현재 승인·issuer 검증, lease와 epoch, 읽기 전용 경계, 취소·실패 후 연결 오염 방지를 먼저 회귀 시험으로 고정해야 한다. 이번에는 풀·증거 캐시·검사 생략·예산 연장을 넣지 않았다.

서버 측 내구성 접수·장기 연결·CDC·독립 사이트 장애·운영 인증·WORM·실제 AWS→온프렘 SLO·금융 보안 인증은 여전히 미검증이다. 합성 계획·IdP·일부 신뢰 입력·로컬 witness·fixture 인증서 공유라는 한계도 남는다.

## 증거와 정리

원문은 `local-dev-records/cutover-critical-path-20261119`에 보존했다. 공개 증거에는 허용한 파일 61개의 SHA-256·크기·상대 경로와 소스 해시, 실제 단계 시각, 범위 식별자 및 결과를 연결했다. 개인 키·kubeconfig·token 설정 파일은 포함하지 않았다. 원문 해시는 서명된 운영 증거나 외부 보관소를 대신하지 않는다.

실행 후 정확한 시험 소유 컨테이너·네트워크 이름 18개가 모두 없음을 읽기 전용으로 확인했다. 캐시 이미지는 보존했다. 시험 프로세스에는 `caffeinate -di`를 사용했고 시스템 잠자기 설정은 바꾸지 않았다.

[측정 계약](../cutover-critical-path.md) · [실측 증거 JSON](../evidence/cutover-critical-path-20261119.json).
