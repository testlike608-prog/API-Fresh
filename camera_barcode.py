"""
camera_barcode.py
-----------------
سكانر الباركود عن طريق الكاميرا المشتركة (camera_hub).

لا يفتح الكاميرا بنفسه — يقرأ الفريمات من camera_hub.get_frame()
عشان الكاميرا مش تتفتح أكتر من مرة.

بيضع الباركودات في نفس queue بتاعة scanner.py.

استخدام:
    import camera_barcode
    camera_barcode.start()    # تشغيل (camera_hub لازم يكون شغال)
    camera_barcode.stop()     # إيقاف
    camera_barcode.is_running()
"""

import cv2
import zxingcpp
import threading
import time
import logging

import scanner
from barcode_utils import normalize_barcode
import camera_hub   # المصدر المشترك

log = logging.getLogger("camera_barcode")

# ─── Internal state ───────────────────────────────────────────────────────────
_thread     = None
_stop_event = threading.Event()
_lock       = threading.Lock()

# كم decode في الثانية (ما نضغطش على المعالج)
DECODE_FPS  = 10


# ─── Decode loop ──────────────────────────────────────────────────────────────
def _decode_loop(stop_event: threading.Event):
    """
    يقرأ الفريمات من camera_hub ويحاول يكشف الباركود فيها.
    """
    frame_interval       = 1.0 / DECODE_FPS
    last_decode_at       = 0.0
    _last_queued_barcode = None
    _none_warn_at        = 0.0   # عشان ما نكررش الـ warning كل 50ms

    log.info("[CameraScanner] في انتظار باركود... (يقرأ من camera_hub)")

    while not stop_event.is_set():
        frame = camera_hub.get_frame()
        if frame is None:
            now = time.time()
            if now - _none_warn_at > 5.0:  # warning كل 5 ثواني بس
                log.warning("[CameraScanner] camera_hub مش بابعت فريمات — استنى...")
                _none_warn_at = now
            time.sleep(0.05)
            continue

        now = time.time()
        if now - last_decode_at < frame_interval:
            time.sleep(0.01)
            continue
        last_decode_at = now

        # ─── محاولة 1: صورة رمادية مباشرة ───
        gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        results = zxingcpp.read_barcodes(gray)

        if not results:
            # ─── محاولة 2: تحسين التباين ───
            enhanced = cv2.equalizeHist(gray)
            results  = zxingcpp.read_barcodes(enhanced)

        if not results:
            # ─── محاولة 3: Threshold ───
            _, thresh = cv2.threshold(gray, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            results   = zxingcpp.read_barcodes(thresh)

        for result in results:
            raw = result.text.strip()
            if not raw:
                continue

            barcode = normalize_barcode(raw)
            if not barcode:
                continue

            # ── منع التكرار المتتالي ────────────────────────────────────
            if barcode == _last_queued_barcode:
                continue
            _last_queued_barcode = barcode

            if raw != barcode:
                log.info(f"[CameraScanner] QR→SN: {raw!r}  →  {barcode!r}")

            scanner.queue_barcode.put(barcode)
            scanner.last_barcode = barcode
            scanner.flag_barcode = True
            log.info(f"[CameraScanner] ✅ باركود: {barcode!r}  ({result.format}) → queue")
            time.sleep(3)
            _last_queued_barcode = None  # بعد 3 ثواني نسمح لنفس الباركود يتكرر لو ظهر تاني 
           
            break   # نأخد أول باركود بس في الفريم

    log.info("[CameraScanner] أوقف.")


# ─── Public API ───────────────────────────────────────────────────────────────
def start(camera_index: int = None):
    """
    تشغيل الـ decode loop في ثريد خلفي.
    camera_hub يجب أن يكون شغالاً قبل الاستدعاء.
    camera_index محتجزة هنا للـ backward compatibility بس — مش بتُستخدم.
    """
    global _thread, _stop_event

    with _lock:
        if _thread is not None and _thread.is_alive():
            log.debug("[CameraScanner] شغالة بالفعل.")
            return

        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_decode_loop,
            args=(_stop_event,),
            name="camera-barcode-scanner",
            daemon=True,
        )
        _thread.start()
        log.info("[CameraScanner] بدأت.")


def stop():
    """إيقاف الـ decode loop."""
    global _thread

    with _lock:
        _stop_event.set()

    if _thread is not None:
        _thread.join(timeout=3.0)
        _thread = None

    log.info("[CameraScanner] أوقف.")


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def get_available_cameras(max_check: int = 6):
    """
    يكتشف الكاميرات المتاحة — مفيد لاختيار رقم الكاميرا في الـ GUI.
    """
    found = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


# ─── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cams = get_available_cameras()
    if not cams:
        print("❌ مفيش كاميرات.")
        sys.exit(1)

    print(f"✅ الكاميرات المتاحة: {cams}")
    cam_idx = cams[0]

    # لازم نشغّل الـ hub الأول
    camera_hub.start(camera_index=cam_idx)
    time.sleep(0.5)   # نستنى الكاميرا تفتح

    start()
    print("اضغط Ctrl+C للإيقاف...")
    try:
        while True:
            time.sleep(0.5)
            if not scanner.queue_barcode.empty():
                bc = scanner.queue_barcode.get_nowait()
                print(f">>> باركود: {bc}")
    except KeyboardInterrupt:
        stop()
        camera_hub.stop()
