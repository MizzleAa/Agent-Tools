// 글자 다듬기 (순수 함수 — format.test.ts)

export const fmtTime = (iso?: string | null) => (iso ? iso.slice(5, 16).replace('T', ' ') : '');

/** 도구 인자를 사람이 읽기 좋게. 코드·본문처럼 긴 문자열 인자는 JSON 이스케이프 없이 그대로 */
export function fmtArgs(args: Record<string, unknown> | undefined): string {
  const entries = Object.entries(args ?? {});
  if (entries.length && entries.every(([, v]) => typeof v !== 'object')) {
    return entries.map(([k, v]) => (String(v).includes('\n') ? `${k}:\n${v}` : `${k}: ${v}`)).join('\n');
  }
  return JSON.stringify(args, null, 2);
}

/** 도구 호출 한 줄 요약 (접힌 칸 제목) */
export function toolSummary(name: string, args: Record<string, unknown> = {}): string {
  if (name === 'api_get' || name === 'api_send') return `${args.method ? `${args.method} ` : ''}${args.api ?? ''}${args.path ?? ''}`;
  for (const k of ['command', 'path', 'url', 'host', 'query', 'name', 'name_pattern', 'code']) {
    const v = args[k];
    if (typeof v === 'string' && v) return v.split('\n')[0].slice(0, 140);
  }
  return '';
}

/** 토큰 어림 (서버 estimate_tokens 와 같은 식: 한글 0.9, 나머지 0.3) */
export function estTokens(text: string): number {
  let hangul = 0;
  for (const ch of text) if (ch >= '가' && ch <= '힣') hangul++;
  return Math.round(hangul * 0.9 + (text.length - hangul) * 0.3);
}

export const sizeText = (text: string) => `${text.length.toLocaleString()}자 · 약 ${estTokens(text).toLocaleString()}토큰`;

export const firstLine = (text: string) => text.split('\n')[0];

export function greeting(hour = new Date().getHours()): string {
  return hour < 5 ? '늦은 시간까지 수고 많으세요' : hour < 11 ? '좋은 아침이에요' : hour < 17 ? '좋은 오후예요' : '좋은 저녁이에요';
}
