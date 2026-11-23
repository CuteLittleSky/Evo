"""画面识别的微调（纯注入，config/、code/screens.py 一个字节都不动）。

## 改了什么：总结页的检查区域往下加高一点（2026-11-23）

症状：16:10（以及 4:3）下认不出总结页，结算界面读不到（用户 2026-11-23 报的）。

原因：总结页的检查区域是 [750, 378, 180, 32]（1920×1080 基准），只罩住「击杀 / 助攻 / 击倒」
那一行字，上下余量很小。比 16:9 窄的画面卡片略往下一点点，按「居中」换算后这一行会掉到
检查区域下沿之外，只裁到半个字 → 外观相似度和参照图对不上（实测 0.18，门槛 0.35）→ 认不出。

改法：只把总结页检查区域的高度从 32 加到 52（往下多留 20 像素）。`screens.match()`
本来就让参照图在检查区域里**上下左右滑动取最像的位置**，所以框加高 = 白得上下容差，
不用逐像素滑、不用改参照图、不用改阈值。实测：
    用户 16:10 截图：0.180 → 0.842（过）；1920×1080 的 16:9：0.747 不变；2560×1440：0.884 → 0.979
    关键字的识别（has_keywords）照旧都对；对非总结页的误判 0/113；每次检查 0.15 → 0.21 毫秒。

只加高、不移动上沿：16:9 完全不走那条多出来的 20 像素（match 取最像位置），16:10 / 4:3 才用得上。

## 怎么挂

`importhook` 等 `screens` 导入后，包一层 `ScreenDetector.__init__`：实例建好以后把
`self.cfg["screens"]["summary"]["roi"]` 的高度补到 52。对每种语言的 detector 都生效
（GameScreens 给每种语言各建一个），大厅注入在它之后再把 ScreenDetector 换成子类，子类继承到这里。
降级：任何一步失败只写日志，识别照上游的行为走（16:10 下总结页照旧可能认不出）。
"""
from __future__ import annotations

SUMMARY = "summary"
MIN_HEIGHT = 52  # 总结页检查区域至少这么高（基准像素）；原来配置里是 32
MARK = "_paimiao_screen_tune"


def _log(message):
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(f"[画面识别微调] {message}")
    except Exception:
        pass


def patch_source(cfg):
    """把这份规则的总结页检查区域加高。返回改没改。"""
    try:
        spec = cfg["screens"][SUMMARY]
        roi = spec.get("roi")
        if not roi or len(roi) != 4 or roi[3] >= MIN_HEIGHT:
            return False
        spec["roi"] = [roi[0], roi[1], roi[2], MIN_HEIGHT]
        return True
    except Exception:
        return False


def _after_import(module):
    cls = getattr(module, "ScreenDetector", None)
    if cls is None or getattr(cls, MARK, False):
        return
    real_init = cls.__init__

    def __init__(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        try:
            patch_source(self.cfg)
        except Exception as exc:  # noqa: BLE001  加高失败就照上游
            _log(f"总结页检查区域加高失败（照上游识别）：{exc!r}")

    setattr(__init__, MARK, True)
    __init__.__wrapped__ = real_init  # type: ignore[attr-defined]
    cls.__init__ = __init__
    setattr(cls, MARK, True)
    _log(f"已接管：总结页检查区域加高到 {MIN_HEIGHT}（16:10 / 4:3 下认得出总结页）")


def install():
    """登记「等 screens 导入后挂上」。幂等。"""
    try:
        from cloud import importhook  # noqa: PLC0415
        importhook.on_import("screens", _after_import, label="画面识别微调")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"挂不上（识别照上游）：{exc!r}")
        return False
