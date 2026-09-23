"""Modo portable: config en <raíz>\\data, y el agente encuentra el Python de <raíz>\\runtime en su PATH (al final).

Simula una raíz portable en un directorio temporal (parcha dsapi.APP_DIR) y la compara con una instalación normal,
que es el control: ahí el comando de runtime\\ NO debe encontrarse. Uso: python test_portable.py
"""
import os
import sys
import tempfile
import tkinter as tk

import deepseek_chat as dc
import dsapi

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 1.00"}

tmp = tempfile.mkdtemp(prefix="dschat_port_ ñ ")
raiz = os.path.join(tmp, "pendrive")
for d in ("app", "runtime", "data", "Models"):
    os.makedirs(os.path.join(raiz, d))
open(os.path.join(raiz, "portable.flag"), "w").write("x")
open(os.path.join(raiz, "runtime", "hola-portable.bat"), "w").write("@echo PORTABLE-OK\r\n")
ws = os.path.join(tmp, "proyecto")
os.makedirs(ws)

real_app_dir = dsapi.APP_DIR
root = tk.Tk()
try:
    # --- portable
    dsapi.APP_DIR = os.path.join(raiz, "app")
    cfg = dsapi.Config()
    check("premisa: la config detecta modo portable", cfg.portable and cfg.root == raiz, (cfg.portable, cfg.root))
    check("los datos van a <raíz>\\data", os.path.normcase(cfg.dir) == os.path.normcase(os.path.join(raiz, "data")), cfg.dir)
    app = dc.App(root, cfg, interactive=False)
    app.use_folder(ws)
    app._set_approval("all")      # sin diálogo: este test corre en el hilo de la UI
    tb = app.toolbox
    check("portable: la caja de herramientas recibe runtime\\ como PATH extra", tb is not None and tb.extra_path == [os.path.join(raiz, "runtime")], getattr(tb, "extra_path", None))
    r = tb.execute("run_command", {"command": "hola-portable"})
    check("portable: el agente ejecuta un programa que solo está en runtime\\", "PORTABLE-OK" in r, r)
    app.on_close()

    # --- control: instalación normal (sin portable.flag) no debe ver runtime\
    dsapi.APP_DIR = real_app_dir
    cfg2 = dsapi.Config(os.path.join(tmp, "cfg_normal"))
    check("control: la config normal no es portable", not cfg2.portable)
    root = tk.Tk()              # on_close() destruyó la anterior
    app2 = dc.App(root, cfg2, interactive=False)
    app2.use_folder(ws)
    app2._set_approval("all")
    tb2 = app2.toolbox
    check("control: sin portable, no hay PATH extra", tb2 is not None and tb2.extra_path == [], getattr(tb2, "extra_path", None))
    r2 = tb2.execute("run_command", {"command": "hola-portable"})
    check("control: sin portable, el mismo comando NO se encuentra", "PORTABLE-OK" not in r2 and "exit code 0" not in r2, r2)
    app2.on_close()
finally:
    dsapi.APP_DIR = real_app_dir

print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
