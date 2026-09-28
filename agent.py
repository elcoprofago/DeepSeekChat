"""El bucle del agente: el modelo pide herramientas, se ejecutan, el resultado vuelve, hasta que responde con texto.

No sabe nada de ventanas ni de red: recibe una fábrica de streams y una caja de herramientas. Así se prueba entero
con streams falsos, y funciona igual contra DeepSeek que contra un modelo local.

Invariante que este módulo mantiene siempre: en `messages`, todo mensaje del asistente con tool_calls va seguido de
exactamente un mensaje 'tool' por cada llamada, aunque se cancele o falle a la mitad. La API rechaza lo contrario.
"""
import json
import re

import agent_tools as at

OMITTED = "[resultado anterior omitido para ahorrar contexto]"
# Lo que queda en una llamada vieja en lugar de un texto largo (el archivo entero de un write_file). Solo textos de más
# de LONG_ARG_CHARS: un comando nunca se toca. Caso real (sesión cronometro, 2026-09-26): al compactar comandos de 300
# caracteres, el modelo local vio en su propio historial {"command": "<aviso>"} y lo imitó siete veces seguidas.
ARGS_OMITTED = "[texto omitido de una llamada vieja para ahorrar contexto; no es el texto real]"
LONG_ARG_CHARS = 1000
# Los últimos pasos con llamadas llegan enteros al modelo mientras haya otra forma de hacer lugar.
KEEP_RECENT_STEPS = 6
PLACEHOLDER_ERROR = ("ERROR: this call was not run: an argument is only the placeholder that DeepSeekChat puts in OLD "
                     "history to save context. It is not a real command or text. Write the actual command or text; if you "
                     "need the exact content of a file, read it again with read_file.")
# Lo que queda en el historial en lugar de argumentos que no son JSON (una respuesta cortada por el límite de tokens).
# Si quedara el texto roto, llama-server vuelve a leer el historial en cada pedido, no puede, y contesta 500 para
# siempre: la sesión queda inservible.
BROKEN_ARGS = "{}"
REPEAT_WARN = 3
REPEAT_ABORT = 5
# El mismo error con argumentos distintos: el contador de arriba no lo ve, porque cada llamada es otra. Caso real (sesión
# TEMPORALES, 2026-09-28): el modelo local recibió «outside the workspace folder» y «BLOCKED» una y otra vez probando
# rutas y comillas nuevas, sin leer nunca el error. Se compara el error sin lo que va entre comillas ni los números.
SAME_ERROR_WARN = 3
SAME_ERROR_ABORT = 8
ERROR_PREFIXES = ("ERROR", "BLOCKED", "DENIED")
DEFAULT_MAX_STEPS = 60
# Modelos locales: el presupuesto de historial sale de la ventana de contexto real, no de un número fijo de caracteres.
# Caracteres por token de arranque (a propósito bajo: sobra lugar), recalibrado con los prompt_tokens de cada respuesta.
DEFAULT_CHARS_PER_TOKEN = 2.5
CTX_MARGIN = 0.92          # la plantilla de chat agrega tokens que no se ven en los caracteres
MIN_BUDGET_CHARS = 4000
# Herramientas que cambian el estado del proyecto: tras una que salió bien, repetir una lectura o volver a correr
# los tests ya no es repetir en vano (el resultado puede ser otro), así que el contador de repeticiones empieza de cero.
STATE_CHANGING = {"write_file", "edit_file", "delete_path", "run_command"}


def messages_size(msgs):
    return sum(len(m.get("content") or "") + sum(len(c["function"]["arguments"]) for c in m.get("tool_calls") or []) for m in msgs)


def args_ok(raw):
    """True si los argumentos de una llamada son un objeto JSON (vacío cuenta como {})."""
    if raw is None or not str(raw).strip():
        return True
    try:
        return isinstance(json.loads(raw), dict)
    except ValueError:
        return False


def _shrink_args(raw):
    """Argumentos de una llamada vieja con los textos largos (el contenido de un archivo escrito) reemplazados por el
    aviso. Sigue siendo JSON válido: la llamada se ve igual, solo sin el texto."""
    try:
        v = json.loads(raw)
    except ValueError:
        return BROKEN_ARGS
    if not isinstance(v, dict):
        return raw
    return json.dumps({k: (ARGS_OMITTED if isinstance(x, str) and len(x) > LONG_ARG_CHARS else x) for k, x in v.items()},
                      ensure_ascii=False)


def error_kind(result):
    """El error sin sus datos (rutas, comandos, números): dos errores del mismo tipo dan lo mismo. None si no es error."""
    if not result.startswith(ERROR_PREFIXES):
        return None
    s = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`|«[^»\n]*»", "…", result.split("\n", 1)[0])
    return re.sub(r"\d+", "#", s)[:300]


def is_placeholder_call(args):
    """True si algún argumento es exactamente uno de los avisos de compactación: el modelo copió su historial en vez
    de escribir el texto real. Igualdad exacta, no «contiene»: escribir un archivo que menciona el aviso es legítimo."""
    return any(isinstance(v, str) and v.strip() in (OMITTED, ARGS_OMITTED) for v in (args or {}).values())


def api_messages(messages, budget_chars=None):
    """Copia lista para enviar a la API: sin campos privados (_...) y recortada al presupuesto. No toca la lista
    original. Primero se recorta lo viejo, de lo más viejo a lo más nuevo; los últimos KEEP_RECENT_STEPS pasos, solo
    si con eso no alcanza:
      1. resultados de herramientas viejos -> aviso
      2. textos largos (> LONG_ARG_CHARS) de llamadas viejas -> aviso (los comandos quedan enteros)
      3. pasos viejos enteros (llamada + sus resultados) se descartan; los mensajes del usuario nunca
      4. y 5. lo mismo que 1 y 2 sobre los pasos recientes."""
    out = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
    if not budget_chars:
        return out
    size = messages_size(out)
    if size <= budget_chars:
        return out
    steps = [i for i, m in enumerate(out) if m.get("tool_calls")]
    cut = steps[-KEEP_RECENT_STEPS] if len(steps) >= KEEP_RECENT_STEPS else 0     # desde acá, lo reciente

    def results(lo, hi):
        nonlocal size
        for m in out[lo:hi]:
            if size <= budget_chars:
                return
            if m["role"] == "tool" and m.get("content") != OMITTED and len(m.get("content") or "") > len(OMITTED):
                size -= len(m["content"]) - len(OMITTED)
                m["content"] = OMITTED

    def args(lo, hi):
        nonlocal size
        for m in out[lo:hi]:
            if size <= budget_chars:
                return
            if m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    new = _shrink_args(c["function"]["arguments"])
                    size -= len(c["function"]["arguments"]) - len(new)
                    calls.append({**c, "function": {**c["function"], "arguments": new}})
                m["tool_calls"] = calls           # lista nueva: la original sigue intacta

    results(0, cut)
    args(0, cut)
    if size > budget_chars and cut:
        kept, i = [], 0
        while i < cut:
            m = out[i]
            i += 1
            if size > budget_chars and m.get("tool_calls"):
                size -= messages_size([m])
                while i < len(out) and out[i]["role"] == "tool":      # sus resultados se van con ella
                    size -= messages_size([out[i]])
                    i += 1
                continue
            kept.append(m)
        out = kept + out[i:]
        cut = len(kept)
    results(cut, len(out))
    args(cut, len(out))
    return out


def validate(messages):
    """Devuelve una lista de problemas de protocolo (vacía si está bien formado). Lo usan los tests y la GUI al cargar."""
    problems, pending = [], None
    for i, m in enumerate(messages):
        if pending:
            if m["role"] == "tool" and m.get("tool_call_id") in pending:
                pending.discard(m["tool_call_id"])
                continue
            problems.append(f"#{i}: faltan resultados para {sorted(pending)}")
            pending = None
        if m["role"] == "tool":
            problems.append(f"#{i}: resultado de herramienta sin llamada previa")
        if m["role"] == "assistant" and m.get("tool_calls"):
            pending = {c["id"] for c in m["tool_calls"]}
    if pending:
        problems.append(f"final: faltan resultados para {sorted(pending)}")
    return problems


def repair(messages):
    """Deja el historial bien formado tras una interrupción (excepción a mitad de un paso, sesión guardada cortada):
    cada llamada sin resultado recibe uno de error, y los resultados huérfanos se descartan. Modifica la lista en su
    lugar y devuelve cuántos arreglos hizo. También cambia por {} los argumentos que no son JSON (una llamada cortada
    por el límite de tokens), que dejarían la sesión rechazada por llama-server en cada pedido."""
    out, fixes, i = [], 0, 0
    while i < len(messages):
        m = messages[i]
        i += 1
        if m.get("role") == "tool":      # un 'tool' que no consumió una llamada anterior es huérfano
            fixes += 1
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                if not args_ok(c["function"].get("arguments")):
                    c["function"]["arguments"] = BROKEN_ARGS
                    fixes += 1
            got, consumed = {}, 0
            while i < len(messages) and messages[i].get("role") == "tool":
                got.setdefault(messages[i].get("tool_call_id"), messages[i])
                consumed += 1
                i += 1
            wanted = {c["id"] for c in m["tool_calls"]}
            fixes += consumed - len(wanted & set(got))       # duplicados o de otra llamada: se descartan
            for c in m["tool_calls"]:
                r = got.get(c["id"])
                if r is None:
                    fixes += 1
                    r = {"role": "tool", "tool_call_id": c["id"], "content": "ERROR: interrupted before this call finished"}
                out.append(r)
    messages[:] = out
    return fixes


class Agent:
    def __init__(self, make_stream, toolbox=None, max_steps=DEFAULT_MAX_STEPS, budget_chars=600000,
                 ctx_tokens=None, reserve_tokens=0, overhead_chars=0, chars_per_token=DEFAULT_CHARS_PER_TOKEN, label=None):
        """Con ctx_tokens (modelo local) el presupuesto del historial es la ventana menos lo reservado para la
        respuesta (reserve_tokens) y lo que ocupan el prompt de sistema y las herramientas (overhead_chars); si no,
        budget_chars fijo. chars_per_token se recalibra en cada paso y queda en el atributo para la próxima corrida."""
        self.make_stream = make_stream      # (mensajes_api, herramientas|None) -> iterable de eventos con .cancel()
        self.toolbox = toolbox
        self.max_steps = max_steps
        self.budget_chars = budget_chars
        self.ctx_tokens = ctx_tokens
        self.reserve_tokens = reserve_tokens
        self.overhead_chars = overhead_chars
        self.chars_per_token = chars_per_token
        self.label = label      # quién contesta: queda en cada respuesta ("_model"), no se deduce del modelo actual
        self._stream = None

    def _stamp(self, msg):
        if self.label:
            msg["_model"] = self.label
        return msg

    def budget(self):
        if not self.ctx_tokens:
            return self.budget_chars
        room = (self.ctx_tokens - self.reserve_tokens) * self.chars_per_token * CTX_MARGIN - self.overhead_chars
        return max(MIN_BUDGET_CHARS, int(room))

    def _calibrate(self, usage, sent_chars):
        pt = (usage or {}).get("prompt_tokens")
        if self.ctx_tokens and isinstance(pt, int) and pt > 0:
            self.chars_per_token = min(8.0, max(1.0, (sent_chars + self.overhead_chars) / pt))

    def cancel_stream(self):
        s = self._stream
        if s is not None:
            s.cancel()

    def run(self, messages, emit, cancel):
        """Avanza hasta la respuesta final. Devuelve 'done', 'cancelled', 'max_steps' o 'loop'. Los errores de red
        (ApiError) se propagan: los mensajes quedan bien formados hasta el último paso completo."""
        seen, errors = {}, {}
        for step in range(1, self.max_steps + 1):
            if cancel.is_set():
                return "cancelled"
            emit("step_begin", step)
            tools = self.toolbox.specs if self.toolbox else None
            sent = api_messages(messages, self.budget())
            stream = self.make_stream(sent, tools)
            self._stream = stream
            content, reasoning, calls, finish = "", "", None, None
            try:
                for kind, val in stream:
                    if kind == "content":
                        content += val
                    elif kind == "reasoning":
                        reasoning += val
                    elif kind == "tool_calls":
                        calls = val
                    elif kind == "finish":
                        finish = val
                    elif kind == "usage":
                        self._calibrate(val, messages_size(sent))
                    emit(kind, val)
            finally:
                self._stream = None
            if cancel.is_set():
                # lo que llegó hasta acá se conserva, pero sin llamadas a medio armar (no habría con qué contestarlas)
                if content:
                    messages.append(self._stamp({"role": "assistant", "content": content, "_reasoning": reasoning}))
                emit("step_end", {"content": content, "reasoning": reasoning, "finish": finish, "calls": []})
                return "cancelled"

            msg = self._stamp({"role": "assistant", "content": content})
            if reasoning:
                msg["_reasoning"] = reasoning
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
            emit("step_end", {"content": content, "reasoning": reasoning, "finish": finish, "calls": calls or []})
            if finish == "length":
                emit("notice", "La respuesta del modelo se cortó por el límite de tokens (contexto lleno o tope de salida). "
                               "Si pasa seguido con un modelo local, subí el contexto en Opciones.")
            if not calls:
                return "done"

            aborted = False
            for i, c in enumerate(calls):
                name, raw = c["function"]["name"], c["function"]["arguments"]
                if cancel.is_set():
                    result = "ERROR: cancelled by the user before this call ran"
                    emit("tool_start", c["id"], name, raw)
                elif self.toolbox is None:
                    result = "ERROR: tools are not available in this session (no workspace folder is open)"
                    emit("tool_start", c["id"], name, raw)
                else:
                    try:
                        args = at.parse_args(raw)
                    except at.ToolError as e:
                        args, result = None, f"ERROR: {e}"
                        if finish == "length":
                            result += (". Your reply was cut off by the token limit before the arguments of this call were "
                                       "complete, so it did not run. Resend it shorter: split a big file into several edits, "
                                       "keep commit messages to one short line.")
                        if not args_ok(raw):
                            c["function"]["arguments"] = BROKEN_ARGS    # en pantalla se sigue viendo `raw`, el texto original
                    emit("tool_start", c["id"], name, args if args is not None else raw)
                    if args is not None:
                        key = (name, json.dumps(args, sort_keys=True))
                        seen[key] = seen.get(key, 0) + 1
                        if seen[key] >= REPEAT_ABORT:
                            aborted = True
                            result = "ERROR: identical call repeated too many times; stopped."
                        elif seen[key] >= REPEAT_WARN:
                            result = ("ERROR: you already made this exact call %d times. Stop repeating it: answer the user "
                                      "with what you know, or try a different approach." % (seen[key] - 1))
                        elif is_placeholder_call(args):
                            result = PLACEHOLDER_ERROR
                        else:
                            result = self.toolbox.execute(name, args, cancel)
                            if name in STATE_CHANGING and not result.startswith(ERROR_PREFIXES):
                                seen.clear()
                                errors.clear()
                    kind = None if aborted or cancel.is_set() else error_kind(result)
                    if kind:
                        errors[kind] = errors.get(kind, 0) + 1
                        if errors[kind] >= SAME_ERROR_ABORT:
                            aborted = True
                            result += ("\n\nStopped: you got this same error %d times. Explain to the user what "
                                       "you were trying to do and what the error says." % errors[kind])
                        elif errors[kind] >= SAME_ERROR_WARN:
                            result += ("\n\nNOTE: you got this same error %d times now, with different arguments. "
                                       "Variations of the same call will not fix it. Read the error message above and do what "
                                       "it says; if it is a restriction of DeepSeekChat (blocked, outside the folder, denied), "
                                       "it will not change: explain it to the user instead of trying again." % errors[kind])
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                emit("tool_result", c["id"], name, result)
            if aborted:
                emit("notice", "Se detectó un bucle (el modelo repitió la misma llamada o recibió el mismo error una y "
                               "otra vez); se detuvo el agente.")
                return "loop"
        emit("notice", f"Se alcanzó el máximo de {self.max_steps} pasos sin una respuesta final. Apretá «Continuar» (o escribí «seguí») para que siga.")
        return "max_steps"
