"""Panel de log (logpanel.py) solo y dentro de la ventana real, con streams falsos (sin red, sin tocar la config real).

- formato «HH:MM:SS - msg», color por nivel, barra de progreso
- overwrite reemplaza solo la línea viva; nunca una normal ni una protegida (controles)
- log desde otro hilo, tope de líneas, Ctrl+L
- minimizar/restaurar, desacoplar/acoplar sin perder contenido, y el estado guardado al cerrar y reabrir
- lo que la app escribe al arrancar, durante un turno con herramientas y ante un error
Uso: python test_logpanel.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk

import deepseek_chat as dc
import dsapi
import logpanel

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}

# ---------------------------------------------------------------- funciones puras
check("barra a la mitad", logpanel.barra(0.5, 10) == "█████░░░░░", logpanel.barra(0.5, 10))
check("barra recorta lo que se pasa", logpanel.barra(1.7, 4) == "████" and logpanel.barra(-1, 4) == "░░░░")
p = logpanel.progreso("Contexto", 12345, 100000)
check("progreso con porcentaje y cuentas con punto de miles", p == f"Contexto [{logpanel.barra(0.12345)}]  12%  (12.345/100.000)", p)

# ---------------------------------------------------------------- el panel solo
root = tk.Tk()
root.geometry("800x600")
cfg = dsapi.Config(tempfile.mkdtemp(prefix="dschat_log_"))
pane = ttk.PanedWindow(root, orient="vertical")
arriba = ttk.Frame(pane, height=300)
pane.add(arriba, weight=1)
cola = []
lp = logpanel.LogPanel(root, pane, cfg, post=cola.append)
pane.add(lp.frame, weight=0)
pane.pack(fill="both", expand=True)
root.update()


def pump(cond=lambda: False, t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def lineas_vista(v=None):
    return (v or lp.view).get("1.0", "end-1c").splitlines()


lp.log("hola", "OK")
check("formato HH:MM:SS - msg", re.fullmatch(r"\d\d:\d\d:\d\d - hola", lp.lines[0][0]) is not None, lp.lines[0][0])
check("la línea tiene el color de su nivel", "OK" in lp.view.tag_names("1.0") and lp.view.tag_cget("OK", "foreground") == "#90ee90")
check("fondo negro", lp.view.cget("bg") == "#000000")
check("nivel desconocido cae en INFO", (lp.log("x", "RARO"), lp.lines[-1][1])[1] == "INFO")

lp.lines = []
lp._render()
lp.log("normal 1")
lp.log("spin 1", "SPINNER", overwrite=True)
lp.log("spin 2", "SPINNER", overwrite=True)
check("overwrite reemplaza la línea viva", [l[0][11:] for l in lp.lines] == ["normal 1", "spin 2"], [l[0] for l in lp.lines])
check("...y la vista coincide con el modelo", [l[11:] for l in lineas_vista()] == ["normal 1", "spin 2"], lineas_vista())
lp.log("fin", "OK", overwrite=True, protect=True)
lp.log("spin 3", "SPINNER", overwrite=True)
check("CONTROL: una línea protegida no se pisa", [l[0][11:] for l in lp.lines] == ["normal 1", "fin", "spin 3"], [l[0] for l in lp.lines])
lp.log("normal 2")
lp.log("spin 4", "SPINNER", overwrite=True)
check("CONTROL: una línea normal no se pisa",
      [l[0][11:] for l in lp.lines][-3:] == ["spin 3", "normal 2", "spin 4"], [l[0] for l in lp.lines])
check("el color sigue a la línea reemplazada", "SPINNER" in lp.view.tag_names(f"{len(lp.lines)}.0"))

# desde otro hilo: no toca tk, lo deja en la cola
th = threading.Thread(target=lambda: lp.log("desde el hilo"))
th.start()
th.join()
check("desde otro hilo no se escribe directo", not any("desde el hilo" in l[0] for l in lp.lines))
check("...deja una tarea para el hilo de la ventana", len(cola) == 1)
cola.pop()()
check("...que al correr la escribe", lp.lines[-1][0].endswith("desde el hilo"))

# tope de líneas
viejo = logpanel.MAX_LINEAS
logpanel.MAX_LINEAS = 50
for i in range(80):
    lp.log(f"n{i}")
check("tope: el modelo queda en el máximo", len(lp.lines) == 50, len(lp.lines))
check("tope: la vista también, y empieza por la más vieja que quedó",
      len(lineas_vista()) == 50 and lineas_vista()[0].endswith("n30"), (len(lineas_vista()), lineas_vista()[:1]))
logpanel.MAX_LINEAS = viejo

# Ctrl+L
lp.view.focus_force()
root.update()
lp.view.event_generate("<Control-l>")
root.update()
check("Ctrl+L limpia y lo deja dicho", len(lp.lines) == 1 and "Log limpiado manualmente" in lp.lines[0][0], lp.text())

# minimizar / restaurar
root.update()
lp._set_height(200)
root.update()
alto = lp._docked_height()
check("premisa: el panel acoplado tiene ~200 px", abs(alto - 200) <= 4, alto)
lp.set_minimized(True)
root.update()
check("minimizado: el cuerpo no se ve", not lp.frame.body.winfo_ismapped())
check("minimizado: el panel queda en la barra de título", lp._docked_height() < 60, lp._docked_height())
check("minimizado: la barra de título se ve entera (botones sin cortar)",
      lp.frame.winfo_height() >= lp.frame.head.winfo_reqheight(), (lp.frame.winfo_height(), lp.frame.head.winfo_reqheight()))
lp.log("mientras minimizado")
lp.set_minimized(False)
root.update()
check("restaurado: vuelve a su alto", abs(lp._docked_height() - alto) <= 4, (lp._docked_height(), alto))
check("restaurado: lo escrito mientras estaba minimizado está", "mientras minimizado" in lp.view.get("1.0", "end"))

# desacoplar / acoplar
lp.detach()
root.update()
check("desacoplado: hay ventana propia", lp.win is not None and lp.win.winfo_exists() and lp.win.title() == logpanel.TITULO)
check("desacoplado: salió del panel principal", str(lp.frame) not in [str(x) for x in pane.panes()])
check("desacoplado: trae todo el contenido", lineas_vista() == [l[0] for l in lp.lines])
lp.log("escrito suelto", "WARN")
check("desacoplado: lo nuevo va a la ventana suelta", lineas_vista()[-1].endswith("escrito suelto") and lp.view.winfo_toplevel() is lp.win)
check("desacoplado: la ventana suelta también tiene fondo negro", lp.view.cget("bg") == "#000000")
lp.win.geometry("700x300+40+50")
root.update()
root.tk.eval(lp.win.protocol("WM_DELETE_WINDOW"))        # lo mismo que corre al cerrar con la X
root.update()
check("cerrar la ventana suelta la acopla de nuevo", lp.win is None and str(lp.frame) in [str(x) for x in pane.panes()])
check("acoplado: no se perdió nada", lineas_vista()[-1].endswith("escrito suelto") and lineas_vista() == [l[0] for l in lp.lines])
check("acoplado: recuerda dónde estaba la ventana suelta", cfg["log_geometry"].startswith("700x300+40+50"), cfg["log_geometry"])
check("acoplado: vuelve con su alto", abs(lp._docked_height() - alto) <= 4, (lp._docked_height(), alto))
root.destroy()

# ---------------------------------------------------------------- dentro de la app


class Flujo:
    def __init__(self, ev):
        self.ev = ev

    def cancel(self):
        pass

    def __iter__(self):
        if isinstance(self.ev, Exception):
            raise self.ev
        yield from self.ev


pasos = []
tmp = tempfile.mkdtemp(prefix="dschat_logapp_")
ws = os.path.join(tmp, "proyecto")
os.makedirs(ws)
open(os.path.join(ws, "a.txt"), "w", encoding="utf-8").write("uno\n")
cfg = dsapi.Config(os.path.join(tmp, "cfg"))
cfg.set_session_key("sk-prueba")
root = tk.Tk()
app = dc.App(root, cfg, stream_factory=lambda e, m, t: Flujo(pasos.pop(0)), interactive=False)
pump(lambda: "Modelos de DeepSeek" in app.logp.text())
log = app.logp.text()
check("arranque: versión", f"CodeAgent {dc.version.VERSION} iniciado." in log, log)
check("arranque: carpeta de configuración", cfg.dir in log)
check("arranque: estado de las keys de los tres proveedores", "DeepSeek cargada" in log and "Claude sin cargar" in log and "OpenAI sin cargar" in log, log)
check("arranque: modelos listados", "Modelos de DeepSeek:" in log)
check("el panel está acoplado debajo del chat", str(app.logp.frame) in [str(x) for x in app.vpane.panes()])
check("la barra de estado quedó fuera del panel dividido (todo el ancho)", app.status.master is root and app.status.winfo_ismapped())

app.use_folder(ws)
lectura = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}}
falla = {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "no-existe.txt"})}}
pasos[:] = [[("reasoning", "pienso"), ("content", "voy"), ("tool_calls", [lectura, falla]),
             ("usage", {"prompt_tokens": 800, "completion_tokens": 10}), ("finish", "tool_calls")],
            [("content", "listo"), ("usage", {"prompt_tokens": 900, "completion_tokens": 5}), ("finish", "stop")]]
app.send("leé a.txt")
check("el turno termina", pump(lambda: not app.busy))
log = app.logp.text()
check("turno: dice a qué modelo se envió", "Enviando a deepseek" in log, log[-800:])
check("turno: cada paso queda cerrado con su resumen", "Paso 1 terminado" in log and "Paso 2 terminado" in log, log[-800:])
check("turno: no queda ninguna línea viva de spinner", not any(l[1] == "SPINNER" and "Paso" in l[0] and "terminado" not in l[0]
                                                            for l in app.logp.lines), log[-800:])
check("turno: tokens del paso", "Tokens: 800 entrada · 10 salida" in log, log[-800:])
check("turno: barra de contexto", re.search(r"Contexto \[[█░]+\] +\d+%", log) is not None, log[-800:])
check("turno: herramienta llamada y resultado", "→ read_file" in log and "← read_file:" in log, log[-800:])
errores = [l for l in app.logp.lines if l[0].split(" - ", 1)[1].startswith("← read_file") and l[1] == "ERROR"]
oks = [l for l in app.logp.lines if l[0].split(" - ", 1)[1].startswith("← read_file") and l[1] == "OK"]
check("turno: el resultado con error va en rojo y el bueno en verde (control)", len(errores) == 1 and len(oks) == 1,
      [(l[0], l[1]) for l in app.logp.lines if "←" in l[0]])
check("turno: terminado con duración", re.search(r"Terminado en \d+\.\d s\.", log) is not None, log[-300:])

pasos[:] = [dsapi.ApiError("HTTP 500: el servidor se cayó")]
n0 = len(app.logp.lines)
app.send("otra")
pump(lambda: not app.busy)
nuevas = app.logp.lines[n0:]       # después del error la app pide el saldo: esa línea puede quedar última
check("error: queda en rojo en el log", any(l[1] == "ERROR" and "servidor se cayó" in l[0] for l in nuevas), nuevas)
check("CONTROL: un turno sin error no tiene líneas rojas", not any(l[1] == "ERROR" for l in app.logp.lines[:n0]
                                                                   if "read_file" not in l[0]), app.logp.text()[-600:])

# guardar el estado desacoplado y reabrir
app.logp.detach()
root.update()
app.on_close()
check("al cerrar se guardó desacoplado", dsapi.Config(cfg.dir)["log_detached"] is True)
cfg2 = dsapi.Config(cfg.dir)
cfg2.set_session_key("sk-prueba")
root = tk.Tk()
app = dc.App(root, cfg2, stream_factory=lambda e, m, t: Flujo(pasos.pop(0)), interactive=False)
check("al reabrir arranca desacoplado", pump(lambda: app.logp.win is not None, 5))
app.logp.dock()
app.on_close()
check("CONTROL: acoplado se guarda acoplado", dsapi.Config(cfg.dir)["log_detached"] is False)

print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
