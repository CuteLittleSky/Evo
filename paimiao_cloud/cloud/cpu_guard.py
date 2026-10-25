from __future__ import annotations

import os
import sys

MARK = "_paimiao_cv_threads"

THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "ORT_NUM_THREADS", "NUMEXPR_NUM_THREADS")

def set_thread_env():

    changed = []
    for name in THREAD_ENV:
        if os.environ.get(name) != "1":
            os.environ[name] = "1"
            changed.append(name)
    return changed

def apply():

    import cv2

    if getattr(cv2, MARK, False):
        return cv2.getNumThreads()
    before = cv2.getNumThreads()
    try:
        cv2.setNumThreads(1)

        cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass
    setattr(cv2, MARK, True)
    return before

def install(module=None):

    env_changed = set_thread_env()
    if env_changed:
        _log("线程限制：%s 设成 1（避免每个 OCR/OpenCV 调用拉起一整池线程空转）"
             % "、".join(env_changed))
    try:
        before = apply()
    except Exception as exc:
        _log("OpenCV 线程数没设置成 1（Evo 本身不受影响）：%r" % (exc,))
        return False
    if before != 1:
        _log("OpenCV 线程数 %s → 1（避免 32 个线程自旋空转烧 CPU）" % (before,))
    return True

def _log(message):
    try:
        from cloud.state import log
        log(message)
    except Exception:
        try:
            print("[OpenCV 线程数] " + message, file=sys.stderr)
        except Exception:
            pass
