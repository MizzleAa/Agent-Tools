import { describe, expect, it } from 'vitest';
import type { RunEvent } from '@/api';
import { buildTurn, collapseImages, finalNotice, imageUrl } from './turnModel';

const ev = (e: Record<string, unknown>, seq: number) => ({ seq, ...e }) as unknown as RunEvent;

describe('buildTurn', () => {
  it('도구 호출에 승인·결과를 붙인다', () => {
    const t = buildTurn([
      ev({ type: 'RunStarted', run_id: 'r1', task: '저장해줘', profile: 'general' }, 0),
      ev({ type: 'LLMCall', step: 1, content: '저장합니다', reasoning: '생각', tool_calls: [{ id: 'c1' }] }, 1),
      ev({ type: 'ToolCall', step: 1, call_id: 'c1', name: 'write_file', arguments: { path: 'a.md' }, permission: 'write' }, 2),
      ev({ type: 'ApprovalRequested', step: 1, call_id: 'c1', name: 'write_file', arguments: {}, reason: '쓰기' }, 3),
      ev({ type: 'ApprovalResolved', call_id: 'c1', approved: true }, 4),
      ev({ type: 'ToolResult', step: 1, call_id: 'c1', name: 'write_file', ok: true, output: '저장함', seconds: 0.1 }, 5),
      ev({ type: 'Final', status: 'done', answer: '끝', steps: 2 }, 6),
    ]);
    expect(t.runId).toBe('r1');
    expect(t.items.map((i) => i.kind)).toEqual(['thinking', 'interim', 'tool']);
    const tool = t.items[2];
    expect(tool.kind === 'tool' && [tool.approved, tool.result?.output]).toEqual([true, '저장함']);
    expect(t.final?.answer).toBe('끝');
  });

  it('사내 API 이미지: Images 이벤트를 순서대로 모으고, 주소에는 실행 id·번호만', () => {
    const item = (id: number, ok: boolean) => ({ id, api: 'parts', path: `/p/${id}/picture`, label: `P_${id}.bmp`, ok,
      reason: ok ? '' : '찾을 수 없음 (HTTP 404)', format: ok ? 'bmp' : '', width: 10, height: 5, bytes: 50 });
    const t = buildTurn([
      ev({ type: 'RunStarted', run_id: 'r9', task: '보여줘', profile: 'intranet' }, 0),
      ev({ type: 'Images', step: 1, call_id: 'c1', items: [item(1, true)] }, 1),
      ev({ type: 'Images', step: 2, call_id: 'c2', items: [item(2, false)] }, 2),
      ev({ type: 'Final', status: 'done', answer: '붙였습니다', steps: 3 }, 3),
    ]);
    expect(t.images.map((im) => [im.id, im.ok])).toEqual([[1, true], [2, false]]);
    expect(t.items).toEqual([]);   // 이미지는 도구 칸이 아니라 답변 아래 갤러리로
    expect(imageUrl('r9', 1, 'thumb')).toBe('/api/v1/runs/r9/images/1/thumb');
  });

  it('같은 이미지의 실패한 시도는 표시된 것이 있으면 빼고, 실패만 있으면 마지막 것만', () => {
    const im = (id: number, label: string, ok: boolean) => ({ id, api: 'a', path: `/p/${id}`, label, ok, reason: ok ? '' : '찾을 수 없음',
      format: '', width: 0, height: 0, bytes: 0 });
    const out = collapseImages([im(1, 'A.bmp', false), im(2, 'A.bmp', false), im(3, 'A.bmp', true), im(4, 'B.bmp', false), im(5, 'B.bmp', false)]);
    expect(out.map((x) => x.id)).toEqual([3, 5]);
  });

  it('워크플로: 노드 안의 도구는 그 노드 칸 안에, 건너뛴 노드는 끝만', () => {
    const t = buildTurn([
      ev({ type: 'RunStarted', run_id: 'w', task: 'x', profile: 'workflow:two' }, 0),
      ev({ type: 'WorkflowStarted', name: 'two', nodes: [{ id: 'a', title: 'A', profile: 'general', after: [] }, { id: 'b', title: 'B', profile: 'general', after: ['a'] }] }, 1),
      ev({ type: 'NodeStarted', node: 'a', title: 'A', profile: 'general', index: 1, total: 2, task: 'x' }, 2),
      ev({ type: 'ToolCall', step: 1, call_id: 'c', name: 'list_dir', arguments: {}, permission: 'read' }, 3),
      ev({ type: 'NodeFinished', node: 'a', title: 'A', status: 'no_action', answer: '' }, 4),
      ev({ type: 'NodeFinished', node: 'b', title: 'B', status: 'skipped', answer: '건너뜀' }, 5),
      ev({ type: 'Final', status: 'no_action', answer: '멈춤' }, 6),
    ]);
    expect(t.items.map((i) => i.kind)).toEqual(['strip', 'node', 'node']);
    const [, a, b] = t.items;
    expect(a.kind === 'node' && a.items.map((i) => i.kind)).toEqual(['tool']);
    expect(b.kind === 'node' && [b.start, b.finish?.status]).toEqual([undefined, 'skipped']);
    expect(t.workflow?.state).toEqual({ a: 'fail', b: 'skip' });
  });
});

describe('finalNotice', () => {
  it('워크플로는 멈춘 노드부터 계속을 안내한다', () => {
    expect(finalNotice('max_steps', 30, true)).toContain('멈춘 노드부터 계속');
    expect(finalNotice('max_steps', 30, false)).toContain('스텝 한도(30)');
  });
});
