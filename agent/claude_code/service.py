"""프로젝트·세션 관리 + 서비스 객체 하나 (관리 서버 이벤트 루프 안에서 처음 쓸 때 만든다).

데이터 폴더: data/claude_code/ (테스트는 AGENT_CLAUDE_CODE_HOME 으로 바꾼다).
프로젝트·세션 '삭제'는 이 목록에서만 지운다 — 폴더와 Claude Code 세션 기록은 그대로 둔다.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import list_sessions

from .cli import CliLocator, scrub_parent_session_env
from .errors import AppError
from .hub import EventHub
from .manager import DEFAULT_TITLE, IDLE, SessionManager
from .settings import ALLOWED_PERMISSION_MODES, Settings
from .store import Database


def home_dir() -> Path:
    override = os.environ.get("AGENT_CLAUDE_CODE_HOME")
    if override:
        return Path(override)
    from agent.config import load_settings

    return load_settings().data_path / "claude_code"


def normalize_dir(path: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve())


@dataclass
class Service:
    db: Database
    settings: Settings
    hub: EventHub
    locator: CliLocator
    manager: SessionManager
    home: Path

    # ---- 프로젝트 -----------------------------------------------------------
    def project_out(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "path_exists": Path(row["path"]).is_dir()}

    def require_project(self, project_id: str) -> dict[str, Any]:
        row = self.db.get_project(project_id)
        if not row:
            raise AppError(404, "project_not_found", "프로젝트를 찾을 수 없습니다.")
        return row

    def create_project(self, path: str, name: str | None) -> dict[str, Any]:
        norm = normalize_dir(path)
        if not Path(norm).is_dir():
            raise AppError(400, "path_not_found", f"폴더가 없습니다: {norm}")
        existing = self.db.find_project_by_path(norm)
        if existing:
            raise AppError(409, "project_exists", "이미 추가한 프로젝트 폴더입니다.", {"project_id": existing["id"]})
        row = self.db.create_project((name or "").strip() or Path(norm).name or norm, norm)
        out = self.project_out(row)
        self.hub.publish("project.updated", None, {"project": out})
        return out

    def update_project(self, project_id: str, name: str | None, path: str | None) -> dict[str, Any]:
        row = self.require_project(project_id)
        fields: dict[str, Any] = {}
        if name is not None and name.strip():
            fields["name"] = name.strip()
        old_path_sessions = 0
        if path is not None and path.strip():
            norm = normalize_dir(path)
            if not Path(norm).is_dir():
                raise AppError(400, "path_not_found", f"폴더가 없습니다: {norm}")
            other = self.db.find_project_by_path(norm)
            if other and other["id"] != project_id:
                raise AppError(409, "project_exists", "다른 프로젝트가 이미 이 폴더를 쓰고 있습니다.", {"project_id": other["id"]})
            if os.path.normcase(norm) != os.path.normcase(row["path"]):
                fields["path"] = norm
                old_path_sessions = sum(1 for s in self.db.list_sessions(project_id)
                                        if os.path.normcase(s["working_directory"]) != os.path.normcase(norm))
        updated = self.db.update_project(project_id, **fields) if fields else row
        out = self.project_out(updated)  # type: ignore[arg-type]
        self.hub.publish("project.updated", None, {"project": out})
        return {"project": out, "sessions_on_old_path": old_path_sessions}

    async def delete_project(self, project_id: str) -> None:
        self.require_project(project_id)
        sessions = self.db.list_sessions(project_id)
        busy = [s["id"] for s in sessions if s["id"] in self.manager.busy_session_ids()]
        if busy:
            raise AppError(409, "project_busy", "응답을 생성 중인 세션이 있어 지금은 삭제할 수 없습니다.", {"sessions": busy})
        for s in sessions:
            await self.manager.delete_session(s["id"])
        self.db.delete_project(project_id)
        self.hub.publish("project.deleted", None, {"project_id": project_id})

    # ---- 세션 ---------------------------------------------------------------
    def create_session(self, project_id: str, title: str | None, model: str | None, mode: str | None,
                       working_directory: str | None = None, profile: str | None = None, effort: str | None = None,
                       harness: str | None = None, engine: str = "claude") -> dict[str, Any]:
        from .engines import ENGINES

        if engine not in ENGINES:
            raise AppError(400, "bad_engine", f"모르는 CLI 엔진입니다: {engine}")
        project = self.require_project(project_id)
        if not Path(working_directory or project["path"]).is_dir():
            raise AppError(409, "path_not_found", f"작업 폴더가 없습니다: {working_directory or project['path']}")
        cfg = self.settings.all()
        row = self.db.create_session(
            project_id=project_id,
            claude_session_id=str(uuid.uuid4()),
            title=(title or "").strip() or DEFAULT_TITLE,
            working_directory=working_directory or project["path"],
            status=IDLE,
            model=(model if model is not None else cfg["default_model"] if engine == "claude" else None) or None,
            permission_mode=mode if mode in ALLOWED_PERMISSION_MODES else cfg["default_permission_mode"],
            profile=profile or None,
            effort=effort or None,
            harness=harness or None,
            engine=engine,
        )
        self.db.touch_project(project_id, row["id"])
        out = self.manager.session_out(row)
        self.hub.publish("session.updated", row["id"], {"session": out})
        return out

    def new_conversation(self, workspace: Path, task: str, model: str | None, mode: str | None, session_folders: bool,
                         profile: str | None = None, effort: str | None = None, harness: str | None = None,
                         engine: str = "claude") -> dict[str, Any]:
        """새 CLI 대화: 에이전트의 새 작업과 같은 규칙으로 작업 폴더를 만든다 (workspace/날짜_시각_요청 첫 문장 — agent/session.py).
        목록에서는 workspace 자체를 '작업 폴더' 프로젝트 하나로 묶는다."""
        from agent.session import create_session_dir

        workspace.mkdir(parents=True, exist_ok=True)
        root = normalize_dir(str(workspace))
        project = self.db.find_project_by_path(root) or self.create_project(root, "작업 폴더 (workspace)")
        wd = create_session_dir(Path(root), task) if session_folders else Path(root)
        return self.create_session(project["id"], None, model, mode, working_directory=str(wd), profile=profile, effort=effort,
                                   harness=harness, engine=engine)

    def import_session(self, project_id: str, claude_session_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        existing = self.db.find_session_by_claude_id(claude_session_id)
        if existing:
            return self.manager.session_out(existing)
        info = self.manager.history.info(claude_session_id, project["path"])
        if info is None:
            raise AppError(404, "claude_session_not_found", "Claude Code 세션 기록을 찾을 수 없습니다.")
        row = self.db.create_session(
            project_id=project_id,
            claude_session_id=claude_session_id,
            title=(info.custom_title or info.summary or info.first_prompt or "가져온 세션")[:200],
            working_directory=info.cwd or project["path"],
            status=IDLE,
            permission_mode=self.settings.get("default_permission_mode"),
            has_transcript=True,
            origin="imported",
        )
        out = self.manager.session_out(row)
        self.hub.publish("session.updated", row["id"], {"session": out})
        return out

    async def project_sessions(self, project_id: str, include_external: bool) -> dict[str, Any]:
        project = self.require_project(project_id)
        sessions = [self.manager.session_out(r) for r in self.db.list_sessions(project_id)]
        external: list[dict[str, Any]] = []
        err = None
        if include_external and Path(project["path"]).is_dir():
            known = {r["claude_session_id"] for r in self.db.list_sessions()}
            try:
                for i in await asyncio.to_thread(list_sessions, project["path"], 200):
                    if i.session_id in known:
                        continue
                    external.append({
                        "claude_session_id": i.session_id,
                        "title": (i.custom_title or i.summary or i.first_prompt or i.session_id)[:200],
                        "first_prompt": (i.first_prompt or "")[:300] or None,
                        "last_modified": i.last_modified,
                        "created_at": i.created_at,
                        "cwd": i.cwd,
                        "git_branch": i.git_branch,
                        "file_size": i.file_size,
                    })
            except Exception as e:  # noqa: BLE001
                err = f"Claude Code 세션 목록을 읽지 못했습니다: {e}"
        return {"sessions": sessions, "external": external, "external_error": err}


_service: Service | None = None


def get_service() -> Service:
    """처음 부를 때 만든다 (관리 서버의 이벤트 루프 안에서 — 백그라운드 정리 작업이 그 루프에서 돈다)."""
    global _service
    if _service is None:
        scrub_parent_session_env()
        home = home_dir()
        home.mkdir(parents=True, exist_ok=True)
        db = Database(home / "claude_code.db")
        settings = Settings(db)
        hub = EventHub()
        locator = CliLocator()
        manager = SessionManager(db, settings, hub, locator, home / "uploads")
        manager.recover()
        manager.start_background()
        _service = Service(db, settings, hub, locator, manager, home)
    return _service


def started() -> bool:
    return _service is not None


async def shutdown() -> None:
    """관리 서버가 꺼질 때: CLI 프로세스를 정상 종료한다 (세션 기록은 CLI 가 이미 저장)."""
    global _service
    if _service is None:
        return
    svc, _service = _service, None
    await svc.manager.shutdown(force=True)
    svc.db.close()
