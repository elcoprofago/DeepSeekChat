"""Claude (API de Anthropic) por HTTP directo, sin SDK: el runtime portable no tiene pip ni site-packages.

El resto del programa habla el formato de OpenAI (mensajes con 'tool_calls' y mensajes 'tool'). Este módulo traduce
ida y vuelta, y ClaudeStream emite los mismos eventos que dsapi.ChatStream ('reasoning', 'content', 'usage',
'tool_calls', 'finish', 'notice') más uno propio, ('blocks', [...]): el contenido crudo de la respuesta, que el agente
guarda en el mensaje como '_blocks'. Hace falta porque los bloques de razonamiento ('thinking') llevan una firma y la
API los quiere de vuelta tal cual en los pasos siguientes.

Reglas de la API que este módulo respeta (ver los tests en test_claudeapi.py):
  - 'system' va aparte, no como mensaje; los resultados de herramientas van en UN mensaje de usuario por paso, con
    los bloques tool_result primero; no hay bloques de texto vacíos; dos mensajes seguidos del mismo rol se unen.
  - La firma de un bloque de razonamiento queda atada a la conversación anterior a él. agent.api_messages recorta los
    resultados viejos para ahorrar contexto, y eso cuenta como editar la conversación: con
    block_binding.prefix_mismatch_behavior = "drop_block" la API descarta esos bloques en vez de rechazar el pedido.
    Si aun así contesta 400 por una firma, se reintenta una vez sin ningún bloque de razonamiento.
"""
import copy
import json
import re
import urllib.request

import dsapi

BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
WHO = "Claude"
BETA_BINDING = "thinking-binding-controls-2026-08-01"
BETA_FALLBACK = "server-side-fallback-2026-07-01"
# Modelos que pueden negarse a responder por sus clasificadores de seguridad: con fallbacks="default" la API reintenta
# sola en el modelo que Anthropic recomienda para esa categoría, dentro del mismo pedido.
FALLBACK_OK = ("claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Si /v1/models no responde. Los datos de cada uno los reemplaza lo que informe la API.
FALLBACK_MODELS = [
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "efforts": EFFORTS, "context": 1000000, "max_out": 128000, "adaptive": True},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "efforts": EFFORTS, "context": 1000000, "max_out": 128000, "adaptive": True},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "efforts": [], "context": 200000, "max_out": 64000, "adaptive": False},
]
ERROR_PREFIXES = ("ERROR", "BLOCKED", "DENIED")
OPEN_TIMEOUT = 60
READ_TIMEOUT = 300
STOP_MAP = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
            "model_context_window_exceeded": "length", "pause_turn": "stop", "refusal": "stop"}
KEEP_BLOCKS = ("thinking", "redacted_thinking", "text", "tool_use")


def headers(key, betas=()):
    h = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    if betas:
        h["anthropic-beta"] = ",".join(betas)
    return h


def _get(path, key):
    return dsapi._get_json(path, key, base=BASE, headers=headers(key), who=WHO)


# ---------------------------------------------------------------- consultas

def _supported(caps, *path):
    cur = caps
    for k in path:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
    return bool(isinstance(cur, dict) and cur.get("supported"))


def list_models(key):
    """[{id, name, efforts, context, max_out, adaptive}] con los ids tal como los usa la API (sin prefijo)."""
    out, after = [], None
    for _ in range(20):                              # paginado; 20 páginas de 100 sobran
        data = _get("/models?limit=100" + (f"&after_id={after}" if after else ""), key)
        for m in data.get("data") or []:
            caps = m.get("capabilities") or {}
            out.append({
                "id": m["id"],
                "name": m.get("display_name") or m["id"],
                "efforts": [e for e in EFFORTS if _supported(caps, "effort", e)],
                "context": m.get("max_input_tokens") or 0,
                "max_out": m.get("max_tokens") or 0,
                "adaptive": _supported(caps, "thinking", "types", "adaptive"),
            })
        if not data.get("has_more") or not data.get("last_id"):
            break
        after = data["last_id"]
    if not out:
        raise dsapi.ApiError("Claude no devolvió ningún modelo")
    return out


# ---------------------------------------------------------------- traducción de mensajes

def _tool_id(cid):
    """Los id de tool_use solo admiten letras, números, '_' y '-'; los de otros proveedores pueden traer otra cosa."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(cid or "")) or "call"


def _input_of(raw):
    try:
        v = json.loads(raw) if raw and str(raw).strip() else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def convert_tools(specs):
    """Herramientas en formato OpenAI -> formato de Anthropic, con los argumentos en streaming a medida que se generan."""
    out = []
    for t in specs or []:
        fn = t.get("function") or t
        out.append({"name": fn["name"], "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                    "eager_input_streaming": True})
    return out


def _assistant_blocks(m):
    """Los bloques de un mensaje del asistente. Si hay '_blocks' (la respuesta cruda de Claude) se usan tal cual, con
    los argumentos de cada herramienta tomados de 'tool_calls' (agent.api_messages puede haberlos acortado); si no
    (el mensaje lo escribió otro modelo), se arman con el texto y las llamadas."""
    calls = {c["id"]: c for c in m.get("tool_calls") or []}
    raw = m.get("_blocks")
    blocks = []
    if isinstance(raw, list) and raw:
        used = set()
        for b in raw:
            t = b.get("type") if isinstance(b, dict) else None
            if t not in KEEP_BLOCKS:
                continue
            if t == "text" and not b.get("text"):
                continue
            if t == "tool_use":
                c = calls.get(b.get("id"))
                if c is None:
                    continue                            # una llamada que el historial ya no tiene: no puede quedar sin resultado
                used.add(c["id"])
                blocks.append({"type": "tool_use", "id": _tool_id(c["id"]), "name": c["function"]["name"],
                               "input": _input_of(c["function"]["arguments"])})
                continue
            blocks.append(copy.deepcopy(b))
        for cid, c in calls.items():                   # llamadas que no estaban en los bloques (no debería pasar)
            if cid not in used:
                blocks.append({"type": "tool_use", "id": _tool_id(cid), "name": c["function"]["name"],
                               "input": _input_of(c["function"]["arguments"])})
        return blocks
    if m.get("content"):
        blocks.append({"type": "text", "text": m["content"]})
    for c in m.get("tool_calls") or []:
        blocks.append({"type": "tool_use", "id": _tool_id(c["id"]), "name": c["function"]["name"],
                       "input": _input_of(c["function"]["arguments"])})
    return blocks


def convert_messages(messages):
    """Mensajes en formato OpenAI -> (system, messages) de Anthropic."""
    system, out = [], []

    def push(role, blocks):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if role == "user":
                # en un mensaje de usuario los tool_result van antes que cualquier texto
                res = [b for b in prev + blocks if b.get("type") == "tool_result"]
                rest = [b for b in prev + blocks if b.get("type") != "tool_result"]
                out[-1]["content"] = res + rest
            else:
                prev.extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system.append(m["content"])
        elif role == "user":
            text = m.get("content") or ""
            push("user", [{"type": "text", "text": text}] if text.strip() else [])
        elif role == "tool":
            res = m.get("content") or "(sin salida)"
            b = {"type": "tool_result", "tool_use_id": _tool_id(m.get("tool_call_id")), "content": res}
            if res.startswith(ERROR_PREFIXES):
                b["is_error"] = True
            push("user", [b])
        elif role == "assistant":
            push("assistant", _assistant_blocks(m))
    # la conversación tiene que empezar por el usuario y no puede terminar en el asistente (no hay «prefill»)
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] == "assistant":
        out.pop()
    return "\n\n".join(system), out


def strip_thinking(messages):
    """Copia sin bloques de razonamiento (la recuperación de una vez ante una firma rechazada)."""
    out = []
    for m in messages:
        if m["role"] == "assistant":
            blocks = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
            if blocks:
                out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


# ---------------------------------------------------------------- streaming

class ClaudeStream(dsapi.ChatStream):
    """Mismos eventos que dsapi.ChatStream, más ('blocks', lista) antes de 'finish'. Hereda de ChatStream solo la
    apertura cancelable con reintentos, cancel() y el timeout de lectura."""

    def __init__(self, key, model, messages, effort=None, max_tokens=32768, tools=None, adaptive=True,
                 timeout=READ_TIMEOUT, open_timeout=OPEN_TIMEOUT, retries=1, base=BASE):
        self._key = key
        self._base = base
        system, msgs = convert_messages(messages)
        body = {"model": model, "max_tokens": int(max_tokens), "messages": msgs, "stream": True,
                "cache_control": {"type": "ephemeral"}}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = convert_tools(tools)
        betas = []
        if adaptive:
            body["thinking"] = {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            betas.append(BETA_BINDING)
        if effort:
            body["output_config"] = {"effort": effort}
        if model in FALLBACK_OK:
            body["fallbacks"] = "default"
            betas.append(BETA_FALLBACK)
        self._body, self._betas = body, betas
        self._who = WHO
        self._timeout = timeout
        self._open_timeout = open_timeout
        self._retries = retries
        self._resp = None
        self._cancelled = False
        self._req = self._make_req()

    def _make_req(self):
        return urllib.request.Request(self._base + "/messages", data=json.dumps(self._body).encode("utf-8"),
                                      headers=headers(self._key, self._betas))

    def _has_thinking(self):
        return any(b.get("type") in ("thinking", "redacted_thinking")
                   for m in self._body["messages"] if m["role"] == "assistant" for b in m["content"])

    def _recover(self, err):
        """Ajusta el pedido tras un 400 que tiene arreglo conocido. Devuelve el aviso a mostrar, o None si no hay."""
        msg = str(err)
        if "signature" in msg and self._has_thinking():
            self._body["messages"] = strip_thinking(self._body["messages"])
            return "Claude rechazó el razonamiento guardado de pasos anteriores; se reintenta sin él."
        if ("block_binding" in msg or BETA_BINDING in msg) and "thinking" in self._body:
            self._body["thinking"].pop("block_binding", None)
            self._betas = [b for b in self._betas if b != BETA_BINDING]
            return "Se reintenta sin el control de firmas del razonamiento."
        if ("fallbacks" in msg or BETA_FALLBACK in msg) and "fallbacks" in self._body:
            self._body.pop("fallbacks")
            self._betas = [b for b in self._betas if b != BETA_FALLBACK]
            return "Se reintenta sin el modelo de respaldo ante negativas."
        return None

    def _open_with_recovery(self):
        tried = set()
        while True:
            try:
                ok = yield from self._connect()
                return ok
            except dsapi.ApiError as e:
                if e.status != 400 or self._cancelled:
                    raise
                note = self._recover(e)
                if note is None or note in tried:
                    raise
                tried.add(note)
                yield ("notice", note)
                self._req = self._make_req()

    def __iter__(self):
        if self._cancelled:
            return
        if not (yield from self._open_with_recovery()):
            return
        blocks = {}           # índice -> bloque en armado
        order = []
        usage = {"in": 0, "cache_read": 0, "cache_write": 0, "out": 0}
        stop, stop_details, served_by = None, None, None
        fell_back = False
        try:
            for raw in self._resp:
                if self._cancelled:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    continue
                t = obj.get("type")
                if t == "message_start":
                    msg = obj.get("message") or {}
                    served_by = msg.get("model")
                    self._take_usage(usage, msg.get("usage"))
                elif t == "content_block_start":
                    idx, cb = obj.get("index"), dict(obj.get("content_block") or {})
                    if cb.get("type") == "tool_use":
                        cb["_json"] = ""
                    elif cb.get("type") == "fallback":
                        # la API pasó la respuesta a otro modelo: lo anterior que no sea texto no vuelve a enviarse
                        fell_back = True
                        to = (cb.get("to") or {}).get("model") or "otro modelo"
                        yield ("notice", f"El modelo pidió no seguir con esto; la API continuó con {to}.")
                        for i in list(order):
                            if blocks[i].get("type") != "text":
                                order.remove(i)
                        continue
                    blocks[idx] = cb
                    order.append(idx)
                elif t == "content_block_delta":
                    b, d = blocks.get(obj.get("index")), obj.get("delta") or {}
                    dt = d.get("type")
                    if b is None:
                        continue
                    if dt == "text_delta":
                        b["text"] = b.get("text", "") + d.get("text", "")
                        if d.get("text"):
                            yield ("content", d["text"])
                    elif dt == "thinking_delta":
                        b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                        if d.get("thinking"):
                            yield ("reasoning", d["thinking"])
                    elif dt == "signature_delta":
                        b["signature"] = b.get("signature", "") + d.get("signature", "")
                    elif dt == "input_json_delta":
                        b["_json"] += d.get("partial_json", "")
                elif t == "message_delta":
                    d = obj.get("delta") or {}
                    stop = d.get("stop_reason") or stop
                    stop_details = d.get("stop_details") or stop_details
                    self._take_usage(usage, obj.get("usage"))
                elif t == "error":
                    err = obj.get("error") or {}
                    et = err.get("type", "")
                    text = err.get("message") or str(err)
                    if et == "overloaded_error":
                        text = "servidor sobrecargado; probá de nuevo en un rato. " + text
                    raise dsapi.ApiError(f"Claude: {text}")
                elif t == "message_stop":
                    break
        except (OSError, ValueError, AttributeError) as e:
            if self._cancelled:
                return
            raise dsapi.ApiError(f"Se cortó la conexión con Claude: {e}")
        finally:
            try:
                self._resp.close()
            except Exception:
                pass
        if self._cancelled:
            return

        final = [blocks[i] for i in order]
        calls = []
        for b in final:
            if b.get("type") == "tool_use":
                raw = b.pop("_json", "") or ""
                b["input"] = _input_of(raw)
                calls.append({"id": b.get("id") or f"toolu_{len(calls)}", "type": "function",
                              "function": {"name": b.get("name", ""), "arguments": raw.strip() or "{}"}})
        if stop == "refusal":
            cat = (stop_details or {}).get("category")
            yield ("notice", "Claude se negó a seguir con este pedido" + (f" (categoría: {cat})" if cat else "") +
                   ". Lo que haya escrito hasta ahí puede estar incompleto.")
            calls = []                                     # una llamada cortada por la negativa no se ejecuta
            final = [b for b in final if b.get("type") != "tool_use"]
        if fell_back and served_by:
            yield ("notice", f"Esta respuesta la dio {served_by}.")
        yield ("blocks", final)
        if calls:
            yield ("tool_calls", calls)
        yield ("usage", {"prompt_tokens": usage["in"] + usage["cache_read"] + usage["cache_write"],
                         "completion_tokens": usage["out"], "prompt_cache_hit_tokens": usage["cache_read"]})
        yield ("finish", STOP_MAP.get(stop, "stop"))

    @staticmethod
    def _take_usage(acc, u):
        """message_start trae la entrada; message_delta, los totales acumulados (la salida y, a veces, la entrada)."""
        if not isinstance(u, dict):
            return
        for k, field in (("in", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
                         ("cache_write", "cache_creation_input_tokens"), ("out", "output_tokens")):
            v = u.get(field)
            if isinstance(v, int):
                acc[k] = v
