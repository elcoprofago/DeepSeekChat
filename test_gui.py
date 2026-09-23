"""La ventana completa contra streams falsos (sin red, sin modelo, sin tocar la configuración real).

Uso: python test_gui.py [carpeta_capturas]
"""
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tkinter as tk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import agent as ag
import deepseek_chat as dc
import dsapi
import localmodels
import sessions
import theme

SHOTS = sys.argv[1] if len(sys.argv) > 1 else ""
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)
fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


# ---- red apagada: lo que la app consulta en segundo plano se simula
listados = []
dsapi.list_models = lambda key: (listados.append(key), dsapi.FALLBACK_MODELS)[1]
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}


def call(id_, name, args):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Guion:
    """Cada pedido al 'modelo' consume el siguiente paso: lista de eventos, una excepción, o una función generadora."""

    def __init__(self):
        self.pasos, self.vistos = [], []

    def __call__(self, entry, msgs, tools):
        self.vistos.append((entry["id"], list(msgs), tools))
        return Flujo(self.pasos.pop(0))


class Flujo:
    def __init__(self, paso):
        self.paso, self.cancelado = paso, False

    def cancel(self):
        self.cancelado = True

    def __iter__(self):
        p = self.paso
        if isinstance(p, Exception):
            raise p
        if callable(p):
            yield from p(self)
            return
        yield from p


def respuesta(texto, razon=""):
    ev = []
    if razon:
        ev.append(("reasoning", razon))
    ev += [("content", texto), ("usage", {"prompt_tokens": 100, "completion_tokens": 20,
                                          "completion_tokens_details": {"reasoning_tokens": 5}}), ("finish", "stop")]
    return ev


def con_llamada(*calls):
    return [("tool_calls", list(calls)), ("usage", {"prompt_tokens": 80, "completion_tokens": 10}), ("finish", "tool_calls")]


tmp = tempfile.mkdtemp(prefix="dschat_gui_")
cfg = dsapi.Config(os.path.join(tmp, "cfg"))
ws = os.path.join(tmp, "proyecto ñ")
os.makedirs(os.path.join(ws, "src"))
open(os.path.join(ws, "a.txt"), "w", encoding="utf-8", newline="").write("uno dos tres\n")
open(os.path.join(ws, "CONTROL.txt"), "w", encoding="utf-8", newline="").write("intacto\n")
open(os.path.join(ws, "src", "m.py"), "w", encoding="utf-8").write("print('hola')\n")

root = tk.Tk()
guion = Guion()
app = dc.App(root, cfg, stream_factory=guion, interactive=False)


def pump(cond=lambda: False, t=8.0):
    """Procesa eventos hasta que cond() sea cierta (o pase el tiempo)."""
    t0 = time.time()
    while time.time() - t0 < t:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def terminar(decision=None, t=8.0):
    """Espera a que el agente termine; si aparece un pedido de permiso lo contesta con decision."""
    def listo():
        if decision and app._dialog is not None and app._dialog.win.winfo_exists():
            app._dialog._finish(decision)
        return not app.busy and app.worker is not None and not app.worker.is_alive()
    ok = pump(listo, t)
    root.update()
    return ok


def shot(nombre):
    if not SHOTS:
        return
    try:
        from PIL import ImageGrab
        root.update()
        x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(SHOTS, nombre))
    except Exception as e:      # noqa: BLE001
        print("(sin captura:", e, ")")


pump(t=0.3)

# ---------------------------------------------------------------- arranque sin key (el bug 4 del usuario)
check("sin key: aviso gris visible", "Falta la API key" in app.chat.get_text(), app.chat.get_text()[-200:])
cfg.set_session_key("sk-de-prueba")
app.key_changed()
pump(lambda: listados and "US$ 4.20" in app.balance_lbl.cget("text"), 3)
check("cargar la key: el aviso desaparece del panel", "Falta la API key" not in app.chat.get_text(), app.chat.get_text()[-200:])
check("cargar la key: se listan modelos y se pide saldo", listados == ["sk-de-prueba"] and "US$ 4.20" in app.balance_lbl.cget("text"), (listados, app.balance_lbl.cget("text")))
check("premisa: sesión inicial vacía, sin carpeta, sin herramientas", not app.sess.messages and not app.sess.workspace and app.toolbox is None)

# ---------------------------------------------------------------- sin carpeta: no hay herramientas
guion.pasos = [respuesta("Hola, ¿en qué te ayudo?", "pienso un poco")]
app.send("hola")
check("enviando: botón dice Detener", app.busy and app.btn_send.cget("text") == "Detener")
check("el agente termina", terminar())
sid_libre = app.sess.id
_, msgs, tools = guion.vistos[-1]
check("sin carpeta: el modelo no recibe herramientas", tools is None)
check("sin carpeta: el prompt de sistema le dice que abra una carpeta", "Abrir carpeta" in msgs[0]["content"], msgs[0]["content"][:80])
check("respuesta en pantalla", "en qué te ayudo" in app.chat.get_text())
check("sesión guardada en disco, bien formada", os.path.isfile(os.path.join(cfg.sessions_dir, sid_libre + ".json")) and not ag.validate(app.sess.messages))
check("tokens acumulados", app.sess.totals["in"] == 100 and app.sess.totals["out"] == 20 and app.sess.totals["reason"] == 5, app.sess.totals)
check("botón vuelve a Enviar", app.btn_send.cget("text") == "Enviar" and not app.busy)
check("título automático desde el primer mensaje", app.sess.title == "hola", app.sess.title)

# ---------------------------------------------------------------- Limpiar avisos + barra de carpeta siempre visible
app.chat.note("✖ un error viejo que no sirve", error=True)
app.chat.note("un aviso cualquiera")
app._set_state("estado de prueba", error=True)
antes = app.chat.get_text()
app.btn_clear.invoke()
despues = app.chat.get_text()
check("premisa: los avisos estaban en pantalla", "un error viejo" in antes and "un aviso cualquiera" in antes)
check("EFECTO: Limpiar avisos borra errores y avisos de la vista", "un error viejo" not in despues and "un aviso cualquiera" not in despues, despues[-200:])
check("Limpiar avisos borra también el estado de la barra", app.state_lbl.cget("text") == "", app.state_lbl.cget("text"))
check("CONTROL: Limpiar avisos deja la conversación (mensaje y respuesta)", "hola" in despues and "en qué te ayudo" in despues, despues[-300:])
check("CONTROL: el historial de la sesión no se tocó", len(app.sess.messages) >= 2 and not ag.validate(app.sess.messages))
app.root.update()
check("la barra de carpeta está a la vista, con el botón Abrir carpeta…", app.btn_folder.winfo_viewable() and app.btn_folder.winfo_height() > 1 and "Abrir carpeta" in app.btn_folder.cget("text"))
check("sin carpeta: la barra lo dice", "Sin carpeta" in app.folder_lbl.cget("text"), app.folder_lbl.cget("text"))
elegida = os.path.join(tmp, "elegida_con_boton")
os.makedirs(elegida)
_askdir = dc.filedialog.askdirectory
dc.filedialog.askdirectory = lambda **kw: elegida
try:
    app.btn_folder.invoke()          # el botón real, no la función
finally:
    dc.filedialog.askdirectory = _askdir
check("EFECTO: el botón abre la carpeta elegida (sesión nueva: la actual tiene historia)", app.sess.workspace == elegida and app.toolbox is not None and app.sess.id != sid_libre, app.sess.workspace)
check("con carpeta: la barra muestra la ruta", elegida in app.folder_lbl.cget("text"), app.folder_lbl.cget("text"))
app._activate(app.store.load(sid_libre))
check("al volver a la sesión sin carpeta, la barra vuelve a avisarlo", "Sin carpeta" in app.folder_lbl.cget("text"), app.folder_lbl.cget("text"))

# ---------------------------------------------------------------- carpeta: sesión con historia => sesión nueva
app.use_folder(ws)
check("carpeta en sesión con historia: crea sesión nueva", app.sess.id != sid_libre and app.sess.workspace == ws and not app.sess.messages)
check("la sesión anterior sigue en la lista", sid_libre in app.sess_tree.get_children() and app.sess.id in app.sess_tree.get_children())
nombres = [app.explorer.tree.item(i, "text").strip() for i in app.explorer.tree.get_children()]
check("explorador muestra la carpeta", app.explorer.root_path == os.path.abspath(ws) and "src" in nombres and "a.txt" in nombres, nombres)
check("hay herramientas", app.toolbox is not None)
sid_a = app.sess.id
otra = os.path.join(tmp, "otra")
os.makedirs(otra)
app.use_folder(otra)
check("carpeta en sesión vacía: se cambia en la misma sesión", app.sess.id == sid_a and app.sess.workspace == otra)
app.use_folder(ws)
check("y se puede volver a la primera", app.sess.workspace == ws and app.toolbox.root == os.path.realpath(ws))
app.use_folder(os.path.join(tmp, "no existe"))
check("carpeta inexistente: aviso, no cambia nada", app.sess.workspace == ws and any("No existe" in w for w in app.warnings), app.warnings)

# ---------------------------------------------------------------- agente edita, con permiso
guion.pasos = [
    con_llamada(call("c1", "edit_file", {"path": "a.txt", "old": "uno", "new": "UNO"})),
    respuesta("Listo, cambié a.txt."),
]
app.send("cambiá uno por UNO en a.txt")
pedido = pump(lambda: app._dialog is not None, 5)
check("aparece el pedido de permiso", pedido)
if pedido:
    check("el pedido muestra el diff", "UNO" in app._dialog.view.get("1.0", "end"), app._dialog.view.get("1.0", "end")[:200])
    shot("01_permiso.png")
    check("mientras espera permiso no hay grab (la ventana sigue viva)", app.root.grab_current() is None)
    app._dialog._finish("allow")
check("termina tras permitir", terminar())
leido = open(os.path.join(ws, "a.txt"), encoding="utf-8").read()
check("archivo editado", leido == "UNO dos tres\n", leido)
check("CONTROL intacto", open(os.path.join(ws, "CONTROL.txt"), encoding="utf-8").read() == "intacto\n")
check("respuesta final en pantalla", "Listo, cambié" in app.chat.get_text())
check("sesión guardada bien formada, con el diario", not ag.validate(app.sess.messages) and len(app.sess.journal) == 1)
disco = sessions.SessionStore(cfg.sessions_dir).load(sid_a)
check("lo guardado en disco coincide con memoria", disco is not None and disco.messages == app.sess.messages and disco.journal == app.sess.journal)
check("el prompt de sistema nombra la carpeta", os.path.realpath(ws) in guion.vistos[-1][1][0]["content"], guion.vistos[-1][1][0]["content"][:120])
check("con carpeta el modelo recibe herramientas", guion.vistos[-1][2] is not None)
shot("02_tras_edicion.png")

# ---------------------------------------------------------------- deshacer
app.undo()
check("deshacer restaura el archivo", open(os.path.join(ws, "a.txt"), encoding="utf-8").read() == "uno dos tres\n")
check("deshacer deja el aviso y vacía el diario", "↶" in app.chat.get_text() and app.sess.journal == [])
app.undo()
check("deshacer sin nada pendiente no rompe", "No hay cambios" in app.chat.get_text())

# ---------------------------------------------------------------- permiso denegado
guion.pasos = [
    con_llamada(call("c2", "write_file", {"path": "nuevo.txt", "content": "x"})),
    respuesta("No pude crear el archivo porque lo denegaste."),
]
app.send("creá nuevo.txt")
check("denegar: termina", terminar("deny"))
check("denegar: el archivo no existe", not os.path.exists(os.path.join(ws, "nuevo.txt")))
check("denegar: el modelo ve el rechazo en el resultado", any(m.get("role") == "tool" and "den" in m["content"].lower() for m in app.sess.messages[-3:]), [m.get("content") for m in app.sess.messages[-3:]])

# ---------------------------------------------------------------- 'permitir todas las ediciones' cambia el modo
guion.pasos = [
    con_llamada(call("c3", "edit_file", {"path": "a.txt", "old": "dos", "new": "DOS"})),
    con_llamada(call("c4", "edit_file", {"path": "a.txt", "old": "tres", "new": "TRES"})),
    respuesta("Hecho."),
]
app.send("editá dos y tres")
pump(lambda: app._dialog is not None, 5)
if app._dialog is not None:
    app._dialog._finish("allow_session")
check("termina con una sola pregunta", terminar())
check("las dos ediciones se hicieron", open(os.path.join(ws, "a.txt"), encoding="utf-8").read() == "uno DOS TRES\n")
check("el selector pasó a 'Editar sin preguntar' y quedó en la sesión", app.appr_var.get() == dc.APPROVALS["edits"] and app.sess.approval == "edits" and app.toolbox.approval == "edits", (app.appr_var.get(), app.sess.approval))


# ---------------------------------------------------------------- cancelar a mitad de una respuesta
def colgado(flujo):
    yield ("content", "empiezo a responder…")
    t0 = time.time()
    while not flujo.cancelado and time.time() - t0 < 8:
        time.sleep(0.02)


guion.pasos = [colgado]
n_antes = len(app.sess.messages)
app.send("algo largo")
pump(lambda: "empiezo a responder" in app.chat.get_text(), 5)
# mientras trabaja: Enter no corta, no se cambia de sesión ni de modelo
app.input.insert("1.0", "siguiente pregunta")
app._on_enter(None)
check("Enter con el agente trabajando no lo cancela", app.busy and not app.cancel_ev.is_set())
check("y no borra lo escrito", app.input.get("1.0", "end-1c") == "siguiente pregunta")
app.input.delete("1.0", "end")
id_previo, modelo_previo = app.sess.id, app.sess.model
app.new_session()
app.switch_session(sid_libre)
check("no se puede cambiar de sesión mientras trabaja", app.sess.id == id_previo and "Hay un agente trabajando" in app.chat.get_text())
app.model_var.set("deepseek-flash" if modelo_previo != "deepseek-flash" else "deepseek-v4-pro")
app.on_model_change()
check("ni de modelo (y el selector vuelve a mostrar el real)", app.sess.model == modelo_previo and app.model_var.get() == modelo_previo, (app.sess.model, app.model_var.get()))
check("ni borrar la sesión en curso", (app.delete_session(app.sess.id) or True) and os.path.isfile(os.path.join(cfg.sessions_dir, app.sess.id + ".json")))
app.cancel()
check("cancelar: termina rápido", terminar(t=4))
check("cancelar: historial válido con el texto parcial", not ag.validate(app.sess.messages) and app.sess.messages[-1].get("content") == "empiezo a responder…", app.sess.messages[-1:])
check("cancelar: aviso visible", "Interrumpido" in app.chat.get_text())
check("cancelar: la pregunta y la respuesta parcial se conservan", len(app.sess.messages) == n_antes + 2, len(app.sess.messages) - n_antes)

# ---------------------------------------------------------------- error sin respuesta: el texto vuelve al campo
n_antes = len(app.sess.messages)
guion.pasos = [dsapi.ApiError("Sin conexión con DeepSeek.")]
app.send("pregunta que va a fallar")
check("error: termina", terminar())
check("error: el mensaje se quitó del historial", len(app.sess.messages) == n_antes and "pregunta que va a fallar" not in json.dumps(app.sess.messages))
check("error: el texto vuelve al campo de entrada", app.input.get("1.0", "end-1c") == "pregunta que va a fallar", app.input.get("1.0", "end-1c"))
check("error: se muestra, y la pregunta ya no está en pantalla", "Sin conexión" in app.chat.get_text() and "pregunta que va a fallar" not in app.chat.get_text())
app.input.delete("1.0", "end")

guion.pasos = [ZeroDivisionError("boom")]
app.send("otra")
check("excepción inesperada: termina y se informa", terminar() and "Error inesperado" in app.chat.get_text())
app.input.delete("1.0", "end")

# ---------------------------------------------------------------- adjuntos
adj = os.path.join(tmp, "nota ñ.txt")
open(adj, "w", encoding="utf-8").write("contenido adjunto\n")
app.attach_paths([adj, adj])
check("adjunto sin duplicados", app.attachments == [os.path.normpath(adj)])
guion.pasos = [respuesta("Vi el adjunto.")]
app.send("mirá esto")
check("adjunto: termina", terminar())
check("adjunto: el modelo recibió el contenido", "contenido adjunto" in json.dumps(guion.vistos[-1][1], ensure_ascii=False))
check("adjuntos se limpian tras enviar", app.attachments == [])
n_av = len(app.warnings)
app.attach_paths([os.path.join(tmp, "no existe.txt")])
check("adjunto inexistente: avisa y no lo agrega", app.attachments == [] and len(app.warnings) == n_av + 1, app.warnings[-1:])

# ---------------------------------------------------------------- sesiones: lista, cambiar, renombrar, exportar, eliminar
check("la lista de sesiones tiene ambas", {sid_libre, sid_a} <= set(app.sess_tree.get_children()))
app.switch_session(sid_libre)
root.update()
check("cambiar de sesión: se ve su transcripción y no la otra", "en qué te ayudo" in app.chat.get_text() and "Listo, cambié" not in app.chat.get_text())
check("cambiar de sesión: carpeta y explorador cambian", app.sess.workspace == "" and app.explorer.root_path == "" and app.toolbox is None)
check("la selección de la lista sigue a la actual", app.sess_tree.selection() == (sid_libre,), app.sess_tree.selection())
app.switch_session(sid_a)
root.update()
check("y volver restaura todo", "Listo, cambié" in app.chat.get_text() and app.explorer.root_path == os.path.abspath(ws) and app.toolbox is not None)
app.sess_tree.selection_set(sid_libre)      # lo mismo que un clic del usuario: dispara <<TreeviewSelect>>
root.update()
check("clic en la lista cambia la sesión", app.sess.id == sid_libre, app.sess.id)
app.sess_tree.selection_set(sid_a)
root.update()
check("clic de vuelta", app.sess.id == sid_a)

app.rename_session(sid_a, "  Arreglo   de a.txt ")
disco = sessions.SessionStore(cfg.sessions_dir).load(sid_a)
check("renombrar: normaliza, guarda y actualiza lista y título",
      disco.title == "Arreglo de a.txt" and app.sess_tree.set(sid_a, "title") == "Arreglo de a.txt" and root.title() == dc.TITULO_VENTANA, (disco.title, root.title()))

out = os.path.join(tmp, "exp ñ.md")
res = app.write_export(app.sess, out)
md = open(out, encoding="utf-8").read() if res else ""
check("exportar: markdown con título, pregunta y respuesta", res == out and "Arreglo de a.txt" in md and "cambiá uno por UNO" in md and "Listo, cambié" in md, md[:200])
check("exportar a ruta imposible: avisa y no revienta", app.write_export(app.sess, os.path.join(tmp, "no", "hay", "x.md")) is None)

modelo_a, esfuerzo_a = app.sess.model, app.sess.effort
app.new_session()
sid_c = app.sess.id
check("nueva sesión: distinta, vacía, con la misma carpeta y modelo", sid_c != sid_a and not app.sess.messages and app.sess.workspace == ws and app.sess.model == modelo_a)
app.new_session()
check("nueva sesión sobre una vacía no apila otra", app.sess.id == sid_c)
app.delete_session(sid_libre)
check("eliminar: sale de la lista y va a la papelera",
      sid_libre not in app.sess_tree.get_children() and os.path.isfile(os.path.join(cfg.sessions_dir, "_papelera", sid_libre + ".json"))
      and not os.path.exists(os.path.join(cfg.sessions_dir, sid_libre + ".json")))
app.switch_session(sid_a)
app.delete_session(sid_a)
check("eliminar la actual: pasa a una en blanco con la misma carpeta", app.sess.id not in (sid_a, sid_libre) and not app.sess.messages and app.sess.workspace == ws)
papelera = os.path.join(cfg.sessions_dir, "_papelera", sid_a + ".json")
check("eliminar: la papelera conserva el contenido", os.path.isfile(papelera) and json.load(open(papelera, encoding="utf-8"))["messages"] != [])

# ---------------------------------------------------------------- modelos: locales en el selector
mdir = os.path.join(tmp, "mis modelos")
os.makedirs(mdir)
with open(os.path.join(mdir, "Qwen-de-mentira.gguf"), "wb") as f:
    f.truncate(3 * 1024 * 1024)
cfg["model_dirs"] = [mdir]
encontrados = app.rescan_models()
check("el escaneo encuentra el modelo local", any(m["name"] == "Qwen-de-mentira" for m in encontrados), encontrados)
vals = list(app.model_cb.cget("values"))
loc = [v for v in vals if v.startswith("[local] Qwen-de-mentira")]
check("el selector lista DeepSeek y el local", "deepseek-v4-pro" in vals and "deepseek-flash" in vals and len(loc) == 1, vals)
app.model_var.set(loc[0])
app.on_model_change()
check("elegir el local: queda en la sesión y en la config", app.sess.model.startswith(localmodels.MODEL_ID_PREFIX) and cfg["model"] == app.sess.model)
check("local: effort desactivado y saldo n/a", str(app.effort_cb.cget("state")) == "disabled" and "n/a" in app.balance_lbl.cget("text"))
guion.pasos = [respuesta("respondo desde el local")]
app.send("probá el local")
check("local vía la fábrica de pruebas: termina", terminar())
check("local: el modelo recibe el prompt de agente con la carpeta", guion.vistos[-1][0].startswith(localmodels.MODEL_ID_PREFIX) and guion.vistos[-1][2] is not None)
check("local: el nombre en la transcripción es el del modelo", "Qwen-de-mentira" in app.chat.get_text())
app.model_var.set("deepseek-flash")
app.on_model_change()
check("volver a DeepSeek: effort activo y saldo real", str(app.effort_cb.cget("state")) != "disabled" and "US$" in app.balance_lbl.cget("text"))
app.effort_var.set("high")
app.on_effort_change()
check("effort se guarda en la sesión", app.sess.effort == "high")
disco = sessions.SessionStore(cfg.sessions_dir).load(app.sess.id)
check("y en disco (la sesión tiene mensajes)", disco.effort == "high" and disco.model == "deepseek-flash", (disco.effort, disco.model))

# modelo local que ya no está (pendrive desconectado): la sesión no se pierde ni se cae
app.sess.model = localmodels.MODEL_ID_PREFIX + os.path.join(tmp, "pendrive_desconectado", "x.gguf")
app._refresh_model_widgets()
check("modelo local ausente: se muestra como no encontrado", "no encontrado" in app.model_var.get(), app.model_var.get())
guion_real, app._stream_factory = app._stream_factory, None       # sin la fábrica de pruebas: camino real, que falla antes de tocar la red
app.send("¿hay alguien?")
check("modelo local ausente: error claro que menciona el pendrive", terminar() and "pendrive" in app.chat.get_text(), app.chat.get_text()[-300:])
check("modelo local ausente: el texto vuelve al campo", app.input.get("1.0", "end-1c") == "¿hay alguien?")
app.input.delete("1.0", "end")
app._stream_factory = guion_real
app.sess.model = "deepseek-flash"
app._refresh_model_widgets()

# ---------------------------------------------------------------- key: enviar sin key a un modelo de DeepSeek
cfg.set_session_key("")
app.key_changed()
n_msgs = len(app.sess.messages)
app.send("sin key")
check("sin key y modelo remoto: no envía, deja el aviso", not app.busy and len(app.sess.messages) == n_msgs and "Falta la API key" in app.chat.get_text())
cfg.set_session_key("sk-de-prueba")
app.key_changed()

# ---------------------------------------------------------------- permisos: selector
app.appr_var.set(dc.APPROVALS["all"])
app.on_approval_change()
check("'Todo sin preguntar' (no interactivo): pasa a la sesión y a las herramientas", app.sess.approval == "all" and app.toolbox.approval == "all")
app.appr_var.set(dc.APPROVALS["ask"])
app.on_approval_change()
check("volver a 'Preguntar todo'", app.sess.approval == "ask" and app.toolbox.approval == "ask")

# ---------------------------------------------------------------- barra lateral
app.toggle_sidebar()
root.update()
check("boton de menu oculta la barra lateral", str(app.side) not in app.main.panes() and not app.sidebar_visible)
app.toggle_sidebar()
pump(t=0.3)
check("boton de menu la vuelve a mostrar", str(app.side) in app.main.panes() and app.sidebar_visible)
shot("03_final.png")

# ---------------------------------------------------------------- título, medidor de tokens, tema oscuro
check("título de la ventana como el de USBagent (y fijo aunque cambie la sesión)",
      dc.TITULO_VENTANA == "DeepSeek Chat - \u00a9 R.A. Sistemas - 2026" and root.title() == dc.TITULO_VENTANA, root.title())
check("el tema por defecto es el oscuro azul", dsapi.Config.DEFAULTS["theme"] == "oscuro"
      and all(k in theme.THEMES[n] for n in theme.THEMES for k in ("meter_fill", "accent_text")))


def texto_canvas(cv):
    return " ".join(str(cv.itemcget(i, "text")) for i in cv.find_all() if cv.type(i) == "text")


app.meter.reset()
app._draw_meter()
check("medidor en reposo: 'Listo' y consumo 0", "Listo" in texto_canvas(app.cv_state) and "Jornada: 0 tok" in texto_canvas(app.cv_bar),
      (texto_canvas(app.cv_state), texto_canvas(app.cv_bar)))
app._handle("step_begin")
for _ in range(3):
    app._handle("content", "x" * 70)
    time.sleep(0.25)
app._draw_meter()
check("medidor generando: muestra velocidad estimada con '~' y total estimado",
      "Generando" in texto_canvas(app.cv_state) and "~" in texto_canvas(app.cv_state) and "tok/s" in texto_canvas(app.cv_state)
      and "Jornada: ~" in texto_canvas(app.cv_bar), (texto_canvas(app.cv_state), texto_canvas(app.cv_bar)))
app._handle("usage", {"prompt_tokens": 4000, "completion_tokens": 60})
app._handle("step_end", {"content": "x", "reasoning": "", "finish": "stop", "calls": []})
app._draw_meter()
check("con el usage exacto: total 4.060 sin '~' y la barra tiene relleno",
      "Jornada: 4.060 tok" in texto_canvas(app.cv_bar).replace(",", ".") and "~" not in texto_canvas(app.cv_bar)
      and any(app.cv_bar.type(i) == "rectangle" for i in app.cv_bar.find_all()), texto_canvas(app.cv_bar))
app._meter_reset()
check("reiniciar el contador lo pone en cero", app.meter.total() == 0 and "Jornada: 0 tok" in texto_canvas(app.cv_bar))

# ---------------------------------------------------------------- geometría: se recuerda y se corrige si el monitor cambió
V = (0, 0, 1920, 1080)
V2 = (-1920, 0, 3840, 1080)          # un segundo monitor a la izquierda
check("fit_geometry: dentro de la pantalla no se toca", dc.fit_geometry("1000x700+100+50", V) == "1000x700+100+50")
check("fit_geometry: fuera de la pantalla (monitor desconectado) se trae adentro", dc.fit_geometry("1000x700+2500+900", V) == "1000x700+920+380")
check("CONTROL: la misma posición es válida si el monitor extra sigue", dc.fit_geometry("1000x700+-1500+100", V2) == "1000x700+-1500+100")
check("fit_geometry: más grande que la pantalla se recorta", dc.fit_geometry("3000x2000+0+0", V) == "1920x1080+0+0")
check("fit_geometry: tamaño diminuto se sube al mínimo", dc.fit_geometry("100x100+0+0", V) == "860x520+0+0")
check("fit_geometry: basura o vacío -> None", dc.fit_geometry("", V) is None and dc.fit_geometry("hola", V) is None)

# ---------------------------------------------------------------- permisos y effort por defecto para sesiones nuevas
app._set_approval("edits")
check("permiso 'edits' se recuerda para sesiones nuevas", dsapi.Config(os.path.join(tmp, "cfg"))["approval"] == "edits"
      and app._blank_session("").approval == "edits")
app.cfg["approval"] = "all"
check("'all' NUNCA se hereda: sesión nueva vuelve a 'ask'", app._blank_session("").approval == "ask")
app.cfg["approval"] = "ask"

# ---------------------------------------------------------------- cierre: guarda y recuerda
sid_final = app.sess.id
app.on_close()
c2 = dsapi.Config(os.path.join(tmp, "cfg"))
check("al cerrar recuerda geometría, maximizado y barra lateral", re.match(r"^\d+x\d+\+-?\d+\+-?\d+$", c2["win_geometry"]) is not None
      and c2["win_zoomed"] is False and c2["sidebar_visible"] is True, (c2["win_geometry"], c2["win_zoomed"], c2["sidebar_visible"]))
check("al cerrar, la config recuerda la última sesión y el ancho de la barra", c2["last_session"] == sid_final and c2["sidebar_w"] >= 160, (c2["last_session"], c2["sidebar_w"]))

shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
