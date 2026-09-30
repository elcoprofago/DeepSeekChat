"""El botón «Actualizar» de la ventana y las piezas de actualizar.py que usa, en una carpeta portable falsa.

- preparar() descarga a app.new\\ sin tocar app\\ (la app está abierta).
- El reemplazo aparte espera a que se cierre la app: con un proceso de verdad corriendo desde runtime\\ de la carpeta
  falsa, no reemplaza hasta que ese proceso termina; si no termina a tiempo, no cambia nada y lo deja escrito.
- data\\ y Models\\ sobreviven (control); la app anterior queda de respaldo; VERSION.txt dice la nueva.
- El reemplazo corre de verdad como proceso aparte (lanzar_reemplazo), no solo como función.
- En la ventana: el botón consulta, descarga, lanza el reemplazo y se cierra; con la última versión no hace nada;
  fuera de una carpeta portable o con el agente trabajando se niega; al abrir muestra el resultado del reemplazo.
Nada sale a internet: pedir() se reemplaza por uno que copia un zip armado del repo.
Uso: python test_actualizar_boton.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import zipfile

import actualizar
import version

HERE = os.path.dirname(os.path.abspath(__file__))
V_VIEJA = "1.0.0.1"
NUEVA = version.VERSION
fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


def leer(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def escribir(p, texto):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(texto)


tmp = tempfile.mkdtemp(prefix="dschat_boton_act_")
ZIP = os.path.join(tmp, "release.zip")
with zipfile.ZipFile(ZIP, "w") as z:
    for n in sorted(os.listdir(HERE)):
        if os.path.isfile(os.path.join(HERE, n)):
            z.write(os.path.join(HERE, n), f"elcoprofago-DeepSeekChat-abc123/{n}")
REL = {"tag_name": "v" + NUEVA, "zipball_url": "https://ejemplo.invalid/zip", "body": "Notas de prueba."}


def pedir_falso(url, destino=None):
    if destino is None:
        return dict(REL)
    shutil.copy(ZIP, destino)


actualizar.pedir = pedir_falso


def portable(nombre):
    """Una carpeta portable falsa con la versión vieja y controles que tienen que sobrevivir."""
    raiz = os.path.join(tmp, nombre)
    escribir(os.path.join(raiz, "portable.flag"), "")
    escribir(os.path.join(raiz, "app", "version.py"), f'VERSION = "{V_VIEJA}"\n')
    escribir(os.path.join(raiz, "app", "SOLO_EN_LA_VIEJA.txt"), "vieja")
    escribir(os.path.join(raiz, "data", "config.json"), '{"control": 1}')
    escribir(os.path.join(raiz, "Models", "modelo.gguf"), "control")
    os.makedirs(os.path.join(raiz, "runtime"), exist_ok=True)
    actualizar.usar_raiz(raiz)
    return raiz


# ---------------------------------------------------------------- funciones, en el mismo proceso

raiz = portable("p1")
nueva, rel = actualizar.ultima()
check("ultima(): versión sin la v del tag", nueva == NUEVA, nueva)
n = actualizar.preparar(rel, nueva)
check("preparar: app.new con la versión nueva", n > 0 and f'"{NUEVA}"' in leer(os.path.join(raiz, "app.new", "version.py")))
check("preparar: app\\ no se toca con la app abierta", os.path.isfile(os.path.join(raiz, "app", "SOLO_EN_LA_VIEJA.txt"))
      and V_VIEJA in leer(os.path.join(raiz, "app", "version.py")))

actualizar.procesos_de_esta_carpeta = lambda: [4242]          # la app no se cierra
actualizar.despues_de_cerrar(V_VIEJA, NUEVA, espera=1, abrir=False)
res = actualizar.leer_resultado(os.path.join(raiz, "data"))
check("app que no se cierra: no reemplaza y lo dice", res.startswith("FALLÓ") and "4242" in res and V_VIEJA in res, res)
check("app que no se cierra: app\\ intacta", os.path.isfile(os.path.join(raiz, "app", "SOLO_EN_LA_VIEJA.txt")))

actualizar.procesos_de_esta_carpeta = lambda: []
actualizar.despues_de_cerrar(V_VIEJA, NUEVA, espera=1, abrir=False)
res = actualizar.leer_resultado(os.path.join(raiz, "data"))
check("app cerrada: reemplaza y lo dice", res.startswith("OK ") and NUEVA in res, res)
check("app\\ es la nueva", f'"{NUEVA}"' in leer(os.path.join(raiz, "app", "version.py"))
      and os.path.isfile(os.path.join(raiz, "app", "CodeAgent.pyw")))
resp = glob.glob(os.path.join(raiz, f"app.respaldo-*-antes-{V_VIEJA}"))
check("la anterior quedó de respaldo, entera", len(resp) == 1 and os.path.isfile(os.path.join(resp[0], "SOLO_EN_LA_VIEJA.txt")),
      resp)
check("control: data\\ sobrevive", leer(os.path.join(raiz, "data", "config.json")) == '{"control": 1}')
check("control: Models\\ sobrevive", leer(os.path.join(raiz, "Models", "modelo.gguf")) == "control")
check("VERSION.txt dice la nueva", leer(os.path.join(raiz, "VERSION.txt")).startswith(f"CodeAgent {NUEVA}"))
check("el resultado se informa una sola vez", actualizar.leer_resultado(os.path.join(raiz, "data")) == "")
check("no queda app.new", not os.path.exists(os.path.join(raiz, "app.new")))
try:
    actualizar.instalar(NUEVA, NUEVA)
    msg = ""
except RuntimeError as e:
    msg = str(e)
check("sin descarga verificada no instala nada", "app.new" in msg and os.path.isfile(os.path.join(raiz, "app", "CodeAgent.pyw")),
      msg)

raiz = portable("p2")
try:
    actualizar.preparar(rel, "9.9.9.9")                       # la release dice una versión y trae otra
    msg = ""
except RuntimeError as e:
    msg = str(e)
check("release con otra versión adentro: se rechaza", "9.9.9.9" in msg, msg)
check("...y no deja app.new ni toca app\\", not os.path.exists(os.path.join(raiz, "app.new"))
      and os.path.isfile(os.path.join(raiz, "app", "SOLO_EN_LA_VIEJA.txt")))

# ---------------------------------------------------------------- el reemplazo como proceso aparte, de verdad

raiz = portable("p3")
actualizar.preparar(rel, NUEVA)
# un proceso de verdad corriendo desde runtime\ de la carpeta falsa hace de «la app todavía abierta»
base = os.path.dirname(sys.executable)
for f in [sys.executable] + glob.glob(os.path.join(base, "python*.dll")) + glob.glob(os.path.join(base, "vcruntime*.dll")):
    shutil.copy2(f, os.path.join(raiz, "runtime"))
exe_falso = os.path.join(raiz, "runtime", os.path.basename(sys.executable))
entorno = dict(os.environ, PYTHONHOME=base)
app_abierta = subprocess.Popen([exe_falso, "-c", "import time; time.sleep(5)"], env=entorno)
time.sleep(0.5)
check("premisa: el proceso de la carpeta falsa está vivo", app_abierta.poll() is None, app_abierta.poll())
import importlib  # noqa: E402
importlib.reload(actualizar)                                 # vuelve el procesos_de_esta_carpeta real
actualizar.pedir = pedir_falso
actualizar.usar_raiz(raiz)
check("premisa: el detector ve ese proceso", app_abierta.pid in actualizar.procesos_de_esta_carpeta(),
      actualizar.procesos_de_esta_carpeta())
t0 = time.time()
actualizar.lanzar_reemplazo(V_VIEJA, NUEVA)
p_res = os.path.join(raiz, "data", actualizar.RESULTADO)
fin = time.time() + 40
tocada_con_app_abierta = False
while not os.path.exists(p_res) and time.time() < fin:
    if app_abierta.poll() is None and not os.path.isfile(os.path.join(raiz, "app", "SOLO_EN_LA_VIEJA.txt")):
        tocada_con_app_abierta = True
    time.sleep(0.2)
espera = time.time() - t0
check("proceso aparte: no reemplazó mientras la app seguía abierta",
      not tocada_con_app_abierta and app_abierta.poll() is not None and espera >= 4, (espera, app_abierta.poll()))
time.sleep(0.5)
res = actualizar.leer_resultado(os.path.join(raiz, "data"))
check("proceso aparte: reemplazó al cerrarse la app", res.startswith("OK ") and NUEVA in res, res)
check("proceso aparte: app\\ es la nueva", f'"{NUEVA}"' in leer(os.path.join(raiz, "app", "version.py")))
check("control: data\\ sobrevive al proceso aparte", leer(os.path.join(raiz, "data", "config.json")) == '{"control": 1}')

# ---------------------------------------------------------------- en la ventana

import dsapi  # noqa: E402
import deepseek_chat as dc  # noqa: E402
import providers  # noqa: E402

dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}
providers.list_models = lambda p, key: providers.fallback(p)

raiz = portable("p4")
cfg = dsapi.Config(os.path.join(raiz, "data"))
escribir(os.path.join(raiz, "data", actualizar.RESULTADO), f"OK Actualizado {V_VIEJA} -> {NUEVA}. Prueba.\n")
root = tk.Tk()
app = dc.App(root, cfg, interactive=False)


def pump(cond, timeout=8):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


pump(lambda: False, 0.3)
check("al abrir: muestra el resultado del reemplazo", f"Actualizado {V_VIEJA} -> {NUEVA}" in app.logp.text())
check("al abrir: el resultado se borra", not os.path.exists(os.path.join(raiz, "data", actualizar.RESULTADO)))
check("el botón Actualizar está en la barra de arriba", app.btn_update.cget("text").endswith("Actualizar"))

lanzados = []
actualizar.lanzar_reemplazo = lambda a, b: lanzados.append((a, b))
cerrada = []
app.on_close = lambda: cerrada.append(1)

actualizar.RAIZ = os.path.join(tmp, "no-portable")            # sin portable.flag
app.check_update()
check("fuera de una carpeta portable: se niega", app.warnings and "portable" in app.warnings[-1] and not lanzados,
      app.warnings)
actualizar.RAIZ = raiz

app.busy = True
app.check_update()
check("con el agente trabajando: se niega", "trabajando" in app.warnings[-1] and not lanzados, app.warnings)
app.busy = False

REL["tag_name"] = "v" + version.VERSION                       # la misma que corre
app.check_update()
check("ya en la última: no descarga ni cierra",
      pump(lambda: "ya tenés la última versión" in app.logp.text()) and not lanzados and not cerrada)
check("ya en la última: el botón vuelve a quedar habilitado", "disabled" not in app.btn_update.state())

REL["tag_name"] = "v99.0.0.0"
real_preparar = actualizar.preparar
actualizar.preparar = lambda rel, nueva: (_ for _ in ()).throw(RuntimeError("zip roto"))
app.check_update()
check("descarga que falla: lo dice y no cierra", pump(lambda: "zip roto" in app.logp.text()) and not lanzados and not cerrada,
      app.logp.text()[-300:])
check("descarga que falla: el botón vuelve a quedar habilitado", "disabled" not in app.btn_update.state())

actualizar.preparar = lambda rel, nueva: 42
app.check_update()
check("versión nueva: lanza el reemplazo y se cierra", pump(lambda: bool(cerrada)) and lanzados == [(version.VERSION, "99.0.0.0")],
      (lanzados, cerrada))
actualizar.preparar = real_preparar

del app.on_close
app.on_close()
shutil.rmtree(tmp, ignore_errors=True)
print()
print("TODO OK" if not fallas else f"{len(fallas)} FALLA(S): {fallas}")
sys.exit(1 if fallas else 0)
