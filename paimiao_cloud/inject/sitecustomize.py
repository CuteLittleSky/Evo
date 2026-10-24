import os
import sys

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "ORT_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    if _name not in os.environ:
        os.environ[_name] = "1"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import cloud.bootstrap as _bootstrap
    _bootstrap.install()
except Exception:

    pass
