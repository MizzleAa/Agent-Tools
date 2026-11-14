// 관리 › 기록 · 사용량: 누가 · 어느 회사가 · 어떤 모델로 무엇을 실행했는지 (외부 모델 실행의 입력·결과도 같은 기록) + 토큰 합계
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { firstLine, fmtTime } from '@/lib/format';
import { STATUS } from '@/lib/labels';
import { Field, Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';

type Group = 'user_id' | 'company_id' | 'connection' | 'model';
const GROUPS: [Group, string][] = [['user_id', '사람별'], ['company_id', '회사별'], ['connection', '외부 연결별'], ['model', '모델별']];
const PAGE = 100;
const num = (n: number) => n.toLocaleString('ko-KR');

export function RecordsTab() {
  const ui = useUi();
  const navigate = useNavigate();
  const [users, setUsers] = useState<Schemas['UserInfo'][]>([]);
  const [companies, setCompanies] = useState<Schemas['CompanyInfo'][]>([]);
  const [group, setGroup] = useState<Group>('user_id');
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [userId, setUserId] = useState<string>('*');
  const [companyId, setCompanyId] = useState<string>('*');
  const [provider, setProvider] = useState<string>('*');
  const [usage, setUsage] = useState<Schemas['UsageRow'][]>([]);
  const [page, setPage] = useState<Schemas['RunPage'] | null>(null);

  useEffect(() => {
    Promise.all([api.admin.users(), api.admin.companies()]).then(([u, c]) => { setUsers(u); setCompanies(c); }).catch(ui.error);
  }, [ui]);

  const range = useCallback(() => ({
    ...(since ? { since } : {}),
    ...(until ? { until: new Date(new Date(until).getTime() + 86400000).toISOString().slice(0, 10) } : {}),   // 그날까지 포함
  }), [since, until]);
  const filters = useCallback(() => ({
    ...range(),
    ...(userId !== '*' ? { user_id: userId } : {}), ...(companyId !== '*' ? { company_id: companyId } : {}),
    ...(provider !== '*' ? { provider } : {}),
  }), [range, userId, companyId, provider]);

  const load = useCallback(async (offset = 0) => {
    try {
      const [u, r] = await Promise.all([api.admin.usage({ group, ...range() }), api.admin.runs({ ...filters(), limit: PAGE, offset })]);
      setUsage(u);
      setPage((prev) => (offset && prev ? { total: r.total, items: [...prev.items, ...r.items] } : r));
    } catch (e) { ui.error(e); }
  }, [group, range, filters, ui]);
  useEffect(() => { load(0); }, [load]);

  const total = usage.reduce((a, r) => ({ runs: a.runs + r.runs, p: a.p + r.prompt_tokens, c: a.c + r.completion_tokens }), { runs: 0, p: 0, c: 0 });
  return (
    <>
      <Section title="기간">
        <div className="field-grid">
          <Field label="부터"><input type="date" className="select" value={since} onChange={(e) => setSince(e.target.value)} /></Field>
          <Field label="까지"><input type="date" className="select" value={until} onChange={(e) => setUntil(e.target.value)} /></Field>
        </div>
      </Section>
      <Section title="사용량" right={<select className="select" value={group} onChange={(e) => setGroup(e.target.value as Group)}>
        {GROUPS.map(([g, l]) => <option key={g} value={g}>{l}</option>)}</select>}
        help="실행 수와 토큰(입력·출력). 외부 모델 요금은 공급사 콘솔의 청구 화면에서 확인하세요.">
        <table className="set-table">
          <thead><tr><th>{GROUPS.find(([g]) => g === group)?.[1].replace('별', '')}</th><th>실행</th><th>입력 토큰</th><th>출력 토큰</th><th>걸린 시간</th><th>마지막</th></tr></thead>
          <tbody>
            {usage.map((r) => (
              <tr key={r.key || '-'}><td>{r.label}</td><td>{num(r.runs)}</td><td>{num(r.prompt_tokens)}</td><td>{num(r.completion_tokens)}</td>
                <td>{Math.round(r.seconds / 60)}분</td><td className="small">{fmtTime(r.last_at)}</td></tr>
            ))}
            {usage.length > 0 && <tr><td><b>합계</b></td><td><b>{num(total.runs)}</b></td><td><b>{num(total.p)}</b></td><td><b>{num(total.c)}</b></td><td /><td /></tr>}
            {usage.length === 0 && <tr><td colSpan={6} className="muted small">이 기간에 실행이 없습니다.</td></tr>}
          </tbody>
        </table>
      </Section>
      <Section title={`실행 기록 ${page ? num(page.total) : ''}건`} right={<>
        <select className="select" value={userId} onChange={(e) => setUserId(e.target.value)} title="사람">
          <option value="*">모든 사람</option><option value="">(혼자 쓰기)</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
        <select className="select" value={companyId} onChange={(e) => setCompanyId(e.target.value)} title="회사">
          <option value="*">모든 회사</option>{companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)} title="모델 종류">
          <option value="*">모든 모델</option><option value="local">로컬 모델</option><option value="anthropic">Claude</option>
          <option value="openai">OpenAI</option><option value="gemini">Gemini</option>
        </select></>}
        help="누르면 그 대화를 엽니다 (요청 · 도구 호출 · 결과 전체).">
        <table className="set-table records">
          <thead><tr><th>시각</th><th>사람</th><th>회사</th><th>모델</th><th>상태</th><th>토큰 (입력/출력)</th><th>요청</th></tr></thead>
          <tbody>{page?.items.map((r) => (
            <tr key={r.id} className="clickable" onClick={() => navigate(`/c/${r.id}`)} title={r.task}>
              <td className="small">{fmtTime(r.created_at)}</td>
              <td>{r.user_name || <span className="muted">혼자 쓰기</span>}</td>
              <td className="small">{r.company_name || '—'}</td>
              <td className="small">{r.provider && r.provider !== 'local' ? <><b>{r.provider}</b> · {r.model}</> : r.model}</td>
              <td className="small">{STATUS[r.status] ?? r.status}</td>
              <td className="small">{num(r.prompt_tokens)} / {num(r.completion_tokens)}</td>
              <td className="small ellipsis">{firstLine(r.task)}</td>
            </tr>
          ))}</tbody>
        </table>
        {page && page.items.length < page.total && (
          <button type="button" className="btn" style={{ marginTop: 10 }} onClick={() => load(page.items.length)}>더 보기</button>
        )}
      </Section>
    </>
  );
}
