"""Regresión del bug 'Falta la API key' que seguía visible tras guardar la key.

Arranca SIN key (config temporal vacía), guarda una por el diálogo real y verifica que la nota desaparezca.
Uso: DEEPSEEK_API_KEY=... python test_gui_nokey.py
"""
import os
import sys
import tempfile
import time
import tkinter as tk

import dsapi
import deepseek_chat as dc

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not KEY:
    sys.exit("Falta DEEPSEEK_API_KEY")

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{detalle}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


cfg = dsapi.Config(tempfile.mkdtemp(prefix="dschat_nokey_"))
root = tk.Tk()
app = dc.App(root, cfg)


def pump(cond, timeout=30):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def chat_text():
    return app.chat.get("1.0", "end")


FRASE = "Falta la API key"
check("sin key: la nota aparece al arrancar", FRASE in chat_text())
tops = lambda: [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
check("sin key: el diálogo de configuración se abre solo", pump(lambda: len(tops()) == 1))

# mismo camino que el usuario: pega la key en el diálogo y aprieta "Probar y guardar"
sd = next(o for o in [getattr(app, "_last_settings", None)] if o) if hasattr(app, "_last_settings") else None
if sd is None:
    # el diálogo no se guarda en la app: se lo busca por el widget
    def find_entry(w):
        for ch in w.winfo_children():
            if isinstance(ch, dc.ttk.Entry):
                return ch
            r = find_entry(ch)
            if r:
                return r
    dlg = tops()[0]
    entry = find_entry(dlg)
    entry.delete(0, "end")
    entry.insert(0, KEY)
    btn = None

    def find_btn(w):
        for ch in w.winfo_children():
            if isinstance(ch, dc.ttk.Button) and ch.cget("text") == "Probar y guardar":
                return ch
            r = find_btn(ch)
            if r:
                return r
    btn = find_btn(dlg)
    btn.invoke()

check("se guardó la key", pump(lambda: cfg.api_key == KEY))
check("tras guardar: la nota 'Falta la API key' ya no está (el bug)", pump(lambda: FRASE not in chat_text(), 5), chat_text()[-200:])
check("tras guardar: cargan los modelos y el saldo", pump(lambda: app.balance_text != "—"), app.balance_text)

# control: una nota que NO es de key sobrevive a la limpieza
app._system_note("NOTA-QUE-DEBE-SOBREVIVIR")
app._nokey_note("Falta la API key (otra vez)")
app.key_changed()
root.update()
check("control: una nota ajena sobrevive a la limpieza", "NOTA-QUE-DEBE-SOBREVIVIR" in chat_text())
check("control: la nota de key repetida sí se retira", FRASE not in chat_text())

root.destroy()
print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
