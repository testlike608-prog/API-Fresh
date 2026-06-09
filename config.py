"""
config.py
---------
إدارة إعدادات البرنامج بحيث تكون قابله للتعديل من داخل الـ GUI
بدون ما نحتاج نعدل الكود أو نبني الـ exe من جديد.

الإعدادات بتتحفظ في ملف config.json:
- في وضع التطوير: جنب الـ .py files
- في وضع الـ exe: جنب الـ .exe (لأن الـ working dir بتكون مكان الـ exe)

الـ password بيتحفظ كـ SHA-256 hash، مش plain text.

استخدام:
    from config import config
    config.get("vision_trig_ip")        # IP بتاع Vision TRIG
    config.set("vision_trig_ip", "...")  # تعديل + حفظ تلقائي
    config.verify_password("admin")      # تحقق من الباسوورد
    config.set_password("new_password")  # تغيير الباسوورد
"""

import os
import sys
import json
import hashlib
import hmac
import threading
import shutil
from datetime import datetime


# ─── تحديد مكان الـ config file ─────────────────────────────────────
def _get_config_dir():
    """
    ترجع المجلد اللي فيه config.json.
    - لو شغّال كـ exe بـ Nuitka: المجلد جنب الـ exe
    - لو شغّال من source: المجلد بتاع الـ .py
    """
    if getattr(sys, "frozen", False):
        # Nuitka standalone — sys.executable هو الـ exe
        return os.path.dirname(os.path.abspath(sys.executable))
    # development mode
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_DIR = _get_config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


# ─── الـ defaults — نفس القيم اللي كانت hardcoded في الكود ──────────
DEFAULT_CONFIG = {
    "_meta": {
        "version":    "1.0",
        "created_at": "",
        "modified_at": "",
    },
    # ── Password (SHA-256 of "admin") ─────────────────────────────
    "password_hash":
        "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918",

    # ── TCP Clients ───────────────────────────────────────────────
    "vision_trig_ip":   "127.0.0.1",
    "vision_trig_port": 8081,
    "vision_id_ip":     "127.0.0.1",
    "vision_id_port":   8080,
    "cobot_ip":         "192.168.57.2",
    "cobot_port":       9000,

    # ── TCP Server ────────────────────────────────────────────────
    "trigger_server_ip":   "0.0.0.0",
    "trigger_server_port": 5000,

    # ── File paths (نسبياً للـ working dir) ───────────────────────
    "program_mapping_file": "program_mapping.xlsx",
    "results_report_file":  "results_report.xlsx",
    "vision_test_count": 6,

    # ── Result images ─────────────────────────────────────────────
    # الفولدر الأساسي اللي فيه صور النتيجة (مسار مطلق أو نسبي للـ working dir)
    "result_images_folder": "result_images",
    # فولدرات إضافية تتنسخ فيها نسخة من كل صورة نتيجة (نسخ احتياطية)
    "result_images_backup_folders": [],

    # ── Scan input mode ───────────────────────────────────────────
    # "manual"  → سكانر كيبورد عادي
    # "camera"  → كاميرا عادية بتقرأ الباركود بـ OpenCV + zxingcpp
    "scan_mode":    "manual",
    "camera_index": 0,

    # ── Intervals (بالثواني) ──────────────────────────────────────
    "watchdog_interval":         2.0,    # thread watchdog (thread_logger)
    "reconnect_check_interval":  3.0,    # TCPClient reconnect monitor
    "reconnect_retry_delay":     5.0,    # delay between reconnect attempts
    "debug_monitor_interval":    2.0,    # debug_monitor tick interval

    # ── TCP timeouts ──────────────────────────────────────────────
    "tcp_buffer_size": 4096,
}


# ─── Helper: hash password ──────────────────────────────────────────
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════
#                          Config class
# ═══════════════════════════════════════════════════════════════════
class Config:
    """
    Wrapper حول config dict بـ thread-safe load/save.
    """

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_CONFIG)
        self._listeners = []  # callbacks بتتنده لما حاجه تتغير
        self.load()

    # ─── Persistence ────────────────────────────────────────────────
    def load(self):
        """قراءة من الـ JSON file. لو الملف مش موجود نبنيه بالـ defaults."""
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    # ندمج مع الـ defaults عشان أي keys جديدة تتضاف تلقائياً
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(loaded)
                    # نتأكد إن الـ _meta موجود
                    merged.setdefault("_meta", dict(DEFAULT_CONFIG["_meta"]))
                    self._data = merged
                except Exception as e:
                    print(f"[CONFIG] Failed to load {self.path}: {e}. Using defaults.")
                    self._data = dict(DEFAULT_CONFIG)
                    self._data["_meta"]["created_at"] = datetime.now().isoformat()
            else:
                # أول مرة — نكتب الـ defaults
                self._data["_meta"]["created_at"] = datetime.now().isoformat()
                self._save_locked()

    def save(self):
        """حفظ الـ config الحالي. آمن للنداء من أي ثريد."""
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        """يفترض إن الـ _lock متاخد بالفعل."""
        self._data["_meta"]["modified_at"] = datetime.now().isoformat()
        try:
            # نكتب أولاً في ملف temp ثم نستبدله — ضد الـ corruption لو حصل crash
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            shutil.move(tmp, self.path)
        except Exception as e:
            print(f"[CONFIG] Failed to save {self.path}: {e}")

    # ─── Get / Set ──────────────────────────────────────────────────
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def get_all(self):
        """ترجع نسخة من كل الـ config (بدون hash الـ password)."""
        with self._lock:
            d = dict(self._data)
            # نخفي الـ hash من أي عرض
            d.pop("password_hash", None)
            return d

    def set(self, key, value, save=True):
        """تعديل قيمة + حفظ تلقائي. بيتنده الـ listeners."""
        with self._lock:
            old_value = self._data.get(key)
            if old_value == value:
                return False
            self._data[key] = value
            if save:
                self._save_locked()
        # ننده الـ listeners (برّه الـ lock عشان مايحصلش deadlock)
        for cb in self._listeners:
            try:
                cb(key, value)
            except Exception:
                pass
        return True

    def update_many(self, updates: dict):
        """تحديث كذا قيمة مرة واحدة (حفظ واحد)."""
        changes = []
        with self._lock:
            for key, value in updates.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changes.append((key, value))
            if changes:
                self._save_locked()
        for cb in self._listeners:
            for k, v in changes:
                try: cb(k, v)
                except Exception: pass
        return len(changes)

    # ─── Password ───────────────────────────────────────────────────
    def verify_password(self, password: str) -> bool:
        """تحقق من الباسوورد — بيستخدم hmac.compare_digest عشان يمنع timing attacks."""
        with self._lock:
            stored = self._data.get("password_hash", "")
        return hmac.compare_digest(_hash_password(password), stored)

    def set_password(self, new_password: str) -> bool:
        """تغيير الباسوورد. لازم يكون 4 chars على الأقل."""
        if not new_password or len(new_password) < 4:
            return False
        with self._lock:
            self._data["password_hash"] = _hash_password(new_password)
            self._save_locked()
        return True

    # ─── Listeners ──────────────────────────────────────────────────
    def add_listener(self, callback):
        """callback(key, value) — بيتنده لما يتعدل أي شيء."""
        self._listeners.append(callback)

    # ─── Reset ──────────────────────────────────────────────────────
    def reset_to_defaults(self, keep_password=True):
        """رجوع للـ defaults. الباسوورد بيتساب لو keep_password=True."""
        with self._lock:
            old_hash = self._data.get("password_hash") if keep_password else None
            self._data = dict(DEFAULT_CONFIG)
            if old_hash:
                self._data["password_hash"] = old_hash
            self._data["_meta"]["modified_at"] = datetime.now().isoformat()
            self._save_locked()


# ─── Singleton instance ─────────────────────────────────────────────
config = Config()


# ─── معلومات للتشخيص ────────────────────────────────────────────────
def info():
    """يطبع معلومات عن الـ config الحالي."""
    print("=" * 60)
    print(f"Config file: {config.path}")
    print(f"Config dir:  {CONFIG_DIR}")
    print(f"Frozen?      {getattr(sys, 'frozen', False)}")
    print("=" * 60)
    print(json.dumps(config.get_all(), indent=2, ensure_ascii=False))
    print("=" * 60)


if __name__ == "__main__":
    info()
