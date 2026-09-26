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
from datetime import datetime
import urllib.error
import urllib.request
from ctypes import wintypes

import dsapi

# Un .gguf que no sirve para conversar: vectores de embeddings, adaptadores multimodales, rerankers.
NOT_CHAT = ("embed", "mmproj", "rerank", "reranker")
MODEL_ID_PREFIX = "local:"


class LocalError(Exception):
    pass


# ---------------------------------------------------------------- memoria: el límite real es el commit, no la RAM libre

class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_status():
    """{'ram_libre', 'ram_total', 'commit_libre'} en bytes, o None si no se pudo medir.
    commit_libre = límite de memoria comprometida menos lo ya comprometido: es lo que Windows deja reservar a un
    programa nuevo, y puede ser muy chico aunque sobre RAM física (otros procesos reservan sin usar)."""
    try:
        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return {"ram_libre": st.ullAvailPhys, "ram_total": st.ullTotalPhys, "commit_libre": st.ullAvailPageFile}
    except (AttributeError, OSError):
        return None


def _gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def memory_message(name, size, mem):
    return (f"El modelo «{name}» pesa {_gb(size)}, pero Windows solo puede reservar {_gb(mem['commit_libre'])} más de memoria "
            f"(hay {_gb(mem['ram_libre'])} de RAM libre; el límite es la memoria comprometida, que otros programas ya ocuparon). "
            "Opciones: cerrar programas grandes (emuladores, otros servidores de modelos), elegir un modelo más chico, "
            "o ampliar el archivo de paginación de Windows.")


def check_memory(model_path, mem=None):
    """Lanza LocalError si el modelo seguro no puede cargarse por falta de memoria comprometida. Si no se puede medir, deja pasar."""
    mem = mem or memory_status()
    if not mem:
        return
    try:
        size = os.path.getsize(model_path)
    except OSError:
        return
    if mem["commit_libre"] < size * 1.02:
        raise LocalError(memory_message(os.path.basename(model_path), size, mem))


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


# ---------------------------------------------------------------- perfiles por familia de modelo
#
# Medidos en la máquina de desarrollo (Ryzen 5 5600G, 48 GB, RTX 5050 de 8 GB) el 24/9/2026, con llama-server CUDA
# b9775 y --fit (llama.cpp decide cuántas capas y expertos van a la GPU según la VRAM libre).
#   reasoning: None = el modelo no tiene interruptor (o piensa siempre, o nunca): no se pasa --reasoning y el
#              selector de razonamiento queda deshabilitado. "on"/"off" = valor por defecto, cambiable en la barra.
#   kv_q8:     caché KV en 8 bits desde 32K de contexto. En modelos densos deja la caché entera en la GPU
#              (Qwen3-4B a 32K: 77,6 tok/s con q8 contra 26 sin). En GLM (MoE) hunde el proceso del prompt
#              (31,7 contra 191 tok/s) y en Gemma 4 no hace falta (73 tok/s hasta 128K sin él).
#   moe:       lotes de 2048: GLM-4.7-Flash procesa el prompt a 451 tok/s en vez de 191.
# El primer patrón que aparece en el nombre del archivo (en minúsculas) gana.
PROFILES = (
    # MoE: 31 tok/s hasta 64K; 12/12 sin razonar en 151 s, 11/12 razonando en 1724 s. El q8 no cambia nada.
    ("qwen3.6-35b-a3b", {"reasoning": "off", "kv_q8": False, "moe": True}),
    ("glm-4.7-flash", {"reasoning": "off", "kv_q8": False, "moe": True}),
    ("gemma-4", {"reasoning": "on", "kv_q8": False, "moe": False}),
    ("qwen3.5", {"reasoning": "off", "kv_q8": True, "moe": False}),   # con razonamiento se enlaza en bucles de 8000 tokens
    ("thinking", {"reasoning": None, "kv_q8": True, "moe": False}),   # Qwen3-*-Thinking: piensa siempre
    ("deepseek-r1", {"reasoning": None, "kv_q8": True, "moe": False}),
)
DEFAULT_PROFILE = {"reasoning": None, "kv_q8": True, "moe": False}
KV_Q8_FROM = 32768
REASONING_CHOICES = {"razonar": "on", "sin razonar": "off"}    # lo que muestra la barra -> valor de --reasoning


def profile_for(model_path):
    low = os.path.basename(model_path).lower()
    for pat, prof in PROFILES:
        if pat in low:
            return dict(prof)
    return dict(DEFAULT_PROFILE)


def reasoning_for(model_path, choice=""):
    """El valor de --reasoning para ese modelo ("on"/"off") o None si el modelo no tiene interruptor.
    choice: lo elegido en la barra (una clave de REASONING_CHOICES); vacío o desconocido = el del perfil."""
    default = profile_for(model_path)["reasoning"]
    if default is None:
        return None
    return REASONING_CHOICES.get(choice, default)


def server_args(model_path, ctx, reasoning=None):
    """Los argumentos de afinado para llama-server (sin ejecutable, modelo, host ni puerto).
    Sin -ngl: --fit (activo por defecto) reparte capas y expertos entre GPU y CPU. Sin GPU usable queda todo en la CPU."""
    prof = profile_for(model_path)
    args = ["-c", str(int(ctx))]
    if prof["kv_q8"] and int(ctx) >= KV_Q8_FROM:
        args += ["-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on"]
    if prof["moe"]:
        args += ["-b", "2048", "-ub", "2048"]
    if reasoning in ("on", "off"):
        args += ["--reasoning", reasoning]
    return args


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

LOG_MAX_BYTES = 4 * 1024 * 1024     # al pasarlo, el registro actual pasa a llama-server.log.1 (se conserva uno anterior)

class LocalServer:
    """Un llama-server a la vez. ensure() reutiliza el que ya corre si es el mismo modelo y contexto."""

    def __init__(self, exe, log_dir, ctx=16384, threads=None):
        self.exe, self.log_dir, self.ctx, self.threads = exe, log_dir, int(ctx), threads
        self.proc = None
        self.model = None
        self.args = None
        self.port = None
        self._job = _make_kill_on_close_job()
        self._log = None
        self._log_start = 0     # desde dónde escribió el intento actual: _tail no muestra líneas de un intento anterior
        self._t0 = None
        self.log_path = os.path.join(log_dir, "llama-server.log")

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1" if self.port else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _tail(self, n=6):
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_start)
                text = f.read().decode("utf-8", errors="replace")
            lines = [l.rstrip() for l in text.splitlines() if l.strip() and not l.startswith("=====")]
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def _mark(self, text):
        """Una línea propia en el registro. Antes cada intento lo pisaba y no quedaba cómo había terminado: un modelo
        que «nunca cargó» dejaba un registro cortado a la mitad, sin decir si se canceló, se colgó o se cayó."""
        if self._log is None:
            return
        try:
            when = f" a los {time.time() - self._t0:.0f} s" if self._t0 else ""
            self._log.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} {text}{when}\n".encode("utf-8"))
            self._log.flush()
        except (OSError, ValueError):
            pass

    def _open_log(self, model_path, ctx):
        try:
            if os.path.getsize(self.log_path) > LOG_MAX_BYTES:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass
        self._log = open(self.log_path, "ab")
        self._log.seek(0, os.SEEK_END)
        self._t0 = None
        self._mark(f"intento: {os.path.basename(model_path)} (ctx {ctx})")
        self._log_start = self._log.tell()

    def ensure(self, model_path, ctx=None, cancel=None, timeout=300, reasoning=None):
        """Deja corriendo el servidor con ese modelo y devuelve la URL base. Bloquea hasta que responde /health.
        cancel: threading.Event opcional para abortar la espera. reasoning: "on"/"off"/None (ver reasoning_for)."""
        ctx = int(ctx or self.ctx)
        if self.alive() and self.model == (os.path.normcase(model_path), ctx, reasoning):
            return self.base
        self.stop()
        if not os.path.isfile(self.exe):
            raise LocalError(f"No se encuentra llama-server.exe en {self.exe}")
        if not os.path.isfile(model_path):
            raise LocalError(f"No se encuentra el modelo {model_path} (¿está conectado el pendrive?)")
        check_memory(model_path)
        os.makedirs(self.log_dir, exist_ok=True)
        self.port = _free_port()
        args = [self.exe, "-m", model_path, "--host", "127.0.0.1", "--port", str(self.port),
                "-np", "1", "--jinja", "--no-webui"] + server_args(model_path, ctx, reasoning)
        if self.threads:
            args += ["-t", str(self.threads)]
        self.args = args
        self._open_log(model_path, ctx)
        flags = 0x08000000   # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         cwd=os.path.dirname(self.exe), creationflags=flags)
        except OSError as e:
            self._mark(f"no arrancó: {e}")
            self.stop()
            raise LocalError(f"No se pudo arrancar llama-server: {e}")
        if self._job:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(wintypes.HANDLE(self._job), wintypes.HANDLE(int(self.proc._handle)))
            except Exception:
                pass
        self.model = (os.path.normcase(model_path), ctx, reasoning)
        t0 = self._t0 = time.time()
        url = f"http://127.0.0.1:{self.port}/health"
        while True:
            if cancel is not None and cancel.is_set():
                self._mark("cancelado por el usuario mientras cargaba")
                self.stop()
                raise LocalError("Cancelado mientras se cargaba el modelo.")
            if self.proc.poll() is not None:
                self._mark(f"llama-server terminó solo (código {self.proc.returncode})")
                tail = self._tail()
                self.stop()
                low = tail.lower()
                mem = memory_status()
                if mem and any(k in low for k in ("unable to allocate", "failed to allocate", "out of memory", "bad_alloc")):
                    raise LocalError(memory_message(os.path.basename(model_path), os.path.getsize(model_path), mem) + "\n" + tail)
                raise LocalError("llama-server terminó al arrancar (¿poca memoria o modelo incompatible?).\n" + tail)
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        self._mark("listo")
                        return self.base
            except urllib.error.HTTPError:
                pass            # 503 mientras carga el modelo
            except (OSError, ValueError):
                pass
            if time.time() - t0 > timeout:
                self._mark(f"sin respuesta tras {timeout} s: se detiene")
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
            self._mark("detenido por la app")
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        self.port = None


def format_size(n):
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"
