"""에이전트 저장소 자체의 pytest 설정: tests/ 만 모은다.

pytest 설정을 pyproject.toml·pytest.ini 에 두지 않는 이유 (QA D9)
- pytest 는 테스트 위치에서 위로 올라가며 설정 파일을 찾고, 없으면 가장 가까운 pyproject.toml 폴더를 기준(rootdir)으로 삼는다.
- 이 저장소 안의 workspace/ 에서 에이전트가 만든 사용자 프로젝트가 그래서 이 저장소 설정(pythonpath·testpaths)을 물려받았다.
- conftest.py 는 기준 폴더 위쪽 것은 읽히지 않으므로 사용자 프로젝트에 영향이 없다.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    """저장소 루트에서 pytest 를 돌리면 tests/ 만 모은다 (workspace/·data/ 의 사용자 프로젝트 테스트는 건드리지 않는다)."""
    try:
        rel = Path(collection_path).resolve().relative_to(ROOT)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return None if rel.parts[0] == "tests" else True
