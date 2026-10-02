# tests package
import glob
import os
import sys

# 테스트 실행 시 시스템 python3로 직접 실행되더라도 로컬 .venv의 패키지를 동적으로 로드
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_venv_dirs = glob.glob(os.path.join(_project_root, ".venv", "lib", "python*", "site-packages"))
for _p in _venv_dirs:
    if os.path.exists(_p) and _p not in sys.path:
        sys.path.append(_p)
