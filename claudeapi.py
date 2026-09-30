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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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


# El informe de costos de la Admin API (GET /v1/organizations/cost_report): cuánto se gastó, en centavos de dólar
# como texto decimal, por día UTC. Pide una Admin key (sk-ant-admin…) o una key con ámbito «Organización»; la Admin
# API no existe para cuentas individuales. El crédito DISPONIBLE (el de la página de facturación) no lo da ninguna API.
# El informe de costos NO trae el día UTC en curso (medido el 30/9/2026: ni pidiéndolo con ending_at); lo de hoy se
# calcula con el informe de uso por hora (usage_report, ese sí lo trae) y la tabla de precios de abajo.
BILLING_URL = "https://platform.claude.com/settings/billing"
PRICES_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
COST_PAGES = 12          # tope de páginas: un mes son 31 buckets diarios = 1 página; esto solo corta un bucle raro
BETA_FAST = "fast-mode-2026-02-01"   # sin este encabezado el informe de uso no separa el modo rápido

# US$ por millón de tokens, copiados de PRICES_URL el 30/9/2026: (entrada, salida, lectura de caché como fracción de la
# entrada). Escritura de caché: 1,25x la entrada (5 min) y 2x (1 h). Un modelo que no esté acá NO se estima: se informa
# como «sin precio». Si Anthropic cambia un precio, month_summary lo detecta comparando lo calculado para ayer con lo
# que dice el informe de costos para ayer.
PRICES = {
    "claude-fable-5-1": (10, 50, 0.025), "claude-mythos-5-1": (10, 50, 0.025),
    "claude-fable-5": (10, 50, 0.1), "claude-mythos-5": (10, 50, 0.1),
    "claude-opus-5-5": (4, 20, 0.05),
    "claude-opus-5": (5, 25, 0.1), "claude-opus-4-8": (5, 25, 0.1), "claude-opus-4-7": (5, 25, 0.1),
    "claude-opus-4-6": (5, 25, 0.1), "claude-opus-4-5": (5, 25, 0.1),
    "claude-opus-4-1": (15, 75, 0.1), "claude-opus-4": (15, 75, 0.1), "claude-opus-4-0": (15, 75, 0.1),
    "claude-sonnet-5-5": (2, 10, 0.1), "claude-sonnet-5": (2, 10, 0.1),
    "claude-sonnet-4-6": (3, 15, 0.1), "claude-sonnet-4-5": (3, 15, 0.1),
    "claude-sonnet-4": (3, 15, 0.1), "claude-sonnet-4-0": (3, 15, 0.1),
    "claude-haiku-4-5": (1, 5, 0.1), "claude-3-5-haiku": (0.8, 4, 0.1),
}
FAST_PRICES = {"claude-opus-5-5": (8, 40), "claude-opus-5": (10, 50), "claude-opus-4-8": (10, 50)}
# «Claude 4.6 y posteriores»: contexto de 1M a precio normal y recargo de 1,1x con inference_geo "us". Para los
# anteriores PRICES_URL no publica esos precios: no se estiman.
MODERN = {"claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5", "claude-opus-5-5",
          "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5",
          "claude-sonnet-5", "claude-sonnet-4-6"}
WEB_SEARCH_USD = 0.01    # US$ 10 cada 1.000 búsquedas
# Campos del informe de uso que se saben cobrar. Uno de tokens que no esté acá (una categoría nueva) no se estima.
TOKEN_FIELDS = {"uncached_input_tokens", "cache_read_input_tokens", "output_tokens"}
CACHE_FIELDS = {"ephemeral_5m_input_tokens": 1.25, "ephemeral_1h_input_tokens": 2.0}
# web_fetch no tiene cargo; code_execution se cobra por hora de contenedor, con 1.550 h gratis por mes por organización
SERVER_TOOLS = {"web_search_requests", "web_fetch_requests", "code_execution_requests"}
PRICE_TOLERANCE = 0.01   # US$: diferencia admitida entre lo calculado para ayer y el informe de costos (redondeo)


class Unpriced(Exception):
    """Una fila del informe de uso con algo que la tabla de precios no cubre."""


def _day(dt):
    return dt.strftime("%Y-%m-%dT00:00:00Z")


def month_start(now=None):
    """Primer día del mes corriente, 00:00 UTC, en RFC 3339 (los buckets del informe son días UTC)."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01T00:00:00Z")


def _admin_get(path, q, key, betas=()):
    try:
        return dsapi._get_json(path + "?" + urllib.parse.urlencode(q, doseq=True), key, base=BASE,
                               headers=headers(key, betas), who=WHO)
    except dsapi.ApiError as e:
        if e.status in (401, 403):
            # la key puede andar bien para chatear: lo que le falta es acceso a la Admin API, no validez
            raise dsapi.ApiError("La key no tiene acceso al informe de costos"
                                 + (f": {e.detail}" if e.detail else ""), status=e.status, detail=e.detail)
        raise


def _buckets(path, q, key, betas=()):
    """Todos los buckets de un informe, siguiendo next_page."""
    out, page = [], None
    for _ in range(COST_PAGES):
        data = _admin_get(path, dict(q, page=page) if page else q, key, betas)
        if not isinstance(data, dict) or not isinstance(data.get("data") or [], list):
            raise dsapi.ApiError(f"Respuesta ilegible del informe de Claude ({path})")
        out += data.get("data") or []
        page = data.get("next_page")
        if not data.get("has_more") or not page:
            break
    return out


def cost_by_day(key, start, end):
    """{"AAAA-MM-DD": US$} del informe de costos, días UTC en [start, end)."""
    days = {}
    try:
        for b in _buckets("/organizations/cost_report", {"starting_at": start, "ending_at": end, "limit": 31}, key):
            day = str(b.get("starting_at") or "")[:10]
            days[day] = days.get(day, 0.0) + sum(float(r.get("amount") or 0) for r in b.get("results") or []) / 100
    except (TypeError, ValueError, AttributeError):
        raise dsapi.ApiError("Respuesta ilegible del informe de costos de Claude")
    return days


def month_cost(key, now=None):
    """Dólares gastados en la organización desde el primer día del mes (UTC) hasta ayer, según el informe de costos."""
    now = now or datetime.now(timezone.utc)
    start, today = month_start(now), _day(now)
    if start == today:                   # día 1: todavía no hay días cerrados en el mes
        return 0.0
    return sum(cost_by_day(key, start, today).values())


def _base_model(model):
    return re.sub(r"-\d{8}$", "", str(model or ""))


def row_cost(r):
    """US$ de una fila del informe de uso. Levanta Unpriced si algo de la fila no tiene precio publicado conocido."""
    model = _base_model(r.get("model"))
    if model not in PRICES:
        raise Unpriced(f"modelo {r.get('model')}")
    inp, out, read = PRICES[model]
    tier = r.get("service_tier") or "standard"
    if tier not in ("standard", "batch"):
        raise Unpriced(f"nivel de servicio «{tier}» en {model}")
    speed = r.get("speed") or "standard"
    if speed == "fast":
        if model not in FAST_PRICES:
            raise Unpriced(f"modo rápido en {model}")
        inp, out = FAST_PRICES[model]
    elif speed != "standard":
        raise Unpriced(f"velocidad «{speed}» en {model}")
    mult = 0.5 if tier == "batch" else 1.0
    geo = r.get("inference_geo") or "global"
    if geo == "us" and model in MODERN:
        mult *= 1.1
    elif geo not in ("global", "not_available"):
        raise Unpriced(f"región «{geo}» en {model}")
    ctx = r.get("context_window") or "0-200k"
    if ctx != "0-200k" and model not in MODERN:
        raise Unpriced(f"contexto largo ({ctx}) en {model}")
    cache = r.get("cache_creation") or {}
    tools = r.get("server_tool_use") or {}
    extra = [k for k in r if k.endswith("tokens") and k not in TOKEN_FIELDS and r.get(k)]
    extra += [k for k in cache if k not in CACHE_FIELDS and cache.get(k)]
    extra += [k for k in tools if k not in SERVER_TOOLS and tools.get(k)]
    if extra:
        raise Unpriced(f"{', '.join(extra)} en {model}")
    try:
        tok = (inp * int(r.get("uncached_input_tokens") or 0)
               + sum(inp * m * int(cache.get(k) or 0) for k, m in CACHE_FIELDS.items())
               + inp * read * int(r.get("cache_read_input_tokens") or 0)
               + out * int(r.get("output_tokens") or 0)) / 1e6
        return tok * mult + WEB_SEARCH_USD * int(tools.get("web_search_requests") or 0)
    except (TypeError, ValueError):
        raise Unpriced(f"números ilegibles en {model}")


def usage_cost(key, start, end, width):
    """(US$, [lo que no tiene precio]) del informe de uso entre start y end, con buckets de width ("1h" o "1d")."""
    q = {"starting_at": start, "ending_at": end, "bucket_width": width, "limit": 24 if width == "1h" else 31,
         "group_by[]": ["model", "service_tier", "inference_geo", "context_window", "speed"]}
    total, unpriced = 0.0, []
    for b in _buckets("/organizations/usage_report/messages", q, key, (BETA_FAST,)):
        for r in (b.get("results") if isinstance(b, dict) else None) or []:
            try:
                total += row_cost(r)
            except Unpriced as e:
                if str(e) not in unpriced:
                    unpriced.append(str(e))
    return total, unpriced


def month_summary(key, now=None):
    """Gasto del mes en Claude, con el día de hoy incluido.

    Días cerrados: el informe de costos (el dato de Anthropic). Hoy: calculado del informe de uso por hora con PRICES.
    Ayer se calcula también y se compara con el informe de costos: si difieren, PRICES está desactualizado y lo de hoy
    NO se suma. Si el informe de costos todavía no trae ayer (0 con uso > 0), ayer se toma calculado.
    Devuelve {closed, today, total, unpriced, check, check_calc, check_reported}; check es "ok", "diferencia",
    "sin uso ayer" o "ayer pendiente"."""
    now = now or datetime.now(timezone.utc)
    start, today = month_start(now), _day(now)
    yday = _day(now - timedelta(days=1))
    if start != today:
        days = cost_by_day(key, start, today)
        reported = days.get(yday[:10], 0.0)
    else:                                # día 1: ayer es del mes anterior, solo sirve para verificar precios
        days = {}
        reported = sum(cost_by_day(key, yday, today).values())
    calc_y, unpriced_y = usage_cost(key, yday, today, "1d")
    today_usd, unpriced = usage_cost(key, today, now.strftime("%Y-%m-%dT%H:%M:%SZ"), "1h")
    closed = sum(days.values())
    if calc_y == 0 and reported == 0 and not unpriced_y:
        check = "sin uso ayer"
    elif reported == 0:
        check = "ayer pendiente"
        if start != today:
            closed += calc_y             # el informe de costos todavía no cerró ayer: se usa lo calculado
            unpriced += [u for u in unpriced_y if u not in unpriced]
    elif unpriced_y or abs(calc_y - reported) > PRICE_TOLERANCE:
        check = "diferencia"
    else:
        check = "ok"
    total = closed + (0.0 if check == "diferencia" else today_usd)
    return {"closed": closed, "today": today_usd, "total": total, "unpriced": unpriced, "check": check,
            "check_calc": calc_y, "check_reported": reported}


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
