// 작업 폴더: 대화마다 생긴 폴더 목록 → 폴더 안 파일 트리 → 코드 보기 (읽기 전용)
//   /files                 폴더 목록
//   /files/<폴더>?path=…   그 폴더의 파일 하나
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { useTitle } from '@/state/title';
import { CopyButton } from '@/ui/CopyButton';
import { Icon } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import { useUi } from '@/ui/UiProvider';
import { tokenize } from './highlight';

type Entry = Schemas['FileEntry'];
interface Node { name: string; path: string; dir: boolean; size: number; skipped: boolean; children: Node[] }

function buildTree(entries: Entry[]): Node[] {
  const root: Node = { name: '', path: '', dir: true, size: 0, skipped: false, children: [] };
  const dirs = new Map<string, Node>([['', root]]);
  for (const e of entries) {
    const parent = e.path.includes('/') ? e.path.slice(0, e.path.lastIndexOf('/')) : '';
    const node: Node = { name: e.path.split('/').pop()!, path: e.path, dir: e.dir, size: e.size, skipped: e.skipped, children: [] };
    (dirs.get(parent) ?? root).children.push(node);
    if (e.dir) dirs.set(e.path, node);
  }
  const sort = (n: Node) => { n.children.sort((a, b) => (Number(b.dir) - Number(a.dir)) || a.name.localeCompare(b.name)); n.children.forEach(sort); };
  sort(root);
  return root.children;
}

const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

export function FilesView() {
  useTitle('작업 폴더');
  const { folder } = useParams();
  const [params, setParams] = useSearchParams();
  const path = params.get('path') ?? '';
  const navigate = useNavigate();
  const ui = useUi();
  const [folders, setFolders] = useState<Schemas['FolderInfo'][] | null>(null);
  const [filter, setFilter] = useState('');
  const [tree, setTree] = useState<Schemas['FolderTree'] | null>(null);

  const loadFolders = () => api.workspace.folders().then(setFolders, ui.error);
  useEffect(() => { loadFolders(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setTree(null);
    if (!folder) return;
    api.workspace.tree(folder).then((t) => {
      setTree(t);
      if (!params.get('path')) {   // 처음 열면 작업기록부터
        const log = t.entries.find((e) => !e.dir && e.path === '작업기록.md');
        if (log) setParams({ path: log.path }, { replace: true });
      }
    }, ui.error);
  }, [folder]);   // eslint-disable-line react-hooks/exhaustive-deps

  const shown = (folders ?? []).filter((f) => !filter || `${f.title} ${f.name}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <section className="view active">
      <div className="files-layout">
        <aside className="files-col files-folders">
          <div className="files-head">
            <b>작업 폴더</b><span className="spacer" />
            <button type="button" className="mini-btn" onClick={loadFolders} title="다시 읽기">새로고침</button>
          </div>
          <input type="text" className="files-filter" placeholder="폴더 찾기" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="files-scroll">
            {folders === null && <div className="muted small">불러오는 중…</div>}
            {folders?.length === 0 && <div className="muted small">아직 작업 폴더가 없습니다.</div>}
            {shown.map((f) => (
              <button key={f.name} type="button" className={`list-item${f.name === folder ? ' active' : ''}`} title={f.name}
                onClick={() => navigate(`/files/${encodeURIComponent(f.name)}`)}>
                <span className="list-title">{f.title}</span>
                <span className="muted small">{f.modified.slice(5, 16).replace('T', ' ')}{f.has_log ? '' : ' · 작업기록 없음'}</span>
              </button>
            ))}
          </div>
        </aside>
        <aside className="files-col files-tree">
          <div className="files-head"><b>파일</b>{tree && <span className="muted small">{tree.entries.filter((e) => !e.dir).length}개</span>}</div>
          <div className="files-scroll">
            {!folder && <div className="muted small">왼쪽에서 폴더를 고르세요.</div>}
            {folder && !tree && <div className="muted small">불러오는 중…</div>}
            {tree && <Tree nodes={buildTree(tree.entries)} selected={path} onOpen={(p) => setParams({ path: p })} />}
            {tree?.truncated && <div className="muted small">파일이 많아 일부만 보여 줍니다.</div>}
          </div>
        </aside>
        <div className="files-col files-view">
          {folder && path ? <FileViewer key={`${folder}/${path}`} folder={folder} path={path} />
            : <div className="files-empty muted">{folder ? '파일을 고르세요.' : '에이전트가 대화마다 만든 작업 폴더와 코드를 여기서 봅니다.'}</div>}
        </div>
      </div>
    </section>
  );
}

function Tree({ nodes, selected, onOpen, depth = 0 }: { nodes: Node[]; selected: string; onOpen: (p: string) => void; depth?: number }) {
  return (
    <ul className="ftree">
      {nodes.map((n) => <TreeNode key={n.path} node={n} selected={selected} onOpen={onOpen} depth={depth} />)}
    </ul>
  );
}

function TreeNode({ node, selected, onOpen, depth }: { node: Node; selected: string; onOpen: (p: string) => void; depth: number }) {
  const [open, setOpen] = useState(() => depth < 1 || selected.startsWith(`${node.path}/`));
  if (!node.dir) {
    return (
      <li>
        <button type="button" className={`ftree-item${selected === node.path ? ' active' : ''}`} style={{ paddingLeft: 8 + depth * 14 }}
          onClick={() => onOpen(node.path)} title={`${node.path} · ${fmtSize(node.size)}`}>
          <span className="ftree-name">{node.name}</span><span className="muted small">{fmtSize(node.size)}</span>
        </button>
      </li>
    );
  }
  return (
    <li>
      <button type="button" className={`ftree-item dir${node.skipped ? ' skipped' : ''}`} style={{ paddingLeft: 8 + depth * 14 }}
        disabled={node.skipped} title={node.skipped ? '가상환경·패키지 폴더라 펼치지 않습니다' : node.path} onClick={() => setOpen(!open)}>
        {!node.skipped && <Icon name="chevron" size={12} className={`chev${open ? ' open' : ''}`} />}
        <Icon name="folder" size={14} /><span className="ftree-name">{node.name}</span>
        {node.skipped && <span className="muted small">생략</span>}
      </button>
      {open && !node.skipped && node.children.length > 0 && <Tree nodes={node.children} selected={selected} onOpen={onOpen} depth={depth + 1} />}
    </li>
  );
}

function FileViewer({ folder, path }: { folder: string; path: string }) {
  const ui = useUi();
  const [file, setFile] = useState<Schemas['FileContent'] | null>(null);
  const [wrap, setWrap] = useState(false);
  const [raw, setRaw] = useState(false);
  useEffect(() => {
    setFile(null);
    api.workspace.file(folder, path).then(setFile, ui.error);
  }, [folder, path, ui]);
  const lines = useMemo(() => (file?.text != null ? tokenize(file.text, file.language) : []), [file]);
  if (!file) return <div className="files-empty muted">불러오는 중…</div>;
  const markdown = file.language === 'markdown' && !raw;
  return (
    <>
      <div className="files-head code-head">
        <span className="code-path" title={file.path}>{file.path}</span>
        <span className="muted small">{fmtSize(file.size)} · {file.language}{file.truncated ? ' · 앞 512KB 만' : ''}</span>
        <span className="spacer" />
        {file.language === 'markdown' && <button type="button" className="mini-btn" onClick={() => setRaw(!raw)}>{raw ? '보기 좋게' : '원문'}</button>}
        {!markdown && <button type="button" className="mini-btn" onClick={() => setWrap(!wrap)}>{wrap ? '줄바꿈 끄기' : '줄바꿈'}</button>}
        {file.text != null && <CopyButton text={file.text} />}
      </div>
      <div className="files-scroll code-scroll">
        {file.binary ? <div className="files-empty muted">이진 파일이라 내용을 보여 주지 않습니다.</div>
          : markdown ? <div className="code-md"><Markdown text={file.text ?? ''} /></div>
            : (
              <div className={`code-view${wrap ? ' wrap' : ''}`}>
                {lines.map((toks, i) => (
                  <div key={i} className="code-line">
                    <span className="ln">{i + 1}</span>
                    <span className="lc">{toks.length ? toks.map((t, j) => <span key={j} className={`t-${t.k}`}>{t.v}</span>) : ' '}</span>
                  </div>
                ))}
              </div>
            )}
      </div>
    </>
  );
}
