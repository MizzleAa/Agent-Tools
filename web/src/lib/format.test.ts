import { describe, expect, it } from 'vitest';
import { estTokens, fmtArgs, toolSummary } from './format';

describe('format', () => {
  it('긴 문자열 인자는 이스케이프 없이', () => {
    expect(fmtArgs({ path: 'a.py', content: 'x\ny' })).toBe('path: a.py\ncontent:\nx\ny');
    expect(fmtArgs({ items: [1] })).toBe('{\n  "items": [\n    1\n  ]\n}');
  });
  it('도구 한 줄 요약', () => {
    expect(toolSummary('api_get', { api: 'mes', path: '/lots/1' })).toBe('mes/lots/1');
    expect(toolSummary('run_command', { command: 'dir\nmore' })).toBe('dir');
  });
  it('토큰 어림은 서버와 같은 식', () => {
    expect(estTokens('가나다abc')).toBe(Math.round(3 * 0.9 + 3 * 0.3));
  });
});
