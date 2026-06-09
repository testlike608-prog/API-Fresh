"""
thread_logger.py
----------------
نظام لوجينج شامل للثريدز والكراش:

الأهداف:
  1. كل ثريد (سواء جديد أو قديم) لما يبدأ أو يخلص أو يكراش، يتسجل في ملف.
  2. أي Exception جوّا أي ثريد متلتقطش لوحدها → اللوجر هيكتب الـ traceback كامل.
  3. Watchdog كل فترة بيراقب الثريدز اللي شغّالة، ويسجل اللي اتولدت
     واللي ماتت (حتى اللي ماتت في صمت من غير exception).
  4. excepthook عالمي للـ main thread + للـ threads (Python 3.8+).
  5. اللوج بيتكتب في ملف rotating علشان مايكبرش بلا حدود.

الاستخدام:
    import thread_logger
    thread_logger.setup()           # في بداية main.py

    # بدل threading.Thread(target=...).start()
    thread_logger.LoggedThread(target=func, name="my-worker", daemon=True).start()

    # للوج يدوي
    log = thread_logger.get_logger()
    log.info("custom message")
"""

import os
import sys
import time
import threading
import traceback
import logging
from logging.handlers import RotatingFileHandler

# ─── إعدادات افتراضية ───────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = "threads.log"
MAX_BYTES = 5 * 1024 * 1024   # 5 ميجا لكل ملف
BACKUP_COUNT = 5              # هنحتفظ بـ 5 ملفات قديمة

# ─── State داخلي ────────────────────────────────────────────────────────────
_logger = None
_logger_lock = threading.Lock()
_watchdog_started = False
_watchdog_lock = threading.Lock()


# ─── Logger setup ───────────────────────────────────────────────────────────
def get_logger() -> logging.Logger:
    """Lazy-init للـ logger. آمن للنداء من أي ثريد، يرجع نفس الـ instance."""
    global _logger
    if _logger is not None:
        return _logger

    with _logger_lock:
        if _logger is not None:
            return _logger

        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, LOG_FILE)

        logger = logging.getLogger("threadlog")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)-8s] "
            "[%(threadName)s tid=%(thread)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # ملف rotating
        fh = RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        # كونسول (INFO وأعلى فقط علشان مايغرقش الشاشة)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        _logger = logger
        logger.info("=" * 70)
        logger.info(f"Logger initialised — log file: {log_path}")
        logger.info(f"Python {sys.version.split()[0]} on {sys.platform}")
        logger.info("=" * 70)
        return logger


# ─── LoggedThread: بديل drop-in لـ threading.Thread ─────────────────────────
class LoggedThread(threading.Thread):
    """
    استخدمها بدل threading.Thread.
    بتلف الـ target بحيث:
      - تلوج لما الثريد يبدأ ولما يخلص.
      - تلتقط أي Exception داخل الثريد وتكتب الـ traceback كامل
        (بدل ما الثريد يموت في صمت).

    استخدام مثال:
        LoggedThread(target=my_func, name="worker-1", daemon=True).start()

    لو عايز تطبع الـ args في اللوج للـ debugging، مرّر log_args=True.
    """

    def __init__(self, target=None, name=None, args=(), kwargs=None,
                 daemon=None, *, log_args=False):
        self._user_target = target
        self._user_args = args
        self._user_kwargs = kwargs or {}
        self._log_args = log_args

        # اسم افتراضي معبّر لو المستخدم مدّاش اسم
        if name is None and target is not None:
            name = getattr(target, "__qualname__", None) or getattr(target, "__name__", "LoggedThread")

        super().__init__(
            target=self._wrapped_run,
            name=name,
            daemon=daemon,
        )

    def _wrapped_run(self):
        log = get_logger()
        tname = self.name
        if self._log_args:
            log.info(f"▶ Thread STARTED  ({tname})  args={self._user_args}  kwargs={self._user_kwargs}")
        else:
            log.info(f"▶ Thread STARTED  ({tname})")
        try:
            if self._user_target is not None:
                self._user_target(*self._user_args, **self._user_kwargs)
            log.info(f"✓ Thread FINISHED ({tname})  — exited cleanly")
        except SystemExit as e:
            log.warning(f"⚠ Thread EXITED via SystemExit ({tname}) code={e.code}")
            raise
        except KeyboardInterrupt:
            log.warning(f"⚠ Thread interrupted by KeyboardInterrupt ({tname})")
            raise
        except Exception:
            log.exception(f"✗ Thread CRASHED ({tname}) — uncaught exception")


# ─── Watchdog: بيكتشف الثريدز اللي ماتت في صمت ──────────────────────────────
def _watchdog_loop(interval: float):
    log = get_logger()
    log.info(f"Watchdog started — checking every {interval}s")

    prev_threads = {}   # name -> thread object
    while True:
        try:
            current = {t.name: t for t in threading.enumerate()}
            current_names = set(current.keys())
            prev_names = set(prev_threads.keys())

            # ثريدز جديدة
            for name in (current_names - prev_names):
                t = current[name]
                log.debug(f"Watchdog: + thread appeared '{name}' (daemon={t.daemon})")

            # ثريدز اختفت (يحتمل تكون ماتت في صمت)
            for name in (prev_names - current_names):
                log.warning(
                    f"Watchdog: − thread DISAPPEARED '{name}' "
                    f"(if no FINISHED/CRASHED log above, it died silently)"
                )

            # سناب شوت دوري (debug فقط — مش هيظهر في الكونسول)
            log.debug(
                f"Watchdog snapshot: {len(current_names)} alive — {sorted(current_names)}"
            )

            prev_threads = current
        except Exception:
            log.exception("Watchdog tick failed")

        time.sleep(interval)


def start_watchdog(interval: float = 2.0):
    """يشغّل الـ watchdog مرة واحدة بس (مهما اتنادى)."""
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    t = threading.Thread(
        target=_watchdog_loop,
        args=(interval,),
        name="ThreadWatchdog",
        daemon=True,
    )
    t.start()


# ─── Excepthooks عالمية ─────────────────────────────────────────────────────
def _install_hooks():
    """يثبت excepthook للـ main thread + للـ threads العادية."""
    log = get_logger()

    # 1) hook للثريدز العادية (Python 3.8+) — شبكة أمان لأي ثريد
    #    ما اتعمليش wrap بـ LoggedThread.
    def thread_excepthook(args):
        tname = args.thread.name if args.thread else "<unknown>"
        tb = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        ))
        log.error(f"✗ Uncaught exception in thread '{tname}':\n{tb}")

    threading.excepthook = thread_excepthook

    # 2) hook للـ main thread / الـ process — يمسك أي crash نهائي
    prev_hook = sys.excepthook

    def main_excepthook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical(f"☠ FATAL crash in main thread:\n{tb}")
        if prev_hook:
            try:
                prev_hook(exc_type, exc_value, exc_tb)
            except Exception:
                pass

    sys.excepthook = main_excepthook


# ─── دالة الإعداد الرئيسية ──────────────────────────────────────────────────
def setup(watchdog_interval: float = 2.0):
    """
    اندهها مرة واحدة في بداية البرنامج (في main.py).
    - تهيّأ الـ logger وملف اللوج.
    - تثبت excepthooks للـ main + للثريدز.
    - تشغّل watchdog لمراقبة الثريدز.

    Returns: الـ logger نفسه علشان تقدر تستعمله بدري.
    """
    log = get_logger()
    _install_hooks()
    start_watchdog(interval=watchdog_interval)
    log.info("thread_logger setup complete ✓")
    return log
