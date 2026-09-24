"""El bucle del agente: el modelo pide herramientas, se ejecutan, el resultado vuelve, hasta que responde con texto.

No sabe nada de ventanas ni de red: recibe una fábrica de streams y una caja de herramientas. Así se prueba entero
con streams falsos, y funciona igual contra DeepSeek que contra un modelo local.

Invariante que este módulo mantiene siempre: en `messages`, todo mensaje del asistente con tool_calls va seguido de
exactamente un mensaje 'tool' por cada llamada, aunque se cancele o falle a la mitad. La API rechaza lo contrario.
"""
import json

import agent_tools as at

OMITTED = "[resultado anterior omitido para ahorrar contexto]"
REPEAT_WARN = 3
REPEAT_ABORT = 5
DEFAULT_MAX_STEPS = 60
# Herramientas que cambian el estado del proyecto: tras una que salió bien, repetir una lectura o volver a correr
# los tests ya no es repetir en vano (el resultado puede ser otro), así que el contador de repeticiones empieza de cero.
STATE_CHANGING = {"write_file", "edit_file", "delete_path", "run_command"}


def api_messages(messages, budget_chars=None):
    """Copia lista para enviar a la API: sin campos privados (_...) y, si no entra en el presupuesto, con los
    resultados de herramientas más viejos reemplazados por un aviso. No toca la lista original."""
    out = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
    if not budget_chars:
        return out
    size = sum(len(m.get("content") or "") + sum(len(c["function"]["arguments"]) for c in m.get("tool_calls") or []) for m in out)
    for m in out:
        if size <= budget_chars:
            break
        if m["role"] == "tool" and m.get("content") != OMITTED and len(m.get("content") or "") > len(OMITTED):
            size -= len(m["content"]) - len(OMITTED)
            m["content"] = OMITTED
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
    lugar y devuelve cuántos arreglos hizo."""
    out, fixes, i = [], 0, 0
    while i < len(messages):
        m = messages[i]
        i += 1
        if m.get("role") == "tool":      # un 'tool' que no consumió una llamada anterior es huérfano
            fixes += 1
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
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
    def __init__(self, make_stream, toolbox=None, max_steps=DEFAULT_MAX_STEPS, budget_chars=600000):
        self.make_stream = make_stream      # (mensajes_api, herramientas|None) -> iterable de eventos con .cancel()
        self.toolbox = toolbox
        self.max_steps = max_steps
        self.budget_chars = budget_chars
        self._stream = None

    def cancel_stream(self):
        s = self._stream
        if s is not None:
            s.cancel()

    def run(self, messages, emit, cancel):
        """Avanza hasta la respuesta final. Devuelve 'done', 'cancelled', 'max_steps' o 'loop'. Los errores de red
        (ApiError) se propagan: los mensajes quedan bien formados hasta el último paso completo."""
        seen = {}
        for step in range(1, self.max_steps + 1):
            if cancel.is_set():
                return "cancelled"
            emit("step_begin", step)
            tools = self.toolbox.specs if self.toolbox else None
            stream = self.make_stream(api_messages(messages, self.budget_chars), tools)
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
                    emit(kind, val)
            finally:
                self._stream = None
            if cancel.is_set():
                # lo que llegó hasta acá se conserva, pero sin llamadas a medio armar (no habría con qué contestarlas)
                if content:
                    messages.append({"role": "assistant", "content": content, "_reasoning": reasoning})
                emit("step_end", {"content": content, "reasoning": reasoning, "finish": finish, "calls": []})
                return "cancelled"

            msg = {"role": "assistant", "content": content}
            if reasoning:
                msg["_reasoning"] = reasoning
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
            emit("step_end", {"content": content, "reasoning": reasoning, "finish": finish, "calls": calls or []})
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
                        else:
                            result = self.toolbox.execute(name, args, cancel)
                            if name in STATE_CHANGING and not result.startswith(("ERROR", "BLOCKED", "DENIED")):
                                seen.clear()
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                emit("tool_result", c["id"], name, result)
            if aborted:
                emit("notice", "Se detectó un bucle (el modelo repitió la misma llamada); se detuvo el agente.")
                return "loop"
        emit("notice", f"Se alcanzó el máximo de {self.max_steps} pasos sin una respuesta final. Apretá «Continuar» (o escribí «seguí») para que siga.")
        return "max_steps"
