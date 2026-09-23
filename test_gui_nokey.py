"""Regresión del bug 'Falta la API key' que seguía visible tras guardar la key.

Arranca SIN key (config temporal vacía) en modo interactivo, deja que la app abra sola la ventana de configuración,
pega una key y aprieta 'Probar y guardar' en el diálogo real. La red se simula. Uso: python test_gui_nokey.py
"""
import sys
import tempfile
import time
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

cfg = dsapi.Config(tempfile.mkdtemp(prefix="dschat_nokey_"))
root = tk.Tk()
app = dc.App(root, cfg)          # interactivo: la ventana de configuración se abre sola


def pump(cond, timeout=10):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def tops():
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def find(w, cls, text=None):
    for ch in w.winfo_children():
        if isinstance(ch, cls) and (text is None or str(ch.cget("text")) == text):
            return ch
        r = find(ch, cls, text)
        if r:
            return r


FRASE = "Falta la API key"
check("sin key: la nota aparece al arrancar", FRASE in app.chat.get_text())
check("sin key: el diálogo de configuración se abre solo", pump(lambda: len(tops()) == 1))

# mismo camino que el usuario: pega la key en el diálogo y aprieta "Probar y guardar"
dlg = tops()[0]
entry = find(dlg, dc.ttk.Entry)
entry.delete(0, "end")
entry.insert(0, "sk-de-prueba-123")
btn = find(dlg, dc.ttk.Button, "Probar y guardar")
check("premisa: el botón existe", btn is not None)
btn.invoke()

check("se guardó la key", pump(lambda: cfg.api_key == "sk-de-prueba-123"), cfg.api_key)
check("tras guardar: la nota 'Falta la API key' ya no está (el bug)", pump(lambda: FRASE not in app.chat.get_text(), 5), app.chat.get_text()[-200:])
check("tras guardar: carga el saldo", pump(lambda: "US$ 1.00" in app.balance_lbl.cget("text")), app.balance_lbl.cget("text"))

# control: una nota que NO es de key sobrevive a la limpieza, y la de key repetida no se acumula
app.chat.note("NOTA-QUE-DEBE-SOBREVIVIR")
cfg.set_session_key("")
app.key_changed()
app.key_changed()
root.update()
txt = app.chat.get_text()
check("control: una nota ajena sobrevive", "NOTA-QUE-DEBE-SOBREVIVIR" in txt)
check("control: sin key la nota vuelve, pero una sola vez", txt.count(FRASE) == 1, txt.count(FRASE))
cfg.set_session_key("sk-otra")
app.key_changed()
root.update()
check("con key otra vez: desaparece", FRASE not in app.chat.get_text())

app.on_close()
print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
