// 파일 탐색기 (기능 '공유 경로·파일 탐색' B1~B7): 위치 목록 → 폴더 목록(나눠 받기·정렬·찾기) → 파일 미리보기.
//   /explorer                       위치 고르기
//   /explorer/<위치>?path=…&file=…  그 위치의 폴더(path)와 고른 파일(file)
// 위치 = 작업 폴더 + 사용자가 허용한 공유 위치 (읽기 전용). 이 밖은 서버가 거절한다 (화면은 받은 것만 그린다).
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { useTitle } from '@/state/title';
import { CopyButton } from '@/ui/CopyButton';
import { Icon } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import { useUi } from '@/ui/UiProvider';
import { tokenize } from './highlight';

type Entry = Schemas['DirEntry'];
type SortKey = 'name' | 'size' | 'modified' | 'type';
const PAGE = 200;

const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB`
  : n < 1024 ** 3 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${(n / 1024 ** 3).toFixed(2)} GB`);
const join = (a: string, b: string) => (a ? `${a}/${b}` : b);
// 긴 공유 경로(\\서버\공유\…\폴더)는 마지막 폴더 이름으로 부르고, 전체 경로는 아래에 작게 (끝이 보이게) 둔다
const tail = (p: string) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;
const locName = (l: Schemas['LocationInfo']) => (l.kind === 'workspace' ? '작업 폴더' : tail(l.label));
const parent = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');

export function ExplorerView() {
  useTitle('파일 탐색기');
  const { loc } = useParams();
  const [params, setParams] = useSearchParams();
  const folder = params.get('path') ?? '';
  const file = params.get('file') ?? '';
  const navigate = useNavigate();
  const ui = useUi();
  const [locs, setLocs] = useState<Schemas['LocationInfo'][] | null>(null);
  const [adding, setAdding] = useState('');

  const loadLocs = () => api.locations.list().then(setLocs, ui.error);
  useEffect(() => { loadLocs(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const current = locs?.find((l) => l.id === loc);

  const add = async () => {
    const path = adding.trim();
    if (!path) return;
    if (!(await ui.confirm({ title: '이 위치를 허용할까요?', message: `${path}\n\n그 폴더와 그 아래를 읽기 전용으로 허용합니다 (이 PC 기준 경로). 채팅의 모든 용도와 이 화면에서 읽을 수 있게 됩니다.`, confirmText: '허용' }))) return;
    try {
      const l = await api.locations.add(path);
      setAdding('');
      await loadLocs();
      navigate(`/explorer/${l.id}`);
    } catch (e) { ui.error(e); }
  };
  const work = (target: string) => navigate('/', { state: { draft: `${target} ` } });

  return (
    <section className="view active">
      <div className="files-layout explorer-layout">
        <aside className="files-col files-folders">
          <div className="files-head">
            <b>위치</b><span className="spacer" />
            <button type="button" className="mini-btn" onClick={loadLocs} title="다시 읽기">새로고침</button>
          </div>
          <div className="files-scroll">
            {locs === null && <div className="muted small">불러오는 중…</div>}
            {locs?.map((l) => (
              <button key={l.id} type="button" className={`list-item${l.id === loc ? ' active' : ''}`} title={l.path}
                onClick={() => navigate(`/explorer/${l.id}`)}>
                <span className="list-title">{locName(l)}</span>
                {l.kind !== 'workspace' && <span className="muted small explorer-locpath"><bdi>{l.path}</bdi></span>}
                <span className="muted small">{l.kind === 'workspace' ? '읽기·쓰기' : `공유 · 읽기 전용${l.has_account ? ' · 계정 있음' : ''}`}</span>
              </button>
            ))}
            {locs && locs.length === 1 && <div className="muted small explorer-hint">허용한 공유 위치가 아직 없습니다. 채팅에 공유 경로를 주면 처음 한 번 허용을 묻고, 아래에서 직접 더할 수도 있습니다.</div>}
          </div>
          <div className="explorer-add">
            <input type="text" placeholder="\\서버\공유\폴더 · Z:\폴더" value={adding} onChange={(e) => setAdding(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') add(); }} aria-label="공유 위치 경로" />
            <button type="button" className="btn" onClick={add} disabled={!adding.trim()}>위치 허용</button>
            <div className="muted small">허용한 위치는 '상태 · 연결' › 공유 위치에서 지우고 계정을 넣습니다.</div>
          </div>
        </aside>
        <div className="files-col explorer-list">
          {current ? <FolderPane key={current.id} loc={current} folder={folder} selected={file}
            onFolder={(p) => setParams(p ? { path: p } : {})} onFile={(p) => setParams({ path: folder, file: p })} onWork={work} />
            : <div className="files-empty muted">{loc ? '허용된 위치가 아닙니다.' : '왼쪽에서 위치를 고르세요.'}</div>}
        </div>
        <div className="files-col files-view">
          {current && file ? <Preview key={`${current.id}/${file}`} loc={current} path={file} onWork={work} />
            : <div className="files-empty muted">{current ? '파일을 고르면 내용을 봅니다.' : '작업 폴더와 허용한 공유 위치의 파일을 봅니다.'}</div>}
        </div>
      </div>
    </section>
  );
}

function FolderPane({ loc, folder, selected, onFolder, onFile, onWork }: {
  loc: Schemas['LocationInfo']; folder: string; selected: string;
  onFolder: (p: string) => void; onFile: (p: string) => void; onWork: (target: string) => void;
}) {
  const ui = useUi();
  const [sort, setSort] = useState<SortKey>('name');
  const [order, setOrder] = useState<'asc' | 'desc'>('asc');
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [listing, setListing] = useState<Schemas['DirListing'] | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [error, setError] = useState('');
  const seq = useRef(0);

  useEffect(() => { const t = setTimeout(() => setQuery(q), 300); return () => clearTimeout(t); }, [q]);
  useEffect(() => {
    const mine = ++seq.current;
    setListing(null); setEntries([]); setError('');
    api.locations.folder(loc.id, { path: folder, offset: 0, limit: PAGE, sort, order, q: query })
      .then((l) => { if (mine === seq.current) { setListing(l); setEntries(l.entries); } },
        (e) => { if (mine === seq.current) setError(e instanceof Error ? e.message : String(e)); });
  }, [loc.id, folder, sort, order, query]);
  const more = () => listing && api.locations.folder(loc.id, { path: folder, offset: entries.length, limit: PAGE, sort, order, q: query })
    .then((l) => setEntries((xs) => [...xs, ...l.entries]), ui.error);
  const by = (k: SortKey) => () => { if (k === sort) setOrder(order === 'asc' ? 'desc' : 'asc'); else { setSort(k); setOrder(k === 'name' || k === 'type' ? 'asc' : 'desc'); } };
  const mark = (k: SortKey) => (k === sort ? (order === 'asc' ? ' ▲' : ' ▼') : '');
  const crumbs = folder ? folder.split('/') : [];

  return (
    <>
      <div className="files-head explorer-crumbs">
        <button type="button" className="icon-btn" title="위 폴더" disabled={!folder} onClick={() => onFolder(parent(folder))}>↑</button>
        <button type="button" className="crumb" onClick={() => onFolder('')} title={loc.path}>{locName(loc)}</button>
        {crumbs.map((c, i) => (
          <span key={i} className="crumb-wrap"><span className="muted">/</span>
            <button type="button" className="crumb" onClick={() => onFolder(crumbs.slice(0, i + 1).join('/'))}>{c}</button></span>
        ))}
      </div>
      <div className="explorer-tools">
        <input type="text" className="files-filter" placeholder="이름으로 찾기" value={q} onChange={(e) => setQ(e.target.value)} />
        {listing && <CopyButton text={listing.abs_path} label="경로 복사" />}
        {listing && <button type="button" className="mini-btn" onClick={() => onWork(listing.abs_path)} title="이 폴더를 채팅 작업 대상으로">이 폴더로 작업 시키기</button>}
      </div>
      {listing && <div className="muted small explorer-count">폴더 {listing.folders}개 · 파일 {listing.files}개{query ? ` (‘${query}’ 찾음)` : ''} · {entries.length}/{listing.total} 표시</div>}
      <div className="files-scroll">
        {error && <div className="notice error"><span className="msg">{error}</span></div>}
        {!listing && !error && <div className="muted small">불러오는 중…</div>}
        {listing && (
          <table className="explorer-table">
            <thead>
              <tr>
                <th><button type="button" onClick={by('name')}>이름{mark('name')}</button></th>
                <th className="num"><button type="button" onClick={by('size')}>크기{mark('size')}</button></th>
                <th><button type="button" onClick={by('modified')}>수정일{mark('modified')}</button></th>
                <th><button type="button" onClick={by('type')}>종류{mark('type')}</button></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const rel = join(folder, e.name);
                return (
                  <tr key={e.name} className={selected === rel ? 'active' : ''}>
                    <td>
                      <button type="button" className="explorer-name" onClick={() => (e.dir ? onFolder(rel) : onFile(rel))} title={e.name}>
                        {e.dir ? <Icon name="folder" size={14} /> : <span className="explorer-file" />}<span>{e.name}</span>
                      </button>
                    </td>
                    <td className="num muted">{e.dir ? '' : fmtSize(e.size)}</td>
                    <td className="muted">{e.modified.slice(0, 16).replace('T', ' ')}</td>
                    <td className="muted">{e.dir ? '폴더' : e.type || '-'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {listing && entries.length < listing.total && (
          <button type="button" className="btn explorer-more" onClick={more}>더 보기 (남은 {listing.total - entries.length}개)</button>
        )}
      </div>
    </>
  );
}

function Preview({ loc, path, onWork }: { loc: Schemas['LocationInfo']; path: string; onWork: (target: string) => void }) {
  const [file, setFile] = useState<Schemas['FilePreview'] | null>(null);
  const [error, setError] = useState('');
  const [wrap, setWrap] = useState(false);
  useEffect(() => {
    setFile(null); setError('');
    api.locations.file(loc.id, path).then(setFile, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [loc.id, path]);
  const lines = useMemo(() => (file?.kind === 'text' && file.text != null && file.language !== 'markdown'
    ? tokenize(file.text, file.language) : []), [file]);
  if (error) return <div className="files-empty"><div className="notice error"><span className="msg">{error}</span></div></div>;
  if (!file) return <div className="files-empty muted">불러오는 중…</div>;
  const head = file.rows[0] ?? [];
  return (
    <>
      <div className="files-head code-head explorer-filehead">
        <span className="code-path" title={file.abs_path}>{file.name}</span>
        <span className="muted small">{fmtSize(file.size)} · {file.modified.slice(0, 16).replace('T', ' ')}{file.note ? ` · ${file.note}` : ''}</span>
        <span className="spacer" />
        <CopyButton text={file.abs_path} label="경로 복사" />
        <button type="button" className="mini-btn" onClick={() => onWork(file.abs_path)} title="이 파일을 채팅 작업 대상으로">이 파일로 작업 시키기</button>
        {file.kind === 'text' && <button type="button" className="mini-btn" onClick={() => setWrap(!wrap)}>{wrap ? '줄바꿈 끄기' : '줄바꿈'}</button>}
      </div>
      <div className="files-scroll code-scroll">
        {file.kind === 'image' && (
          <div className="explorer-image"><img src={api.locations.rawUrl(loc.id, path)} alt={file.name} />
            <div className="muted small">{file.width}×{file.height}</div></div>
        )}
        {file.kind === 'table' && (
          <div className="explorer-tablewrap">
            {file.sheets.length > 1 && <div className="muted small">시트: {file.sheets.join(', ')} (첫 시트를 보여 줍니다 — 전체 내용은 아래 글)</div>}
            <table className="explorer-table data">
              <thead><tr>{head.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
              <tbody>{file.rows.slice(1).map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
            </table>
            {file.rows.length >= 500 && <div className="muted small">앞 500행만 보여 줍니다.</div>}
          </div>
        )}
        {file.kind === 'document' && <pre className="explorer-doc">{file.text}</pre>}
        {file.kind === 'text' && file.language === 'markdown' && <div className="code-md"><Markdown text={file.text ?? ''} /></div>}
        {file.kind === 'text' && file.language !== 'markdown' && (
          <div className={`code-view${wrap ? ' wrap' : ''}`}>
            {lines.map((toks, i) => (
              <div key={i} className="code-line">
                <span className="ln">{i + 1}</span>
                <span className="lc">{toks.length ? toks.map((t, j) => <span key={j} className={`t-${t.k}`}>{t.v}</span>) : ' '}</span>
              </div>
            ))}
          </div>
        )}
        {(file.kind === 'binary' || file.kind === 'too_big') && (
          <div className="files-empty muted">미리보기 불가 — {file.note || '이 형식은 화면에서 볼 수 없습니다'} ({fmtSize(file.size)}, {file.name.split('.').pop()})</div>
        )}
      </div>
    </>
  );
}
