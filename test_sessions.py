"""Sesiones: guardado atómico, orden, papelera, archivos dañados. Sin red."""
import json
import os
import shutil
import tempfile
import time

import sessions as ss

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:200]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


d = tempfile.mkdtemp(prefix="dschat_ss_")
st = ss.SessionStore(os.path.join(d, "sessions"))

a = ss.Session(workspace="C:\\proyecto ñ", model="deepseek-v4-pro", effort="high", approval="edits")
check("sesión vacía no se escribe", st.save(a) is False and not os.path.exists(st.dir))
a.messages = [{"role": "user", "content": "Arreglá el bug\ndel   login " + "x" * 80},
              {"role": "assistant", "content": "", "_reasoning": "pienso", "tool_calls": [
                  {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "c1", "content": "./\n  a.py"}]
a.journal = [{"path": "C:\\proyecto ñ\\a.py", "backup": "C:\\bk\\x", "when": "2026-09-23 10:00:00"}]
a.totals["in"] = 1234
check("con mensajes se guarda", st.save(a) is True and os.path.isfile(os.path.join(st.dir, a.id + ".json")))
check("título automático: una línea, sin espacios dobles, recortado", a.title.startswith("Arreglá el bug del login") and a.title.endswith("…") and "\n" not in a.title, a.title)
b = st.load(a.id)
check("ida y vuelta conserva todo (mensajes, _reasoning, tool_calls, diario, totales, carpeta con ñ)",
      b.messages == a.messages and b.journal == a.journal and b.totals["in"] == 1234 and b.workspace == "C:\\proyecto ñ"
      and b.model == "deepseek-v4-pro" and b.approval == "edits" and b.title == a.title)
check("no quedan temporales", not [n for n in os.listdir(st.dir) if n.endswith(".tmp")])

a.title = "Mi título manual"
a.messages.append({"role": "assistant", "content": "listo"})
st.save(a)
a2 = st.load(a.id)
check("un título puesto a mano no se pisa al guardar de nuevo", a2.title == "Mi título manual" and len(a2.messages) == 4)

time.sleep(1.1)
c = ss.Session()
c.messages = [{"role": "user", "content": "otra cosa"}]
st.save(c)
check("la lista va de la más reciente a la más vieja", [s.id for s in st.list()] == [c.id, a.id], [s.id for s in st.list()])
check("ids únicos aunque se creen en el mismo segundo", len({ss.Session().id for _ in range(50)}) == 50)

# archivo dañado: se aparta, no se borra, no rompe la lista
bad = os.path.join(st.dir, "20260101-000000-dead.json")
open(bad, "w", encoding="utf-8").write('{"id": "x", "messages": [')
notjson = os.path.join(st.dir, "20260101-000001-beef.json")
json.dump({"hola": 1}, open(notjson, "w"))
lst = st.list()
check("archivos dañados no rompen la lista", [s.id for s in lst] == [c.id, a.id], [s.id for s in lst])
check("quedaron apartados con .dañado (nada se pierde)", os.path.exists(bad + ".dañado") and os.path.exists(notjson + ".dañado") and not os.path.exists(bad))
check("se avisó de cada uno", len(st.warnings) == 2, st.warnings)

# papelera
dst = st.delete(a.id)
check("borrar mueve a la papelera (el contenido sigue ahí)", dst and os.path.isfile(dst) and json.load(open(dst, encoding="utf-8"))["title"] == "Mi título manual")
check("ya no aparece en la lista, la otra sí (control)", [s.id for s in st.list()] == [c.id])
check("borrar algo que no existe devuelve None", st.delete("20250101-000000-zzzz") is None)
a3 = ss.Session()
a3.id = a.id          # mismo id de nuevo a la papelera: no debe pisar el anterior
a3.messages = [{"role": "user", "content": "segunda vida"}]
st.save(a3)
dst2 = st.delete(a3.id)
check("dos borrados del mismo id no se pisan en la papelera", dst2 != dst and len(os.listdir(st.trash)) == 2, os.listdir(st.trash))

# ids peligrosos
for x in ("..\\..\\config", "../x", "a:b", "", ".oculto"):
    try:
        st.load(x)
        ok = False
    except ValueError:
        ok = True
    check(f"id peligroso {x!r} rechazado", ok)

# JSON con campos faltantes / extra: tolera
p = os.path.join(st.dir, "20260202-000000-mini.json")
json.dump({"id": "20260202-000000-mini", "messages": [{"role": "user", "content": "x"}], "campo_futuro": 1}, open(p, "w"))
m = st.load("20260202-000000-mini")
check("sesión con menos campos: usa valores por defecto; campos desconocidos se ignoran", m.model == "" and m.approval == "ask" and m.totals["hit"] == 0 and not hasattr(m, "campo_futuro"))

shutil.rmtree(d, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
