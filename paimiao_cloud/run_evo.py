from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
INJECT = ROOT / "inject"
ENTRY = PROJECT / "code" / "evo_app.py"

def main():
    if not ENTRY.exists():
        raise SystemExit(f"找不到 Evo 主程序：{ENTRY}")

    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "ORT_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"

    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(INJECT)] + ([existing] if existing else [])
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    if str(INJECT) not in sys.path:
        sys.path.insert(0, str(INJECT))
    try:
        import sitecustomize
    except Exception as exc:
        print(f"[云同步] 注入失败，Evo 将照常运行：{exc!r}", file=sys.stderr)

    if str(PROJECT / "code") not in sys.path:
        sys.path.insert(0, str(PROJECT / "code"))

    sys.argv[0] = str(ENTRY)
    runpy.run_path(str(ENTRY), run_name="__main__")

if __name__ == "__main__":
    main()
