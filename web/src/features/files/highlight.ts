// 가벼운 코드 색 구분 (주석 · 문자열 · 숫자 · 키워드). 글자를 바꾸지 않고 조각으로만 나눈다 → React 로 그려 안전하다.
export type TokKind = 'txt' | 'kw' | 'str' | 'com' | 'num';
export interface Tok { k: TokKind; v: string }

const WORDS: Record<string, string> = {
  python: 'and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self',
  javascript: 'async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield',
  typescript: 'abstract as async await boolean break case catch class const continue default delete do else enum export extends false finally for from function if implements import in instanceof interface let new null number of private protected public readonly return static string super switch this throw true try type typeof undefined var void while yield',
  csharp: 'abstract as async await base bool break case catch class const continue decimal default delegate do double else enum event false finally float for foreach get if in int interface internal is lock long namespace new null object out override private protected public readonly ref return sealed set static string struct switch this throw true try typeof using var virtual void while',
  java: 'abstract boolean break case catch class continue default do double else enum extends false final finally float for if implements import instanceof int interface long new null package private protected public return static super switch this throw throws true try void while',
  go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map nil package range return select struct switch true false type var',
  rust: 'as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while',
  powershell: 'begin break catch continue do else elseif end exit filter finally for foreach function if in param process return switch throw trap try until while',
  shell: 'case do done elif else esac fi for function if in then until while',
  sql: 'select from where insert into update delete create table drop alter join left right inner outer on group by order having limit as and or not null values set',
};
const KEYWORDS: Record<string, Set<string>> = Object.fromEntries(Object.entries(WORDS).map(([k, v]) => [k, new Set(v.split(' '))]));
KEYWORDS.json = new Set(['true', 'false', 'null']);

const HASH = ['python', 'powershell', 'shell', 'yaml', 'toml', 'ini'];
const SLASH = ['javascript', 'typescript', 'csharp', 'java', 'go', 'rust', 'css', 'json'];

function pattern(lang: string): RegExp {
  const comment = HASH.includes(lang) ? '#[^\\n]*' : SLASH.includes(lang) ? '\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/' : lang === 'sql' ? '--[^\\n]*' : '(?!)';
  const str = lang === 'python'
    ? `"""[\\s\\S]*?"""|'''[\\s\\S]*?'''|"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'`
    : `"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'` + (lang === 'javascript' || lang === 'typescript' ? '|`(?:\\\\.|[^`\\\\])*`' : '');
  return new RegExp(`(${comment})|(${str})|(\\b\\d[\\d_.]*\\b)|([A-Za-z_$][\\w$]*)`, 'g');
}

/** 글자를 줄별 조각으로. 너무 큰 파일·모르는 언어는 색 없이 */
export function tokenize(text: string, lang: string): Tok[][] {
  const lines: Tok[][] = [[]];
  const push = (k: TokKind, v: string) => {
    const parts = v.split('\n');
    parts.forEach((part, i) => {
      if (i > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ k, v: part });
    });
  };
  if (text.length > 300_000 || !(KEYWORDS[lang] || HASH.includes(lang) || SLASH.includes(lang))) {
    push('txt', text);
    return lines;
  }
  const kw = KEYWORDS[lang] ?? new Set<string>();
  const re = pattern(lang);
  let last = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) push('txt', text.slice(last, at));
    if (m[1]) push('com', m[0]);
    else if (m[2]) push('str', m[0]);
    else if (m[3]) push('num', m[0]);
    else push(kw.has(m[0]) ? 'kw' : 'txt', m[0]);
    last = at + m[0].length;
  }
  if (last < text.length) push('txt', text.slice(last));
  return lines;
}
