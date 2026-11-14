import { describe, expect, it } from 'vitest';
import { groupThreads, type RunSummary } from './threads';

const run = (id: string, created_at: string, parent_id = '', status = 'done') =>
  ({ id, task: id, profile: 'general', model: 'm', status, answer: '', steps: 0, prompt_tokens: 0, completion_tokens: 0,
     seconds: 0, created_at, finished_at: null, parent_id, workdir: '' }) as RunSummary;

describe('groupThreads', () => {
  it('이어서 한 실행을 대화 하나로 묶고 최근 대화부터', () => {
    const t = groupThreads([run('a', '1'), run('b', '2', 'a'), run('c', '3'), run('d', '4', 'b', 'running')]);
    expect(t.map((x) => [x.root.id, x.chain.map((r) => r.id), x.last.status])).toEqual([
      ['a', ['a', 'b', 'd'], 'running'],
      ['c', ['c'], 'done'],
    ]);
  });
  it('부모가 목록에 없으면 그 실행이 대화의 시작', () => {
    expect(groupThreads([run('x', '1', 'gone')]).map((t) => t.root.id)).toEqual(['x']);
  });
});
