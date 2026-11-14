// 세션 하나의 대화 화면 상태 + 서버 이벤트를 반영하는 순수 함수들.
// 원본 기록은 Claude Code 세션 기록(REST /messages)이고, 실시간 이벤트는 그 뒤에 덧붙는다.
// 같은 메시지는 uuid 로 중복을 없앤다 (스트림 uuid = 기록 uuid).
import type { InitInfo, NormMessage, PartialBlock, PermissionReq, ServerEvent, TurnResult } from './types';

export interface Notice {
  id: number;
  level: 'info' | 'warning' | 'error';
  text: string;
  afterCount: number; // 이 개수의 메시지 뒤에 표시
}

export interface ChatState {
  history: NormMessage[];
  historyStart: number;
  historyTotal: number;
  historyLoaded: boolean;
  historyError?: string;
  live: NormMessage[];
  optimistic: NormMessage | null;
  partial: PartialBlock[];
  pending: PermissionReq[];
  turn: { id: string; startedAt: number; statusText: string | null; retry?: { attempt: number; max_retries: number } } | null;
  lastResult: TurnResult | null;
  lastPromptText: string;
  init: InitInfo | null;
  notices: Notice[];
  turnError: string | null;
}

export const emptyChat = (): ChatState => ({
  history: [],
  historyStart: 0,
  historyTotal: 0,
  historyLoaded: false,
  live: [],
  optimistic: null,
  partial: [],
  pending: [],
  turn: null,
  lastResult: null,
  lastPromptText: '',
  init: null,
  notices: [],
  turnError: null,
});

let noticeSeq = 1;

function knownUuids(s: ChatState): Set<string> {
  const set = new Set<string>();
  for (const m of s.history) if (m.uuid) set.add(m.uuid);
  for (const m of s.live) if (m.uuid) set.add(m.uuid);
  return set;
}

export function mergedMessages(s: ChatState): NormMessage[] {
  const hist = new Set(s.history.map((m) => m.uuid).filter(Boolean) as string[]);
  const out = [...s.history, ...s.live.filter((m) => !m.uuid || !hist.has(m.uuid))];
  if (s.optimistic) out.push(s.optimistic);
  return out;
}

function promptText(m: NormMessage): string {
  return m.blocks
    .filter((b) => b.type === 'text')
    .map((b) => (b as { text: string }).text)
    .join('\n');
}

/** 최근 기록 페이지를 받았을 때 (처음 열기·재연결). */
export function applyHistoryPage(s: ChatState, page: { total: number; start: number; messages: NormMessage[] }): ChatState {
  const hist = new Set(page.messages.map((m) => m.uuid));
  return {
    ...s,
    history: page.messages,
    historyStart: page.start,
    historyTotal: page.total,
    historyLoaded: true,
    historyError: undefined,
    live: s.live.filter((m) => !m.uuid || !hist.has(m.uuid)),
  };
}

/** '이전 메시지 더 보기'. */
export function prependHistory(s: ChatState, page: { start: number; messages: NormMessage[] }): ChatState {
  const have = new Set(s.history.map((m) => m.uuid));
  return { ...s, history: [...page.messages.filter((m) => !m.uuid || !have.has(m.uuid)), ...s.history], historyStart: page.start };
}

function pushNotice(s: ChatState, level: Notice['level'], text: string): Notice[] {
  return [...s.notices, { id: noticeSeq++, level, text, afterCount: mergedMessages(s).length }].slice(-50);
}

export function applyEvent(s: ChatState, ev: ServerEvent): ChatState {
  switch (ev.type) {
    case 'snapshot': {
      const lt = ev.data.live_turn;
      if (!lt) return { ...s, pending: ev.data.pending, turn: null, partial: [], optimistic: null, init: ev.data.init ?? s.init };
      const known = knownUuids(s);
      const live = [...s.live];
      const add = (m: NormMessage) => {
        if (m.uuid && known.has(m.uuid)) return;
        if (m.uuid) known.add(m.uuid);
        live.push(m);
      };
      if (lt.prompt.uuid) add(lt.prompt);
      lt.messages.forEach(add);
      return {
        ...s,
        live,
        optimistic: lt.prompt.uuid ? null : lt.prompt,
        partial: lt.partial,
        pending: ev.data.pending,
        turn: { id: lt.turn_id, startedAt: lt.started_at * 1000, statusText: lt.status_text },
        init: ev.data.init ?? s.init,
      };
    }
    case 'turn.started':
      return {
        ...s,
        turn: { id: ev.data.turn_id, startedAt: Date.now(), statusText: null },
        optimistic: ev.data.prompt,
        lastPromptText: promptText(ev.data.prompt),
        partial: [],
        turnError: null,
      };
    case 'turn.status':
      return s.turn ? { ...s, turn: { ...s.turn, statusText: ev.data.status, retry: ev.data.retry ?? (ev.data.status === 'retrying' ? s.turn.retry : undefined) } } : s;
    case 'message': {
      const { message, replaces_partial, replaces_local } = ev.data;
      let next: ChatState = s;
      if (replaces_local && s.optimistic?.local_id === replaces_local) next = { ...next, optimistic: null };
      if (replaces_partial) next = { ...next, partial: next.partial.filter((p) => p.key !== replaces_partial) };
      if (message.uuid && knownUuids(next).has(message.uuid)) return next;
      return { ...next, live: [...next.live, message] };
    }
    case 'stream.block_start':
      return { ...s, partial: [...s.partial.filter((p) => p.key !== ev.data.partial.key), ev.data.partial] };
    case 'stream.delta': {
      const i = s.partial.findIndex((p) => p.key === ev.data.key);
      if (i < 0) return s;
      const partial = s.partial.slice();
      partial[i] = { ...partial[i], text: partial[i].text + ev.data.text };
      return { ...s, partial };
    }
    case 'permission.requested':
      return { ...s, pending: [...s.pending.filter((p) => p.id !== ev.data.permission.id), ev.data.permission] };
    case 'permission.resolved':
      return { ...s, pending: s.pending.filter((p) => p.id !== ev.data.permission_id) };
    case 'turn.completed': {
      const r = ev.data.result;
      let notices = s.notices;
      // 남은 낙관적 메시지가 있으면(재생 메시지를 못 받은 경우) 실시간 목록으로 옮긴다
      const live = s.optimistic ? [...s.live, s.optimistic] : s.live;
      const base = { ...s, live, optimistic: null };
      if (r.interrupted) notices = pushNotice(base, 'info', '응답을 중단했습니다.');
      else if (r.is_error) notices = pushNotice(base, 'error', (r.errors ?? []).join('\n') || r.result || `오류로 끝났습니다 (${r.subtype})`);
      return { ...base, turn: null, partial: [], pending: [], lastResult: r, notices };
    }
    case 'turn.error': {
      const failed = s.optimistic ? { ...s.optimistic, failed: true } : null;
      const live = failed ? [...s.live, failed] : s.live;
      return { ...s, live, optimistic: null, turn: null, partial: [], pending: [], turnError: ev.data.error };
    }
    case 'system.init':
      return { ...s, init: ev.data.init };
    case 'notice':
      return { ...s, notices: pushNotice(s, ev.data.level, ev.data.text) };
    default:
      return s;
  }
}
