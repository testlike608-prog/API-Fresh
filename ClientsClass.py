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
import camera_barcode
import camera_hub
from pathlib import Path
from typing import Callable, Optional

import cv2


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
        camera_hub.start()
        frame = camera_hub.get_frame() 
        return True

    def run(self):
        """alias قديم - مازال يشتغل عشان الـ tests و main.py."""
        return self.start()

    def stop(self):
        pass


# Working App implementation used by the FastAPI WebSocket integration.
# It intentionally keeps the same class name so old imports receive this version.
class App():
    def __init__(self):
        self.robot = None
        self.robot_ip = None
        self._robot_lock = threading.RLock()
        self._motion_lock = threading.Lock()
        self._last_images = []

    def check_images_status(self, images_data):
        for image_name, value in images_data.items():
            if str(value).strip().lower() == "yes":
                return "fail"
        return "pass"

    def start(self, camera_index=None):
        camera_hub.start(camera_index=camera_index)
        camera_hub.wait_for_frame(timeout=5.0)
        return True

    def run(self):
        return self.start()

    def stop(self):
        self.disconnect_robot()
        camera_hub.stop()

    def _get_robot_class(self):
        try:
            from fairino.Robot import RPC
        except Exception as exc:
            raise RuntimeError(f"Fairino SDK import failed: {exc}") from exc
        return RPC

    def connect_robot(self, ip=None, enable=True):
        from config import config as _cfg

        robot_ip = ip or _cfg.get("cobot_ip", "192.168.57.2")
        with self._robot_lock:
            if self.robot is not None and self.robot_ip == robot_ip:
                return {"connected": True, "ip": self.robot_ip, "reused": True}

            RPC = self._get_robot_class()
            self.robot = RPC(robot_ip)
            self.robot_ip = robot_ip

            if enable:
                enable_error = self.robot.RobotEnable(1)
                if enable_error not in (0, None):
                    raise RuntimeError(f"RobotEnable failed with error code {enable_error}")

        return {"connected": True, "ip": robot_ip, "reused": False}

    def disconnect_robot(self):
        with self._robot_lock:
            if self.robot is None:
                return {"connected": False}

            try:
                self.robot.CloseRPC()
            finally:
                self.robot = None
                self.robot_ip = None

        return {"connected": False}

    def ensure_robot(self):
        with self._robot_lock:
            if self.robot is None:
                self.connect_robot()
            return self.robot

    def robot_status(self):
        with self._robot_lock:
            if self.robot is None:
                return {"connected": False, "ip": None}
            return {"connected": True, "ip": self.robot_ip}

    def move_to_point(self, point, motion_type="linear", tool=0, user=0, vel=20.0, acc=0.0, ovl=100.0):
        robot = self.ensure_robot()
        point_name = point.get("name") or point.get("id") or "point"
        selected_motion = str(point.get("motion_type", motion_type)).lower()

        if selected_motion in ("joint", "j", "movej"):
            joint_pos = self._read_position(point, ("joint_pos", "joints", "position"))
            desc_pos = point.get("desc_pos", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            error = robot.MoveJ(
                joint_pos=joint_pos,
                tool=point.get("tool", tool),
                user=point.get("user", user),
                desc_pos=desc_pos,
                vel=point.get("vel", vel),
                acc=point.get("acc", acc),
                ovl=point.get("ovl", ovl),
                blendT=point.get("blendT", -1.0),
            )
        else:
            desc_pos = self._read_position(point, ("desc_pos", "pose", "position"))
            error = robot.MoveL(
                desc_pos=desc_pos,
                tool=point.get("tool", tool),
                user=point.get("user", user),
                joint_pos=point.get("joint_pos", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
                vel=point.get("vel", vel),
                acc=point.get("acc", acc),
                ovl=point.get("ovl", ovl),
                blendR=point.get("blendR", -1.0),
            )

        if error not in (0, None):
            raise RuntimeError(f"Move to {point_name} failed with error code {error}")

        return {"point": point_name, "motion_type": selected_motion, "error": error or 0}

    def capture_image(self, point_name="point", index=1, output_dir=None):
        from config import config as _cfg

        if not camera_hub.is_running():
            self.start()

        if not camera_hub.wait_for_frame(timeout=5.0):
            raise RuntimeError("Camera did not provide a frame")

        frame = camera_hub.get_frame()
        if frame is None:
            raise RuntimeError("Camera frame is empty")

        folder = Path(output_dir or _cfg.get("result_images_folder", "result_images"))
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(point_name)).strip("_") or "point"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        image_path = folder / f"{safe_name}_{index}_{timestamp}.png"

        if not cv2.imwrite(str(image_path), frame):
            raise RuntimeError(f"Failed to save image to {image_path}")

        image_info = {
            "point": point_name,
            "index": index,
            "image_path": str(image_path.resolve()),
        }
        self._last_images.append(image_info)
        return image_info

    def move_points_and_capture(
        self,
        points,
        motion_type="linear",
        tool=0,
        user=0,
        vel=20.0,
        acc=0.0,
        ovl=100.0,
        output_dir=None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        if not isinstance(points, list) or not points:
            raise ValueError("points must be a non-empty list")

        results = []
        with self._motion_lock:
            self.start()
            self.ensure_robot()

            for index, point in enumerate(points, start=1):
                point_name = point.get("name") or point.get("id") or f"point_{index}"
                self._emit_progress(progress_callback, "moving", point=point_name, index=index, total=len(points))
                move_result = self.move_to_point(
                    point,
                    motion_type=motion_type,
                    tool=tool,
                    user=user,
                    vel=vel,
                    acc=acc,
                    ovl=ovl,
                )
                self._emit_progress(progress_callback, "arrived", point=point_name, index=index, total=len(points))
                image_info = self.capture_image(point_name=point_name, index=index, output_dir=output_dir)
                item = {**move_result, **image_info}
                results.append(item)
                self._emit_progress(progress_callback, "captured", **item, total=len(points))

        self._emit_progress(progress_callback, "completed", count=len(results), images=results)
        return results

    def stop_robot_motion(self):
        with self._robot_lock:
            if self.robot is None:
                return {"stopped": False, "reason": "robot is not connected"}
            error = self.robot.StopMove()
        if error not in (0, None):
            raise RuntimeError(f"StopMove failed with error code {error}")
        return {"stopped": True, "error": error or 0}

    def _read_position(self, point, keys):
        for key in keys:
            if key in point:
                position = point[key]
                break
        else:
            raise ValueError(f"Point is missing one of these fields: {', '.join(keys)}")

        if not isinstance(position, (list, tuple)) or len(position) != 6:
            raise ValueError("Point position must be a list of 6 numbers")
        return [float(value) for value in position]

    def _emit_progress(self, callback, event, **payload):
        if callback:
            callback({"event": event, **payload, "timestamp": datetime.now().isoformat()})
