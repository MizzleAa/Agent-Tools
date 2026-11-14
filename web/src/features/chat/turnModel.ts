// 실행 하나의 이벤트 목록 → 화면에 그릴 모양 (순수 함수 — turnModel.test.ts)
// 워크플로면 노드마다 칸(node)을 만들고, 노드 안의 도구 호출은 그 칸 안에 넣는다.
import type { EventOf, RunEvent } from '@/api';

export type ToolItem = {
  kind: 'tool'; key: string; call: EventOf<'ToolCall'>;
  approval?: EventOf<'ApprovalRequested'>; approved?: boolean; result?: EventOf<'ToolResult'>;
};
export type NodeItem = {
  kind: 'node'; key: string; start?: EventOf<'NodeStarted'>; finish?: EventOf<'NodeFinished'>; items: Item[];
};
export type Item =
  | { kind: 'thinking'; key: string; text: string }
  | { kind: 'interim'; key: string; text: string }
  | ToolItem
  | { kind: 'note'; key: string; message: string; injected: string }
  | { kind: 'error'; key: string; message: string }
  | { kind: 'strip'; key: string }
  | NodeItem;

export type NodeState = 'waiting' | 'running' | 'ok' | 'fail' | 'skip';

/** show_images 가 화면에 붙인 이미지 한 장 (바이트는 서버의 실행 폴더에 있고 주소로 받는다) */
export type ImageEntry = EventOf<'Images'>['items'][number];

export interface TurnModel {
  runId: string;
  task: string;
  workdir: string;
  workflow?: { name: string; nodes: { id: string; title: string; profile: string; after: string[] }[]; state: Record<string, NodeState> };
  items: Item[];
  images: ImageEntry[];
  final?: EventOf<'Final'>;
}

/** 이미지 주소 — 실행 id 와 번호뿐 (인증 정보 없음). 서버는 받아 둔 파일만 내보낸다. */
export const imageUrl = (runId: string, id: number, size: 'thumb' | 'view' | 'original') =>
  `/api/v1/runs/${encodeURIComponent(runId)}/images/${id}/${size}`;

export const nodeOk = (status: string) => status === 'done' || status === 'partial' || status === 'reused';

export function buildTurn(events: RunEvent[]): TurnModel {
  const model: TurnModel = { runId: '', task: '', workdir: '', items: [], images: [] };
  const tools = new Map<string, ToolItem>();
  let target = model.items;
  let node: NodeItem | null = null;
  events.forEach((ev, i) => {
    const key = `${ev.type}-${ev.seq ?? i}`;
    switch (ev.type) {
      case 'RunStarted':
        Object.assign(model, { runId: ev.run_id, task: ev.task, workdir: ev.workdir ?? '' });
        break;
      case 'WorkflowStarted':
        model.workflow = { name: ev.name, nodes: (ev.nodes ?? []) as NonNullable<TurnModel['workflow']>['nodes'], state: {} };
        model.items.push({ kind: 'strip', key });
        break;
      case 'NodeStarted':
        node = { kind: 'node', key, start: ev, items: [] };
        model.items.push(node);
        target = node.items;
        if (model.workflow) model.workflow.state[ev.node] = 'running';
        break;
      case 'NodeFinished': {
        if (node && node.start?.node === ev.node) node.finish = ev;
        else model.items.push({ kind: 'node', key, finish: ev, items: [] });   // 건너뜀·이전 결과 재사용은 시작 없이 끝만
        node = null;
        target = model.items;
        if (model.workflow) model.workflow.state[ev.node] = nodeOk(ev.status) ? 'ok' : ev.status === 'skipped' ? 'skip' : 'fail';
        break;
      }
      case 'LLMCall':
        if (ev.reasoning) target.push({ kind: 'thinking', key: `${key}-r`, text: ev.reasoning });
        if (ev.tool_calls?.length && ev.content) target.push({ kind: 'interim', key: `${key}-c`, text: ev.content });
        break;
      case 'ToolCall': {
        const item: ToolItem = { kind: 'tool', key, call: ev };
        tools.set(ev.call_id, item);
        target.push(item);
        break;
      }
      case 'ApprovalRequested': {
        const t = tools.get(ev.call_id);
        if (t) t.approval = ev;
        break;
      }
      case 'ApprovalResolved': {
        const t = tools.get(ev.call_id);
        if (t) t.approved = ev.approved;
        break;
      }
      case 'ToolResult': {
        const t = tools.get(ev.call_id);
        if (t) t.result = ev;
        break;
      }
      case 'Note':
        target.push({ kind: 'note', key, message: ev.message, injected: ev.injected ?? '' });
        break;
      case 'Images':
        model.images.push(...(ev.items ?? []));
        break;
      case 'Error':
        target.push({ kind: 'error', key, message: ev.message });
        break;
      case 'Final':
        model.final = ev;
        break;
    }
  });
  model.images = collapseImages(model.images);
  return model;
}

/** 같은 이미지를 여러 경로로 시도했으면 하나만: 한 번이라도 표시됐으면 그 이름의 실패 칸은 빼고, 실패만 있으면 마지막 것만.
 *  (모델이 잘못된 경로로 먼저 시도한 실패가 '찾을 수 없음'으로 남아 같은 이미지가 없는 것처럼 보이지 않게) */
export function collapseImages(images: ImageEntry[]): ImageEntry[] {
  const shown = new Set(images.filter((im) => im.ok).map((im) => im.label));
  const lastFail = new Map<string, number>();
  images.forEach((im, i) => { if (!im.ok) lastFail.set(im.label, i); });
  return images.filter((im, i) => im.ok || (!shown.has(im.label) && lastFail.get(im.label) === i));
}

/** 끝났지만 '완료'가 아닐 때 보여 줄 안내 (워크플로는 따로) */
export function finalNotice(status: string, steps: number, workflow: boolean): string {
  if (workflow) {
    if (status === 'partial') return '일부 노드가 끝나지 못했습니다. 노드 결과를 보고 다시 시키거나, 그 노드만 해당 용도로 이어서 시키세요.';
    if (status === 'cancelled') return "중지했습니다. '멈춘 노드부터 계속'을 누르면 끝난 노드는 그대로 두고 이어서 합니다.";
    return `노드가 멈췄습니다 (${status}). '멈춘 노드부터 계속'을 누르면 끝난 노드는 다시 돌리지 않고, 멈춘 노드가 폴더에 남은 것부터 이어서 합니다.`;
  }
  return ({
    partial: '부분 완료로 보고했습니다. 보고의 남은 항목·확인 못 한 것을 보고 이어서 시키세요.',
    failed: '실패로 보고했습니다. 보고에 적힌 원인을 보고 방법을 바꿔 다시 시켜 보세요.',
    no_action: '계획만 말하고 실제로는 아무것도 실행하지 않았습니다. "바로 실행해줘" 처럼 다시 시켜 보세요.',
    max_steps: `스텝 한도(${steps})에 도달해 멈췄습니다. 입력창에서 스텝을 늘리거나 '계속하기'를 누르세요.`,
    timeout: '시간 한도에 도달해 멈췄습니다.',
    loop: '같은 시도를 반복해서 멈췄습니다. 힌트를 적어 보내면 이어서 합니다.',
    cancelled: '중지했습니다.',
    error: '오류로 멈췄습니다. 위 오류 내용을 확인하세요 — 로컬 모델이면 상태 · 연결의 모델 서버, 외부 모델이면 관리 › 외부 AI 모델의 연결(키 · 결제).',
  } as Record<string, string>)[status] ?? status;
}

export const STUCK = ['max_steps', 'timeout', 'loop', 'cancelled', 'partial', 'no_action'];
