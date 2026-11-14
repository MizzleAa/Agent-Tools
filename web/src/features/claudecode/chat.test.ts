import { describe, expect, it } from 'vitest';
import type { NormMessage, ServerEvent } from './types';
import { buildItems } from './items';
import { applyEvent, applyHistoryPage, emptyChat, mergedMessages } from './chat';

const user = (uuid: string | null, text: string, local_id?: string): NormMessage => ({ uuid, local_id, role: 'user', kind: 'prompt', blocks: [{ type: 'text', text }] });
const asst = (uuid: string, text: string, message_id = 'm1'): NormMessage => ({ uuid, role: 'assistant', kind: 'assistant', blocks: [{ type: 'text', text }], message_id });
const ev = (e: Omit<ServerEvent, 'session_id'> & { session_id?: string }) => ({ session_id: 's', ...e }) as ServerEvent;

describe('chat reducer', () => {
  it('낙관적 사용자 메시지를 재생 메시지로 바꾸고, 부분 스트림을 완성 메시지로 바꾼다', () => {
    let s = emptyChat();
    s = applyEvent(s, ev({ type: 'turn.started', data: { turn_id: 't1', prompt: user(null, 'hi', 'local-1') } }));
    expect(mergedMessages(s).map((m) => m.local_id)).toEqual(['local-1']);
    s = applyEvent(s, ev({ type: 'message', data: { message: { ...user('u1', 'hi'), local_id: 'local-1' }, replaces_local: 'local-1' } }));
    expect(s.optimistic).toBeNull();
    s = applyEvent(s, ev({ type: 'stream.block_start', data: { partial: { key: 'm1:0', message_id: 'm1', index: 0, type: 'text', text: '' } } }));
    s = applyEvent(s, ev({ type: 'stream.delta', data: { key: 'm1:0', text: '안녕' } }));
    s = applyEvent(s, ev({ type: 'stream.delta', data: { key: 'm1:0', text: '하세요' } }));
    expect(s.partial[0].text).toBe('안녕하세요');
    s = applyEvent(s, ev({ type: 'message', data: { message: asst('a1', '안녕하세요'), replaces_partial: 'm1:0' } }));
    expect(s.partial).toHaveLength(0);
    s = applyEvent(s, ev({ type: 'turn.completed', data: { result: { turn_id: 't1', subtype: 'success', is_error: false, interrupted: false, duration_ms: 10, num_turns: 1, total_cost_usd: 0.01, result: 'ok', permission_denials: 0 } } }));
    expect(s.turn).toBeNull();
    expect(mergedMessages(s).map((m) => m.uuid)).toEqual(['u1', 'a1']);
  });

  it('기록과 실시간 메시지의 중복을 uuid 로 없앤다 (재연결 시나리오)', () => {
    let s = emptyChat();
    s = applyEvent(s, ev({ type: 'message', data: { message: asst('a1', 'x') } }));
    s = applyHistoryPage(s, { total: 2, start: 0, messages: [user('u1', 'q'), asst('a1', 'x')] });
    expect(mergedMessages(s).map((m) => m.uuid)).toEqual(['u1', 'a1']);
    s = applyEvent(s, ev({ type: 'message', data: { message: asst('a1', 'x') } }));
    expect(mergedMessages(s)).toHaveLength(2);
  });

  it('스냅숏으로 진행 중인 응답과 승인 대기를 되살린다', () => {
    let s = applyHistoryPage(emptyChat(), { total: 1, start: 0, messages: [user('u1', 'q')] });
    s = applyEvent(s, ev({
      type: 'snapshot',
      data: {
        session: null, process: null, init: null,
        pending: [{ id: 'p1', kind: 'permission', tool_name: 'Bash', tool_use_id: 't9', input: { command: 'rm x' }, suggestions: [], created_at: 1 }],
        live_turn: { turn_id: 't1', started_at: 1, prompt: user('u1', 'q'), messages: [asst('a1', 'part')], partial: [{ key: 'm2:0', message_id: 'm2', index: 0, type: 'text', text: '쓰는 중' }], status_text: null, interrupted: false },
      },
    }));
    expect(s.turn?.id).toBe('t1');
    expect(s.pending).toHaveLength(1);
    expect(mergedMessages(s).map((m) => m.uuid)).toEqual(['u1', 'a1']);
    expect(s.partial[0].text).toBe('쓰는 중');
  });

  it('CLI 비정상 종료 시 보낸 메시지를 실패로 표시한다', () => {
    let s = applyEvent(emptyChat(), ev({ type: 'turn.started', data: { turn_id: 't1', prompt: user(null, 'hi', 'l1') } }));
    s = applyEvent(s, ev({ type: 'turn.error', data: { turn_id: 't1', error: 'boom' } }));
    expect(s.turnError).toBe('boom');
    expect(mergedMessages(s)[0].failed).toBe(true);
    expect(s.lastPromptText).toBe('hi');
  });
});

describe('buildItems', () => {
  it('도구 호출과 결과를 묶고, 서브에이전트 메시지는 부모 카드 안에 넣는다', () => {
    const msgs: NormMessage[] = [
      user('u1', 'go'),
      { uuid: 'a1', role: 'assistant', kind: 'assistant', blocks: [{ type: 'tool_use', id: 'T', name: 'Task', input: { description: 'sub' } }] },
      { uuid: 'a2', role: 'assistant', kind: 'assistant', parent_tool_use_id: 'T', blocks: [{ type: 'tool_use', id: 'B', name: 'Bash', input: { command: 'ls' } }] },
      { uuid: 'r1', role: 'user', kind: 'tool_results', parent_tool_use_id: 'T', blocks: [{ type: 'tool_result', tool_use_id: 'B', is_error: false, content: 'a.txt' }] },
      { uuid: 'r2', role: 'user', kind: 'tool_results', blocks: [{ type: 'tool_result', tool_use_id: 'T', is_error: false, content: 'done' }] },
      asst('a3', '끝'),
    ];
    const items = buildItems(msgs, [], []);
    expect(items.map((i) => i.kind)).toEqual(['prompt', 'tool', 'text']);
    const task = items[1] as Extract<(typeof items)[number], { kind: 'tool' }>;
    expect(task.result?.content).toBe('done');
    expect(task.children).toHaveLength(1);
    expect((task.children[0] as typeof task).result?.content).toBe('a.txt');
  });
});
