// 개발 규격 문서 · 변경 기록 · 안내
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { useApp } from '@/state/AppState';
import { Editor, Section, SizeLine } from '@/ui/form';
import { Markdown } from '@/ui/Markdown';
import { useUi } from '@/ui/UiProvider';
import { useDraft, usePending } from './pending';
import { useSettings } from './parts';

// ───────────────────────── 개발 규격

export function StandardsTab({ selected }: { selected?: string }) {
  const { data } = useSettings();
  const navigate = useNavigate();
  const name = data.standards.some((s) => s.name === selected) ? selected! : data.standards[0]?.name;
  return (
    <div className="set-stack">
      <div className="muted set-intro">에이전트가 따를 개발 규격이자 QA 판정 기준입니다 (원본: Agent_QA/기준). 언어별 문서는 그 언어로 개발할 때 시스템 프롬프트에 통째로 붙으니 짧게 유지하세요. 여기서 고치면 QA 기준 문서도 같이 맞춰야 판정이 어긋나지 않습니다. 시스템 프롬프트에 들어가는 공통 요약은 '공통 블록 › 개발 규격 요약'입니다.</div>
      <div className="form-row">
        {data.standards.map((s) => (
          <button key={s.name} type="button" className={`stab${s.name === name ? ' active' : ''}`} onClick={() => navigate(`/settings/standards/${encodeURIComponent(s.name)}`)}>
            {s.name}<span className="muted small"> {(s.chars / 1024).toFixed(1)}KB</span>
          </button>
        ))}
      </div>
      {name && <StandardEditor key={name} name={name} />}
    </div>
  );
}

function StandardEditor({ name }: { name: string }) {
  const ui = useUi();
  const [text, setText] = useState<string | null>(null);
  useEffect(() => { api.settings.standards.get(name).then((r) => setText(r.text), ui.error); }, [name, ui]);
  if (text === null) return <div className="muted">불러오는 중…</div>;
  return <StandardText name={name} initial={text} />;
}

function StandardText({ name, initial }: { name: string; initial: string }) {
  const [text, update] = useDraft(initial, `std:${name}`, `규격 ${name}`, (v) => api.settings.standards.put(name, v));
  return (
    <Section title={<span className="title set-title">{name}</span>}>
      <Editor value={text} onChange={update} rows={24} max={1400} />
      <div className="set-foot"><SizeLine text={text} /></div>
    </Section>
  );
}

// ───────────────────────── 변경 기록

export function HistoryTab() {
  const ui = useUi();
  const { reload } = useSettings();
  const { reloadCatalog } = useApp();
  const { clear } = usePending();
  const [items, setItems] = useState<Schemas['HistoryEntry'][] | null>(null);
  const load = () => api.settings.history.list().then(setItems, ui.error);
  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const view = async (id: string) => {
    let e: Schemas['HistoryDetail'];
    try { e = await api.settings.history.get(id); } catch (err) { ui.error(err); return; }
    const lines = (e.diff || '(그때와 지금이 같습니다)').split('\n');
    ui.sheet(`${e.target} — ${e.time}`, <>
      <div className="muted small">{e.note} · 아래는 그때(저장 직전) → 지금 차이입니다. '- ' 줄이 그때 내용, '+ ' 줄이 지금 내용.</div>
      <pre className="block diff">{lines.map((l, i) => (
        <span key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'd-add' : l.startsWith('-') && !l.startsWith('---') ? 'd-del' : l.startsWith('@@') ? 'd-hunk' : ''}>{`${l}\n`}</span>
      ))}</pre>
      <details><summary className="muted small">그때 내용 전체</summary><pre className="block">{e.existed ? e.text : '(파일이 없었음)'}</pre></details>
    </>);
  };
  const restore = async (it: Schemas['HistoryEntry']) => {
    if (!(await ui.confirm({ title: '이 기록으로 되돌릴까요?', confirmText: '되돌리기', danger: true,
      message: `${it.target} 를 ${it.time} 저장 직전 상태로 되돌립니다.\n지금 내용도 기록에 남으니 다시 되돌릴 수 있습니다.` }))) return;
    try { await api.settings.history.restore(it.id); } catch (e) { ui.error(e); return; }
    ui.toast('되돌렸습니다. 다음 실행부터 적용됩니다.', 'ok');
    clear();
    await reload();
    await reloadCatalog();
    load();
  };
  return (
    <Section title="변경 기록" help="설정에서 저장할 때마다 바뀌기 전 내용을 남깁니다 (300개까지 보관, 여기엔 최근 100개). 하네스 파일은 기록하지 않습니다. '보기'로 차이를 보고 '되돌리기'로 그때 상태로 돌아갑니다.">
      {items === null ? <div className="muted small" style={{ marginTop: 10 }}>불러오는 중…</div>
        : items.length === 0 ? <div className="muted small" style={{ marginTop: 10 }}>아직 기록이 없습니다.</div> : (
          <table className="set-table">
            <thead><tr><th>시간</th><th>파일</th><th>내용</th><th /></tr></thead>
            <tbody>{items.map((it) => (
              <tr key={it.id}>
                <td className="nowrap">{it.time}</td><td><code className="inline">{it.target}</code></td>
                <td>{it.note}{it.existed ? '' : ' (새 파일)'}</td>
                <td className="nowrap">
                  <button type="button" className="mini-btn" onClick={() => view(it.id)}>보기</button>
                  <button type="button" className="mini-btn" onClick={() => restore(it)}>되돌리기</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        )}
    </Section>
  );
}

// ───────────────────────── 안내

const GUIDE = `## 시스템 프롬프트가 만들어지는 순서
1. **역할 프롬프트** — 용도 탭에서 씁니다. 이 용도의 정체성·목표·일하는 순서.
2. **블록들** — 용도 탭 '프롬프트 구성'의 순서대로. 여러 용도가 같이 쓰는 규칙은 '공통 블록'에 한 번만 씁니다.
3. **자동 블록** — 사내 연결 현황, 언어별 규격처럼 프로그램이 채우는 것.
4. 그 뒤에 도구 설명(JSON)이 붙고, 대화가 이어집니다. 루프가 끼어드는 말은 '루프 메시지'입니다.

순서 원칙: **잘 안 바뀌는 것을 앞에, 대화마다 바뀌는 것(작업 폴더·날짜)을 뒤에.** 모델 서버는 앞부분이 같은 요청의 계산을 재사용하므로 새 대화의 첫 응답이 빨라집니다.

## 문법
- \`{{이름}}\` — 변수. 내장 변수(\`{{cwd}}\`, \`{{today}}\` …) > 용도 변수 > 전역 변수 순서로 찾습니다.
- 줄 맨 앞 \`{{?run_command}}\` — 그 도구를 가진 용도에만 이 줄을 넣습니다. \`{{?a|b}}\` 는 둘 중 하나, \`{{?!이름}}\` 은 반대.
- 없는 도구 얘기를 프롬프트에 쓰면 모델이 그 도구를 부르려다 실패합니다. 미리보기가 찾아 줍니다.

## 워크플로
- 노드(용도 + 지시문)를 정해진 순서로 실행합니다. 순서는 모델이 아니라 프로그램이 정합니다.
- 노드마다 새 컨텍스트로 시작하고, 앞 노드의 최종 답을 \`{{노드id}}\` 로 받습니다. \`{{input}}\` = 요청, \`{{previous}}\` = 같은 대화의 이전 답.
- 검토 노드는 쓰기 도구가 없는 용도(reviewer)로 돌리면 고치지 않고 확인만 합니다.

## 소형 모델(8~35B)에 잘 듣는 프롬프트
- **한 줄에 규칙 하나**, 명령형으로 짧게. "~하지 마" 보다 "~한다"가 잘 듣습니다.
- **순서를 번호로**: 확인 → 실행 → 검증 → 보고.
- **예시 1~2개**가 규칙 열 줄보다 낫습니다 ('예시' 선택 블록).
- 같은 말을 여러 블록에 되풀이하지 않습니다 (토큰 낭비 + 우선순위 혼란).
- 미리보기에서 **컨텍스트 예산의 30% 이하**를 목표로 합니다. 나머지가 대화·도구 결과 자리입니다.

## 바꾼 뒤 확인하는 방법
1. 미리보기로 조립 결과·경고를 봅니다.
2. 같은 요청을 바꾸기 전·후로 실행해 결과를 비교합니다 (\`eval/run_eval.py\` 로 점수 비교).
3. 나빠지면 '변경 기록'에서 되돌립니다.

## 저장 위치
| 무엇 | 파일 |
|---|---|
| 용도(프로필) | config/agents/<이름>.yaml |
| 워크플로 | config/workflows/<이름>.yaml |
| 블록·루프 메시지·도구 설명·변수 | config/prompts.yaml (기본값과 다른 것만) |
| 기본값 원본 | agent/prompt_defaults/ (고치지 않음) |
| 실행 파라미터 | config/settings.yaml (주석 유지) |
| 추가한 모델 · 모델별로 바꾼 값 · 고른 모델 | config/connections.yaml |
| 개발 규격 | config/standards/*.md |
| 변경 기록 | data/settings_history/ |

## API
화면은 관리 서버의 API(\`/api/v1\`)만 씁니다. 계약은 \`/api/v1/openapi.json\`, 사람이 보는 문서는 \`/api/docs\` (인터넷이 되는 PC 에서).
`;

export function GuideTab() {
  return <Section title="설정 안내"><Markdown text={GUIDE} /></Section>;
}
