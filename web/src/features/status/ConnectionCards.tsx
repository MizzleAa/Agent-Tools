// 사내 연결 카드: API · 사이트 · 읽기 허용 내부 주소 (+ 토큰 입력)
import { useEffect, useState } from 'react';
import { api, type Schemas } from '@/api';
import { fmtTime } from '@/lib/format';
import { CRAWL } from '@/lib/labels';
import { useApp } from '@/state/AppState';
import { Badge, Chip } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { useAction } from './parts';

type AuthKind = 'none' | 'token' | 'login';
const AUTH_LABEL: Record<AuthKind, string> = { none: '인증 없음', token: '토큰', login: '계정으로 로그인' };

// API 인증: 방식(없음 · 토큰 · 계정 로그인)과 값. 값은 저장만 하고 화면·채팅·모델 어디로도 돌아오지 않는다.
function AuthPanel({ api: a }: { api: Schemas['ApiInfo'] }) {
  const ui = useUi();
  const act = useAction();
  const saved: AuthKind = (a.auth_kind as AuthKind | null | undefined) ?? 'none';
  const [kind, setKind] = useState<AuthKind>(saved);
  const [loginUrl, setLoginUrl] = useState(a.login_url ?? '');
  const [header, setHeader] = useState(a.auth_header || 'Authorization');
  const [prefix, setPrefix] = useState(a.auth_header ? (a.auth_prefix ?? '') : 'Bearer ');
  const [token, setToken] = useState('');
  const [user, setUser] = useState('');
  const [pw, setPw] = useState('');
  const changed = kind !== saved || (kind === 'login' && loginUrl !== (a.login_url ?? ''))
    || (kind === 'token' && (header !== a.auth_header || prefix !== (a.auth_prefix ?? '')));
  const saveKind = () => act(() => api.apis.setAuth(a.name, { kind, login_url: loginUrl.trim(), header: header.trim(), prefix }));
  const saveValues = async () => {
    const body = kind === 'login' ? { username: user, password: pw } : { token };
    if (!Object.values(body).some(Boolean)) return;
    if (await act(() => api.apis.credentials(a.name, body))) { setToken(''); setUser(''); setPw(''); }
  };
  const check = async () => {
    try {
      const r = await api.apis.loginCheck(a.name);
      ui.toast(r.message, r.ok ? 'ok' : 'error', 8000);
    } catch (e) { ui.error(e); }
  };
  return (
    <details className="auth-panel" open={saved !== 'none' && a.auth === false}>
      <summary className="small">
        인증: <b>{AUTH_LABEL[saved]}</b>
        {saved !== 'none' && <> · {a.auth ? '저장됨' : <span className="bad-text">{saved === 'login' ? '계정' : '토큰'} 없음 — 여기에 넣으세요</span>}</>}
        {a.auth_source && <span className="muted"> · {a.auth_source}</span>}
      </summary>
      <div className="form-row">
        <select value={kind} onChange={(e) => setKind(e.target.value as AuthKind)} aria-label="인증 방식">
          {(Object.keys(AUTH_LABEL) as AuthKind[]).map((k) => <option key={k} value={k}>{AUTH_LABEL[k]}</option>)}
        </select>
        {kind === 'login' && <input type="url" className="grow" placeholder="로그인 주소 (토큰 받는 곳)  예) http://서버:포트/auth/login"
          value={loginUrl} onChange={(e) => setLoginUrl(e.target.value)} />}
        {kind === 'token' && <>
          <input type="text" size={14} placeholder="헤더 이름" value={header} onChange={(e) => setHeader(e.target.value)} />
          <input type="text" size={8} placeholder="앞 글자" value={prefix} onChange={(e) => setPrefix(e.target.value)} />
        </>}
        {changed && <button type="button" className="btn primary" onClick={saveKind}>방식 저장</button>}
      </div>
      {!changed && saved === 'login' && (
        <div className="form-row">
          <input type="text" autoComplete="off" placeholder="아이디" value={user} onChange={(e) => setUser(e.target.value)} />
          <input type="password" className="grow" autoComplete="new-password" placeholder="비밀번호" value={pw}
            onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') saveValues(); }} />
          <button type="button" className="btn" onClick={saveValues}>{a.auth ? '계정 바꾸기' : '계정 저장'}</button>
          {a.auth && <button type="button" className="btn" onClick={check}>로그인 확인</button>}
        </div>
      )}
      {!changed && saved === 'token' && (
        <div className="form-row">
          <input type="password" className="grow" autoComplete="off" placeholder="토큰 값" value={token}
            onChange={(e) => setToken(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') saveValues(); }} />
          <button type="button" className="btn" onClick={saveValues}>{a.auth ? '토큰 바꾸기' : '토큰 저장'}</button>
        </div>
      )}
      <div className="muted small">값은 이 PC 의 config/secrets.yaml 에만 저장되고, 에이전트(모델)·채팅·실행 기록에는 나가지 않습니다.</div>
    </details>
  );
}

function SecretForm({ api: a }: { api: Schemas['ApiInfo'] }) {
  const [value, setValue] = useState('');
  const act = useAction();
  const save = async () => { if (value && await act(() => api.secrets.put(a.secret_name!, value))) setValue(''); };
  return (
    <div className="form-row">
      <input type="password" className="grow" autoComplete="off" placeholder={`${a.secret_name} 값`} value={value}
        onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') save(); }} />
      <button type="button" className="btn" onClick={save}>{a.auth ? '토큰 바꾸기' : '토큰 저장'}</button>
    </div>
  );
}

export function ApiCard({ apis }: { apis: Schemas['ApiInfo'][] }) {
  const ui = useUi();
  const act = useAction();
  const { refreshStatus } = useApp();
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const add = async () => {
    if (!url.trim()) return;
    setBusy(true);
    try {
      const r = await api.apis.add({ url: url.trim(), name: name.trim() });
      ui.toast(r.endpoints ? `'${r.name}' 등록 완료 — 엔드포인트 ${r.endpoints}개를 읽었습니다.` : `'${r.name}' 등록 완료 (엔드포인트 목록 없음)`, 'ok');
      setUrl('');
      setName('');
    } catch (e) { ui.error(e); }
    setBusy(false);
    refreshStatus();
  };
  const remove = async (n: string) => {
    if (await ui.confirm({ title: 'API 등록을 삭제할까요?', message: `'${n}' 을(를) 사내 API 목록에서 뺍니다. 에이전트가 더는 호출할 수 없습니다.
이 등록에만 쓰인 계정·토큰도 함께 지웁니다 (같은 서버를 쓰는 다른 등록이 있으면 남깁니다).`, confirmText: '삭제', danger: true })) {
      act(() => api.apis.remove(n));
    }
  };
  const toggle = async (a: Schemas['ApiInfo'], allow: boolean) => {
    if (allow && !(await ui.confirm({
      title: '변경 요청을 허용할까요?', confirmText: '허용',
      message: `'${a.name}' 에 데이터를 바꾸는 요청(POST·PUT·PATCH·DELETE, 파일 업로드)을 보낼 수 있게 됩니다.\n실행할 때마다 승인은 계속 묻습니다.`,
    }))) return;
    act(() => api.apis.writeAccess(a.name, allow));
  };
  return (
    <div className="card wide">
      <div className="card-head"><span className="title">사내 API</span></div>
      <div className="muted small" style={{ marginTop: 4 }}>
        Swagger 문서 주소(…/docs)를 넣으면 엔드포인트를 자동으로 읽어 등록합니다. 채팅(intranet 용도)에서 "이 API 추가해줘 http://…/docs" 라고 해도 됩니다. 로그인이 필요한 API 는 각 API 의 '인증' 칸에 아이디·비밀번호(또는 토큰)를 넣으세요 — 문서에 로그인 방식이 있으면 등록할 때 자동으로 채워집니다. 비밀번호·토큰은 채팅에 쓰지 마세요.
      </div>
      <div className="form-row">
        <input type="url" className="grow" placeholder="Swagger 문서 주소  예) http://10.1.2.3:8000/docs" value={url}
          onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
        <input type="text" size={10} placeholder="이름 (선택)" value={name} onChange={(e) => setName(e.target.value)} />
        <button type="button" className="btn primary" disabled={busy} onClick={add}>{busy ? '문서 읽는 중…' : '추가 (문서 자동 읽기)'}</button>
      </div>
      {apis.length === 0 && <div className="muted small" style={{ marginTop: 10 }}>아직 등록된 API 가 없습니다.</div>}
      {apis.map((a) => (
        <div key={a.name} className="item">
          <div className="card-head">
            <span className="title">{a.name}</span>{a.title && <span className="muted small">{a.title}</span>}<span className="spacer" />
            <Chip>엔드포인트 {a.endpoints}개</Chip>
            <Chip>{a.methods.join('/')}</Chip>
            {a.auth === null || a.auth === undefined ? <Chip>인증 없음</Chip>
              : <Badge kind={a.auth ? 'ok' : 'fail'}>{a.auth_kind === 'login' ? (a.auth ? '계정 설정됨' : '계정 없음') : (a.auth ? '토큰 설정됨' : '토큰 없음')}</Badge>}
            {a.managed && a.docs_url && <button type="button" className="btn" title={a.docs_url} onClick={() => act(() => api.apis.refresh(a.name))}>문서 다시 읽기</button>}
            {a.managed && <button type="button" className="btn" onClick={() => remove(a.name)}>삭제</button>}
          </div>
          <div className="muted small">{a.description} · {a.base_url}</div>
          {a.managed && (
            <label className="small" style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
              <input type="checkbox" checked={a.methods.some((m) => m !== 'GET')} onChange={(e) => toggle(a, e.target.checked)} />
              변경 요청 허용 (POST·PUT·PATCH·DELETE, 파일 업로드) — 실행할 때마다 승인을 묻습니다
            </label>
          )}
          {a.docs && <details style={{ marginTop: 6 }}><summary className="small muted">엔드포인트 목록 보기</summary><pre className="block">{a.docs}</pre></details>}
          {a.managed ? <AuthPanel api={a} />
            : a.auth !== null && a.auth !== undefined && a.secret_name && <SecretForm api={a} />}
        </div>
      ))}
    </div>
  );
}

export function SitesCard({ sources }: { sources: Schemas['SourceInfo'][] }) {
  const ui = useUi();
  const act = useAction();
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [whole, setWhole] = useState(false);
  const add = async () => {
    if (!url.trim()) return;
    if (await act(() => api.intranet.addSource({ url: url.trim(), name: name.trim(), whole_site: whole }))) { setUrl(''); setName(''); }
  };
  const remove = async (n: string) => {
    if (await ui.confirm({ title: '사이트를 삭제할까요?', message: `'${n}' 등록을 지우고, 수집한 페이지도 검색 색인에서 뺍니다.`, confirmText: '삭제', danger: true })) {
      act(() => api.intranet.removeSource(n));
    }
  };
  return (
    <div className="card wide">
      <div className="card-head"><span className="title">사내 사이트</span></div>
      <div className="muted small" style={{ marginTop: 4 }}>주소를 추가하면 저장 후 바로 수집합니다. 채팅(intranet 용도)에서 "이 주소 추가해줘" 라고 해도 됩니다.</div>
      <div className="form-row">
        <input type="url" className="grow" placeholder="http://wiki.intra/  (사내 주소)" value={url}
          onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
        <input type="text" size={10} placeholder="이름 (선택)" value={name} onChange={(e) => setName(e.target.value)} />
        <label className="small muted" title="체크하면 사이트 전체, 아니면 준 주소가 있는 폴더 아래만">
          <input type="checkbox" checked={whole} onChange={(e) => setWhole(e.target.checked)} /> 사이트 전체
        </label>
        <button type="button" className="btn primary" onClick={add}>추가하고 수집</button>
      </div>
      {sources.length === 0 && <div className="muted small" style={{ marginTop: 10 }}>아직 등록된 사이트가 없습니다.</div>}
      {sources.map((src) => {
        const c = src.crawl;
        const running = c?.state === 'running';
        const progress = c ? (running ? `수집 중… ${c.pages}쪽 (바뀜 ${c.changed}, 오류 ${c.errors})`
          : `마지막 수집: ${CRAWL[c.state] ?? c.state} · ${c.pages}쪽, 바뀜 ${c.changed}, 삭제 ${c.removed}, 오류 ${c.errors}`) : '';
        return (
          <div key={src.name} className="item">
            <div className="card-head">
              <span className="title">{src.name}</span><span className="spacer" />
              <Chip>색인 {src.pages}쪽</Chip>
              {running ? <button type="button" className="btn danger" onClick={() => act(() => api.intranet.cancelCrawl(src.name))}>중지</button>
                : <button type="button" className="btn" onClick={() => act(() => api.intranet.crawl(src.name))}>지금 수집</button>}
              {src.managed && <button type="button" className="btn" onClick={() => remove(src.name)}>삭제</button>}
            </div>
            <div className="muted small">{src.description} · 범위: {src.prefix}</div>
            {progress && <div className="small" style={{ marginTop: 4 }}>{progress}</div>}
            {running && c?.current_url && <div className="muted small">{c.current_url}</div>}
            {c?.last_error && <div className="small err-text">최근 오류: {c.last_error}</div>}
            {src.last_fetched && <div className="muted small">색인 갱신: {fmtTime(src.last_fetched)}</div>}
          </div>
        );
      })}
    </div>
  );
}

export function HostsCard({ hosts }: { hosts: Schemas['HostInfo'][] }) {
  const ui = useUi();
  const act = useAction();
  const [rule, setRule] = useState('');
  const add = async () => { if (rule.trim() && await act(() => api.hosts.add(rule.trim()))) setRule(''); };
  const remove = async (r: string) => {
    if (await ui.confirm({ title: '허용을 지울까요?', message: `'${r}' 은(는) 다시 차단되어 에이전트가 읽을 수 없게 됩니다.`, confirmText: '지우기', danger: true })) {
      act(() => api.hosts.remove(r));
    }
  };
  return (
    <div className="card wide">
      <div className="card-head"><span className="title">읽기 허용된 내부 주소</span></div>
      <div className="muted small" style={{ marginTop: 4 }}>
        내부망 주소는 기본으로 막혀 있습니다. 여기 있는 주소만 에이전트가 읽을 수 있습니다. 채팅에서 "10.1.2.3 허용해줘" 라고 해도 됩니다.
      </div>
      <div className="form-row">
        <input type="text" className="grow" placeholder="내부 주소  예) 10.1.2.3  또는  wiki.intra  또는  10.20.0.0/16" value={rule}
          onChange={(e) => setRule(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
        <button type="button" className="btn primary" onClick={add}>허용</button>
      </div>
      {hosts.length ? (
        <div className="form-row">
          {hosts.map((x) => (
            <span key={x.rule} className="chip" style={{ display: 'inline-flex', gap: 6, alignItems: 'center', padding: '3px 8px' }}>
              {x.rule}{x.registered ? ' (등록된 API·사이트)' : ''}
              {x.managed && <button type="button" className="mini-btn" title="허용 지우기" onClick={() => remove(x.rule)}>✕</button>}
            </span>
          ))}
        </div>
      ) : <div className="muted small" style={{ marginTop: 10 }}>아직 허용된 내부 주소가 없습니다.</div>}
    </div>
  );
}

/** 공유 위치 (읽기 전용 허용 목록): 보기 · 지우기 · 별도 계정 넣기 (값은 저장만 — 다시 보여 주지 않는다) */
export function SharesCard() {
  const ui = useUi();
  const [list, setList] = useState<Schemas['LocationInfo'][] | null>(null);
  const [editing, setEditing] = useState('');
  const [user, setUser] = useState('');
  const [pw, setPw] = useState('');
  const load = () => api.locations.list().then((l) => setList(l.filter((x) => x.kind === 'share')), ui.error);
  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const remove = async (l: Schemas['LocationInfo']) => {
    if (!(await ui.confirm({ title: '허용을 지울까요?', message: `'${l.path}' 은(는) 다시 막혀 채팅·탐색기에서 읽을 수 없게 됩니다${l.has_account ? ' (저장한 계정도 지웁니다)' : ''}.`, confirmText: '지우기', danger: true }))) return;
    try { await api.locations.remove(l.id); load(); } catch (e) { ui.error(e); }
  };
  const save = async (id: string) => {
    try { await api.locations.setAccount(id, { username: user, password: pw }); setEditing(''); setUser(''); setPw(''); ui.toast('계정을 저장했습니다 (화면에 다시 보이지 않습니다)'); load(); }
    catch (e) { ui.error(e); }
  };
  const clear = async (id: string) => { try { await api.locations.clearAccount(id); load(); } catch (e) { ui.error(e); } };
  return (
    <div className="card wide">
      <div className="card-head"><span className="title">공유 위치 (읽기 전용)</span></div>
      <div className="muted small" style={{ marginTop: 4 }}>
        채팅에 공유 경로(\\서버\공유\…, Z:\…, file://…)를 주면 처음 한 번 허용을 묻고 여기에 더합니다. 경로는 에이전트가 도는 이 PC 기준이고,
        모든 용도가 읽기만 합니다. 시스템 폴더·에이전트 설정·비밀 저장소·모델 폴더는 넣을 수 없습니다. 파일 탐색기에서 직접 더할 수도 있습니다.
      </div>
      {list === null ? <div className="muted small" style={{ marginTop: 10 }}>불러오는 중…</div>
        : list.length === 0 ? <div className="muted small" style={{ marginTop: 10 }}>아직 허용한 공유 위치가 없습니다.</div>
          : list.map((l) => (
            <div key={l.id} className="share-row">
              <span className="share-path" title={l.path}>{l.path}</span>
              <span className="muted small">{l.added.slice(0, 16).replace('T', ' ')}{l.has_account ? ' · 계정 있음' : ''}</span>
              <span className="spacer" />
              <button type="button" className="mini-btn" onClick={() => setEditing(editing === l.id ? '' : l.id)}>{l.has_account ? '계정 바꾸기' : '계정 넣기'}</button>
              {l.has_account && <button type="button" className="mini-btn" onClick={() => clear(l.id)}>계정 지우기</button>}
              <button type="button" className="mini-btn" title="허용 지우기" onClick={() => remove(l)}>✕</button>
              {editing === l.id && (
                <div className="form-row share-account">
                  <input type="text" placeholder="아이디 (예: DOMAIN\user)" value={user} autoComplete="off" onChange={(e) => setUser(e.target.value)} />
                  <input type="password" placeholder="비밀번호" value={pw} autoComplete="new-password" onChange={(e) => setPw(e.target.value)} />
                  <button type="button" className="btn primary" disabled={!user || !pw} onClick={() => save(l.id)}>저장</button>
                  <span className="muted small">이 공유가 이 PC 계정으로 열리지 않을 때만 필요합니다. 비밀 저장소에만 두고 채팅·기록·주소에 남기지 않습니다.</span>
                </div>
              )}
            </div>
          ))}
    </div>
  );
}
