// 모델: 목록 · 교체 · 모델별 값(컨텍스트·GPU 층·추가 인자) · GGUF 추가 · 목록에서 빼기
import { api, type Schemas } from '@/api';
import { FILE_STATE, RUNTIME } from '@/lib/labels';
import { useApp } from '@/state/AppState';
import { Field, Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { useDraft, usePending } from './pending';
import { useSettings } from './parts';

/** 쓰는 모델의 값을 바꿨으면 모델 서버를 다시 켜야 적용된다 */
export function useRestart() {
  const ui = useUi();
  const { refreshStatus } = useApp();
  return async (name: string) => {
    if (!(await ui.confirm({ title: '모델 서버를 다시 켤까요?', confirmText: '다시 켜기',
      message: `'${name}' 의 바뀐 값(컨텍스트·GPU 층·추가 인자)은 모델 서버를 다시 켜야 적용됩니다. 수십 초 걸립니다.` }))) return;
    try { await api.system.selectModel(name); ui.toast('모델 서버를 다시 켜는 중입니다. 준비되면 입력창 위 안내가 사라집니다.', 'ok'); }
    catch (e) { ui.error(e); }
    refreshStatus();
  };
}

export function ModelsTab() {
  const { data, reload } = useSettings();
  const { status, refreshStatus } = useApp();
  const ui = useUi();
  const { guard } = usePending();
  const gpu = status?.gpu;
  const switchTo = async (name: string) => {
    if (!(await guard())) return;
    if (status?.active_run) { ui.toast('실행 중인 작업이 끝난 뒤에 모델을 바꿀 수 있습니다.'); return; }
    if (!(await ui.confirm({ title: `'${name}' 모델로 바꿀까요?`, message: '모델 서버를 다시 띄우느라 수십 초 걸립니다. 그동안은 메시지를 보낼 수 없어요.', confirmText: '바꾸기' }))) return;
    try { await api.system.selectModel(name); } catch (e) { ui.error(e); }
    refreshStatus();
    setTimeout(reload, 600);
  };
  const add = async (f: Schemas['ModelFile']) => {
    if (!(await guard())) return;
    const r = await ui.form({
      title: '모델 추가', message: `${f.file} (${f.size_gb} GB) 를 모델 목록에 넣습니다. 이름은 입력창의 모델 선택에 보입니다.`,
      fields: [
        { name: 'name', label: '이름 (영어·숫자·. - _)', value: f.suggested_name },
        { name: 'note', label: '설명', placeholder: '예) 9B · GPU 에 전부 · 빠름' },
        { name: 'context_length', label: '컨텍스트 길이 (비우면 기본값)', placeholder: String(data.model_defaults.context_length) },
        { name: 'extra_args', label: '추가 인자 (MoE 면 --n-cpu-moe N)', placeholder: '예) --n-cpu-moe 36' },
      ],
      confirmText: '추가',
    });
    if (!r?.name) return;
    try {
      await api.settings.models.create({ name: r.name, file: f.file, note: r.note, extra_args: r.extra_args,
        context_length: r.context_length ? Number(r.context_length) : null });
    } catch (e) { ui.error(e); return; }
    ui.toast(`'${r.name}' 을 모델 목록에 넣었습니다. '이 모델로 바꾸기'로 쓸 수 있습니다.`, 'ok');
    await reload();
    refreshStatus();
  };
  return (
    <div className="set-stack">
      <div className="set-intro">
        <div className="muted">모델 서버는 한 번에 모델 하나만 띄웁니다. 바꾸면 수십 초 동안 다시 불러오고, 그동안은 실행할 수 없습니다. 입력창 왼쪽 아래 모델 선택으로도 바꿀 수 있습니다.</div>
        {status && <div className="muted small">모델 서버: {RUNTIME[status.runtime.state] ?? status.runtime.state} · {status.backend.model} · 컨텍스트 {status.backend.context_length.toLocaleString()}
          {gpu && ` · GPU ${gpu.name} ${(gpu.used_mb / 1024).toFixed(1)} / ${(gpu.total_mb / 1024).toFixed(1)} GB 사용`}</div>}
      </div>
      {data.models.map((m) => <ModelCard key={m.name} m={m} onSwitch={switchTo} />)}
      <Section title="모델 추가" help="GGUF 파일을 models 폴더에 넣으면 여기에 나타납니다 (폐쇄망은 USB 로 복사). 대화용 모델만 보이고 임베딩 모델은 뺍니다. 8GB GPU 기준: 8~9B Q4 는 GPU 에 전부, 30B 급 MoE 는 --n-cpu-moe 로 전문가를 RAM 에 둡니다. 바꾼 뒤에는 같은 요청·eval 로 비교하세요.">
        {data.model_files.length === 0 && <div className="muted small" style={{ marginTop: 8 }}>models 폴더에 목록에 없는 GGUF 파일이 없습니다.</div>}
        {data.model_files.map((f) => (
          <div key={f.file} className="brow">
            <div className="brow-main"><div className="brow-title"><b>{f.file}</b><span className="chip">{f.size_gb} GB</span>
              {f.state !== 'ready' && <span className="badge warn">{FILE_STATE[f.state]?.[1] ?? f.state}</span>}</div></div>
            <button type="button" className="btn" disabled={f.state !== 'ready'} onClick={() => add(f)}>목록에 추가</button>
          </div>
        ))}
      </Section>
    </div>
  );
}

interface ModelDraft { note: string; context_length: number | null; gpu_layers: number | null; extra_args: string }

function ModelCard({ m, onSwitch }: { m: Schemas['ModelPresetInfo']; onSwitch: (name: string) => void }) {
  const { data, reload } = useSettings();
  const ui = useUi();
  const { clear } = usePending();
  const restart = useRestart();
  const def = data.model_defaults;
  const [d, update] = useDraft<ModelDraft>({ note: m.note, context_length: m.context_length, gpu_layers: m.gpu_layers, extra_args: m.extra_args.join(' ') },
    `model:${m.name}`, `모델 ${m.name}`, async (v) => {
      const r = await api.settings.models.update(m.name, v);
      if (r.restart_needed) setTimeout(() => restart(m.name), 300);
    });
  const st = FILE_STATE[m.state];
  const num = (v: string) => (v === '' ? null : Number(v));
  const reset = async () => {
    if (!(await ui.confirm({ title: `'${m.name}' 값을 기본값으로 되돌릴까요?`, message: 'settings.yaml 에 적힌 값으로 돌아갑니다.', confirmText: '되돌리기' }))) return;
    try {
      const r = await api.settings.models.reset(m.name);
      clear();
      await reload();
      if (r.restart_needed) restart(m.name);
    } catch (e) { ui.error(e); }
  };
  const remove = async () => {
    if (!(await ui.confirm({ title: `'${m.name}' 을 목록에서 뺄까요?`, message: '모델 파일(GGUF)은 지우지 않습니다. 다시 추가할 수 있습니다.', confirmText: '빼기', danger: true }))) return;
    try { await api.settings.models.remove(m.name); clear(); await reload(); } catch (e) { ui.error(e); }
  };
  return (
    <Section title={<><span className="title set-title">{m.name}</span>
      {m.active && <span className="badge ok">사용 중</span>}{st && <span className={`badge ${st[0]}`}>{st[1]}</span>}
      {m.managed && <span className="badge">직접 추가</span>}{m.overridden && <span className="badge warn">수정됨</span>}
      {m.size_gb && <span className="chip">{m.size_gb} GB</span>}</>}
      right={<>
        {!m.active && m.state === 'ready' && <button type="button" className="btn primary" onClick={() => onSwitch(m.name)}>이 모델로 바꾸기</button>}
        {m.active && <button type="button" className="btn" title="바꾼 값을 적용하려면 모델 서버를 다시 켜야 합니다" onClick={() => restart(m.name)}>다시 켜서 적용</button>}
        {m.overridden && <button type="button" className="btn" onClick={reset}>기본값으로</button>}
        {m.managed && !m.active && <button type="button" className="btn danger" onClick={remove}>목록에서 빼기</button>}
      </>}
      help={`파일: ${m.file}`}>
      <div className="field-grid">
        <Field label="설명"><input type="text" placeholder="예) 35B MoE · 가장 똑똑함" value={d.note} onChange={(e) => update({ ...d, note: e.target.value })} /></Field>
        <Field label="컨텍스트 길이" help="클수록 긴 작업을 하지만 VRAM(KV 캐시)을 더 씁니다. 개발 작업은 32768 권장">
          <input type="number" step={1} placeholder={`기본 ${def.context_length.toLocaleString()}`} value={d.context_length ?? ''} onChange={(e) => update({ ...d, context_length: num(e.target.value) })} />
        </Field>
        <Field label="GPU 층 (-ngl)" help="999 = 전부 GPU. VRAM 이 모자라면 낮춥니다">
          <input type="number" step={1} placeholder={`기본 ${def.gpu_layers}`} value={d.gpu_layers ?? ''} onChange={(e) => update({ ...d, gpu_layers: num(e.target.value) })} />
        </Field>
        <Field label="추가 인자" help="MoE 모델은 --n-cpu-moe N (N층의 전문가를 RAM 에). VRAM 이 남으면 N 을 줄이면 빨라집니다">
          <input type="text" placeholder="예) --n-cpu-moe 36" value={d.extra_args} onChange={(e) => update({ ...d, extra_args: e.target.value })} />
        </Field>
      </div>
      {m.overridden && m.base && (
        <div className="muted small" style={{ marginTop: 8 }}>
          settings.yaml 원래 값: 컨텍스트 {String(m.base.context_length ?? '기본')} · GPU 층 {String(m.base.gpu_layers ?? '기본')} · 추가 인자 {((m.base.extra_args as string[] | undefined) ?? []).join(' ') || '없음'}
        </div>
      )}
    </Section>
  );
}
