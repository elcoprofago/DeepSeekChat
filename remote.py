"""Acceso remoto: un servidor web mínimo dentro de la app, para seguir y dirigir al agente desde el navegador del celular.

No es el «Remote Control» de Claude Code: aquel pasa por los servidores de Anthropic; esto es un servidor propio en esta PC
y el celular tiene que poder llegar a ella (misma red Wi-Fi, o una VPN como Tailscale). No sale a internet por sí solo.

Seguridad, en orden de importancia:
  * Apagado por defecto. Solo escucha si el usuario lo enciende.
  * Todo lo que actúa o muestra datos exige el token (128 bits al azar) en la cabecera Authorization. La página HTML
    en sí es estática y no lleva secretos; el token viaja en el fragmento de la URL (#...), que el navegador no envía.
  * El celular puede: ver la conversación, mandar mensajes, detener, continuar y responder los permisos pendientes.
    No puede: cambiar el modo de permisos, la carpeta de trabajo, la configuración ni la key. El filtro de comandos
    peligrosos y los permisos que elija la PC siguen rigiendo igual.
  * HTTP simple, sin cifrado: en una Wi-Fi ajena el token podría verse. Para eso, Tailscale (cifra todo) en vez de abrir el puerto.

Dos clientes: la página web de abajo (protocolo 1, sin cambios) y la app MovilDeep (protocolo 2: /api/info, sesiones,
modelos, subida de adjuntos y el contador de ejecuciones run.seq). Si cambia lo que MovilDeep espera de la API, hay que
subir PROTOCOL: la app lo compara al conectar y no usa una API que no conoce.
"""
import base64
import binascii
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dsapi
import ocr
import version

DEFAULT_PORT = 8765
PROTOCOL = 2
MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = 21 * 1024 * 1024          # 15 MB en base64 más el JSON; solo para /api/upload
MAX_UPLOAD_FILE = 15 * 1024 * 1024
MAX_TEXT = 20000
UPLOAD_DIR = "movildeep-adjuntos"
QR_PREFIX = "movildeep:"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def new_token():
    return secrets.token_urlsafe(16)          # 128 bits


def lan_ip():
    """La IP con la que esta PC sale a la red (sin mandar ningún paquete). 127.0.0.1 si no hay red."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ipv4s():
    """Las IPv4 de las interfaces de esta PC, sin mandar paquetes (Windows las resuelve a partir del propio nombre)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    out = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def tailscale_ip(addrs=None):
    """La IPv4 de Tailscale de esta PC (la primera local en 100.64.0.0/10), o "" si no hay. No depende del comando
    tailscale ni de que esté en el PATH. addrs: para tests."""
    for ip in (local_ipv4s() if addrs is None else addrs):
        try:
            if ipaddress.ip_address(ip) in TAILSCALE_NET:
                return ip
        except ValueError:
            continue
    return ""


def qr_payload(lan, ts, port, token):
    """Lo que lleva el QR de emparejamiento de MovilDeep: primero la IP local y, si hay, la de Tailscale."""
    hosts = [lan] + ([ts] if ts and ts != lan else [])
    return QR_PREFIX + json.dumps({"v": 1, "hosts": hosts, "port": port, "token": token}, separators=(",", ":"))


# ---------------------------------------------------------------- adjuntos subidos desde el celular

class UploadError(Exception):
    def __init__(self, msg, status=422):
        super().__init__(msg)
        self.status = status


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name):
    """El nombre base de un archivo subido, válido en Windows: sin carpetas, sin '..', sin caracteres prohibidos."""
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in name).strip().rstrip(". ")
    if not name.strip("."):
        name = "archivo"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 150:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        name = stem[:150 - len(ext)] + ext
    return name


def human_size(n):
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB".replace(".", ",")
    return f"{n // 1024} KB" if n >= 1024 else f"{n} bytes"


def save_unique(workspace, name, data):
    """Guarda data en <workspace>\\movildeep-adjuntos\\<name> sin pisar nada: si ya existe, «name (2).ext», etc.
    Escribe a un temporal en la misma carpeta y lo renombra al final (en Windows os.rename no pisa): una subida
    cortada no deja un archivo a medias con el nombre final. Devuelve la ruta."""
    folder = os.path.abspath(os.path.join(workspace, UPLOAD_DIR))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".subiendo-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        stem, ext = os.path.splitext(name)
        for i in range(1, 10000):
            dest = os.path.join(folder, name if i == 1 else f"{stem} ({i}){ext}")
            if os.path.dirname(os.path.abspath(dest)) != folder:
                raise UploadError("nombre de archivo inválido")
            if os.path.lexists(dest):
                continue
            try:
                os.rename(tmp, dest)
                return dest
            except FileExistsError:
                continue
        raise UploadError(f"Hay demasiados archivos llamados {name} en {UPLOAD_DIR}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _looks_text(data):
    if b"\x00" in data[:8192]:                  # el mismo criterio que dsapi.read_text_file
        return False
    for enc in ("utf-8-sig", "cp1252"):
        try:
            data.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def prepare_upload(name, data, workspace, tesseract):
    """Procesa un archivo subido. Devuelve {name, kind, note, att_name, text}: text (o None) es lo que se incrusta en el
    mensaje como adjunto de texto att_name; note (o "") es el aviso para el agente. UploadError si se rechaza."""
    name = safe_name(name)
    if len(data) > MAX_UPLOAD_FILE:
        raise UploadError(f"{name} pesa {human_size(len(data))}; el máximo por archivo es 15 MB", 413)
    ws = workspace if workspace and os.path.isdir(workspace) else ""
    tmpdir = tempfile.mkdtemp(prefix="movildeep_")
    try:
        tmp = os.path.join(tmpdir, name)
        with open(tmp, "wb") as f:
            f.write(data)
        extra = ""
        if ocr.is_image(name):
            if tesseract:
                try:
                    body = ocr.run(tesseract, tmp) or "OCR: no se encontró texto"
                except (ocr.OcrTimeout, ocr.OcrError, ocr.OcrUnavailable) as e:
                    body = str(e)
                if ws:
                    note = f"La imagen original {name} se guardó en {save_unique(ws, name, data)}"
                else:
                    note = f"La imagen original {name} no se guardó: la sesión no tiene carpeta de trabajo"
                return {"name": name, "kind": "ocr", "note": note, "att_name": name + ".ocr.txt",
                        "text": f"Texto extraído por OCR de la imagen {name}\n\n{body}"}
            extra = " (OCR no disponible en esta PC)"
        elif _looks_text(data):
            try:
                text = dsapi.read_text_file(tmp)
            except dsapi.AttachError as e:
                raise UploadError(str(e), 413) from None
            return {"name": name, "kind": "text", "note": "", "att_name": name, "text": text}
        if not ws:
            raise UploadError(f"No se puede recibir {name}: no es texto y la sesión no tiene carpeta de trabajo donde "
                              f"guardarlo{extra}")
        path = save_unique(ws, name, data)
        return {"name": name, "kind": "stored", "att_name": None, "text": None,
                "note": f"Se subió {name} ({human_size(len(data))}) a {path}; no es texto, no se incluye su contenido"
                        + extra}
    except OSError as e:
        raise UploadError(f"No se pudo guardar {name}: {e}", 500) from None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fingerprint(m):
    """Huella barata de un mensaje: sirve para que el celular note que el historial cambió por debajo (p. ej. se retiró un mensaje)."""
    return f"{m.get('role')}:{len(str(m.get('content') or ''))}:{len(m.get('tool_calls') or [])}"


class RemoteServer:
    """bridge debe tener: state(n, fp, sid) -> dict, send(text, attachments) -> (ok, msg), cancel() -> (ok, msg),
    continue_run() -> (ok, msg), confirm(cid, allow) -> (ok, msg), sessions() -> list, switch_session(sid) -> (ok, msg),
    new_session() -> (ok, msg), models() -> list, set_model(mid) -> (ok, msg), upload(name, data) -> dict (o
    UploadError). Todas se llaman desde hilos del servidor."""

    def __init__(self, bridge, token, port=DEFAULT_PORT, host="0.0.0.0"):
        if not token or len(token) < 16:
            raise ValueError("token demasiado corto")
        self.bridge, self.token, self.port, self.host = bridge, token, port, host
        self.httpd = None
        self.thread = None

    def start(self):
        outer = self

        class Handler(_Handler):
            server_ref = outer
        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, kwargs={"poll_interval": 0.2})
        self.thread.start()
        return self.port

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def link(self):
        return f"http://{lan_ip()}:{self.port}/#{self.token}"

    def qr_payload(self):
        """Contenido del QR de emparejamiento de MovilDeep (IP local y, si hay, la de Tailscale)."""
        return qr_payload(lan_ip(), tailscale_ip(), self.port, self.token)


class _Handler(BaseHTTPRequestHandler):
    server_ref = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                    # ni ruido ni rastros; el token no viaja en la URL de todos modos

    # ------------------------------------------------------------ utilidades

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _authorized(self):
        got = self.headers.get("Authorization", "")
        want = "Bearer " + self.server_ref.token
        if hmac.compare_digest(got.encode("utf-8", "replace"), want.encode("utf-8")):
            return True
        time.sleep(0.4)                         # frena la fuerza bruta sin molestar al que acierta
        self._json(401, {"error": "token inválido"})
        return False

    def _body(self, limit=MAX_BODY):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            self.close_connection = True        # el cuerpo queda sin leer: la conexión no se puede reutilizar
            self._json(413, {"error": "pedido demasiado grande" if limit == MAX_BODY
                             else "el archivo supera el máximo de 15 MB"})
            return None
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json(415, {"error": "se espera application/json"})     # así un formulario de otro sitio no puede pegarle
            return None
        raw = self.rfile.read(n)
        if len(raw) < n:                        # la subida se cortó a la mitad
            self.close_connection = True
            return None
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
            return d if isinstance(d, dict) else {}
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "JSON inválido"})
            return None

    # ------------------------------------------------------------ rutas

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/state":
            if not self._authorized():
                return
            q = parse_qs(u.query)
            try:
                n = int((q.get("n") or ["0"])[0])
            except ValueError:
                n = 0
            try:
                self._json(200, self.server_ref.bridge.state(n, (q.get("fp") or [""])[0], (q.get("sid") or [""])[0]))
            except Exception as e:              # noqa: BLE001
                self._json(503, {"error": f"la app no respondió: {e}"})
        elif u.path == "/api/info":
            if self._authorized():
                self._json(200, {"app": "DeepSeekChat", "version": version.VERSION, "protocol": PROTOCOL})
        elif u.path in ("/api/sessions", "/api/models"):
            if not self._authorized():
                return
            b = self.server_ref.bridge
            try:
                self._json(200, b.sessions() if u.path == "/api/sessions" else b.models())
            except Exception as e:              # noqa: BLE001
                self._json(503, {"error": f"la app no respondió: {e}"})
        else:
            self._json(404, {"error": "no existe"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/send", "/api/cancel", "/api/continue", "/api/confirm", "/api/session/switch",
                          "/api/session/new", "/api/model", "/api/upload", "/api/power"):
            self._json(404, {"error": "no existe"})
            return
        if not self._authorized():
            return
        d = self._body(MAX_UPLOAD_BODY if u.path == "/api/upload" else MAX_BODY)
        if d is None:
            return
        b = self.server_ref.bridge
        if u.path == "/api/upload":
            self._upload(b, d)
            return
        try:
            if u.path == "/api/send":
                text = str(d.get("text") or "").strip()
                att = d.get("attachments") or []
                if not isinstance(att, list) or not all(isinstance(x, str) for x in att):
                    self._json(400, {"error": "attachments debe ser una lista de ids"})
                    return
                if not text and not att:
                    self._json(400, {"error": "mensaje vacío"})
                    return
                ok, msg = b.send(text[:MAX_TEXT], att)
            elif u.path == "/api/session/switch":
                ok, msg = b.switch_session(str(d.get("id") or ""))
            elif u.path == "/api/session/new":
                ok, msg = b.new_session()
            elif u.path == "/api/model":
                ok, msg = b.set_model(str(d.get("id") or ""))
            elif u.path == "/api/cancel":
                ok, msg = b.cancel()
            elif u.path == "/api/continue":
                ok, msg = b.continue_run()
            elif u.path == "/api/power":
                ok, msg = b.power(str(d.get("action") or ""))
            else:
                ok, msg = b.confirm(d.get("id"), bool(d.get("allow")))
        except Exception as e:                  # noqa: BLE001
            self._json(503, {"error": f"la app no respondió: {e}"})
            return
        self._json(200 if ok else 409, {"ok": ok, "msg": msg})

    def _upload(self, b, d):
        name = str(d.get("name") or "").strip()
        if not name:
            self._json(400, {"error": "falta el nombre del archivo"})
            return
        try:
            data = base64.b64decode(str(d.get("data_base64") or ""), validate=True)
        except (binascii.Error, ValueError):
            self._json(400, {"error": "data_base64 inválido"})
            return
        try:
            self._json(200, b.upload(name, data))
        except UploadError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:                  # noqa: BLE001
            self._json(503, {"error": f"la app no respondió: {e}"})


class AppBridge:
    """Une el servidor con la ventana. Todo corre en el hilo de tkinter (app.call_ui), así nunca se lee la interfaz
    ni se manda nada desde un hilo del servidor."""

    MAX_ITEM = 20000

    def __init__(self, app):
        self.app = app
        self.uploads = {}               # id -> lo que preparó prepare_upload, hasta que un mensaje lo use
        self._lock = threading.Lock()

    @staticmethod
    def _items(m):
        import chatview
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "user":
            text, files = chatview.split_user_content(content)
            return [{"k": "user", "text": text[:AppBridge.MAX_ITEM], "files": [f.replace("\\", "/").split("/")[-1] for f in files]}]
        if role == "assistant":
            out = []
            reasoning = str(m.get("_reasoning") or "")
            if content or reasoning:
                out.append({"k": "bot", "text": content[:AppBridge.MAX_ITEM], "reasoning": reasoning[:4000], "rlen": len(reasoning),
                            "final": not m.get("tool_calls")})
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"]["arguments"])
                except (ValueError, KeyError, TypeError):
                    args = c.get("function", {}).get("arguments", "")
                out.append({"k": "tool", "text": chatview.summarize_call(c.get("function", {}).get("name", "?"), args)})
            return out
        if role == "tool":
            lines = content.strip().splitlines() or [""]
            return [{"k": "res", "text": lines[0][:200], "more": "\n".join(lines[1:])[:1500],
                     "bad": chatview.result_is_problem(content)}]
        return []

    def _state(self, n, fp, sid):
        a = self.app
        s = a.sess
        msgs = list(s.messages)
        reset = sid != s.id or n < 0 or n > len(msgs) or (n > 0 and fp != fingerprint(msgs[n - 1]))
        start = 0 if reset else n
        items = []
        for m in msgs[start:]:
            items.extend(self._items(m))
        p = a._pending_confirm
        return {
            "sid": s.id, "reset": reset, "items": items, "total": len(msgs),
            "fp": fingerprint(msgs[-1]) if msgs else "",
            "busy": a.busy, "status": a.remote_status(), "title": s.title or "Sesión nueva",
            "live": a._live, "can_continue": a._can_continue,
            "confirm": None if p is None else {"id": p["id"], "kind": p["kind"], "title": p["title"][:300], "detail": p["detail"][:6000]},
            "run": {"seq": a._run_seq, "outcome": a._run_outcome, "msg": a._run_msg},
            "balance": a.balance_status(),
        }

    def state(self, n, fp, sid):
        return self.app.call_ui(lambda: self._state(n, fp, sid))

    def send(self, text, attachments=()):
        with self._lock:
            items = [self.uploads.get(i) for i in attachments]
        if any(it is None for it in items):
            return False, ("Un adjunto ya no está en la PC (¿se reinició DeepSeekChat?). Quitalo y volvé a "
                           "adjuntarlo.")
        ok, msg = self.app.call_ui(lambda: self.app.remote_send(text, items))
        if ok:
            with self._lock:
                for i in attachments:
                    self.uploads.pop(i, None)
        return ok, msg

    def sessions(self):
        return self.app.call_ui(self.app.remote_sessions)

    def switch_session(self, sid):
        return self.app.call_ui(lambda: self.app.remote_switch_session(sid))

    def new_session(self):
        return self.app.call_ui(self.app.remote_new_session)

    def models(self):
        return self.app.call_ui(self.app.remote_model_list)

    def set_model(self, mid):
        return self.app.call_ui(lambda: self.app.remote_set_model(mid))

    def upload(self, name, data):
        """Corre en el hilo del servidor (el OCR puede tardar hasta un minuto y call_ui espera 8 s): de la ventana
        solo lee la carpeta de trabajo."""
        a = self.app
        ws = a.call_ui(lambda: a.sess.workspace or "")
        exe = ocr.find_tesseract(a.cfg.root if a.cfg.portable else None) if ocr.is_image(safe_name(name)) else None
        item = prepare_upload(name, data, ws, exe)
        uid = secrets.token_urlsafe(9)
        with self._lock:
            self.uploads[uid] = item
        return {"id": uid, "name": item["name"], "kind": item["kind"], "note": item["note"]}

    def cancel(self):
        return self.app.call_ui(self.app.remote_cancel)

    def continue_run(self):
        return self.app.call_ui(self.app.remote_continue)

    def confirm(self, cid, allow):
        return self.app.call_ui(lambda: self.app.remote_confirm(cid, allow))

    def power(self, action):
        return self.app.call_ui(lambda: self.app.remote_power(action))


PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>DeepSeek Chat remoto</title>
<style>
:root{--bg:#14202e;--panel:#1c2d42;--fg:#d4e1f0;--muted:#8aa1bb;--user:#5cb3ff;--bot:#4fd1c5;--final:#8be9fd;
--acc:#0e639c;--err:#f48771;--bd:#2c4260;--code:#0d1826}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,Segoe UI,sans-serif}
body{display:flex;flex-direction:column;height:100dvh}
header{padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--bd);display:flex;gap:10px;align-items:baseline}
#title{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#status{color:var(--muted);font-size:13px}
#log{flex:1;overflow-y:auto;padding:10px 14px}
.hdr{font-weight:700;margin-top:14px}.hdr.user{color:var(--user)}.hdr.bot{color:var(--bot)}
.txt{white-space:pre-wrap;word-wrap:break-word}
.final{color:var(--final)}
details.reason{color:var(--muted);font-style:italic;font-size:14px;margin:2px 0}
details.reason summary{cursor:pointer}
.tool{font:13px Consolas,ui-monospace,monospace;color:#6cb6ff;margin:4px 0 0 10px;word-wrap:break-word}
.res{font:12px Consolas,ui-monospace,monospace;color:var(--muted);margin-left:22px;white-space:pre-wrap;word-wrap:break-word}
.res.bad{color:var(--err)}
.note{color:var(--muted);font-size:13px;font-style:italic}
.live{color:var(--muted);white-space:pre-wrap;word-wrap:break-word}
#confirm{display:none;background:var(--panel);border-top:2px solid #e6b422;padding:10px 14px}
#confirm b{color:#e6b422}
#confirm pre{background:var(--code);max-height:30vh;overflow:auto;padding:8px;font-size:12px;white-space:pre-wrap;word-wrap:break-word;margin:6px 0}
#bar{display:flex;gap:8px;padding:8px 10px calc(8px + env(safe-area-inset-bottom));background:var(--panel);border-top:1px solid var(--bd)}
textarea{flex:1;background:var(--code);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px;font:inherit;resize:none;height:64px}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:0 16px;font:inherit;min-height:44px}
button.sec{background:var(--bd)}button.bad{background:#a33}
button:disabled{opacity:.45}
#login{display:none;padding:24px 14px}
#login input{width:100%;padding:10px;margin:10px 0;background:var(--code);color:var(--fg);border:1px solid var(--bd);border-radius:8px;font:inherit}
</style></head><body>
<header><div id="title">DeepSeek Chat</div><div id="status">conectando…</div></header>
<div id="login"><div>Pegá el token de acceso (el que muestra la ventana «Remoto» de la PC):</div>
<input id="tok" autocomplete="off" autocapitalize="off" spellcheck="false"><button id="tokgo">Entrar</button></div>
<div id="log"></div>
<div id="confirm"><b id="ctitle"></b><pre id="cdetail"></pre>
<div style="display:flex;gap:8px"><button id="cyes" style="flex:1">Permitir</button><button id="cno" class="bad" style="flex:1">Denegar</button></div></div>
<div id="bar"><textarea id="inp" placeholder="Escribí una instrucción…"></textarea>
<div style="display:flex;flex-direction:column;gap:6px"><button id="send">Enviar</button><button id="cont" class="sec" style="display:none">Continuar</button></div></div>
<script>
"use strict";
const $=id=>document.getElementById(id);
let tok="";try{tok=localStorage.getItem("dsr_tok")||""}catch(e){}
if(location.hash.length>8){tok=location.hash.slice(1);try{localStorage.setItem("dsr_tok",tok)}catch(e){}history.replaceState(null,"",location.pathname)}
let n=0,fp="",sid="",busy=false,cid=null,lastLive="",timer=null;
const log=$("log");
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function near(){return log.scrollHeight-log.scrollTop-log.clientHeight<80}
function add(it){
  if(it.k==="user"){log.append(el("div","hdr user","Vos"),el("div","txt",it.text));
    (it.files||[]).forEach(f=>log.append(el("div","note","📎 "+f)))}
  else if(it.k==="bot"){log.append(el("div","hdr bot","DeepSeek"));
    if(it.reasoning){const d=el("details","reason");d.append(el("summary",null,"Razonamiento ("+it.rlen+" caracteres)"),el("div","txt",it.reasoning));log.append(d)}
    if(it.text)log.append(el("div","txt"+(it.final?" final":""),it.text))}
  else if(it.k==="tool")log.append(el("div","tool","› "+it.text));
  else if(it.k==="res"){log.append(el("div","res"+(it.bad?" bad":""),"↳ "+it.text));
    if(it.more){const d=el("details","reason");d.append(el("summary",null,"ver detalle"),el("div","res",it.more));log.append(d)}}
}
async function api(path,body){
  const o={headers:{"Authorization":"Bearer "+tok}};
  if(body){o.method="POST";o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body)}
  const r=await fetch(path,o);
  if(r.status===401){showLogin();throw new Error("401")}
  return r.json()
}
function showLogin(){$("login").style.display="block";log.style.display="none";$("bar").style.display="none";$("status").textContent="sin acceso"}
async function poll(){
  let d;
  try{d=await api("/api/state?n="+n+"&fp="+encodeURIComponent(fp)+"&sid="+encodeURIComponent(sid))}
  catch(e){if(e.message!=="401"){$("status").textContent="sin conexión con la PC…"}schedule(3000);return}
  const stick=near();
  if(d.reset){log.textContent="";lastLive=""}
  document.querySelectorAll(".live").forEach(x=>x.remove());
  d.items.forEach(add);
  n=d.total;fp=d.fp;sid=d.sid;busy=d.busy;
  $("title").textContent=d.title||"DeepSeek Chat";
  $("status").textContent=d.status||"";
  if(d.live&&(d.live.content||d.live.rlen)){
    log.append(el("div","live",d.live.content?d.live.content:"razonando… ("+d.live.rlen+" caracteres)"))}
  $("send").textContent=busy?"Detener":"Enviar";$("send").className=busy?"bad":"";
  $("cont").style.display=d.can_continue&&!busy?"block":"none";
  const c=d.confirm;
  if(c){cid=c.id;$("ctitle").textContent=c.kind==="edit"?"Quiere modificar: "+c.title:c.kind==="delete"?"Quiere borrar: "+c.title:"Quiere ejecutar:";$("cdetail").textContent=c.detail;$("confirm").style.display="block"}
  else{cid=null;$("confirm").style.display="none"}
  if(stick)log.scrollTop=log.scrollHeight;
  schedule(busy||c?700:1800)
}
function schedule(ms){clearTimeout(timer);timer=setTimeout(poll,ms)}
async function act(path,body){try{const r=await api(path,body);if(!r.ok&&r.msg)$("status").textContent=r.msg}catch(e){}poll()}
$("send").onclick=()=>{
  if(busy){act("/api/cancel",{});return}
  const t=$("inp").value.trim();if(!t)return;
  $("inp").value="";act("/api/send",{text:t})}
$("cont").onclick=()=>act("/api/continue",{});
$("cyes").onclick=()=>{if(cid!=null)act("/api/confirm",{id:cid,allow:true})};
$("cno").onclick=()=>{if(cid!=null)act("/api/confirm",{id:cid,allow:false})};
$("tokgo").onclick=()=>{tok=$("tok").value.trim();try{localStorage.setItem("dsr_tok",tok)}catch(e){}
  $("login").style.display="none";log.style.display="";$("bar").style.display="";n=0;fp="";sid="";poll()};
if(tok)poll();else showLogin();
</script></body></html>
"""
