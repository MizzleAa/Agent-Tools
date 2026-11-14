# Python 규격 (3.12)

## 환경 확인
설치된 인터프리터의 버전과 위치, 이미 설치된 패키지 목록을 확인하고 보고에 적는다.
```
python --version
python -c "import sys; print(sys.executable)"
python -m pip list
```

## 프로젝트 구성
```
<project>/
  README.md
  pyproject.toml        # 또는 requirements.txt (버전 고정: pkg==x.y.z)
  src/<pkg>/__init__.py
  src/<pkg>/__main__.py # 진입점 → python -m <pkg>
  tests/test_*.py
  .gitignore            # .venv/  __pycache__/  *.pyc
```
- 작은 프로그램은 `src/` 없이 `<pkg>/`만 두거나 `main.py` 한 파일도 허용한다. 이때도 `tests/`와 의존성 파일은 둔다.

## 의존성
- 프로젝트 전용 가상환경(`<project>/.venv`)을 둔다. 실행과 테스트는 그 인터프리터로 한다.
- 쓰는 패키지는 모두 의존성 파일에 버전과 함께 적는다.
- 판정용 개발 도구(`pytest`, `pyflakes`)도 프로젝트 .venv 에 설치하고, 의존성 파일에 개발용으로 구분해 적는다.
- **금지**: 가상환경 밖 시스템 인터프리터에 설치, `pip install --user`

## 판정 명령
| 단계 | 명령 |
|---|---|
| V1 ① 문법 | `.venv\Scripts\python -m compileall -q src tests` |
| V1 ② 정의되지 않은 이름 | `.venv\Scripts\python -m pyflakes src tests` → `undefined name` 0건 (pyflakes 는 프로젝트 .venv 에 개발용으로 설치) |
| V1 ③ 모듈 로드 | 패키지의 **모든 모듈**을 import 해서 오류 0. 예: `.venv\Scripts\python -c "import importlib, pkgutil, <pkg>; [importlib.import_module(m.name) for m in pkgutil.walk_packages(<pkg>.__path__, '<pkg>.')]"` |
| V2 테스트 | `.venv\Scripts\python -m pytest -q` |
| 실행 | `.venv\Scripts\python -m <pkg>` (README에 적은 명령) |

## 끝나지 않는 프로그램 (V3)
`00_공통.md` 4장을 따른다. 테스트는 정해진 시간 안에 스스로 종료해야 하고, 관측한 값을 남겨야 한다. 타임아웃으로 강제 종료되더라도 그때까지의 출력이 남아야 한다.

## 정리 대상
`__pycache__/`, `_scratch/`, 쓰지 않는 패키지
