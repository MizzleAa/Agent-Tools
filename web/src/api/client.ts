// 관리 서버 API 클라이언트. 주소·메서드·요청·응답 타입이 모두 schema.d.ts(= 서버의 OpenAPI 계약)에서 온다.
// 서버 API 를 바꾸면: python scripts/export_openapi.py → npm run gen:api → 타입이 안 맞는 곳을 tsc 가 알려 준다.
import createClient, { type Middleware } from 'openapi-fetch';
import type { components, paths } from './schema';

export type Schemas = components['schemas'];

/** 상태를 바꾸는 요청에 붙이는 토큰. 관리 서버가 index.html 에 넣어 준다. */
export const TOKEN = document.querySelector<HTMLMetaElement>('meta[name="agent-token"]')?.content ?? '';

/** 로그인이 필요해졌을 때(401) 알리는 이벤트 — state/Auth.tsx 가 듣고 로그인 화면을 띄운다 */
export const AUTH_EVENT = 'agent:auth';

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

const auth: Middleware = {
  onRequest({ request }) {
    if (request.method !== 'GET') request.headers.set('X-Agent-Token', TOKEN);
    return request;
  },
};

export const http = createClient<paths>({ baseUrl: '' });
http.use(auth);

type Result<T> = { data?: T; error?: unknown; response: Response };

/** 성공이면 값, 실패면 ApiError (서버 오류는 항상 { detail: "설명" }) */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await call;
  } catch {
    throw new ApiError(0, '관리 서버에 연결할 수 없습니다. start.cmd 창이 켜져 있는지 확인하세요.');
  }
  const { data, error, response } = result;
  if (response.status === 401) window.dispatchEvent(new Event(AUTH_EVENT));   // 로그인이 풀림 → 로그인 화면으로
  if (error !== undefined || !response.ok) {
    const detail = (error as { detail?: unknown } | undefined)?.detail;
    throw new ApiError(response.status, typeof detail === 'string' ? detail : `요청 실패 (${response.status} ${response.statusText})`);
  }
  return data as T;
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
