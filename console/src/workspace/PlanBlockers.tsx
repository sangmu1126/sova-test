export type PlanBlocker = { stage: string; code: string; details: string };

export function checkedBlockers(value: unknown): PlanBlocker[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 100)
    throw new Error("서버의 보류 사유를 확인하지 못했습니다.");
  return value.map((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.stage !== "string" ||
      !/^C\d{1,2}$/.test(item.stage) ||
      typeof item.code !== "string" ||
      !/^[A-Z][A-Z0-9_]{0,99}$/.test(item.code)
    )
      throw new Error("서버의 보류 사유를 확인하지 못했습니다.");
    const details = JSON.stringify(item);
    if (details.length > 16_384)
      throw new Error("서버의 보류 사유 크기를 확인하지 못했습니다.");
    return { stage: item.stage, code: item.code, details };
  });
}

const descriptions: Record<string, { title: string; action: string }> = {
  WRITER_INVENTORY_INCOMPLETE: {
    title: "데이터를 쓸 수 있는 경로의 조사가 끝나지 않았습니다.",
    action:
      "앱 계정과 배치·수동 작업을 포함해 쓰기 권한과 접근 경로를 확인해야 합니다.",
  },
  CONTROL_INVENTORY_INCOMPLETE: {
    title: "서비스 설정을 바꿀 수 있는 경로의 조사가 끝나지 않았습니다.",
    action: "배포·스케줄·권한 변경을 수행하는 계정과 도구를 확인해야 합니다.",
  },
  DISCOVERY_COVERAGE_INCOMPLETE: {
    title: "확인하지 못한 의존성이 남아 있습니다.",
    action:
      "누락된 의존성과 조사 범위를 확인하고 새 조사 결과를 등록해야 합니다.",
  },
  SNAPSHOT_AUTHENTICATION_REQUIRED: {
    title: "조사 결과의 서명 확인이 완료되지 않았습니다.",
    action:
      "조사 범위의 보류 항목을 해결한 뒤, 등록된 검증자·서명·유효기한을 다시 확인해야 합니다.",
  },
  SNAPSHOT_NOT_CURRENT: {
    title: "현재 시점에 유효한 조사 결과가 필요합니다.",
    action: "실제 환경을 다시 조사하고 유효기한이 있는 결과를 등록해야 합니다.",
  },
};

export function blockerDescription(code: string) {
  return (
    descriptions[code] ?? {
      title: "서버가 추가 근거를 요구하고 있습니다.",
      action:
        "아래 판정 코드와 검증 기록을 확인해야 합니다. 화면에서 통과로 바꾸지 않습니다.",
    }
  );
}

export function PlanBlockers({ items }: { items: PlanBlocker[] }) {
  return (
    <section
      className="native-card plan-blockers"
      aria-label="계획 생성 보류 사유"
    >
      <h2>계획 생성에 필요한 확인</h2>
      <p>
        최근 계획 생성 요청에 대한 서버 판정입니다. 근거를 보완한 뒤 다시
        요청해야 하며, 이 결과는 실행을 허용하지 않습니다.
      </p>
      <ul>
        {items.map((item, index) => {
          const description = blockerDescription(item.code);
          return (
            <li key={`${item.stage}-${item.code}-${index}`}>
              <strong>{description.title}</strong>
              <p>{description.action}</p>
              <details>
                <summary>판정 코드 확인</summary>
                <code>
                  {item.stage} · {item.code}
                </code>
                <pre>{item.details}</pre>
              </details>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
