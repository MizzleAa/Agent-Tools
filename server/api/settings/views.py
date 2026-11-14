"""설정 화면에 보여 줄 모양 만들기 (파일·기본값·사용처를 모아서)."""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from agent import config, harness, model_presets, prompts, settings_file, standards
from agent import workflow as workflows
from agent.profiles import MAX_TOOLS, Profile, harness_role, list_profiles, load_profile
from agent.tools import REGISTRY
from server import deps
from server.api.settings import schemas as sc

_BROKEN = (ValidationError, ValueError, OSError, yaml.YAMLError)


def profile_entry(name: str) -> sc.ProfileEntry:
    default = prompts.DEFAULT_AGENTS_DIR / f"{name}.yaml"
    if harness_role(name):   # 하네스 역할(마크다운)이 같은 이름의 yaml 보다 우선 — 하네스 탭에서 고친다
        base = dict(name=name, file=f"harness/agents/{name}.md", builtin=False)
        try:
            return sc.ProfileEntry(**base, harness=True, profile=load_profile(name))
        except _BROKEN as e:
            return sc.ProfileEntry(**base, harness=True, error=str(e))
    base = dict(name=name, file=f"config/agents/{name}.yaml", builtin=default.exists())
    try:
        p = load_profile(name)
    except _BROKEN as e:   # 손으로 고치다 깨진 파일도 목록에는 보여 준다
        return sc.ProfileEntry(**base, error=str(e))
    p = p.model_copy(update={"blocks": p.effective_blocks()})
    modified = None
    if default.exists():
        try:
            d = Profile.model_validate(yaml.safe_load(default.read_text(encoding="utf-8")))
            modified = d.model_copy(update={"blocks": d.effective_blocks()}).model_dump() != p.model_dump()
        except _BROKEN:
            modified = True
    return sc.ProfileEntry(**base, modified=modified, profile=p)


def workflow_entry(name: str) -> sc.WorkflowEntry:
    default = workflows.DEFAULT_DIR / f"{name}.yaml"
    base = dict(name=name, file=f"config/workflows/{name}.yaml", builtin=default.exists())
    try:
        wf = workflows.load_workflow(name)
    except _BROKEN as e:
        return sc.WorkflowEntry(**base, error=str(e))
    modified = None
    if default.exists():
        try:
            modified = workflows.Workflow.model_validate(yaml.safe_load(default.read_text(encoding="utf-8"))).model_dump() \
                != wf.model_dump()
        except _BROKEN:
            modified = True
    explicit = wf.model_copy(deep=True)
    for node, summary in zip(explicit.nodes, wf.summary()):
        node.after = summary["after"]   # 비어 있으면(바로 앞 노드) 실제 값으로 채워 화면이 그대로 그리게
    return sc.WorkflowEntry(**base, modified=modified, warnings=workflows.lint(wf), workflow=explicit)


def _usage(attr: str) -> dict[str, list[str]]:
    """블록 id(또는 도구 이름) → 그것을 쓰는 프로필 이름들."""
    used: dict[str, list[str]] = {}
    for name in list_profiles():
        try:
            p = load_profile(name)
        except _BROKEN:
            continue
        for item in (p.effective_blocks() if attr == "blocks" else p.tools):
            used.setdefault(item, []).append(name)
    return used


def block_users(block_id: str) -> list[str]:
    return _usage("blocks").get(block_id, [])


def settings_text() -> str:
    path = config.CONFIG_DIR / "settings.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def bundle() -> sc.SettingsBundle:
    s = deps.load_settings()
    p = prompts.load_prompts()
    used, tool_used = _usage("blocks"), _usage("tools")
    text = settings_text()
    raw: dict[str, Any] = yaml.safe_load(text) or {}
    return sc.SettingsBundle(
        profiles=[profile_entry(n) for n in list_profiles()],
        workflows=[workflow_entry(n) for n in workflows.list_workflows()],
        harnesses=[sc.HarnessInfo(id=h.id, title=h.title, kind=h.kind, companies=h.companies, roles=h.roles)
                   for h in harness.list_scopes()],
        blocks=[sc.BlockInfo(id=b.id, title=b.title, description=b.description, text=b.text, default=b.default,
                             dynamic=b.dynamic, custom=b.default is None and not b.dynamic, modified=b.modified,
                             used_by=used.get(b.id, [])) for b in p.blocks.values()],
        messages=[sc.MessageInfo(key=m.key, title=m.title, when=m.when, vars=m.vars, text=m.text, default=m.default,
                                 modified=m.modified) for m in p.messages.values()],
        tools=[sc.ToolInfo(name=t.name, permission=t.permission, default=t.description,
                           text=p.tools.get(t.name, t.description), modified=t.name in p.tools,
                           params=t.schema()["function"]["parameters"], used_by=tool_used.get(t.name, []))
               for t in sorted(REGISTRY.values(), key=lambda t: t.name)],
        variables=[sc.VariableInfo(name=v.name, value=v.value, description=v.description, default=v.default,
                                   custom=v.default is None, modified=v.modified) for v in p.variables.values()],
        builtin_vars=[sc.BuiltinVar(name=k, description=d) for k, d in prompts.BUILTIN_VARS.items()],
        params=sc.ParamsInfo(
            sections={k: sc.ParamSection(**v) for k, v in settings_file.PARAMS.items()},
            file=settings_file.file_values(text),
            effective={sec: getattr(s, sec).model_dump() for sec in settings_file.PARAMS},
            defaults={sec: type(getattr(s, sec))().model_dump() for sec in settings_file.PARAMS},
            active_model=s.backend.model),
        standards=[sc.StandardFile(name=f.name, chars=f.stat().st_size) for f in sorted(standards.STANDARDS_DIR.glob("*.md"))],
        models=[sc.ModelPresetInfo(**m) for m in model_presets.payload(s)],
        model_files=[sc.ModelFile(**f) for f in model_presets.model_files(s)],
        model_defaults=sc.ModelDefaults(
            context_length=(raw.get("backend") or {}).get("context_length") or config.Backend().context_length,
            gpu_layers=(raw.get("runtime") or {}).get("gpu_layers") or config.Runtime().gpu_layers),
        max_tools=MAX_TOOLS,
        max_nodes=workflows.MAX_NODES,
        context_length=s.backend.context_length,
    )
