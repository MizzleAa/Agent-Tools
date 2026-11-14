// Claude Code(CLI) 동작: REST 호출 → 상태 반영, 실시간 이벤트(SSE) 연결.
import { CcError, ccApi, fileToBase64, type Project, type Session } from './api';
import { applyHistoryPage, prependHistory } from './chat';
import { confirmUi, getState, handleServerEvent, setState, toast, updateChat, upsertSession } from './store';
import type { NormMessage, ServerEvent } from './types';

const HISTORY_PAGE = 150;
export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

// ───────── 상태 · 목록 (관리자만 부른다 — AppState 가 주기적으로)
export async function refreshCc(refresh = false): Promise<void> {
  try {
    const status = await ccApi.status(refresh);
    setState({ status });
    if (!status.installed) return;
    const [settings, projects, sessions] = await Promise.all([ccApi.settings(), ccApi.projects(), ccApi.sessions()]);
    setState((s) => {
      const map: Record<string, Session> = {};
      for (const x of sessions) map[x.id] = { ...s.sessions[x.id], ...x };
      return { settings, projects, sessions: map, rateLimit: (status.rate_limit as never) ?? s.rateLimit };
    });
  } catch {
    /* 연결 끊김 등 — 다음 주기에 다시 */
  }
}

// ───────── 실시간 이벤트 (세션 하나를 보는 동안 연다)
let source: EventSource | null = null;
let sourceFor: string | null = null;
let hadError = false;

export function connectEvents(sessionId: string | null): void {
  if (source && sourceFor === sessionId) return;
  source?.close();
  sourceFor = sessionId;
  hadError = false;
  setState({ conn: 'connecting' });
  const es = new EventSource(ccApi.eventsUrl(sessionId));
  source = es;
  es.onopen = () => {
    setState({ conn: 'open' });
    if (hadError) {   // 끊긴 동안 생긴 일을 따라잡는다 (다시 붙으면 서버가 스냅숏도 보낸다)
      hadError = false;
      void refreshCc();
      if (sessionId) void loadHistory(sessionId);
    }
  };
  es.onmessage = (m) => {
    try { handleServerEvent(JSON.parse(m.data) as ServerEvent); } catch { /* 깨진 줄 무시 */ }
  };
  es.onerror = () => { hadError = true; setState({ conn: 'closed' }); };   // EventSource 가 알아서 다시 붙는다
}

export function disconnectEvents(): void {
  source?.close();
  source = null;
  sourceFor = null;
}

// ───────── 세션 보기
export async function openSession(sessionId: string): Promise<void> {
  setState({ selectedSessionId: sessionId });
  connectEvents(sessionId);
  ccApi.open(sessionId).then(upsertSession).catch(() => undefined);   // 최근 사용 + CLI 미리 시작
  if (!getState().sessions[sessionId]) ccApi.session(sessionId).then(upsertSession).catch((e) => toast(errText(e), 'error'));
  await loadHistory(sessionId);
}

export async function loadHistory(sessionId: string): Promise<void> {
  try {
    const page = await ccApi.messages(sessionId, HISTORY_PAGE);
    updateChat(sessionId, (c) => applyHistoryPage(c, { ...page, messages: page.messages as unknown as NormMessage[] }));
  } catch (e) {
    updateChat(sessionId, (c) => ({ ...c, historyLoaded: true, historyError: errText(e) }));
  }
}

export async function loadOlder(sessionId: string): Promise<void> {
  const c = getState().chats[sessionId];
  if (!c || c.historyStart <= 0) return;
  try {
    const page = await ccApi.messages(sessionId, HISTORY_PAGE, c.historyStart);
    updateChat(sessionId, (x) => prependHistory(x, { start: page.start, messages: page.messages as unknown as NormMessage[] }));
  } catch (e) {
    toast(errText(e), 'error');
  }
}

// ───────── 보내기
async function uploadAll(sessionId: string, files: File[]): Promise<string[]> {
  const ids: string[] = [];
  for (const f of files) {
    const att = await ccApi.upload(sessionId, { name: f.name || 'pasted.png', mime: f.type, data_base64: await fileToBase64(f) });
    ids.push(att.id);
  }
  return ids;
}

/** 이어서 보내기. 다른 Claude Code 창에서 같은 세션이 열려 있으면 물어본다. */
export async function sendTo(sessionId: string, text: string, files: File[] = [], force = false): Promise<boolean> {
  try {
    const ids = await uploadAll(sessionId, files);
    await ccApi.send(sessionId, { text, attachments: ids, force });
    return true;
  } catch (e) {
    if (e instanceof CcError && e.code === 'session_in_use_elsewhere' && !force) {
      const ok = await confirmUi({ title: '다른 창에서 실행 중인 세션', danger: true, confirmText: '그래도 보내기',
        message: `${e.message}\n\n그래도 보내면 두 곳에서 같은 세션 기록에 이어 쓰게 됩니다.` });
      return ok ? sendTo(sessionId, text, [], true) : false;
    }
    toast(errText(e), 'error', 8000);
    return false;
  }
}

/** 새 대화: 작업 위치(base, '' = workspace) 아래에 에이전트의 새 작업과 같은 규칙으로 폴더를 만들고 바로 보낸다. 만든 세션 id. */
export async function startAuto(text: string, files: File[], model: string, mode: string, base = '', profile = '', effort = '',
  harness = '', engine: 'claude' | 'codex' | 'gemini' = 'claude'): Promise<string | null> {
  try {
    const sess = await ccApi.newConversation({
      task: text, base_dir: base, model: model || null, permission_mode: mode as 'default' | 'acceptEdits' | 'plan',
      profile, effort: effort as '' | 'low' | 'medium' | 'high' | 'xhigh' | 'max', harness, engine,
    });
    upsertSession(sess);
    await sendTo(sess.id, text, files);   // 보내기가 실패해도 세션은 만들어졌으므로 그 세션을 연다 (오류는 알림으로)
    return sess.id;
  } catch (e) {
    toast(errText(e), 'error', 8000);
    return null;
  }
}

export async function interrupt(sessionId: string): Promise<void> {
  try { await ccApi.interrupt(sessionId); } catch (e) { toast(errText(e), 'error'); }
}

export async function resolvePermission(sessionId: string, permissionId: string,
  body: { behavior: 'allow' | 'deny'; message?: string; apply_suggestions?: number[]; answers?: Record<string, string>; interrupt?: boolean }): Promise<void> {
  try { await ccApi.permission(sessionId, permissionId, { apply_suggestions: [], ...body }); } catch (e) { toast(errText(e), 'error'); }
}

export async function patchSession(sessionId: string, body: {
  title?: string; model?: string | null; permission_mode?: 'default' | 'acceptEdits' | 'plan';
  effort?: '' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'; profile?: string;
}): Promise<void> {
  try { upsertSession(await ccApi.updateSession(sessionId, body)); } catch (e) { toast(errText(e), 'error'); }
}

export async function removeSession(sessionId: string): Promise<boolean> {
  try {
    await ccApi.deleteSession(sessionId);
    setState((s) => {
      const sessions = { ...s.sessions };
      delete sessions[sessionId];
      return { sessions };
    });
    return true;
  } catch (e) {
    toast(errText(e), 'error');
    return false;
  }
}

export async function stopProcess(sessionId: string): Promise<void> {
  try { await ccApi.stopProcess(sessionId); } catch (e) { toast(errText(e), 'error'); }
}

export async function startProcess(sessionId: string): Promise<void> {
  try { await ccApi.startProcess(sessionId); } catch (e) { toast(errText(e), 'error', 8000); }
}

export async function terminateOrphan(registryId: number): Promise<void> {
  try { await ccApi.terminate(registryId); toast('이전 프로세스를 종료했습니다.', 'ok'); } catch (e) { toast(errText(e), 'error'); }
}

// ───────── 작업 폴더(프로젝트)
export async function addProject(path: string, name?: string): Promise<Project | null> {
  try {
    const p = await ccApi.createProject(path, name);
    setState((s) => ({ projects: [p, ...s.projects.filter((x) => x.id !== p.id)] }));
    return p;
  } catch (e) {
    if (e instanceof CcError && e.code === 'project_exists' && typeof e.extra.project_id === 'string') {
      return getState().projects.find((x) => x.id === e.extra.project_id) ?? null;
    }
    toast(errText(e), 'error');
    return null;
  }
}

export async function loadExternal(projectId: string): Promise<void> {
  try {
    const r = await ccApi.projectSessions(projectId);
    setState((s) => ({ external: { ...s.external, [projectId]: r.external }, externalError: { ...s.externalError, [projectId]: r.external_error ?? null } }));
  } catch { /* 목록만 못 보여 줌 */ }
}

export async function importExternal(projectId: string, claudeSessionId: string): Promise<string | null> {
  try {
    const sess = await ccApi.importSession(projectId, claudeSessionId);
    upsertSession(sess);
    setState((s) => ({ external: { ...s.external, [projectId]: (s.external[projectId] ?? []).filter((x) => x.claude_session_id !== claudeSessionId) } }));
    return sess.id;
  } catch (e) {
    toast(errText(e), 'error');
    return null;
  }
}
