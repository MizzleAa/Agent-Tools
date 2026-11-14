// 워크플로 흐름도: 노드를 단계(열)별로 놓고 '기다림' 관계를 화살표로 잇는다
import { useLayoutEffect, useRef, useState } from 'react';
import type { Schemas } from '@/api';
import { levels } from './workflowEdit';

type Node = Schemas['Workflow-Output']['nodes'][number];

export function FlowDiagram({ nodes }: { nodes: Node[] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<string[]>([]);
  const level = levels(nodes);
  const cols = Math.max(0, ...Object.values(level)) + 1;
  const rows: Record<number, number> = {};
  const place = nodes.map((n) => { const c = level[n.id]; rows[c] = (rows[c] ?? 0) + 1; return { n, col: c + 1, row: rows[c] }; });
  const sig = JSON.stringify(nodes.map((n) => [n.id, n.after, n.title, n.profile]));

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const draw = () => {
      const base = el.getBoundingClientRect();
      const at = (id: string) => el.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      const out: string[] = [];
      for (const n of nodes) {
        for (const a of n.after ?? []) {
          const s = at(a), t = at(n.id);
          if (!s || !t) continue;
          const x1 = s.right - base.left, y1 = s.top + s.height / 2 - base.top, x2 = t.left - base.left, y2 = t.top + t.height / 2 - base.top;
          const mid = (x1 + x2) / 2;
          out.push(`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 6},${y2}`, `M${x2 - 7},${y2 - 4} L${x2},${y2} L${x2 - 7},${y2 + 4}`);
        }
      }
      setPaths(out);
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    return () => ro.disconnect();
  }, [sig]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flow" ref={wrap} style={{ gridTemplateColumns: `repeat(${cols}, minmax(130px, 1fr))` }}>
      {place.map(({ n, col, row }, i) => (
        <div key={n.id} className="flow-box" data-id={n.id} style={{ gridColumn: col, gridRow: row }}>
          <span className="wf-n">{i + 1}</span><b>{n.title || n.id}</b><span className="muted small">{n.profile}</span>
        </div>
      ))}
      <svg className="flow-lines">{paths.map((d, i) => <path key={i} d={d} />)}</svg>
    </div>
  );
}
