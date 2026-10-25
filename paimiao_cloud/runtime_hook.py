import os
import sys

def _candidates():

    here = globals().get("__file__")
    if here:
        yield os.path.dirname(os.path.abspath(here))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        yield os.path.join(meipass, "paimiao_cloud")
        yield meipass
    if getattr(sys, "frozen", False):
        yield os.path.join(os.path.dirname(sys.executable), "_internal", "paimiao_cloud")
        yield os.path.dirname(sys.executable)
    yield os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "paimiao_cloud")

def _root():
    for folder in _candidates():
        if os.path.isfile(os.path.join(folder, "cloud", "bootstrap.py")):
            return folder
    return ""

try:
    _ROOT = _root()
    if _ROOT and _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import cloud.bootstrap as _bootstrap
    _bootstrap.install()
except Exception:

    pass
