// 관리 › 외부 AI 모델 › CLI: Codex · Gemini 칸 — 설치(이 PC 에 한 번, Agent/runtime 에 npm) · 로그인(이 PC 의 브라우저에서 한 번) · 상태.
// 둘 다 Claude Code 와 같은 대화 화면 · 같은 지침(설정 · 프롬프트 + 하네스) · 같은 승인 패널로 쓴다 (agent/claude_code/engines.py).
import { useEffect, useRef, useState } from 'react';
import { Badge, Field, Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { type CcSettings, type CcStatus, ccApi } from './api';
import { errText, refreshCc } from './actions';
import { ENGINE_LABEL } from './lib';

type Engine = NonNullable<CcStatus['engines']>[number];
const MAKER: Record<string, string> = { codex: 'OpenAI', gemini: 'Google' };
const LOGIN_HINT: Record<string, string> = {
  codex: 'ChatGPT 계정으로 로그인합니다 (Plus · Pro · Team 등). 로그인은 이 PC 의 ~/.codex 에 남아 계속 씁니다.',
  gemini: 'Google 계정으로 로그인합니다. 로그인은 이 PC 의 ~/.gemini 에 남아 계속 씁니다.',
};

export function EngineSection({ info, offline, draft, set }: {
  info: Engine; offline: boolean; draft: CcSettings | null; set: <K extends keyof CcSettings>(k: K, v: CcSettings[K]) => void;
}) {
  const ui = useUi();
  const [busy, setBusy] = useState('');
  const timer = useRef<ReturnType<typeof setInterval>>(undefined);
  const engine = info.engine as 'codex' | 'gemini';
  const label = ENGINE_LABEL[engine];
  const waiting = info.login.state === 'waiting' || info.install.state === 'running';
  useEffect(() => {   // 로그인 · 설치를 기다리는 동안은 자주 확인한다
    clearInterval(timer.current);
    if (waiting) timer.current = setInterval(() => void refreshCc(), 3000);
    return () => clearInterval(timer.current);
  }, [waiting]);

  const login = async () => {
    setBusy('login');
    const win = window.open('', '_blank');   // 팝업 차단을 피하려고 누른 즉시 창을 연다
    try {
      const r = await ccApi.engineLogin(engine);
      if (r.url && win) win.location.href = r.url;
      else win?.close();
      ui.toast(r.state === 'done' ? `${label} 은(는) 이미 로그인되어 있습니다` : '열린 브라우저 창에서 로그인을 마치세요 — 끝나면 여기 상태가 바뀝니다', 'info', 8000);
      await refreshCc();
    } catch (e) { win?.close(); ui.toast(errText(e), 'error', 8000); }
    setBusy('');
  };
  const install = async () => {
    if (!(await ui.confirm({ title: `${label} CLI 를 이 PC 에 설치할까요?`, confirmText: '설치',
      message: `npm 으로 Agent\\runtime\\${engine}-cli 에 설치합니다 (전역 설치·설정은 바꾸지 않음, 인터넷 필요, 1~3분). 한 번 설치하면 계속 씁니다.` }))) return;
    setBusy('install');
    try { await ccApi.engineInstall(engine); await refreshCc(); } catch (e) { ui.toast(errText(e), 'error', 8000); }
    setBusy('');
  };

  const ok = info.installed && info.logged_in && !offline;
  const pathKey = `${engine}_path` as 'codex_path' | 'gemini_path';
  return (
    <Section title={<span className="title">CLI <span className="muted small">· {label} ({MAKER[engine]})</span></span>}
      help={`대화 화면의 모델 선택에서 '⌘ ${label}' 를 고르면 같은 화면 · 같은 지침 · 같은 승인 패널로 작업합니다 (관리자만).`}
      right={<>
        {offline ? <Badge kind="fail">폐쇄망 스위치로 꺼짐</Badge> : ok ? <Badge kind="ok">연결됨</Badge>
          : <Badge kind="fail">{!info.installed ? '설치 필요' : '로그인 필요'}</Badge>}
        <button type="button" className="btn" disabled={Boolean(busy) || info.install.state === 'running' || offline} onClick={install}>
          {info.install.state === 'running' ? '설치하는 중…' : info.installed ? '최신으로 다시 설치' : '설치'}</button>
        <button type="button" className={`btn${info.installed && !info.logged_in ? ' primary' : ''}`}
          disabled={!info.installed || Boolean(busy) || offline} onClick={login}>{info.logged_in ? '다시 로그인' : '로그인'}</button>
      </>}>
      {info.login.state === 'waiting' && (
        <div className="notice"><span className="msg">브라우저에서 로그인을 기다리는 중입니다 (이 서버 PC 의 브라우저에서 해야 합니다).
          {info.login.url && <> 창이 안 열렸으면 <a href={info.login.url} target="_blank" rel="noreferrer">여기를 눌러 여세요</a>.</>}</span></div>
      )}
      {info.login.state === 'failed' && <div className="notice error"><span className="msg">로그인 실패: {info.login.error}</span></div>}
      {info.install.state === 'failed' && <div className="notice error"><span className="msg">설치 실패: {info.install.error}
        {info.install.log && <pre className="block small">{info.install.log}</pre>}</span></div>}
      <dl className="kv">
        <dt>CLI</dt><dd>{info.installed ? <>{info.version ?? '버전 모름'} <span className="muted">({info.source})</span></>
          : <span className="err-text">{info.error ?? '찾지 못함'}</span>}</dd>
        <dt>경로</dt><dd className="small"><code className="inline">{info.path ?? '-'}</code></dd>
        <dt>로그인</dt><dd>{info.logged_in ? <>{info.account ?? ''} <span className="muted small">· {info.login_method}</span></>
          : <span className="err-text">{info.installed ? `로그인되어 있지 않습니다 — '로그인'을 누르세요. ${LOGIN_HINT[engine]}` : '먼저 설치하세요.'}</span>}
          {info.installed && info.error && info.logged_in === false && <div className="muted small">{info.error}</div>}</dd>
        <dt>모델</dt><dd className="small">{info.models.length ? info.models.map((m) => m.label || m.id).join(' · ')
          : <span className="muted">{engine === 'gemini' ? '한 번 쓰고 나면 보입니다 (그 전에는 기본 모델)' : '-'}</span>}</dd>
      </dl>
      {draft && (<div style={{ marginTop: 12 }}>
        <Field label="CLI 경로 직접 지정" help="비우면 자동으로 찾습니다 (위 '설정'의 저장 버튼으로 저장)">
          <input type="text" value={draft[pathKey]} placeholder={info.path ?? ''} onChange={(e) => set(pathKey, e.target.value)} /></Field>
      </div>)}
    </Section>
  );
}
