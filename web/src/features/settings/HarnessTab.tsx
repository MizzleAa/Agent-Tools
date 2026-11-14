// 설정 · 프롬프트 › 하네스: 지침(AGENT.md) · 용도(agents/*.md) · 자료 폴더(사용설명서 · 에러 대처 같은 QA 자료)를 폴더별 목록으로 보고
// 마크다운(위쪽 --- 머리말 --- + 본문)으로 고친다. 폴더마다 '대상 회사'를 정하면 그 회사 사람의 실행에만 그 폴더 자료가 쓰인다.
// 모델이 로컬이든 OpenAI · Claude · Gemini 든 CLI 든 같은 파일이 그대로 적용된다.
// 하네스 여러 개: scope '' = 공통(harness/), 그 밖에는 회사·프로젝트 하네스(harnesses/<이름>/) — 같은 모양으로 고친다.
import { type ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, type Schemas } from '@/api';
import { fmtTime } from '@/lib/format';
import { Badge, Editor, Section } from '@/ui/form';
import { Markdown } from '@/ui/Markdown';
import { Modal } from '@/ui/Modal';
import { useSearchParams } from 'react-router-dom';
import { useUi } from '@/ui/UiProvider';

type Tree = Schemas['HarnessTree'];
type Doc = Schemas['HarnessDoc'];
type Folder = Schemas['HarnessFolder'];

const ROLE_TEMPLATE = (name: string) => `---
name: ${name}
description: 이 용도가 하는 일 (입력창 용도 목록에 보인다)
tools: [list_dir, read_file, search_files]
---

너는 고객사 장비 문의에 답하는 담당자다.
- 장비 오류·사용법은 하네스 자료에서 찾아 읽고 답한다. 자료에 없으면 없다고 말한다.
`;
const DOC_TEMPLATE = (title: string) => `---
title: ${title}
description: 한 줄 설명 (에이전트가 자료 목록에서 보고 고른다)
tags: []
---

# ${title}

## 증상

## 원인

## 조치
1.
`;
const FOLDER_TEMPLATE = `---
companies: []
---

이 폴더 자료에 대한 지침 (예: 이 장비 문의는 먼저 에러 코드를 묻는다).
`;

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',', 2)[1] ?? '');
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export function HarnessTab({ scope = '', info, onChanged, onEdit, onDelete }: {
  scope?: string; info?: Schemas['HarnessInfo']; onChanged?: () => Promise<void> | void;
  onEdit?: () => void; onDelete?: () => void;   // 회사·프로젝트 하네스의 이름·종류·대상 회사 고치기 / 지우기
}) {
  const ui = useUi();
  const [tree, setTree] = useState<Tree | null>(null);
  const [companies, setCompanies] = useState<Schemas['CompanyInfo'][]>([]);
  const [open, setOpen] = useState<string>('');          // 고른 파일
  const [text, setText] = useState('');
  const [saved, setSaved] = useState('');
  const [preview, setPreview] = useState(false);
  const [scopeOf, setScopeOf] = useState<Folder | null>(null);
  const uploadTo = useRef<string>('');
  const [params] = useSearchParams();   // ?open=agents/support.md — 용도 탭에서 '하네스에서 고치기'로 왔을 때
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try { setTree(await api.harness.tree(scope)); } catch (e) { ui.error(e); }
  }, [ui, scope]);
  useEffect(() => { load(); api.admin.companies().then(setCompanies).catch(() => {}); }, [load]);
  useEffect(() => {
    const path = params.get('open');
    if (path) api.harness.file(path, scope).then((f) => { setOpen(f.path); setText(f.text); setSaved(f.text); }).catch(() => {});
  }, [params, scope]);

  const dirty = text !== saved;
  const pick = async (path: string) => {
    if (dirty && !(await ui.confirm({ title: '저장하지 않은 변경이 있습니다', message: '버리고 다른 파일을 열까요?', danger: true, confirmText: '버리고 열기' }))) return;
    try {
      const f = await api.harness.file(path, scope);
      setOpen(f.path); setText(f.text); setSaved(f.text); setPreview(false);
    } catch (e) { ui.error(e); }
  };
  const save = async () => {
    try { await api.harness.save(open, text, scope); setSaved(text); ui.toast('저장했습니다 — 다음 실행부터 적용', 'ok'); load(); onChanged?.(); } catch (e) { ui.error(e); }
  };
  const create = async (path: string, body: string) => {
    if (tree?.files.some((f) => f.path === path)) { await pick(path); return; }
    try { await api.harness.save(path, body, scope); await load(); await pick(path); onChanged?.(); } catch (e) { ui.error(e); }
  };
  const newDoc = async (folder: string) => {
    const v = await ui.form({ title: `'${folder || '맨 위'}' 에 자료 만들기`, confirmText: '만들기',
      fields: [{ name: 'title', label: '제목', placeholder: '예: X100 에러 E-104 노즐 과열' }, { name: 'file', label: '파일 이름', placeholder: '예: E-104 (비우면 제목으로)' }] });
    if (!v?.title.trim()) return;
    const name = (v.file.trim() || v.title.trim()).replace(/[<>:"/\\|?*]/g, '_').replace(/\.md$/i, '');
    create(`${folder ? `${folder}/` : ''}${name}.md`, DOC_TEMPLATE(v.title.trim()));
  };
  const newRole = async () => {
    const v = await ui.form({ title: `새 용도${info ? ` — ${info.title}` : ''}`, confirmText: '만들기',
      message: scope ? '입력창·워크플로 노드에서 이 하네스를 고르면 용도 목록에 이 이름으로 보입니다 (공통 용도와 이름이 같으면 이것이 쓰입니다).'
        : '입력창의 용도 목록에 이 이름으로 보입니다.',
      fields: [{ name: 'name', label: '용도 이름 (영어 소문자·숫자·-·_)', placeholder: '예: support' }] });
    const name = v?.name.trim().toLowerCase();
    if (!name) return;
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)) { ui.toast('용도 이름은 영어 소문자·숫자·-·_ 로 써 주세요', 'error'); return; }
    create(`agents/${name}.md`, ROLE_TEMPLATE(name));
  };
  const newFolder = async (parent = '') => {
    const v = await ui.form({ title: parent ? `'${parent}' 안에 폴더 만들기` : '자료 폴더 만들기', confirmText: '만들기',
      message: '예: 회사별(A사) 또는 장비별(X100) — 폴더 안에 폴더를 또 만들 수 있습니다.', fields: [{ name: 'name', label: '폴더 이름' }] });
    const name = v?.name.trim().replace(/[\\/]+/g, '/');
    if (!name) return;
    try { await api.harness.mkdir(parent ? `${parent}/${name}` : name, scope); load(); } catch (e) { ui.error(e); }
  };
  const remove = async (path: string, folder = false) => {
    if (!(await ui.confirm({ title: `'${path}' 을(를) 지울까요?`, danger: true, confirmText: '지우기',
      message: folder ? '폴더 안의 자료가 모두 지워집니다.' : '되돌릴 수 없습니다.' }))) return;
    try {
      await api.harness.remove(path, scope);
      if (open === path || (folder && open.startsWith(`${path}/`))) { setOpen(''); setText(''); setSaved(''); }
      load(); onChanged?.();
    } catch (e) { ui.error(e); }
  };
  const upload = (folder: string) => { uploadTo.current = folder; fileInput.current?.click(); };
  const onFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = [...(e.target.files ?? [])];
    e.target.value = '';
    if (!files.length) return;
    try {
      const items = await Promise.all(files.map(async (f) => ({ name: f.name, data: await toBase64(f) })));
      const r = await api.harness.upload(uploadTo.current, items, scope);
      ui.toast(`${r.saved.length}개 넣었습니다${r.failed.length ? ` · 실패 ${r.failed.length}: ${r.failed.join(' / ')}` : ''}`, r.failed.length ? 'error' : 'ok', 8000);
      load();
    } catch (err) { ui.error(err); }
  };
  const exportRoles = async () => {
    if (!(await ui.confirm({ title: '기존 용도를 하네스 마크다운으로 옮길까요?', confirmText: '옮기기',
      message: `config/agents 의 용도(${tree?.yaml_roles.join(', ')})를 agents/*.md 로 만듭니다. 이후로는 하네스 파일이 그 용도로 쓰입니다 (같은 이름이면 하네스가 우선 — '설정 · 프롬프트'의 그 용도 편집은 적용되지 않음).` }))) return;
    try { const r = await api.harness.exportRoles(); ui.toast(`${r.written.length}개 옮겼습니다`, 'ok'); load(); } catch (e) { ui.error(e); }
  };

  const groups = useMemo(() => {
    if (!tree) return [];
    const byFolder = new Map<string, Doc[]>();
    tree.files.forEach((f) => byFolder.set(f.folder, [...(byFolder.get(f.folder) ?? []), f]));
    return tree.folders.filter((f) => f.path !== '' && f.path !== 'agents').map((f) => ({ folder: f, files: byFolder.get(f.path) ?? [] }));
  }, [tree]);
  if (!tree) return <div className="muted">불러오는 중…</div>;
  const rootFolder = tree.folders.find((f) => f.path === '');
  const rootAgent = tree.files.find((f) => f.path === 'AGENT.md');
  const roles = tree.files.filter((f) => f.folder === 'agents');
  const rootDocs = tree.files.filter((f) => f.folder === '' && f.path !== 'AGENT.md');
  const current = tree.files.find((f) => f.path === open);
  const body = text.replace(/^---\n[\s\S]*?\n---\n?/, '');

  const fileRow = (f: Doc) => (
    <button key={f.path} type="button" className={`h-file${open === f.path ? ' active' : ''}`} onClick={() => pick(f.path)} title={f.description || f.path}>
      <span className="h-name">{f.kind === 'instructions' ? '📌 지침 (AGENT.md)' : f.title}</span>
      {f.error && <Badge kind="fail">머리말 오류</Badge>}
    </button>
  );
  const scopeChip = (f: Folder) => (f.effective.length
    ? <span className="chip warn" title={f.companies.length ? '이 폴더에서 정함' : '위 폴더에서 물려받음'}>대상: {f.effective.join(', ')}</span>
    : <span className="chip">모든 회사</span>);

  return (
    <div className="harness">
      <input ref={fileInput} type="file" multiple hidden accept=".md,.markdown,.txt,.pdf,.docx,.xlsx,.csv" onChange={onFiles} />
      <div className="harness-side">
        {info && (
          <Section title={<span className="title">{info.kind === 'company' ? '🏢' : '📂'} {info.title}</span>} right={<>
            <button type="button" className="btn small-btn" onClick={onEdit}>고치기</button>
            <button type="button" className="btn small-btn danger" onClick={onDelete}>지우기</button></>}
            help={info.kind === 'company'
              ? '회사 하네스 — 대상 회사 사람의 실행에는 고르지 않아도 공통 하네스 뒤에 붙고, 관리자는 입력창·워크플로 노드에서 골라 씁니다.'
              : '프로젝트 하네스 — 입력창의 하네스 선택이나 워크플로 노드에서 고르면 공통 하네스 뒤에 붙습니다.'}>
            <div className="small" style={{ marginTop: 6 }}>
              <span className="chip">{info.kind === 'company' ? '회사' : '프로젝트'}</span>{' '}
              {info.companies.length ? <span className="chip warn">대상: {info.companies.join(', ')}</span> : <span className="chip">모든 회사</span>}{' '}
              <code className="inline">{scope}</code>
            </div>
          </Section>
        )}
        <Section title={scope ? '하네스 지침' : '공통 지침'}
          help={scope ? '이 하네스를 쓰는 실행에 공통 지침 뒤에 붙는다' : '모든 실행에 붙는 지침 — 로컬·외부·CLI 어느 모델이든 똑같이 적용된다'}>
          {rootAgent ? fileRow(rootAgent) : <button type="button" className="btn" onClick={() => create('AGENT.md', '모든 답은 존댓말로 한다.\n')}>＋ AGENT.md 만들기</button>}
          {!scope && rootFolder && rootFolder.companies.length > 0 && <div style={{ marginTop: 6 }}>{scopeChip(rootFolder)}</div>}
        </Section>
        <Section title="용도" right={<button type="button" className="btn small-btn" onClick={newRole}>＋ 새 용도</button>}
          help={`agents/<이름>.md — ${scope ? '이 하네스를 고르면 ' : ''}입력창·워크플로 노드의 용도로 보인다. 머리말: name · description · tools`}>
          {roles.map(fileRow)}
          {roles.length === 0 && <div className="muted small">아직 없습니다.</div>}
          {tree.yaml_roles.length > 0 && (
            <button type="button" className="btn link" onClick={exportRoles}>기존 용도 {tree.yaml_roles.length}개를 마크다운 파일로 옮기기</button>
          )}
        </Section>
        <Section title="자료 폴더" right={<button type="button" className="btn small-btn" onClick={() => newFolder()}>＋ 폴더</button>}
          help="사용설명서 · 에러 대처 같은 QA 자료. 에이전트는 목록만 보고 필요한 것을 찾아 읽는다.">
          {rootDocs.length > 0 && <div className="h-folder"><div className="h-folder-head"><b>(맨 위)</b></div>{rootDocs.map(fileRow)}</div>}
          {groups.map(({ folder, files }) => (
            <div key={folder.path} className="h-folder" style={{ marginLeft: (folder.path.split('/').length - 1) * 12 }}>
              <div className="h-folder-head">
                <b title={folder.path}>📁 {folder.path.split('/').pop()}</b> {scopeChip(folder)}
                <span className="spacer" />
                <button type="button" className="icon-text" title="자료 만들기" onClick={() => newDoc(folder.path)}>＋자료</button>
                {!folder.has_instructions && <button type="button" className="icon-text" title="이 폴더 자료에 대한 지침 (AGENT.md)"
                  onClick={() => create(`${folder.path}/AGENT.md`, FOLDER_TEMPLATE)}>＋지침</button>}
                <button type="button" className="icon-text" title="파일 올리기 (md·txt·pdf·docx·xlsx·csv)" onClick={() => upload(folder.path)}>올리기</button>
                <button type="button" className="icon-text" title="안에 폴더 만들기" onClick={() => newFolder(folder.path)}>＋폴더</button>
                <button type="button" className="icon-text" title="대상 회사 정하기" onClick={() => setScopeOf(folder)}>대상</button>
                <button type="button" className="icon-text danger" title="폴더 지우기" onClick={() => remove(folder.path, true)}>지우기</button>
              </div>
              {files.map(fileRow)}
              {!files.length && <div className="muted small">비어 있음 — ＋자료 또는 올리기</div>}
            </div>
          ))}
          {groups.length === 0 && <div className="muted small">아직 없습니다. 회사별(A사) 또는 장비별(X100) 폴더를 만드세요.</div>}
        </Section>
        <div className="muted small">저장 위치: <code className="inline">{tree.root}</code></div>
      </div>
      <div className="harness-main">
        {!open ? (
          <Section title="파일을 고르세요">
            <div className="muted small" style={{ marginTop: 8 }}>
              왼쪽에서 지침 · 용도 · 자료를 고르면 여기서 마크다운으로 고칩니다. 위쪽 <code className="inline">---</code> 사이는 머리말
              (title · description · tags / 용도는 name · description · tools / 폴더 지침은 companies), 그 아래가 본문입니다.
            </div>
          </Section>
        ) : (
          <Section title={<span className="title">{open}</span>} right={<>
            {current && <span className="muted small">{fmtTime(current.modified)} · {(current.size / 1024).toFixed(1)}KB</span>}
            <button type="button" className="btn" onClick={() => setPreview(!preview)}>{preview ? '편집' : '미리보기'}</button>
            <button type="button" className="btn danger" onClick={() => remove(open)}>지우기</button>
            <button type="button" className="btn primary" disabled={!dirty} onClick={save}>저장</button></>}>
            {current?.error && <div className="notice error" style={{ marginTop: 8 }}><span className="msg">{current.error}</span></div>}
            {preview ? <div className="md h-preview"><Markdown text={body} /></div>
              : <Editor value={text} onChange={setText} rows={24} max={2000} spellCheck={false} />}
          </Section>
        )}
      </div>
      {scopeOf && <ScopeModal folder={scopeOf} scope={scope} companies={companies} onClose={() => setScopeOf(null)}
        onSaved={() => { setScopeOf(null); load(); }} />}
    </div>
  );
}

function ScopeModal({ folder, scope, companies, onClose, onSaved }: {
  folder: Folder; scope: string; companies: Schemas['CompanyInfo'][]; onClose: () => void; onSaved: () => void;
}) {
  const ui = useUi();
  const [picked, setPicked] = useState<string[]>(folder.companies);
  const [extra, setExtra] = useState('');
  const names = [...new Set([...companies.map((c) => c.name), ...folder.companies])];
  const toggle = (n: string) => setPicked((p) => (p.includes(n) ? p.filter((x) => x !== n) : [...p, n]));
  const save = async () => {
    const all = [...picked, ...extra.split(',').map((x) => x.trim()).filter(Boolean)];
    try { await api.harness.setCompanies(folder.path, all, scope); ui.toast('저장했습니다', 'ok'); onSaved(); } catch (e) { ui.error(e); }
  };
  return (
    <Modal onClose={onClose} labelledBy="scope-title">
      <h2 className="modal-title" id="scope-title">'{folder.path}' 대상 회사</h2>
      <div className="muted small">고른 회사 사람의 실행에만 이 폴더(와 그 아래) 자료·지침이 쓰입니다. 아무것도 고르지 않으면 모든 회사.
        관리자는 늘 전부 봅니다.</div>
      <div className="scope-list">
        {names.map((n) => (
          <label key={n} className="set-toggle"><input type="checkbox" checked={picked.includes(n)} onChange={() => toggle(n)} /><span>{n}</span></label>
        ))}
        {names.length === 0 && <div className="muted small">등록된 회사가 없습니다 ('사용자 · 회사'에서 만들거나 아래에 이름을 적으세요).</div>}
      </div>
      <label className="set-field" style={{ marginTop: 10 }}><span className="set-label">다른 회사 이름 (쉼표로)</span>
        <input type="text" value={extra} onChange={(e) => setExtra(e.target.value)} /></label>
      <div className="modal-actions">
        <button type="button" className="btn" onClick={onClose}>취소</button>
        <button type="button" className="btn primary" onClick={save}>저장</button>
      </div>
    </Modal>
  );
}
