// Test-only, pinned driver provided explicitly. Credentials arrive on stdin.
import { pathToFileURL } from "node:url";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
let input = "";
for await (const part of process.stdin) input += part;
const config = JSON.parse(input);
input = "";
const metadata = JSON.parse(await readFile(join(config.tools, "node_modules/playwright-core/package.json")));
if (metadata.version !== "1.56.1") throw new Error("Pinned browser driver required");
const { chromium } = await import(pathToFileURL(join(config.tools, "node_modules/playwright-core/index.mjs")));
const browser = await chromium.launch({ executablePath: config.executable, headless: true, args: ["--disable-background-networking"] });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: "ko-KR" });
  await context.route("**/*", route => new URL(route.request().url()).origin === config.origin ? route.continue() : route.abort());
  await context.addCookies([{ url: config.origin, name: "sova_security", value: config.cookie, httpOnly: true, sameSite: "Strict" }]);
  await context.addInitScript(({ proof, expires_at }) => localStorage.setItem("sova_operator_proof", JSON.stringify({ token: proof, expires_at })), config);
  const page = await context.newPage();
  let page_errors = 0, mutation_requests = 0, native_gets = 0;
  page.on("pageerror", () => { page_errors++; });
  page.on("request", request => {
    if (request.method() !== "GET") mutation_requests++;
    if (request.url() === config.origin + "/api/native/status") native_gets++;
  });
  await page.goto(config.origin + "/console/");
  await page.getByRole("button", { name: "계약 계획", exact: true }).click();
  await page.waitForFunction(({ state, required, reason_text }) => {
    const text = document.querySelector("main")?.innerText || "";
    return text.includes(state) && text.includes(required ? (reason_text || "효과 결과 미확인") : "현재 관측에서 복구 보류 없음");
  }, config.expected);
  if (config.expected.hold_count !== undefined) {
    await page.getByRole("heading", { name: `거버넌스 원장의 활성 보안 보류 ${config.expected.hold_count}개`, exact: true }).waitFor();
  }
  const nativeControls = await page.locator(".native-plan button").allTextContents();
  if (nativeControls.length !== 1 || !nativeControls[0].includes("원장 새로고침")) throw new Error("Unexpected mutation control");
  await page.screenshot({ path: join(config.directory, "desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  if (overflow) throw new Error("Mobile viewport overflow");
  const rowHeights = await page.locator(".native-table tbody tr").evaluateAll(rows => rows.map(row => row.getBoundingClientRect().height));
  if (rowHeights.some(height => height > 120)) throw new Error("Effect row wraps excessively on mobile");
  await page.screenshot({ path: join(config.directory, "mobile.png"), fullPage: true });
  if (page_errors || mutation_requests || !native_gets) throw new Error("Browser boundary failed");
  console.log(JSON.stringify({ executed: true, driver: metadata.version, browser: browser.version(), page_errors, mutation_requests, native_gets, viewport_overflow: overflow, mobile_effect_row_heights: rowHeights, observed_state: config.expected.state, observed_hold: config.expected.required, synthetic_operator_identity: true }));
} finally {
  await browser.close();
}
