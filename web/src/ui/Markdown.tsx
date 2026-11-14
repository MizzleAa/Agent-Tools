import { Fragment, useMemo } from 'react';
import { tokenize } from '@/features/files/highlight';
import { type Block, type Inline, parseInline, parseMarkdown } from '@/lib/markdown';
import { CopyButton } from './CopyButton';

function Inlines({ text }: { text: string }) {
  return <>{parseInline(text).map((n, i) => <InlineNode key={i} node={n} />)}</>;
}

function InlineNode({ node }: { node: Inline }) {
  switch (node.t) {
    case 'text': return <>{node.v}</>;
    case 'code': return <code className="inline">{node.v}</code>;
    case 'strong': return <strong>{node.c.map((c, i) => <InlineNode key={i} node={c} />)}</strong>;
    case 'em': return <em>{node.v}</em>;
    case 'link': return <a href={node.href} target="_blank" rel="noopener noreferrer">{node.label}</a>;
  }
}

// 코드 블록 언어 이름 → features/files/highlight 의 언어 (모르는 언어는 색 없이)
const LANG_ALIAS: Record<string, string> = { py: 'python', js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  sh: 'shell', bash: 'shell', zsh: 'shell', console: 'shell', ps1: 'powershell', pwsh: 'powershell', cs: 'csharp', rs: 'rust', yml: 'yaml' };

export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const lines = useMemo(() => tokenize(code, LANG_ALIAS[(lang ?? '').toLowerCase()] ?? (lang ?? '').toLowerCase()), [code, lang]);
  return (
    <div className="codeblock">
      <div className="codeblock-head"><span>{lang || '코드'}</span><CopyButton text={code} /></div>
      <pre><code>{lines.map((toks, i) => (
        <Fragment key={i}>{toks.map((t, j) => (t.k === 'txt' ? t.v : <span key={j} className={`t-${t.k}`}>{t.v}</span>))}{i < lines.length - 1 ? '\n' : ''}</Fragment>
      ))}</code></pre>
    </div>
  );
}

function BlockNode({ block }: { block: Block }) {
  switch (block.t) {
    case 'code': return <CodeBlock code={block.code} lang={block.lang} />;
    case 'h': {
      const Tag = `h${Math.min(block.level + 1, 5)}` as 'h2';
      return <Tag><Inlines text={block.text} /></Tag>;
    }
    case 'hr': return <hr />;
    case 'table':
      return (
        <table>
          <thead><tr>{block.header.map((c, i) => <th key={i}><Inlines text={c} /></th>)}</tr></thead>
          <tbody>{block.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}><Inlines text={c} /></td>)}</tr>)}</tbody>
        </table>
      );
    case 'quote': return <blockquote>{block.blocks.map((b, i) => <BlockNode key={i} block={b} />)}</blockquote>;
    case 'list': {
      const items = block.items.map((it, i) => (
        <li key={i} className={it.sub ? 'sub' : undefined}>
          {it.lines.map((l, k) => <Fragment key={k}>{k > 0 && <br />}<Inlines text={l} /></Fragment>)}
        </li>
      ));
      return block.ordered ? <ol start={block.start}>{items}</ol> : <ul>{items}</ul>;
    }
    case 'p':
      return <p>{block.lines.map((l, k) => <Fragment key={k}>{k > 0 && <br />}<Inlines text={l} /></Fragment>)}</p>;
  }
}

/** 마크다운 → 화면. 모델 출력에 HTML 이 섞여 있어도 글자로만 보인다. */
export function Markdown({ text, className = '' }: { text: string; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(text), [text]);
  return <div className={`md ${className}`}>{blocks.map((b, i) => <BlockNode key={i} block={b} />)}</div>;
}
