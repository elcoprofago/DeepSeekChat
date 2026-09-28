"""API de MovilDeep (protocolo 2): info, sesiones, modelos, subida de adjuntos, run.seq, Tailscale y el QR.

Primero las funciones sueltas (sin ventana); después el servidor real contra la ventana real, con streams falsos, como
test_remote.py. El OCR usa el Tesseract instalado si lo hay; si no, esas pruebas se informan como omitidas.
"""
import base64
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
import dialogs
import dsapi
import ocr
import qrcodegen
import remote
import version

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}

try:
    import cv2
    import numpy as np
except ImportError:                     # sin OpenCV no se puede leer el QR ni fabricar la imagen con texto
    cv2 = np = None

tmp = tempfile.mkdtemp(prefix="dschat_movildeep_")


def decode_qr(matrix, quiet=4, scale=6):
    n = len(matrix) + 2 * quiet
    img = np.full((n, n), 255, np.uint8)
    for y, row in enumerate(matrix):
        for x, c in enumerate(row):
            if c:
                img[y + quiet, x + quiet] = 0
    img = cv2.resize(img, (n * scale, n * scale), interpolation=cv2.INTER_NEAREST)
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


def imagen_con_texto(path, texto):
    img = np.full((140, 900, 3), 255, np.uint8)
    cv2.putText(img, texto, (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.imwrite(path, img)
    with open(path, "rb") as f:
        return f.read()


def imagen_en_blanco(path):
    cv2.imwrite(path, np.full((200, 300, 3), 255, np.uint8))
    with open(path, "rb") as f:
        return f.read()


def en_adjuntos(ws):
    d = os.path.join(ws, remote.UPLOAD_DIR)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ---------------------------------------------------------------- 1. Tailscale y QR
check("Tailscale: toma la IP de 100.64.0.0/10", remote.tailscale_ip(["192.168.1.5", "100.101.102.103"]) == "100.101.102.103")
check("Tailscale: sin IP de Tailscale devuelve vacío", remote.tailscale_ip(["192.168.1.5", "127.0.0.1"]) == "")
check("CONTROL: 100.128.0.1 está fuera de 100.64.0.0/10", remote.tailscale_ip(["100.128.0.1", "100.63.255.255"]) == "")
check("Tailscale: sin interfaces, vacío", remote.tailscale_ip([]) == "")
check("Tailscale: la detección real no rompe (devuelve texto)", isinstance(remote.tailscale_ip(), str))

p = remote.qr_payload("192.168.1.5", "100.101.102.103", 8765, "t" * 22)
check("QR: empieza con movildeep:", p.startswith("movildeep:"), p)
d = json.loads(p[len("movildeep:"):])
check("QR: v=1, hosts en orden (LAN, Tailscale), puerto y token", d == {"v": 1, "hosts": ["192.168.1.5", "100.101.102.103"],
                                                                         "port": 8765, "token": "t" * 22}, d)
d = json.loads(remote.qr_payload("192.168.1.5", "", 8765, "t" * 22)[len("movildeep:"):])
check("QR: sin Tailscale lleva un solo host", d["hosts"] == ["192.168.1.5"], d)
if cv2 is not None:
    check("QR: el código dibujado se lee y devuelve exactamente el contenido", decode_qr(qrcodegen.encode(p)) == p)
    largo = "movildeep:" + "x" * 400
    check("QR: un contenido largo también se lee", decode_qr(qrcodegen.encode(largo)) == largo)
else:
    print("OMITIDO lectura del QR: falta OpenCV")

# ---------------------------------------------------------------- 2. nombres y subida (sin ventana)
check("nombre: se queda con el nombre base (sin ..)", remote.safe_name("../../etc/passwd") == "passwd")
check("nombre: también con barras de Windows", remote.safe_name("..\\..\\Windows\\x.txt") == "x.txt")
check("nombre: caracteres inválidos en Windows se reemplazan", remote.safe_name('a:b?c*"d<e>|.txt') == "a_b_c__d_e__.txt")
check("nombre: '..' solo no queda como nombre", remote.safe_name("..") == "archivo")
check("nombre: nombres reservados de Windows", remote.safe_name("CON.txt") == "_CON.txt")

ws = os.path.join(tmp, "ws")
os.makedirs(ws)
open(os.path.join(ws, "CONTROL.txt"), "w", encoding="utf-8").write("intacto\n")
exe = ocr.find_tesseract()

it = remote.prepare_upload("nota.py", "print('hola')\n".encode("utf-8"), ws, exe)
check("texto: kind text, se incrusta con su nombre y NO se guarda en la carpeta",
      it["kind"] == "text" and it["att_name"] == "nota.py" and it["text"] == "print('hola')\n" and en_adjuntos(ws) == [], it)
try:
    remote.prepare_upload("grande.txt", b"a" * (dsapi.MAX_FILE_BYTES + 1), ws, exe)
    check("texto sobre el límite de dsapi: se rechaza (413)", False)
except remote.UploadError as e:
    check("texto sobre el límite de dsapi: se rechaza (413)", e.status == 413 and "KB" in str(e), str(e))
try:
    remote.prepare_upload("enorme.bin", b"\0" * (remote.MAX_UPLOAD_FILE + 1), ws, exe)
    check("más de 15 MB: se rechaza (413)", False)
except remote.UploadError as e:
    check("más de 15 MB: se rechaza (413)", e.status == 413 and "15 MB" in str(e), str(e))
check("CONTROL: lo rechazado no dejó nada en movildeep-adjuntos", en_adjuntos(ws) == [], en_adjuntos(ws))

binario = bytes(range(256)) * 40
it = remote.prepare_upload("datos.zip", binario, ws, exe)
dest = os.path.join(ws, remote.UPLOAD_DIR, "datos.zip")
check("binario: kind stored, guardado idéntico en movildeep-adjuntos", it["kind"] == "stored" and open(dest, "rb").read() == binario, it)
check("binario: el aviso para el agente", it["note"] == f"Se subió datos.zip (10 KB) a {dest}; no es texto, no se incluye su contenido", it["note"])
it2 = remote.prepare_upload("datos.zip", b"\0otro", ws, exe)
check("colisión: se guarda como «datos (2).zip» y NO pisa el primero",
      "datos (2).zip" in it2["note"] and open(dest, "rb").read() == binario, it2["note"])
it3 = remote.prepare_upload("..\\..\\datos.zip", b"\0tercero", ws, exe)
check("nombre con ..: queda dentro de movildeep-adjuntos como «datos (3).zip»", "datos (3).zip" in it3["note"]
      and os.path.isfile(os.path.join(ws, remote.UPLOAD_DIR, "datos (3).zip")), it3["note"])
check("CONTROL: nada se escribió fuera de movildeep-adjuntos", sorted(os.listdir(ws)) == ["CONTROL.txt", remote.UPLOAD_DIR]
      and sorted(os.listdir(tmp)) == ["ws"], (os.listdir(ws), os.listdir(tmp)))
check("no quedan temporales a medias", not [n for n in en_adjuntos(ws) if n.startswith(".subiendo-")], en_adjuntos(ws))
try:
    remote.prepare_upload("datos.zip", binario, "", exe)
    check("binario sin carpeta de trabajo: se rechaza con un mensaje claro", False)
except remote.UploadError as e:
    check("binario sin carpeta de trabajo: se rechaza con un mensaje claro", "carpeta de trabajo" in str(e), str(e))

it = remote.prepare_upload("foto.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 50, ws, None)
check("imagen sin Tesseract: se guarda como binario con «OCR no disponible en esta PC»",
      it["kind"] == "stored" and "OCR no disponible en esta PC" in it["note"] and "foto.png" in en_adjuntos(ws), it)

guardado = ocr.run
ocr.run = lambda exe_, img, timeout=ocr.TIMEOUT: (_ for _ in ()).throw(ocr.OcrTimeout("OCR: tiempo agotado"))
it = remote.prepare_upload("lenta.png", b"\x89PNG-lenta", ws, "tesseract-falso.exe")
ocr.run = guardado
check("OCR que tarda más de 60 s: el adjunto dice «OCR: tiempo agotado» y la imagen se guarda",
      it["kind"] == "ocr" and it["text"].endswith("OCR: tiempo agotado") and "lenta.png" in en_adjuntos(ws), it)

if exe and cv2 is not None:
    datos = imagen_con_texto(os.path.join(tmp, "cap.png"), "Hola mundo prueba OCR 2026")
    it = remote.prepare_upload("captura.png", datos, ws, exe)
    check("imagen con OCR: kind ocr, adjunto <nombre>.ocr.txt con la primera línea pedida",
          it["kind"] == "ocr" and it["att_name"] == "captura.png.ocr.txt"
          and it["text"].splitlines()[0] == "Texto extraído por OCR de la imagen captura.png", it)
    check("imagen con OCR: el texto de la imagen llegó", "mundo" in it["text"].lower() and "2026" in it["text"], it["text"])
    check("imagen con OCR: la original se guardó y el aviso da su ruta",
          "captura.png" in en_adjuntos(ws) and os.path.join(ws, remote.UPLOAD_DIR, "captura.png") in it["note"], it["note"])
    it = remote.prepare_upload("vacia.png", imagen_en_blanco(os.path.join(tmp, "b.png")), ws, exe)
    check("imagen sin texto: «OCR: no se encontró texto» y se guarda", it["text"].endswith("OCR: no se encontró texto")
          and "vacia.png" in en_adjuntos(ws), it)
    it = remote.prepare_upload("sin_ws.png", datos, "", exe)
    check("imagen sin carpeta de trabajo: igual pasa el texto, no se guarda y el aviso lo dice",
          it["kind"] == "ocr" and "no se guardó" in it["note"] and "mundo" in it["text"].lower(), it)
else:
    print("OMITIDO OCR real: falta Tesseract instalado u OpenCV")

# ---------------------------------------------------------------- 3. contra la ventana real
TOKEN_LEN = 22


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
        self.vistos = []                # los mensajes que recibió el modelo en cada paso

    def __call__(self, entry, msgs, tools):
        self.vistos.append(msgs)
        return Flujo(self.pasos.pop(0))


def respuesta(texto):
    return [("content", texto), ("usage", {"prompt_tokens": 10, "completion_tokens": 5}), ("finish", "stop")]


def con_llamada(i, name, args):
    c = {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return [("tool_calls", [c]), ("usage", {"prompt_tokens": 10, "completion_tokens": 5}), ("finish", "tool_calls")]


cfg = dsapi.Config(os.path.join(tmp, "cfg"))
cfg.set_session_key("sk-de-prueba")
cfg["remote_port"] = 0
root = tk.Tk()
guion = Guion()
app = dc.App(root, cfg, stream_factory=guion, interactive=False)
energia = []        # lo que se le pidió a la energía de la PC: simulada, nunca se apaga ni se suspende de verdad
app.power_fn = lambda accion: (energia.append(accion), (True, ""))[1]


def pump(cond=lambda: False, t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def pedir(metodo, ruta, cuerpo=None, token="", ctype="application/json", raw=None):
    headers = {}
    tok = cfg["remote_token"] if token == "" else token
    if tok is not None:
        headers["Authorization"] = "Bearer " + tok
    data = raw if cuerpo is None else json.dumps(cuerpo).encode("utf-8")
    if data is not None and ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"http://127.0.0.1:{app.remote_srv.port}{ruta}", data=data, method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def http(*a, t=90, **kw):
    """Pide desde otro hilo (como el celular) mientras el principal atiende la ventana."""
    box = {}
    th = threading.Thread(target=lambda: box.update(r=pedir(*a, **kw)), daemon=True)
    th.start()
    pump(lambda: "r" in box, t)
    c, txt = box.get("r", (0, ""))
    try:
        return c, json.loads(txt)
    except ValueError:
        return c, txt


def terminar(t=8.0):
    ok = pump(lambda: not app.busy and app.worker is not None and not app.worker.is_alive(), t)
    root.update()
    return ok


def subir(nombre, datos, **kw):
    return http("POST", "/api/upload", {"name": nombre, "data_base64": base64.b64encode(datos).decode("ascii")}, **kw)


def run():
    return http("GET", "/api/state?n=0")[1]["run"]


pump(t=0.3)
app.use_folder(ws)
ok, msg = app.set_remote(True)
check("premisa: servidor encendido", ok, msg)

pump(lambda: app.balance_text == "US$ 4.20", 3)
st = http("GET", "/api/state?n=0")[1]
check("state trae el saldo tal como lo muestra la barra de la PC", st.get("balance") == "US$ 4.20", st.get("balance"))

# info
c, d = http("GET", "/api/info")
check("/api/info: app, versión y protocolo 2", c == 200 and d == {"app": "DeepSeekChat", "version": version.VERSION, "protocol": 2}, (c, d))
c, d = http("GET", "/api/info", token=None)
check("/api/info sin token: 401", c == 401, c)
for ruta in ("/api/sessions", "/api/models"):
    c, d = http("GET", ruta, token="x" * TOKEN_LEN)
    check(f"{ruta} con token equivocado: 401", c == 401, c)
for ruta, cuerpo in (("/api/session/switch", {"id": "x"}), ("/api/session/new", {}), ("/api/model", {"id": "x"}),
                     ("/api/upload", {"name": "a.txt", "data_base64": "YQ=="})):
    c, d = http("POST", ruta, cuerpo, token=None)
    check(f"{ruta} sin token: 401", c == 401, c)
    c, d = http("POST", ruta, cuerpo, ctype="text/plain")
    check(f"{ruta} sin Content-Type JSON: 415", c == 415, c)
check("CONTROL: los pedidos rechazados no crearon sesiones ni subidas", app.remote_srv.bridge.uploads == {}
      and len(app.store.list()) == 0)

# run.seq antes de todo
r0 = run()
check("run arranca en seq 0", r0 == {"seq": 0, "outcome": "", "msg": ""}, r0)

# primera tarea: termina bien
guion.pasos = [respuesta("Listo: hice todo lo pedido.")]
c, r = http("POST", "/api/send", {"text": "primera tarea"})
check("mandar: ok", c == 200 and r["ok"] and terminar(), (c, r))
r1 = run()
check("run: seq sube a 1, outcome done y msg con el comienzo de la respuesta",
      r1 == {"seq": 1, "outcome": "done", "msg": "Listo: hice todo lo pedido."}, r1)
primera = app.sess.id
modelo = app.sess.model

# sesiones
c, d = http("GET", "/api/sessions")
check("sesiones: la actual listada y marcada", c == 200 and d[0]["id"] == primera and d[0]["current"] is True
      and d[0]["title"] and d[0]["updated"], (c, d))
c, r = http("POST", "/api/session/new", {})
nueva = app.sess.id
check("sesión nueva: ok y es otra", c == 200 and r["ok"] and nueva != primera, (c, r))
check("EFECTO: la nueva hereda la carpeta de trabajo y el modelo", app.sess.workspace == ws and app.sess.model == modelo,
      (app.sess.workspace, app.sess.model))
c, r = http("POST", "/api/session/new", {})
check("sesión nueva con la actual vacía: no se crea otra", c == 200 and app.sess.id == nueva and r["msg"], (c, r))
c, d = http("GET", "/api/sessions")
ids = [s["id"] for s in d]
check("sesiones: aparecen la vacía (actual) y la anterior, sin repetir", ids[0] == nueva and primera in ids and len(ids) == len(set(ids)), d)
c, r = http("POST", "/api/session/switch", {"id": primera})
check("cambiar de sesión: ok y la PC la activa", c == 200 and r["ok"] and app.sess.id == primera, (c, r))
c, r = http("POST", "/api/session/switch", {"id": "no-existe"})
check("cambiar a una sesión que no existe: 409 y sigue la actual", c == 409 and app.sess.id == primera, (c, r))

# modelos
c, d = http("GET", "/api/models")
cur = [m for m in d if m["current"]]
check("modelos: la lista de la PC con el actual marcado", c == 200 and len(cur) == 1 and cur[0]["id"] == app.sess.model
      and all(set(m) == {"id", "name", "kind", "current"} for m in d), (c, d))
otro = next(m for m in d if not m["current"] and m["kind"] == "remote")
c, r = http("POST", "/api/model", {"id": otro["id"]})
check("cambiar de modelo: ok y la sesión de la PC lo usa", c == 200 and r["ok"] and app.sess.model == otro["id"]
      and app.model_var.get() == otro["name"], (c, r, app.sess.model))
c, r = http("POST", "/api/model", {"id": "modelo-inexistente"})
check("modelo que no existe: 409 y no cambia", c == 409 and app.sess.model == otro["id"], (c, r))


# rechazo mientras trabaja
def lento(flujo):
    yield ("content", "empiezo")
    t0 = time.time()
    while time.time() - t0 < 6:
        time.sleep(0.02)
    yield ("finish", "stop")


guion.pasos = [lento]
http("POST", "/api/send", {"text": "algo largo"})
pump(lambda: app.busy, 3)
sid_antes, modelo_antes = app.sess.id, app.sess.model
c, r = http("POST", "/api/session/new", {})
check("sesión nueva trabajando: 409 con el aviso de la PC", c == 409 and r["msg"] == dc.BUSY_SESSION_NOTE, (c, r))
c, r = http("POST", "/api/session/switch", {"id": nueva})
check("cambiar de sesión trabajando: 409 con el aviso de la PC", c == 409 and r["msg"] == dc.BUSY_SESSION_NOTE, (c, r))
c, r = http("POST", "/api/model", {"id": modelo})
check("cambiar de modelo trabajando: 409", c == 409 and "trabajando" in r["msg"], (c, r))
c, r = http("POST", "/api/power", {"action": "shutdown"})
check("apagar trabajando: 409 con el aviso de la PC", c == 409 and r["msg"] == dc.BUSY_POWER_NOTE, (c, r))
c, r = http("POST", "/api/power", {"action": "suspend"})
check("suspender trabajando: 409 con el aviso de la PC", c == 409 and r["msg"] == dc.BUSY_POWER_NOTE, (c, r))
check("CONTROL: ni la sesión ni el modelo cambiaron", app.sess.id == sid_antes and app.sess.model == modelo_antes)
http("POST", "/api/cancel", {})
terminar()
r2 = run()
check("run: la cancelación sube seq con outcome cancelled", r2["seq"] == 2 and r2["outcome"] == "cancelled", r2)

# tope de pasos y bucle
app.max_steps = 1
guion.pasos = [con_llamada("m1", "list_dir", {"path": "."})]
http("POST", "/api/send", {"text": "listá"})
terminar()
r3 = run()
check("run: tope de pasos = max_steps con el aviso", r3["seq"] == 3 and r3["outcome"] == "max_steps" and "máximo" in r3["msg"], r3)
app.max_steps = 20
guion.pasos = [con_llamada(f"l{i}", "list_dir", {"path": "."}) for i in range(8)]
http("POST", "/api/send", {"text": "listá otra vez"})
terminar()
r4 = run()
check("run: bucle = loop con el aviso", r4["seq"] == 4 and r4["outcome"] == "loop" and "bucle" in r4["msg"], r4)


def falla(flujo):
    raise dsapi.ApiError("Falla del servidor de DeepSeek", status=500)
    yield


guion.pasos = [falla]
http("POST", "/api/send", {"text": "fallá"})
terminar()
r5 = run()
check("run: un error = error con el motivo", r5["seq"] == 5 and r5["outcome"] == "error" and "Falla del servidor" in r5["msg"], r5)
app.input.delete("1.0", "end")

# subida por HTTP y envío con adjuntos
c, u1 = subir("script.py", "def f():\n    return 42\n".encode("utf-8"))
check("subir texto: 200 con id, nombre y kind", c == 200 and u1["kind"] == "text" and u1["name"] == "script.py" and u1["id"], (c, u1))
c, u2 = subir("..\\..\\paquete.zip", b"PK\x03\x04\0\0binario")
check("subir binario con ..: stored, nombre saneado", c == 200 and u2["kind"] == "stored" and u2["name"] == "paquete.zip", (c, u2))
c, d = http("POST", "/api/upload", {"name": "x.bin", "data_base64": "esto no es base64!!"})
check("base64 inválido: 400", c == 400, (c, d))
c, d = http("POST", "/api/send", {"text": "", "attachments": ["id-inventado"]})
check("mandar con un adjunto desconocido: 409", c == 409 and not d["ok"], (c, d))
guion.pasos = [respuesta("Vi el script.")]
guion.vistos = []
c, r = http("POST", "/api/send", {"text": "mirá esto", "attachments": [u1["id"], u2["id"]]})
check("mandar con adjuntos: ok", c == 200 and r["ok"] and terminar(), (c, r))
user = [m for m in app.sess.messages if m["role"] == "user"][-1]["content"]
check("EFECTO: el texto se incrustó como un adjunto hecho en la PC", "Archivo adjunto: script.py\n```python\ndef f():" in user, user)
check("EFECTO: el aviso del binario llegó en el mensaje", "Se subió paquete.zip" in user and "no es texto" in user, user)
check("EFECTO: el modelo recibió el mensaje con el adjunto", "return 42" in json.dumps(guion.vistos[-1], ensure_ascii=False))
check("los adjuntos usados se descartan", app.remote_srv.bridge.uploads == {})
c, d = http("GET", "/api/state?n=0")
items = [i for i in d["items"] if i["k"] == "user"]
check("el estado muestra el adjunto como archivo", items[-1]["files"] == ["script.py"], items[-1])

c, d = http("POST", "/api/upload", raw=b"x" * (remote.MAX_BODY + 10))
check("subida: el límite de 64 KB no rige en /api/upload (llega al JSON y falla por JSON roto: 400)", c == 400, (c, d))
c, d = subir("enorme.bin", b"\0" * (remote.MAX_UPLOAD_FILE + 1), t=120)
check("subir más de 15 MB: 413 con el motivo", c == 413 and "15 MB" in d["error"], (c, d))
check("CONTROL: el archivo enorme no quedó guardado", "enorme.bin" not in en_adjuntos(ws), en_adjuntos(ws))

# sin carpeta de trabajo
http("POST", "/api/session/new", {})
app.sess.workspace = ""
c, d = subir("otro.zip", b"\0binario")
check("binario sin carpeta de trabajo: rechazado con el motivo", c == 422 and "carpeta de trabajo" in d["error"], (c, d))
if exe and cv2 is not None:
    c, u = subir("pantalla.png", imagen_con_texto(os.path.join(tmp, "p.png"), "Texto de la captura 77"))
    check("imagen por HTTP: kind ocr y el aviso dice que no se guardó", c == 200 and u["kind"] == "ocr" and "no se guardó" in u["note"], (c, u))
app.sess.workspace = ws

# apagar y suspender (con la energía simulada)
pump(t=3.5)
check("CONTROL: los rechazos (trabajando) no llamaron a la energía", energia == [], energia)
c, r = http("POST", "/api/power", {"action": "reboot"})
check("acción desconocida: 409 y no llama a nada", c == 409 and energia == [], (c, r, energia))
c, r = http("POST", "/api/power", {"action": "shutdown"}, token="malo")
check("apagar sin token válido: 401 y no llama a nada", c == 401 and energia == [], (c, energia))
c, r = http("POST", "/api/power", {"action": "shutdown"})
check("apagar: 200 y llama una sola vez a «shutdown»", c == 200 and energia == ["shutdown"], (c, r, energia))
win = app._shutdown_win
check("apagar: ventana siempre encima con la cuenta regresiva",
      win is not None and bool(win.attributes("-topmost"))
      and any("Se apaga en" in str(x.cget("text")) for x in win.winfo_children() if isinstance(x, dc.ttk.Label)))
boton = [x for x in win.winfo_children() if isinstance(x, dc.ttk.Button)]
check("apagar: la ventana ofrece «Cancelar apagado»", len(boton) == 1 and boton[0].cget("text") == "Cancelar apagado")
boton[0].invoke()
root.update()
check("Cancelar apagado: llama a «cancel» y cierra la ventana", energia == ["shutdown", "cancel"] and app._shutdown_win is None,
      energia)
app.power_fn = lambda accion: (energia.append(accion), (False, "shutdown devolvió 1190: ya hay un apagado"))[1]
c, r = http("POST", "/api/power", {"action": "shutdown"})
check("apagar con error de Windows: 409 con el motivo y sin ventana",
      c == 409 and "1190" in r["msg"] and app._shutdown_win is None, (c, r))
app.power_fn = lambda accion: (energia.append(accion), (True, ""))[1]
del energia[:]
c, r = http("POST", "/api/power", {"action": "suspend"})
check("suspender: 200 y todavía no suspendió (la respuesta sale primero)", c == 200 and energia == [], (c, r, energia))
pump(t=2.0)
check("suspender: a los 2 s todavía no", energia == [], energia)
check("suspender: a los 3 s llama una sola vez a «suspend»", pump(lambda: energia == ["suspend"], 3) and energia == ["suspend"],
      energia)

# el QR en la ventana «Remoto»
dlg = dialogs.RemoteDialog(app)
root.update()
check("QR: con el servidor encendido se muestra", dlg.qr_img is not None and dlg.qr_img.width() > 100)
check("QR: el contenido es el del servidor", app.remote_srv.qr_payload().startswith("movildeep:{\"v\":1,\"hosts\":[")
      and json.loads(app.remote_srv.qr_payload()[10:])["token"] == cfg["remote_token"])
if cv2 is not None:
    w = dlg.qr_img.width()
    px = np.array([[0 if dlg.qr_img.get(x, y) == (0, 0, 0) else 255 for x in range(w)] for y in range(w)], np.uint8)
    leido = cv2.QRCodeDetector().detectAndDecode(cv2.resize(px, (w * 2, w * 2), interpolation=cv2.INTER_NEAREST))[0]
    check("QR: la imagen de la ventana se lee y coincide con el contenido", leido == app.remote_srv.qr_payload(), leido)
viejo = cfg["remote_token"]
dlg.regen()
root.update()
check("QR: «Generar token nuevo» lo actualiza con el token nuevo", dlg.qr_img is not None
      and json.loads(app.remote_srv.qr_payload()[10:])["token"] == cfg["remote_token"] != viejo)
dlg.on_var.set(False)
dlg.toggle()
root.update()
check("QR: con el servidor apagado no se muestra ningún QR", dlg.qr_img is None and str(dlg.qr_lbl.cget("image")) == "")
dlg.on_var.set(True)
dlg.toggle()
root.update()
check("QR: al volver a encender reaparece", dlg.qr_img is not None)
dlg.win.destroy()

app.on_close()
shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
