"""대화 작업 폴더의 경계 (QA E1·E3).

대화마다 workspace 아래에 전용 폴더가 생기고 명령도 그 안에서 돈다. 그런데 소형 모델은 시스템 프롬프트의
절대 경로를 옮겨 적다 이름을 틀려(잘린 이름) workspace 에 '옆 폴더'를 새로 만들고 거기서 일했다.
샌드박스는 workspace 전체가 rw 라 허용했고, 규격 점검은 대화 폴더만 봐서 모델은 지적을 이해하지 못했다.

- 파일 도구: 대화 폴더 밖(같은 workspace 안)에는 쓰지 않는다 — 사용자가 요청에 그 폴더를 적은 경우만 예외.
- 명령: workspace 에 아직 없는 폴더를 가리키는 절대 경로가 있으면 실행하지 않는다 (옆 폴더를 새로 만드는 길).
- 명령을 실행한 뒤 workspace 에 새 폴더가 생겼으면 알린다 (상대 경로 '..\\x' 나 코드로 만든 경우).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_STOP = set("\\/'\"`;|&) \t\r\n")


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


class Boundary:
    def __init__(self, workspace: Path, session: Path, task: str):
        self.root = _real(workspace)
        self.session = _real(session)
        self.task = (task or "").lower()
        self.before = self._children()
        self.created: list[str] = []          # 이번 실행에서 대화 폴더 밖에 새로 생긴 것
        self.outside: dict[str, str] = {}     # 대화 폴더 밖에 쓰거나 만든 것 → 어떻게 (점검 메시지에 씀)

    def _children(self) -> set[str]:
        try:
            return {p.name for p in self.root.iterdir()}
        except OSError:
            return set()

    @property
    def name(self) -> str:
        return self.session.name

    def _top(self, path: Path) -> str | None:
        """대화 폴더 밖이면서 workspace 안이면 workspace 바로 아래 이름, 아니면 None."""
        real = _real(path)
        if real == self.session or real.is_relative_to(self.session) or not real.is_relative_to(self.root) or real == self.root:
            return None
        return real.relative_to(self.root).parts[0]

    def _asked(self, name: str) -> bool:
        return bool(name) and name.lower() in self.task

    def where(self) -> str:
        return f"'{self.name}' (명령의 현재 폴더 '.')"

    # ───────────────────────── 파일 도구

    def check_write(self, path: Path) -> str | None:
        top = self._top(path)
        if top is None or self._asked(top):
            return None
        self.outside.setdefault(top, "파일 저장 시도 (막음)")
        guess = " 경로를 옮겨 적다 폴더 이름이 잘린 것 같습니다." if self.name.lower().startswith(top.lower()[:12]) else ""
        moved = (" 이 대화에서 만든 것이 그 폴더에 있다면 Move-Item 으로 작업 폴더 안으로 옮긴 뒤 계속하세요."
                 if top in self.before else "")
        return (f"차단됨: 대화 작업 폴더 밖이라 쓰지 않았습니다 ({top}\\…). 이 대화의 작업 폴더는 {self.where()} 입니다. "
                f"파일은 상대 경로로 이 폴더 안에 만드세요 — 현재 폴더가 곧 작업 폴더라 폴더 이름을 앞에 붙이지 않습니다 "
                f"(예: path='{Path(path).name}').{guess}{moved}")

    # ───────────────────────── 명령

    def check_command(self, text: str) -> str | None:
        """명령·코드에 workspace 의 없는 폴더(= 새로 만들 옆 폴더)를 가리키는 절대 경로가 있으면 거절 사유."""
        norm = text.replace("/", "\\")
        low = norm.lower()
        root = str(self.root).lower() + "\\"
        known = sorted({self.name, *self._children()}, key=len, reverse=True)   # 이미 있는 폴더는 읽기·옮기기에 쓸 수 있다
        at = low.find(root)
        while at >= 0:
            rest = norm[at + len(root):]
            hit = next((n for n in known if rest.lower().startswith(n.lower())
                        and (len(rest) == len(n) or rest[len(n)] in _STOP)), None)
            if hit is None:
                first = re.split(r"[\\'\"`;|&\r\n]", rest, maxsplit=1)[0].strip()
                if first and not self._asked(first):
                    self.outside.setdefault(first, "명령에서 가리킴 (막음)")
                    return (f"차단됨: 명령이 workspace 에 없는 폴더를 가리킵니다 ('{first}'). 대화 작업 폴더 밖에 새 폴더를 "
                            f"만들지 않습니다. 이 대화의 작업 폴더는 {self.where()} 이고 명령은 그 안에서 실행되니 "
                            "절대 경로를 옮겨 적지 말고 상대 경로로 쓰세요.")
            at = low.find(root, at + 1)
        return None

    def after_command(self) -> str | None:
        """명령 뒤 workspace 에 새로 생긴 폴더·파일이 있으면 알림 (모델에게)."""
        new = sorted(self._children() - self.before - set(self.created) - {self.name})
        if not new:
            return None
        self.created += new
        for n in new:
            self.outside[n] = "명령으로 새로 생김"
        listed = ", ".join(f"'{n}'" for n in new)
        return (f"⚠ 대화 작업 폴더 밖에 새로 생겼습니다: {listed}. 이 대화의 작업 폴더는 {self.where()} 입니다. "
                "거기 만든 것은 Move-Item 으로 작업 폴더 안으로 옮기고, 앞으로는 상대 경로로 쓰세요.")

    def summary(self) -> str:
        """규격 점검 메시지에 붙일 '작업 폴더 밖에서 한 일' (없으면 빈 글자)."""
        if not self.outside:
            return ""
        items = ", ".join(f"'{n}'({how})" for n, how in list(self.outside.items())[:4])
        return (f"작업 폴더 밖에서 한 일이 있습니다: {items}. 점검은 대화 작업 폴더 {self.where()} 안만 봅니다 — "
                "산출물을 작업 폴더 안으로 옮기세요")
