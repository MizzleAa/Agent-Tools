"""설정 화면 '모델' 탭: 모델 목록에 GGUF 를 추가하고, 모델별 값(컨텍스트·GPU 층·추가 인자)을 바꾸고, 지운다.

settings.yaml 은 사람이 주석을 달아 관리하므로 다시 쓰지 않는다. 대신 config/connections.yaml 에
- models:           화면에서 추가한 모델 (지울 수 있다)
- model_overrides:  settings.yaml 에 있는 모델에 화면에서 바꾼 값 ('기본값으로'를 누르면 지운다)
를 두고 load_settings() 가 합친다. 모델 교체(고르기)는 connections.set_active_model + 모델 서버 다시 띄우기.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import yaml

from agent import config, connections
from agent.config import ModelPreset, Settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # llama-server --alias 로도 쓰인다
FIELDS = ("note", "context_length", "gpu_layers", "extra_args")
# 관리 서버가 직접 정하는 인자. 추가 인자로 바꾸면 에이전트가 모델 서버를 못 찾거나 (--port) 다른 PC 에 열린다 (--host)
RESERVED_ARGS = {"-m", "--model", "--alias", "-a", "--host", "--port", "-c", "--ctx-size", "-ngl", "--gpu-layers",
                 "--n-gpu-layers", "-np", "--parallel", "--api-key", "--api-key-file"}


def parse_args(text: str | list[str] | None) -> list[str]:
    """'--n-cpu-moe 36 --threads 8' → ['--n-cpu-moe', '36', '--threads', '8']"""
    if text is None or text == "":
        return []
    args = [str(a) for a in text] if isinstance(text, list) else shlex.split(str(text), posix=False)
    for a in args:
        flag = a.split("=", 1)[0]
        if flag in RESERVED_ARGS:
            raise ValueError(f"추가 인자에 {flag} 는 쓸 수 없습니다 (관리 서버가 직접 정합니다. "
                             "컨텍스트·GPU 층은 위 칸에서 고치세요)")
    return args


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in FIELDS:
        if key not in values:
            continue
        v = values[key]
        if key == "note":
            out[key] = str(v or "").strip()
        elif key == "extra_args":
            out[key] = parse_args(v)
        else:
            if v in (None, ""):
                out[key] = None
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 는 정수로 입력하세요: {v}") from None
            lo, hi = (2048, 262144) if key == "context_length" else (0, 999)
            if not lo <= n <= hi:
                raise ValueError(f"{'컨텍스트 길이' if key == 'context_length' else 'GPU 층'}는 {lo} ~ {hi} 사이로 입력하세요")
            out[key] = n
    return out


def model_files(settings: Settings) -> list[dict[str, Any]]:
    """models/ 폴더의 GGUF 중 아직 목록에 없는 것 (임베딩 모델은 대화용이 아니라 뺀다)."""
    folder = settings.path("models")
    used = {settings.path(m.file).resolve() for m in settings.models}
    out = []
    for p in sorted(folder.glob("*.gguf")) if folder.is_dir() else []:
        if p.resolve() in used or "embed" in p.name.lower():
            continue
        out.append({"file": f"models/{p.name}", "size_gb": round(p.stat().st_size / 2**30, 1),
                    "state": config.model_file_state(p), "suggested_name": suggest_name(p.name)})
    return out


def suggest_name(filename: str) -> str:
    """'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf' → 'qwen3.6-35b-a3b' (양자화 표시를 뗀다)"""
    stem = re.sub(r"\.gguf$", "", filename, flags=re.I)
    stem = re.sub(r"[-_.](UD[-_])?(I?Q\d[\w]*|F16|BF16|F32)$", "", stem, flags=re.I)
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-")[:64] or "model"


def _check_file(settings: Settings, file: str) -> str:
    file = str(file or "").strip().replace("\\", "/")
    if not file.lower().endswith(".gguf"):
        raise ValueError("모델 파일은 .gguf 여야 합니다")
    if not settings.path(file).is_file():
        raise ValueError(f"모델 파일이 없습니다: {settings.path(file)}")
    return file


def add(settings: Settings, data: dict, values: dict[str, Any]) -> dict:
    """새 모델을 목록에 추가한 connections 내용을 돌려준다 (쓰기는 부르는 쪽)."""
    name = str(values.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("모델 이름은 영어·숫자·. - _ 로 64자 이하 (예: qwen3.6-35b-a3b)")
    if any(m.name == name for m in settings.models):
        raise ValueError(f"'{name}' 모델이 이미 목록에 있습니다")
    item = {"name": name, "file": _check_file(settings, values.get("file")), **_clean(values)}
    item = {k: v for k, v in item.items() if v not in (None, "", [])}
    ModelPreset.model_validate(item)
    out = dict(data)
    out["models"] = [*(data.get("models") or []), item]
    return out


def update(settings: Settings, data: dict, name: str, values: dict[str, Any]) -> dict:
    preset = next((m for m in settings.models if m.name == name), None)
    if preset is None:
        raise ValueError(f"모델 목록에 없는 이름입니다: {name}")
    changes = _clean(values)
    out = dict(data)
    if preset.managed:   # 화면에서 추가한 모델: 그 항목을 고친다
        items = []
        for item in data.get("models") or []:
            if item.get("name") == name:
                item = {**item, **changes}
                item = {k: v for k, v in item.items() if v not in (None, "", [])}
                ModelPreset.model_validate(item)
            items.append(item)
        out["models"] = items
        return out
    base = _base_presets().get(name, {})   # settings.yaml 의 값과 다른 것만 남긴다
    current = {k: v for k, v in (data.get("model_overrides") or {}).get(name, {}).items()}
    for key, v in changes.items():
        default = base.get(key, [] if key == "extra_args" else ("" if key == "note" else None))
        if v == default:
            current.pop(key, None)
        else:
            current[key] = v
    overrides = dict(data.get("model_overrides") or {})
    if current:
        overrides[name] = current
    else:
        overrides.pop(name, None)
    out["model_overrides"] = overrides
    if not overrides:
        out.pop("model_overrides")
    return out


def reset(data: dict, name: str) -> dict:
    out = dict(data)
    overrides = dict(data.get("model_overrides") or {})
    overrides.pop(name, None)
    out["model_overrides"] = overrides
    if not overrides:
        out.pop("model_overrides")
    return out


def remove(settings: Settings, data: dict, name: str) -> dict:
    preset = next((m for m in settings.models if m.name == name), None)
    if preset is None:
        raise ValueError(f"모델 목록에 없는 이름입니다: {name}")
    if not preset.managed:
        raise ValueError("settings.yaml 에 적힌 모델은 화면에서 지울 수 없습니다 (파일에서 지우세요)")
    if name == settings.backend.model:
        raise ValueError("지금 쓰는 모델은 지울 수 없습니다. 다른 모델로 바꾼 뒤 지우세요")
    out = dict(data)
    out["models"] = [m for m in data.get("models") or [] if m.get("name") != name]
    return out


def _base_presets() -> dict[str, dict[str, Any]]:
    path = config.CONFIG_DIR / "settings.yaml"
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    return {m["name"]: m for m in raw.get("models") or [] if isinstance(m, dict) and "name" in m}


def payload(settings: Settings) -> list[dict[str, Any]]:
    """설정 화면에 보일 모델 목록 (실제로 적용되는 값 + settings.yaml 원래 값)."""
    base = _base_presets()
    out = []
    for m in settings.models:
        path = settings.path(m.file)
        out.append({
            "name": m.name, "file": m.file, "note": m.note, "context_length": m.context_length,
            "gpu_layers": m.gpu_layers, "extra_args": m.extra_args, "managed": m.managed, "overridden": m.overridden,
            "base": None if m.managed else {k: base.get(m.name, {}).get(k) for k in FIELDS},
            "state": config.model_file_state(path), "active": m.name == settings.backend.model,
            "size_gb": round(path.stat().st_size / 2**30, 1) if path.exists() else None,
        })
    return out


def write_text(data: dict) -> str:
    """connections.yaml 에 쓸 내용 (머리 주석 포함)."""
    return connections.text_of(data)


def read() -> dict:
    return connections._read()


def default_path() -> Path:
    return config.CONNECTIONS_FILE
