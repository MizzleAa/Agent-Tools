import { describe, expect, it } from 'vitest';
import { nestItems, parseInline, parseMarkdown } from './markdown';

describe('parseMarkdown', () => {
  it('제목·목록·표·코드·인용·문단을 나눈다', () => {
    const md = '## 결과: 완료\n\n- 하나\n  이어지는 줄\n- 둘\n\n| # | 요구 |\n|---|---|\n| 1 | 저장 |\n\n```py\nprint(1)\n```\n\n> 인용\n\n끝 문단';
    expect(parseMarkdown(md).map((b) => b.t)).toEqual(['h', 'list', 'table', 'code', 'quote', 'p']);
    const list = parseMarkdown(md)[1];
    expect(list.t === 'list' && list.items[0].lines).toEqual(['하나', '이어지는 줄']);
    const table = parseMarkdown(md)[2];
    expect(table.t === 'table' && table.rows).toEqual([['1', '저장']]);
  });

  it('HTML 은 해석하지 않고 글자 그대로 둔다', () => {
    const [p] = parseMarkdown('<img src=x onerror=alert(1)>');
    expect(p).toEqual({ t: 'p', lines: ['<img src=x onerror=alert(1)>'] });
  });
});

describe('parseInline', () => {
  it('코드·굵게·링크·주소·기울임', () => {
    expect(parseInline('`a` **b** [c](https://x.y) https://z.w *e*').map((n) => n.t))
      .toEqual(['code', 'text', 'strong', 'text', 'link', 'text', 'link', 'text', 'em']);
  });
  it('번호 목록 안의 하위 번호 목록은 바깥 번호를 이어받지 않고 1 부터 (위 항목 아래로 묶는다)', () => {
    const md = '3. **E-221 조치 순서:**\n   1. 밸브 확인\n   2. 필터 확인\n4. 다음 질문';
    const list = parseMarkdown(md)[0];
    expect(list.t === 'list' && list.start).toBe(3);
    const nested = list.t === 'list' ? nestItems(list.items) : [];
    expect(nested.map((n) => [n.item.lines[0], n.subs.map((s) => [s.num, s.lines[0]])])).toEqual([
      ['**E-221 조치 순서:**', [[1, '밸브 확인'], [2, '필터 확인']]], ['다음 질문', []]]);
    expect(nested[0].subs[0].ordered).toBe(true);
    expect(nestItems([{ sub: true, lines: ['a'] }]).length).toBe(1);   // 하위 항목으로 시작하면 그대로
  });
  it('javascript: 같은 주소는 링크로 만들지 않는다', () => {
    expect(parseInline('[x](javascript:alert(1))').every((n) => n.t === 'text')).toBe(true);
  });
});
