"""Modelos locales: encontrar archivos .gguf y un llama-server, y manejar el servidor (uno a la vez).

Sin dependencias externas. llama-server habla el mismo protocolo que DeepSeek (/v1/chat/completions con herramientas),
así que el agente lo usa igual; solo cambia la URL base.
"""
import ctypes
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from ctypes import wintypes

import dsapi

# Un .gguf que no sirve para conversar: vectores de embeddings, adaptadores multimodales, rerankers.
NOT_CHAT = ("embed", "mmproj", "rerank", "reranker")
MODEL_ID_PREFIX = "local:"


class LocalError(Exception):
    pass


# ---------------------------------------------------------------- búsqueda

def default_model_dirs(cfg=None):
    """Carpetas donde se buscan modelos sin que el usuario configure nada: junto al programa, y la raíz de su unidad
    (un pendrive con Models\\ en la raíz es el caso típico)."""
    root = dsapi.portable_root() or dsapi.APP_DIR
    dirs = [os.path.join(root, "Models"), os.path.join(dsapi.APP_DIR, "Models"), os.path.join(os.path.dirname(dsapi.APP_DIR), "Models")]
    drive = os.path.splitdrive(dsapi.APP_DIR)[0]
    if drive:
        dirs.append(drive + "\\Models")
    if cfg is not None:
        dirs += [d for d in cfg["model_dirs"] if d]
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            out.append(os.path.abspath(d))
    return out


def scan_models(dirs, max_depth=3):
    """Devuelve [{'id', 'name', 'path', 'size'}] de los .gguf de conversación, sin repetir, ordenados por nombre."""
    found = {}
    for base in dirs:
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip("\\/").count(os.sep)
        try:
            for dirpath, dirnames, files in os.walk(base):
                if dirpath.rstrip("\\/").count(os.sep) - base_depth >= max_depth:
                    dirnames[:] = []
                for f in files:
                    if not f.lower().endswith(".gguf"):
                        continue
                    low = f.lower()
                    if any(w in low for w in NOT_CHAT):
                        continue
                    p = os.path.join(dirpath, f)
                    k = os.path.normcase(os.path.abspath(p))
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size < 1024 * 1024:       # no puede ser un modelo real
                        continue
                    found.setdefault(k, {"id": MODEL_ID_PREFIX + os.path.abspath(p), "name": f[:-5], "path": os.path.abspath(p), "size": size})
        except OSError:
            continue
    return sorted(found.values(), key=lambda m: m["name"].lower())


def find_llama_server(configured=""):
    """Ruta a llama-server.exe o None. Orden: la configurada, y luego bin\\ junto al programa."""
    cands = [configured] if configured else []
    root = dsapi.portable_root() or dsapi.APP_DIR
    for d in (os.path.join(root, "bin"), os.path.join(dsapi.APP_DIR, "bin"), os.path.join(os.path.dirname(dsapi.APP_DIR), "bin")):
        cands.append(os.path.join(d, "llama-server.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


# ---------------------------------------------------------------- Job Object: que el hijo muera con nosotros

_JOB_LIMIT_KILL_ON_CLOSE = 0x2000


class _BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]


class _EXT(ctypes.Structure):
    _fields_ = [("Basic", _BASIC), ("Io", _IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _make_kill_on_close_job():
    """Un Job de Windows que mata a sus procesos cuando se cierra su último handle, es decir, cuando muere este
    programa aunque sea a la fuerza. Devuelve el handle (hay que conservarlo) o None si no se pudo (WinPE, etc.)."""
    try:
        k = ctypes.windll.kernel32
        k.CreateJobObjectW.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXT()
        info.Basic.LimitFlags = _JOB_LIMIT_KILL_ON_CLOSE
        if not k.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job
    except Exception:
        return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- el servidor

class LocalServer:
    """Un llama-server a la vez. ensure() reutiliza el que ya corre si es el mismo modelo y contexto."""

    def __init__(self, exe, log_dir, ctx=16384, threads=None):
        self.exe, self.log_dir, self.ctx, self.threads = exe, log_dir, int(ctx), threads
        self.proc = None
        self.model = None
        self.port = None
        self._job = _make_kill_on_close_job()
        self._log = None
        self.log_path = os.path.join(log_dir, "llama-server.log")

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1" if self.port else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _tail(self, n=6):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f.readlines() if l.strip()]
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def ensure(self, model_path, ctx=None, cancel=None, timeout=300):
        """Deja corriendo el servidor con ese modelo y devuelve la URL base. Bloquea hasta que responde /health.
        cancel: threading.Event opcional para abortar la espera."""
        ctx = int(ctx or self.ctx)
        if self.alive() and self.model == (os.path.normcase(model_path), ctx):
            return self.base
        self.stop()
        if not os.path.isfile(self.exe):
            raise LocalError(f"No se encuentra llama-server.exe en {self.exe}")
        if not os.path.isfile(model_path):
            raise LocalError(f"No se encuentra el modelo {model_path} (¿está conectado el pendrive?)")
        os.makedirs(self.log_dir, exist_ok=True)
        self.port = _free_port()
        args = [self.exe, "-m", model_path, "--host", "127.0.0.1", "--port", str(self.port), "-c", str(ctx),
                "-np", "1", "--jinja", "-ngl", "0", "--no-webui"]
        if self.threads:
            args += ["-t", str(self.threads)]
        self._log = open(self.log_path, "wb")
        flags = 0x08000000   # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         cwd=os.path.dirname(self.exe), creationflags=flags)
        except OSError as e:
            raise LocalError(f"No se pudo arrancar llama-server: {e}")
        if self._job:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(wintypes.HANDLE(self._job), wintypes.HANDLE(int(self.proc._handle)))
            except Exception:
                pass
        self.model = (os.path.normcase(model_path), ctx)
        t0 = time.time()
        url = f"http://127.0.0.1:{self.port}/health"
        while True:
            if cancel is not None and cancel.is_set():
                self.stop()
                raise LocalError("Cancelado mientras se cargaba el modelo.")
            if self.proc.poll() is not None:
                tail = self._tail()
                self.stop()
                raise LocalError("llama-server terminó al arrancar (¿poca memoria o modelo incompatible?).\n" + tail)
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return self.base
            except urllib.error.HTTPError:
                pass            # 503 mientras carga el modelo
            except (OSError, ValueError):
                pass
            if time.time() - t0 > timeout:
                tail = self._tail()
                self.stop()
                raise LocalError(f"El modelo no estuvo listo en {timeout} s.\n{tail}")
            time.sleep(0.4)

    def stop(self):
        p, self.proc = self.proc, None
        self.model = None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        self.port = None


def format_size(n):
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"
