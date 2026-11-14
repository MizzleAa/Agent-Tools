// 워크플로 편집 규칙 (순수 함수 — workflowEdit.test.ts)
interface NodeLike { id: string; title?: string | null; after?: string[] | null; prompt: string }

/** 순서를 바꾸거나 노드를 지운 뒤: 기다릴 노드는 '앞에 있는 노드'만 남긴다. 연결을 뺀 노드 이름들을 돌려준다 */
export function fixAfter<T extends NodeLike>(nodes: T[]): string[] {
  const dropped: string[] = [];
  nodes.forEach((n, i) => {
    const before = new Set(nodes.slice(0, i).map((x) => x.id));
    const kept = (n.after ?? []).filter((a) => before.has(a));
    if (kept.length !== (n.after ?? []).length) dropped.push(n.title || n.id);
    n.after = kept;
  });
  return dropped;
}

/** 노드 id 를 바꾸면 다른 노드의 연결·{{참조}} 도 같이 바꾼다 */
export function renameNode<T extends NodeLike>(nodes: T[], index: number, neu: string): T[] {
  const old = nodes[index].id;
  return nodes.map((n, k) => ({
    ...n, id: k === index ? neu : n.id,
    after: (n.after ?? []).map((a) => (a === old ? neu : a)),
    prompt: n.prompt.split(`{{${old}}}`).join(`{{${neu}}}`),
  }));
}

/** 단계(열) 번호: 기다리는 노드들 중 가장 늦은 단계 + 1 */
export function levels(nodes: NodeLike[]): Record<string, number> {
  const level: Record<string, number> = {};
  for (const n of nodes) level[n.id] = Math.max(-1, ...(n.after ?? []).map((a) => level[a] ?? -1)) + 1;
  return level;
}
