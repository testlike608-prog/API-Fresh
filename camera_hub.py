"""
camera_hub.py
-------------
مصدر الكاميرا المشترك — يفتح الكاميرا مرة واحدة بس
ويوزّع الفريمات على كل المستهلكين (camera_barcode + live_image).

API:
    start(camera_index)  → يبدأ التقاط الفريمات
    stop()               → يوقف الكاميرا
    get_frame()          → يرجع نسخة من آخر فريم (numpy array أو None)
    is_running()         → True لو شغالة
"""

import cv2
import threading
import time
import logging

log = logging.getLogger("camera_hub")

# ── إعدادات افتراضية ──────────────────────────────────────────────────
_DEFAULT_CAM_INDEX = 1
_FRAME_WIDTH       = 1280
_FRAME_HEIGHT      = 720

# ── state داخلي ──────────────────────────────────────────────────────
_stop_event    = threading.Event()
_thread        = None
_lock          = threading.Lock()          # يحمي _thread
_frame_lock    = threading.Lock()          # يحمي _latest_frame
_latest_frame  = None                      # آخر فريم اتقرأ
_cam_index     = _DEFAULT_CAM_INDEX        # رقم الكاميرا الحالية (للـ restart)


# ─────────────────────────────────────────────────────────────────────
def get_frame():
    """
    يرجع نسخة (copy) من آخر فريم التُقط.
    يرجع None لو الكاميرا لسه مبدأتش أو مفيش فريم.
    """
    with _frame_lock:
        return _latest_frame.copy() if _latest_frame is not None else None


def is_running():
    """يرجع True لو الـ capture loop شغال."""
    with _lock:
        return _thread is not None and _thread.is_alive()


def wait_for_frame(timeout: float = 5.0) -> bool:
    """
    يستنى لحد ما أول فريم يتقرأ (أو timeout).
    يرجع True لو جه الفريم، False لو انتهى الوقت بدون فريم.
    مهم تنده بعد start() وقبل ما camera_barcode يبدأ.
    """
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with _frame_lock:
            if _latest_frame is not None:
                log.info("camera_hub: ✓ أول فريم اتقرأ")
                return True
        _time.sleep(0.05)
    log.error(f"camera_hub: ✗ timeout {timeout}s — مفيش فريم! تأكد من رقم الكاميرا.")
    return False


# ─────────────────────────────────────────────────────────────────────
def _capture_loop(camera_index: int):
    """الـ loop الأساسي — يشتغل في ثريد منفصل، بيحدّث _latest_frame باستمرار."""
    global _latest_frame

    # نجرب DSHOW أولاً (أسرع على Windows) وإلا auto-detect
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        log.error(f"camera_hub: ❌ مش قادر أفتح الكاميرا {camera_index} — تأكد إنها متوصلة ومش مفتوحة ببرنامج تاني")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  _FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _FRAME_HEIGHT)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # نطبع رقم الكاميرا الحقيقي اللي اتفتحت عشان نتأكد
    log.info(f"camera_hub: ✅ Camera {camera_index} شغالة ({actual_w}x{actual_h}) — index={camera_index}")
    print(f"[camera_hub] opened camera index={camera_index} ({actual_w}x{actual_h})")

    try:
        while not _stop_event.is_set():
            ret, frame = cap.read()
            if ret:
                with _frame_lock:
                    _latest_frame = frame
            else:
                log.warning("camera_hub: فريم فاشل، هحاول تاني...")
                time.sleep(0.05)
                continue
            # ~100 fps max — كافي لكل المستهلكين
            time.sleep(0.01)
    except Exception as e:
        log.error(f"camera_hub: خطأ غير متوقع: {e}")
    finally:
        cap.release()
        with _frame_lock:
            _latest_frame = None
        log.info("camera_hub: الكاميرا اتقفلت.")


# ─────────────────────────────────────────────────────────────────────
def start(camera_index: int = None):
    """
    يبدأ التقاط الفريمات في ثريد خلفي.
    لو شغالة بالفعل مش بيعمل حاجة.
    """
    global _thread, _cam_index

    with _lock:
        if _thread is not None and _thread.is_alive():
            log.debug("camera_hub: start() — شغالة بالفعل")
            return

        # اقرأ رقم الكاميرا من config لو مش اتبعت
        if camera_index is None:
            try:
                from config import config as _cfg
                camera_index = int(_cfg.get("camera_index", _DEFAULT_CAM_INDEX))
            except Exception:
                camera_index = _DEFAULT_CAM_INDEX

        _cam_index = camera_index          # احفظ الرقم للـ restart
        _stop_event.clear()
        _thread = threading.Thread(
            target=_capture_loop,
            args=(camera_index,),
            name="camera-hub",
            daemon=True,
        )
        _thread.start()
        log.info(f"camera_hub: بدأت (كاميرا {camera_index})")


def stop(timeout: float = 3.0):
    """يوقف الكاميرا وينتظر الثريد ينتهي."""
    global _thread

    with _lock:
        if _thread is None or not _thread.is_alive():
            log.debug("camera_hub: stop() — مش شغالة")
            return
        _stop_event.set()
        t = _thread

    t.join(timeout=timeout)
    if t.is_alive():
        log.warning("camera_hub: الثريد لم ينتهِ في الوقت المحدد")
    else:
        log.info("camera_hub: أوقفت بنجاح")

    with _lock:
        _thread = None


def restart(camera_index: int = None):
    """
    يوقف الكاميرا ويشغّلها تاني برقم جديد.
    بيُستخدم لما يتغير camera_index في الـ config.
    """
    global _cam_index
    log.info(f"camera_hub: restarting (camera {camera_index})...")
    stop(timeout=3.0)
    time.sleep(0.2)   # استنى الـ driver يحرر الكاميرا
    start(camera_index=camera_index)
    ok = wait_for_frame(timeout=6.0)
    if ok:
        log.info(f"camera_hub: restarted successfully (camera {camera_index})")
    else:
        log.error(f"camera_hub: restart failed — camera {camera_index} لم تستجب")
    return ok


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cam = int(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CAM_INDEX
    start(camera_index=cam)
    print("اضغط Ctrl+C للإيقاف...")
    try:
        while True:
            f = get_frame()
            if f is not None:
                print(f"فريم: {f.shape}")
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
