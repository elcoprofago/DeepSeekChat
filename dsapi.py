"""Acceso a la API de DeepSeek y configuración local. Sin dependencias externas.

Todo lo que no es ventana vive acá, para poder probarlo sin abrir la interfaz.
"""
import base64
import ctypes
import http.client
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from ctypes import wintypes

BASE = "https://api.deepseek.com"

# Si /models no responde, estos son los que la API informó en la última verificación.
FALLBACK_MODELS = [
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "efforts": ["low", "high", "max"], "context": 1048576},
    {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash", "efforts": ["low", "high", "max"], "context": 1048576},
]

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

_LANG_BY_EXT = {
    ".py": "python", ".pyw": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
    ".tsx": "tsx", ".json": "json", ".html": "html", ".css": "css", ".md": "markdown",
    ".cs": "csharp", ".sql": "sql", ".sh": "bash", ".bat": "bat", ".ps1": "powershell",
    ".yml": "yaml", ".yaml": "yaml", ".xml": "xml", ".java": "java", ".go": "go", ".rs": "rust",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".php": "php", ".rb": "ruby", ".txt": "",
}


class ApiError(Exception):
    def __init__(self, message, status=None, timeout=False):
        super().__init__(message)
        self.status = status
        self.timeout = timeout   # el servidor no llegó a responder a tiempo (distinto de una respuesta de error)


class AttachError(ValueError):
    """Un archivo adjunto que no se puede leer como texto o que excede el límite."""


# ---------------------------------------------------------------- errores HTTP

def _friendly(status, detail):
    base = {
        400: "Pedido inválido",
        401: "API key inválida o vencida",
        402: "Saldo insuficiente",
        422: "Parámetro inválido",
        429: "Demasiados pedidos seguidos",
        500: "Falla del servidor de DeepSeek",
        503: "Servidor de DeepSeek sobrecargado",
    }.get(status, f"Error HTTP {status}")
    return f"{base}: {detail}" if detail else base


def _from_http_error(e):
    detail = ""
    try:
        raw = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw)
            err = obj.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) or raw
        except ValueError:
            detail = raw
    except Exception:
        pass
    return ApiError(_friendly(e.code, str(detail).strip()[:400]), status=e.code)


def _open(req, timeout, who="DeepSeek"):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _from_http_error(e)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise ApiError(f"{who} no respondió a tiempo", timeout=True)
        raise ApiError(f"Sin conexión con {who}: {e.reason}")
    except TimeoutError:
        raise ApiError(f"{who} no respondió a tiempo", timeout=True)
    except OSError as e:
        raise ApiError(f"Sin conexión con {who}: {e}")


def _get_json(path, key, timeout=30):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {key}"})
    with _open(req, timeout) as resp:
        try:
            return json.loads(resp.read().decode("utf-8"))
        except ValueError:
            raise ApiError("Respuesta ilegible de DeepSeek")


# ---------------------------------------------------------------- consultas

def list_models(key):
    data = _get_json("/models", key)
    out = []
    for m in data.get("data", []):
        out.append({
            "id": m["id"],
            "name": m.get("name") or m["id"],
            "efforts": list((m.get("effort") or {}).get("supported_levels") or []),
            "context": m.get("context_window") or 0,
        })
    if not out:
        raise ApiError("DeepSeek no devolvió ningún modelo")
    return out


def get_balance(key):
    """Devuelve {'available': bool, 'text': 'US$ 5.93'}. El saldo es prepago (no hay plan)."""
    data = _get_json("/user/balance", key)
    infos = data.get("balance_infos") or []
    parts = []
    for b in infos:
        cur, tot = b.get("currency", ""), b.get("total_balance", "?")
        parts.append(f"US$ {tot}" if cur == "USD" else f"{tot} {cur}")
    return {"available": bool(data.get("is_available")), "text": " + ".join(parts) or "sin datos"}


# ---------------------------------------------------------------- chat en streaming

class ChatStream:
    """Itera eventos ('reasoning'|'content'|'usage'|'tool_calls'|'finish', valor). cancel() lo corta desde otro hilo.

    Sirve igual para DeepSeek y para un servidor local compatible con OpenAI (base=http://127.0.0.1:PUERTO/v1, sin key).
    'tool_calls' llega una sola vez, ya armado, justo antes de 'finish': [{'id', 'type', 'function': {'name', 'arguments'}}].
    """

    # deepseek-v4-pro con streaming y herramientas a veces no llega ni a mandar la cabecera HTTP (medido: 2 de 6
    # pedidos idénticos; sin herramientas o con deepseek-flash, 0 de 12). Se corta pronto y se reintenta.
    OPEN_TIMEOUT = 25
    OPEN_RETRIES = 2

    def __init__(self, key, model, messages, effort=None, max_tokens=32768, base=BASE, tools=None, timeout=120,
                 open_timeout=None, retries=None):
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": int(max_tokens),
        }
        if effort:
            body["reasoning_effort"] = effort
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._who = "DeepSeek" if base == BASE else "el modelo local"
        self._timeout = timeout
        remote = base == BASE
        # un modelo local puede tardar minutos en procesar el pedido antes de contestar: ahí no se corta ni se reintenta
        self._open_timeout = open_timeout if open_timeout is not None else (self.OPEN_TIMEOUT if remote else timeout)
        self._retries = retries if retries is not None else (self.OPEN_RETRIES if remote else 0)
        self._req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers)
        self._resp = None
        self._cancelled = False
        self._calls = {}       # índice -> llamada en armado: los argumentos llegan en trozos
        self._calls_done = False

    def _add_call_deltas(self, deltas):
        for tc in deltas:
            idx = tc.get("index")
            if idx is None:
                # servidores que no numeran: una llamada nueva trae 'id'; los trozos siguientes no
                idx = len(self._calls) if (tc.get("id") or not self._calls) else max(self._calls)
            cur = self._calls.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                cur["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                cur["function"]["arguments"] += fn["arguments"]

    def _take_calls(self):
        if self._calls_done or not self._calls:
            return None
        self._calls_done = True
        out = [self._calls[i] for i in sorted(self._calls)]
        for n, c in enumerate(out):
            if not c["id"]:
                c["id"] = f"call_{n}_{abs(hash(c['function']['name'])) % 10**6}"   # algunos servidores no mandan id
        return out

    def cancel(self):
        self._cancelled = True
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    def _open_interruptible(self):
        """_open en un hilo aparte, para poder cancelar sin esperar los 25 s (o más) de la cabecera. Devuelve la
        respuesta, o None si se canceló; lo que el hilo abra después de cancelar se cierra solo."""
        box = {}

        def run():
            try:
                box["resp"] = _open(self._req, self._open_timeout, self._who)
            except BaseException as e:      # se relanza en el hilo que llama
                box["err"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.1)
            if self._cancelled:
                def cleanup():
                    t.join()
                    r = box.get("resp")
                    if r is not None:
                        try:
                            r.close()
                        except Exception:
                            pass
                threading.Thread(target=cleanup, daemon=True).start()
                return None
        if "err" in box:
            raise box["err"]
        return box["resp"]

    def _relax_read_timeout(self):
        """urlopen aplica un solo timeout a la cabecera y a todas las lecturas siguientes. La cabecera necesita uno
        corto (ver OPEN_TIMEOUT); una vez que la respuesta empezó, las pausas entre trozos pueden ser largas."""
        try:
            self._resp.fp.raw._sock.settimeout(self._timeout)
        except Exception:
            pass   # detalle interno de http.client: si cambia, queda el timeout corto y se verá en los tests

    def __iter__(self):
        if self._cancelled:
            return
        for intento in range(self._retries + 1):
            try:
                self._resp = self._open_interruptible()
                if self._resp is None:      # cancelado mientras se esperaba la cabecera
                    return
                break
            except ApiError as e:
                if self._cancelled:
                    return
                if e.timeout and intento < self._retries:
                    yield ("notice", f"{self._who} no respondió en {self._open_timeout} s; reintentando ({intento + 2}/{self._retries + 1})…")
                    continue
                if e.timeout:
                    raise ApiError(f"{self._who} no respondió tras {self._retries + 1} intentos de {self._open_timeout} s. "
                                   "Probá de nuevo o cambiá a deepseek-flash.")
                raise
        self._relax_read_timeout()
        try:
            for raw in self._resp:
                if self._cancelled:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    calls = self._take_calls()
                    if calls:
                        yield ("tool_calls", calls)
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    err = obj["error"]
                    raise ApiError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
                if obj.get("usage"):
                    yield ("usage", obj["usage"])
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        yield ("reasoning", d["reasoning_content"])
                    if d.get("content"):
                        yield ("content", d["content"])
                    if d.get("tool_calls"):
                        self._add_call_deltas(d["tool_calls"])
                    if ch.get("finish_reason"):
                        calls = self._take_calls()
                        if calls:
                            yield ("tool_calls", calls)
                        yield ("finish", ch["finish_reason"])
        except (OSError, ValueError, AttributeError, http.client.HTTPException) as e:
            # Cancelar cierra el socket desde otro hilo, y el iterador interno de http.client puede fallar de
            # formas dispares (AttributeError sobre un buffer ya cerrado, etc.). Si fue a pedido, no es un error.
            if self._cancelled:
                return
            raise ApiError(f"Se cortó la conexión: {e}")
        finally:
            try:
                self._resp.close()
            except Exception:
                pass


# ---------------------------------------------------------------- adjuntos

def read_text_file(path):
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise AttachError(f"{os.path.basename(path)} pesa {size // 1024} KB; el máximo por archivo es {MAX_FILE_BYTES // 1024} KB")
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:8192]:
        raise AttachError(f"{os.path.basename(path)} parece un archivo binario, no de texto")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise AttachError(f"{os.path.basename(path)}: no se pudo decodificar como texto")


def build_message(text, paths):
    """Arma el contenido del mensaje de usuario con los archivos embebidos en bloques de código."""
    total = 0
    parts = [text.strip()] if text.strip() else []
    for p in paths:
        content = read_text_file(p)
        total += len(content.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise AttachError(f"Los adjuntos suman más de {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
        name = os.path.basename(p)
        lang = _LANG_BY_EXT.get(os.path.splitext(name)[1].lower(), "")
        fence = "```"
        while fence in content:
            fence += "`"
        parts.append(f"Archivo adjunto: {name}\n{fence}{lang}\n{content.rstrip()}\n{fence}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- DPAPI (la key nunca queda en texto plano)

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # Las dos funciones comparten la misma firma de siete argumentos.
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI falló: no se pudo (des)cifrar. La key guardada es de otro usuario o de otra instalación de Windows.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text):
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(b64):
    return _dpapi(base64.b64decode(b64), False).decode("utf-8")


# ---------------------------------------------------------------- configuración

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def portable_root():
    """Carpeta raíz del programa si corre en modo portable (marcado por un archivo 'portable.flag' en la carpeta de
    la app o en su padre, que es donde lo deja build_portable.py); None si corre instalado / desde el repositorio."""
    for d in (APP_DIR, os.path.dirname(APP_DIR)):
        if os.path.isfile(os.path.join(d, "portable.flag")):
            return d
    return None


class Config:
    DEFAULTS = {
        "model": "deepseek-v4-pro",
        "effort": "",
        "system_prompt": "",
        "theme": "oscuro",
        "font_size": 11,
        "max_tokens": 32768,
        "api_key_enc": "",       # DPAPI: atada al usuario de Windows (modo instalado)
        "api_key_pw": "",        # cifrada con contraseña (modo portable); ver secret.py
        "model_dirs": [],        # carpetas extra donde buscar modelos .gguf
        "llama_server_path": "", # vacío = buscar junto al programa
        "local_ctx": 16384,      # contexto con que se arranca un modelo local
        "last_session": "",
        "approval": "ask",       # ask | edits | all
        "sidebar_w": 260,
        "sidebar_visible": True,
        "win_geometry": "",      # "ANCHOxALTO+X+Y" de la ventana en estado normal
        "win_zoomed": False,
        "token_budget": 1000000, # 100% de la barra de consumo acumulado del medidor
        "remote_enabled": False, # acceso desde el celular (remote.py); apagado salvo que el usuario lo encienda
        "remote_port": 8765,
        "remote_token": "",      # se genera al encender por primera vez
    }

    def __init__(self, directory=None):
        self.root = None if directory else portable_root()
        self.portable = self.root is not None
        self.dir = directory or os.environ.get("DSCHAT_CONFIG_DIR") or (
            os.path.join(self.root, "data") if self.root else
            os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "DeepSeekChat"))
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "config.json")
        self.history_dir = os.path.join(self.dir, "historial")
        self.sessions_dir = os.path.join(self.dir, "sessions")
        self.load_warning = ""
        self.data = dict(self.DEFAULTS)
        self._key = ""            # la key en claro, solo en memoria
        self.load()
        if self.data.get("api_key_enc") and not self.data.get("api_key_pw"):
            try:
                self._key = unprotect(self.data["api_key_enc"])
            except (OSError, ValueError):
                self._key = ""

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("no es un objeto JSON")
            self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except (OSError, ValueError) as e:
            # No se pisa: se aparta el archivo dañado para no perder la key ni los ajustes.
            bad = self.path + ".dañado"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            self.load_warning = f"La configuración estaba dañada ({e}); se guardó una copia en {bad} y se usan valores por defecto."

    def save(self):
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    # --- la key: en disco solo cifrada (DPAPI o contraseña); en memoria en claro

    @property
    def api_key(self):
        return self._key

    @property
    def key_mode(self):
        """'password' | 'dpapi' | 'none' según cómo esté guardada."""
        if self.data.get("api_key_pw"):
            return "password"
        return "dpapi" if self.data.get("api_key_enc") else "none"

    @property
    def needs_unlock(self):
        return self.key_mode == "password" and not self._key

    def unlock(self, password):
        """Descifra la key guardada con contraseña. True si la contraseña era correcta."""
        import secret
        try:
            self._key = secret.decrypt(self.data["api_key_pw"], password)
            return True
        except secret.WrongPassword:
            return False

    def set_api_key(self, key, password=None):
        """Guarda la key. Con contraseña queda cifrada con ella (portable); sin contraseña, con la DPAPI del usuario
        de Windows, salvo en modo portable, donde se exige contraseña (la DPAPI no sobreviviría a otra PC)."""
        if key and self.portable and not password:
            raise ValueError("En modo portable la key se guarda con contraseña.")
        self._key = key or ""
        self.data["api_key_enc"] = self.data["api_key_pw"] = ""
        if key and password:
            import secret
            self.data["api_key_pw"] = secret.encrypt(key, password)
        elif key:
            self.data["api_key_enc"] = protect(key)
        self.save()

    def set_session_key(self, key):
        """La key vale solo mientras el programa está abierto; no se escribe nada en disco."""
        self._key = key or ""

    def has_key(self):
        return bool(self.data.get("api_key_enc") or self.data.get("api_key_pw"))

    @staticmethod
    def mask(key):
        return "" if not key else (key[:3] + "…" + key[-4:] if len(key) > 10 else "…")
