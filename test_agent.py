"""Prueba del bucle del agente con streams falsos (sin red ni modelo): protocolo, cancelación, bucles, compactación."""
import copy
import json
import os
import tempfile
import threading

import agent
import agent_tools as at
import dsapi

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:200]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


class Fake:
    """Un stream falso: cada paso del guion es una lista de eventos (o una excepción a lanzar)."""

    def __init__(self, script):
        self.script, self.i, self.seen, self.cancelled = script, 0, [], False

    def factory(self, msgs, tools):
        self.seen.append((copy.deepcopy(msgs), tools))
        step = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return _S(step, self)


class _S:
    def __init__(self, evs, owner):
        self.evs, self.owner = evs, owner

    def cancel(self):
        self.owner.cancelled = True

    def __iter__(self):
        if isinstance(self.evs, Exception):
            raise self.evs
        for e in self.evs:
            if callable(e):
                e()       # gancho: permite cancelar "a mitad de la respuesta"
            else:
                yield e


def call(id_, name, args):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": args if isinstance(args, str) else __import__("json").dumps(args)}}


base = tempfile.mkdtemp(prefix="dschat_agent_")
ws = os.path.join(base, "p")
os.makedirs(ws)
open(os.path.join(ws, "a.txt"), "w").write("uno dos tres\n")
open(os.path.join(ws, "CONTROL.txt"), "w").write("intacto\n")
tb = at.ToolBox(ws, confirm=lambda *a: True, approval="all", backup_dir=os.path.join(base, "bk"))


def run(script, msgs=None, toolbox=tb, cancel=None, **kw):
    f = Fake(script)
    ag = agent.Agent(f.factory, toolbox, **kw)
    ev = []
    m = msgs if msgs is not None else [{"role": "user", "content": "hola"}]
    st = ag.run(m, lambda k, *a: ev.append((k, *a)), cancel or threading.Event())
    return st, m, ev, f


# ---- 1. respuesta directa
st, m, ev, f = run([[("content", "Ho"), ("content", "la"), ("finish", "stop")]])
check("respuesta sin herramientas termina en 'done'", st == "done" and m[-1] == {"role": "assistant", "content": "Hola"}, m)
check("se emitieron los trozos en orden", [e[1] for e in ev if e[0] == "content"] == ["Ho", "la"])
check("las herramientas se ofrecieron al modelo", f.seen[0][1] == at.SPECS)

# ---- 2. una herramienta y luego respuesta
st, m, ev, f = run([
    [("reasoning", "pienso"), ("tool_calls", [call("c1", "read_file", {"path": "a.txt"})]), ("finish", "tool_calls")],
    [("content", "Tiene tres palabras."), ("finish", "stop")]])
check("ciclo herramienta -> respuesta", st == "done" and m[-1]["content"] == "Tiene tres palabras.", m)
check("estructura: user, assistant(tool_calls), tool, assistant", [x["role"] for x in m] == ["user", "assistant", "tool", "assistant"], [x["role"] for x in m])
check("el resultado lleva el contenido real del archivo", "uno dos tres" in m[2]["content"] and m[2]["tool_call_id"] == "c1", m[2])
check("el protocolo queda bien formado", agent.validate(m) == [], agent.validate(m))
check("el razonamiento se guarda como campo privado", m[1].get("_reasoning") == "pienso")
segunda = f.seen[1][0]
check("a la API no viaja el campo privado _reasoning", all(not any(k.startswith("_") for k in x) for x in segunda), segunda)
check("a la API sí viaja la llamada y su resultado", segunda[1].get("tool_calls") and segunda[2]["role"] == "tool")
check("eventos de herramienta emitidos", [e[0] for e in ev if e[0].startswith("tool_")] == ["tool_calls", "tool_start", "tool_result"], [e[0] for e in ev])

# ---- 3. varias llamadas en un paso
st, m, ev, f = run([
    [("tool_calls", [call("a", "list_dir", {}), call("b", "read_file", {"path": "a.txt"}), call("c", "search", {"pattern": "dos"})]), ("finish", "tool_calls")],
    [("content", "ok"), ("finish", "stop")]])
check("tres llamadas -> tres resultados en orden", [x.get("tool_call_id") for x in m if x["role"] == "tool"] == ["a", "b", "c"] and agent.validate(m) == [])

# ---- 4. argumentos rotos, herramienta inexistente
st, m, ev, f = run([
    [("tool_calls", [call("x", "read_file", '{"path": "a.t')]), ("finish", "length")],
    [("tool_calls", [call("y", "hackear", {})]), ("finish", "tool_calls")],
    [("content", "listo"), ("finish", "stop")]])
res = [x["content"] for x in m if x["role"] == "tool"]
check("JSON cortado: el modelo recibe el error y el bucle sigue", st == "done" and "not valid JSON" in res[0], res)
check("herramienta inexistente: error que lista las válidas", "unknown tool" in res[1] and "read_file" in res[1], res)
check("protocolo bien formado tras errores", agent.validate(m) == [])

# ---- 5. sin carpeta de trabajo (chat puro)
st, m, ev, f = run([[("tool_calls", [call("z", "read_file", {"path": "a.txt"})]), ("finish", "tool_calls")], [("content", "ok"), ("finish", "stop")]], toolbox=None)
check("sin caja de herramientas: no se ofrecen y una llamada colada se contesta con error", f.seen[0][1] is None and "not available" in m[2]["content"] and agent.validate(m) == [], m)

# ---- 6. cancelar mientras se ejecutan las herramientas
ev_c = threading.Event()
script = [[("tool_calls", [call("p", "read_file", {"path": "a.txt"}), call("q", "read_file", {"path": "a.txt"}), call("r", "list_dir", {})]), ("finish", "tool_calls")]]
f6 = Fake(script)
ag6 = agent.Agent(f6.factory, tb)
m6 = [{"role": "user", "content": "x"}]
n = {"v": 0}
orig = tb.execute


def cancelando(name, args, cancel=None):
    n["v"] += 1
    r = orig(name, args, cancel)
    if n["v"] == 1:
        ev_c.set()      # el usuario aprieta Detener tras la primera herramienta
    return r


tb.execute = cancelando
st = ag6.run(m6, lambda *a: None, ev_c)
tb.execute = orig
check("cancelar entre herramientas -> 'cancelled'", st == "cancelled")
check("las llamadas pendientes se contestan igual (la API lo exige)", agent.validate(m6) == [] and len([x for x in m6 if x["role"] == "tool"]) == 3, agent.validate(m6))
check("las pendientes dicen que se cancelaron, no fingen resultados", "cancelled" in m6[-1]["content"] and "cancelled" in m6[-2]["content"], m6[-2:])
check("la primera sí se ejecutó", "uno dos tres" in m6[2]["content"])

# ---- 7. cancelar a mitad de la respuesta: se conserva el texto parcial, se descartan llamadas a medias
ev7 = threading.Event()
st, m, ev, f = run([[("content", "Parte "), ev7.set, ("content", "dos"), ("tool_calls", [call("k", "list_dir", {})]), ("finish", "tool_calls")]], cancel=ev7)
check("cancelar a mitad: 'cancelled' y conserva lo parcial", st == "cancelled" and m[-1]["role"] == "assistant" and m[-1]["content"].startswith("Parte"), m)
check("cancelar a mitad: no deja llamadas sin contestar", agent.validate(m) == [] and "tool_calls" not in m[-1])

# ---- 8. error de red a mitad de la tarea: lo hecho queda bien formado
try:
    m8 = [{"role": "user", "content": "x"}]
    f8 = Fake([[("tool_calls", [call("e", "list_dir", {})]), ("finish", "tool_calls")], dsapi.ApiError("Sin conexión")])
    agent.Agent(f8.factory, tb).run(m8, lambda *a: None, threading.Event())
    lanzo = False
except dsapi.ApiError:
    lanzo = True
check("el error de red se propaga al que llama", lanzo)
check("tras el error, los mensajes siguen bien formados y conservan lo hecho", agent.validate(m8) == [] and m8[-1]["role"] == "tool", [x["role"] for x in m8])

# ---- 9. máximo de pasos
distintos = [[("tool_calls", [call(f"m{i}", "search", {"pattern": f"x{i}"})]), ("finish", "tool_calls")] for i in range(10)]
st, m, ev, f = run(distintos, max_steps=4)
check("máximo de pasos: se detiene con aviso", st == "max_steps" and any(e[0] == "notice" and "máximo" in e[1] for e in ev) and agent.validate(m) == [])
check("no hace más pasos que el máximo", f.i == 4, f.i)

# ---- 10. bucle: la misma llamada una y otra vez
igual = [[("tool_calls", [call(f"l{i}", "read_file", {"path": "a.txt"})]), ("finish", "tool_calls")] for i in range(20)]
st, m, ev, f = run(igual)
res = [x["content"] for x in m if x["role"] == "tool"]
check("bucle detectado: avisa al modelo a la 3.ª y corta a la 5.ª", st == "loop" and "already made this exact call" in res[2] and len(res) == 5, (st, len(res)))
check("protocolo bien formado tras cortar el bucle", agent.validate(m) == [])

# ---- 11. compactación
grande = "x" * 5000
hist = [{"role": "user", "content": "hola"}]
for i in range(6):
    hist += [{"role": "assistant", "content": "", "tool_calls": [call(f"t{i}", "read_file", {"path": "a"})]},
             {"role": "tool", "tool_call_id": f"t{i}", "content": grande}]
orig_copy = copy.deepcopy(hist)
comp = agent.api_messages(hist, budget_chars=12000)
tools_c = [x["content"] for x in comp if x["role"] == "tool"]
check("compactar: los resultados más viejos se reemplazan por un aviso", tools_c[0] == agent.OMITTED, tools_c[0][:40])
check("compactar: los más recientes se conservan", tools_c[-1] == grande)
check("compactar: no toca la lista original", hist == orig_copy)
check("compactar: el protocolo sigue bien formado", agent.validate(comp) == [])
check("compactar: entra en el presupuesto", sum(len(x.get("content") or "") for x in comp) <= 12000 + 200)
check("sin presupuesto no se compacta", agent.api_messages(hist)[2]["content"] == grande)

# ---- 12. ensamblado de llamadas en trozos (lo que hace ChatStream)
cs = dsapi.ChatStream("k", "m", [], tools=at.SPECS)
for d in [[{"index": 0, "id": "c9", "type": "function", "function": {"name": "read_file", "arguments": ""}}],
          [{"index": 0, "function": {"arguments": '{"pa'}}], [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}],
          [{"index": 1, "id": "c10", "function": {"name": "list_dir", "arguments": "{}"}}]]:
    cs._add_call_deltas(d)
got = cs._take_calls()
check("ChatStream arma llamadas a partir de trozos", got[0]["function"]["arguments"] == '{"path": "a.txt"}' and got[1]["function"]["name"] == "list_dir" and got[0]["id"] == "c9", got)
check("ChatStream entrega las llamadas una sola vez", cs._take_calls() is None)
cs2 = dsapi.ChatStream("k", "m", [])
cs2._add_call_deltas([{"function": {"name": "a", "arguments": "{}"}}, {"id": "zz", "function": {"name": "b", "arguments": "{}"}}])
g2 = cs2._take_calls()
check("servidor sin 'index' ni 'id': se separan y se les da id", len(g2) == 2 and all(c["id"] for c in g2) and g2[0]["id"] != g2[1]["id"], g2)

check("CONTROL intacto", open(os.path.join(ws, "CONTROL.txt")).read() == "intacto\n")


# ---- repair: deja el historial válido tras una interrupción
def _call(i):
    return {"id": f"c{i}", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}


def _res(i, txt="ok"):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": txt}


ok = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "", "tool_calls": [_call(1), _call(2)]},
      _res(1), _res(2), {"role": "assistant", "content": "listo"}]
h = copy.deepcopy(ok)
check("repair: un historial sano no cambia", agent.repair(h) == 0 and h == ok)

h = copy.deepcopy(ok)[:3]      # cortado: falta el resultado de c2 y la respuesta final
n = agent.repair(h)
check("repair: agrega el resultado que faltaba", n == 1 and agent.validate(h) == [] and h[-1]["tool_call_id"] == "c2"
      and h[-1]["content"].startswith("ERROR"), (n, h))
check("repair: conserva el resultado que sí existía", h[2] == _res(1))

h = copy.deepcopy(ok)[:2]      # cortado antes de cualquier resultado
n = agent.repair(h)
check("repair: dos resultados faltantes, en el orden de las llamadas", n == 2 and [m["tool_call_id"] for m in h[2:]] == ["c1", "c2"] and agent.validate(h) == [], (n, h))

h = [{"role": "user", "content": "a"}, _res(9, "huérfano"), {"role": "assistant", "content": "b"}]
n = agent.repair(h)
check("repair: descarta un resultado huérfano", n == 1 and [m["role"] for m in h] == ["user", "assistant"], (n, h))

h = copy.deepcopy(ok)
h.insert(4, _res(1, "duplicado"))
n = agent.repair(h)
check("repair: descarta un duplicado y queda bien formado", n == 1 and agent.validate(h) == [] and h[2]["content"] == "ok", (n, h))

h = copy.deepcopy(ok)
h[3] = _res(7, "de otra llamada")       # c2 sin resultado, y uno que no corresponde a nada
n = agent.repair(h)
check("repair: resultado ajeno se descarta y el faltante se repone", agent.validate(h) == [] and n == 2 and not any(m.get("tool_call_id") == "c7" for m in h), (n, h))

# ---- 12. la guarda de repeticiones se reinicia cuando algo cambió de verdad
check("tope de pasos por defecto: 60 (el de antes, 30, cortaba tareas normales)",
      agent.DEFAULT_MAX_STEPS == 60 and agent.Agent(None).max_steps == 60)
rd = lambda i: [("tool_calls", [call(f"r{i}", "read_file", {"path": "a.txt"})]), ("finish", "tool_calls")]
wr = [("tool_calls", [call("w1", "write_file", {"path": "b.txt", "content": "nuevo\n"})]), ("finish", "tool_calls")]
fin = [("content", "listo"), ("finish", "stop")]
st, m, ev, f = run([rd(1), rd(2), wr, rd(3), rd(4), fin])
res = [x["content"] for x in m if x["role"] == "tool"]
check("una escritura exitosa reinicia el contador: releer después NO se trata como repetición",
      st == "done" and not any("already made" in r for r in res) and res[2].startswith("OK"), (st, res))
ed_mal = [("tool_calls", [call("e1", "edit_file", {"path": "a.txt", "old": "no está", "new": "x"})]), ("finish", "tool_calls")]
st, m, ev, f = run([rd(1), rd(2), ed_mal, rd(3), fin])
res = [x["content"] for x in m if x["role"] == "tool"]
check("CONTROL: una edición que falló NO reinicia: la 3.ª lectura igual se frena",
      res[2].startswith("ERROR") and "already made" in res[3], res)
st, m, ev, f = run([rd(1), rd(2), rd(3), fin])
check("CONTROL: sin cambios de por medio, repetir sigue frenándose", "already made" in [x["content"] for x in m if x["role"] == "tool"][2])

# ---- 13. respuesta cortada por el límite de tokens (caso real: sesión cronometro, 2026-09-25)
# llama-server llenó la ventana en medio de un `git commit -m ...`; el JSON roto quedaba en el historial y cada pedido
# siguiente volvía con 500 "Failed to parse tool call arguments as JSON": la sesión quedaba inservible.
roto = '{"command":"git commit --amend -m \\"Version 0.21\\" -m \\"- Cálculo mejorado de ángulos\\" -m'
st, m, ev, f = run([
    [("tool_calls", [call("k1", "run_command", roto), call("k2", "read_file", {"path": "a.txt"})]), ("finish", "length")],
    [("content", "listo"), ("finish", "stop")]])
res = [x["content"] for x in m if x["role"] == "tool"]
check("cortada: el modelo recibe el error JSON y la explicación del corte", "not valid JSON" in res[0] and "cut off by the token limit" in res[0], res[0])
check("cortada: en el historial quedan {} en vez del JSON roto", m[1]["tool_calls"][0]["function"]["arguments"] == agent.BROKEN_ARGS)
check("CONTROL: la otra llamada del mismo paso conserva sus argumentos y corre",
      json.loads(m[1]["tool_calls"][1]["function"]["arguments"]) == {"path": "a.txt"} and "uno dos tres" in res[1], m[1])
check("cortada: el pedido siguiente no lleva ningún argumento inválido",
      all(agent.args_ok(c["function"]["arguments"]) for x in f.seen[1][0] for c in x.get("tool_calls") or []))
check("cortada: se avisa en pantalla que fue por el límite de tokens", any(e[0] == "notice" and "límite de tokens" in e[1] for e in ev))
check("cortada: en pantalla se sigue viendo el texto original", any(e[0] == "tool_start" and e[3] == roto for e in ev))
st, m, ev, f = run([[("content", "a medi"), ("finish", "length")]])
check("respuesta de texto cortada: también avisa", st == "done" and any(e[0] == "notice" and "límite de tokens" in e[1] for e in ev))
st, m, ev, f = run([[("tool_calls", [call("k3", "read_file", '{"path": "a.t')]), ("finish", "tool_calls")], fin])
check("CONTROL: JSON roto sin corte por límite: sin la explicación del corte, pero igual se sanea",
      "cut off" not in m[2]["content"] and m[1]["tool_calls"][0]["function"]["arguments"] == agent.BROKEN_ARGS, m[1:3])

# ---- 14. repair sanea una sesión ya guardada con el JSON roto (la del caso real, al volver a abrirla)
h = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": roto}}]},
     {"role": "tool", "tool_call_id": "c1", "content": "ERROR: arguments are not valid JSON"}]
n = agent.repair(h)
check("repair: argumentos rotos -> {} y cuenta el arreglo", n == 1 and h[1]["tool_calls"][0]["function"]["arguments"] == "{}" and agent.validate(h) == [], (n, h))
check("repair: segunda pasada no cambia nada", agent.repair(h) == 0)
h = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": ""}}]}, _res(1)]
check("CONTROL: repair deja intactos los argumentos vacíos (válidos: equivalen a {})", agent.repair(h) == 0 and h[1]["tool_calls"][0]["function"]["arguments"] == "")

# ---- 15. compactar también los textos largos de las llamadas viejas (write_file lleva el archivo entero)
hist = [{"role": "user", "content": "hola"}]
for i in range(4):
    hist += [{"role": "assistant", "content": "", "tool_calls": [call(f"w{i}", "write_file", {"path": f"f{i}.py", "content": "y" * 6000})]},
             {"role": "tool", "tool_call_id": f"w{i}", "content": "OK"}]
orig_copy = copy.deepcopy(hist)
comp = agent.api_messages(hist, budget_chars=10000)
a0 = json.loads(comp[1]["tool_calls"][0]["function"]["arguments"])
check("compactar llamadas: la más vieja pierde el texto largo pero conserva la ruta", a0 == {"path": "f0.py", "content": agent.OMITTED}, a0)
check("compactar llamadas: la más reciente queda entera", json.loads(comp[-2]["tool_calls"][0]["function"]["arguments"])["content"] == "y" * 6000)
check("compactar llamadas: entra en el presupuesto", agent.messages_size(comp) <= 10000, agent.messages_size(comp))
check("compactar llamadas: todos los argumentos siguen siendo JSON válido",
      all(agent.args_ok(c["function"]["arguments"]) for x in comp for c in x.get("tool_calls") or []))
check("compactar llamadas: no toca la lista original", hist == orig_copy)
check("CONTROL: si alcanza con los resultados, las llamadas no se tocan", agent.api_messages(hist, budget_chars=10 ** 6) == hist)

# ---- 16. presupuesto de un modelo local: sale de la ventana real y se recalibra con prompt_tokens
a = agent.Agent(None, ctx_tokens=20480, reserve_tokens=6826, overhead_chars=9000, chars_per_token=2.5)
check("presupuesto local: (ctx - reserva) * c/t * margen - sobrecarga",
      a.budget() == int((20480 - 6826) * 2.5 * agent.CTX_MARGIN - 9000), a.budget())
check("CONTROL: sin ctx_tokens (DeepSeek) el presupuesto es el fijo", agent.Agent(None, budget_chars=600000).budget() == 600000)
check("presupuesto local: nunca menos que el piso", agent.Agent(None, ctx_tokens=4000, reserve_tokens=3900, overhead_chars=9000).budget() == agent.MIN_BUDGET_CHARS)


class Calib:
    """Stream falso que informa prompt_tokens como un servidor más denso que lo supuesto: (enviado + 2000) / 1,5."""

    def __init__(self):
        self.sent, self.cancelled = [], False

    def factory(self, msgs, tools):
        self.sent.append(agent.messages_size(msgs))
        n = len(self.sent)
        evs = [("usage", {"prompt_tokens": int((self.sent[-1] + 2000) / 1.5)})]
        evs += [("tool_calls", [call(f"n{n}", "list_dir", {})]), ("finish", "tool_calls")] if n < 3 else fin
        return _S(evs, self)


hist = [{"role": "user", "content": "hola"}]
for i in range(12):
    hist += [{"role": "assistant", "content": "", "tool_calls": [call(f"r{i}", "read_file", {"path": "a"})]},
             {"role": "tool", "tool_call_id": f"r{i}", "content": "z" * 3000}]
cb = Calib()
a = agent.Agent(cb.factory, tb, ctx_tokens=12000, reserve_tokens=4000, overhead_chars=2000, chars_per_token=2.5)
st = a.run(hist, lambda *x: None, threading.Event())
check("recalibra: c/t medido con los prompt_tokens del servidor", st == "done" and abs(a.chars_per_token - 1.5) < 0.01, (st, a.chars_per_token))
check("recalibra: el paso siguiente manda menos que el primero", cb.sent[1] < cb.sent[0], cb.sent)
check("recalibra: lo mandado tras calibrar entra en la ventana menos la reserva", (cb.sent[1] + 2000) / 1.5 <= 12000 - 4000, cb.sent)

print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
