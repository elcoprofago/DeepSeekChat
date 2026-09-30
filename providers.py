"""Los proveedores de modelos por API: DeepSeek, Claude (Anthropic) y OpenAI.

Dentro del programa, el id de un modelo lleva el prefijo de su proveedor ("anthropic:claude-opus-5-5",
"openai:gpt-5"), salvo los de DeepSeek, que siguen sin prefijo: así las sesiones y la configuración guardadas por
versiones anteriores siguen valiendo. api_model() le quita el prefijo para mandarlo a la API.
"""
import re

import claudeapi
import dsapi

OPENAI_BASE = "https://api.openai.com/v1"

ORDER = ["deepseek", "anthropic", "openai"]
NAMES = {"deepseek": "DeepSeek", "anthropic": "Claude", "openai": "OpenAI", "anthropic_admin": "Claude (Admin)"}
KEY_HINT = {"deepseek": "platform.deepseek.com", "anthropic": "console.anthropic.com", "openai": "platform.openai.com",
            "anthropic_admin": "la consola de Claude, Admin keys; opcional"}
# Una key que no es de un proveedor de modelos: la Admin key de Claude (sk-ant-admin…) solo sirve para leer cuánto se
# gastó en el mes (claudeapi.month_cost); no manda mensajes. Se carga y guarda como las demás, en Configuración.
ADMIN = "anthropic_admin"
KEY_SLOTS = ORDER + [ADMIN]
PREFIX = {"anthropic": "anthropic:", "openai": "openai:"}

# OpenAI no informa contexto ni salida máxima en /v1/models: tabla por prefijo (el más largo que coincida gana)
_OPENAI_LIMITS = [("gpt-4.1", 1047576, 32768), ("gpt-4o", 128000, 16384), ("gpt-4-turbo", 128000, 4096),
                  ("gpt-5", 400000, 128000), ("o1", 200000, 100000), ("o3", 200000, 100000), ("o4", 200000, 100000),
                  ("chatgpt-", 128000, 16384)]
OPENAI_UNKNOWN_OUT = 16384
_OPENAI_SKIP = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation")
_OPENAI_REASONING = re.compile(r"^(o1|o3|o4|gpt-5)")

FALLBACK = {
    "deepseek": dsapi.FALLBACK_MODELS,
    "anthropic": claudeapi.FALLBACK_MODELS,
    "openai": [
        {"id": "gpt-5", "name": "GPT-5", "efforts": ["low", "medium", "high"], "context": 400000, "max_out": 128000},
        {"id": "gpt-4.1", "name": "GPT-4.1", "efforts": [], "context": 1047576, "max_out": 32768},
    ],
}


def provider_of(mid):
    for p, pre in PREFIX.items():
        if str(mid or "").startswith(pre):
            return p
    return "deepseek"


def api_model(mid):
    pre = PREFIX.get(provider_of(mid))
    return mid[len(pre):] if pre else mid


def full_id(provider, api_id):
    return PREFIX.get(provider, "") + api_id


def usd(x):
    return f"US$ {x:,.2f}"


def name_of(provider):
    return NAMES.get(provider, provider)


def openai_limits(api_id):
    best = None
    for pre, ctx, out in _OPENAI_LIMITS:
        if api_id.startswith(pre) and (best is None or len(pre) > len(best[0])):
            best = (pre, ctx, out)
    return (best[1], best[2]) if best else (0, OPENAI_UNKNOWN_OUT)


def _openai_chat_model(mid):
    if not (mid.startswith(("gpt-", "chatgpt-")) or re.match(r"^o[134](-|$)", mid)):
        return False
    return not any(s in mid for s in _OPENAI_SKIP)


def _entries(provider, models):
    """Modelos tal como los da la API -> entradas con id prefijado, 'provider' y 'kind' remoto."""
    out = []
    for m in models:
        e = dict(m)
        e["id"] = full_id(provider, m["id"])
        e["provider"] = provider
        e["kind"] = "remote"
        out.append(e)
    return out


def fallback(provider):
    return _entries(provider, FALLBACK.get(provider) or [])


def list_models(provider, key):
    """Entradas de modelos del proveedor (ids con prefijo). Lanza dsapi.ApiError si la key o la conexión fallan."""
    if provider == "deepseek":
        return _entries("deepseek", dsapi.list_models(key))
    if provider == "anthropic":
        return _entries("anthropic", claudeapi.list_models(key))
    if provider == "openai":
        data = dsapi._get_json("/models", key, base=OPENAI_BASE, who="OpenAI")
        ids = sorted({m["id"] for m in data.get("data") or [] if _openai_chat_model(m.get("id", ""))}, reverse=True)
        if not ids:
            raise dsapi.ApiError("OpenAI no devolvió ningún modelo de chat")
        models = []
        for mid in ids:
            ctx, out = openai_limits(mid)
            models.append({"id": mid, "name": mid, "context": ctx, "max_out": out,
                           "efforts": ["low", "medium", "high"] if _OPENAI_REASONING.match(mid) else []})
        return _entries("openai", models)
    raise ValueError(f"proveedor desconocido: {provider}")


def check_key(provider, key):
    """Prueba la key. DeepSeek: devuelve el saldo ({'available', 'text'}); los demás: {'available': True, 'text': 'N modelos'}."""
    if provider == "deepseek":
        return dsapi.get_balance(key)
    if provider == ADMIN:
        return {"available": True, "text": f"Gastado este mes: {usd(claudeapi.month_cost(key))}"}
    n = len(list_models(provider, key))
    return {"available": True, "text": f"{n} modelo{'s' if n != 1 else ''} disponible{'s' if n != 1 else ''}"}


def make_stream(entry, key, messages, effort=None, max_tokens=32768, tools=None):
    """El stream del proveedor de la entrada. max_tokens se recorta a la salida máxima del modelo, si se conoce."""
    provider = entry.get("provider") or provider_of(entry["id"])
    model = api_model(entry["id"])
    cap = entry.get("max_out") or 0
    if cap:
        max_tokens = min(int(max_tokens), int(cap))
    if effort and entry.get("efforts") is not None and effort not in entry.get("efforts", []):
        effort = None                         # un esfuerzo que el modelo no acepta es un 400 seguro
    if provider == "anthropic":
        return claudeapi.ClaudeStream(key, model, messages, effort=effort, max_tokens=max_tokens, tools=tools,
                                      adaptive=entry.get("adaptive", True))
    if provider == "openai":
        return dsapi.ChatStream(key, model, messages, effort=effort, max_tokens=max_tokens, base=OPENAI_BASE,
                                tools=tools, who="OpenAI", remote=True, token_param="max_completion_tokens")
    return dsapi.ChatStream(key, model, messages, effort=effort, max_tokens=max_tokens, tools=tools)
