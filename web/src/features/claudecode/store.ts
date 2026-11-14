// Claude Code 탭 상태. 외부 라이브러리 없이 useSyncExternalStore 로 만든 작은 저장소 (폐쇄망 원칙 — 의존성 최소).
import { useSyncExternalStore } from 'react';
import type { CcSettings, CcStatus, ExternalSession, Project, Session } from './api';
import { applyEvent, emptyChat, type ChatState } from './chat';
import type { ProcessInfo, RateLimit, ServerEvent } from './types';

export interface CcState {
  projects: Project[];
  sessions: Record<string, Session>;
  external: Record<string, ExternalSession[]>;
  externalError: Record<string, string | null>;
  selectedProjectId: string | null;
  selectedSessionId: string | null;
  chats: Record<string, ChatState>;
  status: CcStatus | null;
  settings: CcSettings | null;
  conn: 'connecting' | 'open' | 'closed';
  processes: Record<string, ProcessInfo>;
  rateLimit: RateLimit | null;
}

const initial = (): CcState => ({
  projects: [],
  sessions: {},
  external: {},
  externalError: {},
  selectedProjectId: null,
  selectedSessionId: null,
  chats: {},
  status: null,
  settings: null,
  conn: 'connecting',
  processes: {},
  rateLimit: null,
});

let state: CcState = initial();
const listeners = new Set<() => void>();

export const getState = () => state;

export function setState(patch: Partial<CcState> | ((s: CcState) => Partial<CcState>)): void {
  const next = typeof patch === 'function' ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

export function resetState(): void {
  state = initial();
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** 선택자는 같은 값이면 같은 참조를 돌려줘야 한다 (새 배열·객체를 만들면 무한 렌더) */
export function useCc<T>(selector: (s: CcState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(state));
}

const EMPTY = emptyChat();
export function chatOf(s: CcState, sessionId: string | null): ChatState {
  return (sessionId && s.chats[sessionId]) || EMPTY;
}

export function updateChat(sessionId: string, fn: (c: ChatState) => ChatState): void {
  setState((s) => ({ chats: { ...s.chats, [sessionId]: fn(s.chats[sessionId] ?? emptyChat()) } }));
}

export function upsertSession(sess: Session): void {
  setState((s) => ({ sessions: { ...s.sessions, [sess.id]: { ...s.sessions[sess.id], ...sess } } }));
}

// 화면 밖(actions)에서도 알림·확인 창을 쓰도록 Agent 의 useUi 를 꽂아 둔다 (ClaudeCodeView 가 연결)
export interface UiBridge {
  toast(message: string, kind?: 'info' | 'ok' | 'error', ms?: number): void;
  confirm(o: { title: string; message?: string; confirmText?: string; danger?: boolean }): Promise<boolean>;
}
let ui: UiBridge = { toast: () => undefined, confirm: async () => false };
export const setUi = (u: UiBridge) => { ui = u; };
export const toast = (message: string, kind: 'info' | 'ok' | 'error' = 'info', ms?: number) => ui.toast(message, kind, ms);
export const confirmUi = (o: Parameters<UiBridge['confirm']>[0]) => ui.confirm(o);

/** 서버 이벤트(SSE)를 상태에 반영한다. */
export function handleServerEvent(ev: ServerEvent): void {
  switch (ev.type) {
    case 'session.updated':
      upsertSession(ev.data.session);
      return;
    case 'session.deleted':
      setState((s) => {
        const sessions = { ...s.sessions };
        delete sessions[ev.data.session_id];
        return { sessions, selectedSessionId: s.selectedSessionId === ev.data.session_id ? null : s.selectedSessionId };
      });
      return;
    case 'project.updated':
      setState((s) => {
        const i = s.projects.findIndex((p) => p.id === ev.data.project.id);
        const projects = s.projects.slice();
        if (i >= 0) projects[i] = { ...projects[i], ...ev.data.project };
        else projects.unshift(ev.data.project);
        return { projects };
      });
      return;
    case 'project.deleted':
      setState((s) => ({
        projects: s.projects.filter((p) => p.id !== ev.data.project_id),
        selectedProjectId: s.selectedProjectId === ev.data.project_id ? null : s.selectedProjectId,
      }));
      return;
    case 'process.updated':
      setState((s) => ({ processes: { ...s.processes, [ev.session_id]: ev.data.process } }));
      return;
    case 'rate_limit':
      setState({ rateLimit: ev.data.rate_limit });
      return;
    case 'notice':
      if (ev.data.level !== 'info' && ev.session_id !== state.selectedSessionId) toast(ev.data.text, 'error');
      break;
    case 'hello':
    case 'resync':
      return;
  }
  if (ev.session_id) {
    const sid = ev.session_id;
    if (ev.type === 'snapshot' && ev.data.session) upsertSession(ev.data.session);
    if (ev.type === 'snapshot' && ev.data.process) setState((s) => ({ processes: { ...s.processes, [sid]: ev.data.process! } }));
    updateChat(sid, (c) => applyEvent(c, ev));
  }
}
