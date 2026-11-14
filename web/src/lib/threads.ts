// 실행 목록 → 대화 목록. '이어서' 한 실행들(parent_id 로 연결)은 대화 하나로 묶는다 (threads.test.ts)
import type { Schemas } from '@/api';

export type RunSummary = Schemas['RunSummary'];
export interface Thread {
  root: RunSummary;    // 대화의 첫 실행 (주소 /c/<root.id>)
  chain: RunSummary[]; // 처음부터 마지막까지 (각 실행의 가장 최근 '이어서')
  last: RunSummary;
}

export function groupThreads(runs: RunSummary[]): Thread[] {
  const byId = new Map(runs.map((r) => [r.id, r]));
  const children = new Map<string, RunSummary[]>();
  for (const r of runs) {
    if (r.parent_id && byId.has(r.parent_id)) children.set(r.parent_id, [...(children.get(r.parent_id) ?? []), r]);
  }
  return runs
    .filter((r) => !r.parent_id || !byId.has(r.parent_id))
    .map((root) => {
      const chain = [root];
      let cur = root;
      while (children.has(cur.id) && chain.length < 50) {
        cur = [...children.get(cur.id)!].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
        chain.push(cur);
      }
      return { root, chain, last: cur };
    })
    .sort((a, b) => (a.last.created_at < b.last.created_at ? 1 : -1));
}

export function statusDot(status: string): string {
  return status === 'running' ? 'running' : status === 'error' ? 'error' : status === 'done' ? '' : 'stuck';
}
