# 실제 워커 Pod 복구 인수 개발 기록

2026-11-18 · 개인 브랜치 `feature/migration-request-qualification`.

## 기준과 작업 경계

시작 SHA는 `928c992c02c04d0530c1bef0db04bf3534f887fc`다. 다른 개인 작업 트리는 `d664566e2bc214890aebf3a5c6f0462c7b916435`에서 깨끗한 상태였고, 세션·볼륨 어댑터 경로와 겹치지 않았다. 로컬 `SESSION-SCOPES.json`에 새로운 qualification fixture·시험·문서와 기존 PostgreSQL 실행기 확장을 게시했다. 상대 세션의 전체 작업이나 동의를 확인한 것으로 표현하지 않는다.

공유 `sova-test-env`·`sova-test-env-b`는 그대로 두고 UUID 클러스터를 만들었다. 사용자 kubeconfig·팀 저장소·클라우드·제품 어댑터·worker·quiescence·SQL 스키마는 수정하지 않았다. 서비스 이미지도 앞서 대조한 빌드를 그대로 재사용했다.

## 구현

- `1b4ace57c1e54b6b011afe0826626d536aaf6956`: 별도 kind·TLS DB·restricted Pod·실제 catalog·SA·Service·메타데이터 감사 로그를 이용하는 복구 시험.
- `237eee26cc432b24c53e1acf20990994fe36b642`: 실제 SA UID·Pod UID의 감사 근거 결속, Secret GET 403, 권한 없는 Pod 관측 403과 unknown 처리까지 확인.

runtime `ctr tasks pause`로 실제 소유자와 lease keeper를 정지시켰다. DB 시각을 고치거나 강제로 lease를 만료시키지 않았다. successor는 실제 Kubernetes API를 SA 토큰·CA로 조회했다. 살아 있는 소유자 또는 권한 없는 관측에서 정지 증명을 발급하도록 대체하지 않았다.

원래 소유자를 멈춘 상태에서 target endpoint를 준비한 뒤 일반 Pod 삭제를 요청했다. kubelet의 종료와 CRI의 종료 상태를 확인했다. successor는 기존 효과를 readback으로 정리했고, API 서버 감사 로그에서 성공한 route Service PATCH가 한 번임을 확인했다. 단일 effect row만으로 재실행이 없었다고 추정하지 않았다.

## 실행 기록

| 실행 | 결과 | 의미 |
|---|---|---|
| run-01 | 1 실패 | 전용 node의 Docker memory 제한만 변경하면 기존 swap 제한과 충돌. worker 실행 전 설정 오류 |
| run-02 | 1 PASS | live Pod·실제 만료·정지·successor 복구·route PATCH 1회 확인 |
| run-03 | **1 PASS, 오류·실패·건너뜀 0** | 최종 코드. 추가한 SA/Pod UID 결속·실제 Secret/Pod 조회 403도 확인 |

run-01의 자원은 정리했고 원문을 보존했다. 전용 노드의 memory와 memory-swap을 함께 2GiB로 지정해 swap을 늘리지 않고 해결했다. 제품 판정·권한·필수 검사를 완화하지 않았다. 진단 수집이 실패해도 소유 클러스터 정리를 시도하도록 종료 처리를 분리했다.

run-03의 측정 파일 **38개**는 `237eee26…`와 일치한다. DB 시각 감시 **184개 샘플**을 통과했다. 호스트 test module import의 Starlette 사용 중단 예고 경고 1건은 남아 있다. 세 실행 모두 새로운 자원을 사용했고 종료 후 클러스터·DB·네트워크가 남지 않았음을 확인했다. 임시 이미지 태그만 제거했으며 고정 서비스 이미지와 기존 두 kind 클러스터는 유지했다.

## 최종 관측

원래 소유자는 `worker-b.09ee6364-a56e-4890-bc7c-85ca63f82673.eb13c1436537`, 복구 소유자는 `worker-a.045d5de1-24f0-4c94-b1ed-82ff133193bc.2119a2a24a99`였다. Pod 이름을 만든 것만으로 신원을 인정하지 않고, 실제 Pod UID·SA UID·API 감사 토큰의 Pod 결속을 대조했다.

- DB lease 만료: `2026-11-18T14:54:40.853713Z`.
- `14:54:43.121581Z` DB 관측에서 기존 소유자 유지, recovery 0건, effect `in_flight`.
- 실제 runtime 종료 관측: `14:54:46.182652Z`.
- successor의 recovery 기록: `14:54:46.448828Z`.
- 최종 상태: `HANDOFF_COMPLETE`, route effect `confirmed_applied`, 성공한 Service PATCH **1회**.
- 과거 주문 50건 확인 후 목적지에서 신규 SQL 쓰기 성공.

이 시각은 한 실행의 관측 기록이다. 통계적 복구 시간이나 가용성 SLO로 사용하지 않는다.

## 증거와 남은 일

원문 위치는 `local-dev-records/worker-pod-qualification-20261118/`이며 [공개 요약](../evidence/worker-pod-qualification-20261118.json)에 소스와 허용한 원문 파일의 해시를 연결했다. kubeconfig·Secret·인증서 개인 키·토큰은 공개하지 않는다. 노드 감사 정책은 요청 본문·응답 본문을 남기지 않는 Metadata 수준이다.

다음 단계는 실제 HTTP 업무 앱·고정 진입점·지속 요청과의 연결이다. 이번 EndpointSlice fixture는 업무 응답을 하지 않는다. 합성 inventory와 IdP, 공유 fixture 인증서, 파일 witness, 단일 서버·노드, 운영 NetworkPolicy 미검증은 그대로 남는다. 고객 운영·AWS→VM·무중단·금융 보안 자격으로 확대하지 않는다.
