"""在记录时就把经验值页归对局，不产生「同一局第二条」（上游 0 文件改动，注入）。

## 根在哪

一局按顺序：总结页（可能先读到一半、名次还可能 OCR 读错，先存下并标「待确认」）→ 经验值页
→ 总结页补全。上游 `post_match._use_xp` 决定经验值页归哪一局：

    经验值页名次（13）和总结页名次（读错的 1）对不上 → 判成「可能是另一局」
    → 单独存一局（只有经验值页，伤害/生存空）

这就是重复行的来源。而「对不上」里真正能说明是另一局的，只有**时间隔了两分钟以上**
（一局总结页到经验值页只隔几秒；超过 2 分钟基本就是另一局了）。**名次/击杀对不上**，
绝大多数是总结页刚存下时还没读准，同一局的可能性远大于另一局。

## 怎么治本：记录时就按同一局处理

`inject.py` 只改 `PostMatch._use_xp` 这一个决策点：
- 和这一局总结页的时间相差 **≥ 2 分钟** → 确实是另一局，按上游原来那样另存（该拆才拆）；
- 其余（名次/击杀对不上、时间差 < 2 分钟）→ **把经验值页补进当前这一局**，绝不另存一条。
  名次对不上交给 `settle` 标「待确认」，用户能改；但不会有两条。

这样从源头不会出现「同一局两条」，不需要任何「记录完再判重删掉」。
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from functools import wraps

TARGET = "post_match"
_INSTALLED = False
# 经验值页和这一局总结页相差这么久以上，才当作「另一局」（一局从总结页到经验值页只隔几秒）
DIFFERENT_GAME_SECONDS = 120.0


def install():
    """装注入（幂等）：已导入→当场换；导入中→包 loader；找不到→挂 finder（记录子进程里生效）。"""
    global _INSTALLED
    if _INSTALLED:
        return True
    module = sys.modules.get(TARGET)
    if module is not None:
        try:
            _patch(module)
        except Exception as exc:  # noqa: BLE001
            _log(f"[同一局] 上游已经导入，但没换上去：{exc!r}")
            return False
        _INSTALLED = True
        return True
    try:
        _wrap_loader_if_needed()
        _INSTALLED = True
        return True
    except _StillImporting:
        return True
    except ModuleNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        _log(f"[同一局] 包住上游 loader 失败：{exc!r}")
    try:
        sys.meta_path.insert(0, _Finder())
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"[同一局] 钩子没装上：{exc!r}")
        return False


def _patch(module):
    cls = getattr(module, "PostMatch", None)
    if cls is None or getattr(cls, "_paimiao_matchdedup", False):
        return
    _patch_use_xp(module, cls)
    cls._paimiao_matchdedup = True


def _patch_use_xp(module, cls):
    """记录时的归属决策：经验值页和这一局总结页相差 ≥2 分钟才算另一局，否则补进当前局。"""
    original = cls._use_xp

    @wraps(original)
    def _use_xp(self):
        try:
            ready = (not self.xp_done and self.xp is not None and not self.xp[1]
                     and self.match_id is not None)
            if ready:
                reading, _problems, path, when = self.xp
                gap = abs((when - self.summary[1]).total_seconds())
                if gap >= DIFFERENT_GAME_SECONDS:
                    # 隔了 2 分钟以上：确实是另一局 → 按上游「对不上」的做法另存（并标待确认）
                    note = (f"经验值页和这段时间的总结页对不上（时间相差 {gap / 60:.0f} 分钟），"
                            f"可能是另一局，请核对")
                    self._insert_xp_only(reading, True, [note], path, when, with_others=False)
                    self.xp_done = True
                    _log(f"[同一局] 经验值页比总结页晚 {gap / 60:.1f} 分钟，按另一局另存")
                    return
                # 2 分钟以内：同一局，补进当前局（名次/击杀对不上交给 settle 标待确认）
                conflicts = module.xp_conflicts(reading, when, *self.summary)
                self._apply(reading)
                self.xp_done = True
                if conflicts:
                    _log(f"[同一局] 经验值页和总结页对不上（{'；'.join(map(str, conflicts))}），"
                         f"但时间只差 {gap:.0f} 秒、按同一局补进编号 {self.match_id}（不另存）")
                return
        except Exception as exc:  # noqa: BLE001  补丁出错就按上游原样，绝不影响记录
            _log(f"[同一局] 归属判断补丁出错，按上游原样：{exc!r}")
        return original(self)

    cls._use_xp = _use_xp


# ---------- 子进程里的导入钩子（照抄 cloud.summary_rows.detect 的做法） ----------

class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if isinstance(spec.loader, _AfterImportLoader):
            return spec
        try:
            spec.loader = _AfterImportLoader(spec.loader)
            return spec
        except Exception:  # noqa: BLE001
            return spec


class _AfterImportLoader(importlib.abc.Loader):
    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            _patch(module)
        except Exception as exc:  # noqa: BLE001
            _log(f"[同一局] 注入失败（按上游原样，不影响记录）：{exc!r}")


class _StillImporting(RuntimeError):
    pass


def _wrap_loader_if_needed():
    if TARGET in sys.modules:
        raise _StillImporting(TARGET)
    spec = importlib.machinery.PathFinder.find_spec(TARGET)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(TARGET)
    if isinstance(spec.loader, _AfterImportLoader):
        return
    spec.loader = _AfterImportLoader(spec.loader)
    sys.modules[TARGET] = importlib.util.module_from_spec(spec)
    importlib._bootstrap._load(spec)  # noqa: SLF001


def _log(message):
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
    except Exception:
        pass
