"""Prueba de la ventana real contra la API en vivo. Uso: DEEPSEEK_API_KEY=... python test_gui.py [carpeta_capturas]

Usa una carpeta de configuración temporal: nunca toca la configuración real del usuario.
"""
import ctypes
import os
import sys
import tempfile
import time
import tkinter as tk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import dsapi
import deepseek_chat as dc
from tkinter import messagebox

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not KEY:
    sys.exit("Falta DEEPSEEK_API_KEY")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="dschat_shots_")
os.makedirs(SHOTS, exist_ok=True)

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{detalle}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


cfgdir = tempfile.mkdtemp(prefix="dschat_cfg_")
cfg = dsapi.Config(cfgdir)
cfg.set_api_key(KEY)

# los cuadros de diálogo modales se interceptan y se registran
avisos = []
messagebox.showwarning = lambda *a, **k: avisos.append(("warn", a[1] if len(a) > 1 else k.get("message")))
messagebox.showerror = lambda *a, **k: avisos.append(("error", a[1] if len(a) > 1 else k.get("message")))

root = tk.Tk()
root.attributes("-topmost", True)
app = dc.App(root, cfg)

cargados = []
orig = app._models_loaded
app._models_loaded = lambda res: (cargados.append(res), orig(res))


def pump(cond, timeout=90):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def chat_text():
    return app.chat.get("1.0", "end")


def shot(nombre):
    from PIL import ImageGrab
    root.update()
    time.sleep(0.4)
    root.update()
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(SHOTS, nombre))


# ---- arranque
check("arranca sin abrir el diálogo de key (ya había una)", pump(lambda: app.balance_text != "—", 30))
check("saldo visible en la barra", "US$" in app.balance_lbl.cget("text"), app.balance_lbl.cget("text"))
check("modelos cargados desde el servidor", pump(lambda: bool(cargados), 30) and len(cargados[0]) >= 1)
check("selector de modelo con los ids del servidor", set(app.model_cb.cget("values")) >= {m["id"] for m in cargados[0]})
app.effort_var.set("low")
app.on_effort_change()
check("selector de effort ofrece low/high/max", set(app.effort_cb.cget("values")) == {dc.SIN_EFFORT, "low", "high", "max"}, str(app.effort_cb.cget("values")))
check("effort persistido en config", dsapi.Config(cfgdir)["effort"] == "low")

# ---- adjunto + envío
marca = os.path.join(cfgdir, "datos.py")
open(marca, "w", encoding="utf-8").write("CODIGO_SECRETO = 'AZUL-7391'\n")
binario = os.path.join(cfgdir, "raro.bin")
open(binario, "wb").write(b"\x00\x01\x02")
app.attach_paths([marca, binario])
check("adjunto de texto aceptado", app.attachments == [os.path.normpath(marca)], str(app.attachments))
check("adjunto binario rechazado con aviso", any(t == "warn" and "binario" in str(m) for t, m in avisos), str(avisos))
check("aparece la ficha del adjunto", len(app.attach_bar.winfo_children()) == 1)

app.send("¿Cuál es el valor de CODIGO_SECRETO en el archivo adjunto? Respondé solo el valor.")
check("mientras responde: botón dice Detener y el + se bloquea", app.busy and app.btn_send.cget("text") == "Detener" and str(app.btn_plus.cget("state")) == "disabled")
check("respuesta 1 completa", pump(lambda: not app.busy))
r1 = app.messages[-1]["content"] if app.messages and app.messages[-1]["role"] == "assistant" else ""
check("el modelo leyó el adjunto (dato imposible de adivinar)", "AZUL-7391" in r1, r1[:200])
check("adjuntos limpios tras enviar", app.attachments == [] and len(app.attach_bar.winfo_children()) == 0)
check("tokens contados", app.totals["in"] > 0 and app.totals["out"] > 0, str(app.totals))
check("historial autoguardado en disco", app.transcript_path and os.path.exists(app.transcript_path) and "AZUL-7391" in open(app.transcript_path, encoding="utf-8").read())
ctx1 = app.last_usage["prompt_tokens"]

# ---- segundo turno: el historial viaja
app.send("Repetí el nombre de la variable de la que te pregunté antes, solo el nombre.")
check("respuesta 2 completa", pump(lambda: not app.busy))
r2 = app.messages[-1]["content"]
check("recuerda el turno anterior (historial enviado)", "CODIGO_SECRETO" in r2, r2[:200])
check("el contexto usado creció", app.last_usage["prompt_tokens"] > ctx1, f"{ctx1} -> {app.last_usage['prompt_tokens']}")

# ---- prompt de sistema con marca detectable
cfg["system_prompt"] = "Terminá SIEMPRE tu respuesta con la palabra exacta FIN-DE-PRUEBA."
app.send("Decime un color.")
check("respuesta 3 completa", pump(lambda: not app.busy))
check("el prompt de sistema se envía", "FIN-DE-PRUEBA" in app.messages[-1]["content"], app.messages[-1]["content"][:200])
cfg["system_prompt"] = ""

# ---- bloque de código con botón de copiar
app.send("Dame una función Python que sume dos números. Solo el bloque de código, sin explicación.")
check("respuesta 4 completa", pump(lambda: not app.busy))
ventanas = [x for x in app.chat.dump("1.0", "end", window=True) if x[0] == "window"]
check("el bloque de código trae botón Copiar", len(ventanas) >= 1, chat_text()[-300:])
check("copiar última respuesta va al portapapeles", (app.copy_last(), "def" in root.clipboard_get())[1])
check("razonamiento plegado disponible", any(t.startswith("rt") for t in app.chat.tag_names()))
txt = chat_text()
check("el encabezado DeepSeek no queda pegado al texto (regresión)", "DeepSeek▸" not in txt and "DeepSeek✖" not in txt and "DeepSeekdef" not in txt, txt[-300:])
# la barra de estado y la entrada tienen que estar DENTRO de la ventana (regresión: quedaban fuera de pantalla)
root.update()
H = root.winfo_height()
for nombre, w in (("barra de estado (tokens/saldo)", app.status), ("cuadro de entrada", app.bottom)):
    check(f"{nombre} visible dentro de la ventana", w.winfo_ismapped() and w.winfo_y() + w.winfo_height() <= H,
          f"y={w.winfo_y()} alto={w.winfo_height()} ventana={H}")
check("el visor de tokens muestra datos de la sesión", "entrada" in app.tokens_lbl.cget("text") and "Contexto" in app.tokens_lbl.cget("text"), app.tokens_lbl.cget("text"))
root.geometry("640x480")   # ventana mínima: lo de abajo tiene que seguir a la vista
root.update()
H = root.winfo_height()
check("con la ventana mínima la barra de estado sigue visible", app.status.winfo_y() + app.status.winfo_height() <= H,
      f"y={app.status.winfo_y()} alto={app.status.winfo_height()} ventana={H}")
root.geometry("960x720")
root.update()
shot("1_chat_claro.png")

# ---- cancelar a mitad
antes = len(app.messages)
app.input.delete("1.0", "end")
app.send("Escribí un cuento largo de 800 palabras sobre un faro.")
check("empezó a llegar la respuesta (no solo razonamiento)", pump(lambda: bool(app.cur and app.cur["content"]), 90))
parcial = app.cur["content"]
app.cancel()
check("cancelar termina limpio (no queda ocupado)", pump(lambda: not app.busy, 30))
tras = chat_text()
check("cancelar NO muestra un error (regresión: salía 'NoneType has no attribute peek')",
      "Error inesperado" not in tras and "Se cortó la conexión" not in tras and "✖" not in tras[tras.rfind("faro"):], tras[-300:])
check("cancelar avisa 'Interrumpido'", "Interrumpido" in tras)
check("cancelar conserva la respuesta parcial en el historial",
      len(app.messages) == antes + 2 and app.messages[-1]["role"] == "assistant" and app.messages[-1]["content"].startswith(parcial[:20]),
      str([m["role"] for m in app.messages]))
check("cancelar deja la ventana usable (botón vuelve a Enviar)", app.btn_send.cget("text") == "Enviar")

# ---- key mala: error visible, mensaje restaurado, nada corrupto
cfg.set_api_key("sk-key-mala-de-prueba")
n = len(app.messages)
app.input.delete("1.0", "end")
app.send("Esto no debería enviarse bien.")
check("con key mala termina", pump(lambda: not app.busy, 30))
check("muestra el error en el chat", "API key inválida" in chat_text() or "inválida" in chat_text(), chat_text()[-200:])
check("no deja el mensaje huérfano en el historial", len(app.messages) == n)
check("devuelve el texto al cuadro de entrada", "Esto no debería" in app.input.get("1.0", "end"))
app.input.delete("1.0", "end")
cfg.set_api_key(KEY)

# ---- diálogo de configuración
app.open_settings()
root.update()
dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
check("el diálogo de configuración se abre", dlg.winfo_exists() == 1)
from deepseek_chat import SettingsDialog
# reabrir para tener la referencia al objeto
dlg.destroy()
sd = SettingsDialog(app)
root.update()
check("muestra la key enmascarada, no completa", dsapi.Config.mask(KEY) in sd.key_state.cget("text") and KEY not in sd.key_state.cget("text"), sd.key_state.cget("text"))
sd.key_var.set("sk-mala-mala")
sd.test_and_save()
pump(lambda: "No se guardó" in sd.key_msg.cget("text"), 30)
check("key mala: no se guarda y explica por qué", "No se guardó" in sd.key_msg.cget("text") and cfg.api_key == KEY, sd.key_msg.cget("text"))
sd.key_var.set(KEY)
sd.test_and_save()
pump(lambda: "guardada" in sd.key_msg.cget("text").lower() and "válida" in sd.key_msg.cget("text").lower(), 30)
check("key buena: se prueba, se guarda y muestra saldo", "Saldo" in sd.key_msg.cget("text"), sd.key_msg.cget("text"))
check("el campo de key se vacía tras guardar", sd.key_var.get() == "")
sd.theme_var.set("oscuro")
sd.font_var.set(12)
sd.max_var.set(8192)
sd.sys_text.insert("1.0", "Sé breve. ")
sd.save_options()
c2 = dsapi.Config(cfgdir)
check("opciones persistidas", c2["theme"] == "oscuro" and c2["font_size"] == 12 and c2["max_tokens"] == 8192 and c2["system_prompt"].startswith("Sé breve"))
check("la key sigue intacta tras guardar opciones", c2.api_key == KEY)
oscuro = dc.TEMAS["oscuro"]
check("el diálogo repinta su caja de texto al cambiar de tema (regresión: quedaba blanca)", sd.sys_text.cget("bg") == oscuro["panel"], sd.sys_text.cget("bg"))
app._set_state("prueba")
check("el texto de ayuda sigue el tema (regresión: quedaba ilegible en oscuro)",
      str(app.state_lbl.cget("style")) == "TLabel" and app.style.lookup("TLabel", "foreground") == oscuro["fg"], app.style.lookup("TLabel", "foreground"))
app._set_state("falla", error=True)
check("los errores de estado usan el color de error del tema", app.style.lookup("Err.TLabel", "foreground") == oscuro["err"])
app._set_state("Enter envía · Shift+Enter salto de línea")
shot("2_configuracion_oscuro.png")
sd.win.destroy()
root.update()
shot("3_chat_oscuro.png")

# ---- nuevo chat
app.new_chat()
check("nuevo chat limpia la pantalla y el historial", app.messages == [] and chat_text().strip() == "")
hist = os.listdir(cfg.history_dir)
check("la conversación anterior quedó guardada", len(hist) >= 1, str(hist))

root.destroy()
print()
print(f"Capturas en: {SHOTS}")
print("TODO OK" if not fallas else f"FALLARON {len(fallas)}: {fallas}")
sys.exit(1 if fallas else 0)
