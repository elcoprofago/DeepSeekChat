"""dialogs: confirmar, desbloquear, visor y configuración, contra una app falsa. Sin red (get_balance se simula)."""
import os
import queue
import shutil
import tempfile
import tkinter as tk
from tkinter import ttk

import dialogs
import dsapi
import theme

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


class FakeApp:
    themes = theme.THEMES

    def __init__(self, root, cfg):
        self.root, self.cfg, self.t = root, cfg, theme.pick(cfg["theme"])
        self.ui = queue.Queue()
        self.key_changes = 0
        self.themes_applied = 0
        self.rescans = 0

    def post(self, fn):
        self.ui.put(fn)

    def pump(self):
        while not self.ui.empty():
            self.ui.get()()
        self.root.update()

    def key_changed(self):
        self.key_changes += 1

    def rescan_models(self):
        self.rescans += 1
        return []

    def apply_theme(self):
        self.themes_applied += 1
        self.t = theme.pick(self.cfg["theme"])
        theme.apply_styles(self.root, self.t)


tmp = tempfile.mkdtemp(prefix="dschat_dlg_")
os.environ["DSCHAT_CONFIG_DIR"] = os.path.join(tmp, "cfg")
cfg = dsapi.Config()
root = tk.Tk()
app = FakeApp(root, cfg)
theme.apply_styles(root, app.t)
root.update()

# ---- ConfirmDialog
res = []
diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-viejo\n+nuevo\n contexto\n"
d = dialogs.ConfirmDialog(app, "edit", "x.py", diff, res.append)
root.update()
tags = lambda pal: [t for t in d.view.tag_names(d.view.search(pal, "1.0"))]
check("diff: '+' verde, '-' rojo, '@@' gris", "add" in tags("+nuevo") and "del" in tags("-viejo") and "hunk" in tags("@@ -1"), (tags("+nuevo"), tags("-viejo")))
check("diff: control, la línea de contexto no lleva color", not {"add", "del", "hunk"} & set(tags("contexto")), tags("contexto"))
check("el visor es de solo lectura", str(d.view.cget("state")) == "disabled")
d._finish("allow")
d._finish("deny")           # una segunda respuesta no cuenta
root.update()
check("responde una sola vez", res == ["allow"], res)
check("la ventana se cerró", not d.win.winfo_exists())

res.clear()
d = dialogs.ConfirmDialog(app, "command", "git status", "git status", res.append)
root.update()
check("comando: sin botón de 'permitir todas las ediciones'", not any("ediciones" in str(w.cget("text")) for w in d.win.winfo_children()[0].winfo_children()[-1].winfo_children() if isinstance(w, ttk.Button)))
d.dismiss()
check("dismiss niega", res == ["deny"], res)

res.clear()
d = dialogs.ConfirmDialog(app, "edit", "grande", "+" + "x" * 50000, res.append)
root.update()
check("detalle enorme: se recorta en pantalla", "recortado" in d.view.get("1.0", "end") and len(d.view.get("1.0", "end")) < 25000)
d.win.focus_force()
d.win.event_generate("<Escape>")
root.update()
check("Escape niega", res == ["deny"], res)

# la X de la ventana también niega
res.clear()
d = dialogs.ConfirmDialog(app, "edit", "y", "+a", res.append)
root.update()
check("la X de la ventana tiene manejador", bool(d.win.protocol("WM_DELETE_WINDOW")))
d._finish("deny")
check("cerrar con la X niega", res == ["deny"])

# ---- UnlockDialog
blob_cfg_dir = os.path.join(tmp, "cfg2")
os.environ["DSCHAT_CONFIG_DIR"] = blob_cfg_dir
c2 = dsapi.Config()
c2.set_api_key("sk-prueba-123456", "secreto1")
c3 = dsapi.Config()
check("premisa: la key con contraseña arranca bloqueada", c3.needs_unlock and not c3.api_key)
app3 = FakeApp(root, c3)
u = dialogs.UnlockDialog(app3)
root.update()
u.pw.set("mala")
u.submit()
check("contraseña mala: mensaje y sigue bloqueada", "incorrecta" in u.msg.cget("text") and c3.needs_unlock and app3.key_changes == 0)
check("contraseña mala: se vacía el campo", u.pw.get() == "")
u.pw.set("secreto1")
u.submit()
root.update()
check("contraseña buena: key disponible, aviso a la app, ventana cerrada", c3.api_key == "sk-prueba-123456" and app3.key_changes == 1 and not u.win.winfo_exists())

# ---- FileViewer
f_ok = os.path.join(tmp, "nota ñ.txt")
open(f_ok, "w", encoding="utf-8").write("línea uno\nlínea dos\n")
f_bin = os.path.join(tmp, "b.bin")
open(f_bin, "wb").write(b"\x00\x01\x02" * 100)
adj = []
v = dialogs.FileViewer(app, f_ok, adj.append)
root.update()
check("visor muestra el contenido", "línea dos" in v.view.get("1.0", "end"))
v.copy_all()
check("copiar todo", root.clipboard_get() == "línea uno\nlínea dos\n", root.clipboard_get())
v.win.destroy()
v = dialogs.FileViewer(app, f_bin, adj.append)
root.update()
check("binario: mensaje en vez de basura", "No se puede mostrar" in v.view.get("1.0", "end"), v.view.get("1.0", "end"))
v.win.destroy()
v = dialogs.FileViewer(app, os.path.join(tmp, "no existe.txt"), adj.append)
root.update()
check("inexistente: no revienta", "No se puede mostrar" in v.view.get("1.0", "end"))
v.win.destroy()

# ---- SettingsDialog
os.environ["DSCHAT_CONFIG_DIR"] = os.path.join(tmp, "cfg4")
c4 = dsapi.Config()
app4 = FakeApp(root, c4)
llamadas = []


def balance_falso(key):
    llamadas.append(key)
    if key == "sk-mala":
        raise dsapi.ApiError("La API key fue rechazada (401).")
    if key == "sk-sinsaldo":
        return {"available": False, "text": "US$ 0.00"}
    return {"available": True, "text": "US$ 5.00"}


real_balance = dsapi.get_balance
dsapi.get_balance = balance_falso
try:
    s = dialogs.SettingsDialog(app4)
    root.update()
    s.win.grab_release()

    s.key_var.set("")
    s.test_and_save()
    check("key vacía: pide pegarla y no llama a la red", "primero" in s.key_msg.cget("text") and not llamadas)

    s.key_var.set("sk-mala")
    s.mode.set("dpapi")
    s.test_and_save()
    check("mientras prueba, el botón se deshabilita", str(s.btn_test.cget("state")) == "disabled")
    import time
    t0 = time.time()
    while not app4.ui.qsize() and time.time() - t0 < 5:
        time.sleep(0.02)
    app4.pump()
    check("key rechazada: error visible, NO se guarda", "rechazada" in s.key_msg.cget("text") and not c4.api_key and not c4.has_key(), s.key_msg.cget("text"))
    check("key rechazada: el botón vuelve a habilitarse", str(s.btn_test.cget("state")) == "normal")

    s.key_var.set("sk-sinsaldo")
    s.test_and_save()
    t0 = time.time()
    while not app4.ui.qsize() and time.time() - t0 < 5:
        time.sleep(0.02)
    app4.pump()
    check("cuenta no disponible: no se guarda", "no disponible" in s.key_msg.cget("text") and not c4.has_key(), s.key_msg.cget("text"))

    s.mode.set("password")
    s._mode_changed()
    s.key_var.set("sk-buena-abcdef")
    s.pw1.set("abc")
    s.pw2.set("abc")
    n = len(llamadas)
    s.test_and_save()
    check("contraseña corta: se rechaza antes de llamar a la red", "6 caracteres" in s.key_msg.cget("text") and len(llamadas) == n)
    s.pw1.set("abcdef")
    s.pw2.set("abcdeg")
    s.test_and_save()
    check("contraseñas distintas: se rechaza antes de llamar a la red", "no coinciden" in s.key_msg.cget("text") and len(llamadas) == n)

    s.pw2.set("abcdef")
    s.test_and_save()
    t0 = time.time()
    while not app4.ui.qsize() and time.time() - t0 < 5:
        time.sleep(0.02)
    app4.pump()
    check("key buena con contraseña: guardada, campos limpios, app avisada",
          c4.api_key == "sk-buena-abcdef" and c4.key_mode == "password" and s.key_var.get() == "" and s.pw1.get() == "" and app4.key_changes == 1,
          (c4.key_mode, app4.key_changes, s.key_msg.cget("text")))
    check("saldo visible en el mensaje", "US$ 5.00" in s.key_msg.cget("text"))
    raw = open(c4.path, encoding="utf-8").read()
    check("la key no está en claro en config.json", "sk-buena-abcdef" not in raw and c4["api_key_pw"] != "")

    # modo sesión con una copia guardada antes: la key nueva vale en memoria, la copia vieja NO se toca ni se oculta
    s.mode.set("session")
    s.key_var.set("sk-sesion-zzz")
    s.test_and_save()
    t0 = time.time()
    while not app4.ui.qsize() and time.time() - t0 < 5:
        time.sleep(0.02)
    app4.pump()
    raw = open(c4.path, encoding="utf-8").read()
    check("modo sesión: la key nueva no se escribe en disco", c4.api_key == "sk-sesion-zzz" and "sk-sesion-zzz" not in raw)
    check("modo sesión con copia previa: avisa que sigue guardada", "sigue en disco" in s.key_msg.cget("text"), s.key_msg.cget("text"))
    c4.set_api_key("")
    check("borrar la key deja config sin rastro", not c4.has_key() and "sk-buena" not in open(c4.path, encoding="utf-8").read())
    s.key_var.set("sk-sesion-yyy")
    s.test_and_save()
    t0 = time.time()
    while not app4.ui.qsize() and time.time() - t0 < 5:
        time.sleep(0.02)
    app4.pump()
    raw = open(c4.path, encoding="utf-8").read()
    check("modo sesión sin copia previa: nada en disco y sin aviso de copia",
          c4.api_key == "sk-sesion-yyy" and "sk-sesion" not in raw and c4.key_mode == "none" and "sigue en disco" not in s.key_msg.cget("text"),
          (c4.key_mode, s.key_msg.cget("text")))

    # opciones
    s.max_var.set(2048)
    s.font_var.set(13)
    s.theme_var.set("oscuro")
    s.sys_text.delete("1.0", "end")
    s.sys_text.insert("1.0", "Sé breve.")
    carpeta_modelos = os.path.join(tmp, "mis modelos")
    os.makedirs(carpeta_modelos)
    s.dirs.insert("end", carpeta_modelos)
    s.ctx_var.set(8192)
    s.save_options()
    root.update()
    c5 = dsapi.Config()
    check("opciones guardadas en disco", c5["max_tokens"] == 2048 and c5["font_size"] == 13 and c5["theme"] == "oscuro"
          and c5["system_prompt"] == "Sé breve." and c5["local_ctx"] == 8192 and c5["model_dirs"] == [carpeta_modelos],
          (c5["max_tokens"], c5["theme"], c5["model_dirs"]))
    check("guardar opciones re-aplica tema y reescanea modelos", app4.themes_applied == 1 and app4.rescans == 1)
    check("el diálogo se repinta con el tema nuevo", str(s.win.cget("bg")) == theme.THEMES["oscuro"]["bg"], s.win.cget("bg"))

    s.max_var.set(1)
    s.save_options()
    check("max_tokens fuera de rango se acota", c4["max_tokens"] == 256, c4["max_tokens"])
    s.win.destroy()

    # --- persistencia: cerrar guarda sin apretar «Guardar opciones»; lo local se guarda apenas cambia
    s = dialogs.SettingsDialog(app4)
    root.update()
    n_temas, n_scans = app4.themes_applied, app4.rescans
    s.theme_var.set("claro")
    s.budget_var.set(2500000)
    s.close()
    root.update()
    c6 = dsapi.Config()
    check("cerrar sin guardar: el tema y el presupuesto quedan en disco", c6["theme"] == "claro" and c6["token_budget"] == 2500000, (c6["theme"], c6["token_budget"]))
    check("cerrar con el tema cambiado lo aplica; sin cambios locales no reescanea", app4.themes_applied == n_temas + 1 and app4.rescans == n_scans)
    s = dialogs.SettingsDialog(app4)
    root.update()
    n_temas = app4.themes_applied
    s.close()
    check("CONTROL: cerrar sin tocar nada no re-aplica nada", app4.themes_applied == n_temas)
    s = dialogs.SettingsDialog(app4)
    root.update()
    falso_srv = os.path.join(tmp, "bin", "llama-server.exe")
    os.makedirs(os.path.dirname(falso_srv), exist_ok=True)
    open(falso_srv, "wb").write(b"x")
    s.srv_var.set(falso_srv)
    s.ctx_var.set(12288)
    s._autosave_local()
    c7 = dsapi.Config()
    check("ruta del llama-server y contexto se guardan sin cerrar ni apretar nada", c7["llama_server_path"] == falso_srv and c7["local_ctx"] == 12288,
          (c7["llama_server_path"], c7["local_ctx"]))
    s.win.destroy()
finally:
    dsapi.get_balance = real_balance

root.destroy()
shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
