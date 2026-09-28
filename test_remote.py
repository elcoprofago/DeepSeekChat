"""Acceso remoto (celular): el servidor HTTP real contra la ventana real, con streams falsos y sin red externa.

Las peticiones salen por un socket de verdad (urllib) desde otro hilo, mientras el hilo principal bombea tkinter: es el
mismo cruce de hilos que habrá con un celular.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request

import deepseek_chat as dc
import dsapi
import remote

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}


def call(id_, name, args):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Flujo:
    def __init__(self, paso):
        self.paso = paso

    def cancel(self):
        pass

    def __iter__(self):
        if callable(self.paso):
            yield from self.paso(self)
        else:
            yield from self.paso


class Guion:
    def __init__(self):
        self.pasos = []

    def __call__(self, entry, msgs, tools):
        return Flujo(self.pasos.pop(0))


def respuesta(texto):
    return [("content", texto), ("usage", {"prompt_tokens": 10, "completion_tokens": 5}), ("finish", "stop")]


def con_llamada(*calls):
    return [("tool_calls", list(calls)), ("usage", {"prompt_tokens": 10, "completion_tokens": 5}), ("finish", "tool_calls")]


# ---------------------------------------------------------------- 1. el servidor solo, con un puente falso (sin ventana)
class Falso:
    def __init__(self):
        self.enviados = []

    def state(self, n, fp, sid):
        return {"n": n, "sid": sid}

    def send(self, text, attachments=()):
        self.enviados.append(text)
        return True, ""

    def cancel(self):
        return False, "nada en curso"

    def continue_run(self):
        return True, ""

    def confirm(self, cid, allow):
        return True, ""


TOKEN = remote.new_token()
falso = Falso()
srv = remote.RemoteServer(falso, TOKEN, 0, host="127.0.0.1")
port = srv.start()
BASE = f"http://127.0.0.1:{port}"


def pedir(base, metodo, ruta, cuerpo=None, token=TOKEN, ctype="application/json", raw=None):
    """(código, texto). Sin ventana de por medio."""
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = raw
    if cuerpo is not None:
        data = json.dumps(cuerpo).encode("utf-8")
    if data is not None and ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(base + ruta, data=data, method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


try:
    remote.RemoteServer(falso, "corto")
    check("un token corto se rechaza al crear el servidor", False)
except ValueError:
    check("un token corto se rechaza al crear el servidor", True)
check("los tokens generados son distintos y largos", remote.new_token() != remote.new_token() and len(remote.new_token()) >= 21)

c, t = pedir(BASE, "GET", "/", token=None)
check("la página se sirve sin token (es estática)", c == 200 and "<title>" in t)
check("la página NO contiene el token", TOKEN not in t)
check("la página manda el token por cabecera, nunca en la URL", "Authorization" in t and "?t=" not in t and "token=" not in t)
c, t = pedir(BASE, "GET", "/api/state", token=None)
check("estado sin token: 401", c == 401, (c, t))
c, t = pedir(BASE, "GET", "/api/state", token="x" * len(TOKEN))
check("estado con token equivocado: 401", c == 401, (c, t))
c, t = pedir(BASE, "GET", "/api/state", token=TOKEN[:-1])
check("estado con el token truncado: 401", c == 401, (c, t))
c, t = pedir(BASE, "GET", "/api/state?n=3&sid=abc")
check("estado con el token bueno: 200 y llegan los parámetros", c == 200 and json.loads(t) == {"n": 3, "sid": "abc"}, (c, t))
c, t = pedir(BASE, "POST", "/api/send", {"text": "hola"}, token=None)
check("mandar sin token: 401 y NO llega al puente", c == 401 and falso.enviados == [], (c, falso.enviados))
c, t = pedir(BASE, "POST", "/api/send", {"text": "hola"}, ctype="text/plain")
check("mandar sin Content-Type JSON: 415 (un formulario ajeno no puede pegarle)", c == 415 and falso.enviados == [], (c, falso.enviados))
c, t = pedir(BASE, "POST", "/api/send", raw=b"x" * (remote.MAX_BODY + 10))
check("cuerpo enorme: 413", c == 413, c)
c, t = pedir(BASE, "POST", "/api/send", raw=b"{roto")
check("JSON roto: 400", c == 400, c)
c, t = pedir(BASE, "POST", "/api/send", {"text": "   "})
check("mensaje vacío: 400", c == 400 and falso.enviados == [], c)
c, t = pedir(BASE, "POST", "/api/send", {"text": "hacé algo"})
check("mandar con token: 200 y llega al puente", c == 200 and falso.enviados == ["hacé algo"], (c, falso.enviados))
c, t = pedir(BASE, "POST", "/api/cancel", {})
check("cancelar sin nada en curso: 409 con el motivo", c == 409 and "nada en curso" in t, (c, t))
c, t = pedir(BASE, "GET", "/api/otra")
check("ruta inexistente: 404", c == 404, c)
c, t = pedir(BASE, "POST", "/api/config", {"approval": "all"})
check("CONTROL: no hay ruta para cambiar permisos ni configuración", c == 404, c)
srv.stop()
try:
    pedir(BASE, "GET", "/")
    check("tras detenerlo, el servidor ya no atiende", False)
except (urllib.error.URLError, ConnectionError, OSError):
    check("tras detenerlo, el servidor ya no atiende", True)

# ---------------------------------------------------------------- 2. contra la ventana real
tmp = tempfile.mkdtemp(prefix="dschat_remote_")
cfg = dsapi.Config(os.path.join(tmp, "cfg"))
cfg.set_session_key("sk-de-prueba")
cfg["remote_port"] = 0                                  # el sistema elige un puerto libre
ws = os.path.join(tmp, "proyecto")
os.makedirs(ws)
open(os.path.join(ws, "CONTROL.txt"), "w", encoding="utf-8").write("intacto\n")
root = tk.Tk()
guion = Guion()
app = dc.App(root, cfg, stream_factory=guion, interactive=False)


def pump(cond=lambda: False, t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def http(metodo, ruta, cuerpo=None, token=None, **kw):
    """Pide desde otro hilo (como un celular) mientras el principal atiende la ventana."""
    box = {}
    base = f"http://127.0.0.1:{app.remote_srv.port}"
    th = threading.Thread(target=lambda: box.update(r=pedir(base, metodo, ruta, cuerpo, token=token or cfg["remote_token"], **kw)), daemon=True)
    th.start()
    pump(lambda: "r" in box, 10)
    c, t = box.get("r", (0, ""))
    try:
        return c, json.loads(t)
    except ValueError:
        return c, t


def estado(n=0, fp="", sid=""):
    return http("GET", f"/api/state?n={n}&fp={fp}&sid={sid}")


def terminar(t=8.0):
    ok = pump(lambda: not app.busy and app.worker is not None and not app.worker.is_alive(), t)
    root.update()
    return ok


pump(t=0.3)
check("por defecto el acceso remoto está apagado y nadie escucha", cfg["remote_enabled"] is False and app.remote_srv is None)
app.use_folder(ws)
ok, msg = app.set_remote(True)
check("encender: ok, escuchando y guardado en la configuración", ok and app.remote_srv is not None and dsapi.Config(os.path.join(tmp, "cfg"))["remote_enabled"] is True, msg)
check("el token se generó y se guardó", len(cfg["remote_token"]) >= 21 and dsapi.Config(os.path.join(tmp, "cfg"))["remote_token"] == cfg["remote_token"])
check("el enlace lleva el token en el fragmento (#)", app.remote_srv.link().endswith("/#" + cfg["remote_token"]), app.remote_srv.link())

c, d = estado()
check("estado inicial: sin mensajes, sin ocupado, sin permiso pendiente", c == 200 and d["items"] == [] and d["busy"] is False and d["confirm"] is None, (c, d))
sid = d["sid"]

# un mensaje desde el celular: se ejecuta en la ventana, aparece en el chat de la PC y no toca el borrador
app.input.insert("1.0", "borrador de la PC")
guion.pasos = [respuesta("Todo listo desde el celular.")]
c, r = http("POST", "/api/send", {"text": "hola desde el celu"})
check("EFECTO: mandar desde el celular arranca el agente", c == 200 and r["ok"] and terminar(), (c, r))
check("EFECTO: el mensaje se ve también en la ventana de la PC", "hola desde el celu" in app.chat.get_text() and "Todo listo desde el celular." in app.chat.get_text())
check("CONTROL: el borrador que había escrito en la PC quedó intacto", app.input.get("1.0", "end-1c") == "borrador de la PC", app.input.get("1.0", "end-1c"))
c, d = estado(0, "", sid)
kinds = [(i["k"], i.get("final")) for i in d["items"]]
check("el estado devuelve usuario y respuesta final marcada", kinds == [("user", None), ("bot", True)], kinds)
check("el texto de la respuesta llega completo", d["items"][1]["text"] == "Todo listo desde el celular.", d["items"])
n, fp = d["total"], d["fp"]
c, d2 = estado(n, fp, sid)
check("CONTROL: si ya tiene todo, el estado no repite mensajes", d2["items"] == [] and d2["reset"] is False and d2["total"] == n, d2)
c, d3 = estado(n, "user:1:0", sid)
check("si la huella no coincide (historial cambiado), se reenvía todo", d3["reset"] is True and len(d3["items"]) == 2, d3["reset"])
c, d3 = estado(n, fp, "otra-sesion")
check("si es otra sesión, se reenvía todo", d3["reset"] is True and len(d3["items"]) == 2)
c, d3 = estado(99, fp, sid)
check("si el celular dice tener más de lo que hay, se reenvía todo", d3["reset"] is True and len(d3["items"]) == 2)

# permisos: el pedido aparece en el estado y se resuelve desde el celular (permitir)
app.input.delete("1.0", "end")
guion.pasos = [con_llamada(call("c1", "run_command", {"command": "echo desde_el_celular"})), respuesta("Ejecutado.")]
c, r = http("POST", "/api/send", {"text": "corré un eco"})
check("mandar otra tarea: ok", c == 200 and r["ok"], (c, r))
pump(lambda: app._pending_confirm is not None, 5)
c, d = estado(n, fp, sid)
conf = d["confirm"]
check("EFECTO: el permiso pendiente aparece en el estado con el comando", conf is not None and "desde_el_celular" in conf["detail"] and conf["kind"] == "command", conf)
check("el estado marca ocupado", d["busy"] is True)
c, r = http("POST", "/api/send", {"text": "otra cosa"})
check("CONTROL: mandar con el agente ocupado se rechaza (409)", c == 409 and not r["ok"], (c, r))
c, r = http("POST", "/api/confirm", {"id": conf["id"] + 99, "allow": True})
check("CONTROL: responder un permiso que no existe se rechaza (409)", c == 409, (c, r))
check("premisa: sigue pendiente tras la respuesta inválida", app._pending_confirm is not None and app.busy)
dlg = app._dialog
c, r = http("POST", "/api/confirm", {"id": conf["id"], "allow": True})
check("permitir desde el celular: ok", c == 200 and r["ok"], (c, r))
check("EFECTO: el agente termina y el comando corrió de verdad", terminar() and any("desde_el_celular" in str(m.get("content")) for m in app.sess.messages if m["role"] == "tool"))
check("EFECTO: la ventana de permiso de la PC se cerró sola", not dlg.win.winfo_exists())
check("el permiso ya no está pendiente", app._pending_confirm is None)
c, d = estado(0, "", sid)
res = [i for i in d["items"] if i["k"] == "res"]
check("el estado incluye la llamada y su resultado", any(i["k"] == "tool" and "echo desde_el_celular" in i["text"] for i in d["items"]) and res, d["items"])
check("la respuesta intermedia (con herramientas) NO es 'final' y la última sí", [i["final"] for i in d["items"] if i["k"] == "bot"][-1] is True)

# permisos: denegar desde el celular
guion.pasos = [con_llamada(call("c2", "run_command", {"command": "echo NO_DEBE_CORRER > marca.txt"})), respuesta("Entendido, no lo hice.")]
n, fp = d["total"], d["fp"]
http("POST", "/api/send", {"text": "escribí una marca"})
pump(lambda: app._pending_confirm is not None, 5)
cid = app._pending_confirm["id"]
c, r = http("POST", "/api/confirm", {"id": cid, "allow": False})
check("denegar desde el celular: ok", c == 200 and r["ok"] and terminar())
check("EFECTO: denegar impide que el comando corra", not os.path.exists(os.path.join(ws, "marca.txt")))
check("CONTROL: lo que ya estaba en la carpeta sigue igual", open(os.path.join(ws, "CONTROL.txt"), encoding="utf-8").read() == "intacto\n")

# la PC y el celular responden a la vez: gana la primera, la segunda no rompe nada
guion.pasos = [con_llamada(call("c3", "run_command", {"command": "echo carrera"})), respuesta("ok")]
http("POST", "/api/send", {"text": "otra vez"})
pump(lambda: app._pending_confirm is not None, 5)
cid = app._pending_confirm["id"]
app._dialog._finish("deny")                              # la PC gana
c, r = http("POST", "/api/confirm", {"id": cid, "allow": True})
check("si la PC ya respondió, el celular recibe 409 y el comando NO corre", c == 409 and terminar()
      and not any("carrera" in str(m.get("content")) and m["role"] == "tool" and "DENIED" not in str(m.get("content")) for m in app.sess.messages), (c, r))

# cancelar desde el celular
c, r = http("POST", "/api/cancel", {})
check("cancelar sin nada en curso: 409", c == 409)


def lento(flujo):
    yield ("content", "empiezo")
    t0 = time.time()
    while time.time() - t0 < 5:
        time.sleep(0.02)
    yield ("finish", "stop")


guion.pasos = [lento]
http("POST", "/api/send", {"text": "algo largo"})
pump(lambda: app.busy, 3)
c, r = http("POST", "/api/cancel", {})
check("cancelar con el agente trabajando: ok", c == 200 and r["ok"], (c, r))
check("EFECTO: el agente se detiene", pump(lambda: not app.busy, 8))
terminar()

# el paso en curso (streaming) se ve en 'live'
app._handle("step_begin")
app._handle("content", "parte uno ")
app._handle("content", "parte dos")
app._handle("reasoning", "x" * 40)
c, d = estado(0, "", sid)
check("EFECTO: el texto en curso se ve en 'live' y el razonamiento como longitud", d["live"]["content"] == "parte uno parte dos" and d["live"]["rlen"] == 40, d["live"])
app._handle("step_end", {"content": "", "reasoning": "", "finish": "stop", "calls": []})
c, d = estado(0, "", sid)
check("al terminar el paso, 'live' se vacía", d["live"] == {"content": "", "rlen": 0}, d["live"])

# regenerar el token invalida el enlace anterior
viejo = cfg["remote_token"]
ok, msg = app.regenerate_remote_token()
check("regenerar token: ok, token distinto, servidor sigue encendido", ok and cfg["remote_token"] != viejo and app.remote_srv is not None, msg)
box = {}
base = f"http://127.0.0.1:{app.remote_srv.port}"
th = threading.Thread(target=lambda: box.update(r=pedir(base, "GET", "/api/state", token=viejo)), daemon=True)
th.start()
pump(lambda: "r" in box, 5)
check("EFECTO: el token viejo ya no sirve", box["r"][0] == 401, box)
c, d = estado()
check("el token nuevo sí", c == 200, c)

# ventana de configuración remota
d = __import__("dialogs").RemoteDialog(app)
root.update()
check("el diálogo muestra el enlace cuando está encendido", d.link_var.get() == app.remote_srv.link(), d.link_var.get())
d.on_var.set(False)
d.toggle()
root.update()
check("EFECTO: apagar desde el diálogo detiene el servidor y lo guarda", app.remote_srv is None
      and dsapi.Config(os.path.join(tmp, "cfg"))["remote_enabled"] is False and d.link_var.get() == "")
d.win.destroy()

# arranque con el remoto guardado como encendido
app.on_close()
cfg2 = dsapi.Config(os.path.join(tmp, "cfg"))
cfg2["remote_enabled"] = True
cfg2.save()
root = tk.Tk()
app = dc.App(root, cfg2, stream_factory=guion, interactive=False)
pump(t=0.5)
check("EFECTO: si quedó encendido, al abrir la app vuelve a escuchar", app.remote_srv is not None, app.chat.get_text()[-200:])
app.on_close()
check("al cerrar la app el servidor se detiene", app.remote_srv is None)

shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
