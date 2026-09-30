"""chatview: render de una sesión guardada, plegado, streaming y borrado del paso en curso."""
import json
import tkinter as tk

import chatview
import dsapi

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


TEMA = dict(bg="#fff", panel="#fff", fg="#000", muted="#666", user="#06c", bot="#080", code_bg="#eee",
            reason="#888", accent="#06c", accent_fg="#fff", err="#c00", border="#ccc", final="#0aa")

# ---- funciones puras
msg = dsapi.build_message("Hola mundo", [__file__, __file__])
txt, files = chatview.split_user_content(msg)
check("split: texto y dos adjuntos", txt == "Hola mundo" and files == ["test_chatview.py"] * 2, (txt, files))
check("split: sin adjuntos", chatview.split_user_content("solo texto") == ("solo texto", []))
txt, files = chatview.split_user_content(dsapi.build_message("", [__file__]))
check("split: adjunto sin texto", txt == "" and files == ["test_chatview.py"], (txt, files))
check("summarize: run_command", chatview.summarize_call("run_command", {"command": "dir"}) == "$ dir")
check("summarize: JSON roto", "ilegibles" in chatview.summarize_call("read_file", "{roto"))
check("problema: ERROR/DENIED/BLOCKED", all(chatview.result_is_problem(x) for x in ("ERROR: x", "DENIED: y", "BLOCKED: z"))
      and not chatview.result_is_problem("ok"))

# ---- render de una sesión
copiado = []
root = tk.Tk()
cv = chatview.ChatView(root, copiado.append)
cv.frame.pack(fill="both", expand=True)
cv.apply_theme(TEMA, 10)
check("CONTROL: un tema sin chat_bg usa su bg", cv.text.cget("bg") == "#fff", cv.text.cget("bg"))
cv.apply_theme(dict(TEMA, chat_bg="#000000"), 10)
check("el fondo del chat es chat_bg (negro en el tema oscuro)", cv.text.cget("bg") == "#000000", cv.text.cget("bg"))
import theme          # noqa: E402
check("el tema oscuro trae el chat en negro", theme.THEMES["oscuro"]["chat_bg"] == "#000000")
cv.apply_theme(TEMA, 10)

historial = [
    {"role": "user", "content": msg},
    {"role": "assistant", "content": "Voy a mirar.", "_reasoning": "PENSAMIENTO_SECRETO_UNO",
     "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
                    {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{roto"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "3 entradas\nDETALLE_LINEA_A\nDETALLE_LINEA_B"},
    {"role": "tool", "tool_call_id": "c2", "content": "ERROR: argumentos inválidos"},
    {"role": "assistant", "content": "Listo:\n```python\nprint('hola')\n```\nFin **negrita** y `codigo`."},
]
cv.render_session(historial, "DeepSeek")
root.update()
t = cv.get_text()
tag = cv.text.tag_cget


def oculto(fragmento):
    """True si el fragmento está dentro de un rango con elide (comprobado con el widget, no con el texto)."""
    i = cv.text.search(fragmento, "1.0", elide=True)   # sin elide=True, search ignora justamente lo oculto
    if not i:
        return None
    cuerpos = [g for g in cv.text.tag_names(i) if g[:2] in ("rb", "xb")]
    return any(int(cv.text.tag_cget(g, "elide") or 0) for g in cuerpos)


check("aparece el texto del usuario y su adjunto", "Hola mundo" in t and "📎 test_chatview.py" in t, t)
check("el adjunto embebido NO se muestra como bloque de código", "import json" not in t, t[:300])
check("encabezado del asistente una sola vez", t.count("DeepSeek") == 1, t.count("DeepSeek"))
check("llamada a list_dir resumida", "› list_dir  ." in t, t)
check("llamada con JSON roto no revienta", "argumentos ilegibles" in t)
check("primera línea del resultado visible", "↳ 3 entradas" in t and "↳ ERROR: argumentos inválidos" in t)
check("razonamiento presente en el widget", "PENSAMIENTO_SECRETO_UNO" in t)
check("razonamiento plegado por defecto", oculto("PENSAMIENTO_SECRETO_UNO") is True, oculto("PENSAMIENTO_SECRETO_UNO"))
check("detalle de la herramienta plegado por defecto", oculto("DETALLE_LINEA_A") is True)
check("control: 'Voy a mirar.' NO está plegado", oculto("Voy a mirar.") is False, oculto("Voy a mirar."))
check("control: el error NO está plegado", oculto("↳ ERROR") is False)
check("código sin las cercas ```", "```" not in t and "print('hola')" in t, t)
check("negrita e inline sin asteriscos ni comillas", "**" not in t and "Fin negrita y codigo." in t, t)
check("botón de copiar creado", len(cv.text.window_names()) == 1, cv.text.window_names())

# clic en el encabezado del razonamiento despliega
idx = cv.text.search("Razonamiento (", "1.0")
tg = [g for g in cv.text.tag_names(idx) if g[:2] == "rt" and g[2:].isdigit()][0]
script = cv.text.tk.call(cv.text._w, "tag", "bind", tg, "<Button-1>")
check("el encabezado tiene manejador de clic", bool(script))
bd = "rb" + tg[2:]
antes = int(cv.text.tag_cget(bd, "elide"))
cv.text.see(idx)
root.update()
x, y, w, h = cv.text.bbox(idx)


def clic():
    # el tag responde según la marca "current", que solo se actualiza con movimiento del mouse
    cv.text.event_generate("<Motion>", x=x + 3, y=y + h // 2)
    root.update()
    cv.text.event_generate("<Button-1>", x=x + 3, y=y + h // 2)
    root.update()


clic()
check("el clic alterna elide", int(cv.text.tag_cget(bd, "elide")) != antes, (antes, cv.text.tag_cget(bd, "elide")))
clic()
check("y un segundo clic lo devuelve", int(cv.text.tag_cget(bd, "elide")) == antes)

# ---- la conclusión (paso sin herramientas) lleva su color; el resto del trabajo, no
def color_en(fragmento):
    i = cv.text.search(fragmento, "1.0", elide=True)
    return i, [g for g in cv.text.tag_names(i) if g == "final"] if i else None


i, tg = color_en("Fin ")
check("EFECTO: el texto del mensaje final (sin herramientas) tiene el tag de conclusión", tg == ["final"], tg)
check("y ese tag pinta en el color del tema", cv.text.tag_cget("final", "foreground") == TEMA["final"], cv.text.tag_cget("final", "foreground"))
i, tg = color_en("negrita")
check("la negrita dentro de la conclusión conserva el color", "final" in cv.text.tag_names(i) and "bold" in cv.text.tag_names(i), cv.text.tag_names(i))
i, tg = color_en("Voy a mirar.")
check("CONTROL: la narración de un paso CON herramientas NO es conclusión", tg == [], tg)
i, tg = color_en("PENSAMIENTO_SECRETO_UNO")
check("CONTROL: el razonamiento tampoco", tg == [], tg)
i, tg = color_en("list_dir")
check("CONTROL: el log de herramientas tampoco", tg == [], tg)
cv.clear()
cv.assistant_body("", "en vivo, sin herramientas", final=True)
check("en vivo (assistant_body con final=True) también se pinta", "final" in cv.text.tag_names(cv.text.search("en vivo", "1.0")))
cv.clear()
cv.assistant_body("", "narración intermedia")
check("CONTROL: sin final=True no se pinta", "final" not in cv.text.tag_names(cv.text.search("narración", "1.0")))

# ---- streaming: el paso en curso se borra sin comerse el encabezado
cv.clear()
cv.assistant_header("DeepSeek")
cv.begin_stream()
cv.stream_piece("reasoning", "pensando...")
cv.stream_piece("content", "respuesta parcial")
check("streaming visible", "respuesta parcial" in cv.get_text())
cv.clear_stream()
t = cv.get_text()
check("clear_stream borra lo del paso", "respuesta parcial" not in t and "pensando" not in t, t)
check("clear_stream conserva el encabezado en su propia línea", t.strip() == "DeepSeek", repr(t))

# sin un paso abierto, clear_stream NO debe tocar nada (la marca 'as' vieja borraría contenido definitivo)
cv.clear()
cv.assistant_header("DeepSeek")
cv.begin_stream()
cv.stream_piece("content", "paso uno")
cv.clear_stream()
cv.assistant_body("", "RESPUESTA DEFINITIVA")
cv.clear_stream()          # p. ej. un error que llega después de terminar el paso
check("clear_stream sin paso abierto conserva lo definitivo", "RESPUESTA DEFINITIVA" in cv.get_text() and "paso uno" not in cv.get_text(), cv.get_text())

# ---- clear_tagged
cv.note("aviso uno", False, "nk")
cv.note("aviso dos", False, "nk")
cv.note("otro", False)
cv.clear_tagged("nk")
t = cv.get_text()
check("clear_tagged borra solo lo marcado", "aviso" not in t and "otro" in t, t)

# ---- notas y errores llevan el tag "aviso" (lo que borra «Limpiar avisos»)
cv.clear()
cv.user("mi pregunta", [])
cv.put("📎 adjunto.txt\n", "note")             # una línea de adjunto usa el mismo estilo pero NO es un aviso
cv.note("una nota")
cv.note("✖ un error", error=True)
cv.assistant_body("", "respuesta del modelo")
cv.clear_tagged("aviso")
t = cv.get_text()
check("clear_tagged('aviso') borra notas y errores", "una nota" not in t and "un error" not in t, t)
check("CONTROL: no borra pregunta, respuesta ni la línea de adjunto", "mi pregunta" in t and "respuesta del modelo" in t and "adjunto.txt" in t, t)

# ---- copiar
cv.clear()
cv.markdown("```js\nlet a = 1\n```")
root.update()
win = cv.text.window_names()[0]
cv.text.nametowidget(win).invoke()
check("botón copiar entrega el código exacto", copiado == ["let a = 1"], copiado)

# ---- quién contestó cada respuesta
mixto = [
    {"role": "user", "content": "uno"},
    {"role": "assistant", "content": "respuesta vieja"},
    {"role": "user", "content": "dos"},
    {"role": "assistant", "content": "de la nube", "_model": "DeepSeek"},
    {"role": "user", "content": "tres"},
    {"role": "assistant", "content": "paso 1", "_model": "Qwen-X", "tool_calls": [{"id": "k", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "k", "content": "ok"},
    {"role": "assistant", "content": "paso 2", "_model": "Qwen-X"},
]
cv.render_session(mixto)
t = cv.get_text()
check("cada respuesta lleva el nombre de quien la escribió", t.index("DeepSeek") < t.index("de la nube") and t.index("Qwen-X") < t.index("paso 1"), t)
check("una respuesta vieja sin registro dice Asistente, no el modelo actual", t.index(chatview.UNKNOWN_MODEL) < t.index("respuesta vieja"), t)
check("CONTROL: los pasos seguidos del mismo modelo llevan un solo encabezado", t.count("Qwen-X") == 1, t.count("Qwen-X"))
cv.clear()
cv.assistant_header("DeepSeek")
cv.assistant_header("Qwen-X")
check("en vivo: si cambia el modelo entre respuestas seguidas, sale otro encabezado", "Qwen-X" in cv.get_text(), cv.get_text())

root.destroy()
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
