import {
  InMemoryWebStorage,
  UserManager,
  WebStorageStateStore,
} from "oidc-client-ts";
import type { User } from "oidc-client-ts";

export type Config = {
  version: number;
  issuer: string;
  client_id: string;
  tenant_id: string;
  audience: string;
  origin: string;
  redirect_uri: string;
  post_logout_redirect_uri: string;
};
export type Identity = { tenant_id: string; subject: string; roles: string[] };
let manager: UserManager | null = null;
let config: Config | null = null;
let initialized: Promise<User | null> | null = null;

export async function initialize() {
  if (initialized) return initialized;
  initialized = (async () => {
    const response = await fetch("/workspace/config", {
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) throw new Error("인증 구성을 읽지 못했습니다.");
    config = (await response.json()) as Config;
    if (
      config.version !== 1 ||
      config.origin !== window.location.origin ||
      config.redirect_uri !== config.origin + "/workspace/callback" ||
      config.post_logout_redirect_uri !== config.origin + "/workspace/" ||
      !config.client_id ||
      !config.tenant_id ||
      !config.audience
    )
      throw new Error("인증 구성을 확인할 수 없습니다.");
    const issuer = new URL(config.issuer);
    if (
      issuer.protocol !== "https:" &&
      !(issuer.protocol === "http:" && issuer.hostname === "127.0.0.1")
    )
      throw new Error("검증된 인증 서버가 필요합니다.");
    manager = new UserManager({
      authority: config.issuer,
      client_id: config.client_id,
      redirect_uri: config.redirect_uri,
      post_logout_redirect_uri: config.post_logout_redirect_uri,
      response_type: "code",
      scope: "openid profile",
      automaticSilentRenew: false,
      monitorSession: false,
      loadUserInfo: false,
      stateStore: new WebStorageStateStore({
        store: window.sessionStorage,
        prefix: "sova.oidc.state.",
      }),
      userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
      staleStateAgeInSeconds: 300,
    });
    manager.events.addAccessTokenExpired(() => {
      void manager?.removeUser();
      window.dispatchEvent(new Event("sova-auth-expired"));
    });
    if (window.location.pathname === "/workspace/callback") {
      try {
        return await manager.signinRedirectCallback();
      } finally {
        window.history.replaceState({}, "", "/workspace/");
      }
    }
    await manager.clearStaleState();
    return manager.getUser();
  })();
  return initialized;
}

export async function login() {
  await initialize();
  if (!manager) throw new Error("인증 서버를 확인하세요.");
  await manager.signinRedirect({ prompt: "login" });
}
export async function logout() {
  if (!manager) return;
  await manager.removeUser();
  // No access/id token is put into a logout URL. A new sign-in requires login.
  window.location.replace("/workspace/");
}
export const currentConfig = () => config;
export async function api<T>(
  path: string,
  body?: unknown,
  method?: "POST" | "PUT",
): Promise<T> {
  if (!config || !manager) throw new Error("먼저 로그인하세요.");
  const user = await manager.getUser();
  if (!user || user.expired || !user.access_token)
    throw new Error("로그인이 만료되었습니다.");
  const prefix = "/v1/tenants/" + encodeURIComponent(config.tenant_id);
  if (
    !path.startsWith("/") ||
    (path.includes("?") && !path.includes("?after="))
  )
    throw new Error("등록된 API 경로가 필요합니다.");
  let response: Response;
  try {
    response = await fetch(prefix + path, {
      method: method || (body === undefined ? "GET" : "POST"),
      headers: {
        Authorization: "Bearer " + user.access_token,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(15000),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new Error(
      body === undefined
        ? "현재 서버 응답을 확인하지 못했습니다."
        : "요청 결과 미확인. 자동 재시도하지 않습니다. 계획과 원장을 다시 조회하세요.",
    );
  }
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) {
      await manager.removeUser();
      window.dispatchEvent(new Event("sova-auth-expired"));
    }
    if (
      value &&
      typeof value.status === "string" &&
      Array.isArray(value.blockers)
    )
      return value as T;
    throw new Error(
      response.status === 403
        ? "현재 사용자에게 이 동작의 권한이 없거나 작성자·승인자가 같습니다."
        : `요청이 거부됐습니다 (HTTP ${response.status}). 현재 계획·승인·근거를 다시 확인하세요.`,
    );
  }
  return value as T;
}
