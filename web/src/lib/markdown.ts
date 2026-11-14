// 작은 마크다운 파서 (모델 답·보고서용). HTML 은 해석하지 않는다 — 결과는 글자 그대로 React 로 그린다 (안전).
// 지원: 제목(#~####), 문단, 목록(-, 1.), 들여쓴 이어지는 줄, 표, 인용, 코드 블록, 가로줄, `코드`, **굵게**, *기울임*, 링크

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; v: string }
  | { t: 'link'; href: string; label: string };

export type Block =
  | { t: 'code'; lang: string; code: string }
  | { t: 'h'; level: number; text: string }
  | { t: 'hr' }
  | { t: 'table'; header: string[]; rows: string[][] }
  | { t: 'quote'; blocks: Block[] }
  | { t: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { t: 'p'; lines: string[] };

/** 목록 항목. sub = 들여쓴 하위 항목 (ordered · num 은 그 항목의 표시 — '1.' 이면 번호 목록의 1) */
export interface ListItem { sub: boolean; lines: string[]; ordered?: boolean; num?: number }

/** 하위 항목을 바로 위 항목 아래로 묶는다 — 번호 목록 안의 하위 번호 목록이 바깥 번호를 이어받지 않게 (1. → 1. 2. 3.) */
export function nestItems(items: ListItem[]): { item: ListItem; subs: ListItem[] }[] {
  const out: { item: ListItem; subs: ListItem[] }[] = [];
  for (const it of items) {
    if (it.sub && out.length && !out[out.length - 1].item.sub) out[out.length - 1].subs.push(it);
    else out.push({ item: it, subs: [] });   // 목록이 하위 항목으로 시작하면 그대로 둔다
  }
  return out;
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\((https?:\/\/[^)\s]+)\))|(https?:\/\/[^\s<>()]*[^\s<>().,!?;:'"])|(\*[^*\s][^*\n]*\*)/g;
const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const FENCE = /^\s*```\s*([\w+#.-]*)/;
const HR = /^\s*(---|\*\*\*|___)\s*$/;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: 'text', v: text.slice(last, at) });
    const tok = m[0];
    if (m[1]) out.push({ t: 'code', v: tok.slice(1, -1) });
    else if (m[2]) out.push({ t: 'strong', c: parseInline(tok.slice(2, -2)) });
    else if (m[3]) out.push({ t: 'link', href: m[4], label: tok.slice(1, tok.indexOf('](')) });
    else if (m[5]) out.push({ t: 'link', href: tok, label: tok });
    else if (m[6]) out.push({ t: 'em', v: tok.slice(1, -1) });
    last = at + tok.length;
  }
  if (last < text.length) out.push({ t: 'text', v: text.slice(last) });
  return out;
}

const blockStart = (l: string) => FENCE.test(l) || /^#{1,4}\s/.test(l) || LIST.test(l) || /^\s*>/.test(l) || HR.test(l);
const cells = (l: string) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());

export function parseMarkdown(src: string): Block[] {
  const lines = (src || '').replace(/\r\n?/g, '\n').split('\n');
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(FENCE);
    if (fence) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push({ t: 'code', lang: fence[1], code: buf.join('\n') });
      continue;
    }
    const head = line.match(/^(#{1,4})\s+(.*)$/);
    if (head) { out.push({ t: 'h', level: head[1].length, text: head[2] }); i++; continue; }
    if (HR.test(line)) { out.push({ t: 'hr' }); i++; continue; }
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const header = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push({ t: 'table', header, rows });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push({ t: 'quote', blocks: parseMarkdown(buf.join('\n')) });
      continue;
    }
    const first = line.match(LIST);
    if (first) {
      const ordered = /\d/.test(first[2]);
      const items: ListItem[] = [];
      while (i < lines.length) {
        const m = lines[i].match(LIST);
        if (m) { items.push({ sub: m[1].length >= 2, lines: [m[3]], ordered: /\d/.test(m[2]), num: parseInt(m[2], 10) || 1 }); i++; continue; }
        if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {   // 들여쓴 이어지는 줄
          items[items.length - 1].lines.push(lines[i].trim());
          i++;
          continue;
        }
        break;
      }
      out.push({ t: 'list', ordered, start: ordered ? parseInt(first[2], 10) : 1, items });
      continue;
    }
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !blockStart(lines[i])) buf.push(lines[i++]);
    out.push({ t: 'p', lines: buf });
  }
  return out;
}
