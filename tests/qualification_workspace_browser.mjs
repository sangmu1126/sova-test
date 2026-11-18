// Real Authorization Code + PKCE login and native UI mutations. No token injection.
import { pathToFileURL } from "node:url";
import { readFile, writeFile, access } from "node:fs/promises";
import { join } from "node:path";
let input = "";
for await (const part of process.stdin) input += part;
const c = JSON.parse(input);
input = "";
const metadata = JSON.parse(await readFile(join(c.tools, "node_modules/playwright-core/package.json")));
if (metadata.version !== "1.56.1") throw new Error("Pinned browser driver required");
const { chromium } = await import(pathToFileURL(join(c.tools, "node_modules/playwright-core/index.mjs")));
const browser = await chromium.launch({ executablePath: c.executable, headless: true,
  args: ["--disable-background-networking"] });
const requests = [], errors = [], contexts = [];
let pkce = 0, tokenExchanges = 0, operatorAuthorization = "", selfApprovalStatus = null;
async function phase(name) {
  await writeFile(join(c.directory, "phase.json"), JSON.stringify({ phase: name, at: new Date().toISOString() }), { mode: 0o600 });
}
async function context() {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: "ko-KR" });
  contexts.push(context);
  await context.route("**/*", route => {
    const origin = new URL(route.request().url()).origin;
    return [c.origin, c.idpOrigin].includes(origin) ? route.continue() : route.abort();
  });
  context.on("page", page => {
    page.on("pageerror", error => errors.push(error.name));
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.origin === c.origin && url.pathname.startsWith("/v1/") &&
          request.headers().authorization && contexts[0] === context)
        operatorAuthorization = request.headers().authorization;
      if (url.origin === c.idpOrigin && url.pathname.endsWith("/auth")) {
        if (url.searchParams.get("code_challenge_method") !== "S256" || !url.searchParams.get("code_challenge"))
          errors.push("MissingPKCE");
        else pkce++;
      }
      if (url.origin === c.idpOrigin && url.pathname.endsWith("/token")) tokenExchanges++;
    });
    page.on("response", response => {
      const url = new URL(response.url());
      if (url.origin === c.origin && url.pathname.startsWith("/v1/"))
        requests.push({ method: response.request().method(), path: url.pathname, status: response.status() });
    });
  });
  return context;
}
async function login(context, user) {
  const page = await context.newPage();
  await page.goto(c.origin + "/workspace/");
  await page.getByRole("button", { name: "로그인", exact: true }).click();
  await page.locator("#username").fill(user.username);
  await page.locator("#password").fill(user.password);
  await page.locator("#kc-login").click();
  await page.getByLabel("앱", { exact: true }).waitFor();
  if (new URL(page.url()).search || new URL(page.url()).pathname !== "/workspace/")
    throw new Error("Login callback parameters were retained");
  const storage = await page.evaluate(() => ({ local: Object.entries(localStorage), session: Object.entries(sessionStorage) }));
  if ([...storage.local, ...storage.session].some(([k, v]) => /access_token|refresh_token|id_token/.test(v) || k.startsWith("oidc.user:")))
    throw new Error("Persistent token storage detected");
  return page;
}
async function post(page, suffix, trigger) {
  const waiting = page.waitForResponse(r => new URL(r.url()).origin === c.origin &&
    new URL(r.url()).pathname.endsWith(suffix) && ["POST", "PUT"].includes(r.request().method()));
  await trigger();
  const response = await waiting;
  if (![200, 201].includes(response.status())) throw new Error(`Native mutation rejected: ${response.status()} ${suffix}`);
  return response.json();
}
async function gate(name) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    try { await access(join(c.directory, name)); return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("Owned parent checkpoint did not finish");
}
async function registerInputs(page, bundle) {
  await page.getByRole("button", { name: "환경·앱 등록", exact: true }).click();
  for (const registration of bundle.registrations) {
    await page.getByLabel("등록 종류", { exact: true }).selectOption(registration.kind);
    await page.getByLabel("등록 ID", { exact: true }).fill(registration.subject);
    await page.getByLabel("등록 JSON", { exact: true }).fill(JSON.stringify(registration.document));
    await post(page, `/${registration.kind}/${registration.subject}`, () => page.getByRole("button", { name: "등록 저장", exact: true }).click());
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent.trim() === "등록 저장")?.disabled);
  }
  for (const envelope of bundle.attestations) {
    await page.getByLabel("서명 JSON", { exact: true }).fill(JSON.stringify(envelope));
    await post(page, "/attestations", () => page.getByRole("button", { name: "서명 제출", exact: true }).click());
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent.trim() === "서명 제출")?.disabled);
  }
}
async function fillPlan(page, plan) {
  for (const [label, value] of [["의존성 조사", plan.snapshot_id], ["출발 환경", plan.source_profile],
      ["목적지 환경", plan.target_profile], ["이전 어댑터", plan.manifest], ["방향별 자격", plan.qualification]])
    await page.getByLabel(label, { exact: true }).selectOption(value);
  await page.getByLabel("이전 구성 요소", { exact: true }).fill(plan.nodes.join(","));
  await page.getByLabel("행 수 상한", { exact: true }).fill(String(plan.rows));
}
try {
  await phase("planner-login");
  const a = await login(await context(), c.users.planner);
  await phase("registrations");
  await registerInputs(a, c);
  await phase("compile");
  await a.getByLabel("앱", { exact: true }).selectOption(c.application);
  await a.getByRole("button", { name: "이전 계획", exact: true }).click();
  await fillPlan(a, c.plan);
  const compiled = await post(a, "/plans", () => a.getByRole("button", { name: "계획 만들기", exact: true }).click());
  await a.getByLabel("계획", { exact: true }).selectOption(compiled.plan_id);
  await a.getByRole("button", { name: "승인·실행", exact: true }).click();
  await a.getByText("계획 작성자는 이 계획을 승인할 수 없습니다.", { exact: false }).waitFor();
  if (!await a.getByRole("button", { name: "계획 승인", exact: true }).isDisabled()) throw new Error("Self approval enabled");
  // A hostile client can ignore a disabled button. Reuse this actual user's
  // issued credential only in memory to check the server boundary as well.
  if (!operatorAuthorization) throw new Error("No real user authorization observed");
  const rejected = await a.request.post(`${c.origin}/v1/tenants/${c.tenant}/applications/${c.application}/plans/${compiled.plan_id}/approvals`, {
    headers: { Authorization: operatorAuthorization }, data: { approval_id: "self-approval-must-fail" },
  });
  selfApprovalStatus = rejected.status();
  operatorAuthorization = "";
  if (selfApprovalStatus !== 403) throw new Error("Server permitted self approval");
  await writeFile(join(c.directory, "compiled.json"), JSON.stringify(compiled), { mode: 0o600 });
  await gate("bootstrap.ready");
  await phase("approver-login");
  const b = await login(await context(), c.users.approver);
  await b.getByLabel("앱", { exact: true }).selectOption(c.application);
  await b.getByLabel("계획", { exact: true }).selectOption(compiled.plan_id);
  await b.getByRole("button", { name: "승인·실행", exact: true }).click();
  await b.getByRole("button", { name: "계획 승인", exact: true }).click();
  const approval = await post(b, "/approvals", () => b.getByRole("dialog").getByRole("button", { name: "확인하고 요청", exact: true }).click());
  await phase("approved");
  await b.getByRole("dialog").waitFor({ state: "hidden" });
  await b.screenshot({ path: join(c.directory, "independent-approval.png"), fullPage: true });
  await a.getByRole("button", { name: "새로고침", exact: true }).click();
  await a.getByLabel("저장된 승인", { exact: true }).selectOption(approval.approval_id);
  await a.getByRole("button", { name: "이전 실행 접수", exact: true }).click();
  const admitted = await post(a, "/admissions", () => a.getByRole("dialog").getByRole("button", { name: "확인하고 요청", exact: true }).click());
  await phase("admitted");
  await a.getByRole("dialog").waitFor({ state: "hidden" });
  await a.getByRole("button", { name: "워크로드·보안", exact: true }).click();
  await writeFile(join(c.directory, "admitted.json"), JSON.stringify(admitted), { mode: 0o600 });
  await gate("execution.ready");
  let finalPlan = compiled, finalAdmission = admitted;
  if (c.outcome === "failback") {
    await a.getByText("ROUTE_CONFIRMED", { exact: false }).first().waitFor({ timeout: 15000 });
    await a.getByRole("button", { name: "복귀 검토 요청", exact: true }).click();
    await post(a, "/failback-requests", () => a.getByRole("dialog").getByRole("button", { name: "확인하고 요청", exact: true }).click());
    await a.getByRole("dialog").waitFor({ state: "hidden" });
    await registerInputs(a, c.reverse);
    await a.getByRole("button", { name: "워크로드·보안", exact: true }).click();
    await a.getByRole("button", { name: "별도 복귀 계획 작성", exact: true }).click();
    if (await a.getByLabel("복귀 기준 계획 ID", { exact: true }).inputValue() !== compiled.plan_id)
      throw new Error("Failback parent plan was not bound");
    await fillPlan(a, c.reverse.plan);
    finalPlan = await post(a, "/failback-plans", () => a.getByRole("button", { name: "계획 만들기", exact: true }).click());
    await a.getByLabel("계획", { exact: true }).selectOption(finalPlan.plan_id);
    await writeFile(join(c.directory, "reverse-compiled.json"), JSON.stringify(finalPlan), { mode: 0o600 });
    await gate("reverse-bootstrap.ready");
    await b.getByRole("button", { name: "새로고침", exact: true }).click();
    await b.getByLabel("계획", { exact: true }).selectOption(finalPlan.plan_id);
    await b.getByRole("button", { name: "승인·실행", exact: true }).click();
    await b.getByRole("button", { name: "계획 승인", exact: true }).click();
    const returnApproval = await post(b, "/approvals", () => b.getByRole("dialog").getByRole("button", { name: "확인하고 요청", exact: true }).click());
    await b.getByRole("dialog").waitFor({ state: "hidden" });
    if (returnApproval.approval_id === approval.approval_id) throw new Error("Forward approval reused for failback");
    await a.getByRole("button", { name: "승인·실행", exact: true }).click();
    await a.getByRole("button", { name: "새로고침", exact: true }).click();
    await a.getByLabel("저장된 승인", { exact: true }).selectOption(returnApproval.approval_id);
    await a.getByRole("button", { name: "이전 실행 접수", exact: true }).click();
    finalAdmission = await post(a, "/admissions", () => a.getByRole("dialog").getByRole("button", { name: "확인하고 요청", exact: true }).click());
    await a.getByRole("dialog").waitFor({ state: "hidden" });
    await a.getByRole("button", { name: "워크로드·보안", exact: true }).click();
    await writeFile(join(c.directory, "reverse-admitted.json"), JSON.stringify(finalAdmission), { mode: 0o600 });
    await gate("execution-final.ready");
  }
  const expectedState = c.outcome === "unknown" ? "SOURCE_FENCED" : "HANDOFF_COMPLETE";
  await a.getByText(expectedState, { exact: false }).first().waitFor({ timeout: 15000 });
  if (c.outcome === "unknown") {
    await a.getByRole("heading", { name: "거버넌스 원장의 활성 보안 보류 1개", exact: true }).waitFor();
    await a.getByText("실행권 만료 · 미확정 효과 남음", { exact: false }).first().waitFor();
  }
  await a.screenshot({ path: join(c.directory, "execution-status.png"), fullPage: true });
  await a.setViewportSize({ width: 390, height: 844 });
  const overflow = await a.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  await a.screenshot({ path: join(c.directory, "mobile.png"), fullPage: true });
  if (overflow || errors.length || pkce !== 2 || tokenExchanges !== 2) throw new Error("Browser identity or display boundary failed: " + JSON.stringify({ overflow, errors, pkce, tokenExchanges }));
  console.log(JSON.stringify({ executed: true, driver: metadata.version, browser: browser.version(), pkce_logins: pkce,
    token_exchanges: tokenExchanges, page_errors: errors.length, mobile_overflow: overflow,
    self_approval_status: selfApprovalStatus,
    token_injection: false, persistent_tokens: false, forward_plan_id: compiled.plan_id,
    plan_id: finalPlan.plan_id, unit_digest: finalAdmission.unit_digest,
    independent_subjects: c.users.planner.subject !== c.users.approver.subject, requests }));
} catch (error) {
  for (let i = 0; i < contexts.length; i++) {
    const pages = contexts[i].pages();
    for (let j = 0; j < pages.length; j++) {
      await pages[j].screenshot({ path: join(c.directory, `failure-${i}-${j}.png`), fullPage: true }).catch(() => {});
    }
  }
  throw error;
} finally {
  for (const context of contexts) await context.close();
  await browser.close();
}
