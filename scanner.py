import keyboard
import time
import queue
import threading

from barcode_utils import normalize_barcode

# ─── Public API (للاستخدام من باقي الموديولز) ────────────────────────────────
queue_barcode = queue.Queue()
flag_barcode = False

# ─── Internal state ──────────────────────────────────────────────────────────
_recorded_keys = []
_listener_started = False
_listener_lock = threading.Lock()

last_barcode = None  # لتخزين آخر


def _on_key_event(e):
    """Callback اللي بيتنادى مع كل ضغطة على الكيبورد."""
    global flag_barcode, last_barcode

    if e.event_type == keyboard.KEY_DOWN:
        if e.name == 'enter':  # عادة الإسكانر يرسل زر Enter بعد قراءة الباركود
            raw = "".join(_recorded_keys)
            _recorded_keys.clear()
            if raw:
                barcode = normalize_barcode(raw)
                if not barcode:
                    pass  # فضي بعد الـ normalize — تجاهل
                #elif barcode == last_barcode:
                    #print(f"تم قراءة نفس الباركود مرة أخرى: {barcode} — تجاهل")
                else:
                    if raw != barcode:
                        print(f"QR→SN: {raw!r}  →  {barcode!r}")
                    queue_barcode.put(barcode)
                    last_barcode = barcode
                    flag_barcode = True
                    print(f"تمت قراءة الباركود: {barcode}")
        elif len(e.name) == 1:  # لتجاهل أزرار زي Shift و CapsLock
            _recorded_keys.append(e.name)


def start_listener():
    """تشغيل الـ keyboard hook في الباك جراوند (مش blocking)."""
    global _listener_started
    with _listener_lock:
        if _listener_started:
            return
        try:
            keyboard.hook(_on_key_event)
            _listener_started = True
            print("Scanner listener started — waiting for barcode...")
        except Exception as e:
            print(f"⚠ Scanner listener could not start: {e}")


def stop_listener():
    """إيقاف الـ keyboard hook."""
    global _listener_started
    with _listener_lock:
        if not _listener_started:
            return
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        _listener_started = False


def reset_queue():
    """تصفير الكيو والعلم قبل عملية فحص جديدة."""
    global flag_barcode
    flag_barcode = False
    while not queue_barcode.empty():
        try:
            queue_barcode.get_nowait()
        except queue.Empty:
            break


# ─── Standalone mode (لو شغلت الملف لوحده للاختبار) ─────────────────────────
if __name__ == "__main__":
    print("في انتظار قراءة الباركود (سيتم التقاطه ككيبورد)...")
    print("اضغط ESC لإيقاف البرنامج.")
    start_listener()
    keyboard.wait('esc')
    stop_listener()
