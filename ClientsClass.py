from cmath import log
import socket
import threading
import time
import queue
try:
    import pyodbc
except ModuleNotFoundError:
    pyodbc = None
import os
import re
import textwrap
from datetime import datetime
import csv
import pandas as pd
from openpyxl import load_workbook
import openpyxl
from openpyxl.styles import Font
import scanner as sc
import excel as ex
from thread_logger import LoggedThread, get_logger as _get_thread_logger


def _to_bytes(message, is_hex=False):
    """
    تحويل أي قيمة لـ bytes جاهزه للإرسال على السوكيت.
    بيتعامل مع: bytes, str, int, float (وأي رقم).
    لو is_hex=True بيفسر الـ str كـ hex.

    قبل التعديل: send_only(1) كان بيرمي 'int' object has no attribute 'encode'.
    """
    if isinstance(message, bytes):
        return message
    if isinstance(message, bytearray):
        return bytes(message)
    if is_hex and isinstance(message, str):
        return bytes.fromhex(message)
    # نحوّل أي رقم لـ str قبل encode
    return str(message).encode('utf-8')


class TCPServer:
    def __init__(self, ip="0.0.0.0", port=5000, timeout=None, buffer_size=4096):
        """
        :param ip: "0.0.0.0" تعني الاستماع على كل كروت الشبكة المتاحة
        """
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.buffer_size = buffer_size
        self.server_sock = None
        self.running = False
        
        # إدارة الكلاينت المتصلين — مع قفل لمنع race condition بين accept/handle/stop
        self.clients = []
        self._clients_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._log = list()
        self.name = "TCP_SERVER"

        # الكيوز
        self.shared_queue = queue.Queue()
        self.receive_queue = queue.Queue()
        


    def start_listening(self, callback):
        """
        كل اللي بنعمله هنا إننا بنسجل الفانكشن اللي هتشتغل 
        أول ما أي داتا توصل.
        """
        self.callback = callback
        self._log_add("INFO", "Callback registered. Waiting for incoming data...")


    
    def start(self):
        """بدء تشغيل السيرفر وحجز البورت"""
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # SO_REUSEADDR عشان لو السيرفر قفل يفتح تاني فوراً على نفس البورت
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind((self.ip, self.port))
            self.server_sock.listen(5) # أقصى عدد من الاتصالات المنتظرة
            self.running = True
            
            # تشغيل خيط لاستقبال الكلاينتس الجدد
            self.accept_thread = LoggedThread(
                target=self._accept_loop,
                name=f"{self.name}-accept-loop",
                daemon=True,
            )
            self.accept_thread.start()
            
            self._log_add("INFO", f"Server started on {self.ip}:{self.port}")
            return True
        except Exception as e:
            self._log_add("ERROR", f"Failed to start server: {e}")
            return False

    def _accept_loop(self):
        """لوب دائم لاستقبال أي كلاينت بيحاول يتصل"""
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                self._log_add("INFO", f"New connection from {addr}")
                
                # تشغيل خيط خاص لكل كلاينت عشان السيرفر يخدم كذا حد في نفس الوقت
                client_handler = LoggedThread(
                    target=self._handle_client,
                    args=(client_sock, addr),
                    name=f"{self.name}-client-{addr[0]}:{addr[1]}",
                    daemon=True,
                )
                client_handler.start()
                with self._clients_lock:
                    self.clients.append(client_sock)
                
            except Exception as e:
                if self.running:
                    self._log_add("ERROR", f"Accept error: {e}")
                break

    
    
    def _handle_client(self, client_sock, addr):
        """
        الدالة دي بتشتغل في Thread منفصل لكل كلاينت بيتصل، 
        وبتفضل مستنية داتا منه.
        """
        while self.running:
            try:
                # السطر ده بيفضل عامل بلوك (واقف) لحد ما الكلاينت يبعت داتا
                data = client_sock.recv(self.buffer_size)
                
                if not data:
                    # لو الداتا فاضية، معناه إن الكلاينت قفل الاتصال
                    break
                
                self._log_add("INFO", f"Received from {addr}: {data}")
                
                # ============== السحر كله هنا ==============
                # أول ما الداتا توصل، ننده الـ Callback فوراً
                if hasattr(self, 'callback') and self.callback:
                    try:
                        # بنبعت الـ client_sock (عشان لو حبيت ترد عليه)، والـ addr، والـ data
                        self.callback(client_sock, addr, data)
                    except Exception as cb_err:
                        self._log_add("ERROR", f"Error inside callback: {cb_err}")
                # ==========================================

            except Exception as e:
                self._log_add("WARNING", f"Client {addr} disconnected: {e}")
                break
        
        # لما اللوب يخلص (الكلاينت يقفل)، ننظف السوكيت
        client_sock.close()
        with self._clients_lock:
            if client_sock in self.clients:
                self.clients.remove(client_sock)
        self._log_add("INFO", f"Connection closed for {addr}")  
    
    def broadcast(self, message, is_hex=False):
        """إرسال رسالة لكل الكلاينتس المتصلين حالياً"""
        data_to_send = self._prepare_data(message, is_hex)
        with self._clients_lock:
            clients_snapshot = list(self.clients)
        for client in clients_snapshot:
            try:
                client.sendall(data_to_send)
            except Exception:
                pass  # السوكيت غالباً ميت، الـ handle_client هينظفه

    def _prepare_data(self, message, is_hex):
        return _to_bytes(message, is_hex)

    def stop(self):
        """إيقاف السيرفر تماماً"""
        self.running = False
        with self._clients_lock:
            clients_snapshot = list(self.clients)
        for client in clients_snapshot:
            try:
                client.close()
            except Exception:
                pass
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        self._log_add("INFO", "Server stopped.")

    def _log_add(self, level: str, msg: str):
        with self._log_lock:
            self._log_seq += 1
            self._log.append((self._log_seq, time.time(), level, msg))
        print(f"[{self.name}][{level}] {msg}")

    def get_last_received(self, block=False, timeout=None):
        try:
            return self.receive_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None


# General Class
class  TCPClient():
    def __init__(self, ip, port, timeout=None, buffer_size=4096, name=None):
        """
        :param timeout: لو خليته None هيفضل مستني للأبد لحد ما السيرفر يرد
        :param name: اسم اختياري للعميل يظهر في اللوج (مفيد لما يكون عندك أكتر من client)
        """
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.buffer_size = buffer_size
        self.sock = None  # هنا هنحتفظ بالسوكيت عشان يفضل مفتوح
        self.connected = False
        self._send_queue: "queue.Queue[dict]" = queue.Queue()
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._log = list()
        # اسم افتراضي معبّر بدل ما يطلع [][INFO] في اللوج
        self.name = name if name else f"TCPClient-{ip}:{port}"
        self.current_program_label =""
        self.current_program_data=""

        # ── علم لإيقاف monitors و listeners ──────────────────────────────
        self._stop_monitor = threading.Event()
        # قفل بيمنع التداخل بين send_request و watchdog ping
        self._send_lock = threading.Lock()

        # كيو الاستقبال (ينعمل من بدري عشان get_last_received مايرميش AttributeError)
        self.receive_queue = queue.Queue()

        self.shared_queue = queue.Queue()
        self.shared_queue2= queue.Queue() #FOR DUMMY shared between scanner and data proccesing function
        self.shared_queue3= queue.Queue() # for dummies shared between scanner and i/o writer function

    def connect(self):
        """دالة لفتح الاتصال مرة واحدة"""
        try:
            if self.connected:
                print(f"[{self.ip}] Already connected.")
                return True
            
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout) # تحديد وقت الانتظار (أو None للانتظار الدائم)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            # ضعه داخل دالة connect() بعد السطر self.sock.connect(...)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            # لتعديل الوقت على ويندوز/لينكس ليصبح الفحص سريعاً (مثلاً كل 5 ثواني)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"): # Windows
                # (تفعيل، الوقت بالمللي ثانية قبل بدء الفحص، الوقت بين الفحص والتالي)
                self.sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5000, 3000))
            elif hasattr(socket, "TCP_KEEPIDLE"): # Linux
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            print(f"[{self.ip}] : [{self.port}] Connected successfully.")
            return True
        except Exception as e:
            print(f"[{self.ip}] : [{self.port}] Connection Failed: {e}")
            self.connected = False
            return False
            
    def ensure_connected(self):
        """تتأكد إننا متصلين، تحاول لحد ما تتصل أو يتم إيقاف الـ monitor."""
        while not self.connected and not self._stop_monitor.is_set():
            self._log_add("INFO", f"Trying to reconnect to {self.ip}...")
            if self.connect():
                self._log_add("INFO", "✅ Reconnected successfully!")
                break
            else:
                try:
                    from config import config as _cfg
                    delay = float(_cfg.get("reconnect_retry_delay", 5.0))
                except Exception:
                    delay = 5.0
                self._log_add("WARNING", f"❌ Retrying in {delay} seconds...")
                if self._stop_monitor.wait(timeout=delay):
                    break
    
    def start_reconnection_watchdog(self):
        """تشغيل خيط المراقبة في الخلفية"""
        thread = LoggedThread(
            target=self._connection_monitor,
            name=f"TCPClient-{self.ip}:{self.port}-reconnect-watchdog",
            daemon=True,
        )
        thread.start()

    def _connection_monitor(self):
        """
        watchdog حقيقي بيراقب الاتصال بدون ما يبعت داتا للسيرفر.

        الطريقة:
        - بنستخدم select.select لمعرفه لو السوكيت لسه شغال (writable).
        - بنفحص لو فيه بيانات في الـ buffer جاهزة للقراءة (MSG_PEEK).
        - لو السوكيت اتقفل من الناحيه التانيه، recv بترجع b'' فبنعرف الاتصال راح.
        - مش بنبعت "ping" عشان مانلخبطش السيرفر الحقيقي بأوامر مش متوقعة.
        """
        import select
        log = _get_thread_logger()
        log.info(f"Connection watchdog started for {self.name}")

        while not self._stop_monitor.is_set():
            if not self.connected:
                # لو لقيناه فصل، نصلحه
                self.ensure_connected()
            else:
                # فحص بدون إرسال داتا
                try:
                    if self.sock is None:
                        self.connected = False
                        continue

                    # 1. فحص لو السوكيت writable (مش مقفول)
                    _, writable, errored = select.select([], [self.sock], [self.sock], 0.5)
                    if errored:
                        raise OSError("socket reported error via select")

                    # 2. peek لو في داتا قادمه عشان نعرف لو السيرفر قفل
                    self.sock.setblocking(False)
                    try:
                        peek = self.sock.recv(1, socket.MSG_PEEK)
                        if peek == b'':
                            # السيرفر قفل الاتصال
                            raise ConnectionResetError("peer closed connection (peek returned empty)")
                    except BlockingIOError:
                        # مفيش داتا — ده الوضع الطبيعي = الاتصال شغال
                        pass
                    finally:
                        try:
                            self.sock.setblocking(True)
                            self.sock.settimeout(self.timeout)
                        except Exception:
                            pass

                except Exception as e:
                    self._log_add("WARNING", f"Connection lost in background: {e}")
                    self.connected = False
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None

            # فحص كل N ثواني (من config) — قابل للإيقاف فوراً
            try:
                from config import config as _cfg
                check_interval = float(_cfg.get("reconnect_check_interval", 3.0))
            except Exception:
                check_interval = 3.0
            if self._stop_monitor.wait(timeout=check_interval):
                break

        log.info(f"Connection watchdog stopped for {self.name}")
    
    def _get_sock(self):
        """يرجع (local_ip, local_port) أو (None, None) لو مش متصل."""
        if self.sock is None:
            return None, None
        try:
            return self.sock.getsockname()
        except Exception:
            return None, None
   
    # رسائل الـ keepalive اللي بنتجاهلها في send_request
    # الكوبوت بيبعتها تلقائياً على الـ socket كـ heartbeat
    _KEEPALIVE_MSGS = {b"ping", b"pong", b"PING", b"PONG"}

    def send_request(self, message, is_hex=False):
        """
        إرسال واستقبال فقط (بدون إغلاق الاتصال).
        بيتجاهل رسائل الـ keepalive (ping/pong) اللي بيبعتها الكوبوت
        تلقائياً على الـ socket ويفضل مستني الرد الحقيقي.
        """
        if not self.connected or self.sock is None:
            print(f"[{self.ip}]:[{self.port}] Error: Not connected! Trying to connect...")
            self.ensure_connected()

        try:
            # 1. تجهيز الرسالة (بيتعامل مع int/str/bytes/float عبر _to_bytes)
            data_to_send = _to_bytes(message, is_hex)

            # 2. الإرسال
            self.sock.sendall(data_to_send)

            # 3. الاستقبال — بنتجاهل keepalive ونفضل مستنيين الرد الحقيقي
            while True:
                response = self.sock.recv(self.buffer_size)
                # لو الرد رسالة keepalive → تجاهلها وارجع اسمع تاني
                if response and response.strip() in self._KEEPALIVE_MSGS:
                    print(f"[{self.ip}]:[{self.port}] Ignoring keepalive: {response!r}")
                    continue
                return response

        except socket.timeout:
            print(f"[{self.ip}]:[{self.port}] Timeout: Server took too long to respond.")
            return None

        except (OSError, BrokenPipeError, ConnectionResetError, socket.error) as e:
            print(f"[{self.ip}]:[{self.port}] Connection Lost ({e}). Reconnecting...")
            self.connected = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            self.ensure_connected()
            return None

        except Exception as e:
            print(f"[{self.ip}]:[{self.port}] General Error: {e}")
            return None
    
    '''
    def _start_monitoring(self):
        """بدء خيط المراقبة"""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._stop_monitor.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_connections, daemon=True)
            self._monitor_thread.start()

    def _monitor_connections(self):
        """فانكشن المراقبة اللي بتشيك على حالة الاتصال كل فترة"""
        print(f"[{self.ip}] Connection monitor started.")
        while not self._stop_monitor.is_set():
            if self.connected and self.sock:
                try:
                    # بنبعث "بيانات فارغة" عشان نختبر لو السوكيت لسه شغال (Keep-alive check)
                    # MSG_PEEK بيشوف البيانات من غير ما يسحبها من البافر
                    self.sock.send(b"", socket.MSG_DONTWAIT)
                except (OSError, BrokenPipeError):
                    print(f"[{self.ip}] Monitor detected broken connection!")
                    self.connected = False
                    # هنا ممكن تختار تنادي self.connect() تاني لو عايز Auto-reconnect
                    break
            time.sleep(5)  # شيك كل 5 ثواني مثلاً
     
   '''
    
    def disconnect(self):
        """إغلاق الاتصال وإيقاف المونيتور"""
        self._stop_monitor.set() # وقف اللوب في المونيتور
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._log_add("INFO", f"[{self.ip}] Connection Closed.")

    def _log_add(self, level: str, msg: str):
        with self._log_lock:
            self._log_seq += 1
            self._log.append((self._log_seq, time.time(), level, msg))
            if len(self._log) > 5000:
                self._log = self._log[-3000:]
        print(f"[{self.name}][{level}] {msg}")
    
    def start_listening(self, callback=None):
        """
        دالة لبدء عملية الاستماع في Thread منفصل
        :param callback: دالة اختيارية يتم استدعاؤها فور استلام بيانات
        """
        # ملحوظه: receive_queue معرفه من الـ __init__ مرة واحدة بس،
        # عشان get_last_received يقدر يتنادى قبل أو بعد start_listening.
        self.listen_thread = LoggedThread(
            target=self._listen_loop,
            args=(callback,),
            name=f"TCPClient-{self.ip}:{self.port}-listen-loop",
            daemon=True,
        )
        self.listen_thread.start()
        self._log_add("INFO", f"[{self.ip}] : [{self.port}] Started listening for incoming data...")
        

    def _listen_loop(self, callback):
        """الـ Loop الداخلي اللي بيفضل مستني داتا"""
        while self.connected:
            try:
                # الكود هيفضل واقف هنا لحد ما السيرفر يبعت حاجة
                data = self.sock.recv(self.buffer_size)
                
                if not data:
                    # لو السيرفر بعت داتا فاضية معناها قفل الاتصال
                    print(f"[{self.ip}] Server closed the connection.")
                    self.connected = False
                    break
                
                if callback:
                    callback(data)
                # إضافة البيانات للكيو
                #self.receive_queue.put(data)

                # اختياري: تسجيل اللوج
                # self._log_add("INFO", f"Received data: {data}")

            except socket.timeout:
                continue # لو حصل تايم أوت يرجع يحاول يستقبل تاني
            except Exception as e:
                if self.connected:
                    print(f"[{self.ip}] Listening Error: {e}")
                    self.connected = False
                break

    def get_last_received(self, block=False, timeout=None):
        """دالة لسحب آخر داتا وصلت من الكيو"""
        try:
            return self.receive_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None


    def send_only(self, message, is_hex=False):
            """
            إرسال رسالة فقط دون انتظار أي رد من السيرفر
            """
            if not self.connected or self.sock is None:
                print(f"[{self.ip}]:[{self.port}] Error: Not connected! Trying to connect...")
                if not self.connect():
                    return False

            try:
                # 1. تجهيز الرسالة (بيتعامل مع int/str/bytes/float)
                data_to_send = _to_bytes(message, is_hex)

                # 2. الإرسال (sendall تضمن وصول البيانات بالكامل للـ Buffer)
                self.sock.sendall(data_to_send)
                
                # اختياري: إضافة لوج للعملية
                # self._log_add("INFO", f"Message sent (No response expected): {message}")
                
                return True

            except (OSError, BrokenPipeError, ConnectionResetError, socket.error) as e:
                print(f"[{self.ip}]:[{self.port}] Send Failed ({e}). Reconnecting...")
                self.connected = False
                if self.sock:
                    try: self.sock.close()
                    except: pass
                    self.sock = None
                return False
                
            except Exception as e:
                print(f"[{self.ip}]:[{self.port}] General Error in send_only: {e}")
                return False

##################################################################
class AppStage:
    """
    المراحل اللي ممكن البرنامج يكون فيها.
    يدعم حتى MAX_VISION_TESTS اختبار ديناميكياً بدون تعديل الكود.
    """
    # ─── الحد الأقصى لعدد الاختبارات ────────────────────────────────
    MAX_VISION_TESTS = 30   # ← غيّر الرقم ده لو عايز أكتر أو أقل

    # ─── الـ stages الثابتة ──────────────────────────────────────────
    IDLE             = "IDLE"
    BARCODE_RECEIVED = "BARCODE_RECEIVED"
    PROGRAM_LOOKUP   = "PROGRAM_LOOKUP"
    SENDING_PROGRAM  = "SENDING_PROGRAM"
    REPORTING        = "REPORTING"
    DONE             = "DONE"
    ERROR            = "ERROR"

    # ─── الـ vision stages ديناميكية (VISION_TEST_1 .. VISION_TEST_30) ──
    # بيتولدوا تلقائياً حسب MAX_VISION_TESTS
    @classmethod
    def vision_stage(cls, i: int) -> str:
        """يرجع اسم الـ stage للاختبار i (0-indexed). مثال: i=0 → 'VISION_TEST_1'"""
        return f"VISION_TEST_{i + 1}"

    # ─── عدد الاختبارات الافتراضي ────────────────────────────────────
    VISION_TEST_COUNT = 6   # القيمة الافتراضية لو مش موجودة في config

    @classmethod
    def get_vision_test_count(cls) -> int:
        """يجيب عدد الاختبارات من config (1 .. MAX_VISION_TESTS)."""
        try:
            from config import config as _cfg
            count = int(_cfg.get("vision_test_count", cls.VISION_TEST_COUNT))
        except Exception:
            count = cls.VISION_TEST_COUNT
        return max(1, min(cls.MAX_VISION_TESTS, count))

    # ─── ORDER: الترتيب للـ progress bar (يتولد ديناميكياً) ─────────
    @classmethod
    def get_order(cls) -> list:
        vision_stages = [cls.vision_stage(i) for i in range(cls.MAX_VISION_TESTS)]
        return [
            cls.IDLE, cls.BARCODE_RECEIVED, cls.PROGRAM_LOOKUP, cls.SENDING_PROGRAM,
            *vision_stages,
            cls.REPORTING, cls.DONE,
        ]

    # ORDER ثابت بيستخدمه الكود القديم اللي بيقرأ AppStage.ORDER مباشرة
    ORDER = (
        ["IDLE", "BARCODE_RECEIVED", "PROGRAM_LOOKUP", "SENDING_PROGRAM"]
        + [f"VISION_TEST_{i}" for i in range(1, MAX_VISION_TESTS + 1)]
        + ["REPORTING", "DONE"]
    )

    # ─── LABELS: النصوص للـ GUI ────────────────────────────────────
    LABELS = {
        "IDLE":             "في الانتظار",
        "BARCODE_RECEIVED": "تم استقبال باركود",
        "PROGRAM_LOOKUP":   "البحث عن البرنامج",
        "SENDING_PROGRAM":  "إرسال البرنامج للكوبوت",
        **{f"VISION_TEST_{i}": f"اختبار الرؤية {i}" for i in range(1, MAX_VISION_TESTS + 1)},
        "REPORTING":        "كتابة التقرير",
        "DONE":             "انتهى",
        "ERROR":            "خطأ",
    }

    # backward-compat: attributes for old code using AppStage.VISION_TEST_1 etc.
    # يتولدوا تلقائياً في آخر الكلاس


# نضيف الـ attributes ديناميكياً على الـ class عشان الكود القديم يشتغل
for _i in range(1, AppStage.MAX_VISION_TESTS + 1):
    setattr(AppStage, f"VISION_TEST_{_i}", f"VISION_TEST_{_i}")


class App():
    def __init__(self):

        pass 
   
    def check_images_status(self, images_data):
        # المرور على جميع القيم (Values) داخل الـ JSON
        for image_name, value in images_data.items():
            # التحقق إذا كانت القيمة تساوي 'yes' (مع تجاهل المسافات وحالة الأحرف)
            if str(value).strip().lower() == 'yes':
                return "fail"
                
        # إذا انتهت الحلقة بدون العثور على 'yes'، يتم إرجاع 'pass'
        return "pass"

    def start(self):
        return True

    def run(self):
        """alias قديم - مازال يشتغل عشان الـ tests و main.py."""
        return self.start()

    def stop(self):
        """
        إيقاف البرنامج بشكل نضيف — non-blocking للـ GUI thread.

        الـ stop بيتم في ثريد خلفي عشان الـ GUI ما تتجمدش:
        1. نشير للـ threads إنها توقف (_stop_app)
        2. ننتظر السيكوانس تخلص (في الخلفية) عشان الكوبوت ياخد النتيجة الصح
        3. نقفل الـ connections بعد ما كل حاجة تخلص
        """
        log = _get_thread_logger()
        with self._start_stop_lock:
            if not self.is_running:
                log.warning("App.stop() called but not running")
                return
            log.info("App: STOPPING...")
            self._stop_app.set()
            self.is_running = False   # نحجب start() جديدة فوراً
            self._set_stage(AppStage.IDLE, current_step=0,
                            current_barcode=None, current_program=None)

        # ── الإغلاق الحقيقي في ثريد خلفي عشان GUI ما تتجمدش ──────────────
        def _shutdown_worker():
            # انتظر السيكوانس الحالية تخلص (max 120s) قبل ما نقفل الـ connections
            seq_thread = getattr(self, "_sequance_worker_thread", None)
            if seq_thread is not None and seq_thread.is_alive():
                log.info("App: waiting for current sequence to finish (max 120s)...")
                seq_thread.join(timeout=120)
                if seq_thread.is_alive():
                    log.warning("App: sequence timeout — forcing disconnect now")
                else:
                    log.info("App: sequence finished cleanly")

            # نوقف مصدر الباركود
            from config import config as _cfg_stop
            if _cfg_stop.get("scan_mode", "manual") == "camera":
                try:
                    import camera_barcode
                    camera_barcode.stop()
                    log.info("Camera barcode scanner stopped")
                except Exception as e:
                    log.warning(f"camera_barcode.stop failed: {e}")
            # ── وقف الكاميرا المباشرة ─────────────────────────────────────
            try:
                import live_image
                live_image.stop()
                log.info("live_image: stopped")
            except Exception as e:
                log.warning(f"live_image.stop failed: {e}")

            try:
                sc.stop_listener()
                log.info("Scanner listener stopped")
            except Exception as e:
                log.warning(f"scanner.stop_listener failed: {e}")

            for client in (self.VisionClient_TRIG, self.VisionClient_ID, self.cobotClient):
                try:
                    client.disconnect()
                except Exception as e:
                    log.warning(f"client.disconnect failed: {e}")
            try:
                self.triggerserver.stop()
            except Exception as e:
                log.warning(f"server.stop failed: {e}")

            # ── camera_hub: آخر حاجة تتوقف (بعد camera_barcode و live_image) ──
            try:
                import camera_hub
                camera_hub.stop()
                log.info("camera_hub: stopped")
            except Exception as e:
                log.warning(f"camera_hub.stop failed: {e}")

            log.info("App: STOPPED")

        LoggedThread(target=_shutdown_worker, name="App-shutdown", daemon=True).start()
