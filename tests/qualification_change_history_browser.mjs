// Actual PKCE browser -> native API -> isolated PostgreSQL; fixture event inputs.
import { pathToFileURL } from "node:url";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
let input = "";
for await (const part of process.stdin) input += part;
const c = JSON.parse(input); input = "";
const pkg = JSON.parse(await readFile(join(c.tools, "node_modules/playwright-core/package.json")));
if (pkg.version !== "1.56.1") throw new Error("Pinned browser driver required");
const { chromium } = await import(pathToFileURL(join(c.tools, "node_modules/playwright-core/index.mjs")));
const browser = await chromium.launch({ executablePath: c.executable, headless: true,
  args: ["--disable-background-networking"] });
let pkce = 0, tokenExchanges = 0, phase = "start", errors = 0;
const mark = async (name) => { phase = name; await writeFile(join(c.directory, "phase.json"), JSON.stringify({ phase }), { mode: 0o600 }); };
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: "ko-KR" });
  await context.route("**/*", route => [c.origin, c.idpOrigin].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  page.on("pageerror", () => errors++);
  page.on("request", r => {
    const u = new URL(r.url());
    if (u.origin === c.idpOrigin && u.pathname.endsWith("/auth")) {
      if (u.searchParams.get("code_challenge_method") !== "S256" || !u.searchParams.get("code_challenge")) errors++;
      else pkce++;
    }
    if (u.origin === c.idpOrigin && u.pathname.endsWith("/token")) tokenExchanges++;
  });
  await mark("login");
  await page.goto(c.origin + "/workspace/");
  await page.getByRole("button", { name: "로그인", exact: true }).click();
  await page.locator("#username").fill(c.user.username);
  await page.locator("#password").fill(c.user.password);
  await page.locator("#kc-login").click();
  await page.getByLabel("앱", { exact: true }).waitFor();
  await page.getByLabel("앱", { exact: true }).selectOption(c.application);
  await mark("history");
  const waiting = page.waitForResponse(r => new URL(r.url()).pathname.endsWith("/change-history"));
  await page.getByRole("button", { name: "실측·비용", exact: true }).click();
  const response = await waiting;
  if (response.status() !== 200) throw new Error("History response failed");
  await page.getByText(c.event, { exact: true }).waitFor();
  await page.getByText("수집 범위 완전성: 미확인 (UNKNOWN)", { exact: true }).waitFor();
  if (await page.getByText("private-resource", { exact: true }).count()) throw new Error("Other app leaked");
  const storage = await page.evaluate(() => [...Object.entries(localStorage), ...Object.entries(sessionStorage)]);
  if (storage.some(([k, v]) => /access_token|refresh_token|id_token/.test(v) || k.startsWith("oidc.user:"))) throw new Error("Persistent token");
  if (errors) throw new Error("Browser error");
  await page.screenshot({ path: join(c.directory, "history.png"), fullPage: true });
  await mark("logout");
  await page.getByRole("button", { name: "로그아웃", exact: true }).click();
  await page.getByRole("button", { name: "로그인", exact: true }).waitFor();
  if (await page.getByText(c.event, { exact: true }).count()) throw new Error("History remained after logout");
  console.log(JSON.stringify({ pkce, token_exchanges: tokenExchanges, history_http_status: response.status(),
    private_app_absent: true, persistent_tokens_absent: true, logout_cleared: true, page_errors: errors }));
} catch {
  await writeFile(join(c.directory, "failure.json"), JSON.stringify({ phase, errors }), { mode: 0o600 });
  process.exitCode = 1;
} finally { await browser.close(); }
