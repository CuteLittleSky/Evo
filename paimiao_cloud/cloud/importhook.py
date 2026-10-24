from __future__ import annotations

import sys
import traceback

_WAITING: dict[str, list[tuple[str, object]]] = {}

_FIRED: set[str] = set()

_HOOK_ATTR = "_paimiao_shared_import_hook"

def _log(message):

    wrote = False
    try:
        from cloud import state as cloud_state

        cloud_state.log(message)
        wrote = True
    except Exception:
        pass
    if not wrote:
        try:
            print(message, flush=True)
        except Exception:
            pass

class _DispatchLoader:

    def __init__(self, inner, fullname):
        self._inner = inner
        self._fullname = fullname

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        _fire(self._fullname, module)

    def __getattr__(self, name):

        return getattr(self._inner, name)

class _SharedFinder:

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _WAITING:
            return None
        for finder in sys.meta_path:

            if getattr(finder, _HOOK_ATTR, False):
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is not None:
                spec.loader = _DispatchLoader(spec.loader, fullname)
                return spec
        return None

    _paimiao_injection_hook = True

def _ensure_finder():
    for finder in sys.meta_path:
        if getattr(finder, _HOOK_ATTR, False):
            return finder
    finder = _SharedFinder()
    setattr(finder, _HOOK_ATTR, True)
    sys.meta_path.insert(0, finder)
    return finder

def _fire(fullname, module):
    if fullname in _FIRED:
        return
    _FIRED.add(fullname)
    for label, callback in list(_WAITING.get(fullname, [])):
        try:
            callback(module)
        except BaseException as exc:
            try:
                where = traceback.extract_tb(exc.__traceback__)
                spot = (f"{where[-1].filename.rsplit(chr(92), 1)[-1]}:{where[-1].lineno}"
                        if where else "?")
                _log(f"[注入] {label} 在 {fullname} 导入后没跑成："
                     f"{type(exc).__name__}: {exc} @ {spot}")
            except Exception:
                pass

def on_import(fullname: str, callback, label: str = "") -> None:

    label = label or getattr(callback, "__module__", "?")
    bucket = _WAITING.setdefault(fullname, [])
    if all(cb is not callback for _, cb in bucket):
        bucket.append((label, callback))

    module = sys.modules.get(fullname)
    if module is not None:
        _fire(fullname, module)
        return
    _ensure_finder()

def reset_for_tests():

    _WAITING.clear()
    _FIRED.clear()
