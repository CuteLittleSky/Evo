"""修 `app_settings.set_value(key, None)` 的语义（2026-11-24，用户报「录像页 500、记录出错」时查出来的）。

## 问题

上游 `code/app_settings.py` 的 `set_value` 是「把键设成这个值」：

    def set_value(key, value):
        data[key] = value

但**所有调用方**都把 `None` 当成「删掉这个键」来用（这是 Python 里很自然的写法）：

- `code/updater.py:143`、`code/data_move.py:116` 的 `take_result()`：读一次就消费掉，写完 `None`
- `code/update_check.py:177`、`paimiao_cloud/cloud/upstream.py:137`：清掉上次读到的版本
- `code/panel_recordings.py:740`：清掉用户选的录像文件夹

实际写进去的是 JSON 的 `null`，键还在。于是 `get(key, default)` 拿到的是 `None` 而不是 `default`
（`.get` 只在**键不存在**时才用默认值），`float(None)` 直接抛 TypeError。

用户实际撞上的两处：

    code/match_recording.py:192   self.limit = float(settings.get("recording_limit_gb", DEFAULT_LIMIT_GB)) * 2 ** 30
    code/panel_recordings.py:317  limit = float(app_settings.get("recording_limit_gb", DEFAULT_LIMIT_GB))

一次录像相关的设置改过（或测试清理过）以后，`recording_limit_gb` 变成 `null`，
记录程序每局开录都抛错，录像页直接 500。而且 `_start` 在赋值 `self.limit` 之前就抛了，
`close()` 里 `_finish` 再读 `self.limit` 又抛一次 `AttributeError`，两处堆栈叠在一起。

## 做法（纯注入，`code/app_settings.py` 一个字节都不动）

`set_value(key, None)` 改成**删掉这个键**——这正是所有调用方的意图，也是他们本来以为的行为。
顺带让 `get()` 把值本身是 `None` 的键也当作不存在（用户现在这份 settings.json 里已经存了一堆
`null`，删键只管以后；对已经写坏的，`get` 兜住，升级后立刻恢复正常）。

取值一律走 `get`，所以这一层就够了，不需要改任何调用方。

降级：挂不上只写日志，上游行为不变（只是那些记录项会读到 None，和现在一样）。
"""
from __future__ import annotations

MARK = "_paimiao_app_settings"
_INSTALLED = False
_said = set()


def _log(message):
    if message in _said:
        return
    _said.add(message)
    try:
        from cloud.state import log
        log(f"[设置项] {message}")
    except Exception:
        pass


def _install_for(module):
    """包 set_value 和 get：None = 删键；值为 None 的键当作没有。"""
    real_set = getattr(module, "set_value", None)
    real_get = getattr(module, "get", None)
    if real_set is None or real_get is None:
        _log("app_settings 里没有 set_value / get，跳过（设置项照上游行为）")
        return False
    if getattr(real_set, MARK, False):
        return True

    def set_value(key, value):
        if value is not None:
            return real_set(key, value)
        # None = 删键。整个读-改-写都在上游那把锁里做，免得和托盘/面板同时写互相覆盖
        import json  # noqa: PLC0415

        with module._lock:  # noqa: SLF001  上游自己的锁
            try:
                data = module.load()
            except Exception:
                data = {}
            if key not in data:
                return None
            data.pop(key, None)
            module.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            module.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return None

    def get(key, default=None):
        value = real_get(key, default)
        return default if value is None else value

    setattr(set_value, MARK, True)
    setattr(get, MARK, True)
    set_value.__wrapped__ = real_set  # type: ignore[attr-defined]
    get.__wrapped__ = real_get  # type: ignore[attr-defined]
    module.set_value = set_value
    module.get = get
    return True


def patch_module(module=None):
    if module is None:
        import app_settings as module  # noqa: PLC0415
    try:
        ok = _install_for(module)
    except Exception as exc:  # noqa: BLE001
        _log(f"挂不上（设置项照上游行为）：{exc!r}")
        return False
    if ok:
        _log("已接管：set_value(键, None) 删键；值为 None 的键当作没有")
    return ok


def install():
    """等 app_settings 导入后挂上。幂等。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True
    try:
        from cloud import importhook
        importhook.on_import("app_settings", patch_module, "app_settings 的 None")
    except Exception as exc:  # noqa: BLE001
        _INSTALLED = False
        _log(f"挂不上（设置项照上游行为）：{exc!r}")
        return False
    return True
