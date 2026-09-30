"""Keys de varios proveedores en el diálogo real: guardar la de Claude sin tocar la de DeepSeek (control),
desbloquear keys con contraseñas distintas, y 'Olvidé' que borra solo las bloqueadas. Red simulada.
Uso: python test_gui_providers.py
"""
import sys
import tempfile
import time
import tkinter as tk

import claudeapi
import deepseek_chat as dc
import dialogs
import dsapi
import providers

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 1.00"}
claudeapi.month_summary = lambda key, now=None: {"closed": 0.0, "today": 1.0, "total": 1.0, "unpriced": [], "check": "ok", "check_calc": 1.0, "check_reported": 1.0}  # sin red: el gasto de Claude también simulado
probadas = []


def fake_list(p, key):
    probadas.append((p, key))
    return providers.fallback(p)


providers.list_models = fake_list

cfg = dsapi.Config(tempfile.mkdtemp(prefix="dschat_prov_"))
cfg.set_api_key("sk-deepseek-control", provider="deepseek")
root = tk.Tk()
app = dc.App(root, cfg, interactive=False)


def pump(cond, timeout=10):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- guardar la key de Claude desde el diálogo
dlg = dialogs.SettingsDialog(app)
check("premisa: arranca en DeepSeek", dlg.provider() == "deepseek", dlg.provider())
dlg.prov_var.set("Claude")
dlg._provider_changed()
check("cambió a Claude", dlg.provider() == "anthropic")
check("sin key de Claude el estado no muestra la de DeepSeek", "sk-deep" not in dlg.key_state.cget("text") and "…" not in dlg.key_state.cget("text"),
      dlg.key_state.cget("text"))
dlg.key_var.set("sk-ant-prueba-1")
dlg.mode.set("dpapi")
dlg._mode_changed()
dlg.test_and_save()
check("guardó la key de Claude", pump(lambda: cfg.key("anthropic") == "sk-ant-prueba-1"), cfg.key("anthropic"))
check("la probó contra Claude", ("anthropic", "sk-ant-prueba-1") in probadas, probadas)
check("control: la key de DeepSeek sigue intacta", cfg.key("deepseek") == "sk-deepseek-control", cfg.key("deepseek"))
check("mensaje con modelos, sin 'Saldo'", "modelos disponibles" in dlg.key_msg.cget("text") and "Saldo" not in dlg.key_msg.cget("text"),
      dlg.key_msg.cget("text"))
check("aparecen los modelos de Claude en la lista", pump(lambda: any(app._prov(m) == "anthropic" for m in app.remote_models)))
dlg.win.destroy()

# --- dos keys con contraseñas distintas
cfg2 = dsapi.Config(tempfile.mkdtemp(prefix="dschat_prov2_"))
cfg2.set_api_key("sk-ds-A", "clave-uno", provider="deepseek")
cfg2.set_api_key("sk-oa-B", "clave-dos", provider="openai")
cfg3 = dsapi.Config(cfg2.dir)                                      # reabrir: como al arrancar
check("premisa: al reabrir, las dos están bloqueadas", sorted(cfg3.locked()) == ["deepseek", "openai"], cfg3.locked())
app.cfg = cfg3
u = dialogs.UnlockDialog(app)
check("el título nombra a los dos", "DeepSeek" in u.title.cget("text") and "OpenAI" in u.title.cget("text"), u.title.cget("text"))
u.pw.set("clave-uno")
u.submit()
check("con la primera: DeepSeek abierta", cfg3.key("deepseek") == "sk-ds-A")
check("con la primera: la ventana sigue abierta para OpenAI", u.win.winfo_exists() and "OpenAI" in u.msg.cget("text"), u.msg.cget("text"))
u.pw.set("mal")
u.submit()
check("contraseña incorrecta: lo dice", "incorrecta" in u.msg.cget("text"))
u.pw.set("clave-dos")
u.submit()
check("con la segunda: OpenAI abierta y ventana cerrada", cfg3.key("openai") == "sk-oa-B" and not u.win.winfo_exists())

# --- Olvidé: borra solo las bloqueadas
cfg4 = dsapi.Config(tempfile.mkdtemp(prefix="dschat_prov4_"))
cfg4.set_api_key("sk-ant-sin-pw", provider="anthropic")          # DPAPI: no está bloqueada, debe sobrevivir
cfg4.set_api_key("sk-oa-bloq", "pw", provider="openai")
cfg5 = dsapi.Config(cfg4.dir)

check("premisa: solo OpenAI bloqueada", cfg5.locked() == ["openai"], cfg5.locked())
app.cfg = cfg5
u = dialogs.UnlockDialog(app)
dialogs.messagebox.askyesno = lambda *a, **k: True
u.forgot()
check("olvidé: OpenAI borrada", not cfg5.has_key("openai"))
check("control: Claude sobrevive", cfg5.key("anthropic") == "sk-ant-sin-pw", cfg5.key("anthropic"))

app.cfg = cfg
app.on_close()
print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
