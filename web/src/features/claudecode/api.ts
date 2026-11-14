// Claude Code(CLI) API — 모양은 서버 계약(schema.d.ts)에서. 오류는 {detail} + X-Error-Code 머리글로 경우를 나눈다.
import { AUTH_EVENT, http, type Schemas as S } from '@/api/client';

export type Project = S['ProjectOut'];
export type Session = S['CcSession'];
export type SessionStatus = Session['status'];
export type ExternalSession = S['ExternalSession'];
export type CcStatus = S['CcStatus'];
export type CcSettings = S['CcSettings'];
export type ProcessRow = S['ProcessRow'];
export type Attachment = S['AttachmentOut'];

export class CcError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string, public readonly extra: Record<string, unknown> = {}) {
    super(message);
    this.name = 'CcError';
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

async function cc<T>(call: Promise<Result<T>>): Promise<T> {
  let r: Result<T>;
  try {
    r = await call;
  } catch {
    throw new CcError(0, 'network', '관리 서버에 연결할 수 없습니다.');
  }
  if (r.response.status === 401) window.dispatchEvent(new Event(AUTH_EVENT));
  if (r.error !== undefined || !r.response.ok) {
    const detail = (r.error as { detail?: unknown } | undefined)?.detail;
    let extra: Record<string, unknown> = {};
    try { extra = JSON.parse(r.response.headers.get('x-error-extra') ?? '{}'); } catch { /* 없음 */ }
    throw new CcError(r.response.status, r.response.headers.get('x-error-code') ?? `http_${r.response.status}`,
      typeof detail === 'string' ? detail : `요청 실패 (${r.response.status})`, extra);
  }
  return r.data as T;
}

const P = '/api/v1/claude-code';
const sid = (session_id: string) => ({ params: { path: { session_id } } });
const pid = (project_id: string) => ({ params: { path: { project_id } } });

export const ccApi = {
  status: (refresh = false) => cc(http.GET(`${P}/status`, { params: { query: { refresh } } })),
  settings: () => cc(http.GET(`${P}/settings`)),
  saveSettings: (body: S['CcSettingsUpdate']) => cc(http.PUT(`${P}/settings`, { body })),
  processes: () => cc(http.GET(`${P}/processes`)),
  engineLogin: (engine: 'codex' | 'gemini') => cc(http.POST(`${P}/engines/{engine}/login`, { params: { path: { engine } } })),
  engineInstall: (engine: 'codex' | 'gemini') => cc(http.POST(`${P}/engines/{engine}/install`, { params: { path: { engine } } })),
  terminate: (registry_id: number) => cc(http.POST(`${P}/processes/{registry_id}/terminate`, { params: { path: { registry_id } } })),
  projects: () => cc(http.GET(`${P}/projects`)),
  createProject: (path: string, name?: string) => cc(http.POST(`${P}/projects`, { body: { path, name: name || null } })),
  updateProject: (project_id: string, body: S['ProjectUpdate']) => cc(http.PATCH(`${P}/projects/{project_id}`, { ...pid(project_id), body })),
  deleteProject: (project_id: string) => cc(http.DELETE(`${P}/projects/{project_id}`, pid(project_id))),
  projectSessions: (project_id: string) => cc(http.GET(`${P}/projects/{project_id}/sessions`, pid(project_id))),
  createSession: (project_id: string, body: S['SessionCreate']) => cc(http.POST(`${P}/projects/{project_id}/sessions`, { ...pid(project_id), body })),
  /** 새 대화: 에이전트의 새 작업처럼 작업 폴더(workspace/날짜_시각_요청)를 자동으로 만든다 */
  newConversation: (body: S['ConversationCreate']) => cc(http.POST(`${P}/conversations`, { body })),
  /** CLI 에 붙는 지침 (로컬·API 와 같은 '설정 · 프롬프트' + 하네스). session_id 를 주면 그 대화 기준 */
  instructions: (profile?: string, session_id?: string, harness?: string) =>
    cc(http.GET(`${P}/instructions`, { params: { query: { profile: profile ?? '', session_id: session_id ?? null, harness: harness ?? '' } } })),
  importSession: (project_id: string, claude_session_id: string) =>
    cc(http.POST(`${P}/projects/{project_id}/sessions/import`, { ...pid(project_id), body: { claude_session_id } })),
  sessions: () => cc(http.GET(`${P}/sessions`)),
  session: (session_id: string) => cc(http.GET(`${P}/sessions/{session_id}`, sid(session_id))),
  updateSession: (session_id: string, body: S['SessionUpdate']) => cc(http.PATCH(`${P}/sessions/{session_id}`, { ...sid(session_id), body })),
  deleteSession: (session_id: string) => cc(http.DELETE(`${P}/sessions/{session_id}`, sid(session_id))),
  open: (session_id: string) => cc(http.POST(`${P}/sessions/{session_id}/open`, sid(session_id))),
  messages: (session_id: string, limit: number, before?: number) =>
    cc(http.GET(`${P}/sessions/{session_id}/messages`, { params: { path: { session_id }, query: { limit, before: before ?? null } } })),
  send: (session_id: string, body: S['SendMessage']) => cc(http.POST(`${P}/sessions/{session_id}/messages`, { ...sid(session_id), body })),
  interrupt: (session_id: string) => cc(http.POST(`${P}/sessions/{session_id}/interrupt`, sid(session_id))),
  permission: (session_id: string, permission_id: string, body: S['PermissionDecision']) =>
    cc(http.POST(`${P}/sessions/{session_id}/permissions/{permission_id}`, { params: { path: { session_id, permission_id } }, body })),
  startProcess: (session_id: string) => cc(http.POST(`${P}/sessions/{session_id}/process/start`, { ...sid(session_id), body: { force: false } })),
  stopProcess: (session_id: string) => cc(http.POST(`${P}/sessions/{session_id}/process/stop`, sid(session_id))),
  upload: (session_id: string, body: S['AttachmentIn']) => cc(http.POST(`${P}/sessions/{session_id}/attachments`, { ...sid(session_id), body })),
  eventsUrl: (session_id?: string | null) => `${P}/events${session_id ? `?session_id=${encodeURIComponent(session_id)}` : ''}`,
};

export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',', 2)[1] ?? '');
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}
