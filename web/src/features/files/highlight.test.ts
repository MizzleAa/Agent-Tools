import { describe, expect, it } from 'vitest';
import { tokenize } from './highlight';

const join = (lines: ReturnType<typeof tokenize>) => lines.map((l) => l.map((t) => t.v).join('')).join('\n');

describe('tokenize', () => {
  it('글자를 바꾸지 않고 줄 수를 지킨다', () => {
    const src = 'def f(x):\n    # 주석\n    return "a\\"b" + 12\n\n';
    const lines = tokenize(src, 'python');
    expect(join(lines)).toBe(src);
    expect(lines.length).toBe(src.split('\n').length);
  });

  it('키워드·주석·문자열·숫자를 나눈다', () => {
    const [line] = tokenize('const a = 1; // 끝', 'javascript');
    const kinds = Object.fromEntries(line.filter((t) => t.k !== 'txt').map((t) => [t.v, t.k]));
    expect(kinds).toEqual({ const: 'kw', 1: 'num', '// 끝': 'com' });
  });

  it('여러 줄 주석·문자열은 줄마다 같은 종류로 이어진다', () => {
    const lines = tokenize('/* a\nb */ x', 'csharp');
    expect(lines[0]).toEqual([{ k: 'com', v: '/* a' }]);
    expect(lines[1][0]).toEqual({ k: 'com', v: 'b */' });
  });

  it('모르는 언어는 색 없이 그대로', () => {
    const lines = tokenize('<a href="x">\n</a>', 'html');
    expect(lines.every((l) => l.every((t) => t.k === 'txt'))).toBe(true);
    expect(join(lines)).toBe('<a href="x">\n</a>');
  });
});
