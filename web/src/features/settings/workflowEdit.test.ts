import { describe, expect, it } from 'vitest';
import { fixAfter, levels, renameNode } from './workflowEdit';

const nodes = () => [
  { id: 'a', title: 'A', after: [], prompt: '{{input}}' },
  { id: 'b', title: 'B', after: ['a'], prompt: '{{a}} 정리' },
  { id: 'c', title: 'C', after: ['a', 'b'], prompt: '{{b}}' },
];

describe('workflowEdit', () => {
  it('순서를 바꾸면 앞에 없는 노드 연결을 뺀다', () => {
    const n = nodes();
    [n[0], n[1]] = [n[1], n[0]];
    expect(fixAfter(n)).toEqual(['B']);
    expect(n.map((x) => x.after)).toEqual([[], [], ['a', 'b']]);
  });
  it('이름을 바꾸면 연결과 {{참조}} 도 바뀐다', () => {
    const n = renameNode(nodes(), 0, 'research');
    expect(n.map((x) => x.id)).toEqual(['research', 'b', 'c']);
    expect(n[1]).toMatchObject({ after: ['research'], prompt: '{{research}} 정리' });
    expect(n[2].after).toEqual(['research', 'b']);
  });
  it('단계는 기다리는 노드 중 가장 늦은 단계 + 1', () => {
    expect(levels(nodes())).toEqual({ a: 0, b: 1, c: 2 });
  });
});
