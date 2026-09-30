"""Claude por HTTP directo (claudeapi) y el registro de proveedores (providers), contra servidores de juguete: sin red
real ni keys. Cada respuesta del servidor falso sigue la forma documentada de la API de Anthropic (eventos SSE
message_start / content_block_* / message_delta / message_stop)."""
import http.server
import json
import sys
import threading

import agent as ag
import claudeapi
import dsapi
import providers

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def start(model="claude-opus-5-5", inp=100, read=0, write=0):
    return {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [], "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                     "cache_creation_input_tokens": write, "output_tokens": 1}}}


def block(i, b):
    return {"type": "content_block_start", "index": i, "content_block": b}


def delta(i, d):
    return {"type": "content_block_delta", "index": i, "delta": d}


def stop(i):
    return {"type": "content_block_stop", "index": i}


def end(reason, out=50, details=None):
    d = {"stop_reason": reason}
    if details:
        d["stop_details"] = details
    return [{"type": "message_delta", "delta": d, "usage": {"output_tokens": out}}, {"type": "message_stop"}]


def thinking_turn(i0=0, text="pienso", sig="SIG1"):
    return [block(i0, {"type": "thinking", "thinking": "", "signature": ""}), delta(i0, {"type": "thinking_delta", "thinking": text}),
            delta(i0, {"type": "signature_delta", "signature": sig}), stop(i0)]


def tool_turn(i, tid, name, args_json):
    half = len(args_json) // 2
    return [block(i, {"type": "tool_use", "id": tid, "name": name, "input": {}}),
            delta(i, {"type": "input_json_delta", "partial_json": args_json[:half]}),
            delta(i, {"type": "input_json_delta", "partial_json": args_json[half:]}), stop(i)]


class Srv:
    """Devuelve, en orden, las respuestas de la lista: (status, bytes). Guarda cada pedido (cabeceras y cuerpo)."""

    def __init__(self, respuestas, get=None):
        self.respuestas, self.pedidos, self.get = list(respuestas), [], get or {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.pedidos.append({"path": self.path, "headers": dict(self.headers)})
                body = outer.get.get(self.path.split("?")[0])
                pages = body if isinstance(body, list) else [body]
                data = pages[1 if "after_id=" in self.path and len(pages) > 1 else 0]     # 2.ª página si la piden
                raw = json.dumps(data).encode()
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.pedidos.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(n))})
                status, payload = outer.respuestas.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def correr(stream):
    ev, err = [], None
    try:
        for e in stream:
            ev.append(e)
    except dsapi.ApiError as e:
        err = e
    return ev, err


def of(ev, kind):
    return [v for k, v in ev if k == kind]


def err400(msg):
    return (400, json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": msg}}).encode())


# ================================================================ 1. traducción de mensajes
msgs = [
    {"role": "system", "content": "SOS UN AGENTE"},
    {"role": "user", "content": "hola"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call:1/x", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call:1/x", "content": "contenido"},
    {"role": "tool", "tool_call_id": "call_2", "content": "ERROR: no existe"},
    {"role": "user", "content": "seguí"},
    {"role": "assistant", "content": ""},              # vacío (p. ej. cancelado): no puede viajar
    {"role": "user", "content": "   "},                # vacío: no puede viajar
    {"role": "user", "content": "¿y?"},
]
system, out = claudeapi.convert_messages(msgs)
check("system va aparte", system == "SOS UN AGENTE" and all(m["role"] != "system" for m in out), system)
check("roles alternados, empieza y termina con el usuario", [m["role"] for m in out] == ["user", "assistant", "user"]
      and out[-1]["role"] == "user", [m["role"] for m in out])
res = out[2]["content"]
check("los dos resultados van en UN mensaje de usuario, antes que el texto",
      [b["type"] for b in res] == ["tool_result", "tool_result", "text", "text"], [b["type"] for b in res])
ids_use = [b["id"] for b in out[1]["content"] if b["type"] == "tool_use"]
ids_res = [b["tool_use_id"] for b in res if b["type"] == "tool_result"]
check("ids saneados igual en tool_use y tool_result", ids_use == ids_res == ["call_1_x", "call_2"], (ids_use, ids_res))
check("is_error solo en el resultado que empieza con ERROR", [b.get("is_error", False) for b in res[:2]] == [False, True])
check("sin bloques de texto vacíos", not any(b.get("type") == "text" and not b["text"].strip() for m in out for b in m["content"]))
check("tool_use lleva el input como objeto", out[1]["content"][0]["input"] == {"path": "a.txt"}, out[1]["content"][0])

_, out = claudeapi.convert_messages([{"role": "user", "content": "x"}, {"role": "assistant", "content": "prefill"}])
check("no termina en el asistente (la API no acepta prefill)", [m["role"] for m in out] == ["user"], out)

# _blocks: se reenvía el razonamiento firmado tal cual, con los argumentos ACTUALES de la llamada
raw_blocks = [{"type": "thinking", "thinking": "t", "signature": "S"},
              {"type": "text", "text": ""},
              {"type": "tool_use", "id": "toolu_A", "name": "write_file", "input": {"path": "x", "content": "MUY LARGO"}},
              {"type": "tool_use", "id": "toolu_GONE", "name": "list_dir", "input": {}}]
m = {"role": "assistant", "content": "", "_blocks": raw_blocks,
     "tool_calls": [{"id": "toolu_A", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "x", "content": "[omitido]"}'}}]}
_, out = claudeapi.convert_messages([{"role": "user", "content": "x"}, m,
                                     {"role": "tool", "tool_call_id": "toolu_A", "content": "OK"}])
a = out[1]["content"]
check("_blocks: el thinking firmado viaja intacto y primero", a[0] == {"type": "thinking", "thinking": "t", "signature": "S"}, a)
check("_blocks: el tool_use usa los argumentos actuales (recortados), no los viejos",
      a[1]["type"] == "tool_use" and a[1]["input"]["content"] == "[omitido]", a)
check("_blocks: sin texto vacío y sin la llamada que el historial ya no tiene", len(a) == 2, a)
check("CONTROL: la lista original de _blocks no se modificó", raw_blocks[2]["input"]["content"] == "MUY LARGO")

stripped = claudeapi.strip_thinking(out)
check("strip_thinking quita el razonamiento y deja el resto", [b["type"] for b in stripped[1]["content"]] == ["tool_use"], stripped)

t = claudeapi.convert_tools([{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}}])
check("herramientas: formato de Anthropic con streaming de argumentos",
      t == [{"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {}}, "eager_input_streaming": True}], t)

# ================================================================ 2. un turno con razonamiento, texto y herramienta
turno = sse(start(inp=10, read=80, write=5), *thinking_turn(0),
            block(1, {"type": "text", "text": ""}), delta(1, {"type": "text_delta", "text": "Voy "}),
            delta(1, {"type": "text_delta", "text": "a leer."}), stop(1),
            *tool_turn(2, "toolu_01", "read_file", '{"path": "a.txt"}'), *end("tool_use", out=42))
s = Srv([(200, turno)])
cs = claudeapi.ClaudeStream("KEY", "claude-opus-5-5", [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hola"}],
                            effort="high", max_tokens=5000, tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
                            base=s.url)
ev, err = correr(cs)
req = s.pedidos[0]
h = {k.lower(): v for k, v in req["headers"].items()}
b = req["body"]
check("sin error", err is None, err)
check("cabeceras: key, versión y betas", h.get("x-api-key") == "KEY" and h.get("anthropic-version") == "2023-06-01"
      and set(h.get("anthropic-beta", "").split(",")) == {claudeapi.BETA_BINDING, claudeapi.BETA_FALLBACK}, h)
check("CONTROL: no manda Authorization Bearer (eso es de OpenAI/DeepSeek)", "authorization" not in h, h)
check("cuerpo: system aparte, stream, caché, esfuerzo, tope",
      b["system"] == "SYS" and b["stream"] is True and b["cache_control"] == {"type": "ephemeral"}
      and b["output_config"] == {"effort": "high"} and b["max_tokens"] == 5000, b)
check("cuerpo: razonamiento adaptativo resumido con drop_block", b["thinking"] == {"type": "adaptive", "display": "summarized",
      "block_binding": {"prefix_mismatch_behavior": "drop_block"}}, b.get("thinking"))
check("cuerpo: respaldo del servidor ante negativas", b.get("fallbacks") == "default", b.get("fallbacks"))
check("eventos: razonamiento y texto en orden", of(ev, "reasoning") == ["pienso"] and "".join(of(ev, "content")) == "Voy a leer.", ev)
calls = of(ev, "tool_calls")
check("tool_calls en formato OpenAI con los argumentos armados",
      calls == [[{"id": "toolu_01", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]], calls)
check("finish: tool_use -> tool_calls", of(ev, "finish") == ["tool_calls"], ev)
u = of(ev, "usage")
check("usage: entrada = nueva + caché leída + caché escrita; aciertos = caché leída",
      u == [{"prompt_tokens": 95, "completion_tokens": 42, "prompt_cache_hit_tokens": 80}], u)
bl = of(ev, "blocks")
check("blocks: la respuesta cruda con la firma, sin campos internos",
      len(bl) == 1 and bl[0][0] == {"type": "thinking", "thinking": "pienso", "signature": "SIG1"}
      and bl[0][2] == {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "a.txt"}}, bl)
check("blocks llega antes que finish", [k for k, _ in ev].index("blocks") < [k for k, _ in ev].index("finish"))
s.close()

# Haiku 4.5: sin razonamiento adaptativo ni respaldo; entonces tampoco sus betas
s = Srv([(200, sse(start(), block(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "ok"}), stop(0), *end("end_turn")))])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-haiku-4-5", [{"role": "user", "content": "x"}], adaptive=False, base=s.url))
b = s.pedidos[0]["body"]
h = {k.lower(): v for k, v in s.pedidos[0]["headers"].items()}
check("modelo sin adaptativo: sin thinking, sin fallbacks, sin betas", "thinking" not in b and "fallbacks" not in b
      and "anthropic-beta" not in h, (b, h))
check("CONTROL: igual contesta", of(ev, "content") == ["ok"] and of(ev, "finish") == ["stop"] and err is None, (ev, err))
s.close()

# ================================================================ 3. recuperación ante un 400
hist = [{"role": "user", "content": "x"},
        {"role": "assistant", "content": "a", "_blocks": [{"type": "thinking", "thinking": "t", "signature": "VIEJA"}, {"type": "text", "text": "a"}]},
        {"role": "user", "content": "y"}]
ok_turn = sse(start(), block(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "listo"}), stop(0), *end("end_turn"))
s = Srv([err400("Invalid `signature` in `thinking` block"), (200, ok_turn)])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", hist, base=s.url))
check("firma rechazada: reintenta una vez y contesta", err is None and of(ev, "content") == ["listo"] and len(s.pedidos) == 2, (err, len(s.pedidos)))
check("firma rechazada: el primer pedido llevaba el thinking, el segundo no",
      any(b["type"] == "thinking" for b in s.pedidos[0]["body"]["messages"][1]["content"])
      and not any(b["type"] == "thinking" for b in s.pedidos[1]["body"]["messages"][1]["content"]))
check("firma rechazada: avisa", any("razonamiento" in n for n in of(ev, "notice")), of(ev, "notice"))
s.close()

s = Srv([err400("Invalid `signature` in `thinking` block"), err400("Invalid `signature` in `thinking` block")])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", hist, base=s.url))
check("CONTROL: si vuelve a fallar no reintenta en bucle", err is not None and len(s.pedidos) == 2, (err, len(s.pedidos)))
s.close()

s = Srv([err400("messages: text content blocks must be non-empty")])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", hist, base=s.url))
check("CONTROL: un 400 sin arreglo conocido falla enseguida, con el detalle", err is not None and len(s.pedidos) == 1
      and "non-empty" in str(err) and err.status == 400, (err, len(s.pedidos)))
s.close()

s = Srv([err400("thinking.block_binding: Extra inputs are not permitted"), (200, ok_turn)])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", [{"role": "user", "content": "x"}], base=s.url))
b2 = s.pedidos[1]["body"]
h2 = {k.lower(): v for k, v in s.pedidos[1]["headers"].items()}
check("block_binding no disponible: reintenta sin él y sin su beta", err is None and "block_binding" not in b2["thinking"]
      and claudeapi.BETA_BINDING not in h2.get("anthropic-beta", ""), (err, b2.get("thinking"), h2))
check("CONTROL: el reintento conserva el respaldo ante negativas", b2.get("fallbacks") == "default"
      and claudeapi.BETA_FALLBACK in h2.get("anthropic-beta", ""), h2)
s.close()

# ================================================================ 4. respaldo del servidor, negativa y error a mitad
fb = sse(start(), *thinking_turn(0, "antes"), *tool_turn(1, "toolu_X", "list_dir", "{}"),
         block(2, {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}), stop(2),
         *thinking_turn(3, "despues", "SIG2"), *tool_turn(4, "toolu_Y", "read_file", '{"path": "b"}'), *end("tool_use"))
s = Srv([(200, fb)])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", [{"role": "user", "content": "x"}], base=s.url))
bl = of(ev, "blocks")[0]
check("respaldo: solo cuenta lo posterior al cambio de modelo", [b.get("id") or b.get("signature") for b in bl] == ["SIG2", "toolu_Y"], bl)
check("respaldo: sin el bloque 'fallback' en lo que se guarda", all(b["type"] != "fallback" for b in bl))
check("respaldo: solo se ejecuta la llamada posterior", [c["id"] for c in of(ev, "tool_calls")[0]] == ["toolu_Y"], of(ev, "tool_calls"))
check("respaldo: avisa a qué modelo pasó", any("claude-opus-4-8" in n for n in of(ev, "notice")), of(ev, "notice"))
s.close()

ref = sse(start(), block(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "Empiezo"}), stop(0),
          *tool_turn(1, "toolu_Z", "run_command", '{"command": "x"'), *end("refusal", details={"category": "cyber"}))
s = Srv([(200, ref)])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", [{"role": "user", "content": "x"}], base=s.url))
check("negativa: no se ejecuta la llamada cortada", of(ev, "tool_calls") == [] and of(ev, "finish") == ["stop"], ev)
check("negativa: avisa con la categoría", any("cyber" in n for n in of(ev, "notice")), of(ev, "notice"))
check("negativa: lo guardado no tiene tool_use sin resultado", all(b["type"] != "tool_use" for b in of(ev, "blocks")[0]))
s.close()

mid = sse(start(), block(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "a"}),
          {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
s = Srv([(200, mid)])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", [{"role": "user", "content": "x"}], base=s.url))
check("error a mitad: ApiError que nombra a Claude y dice sobrecargado", err is not None and str(err).startswith("Claude:")
      and "sobrecargado" in str(err), err)
s.close()

s = Srv([(200, sse(start(), *end("max_tokens")))])
ev, err = correr(claudeapi.ClaudeStream("K", "claude-opus-5-5", [{"role": "user", "content": "x"}], base=s.url))
check("max_tokens -> finish 'length'", of(ev, "finish") == ["length"], ev)
s.close()

# ================================================================ 5. el agente devuelve el razonamiento firmado al paso siguiente
paso1 = sse(start(), *thinking_turn(0, "leo", "FIRMA-PASO-1"), *tool_turn(1, "toolu_P", "list_dir", "{}"), *end("tool_use"))
paso2 = sse(start(), block(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "fin"}), stop(0), *end("end_turn"))
s = Srv([(200, paso1), (200, paso2)])
hist = [{"role": "user", "content": "hacé algo"}]
a = ag.Agent(lambda msgs, tools: claudeapi.ClaudeStream("K", "claude-opus-5-5", msgs, tools=tools, base=s.url),
             None, keep=("_blocks",))
emitted = []
out = a.run(hist, lambda k, *v: emitted.append(k), threading.Event())
check("agente: termina", out == "done", out)
check("agente: 'blocks' no llega a la pantalla", "blocks" not in emitted, emitted)
check("agente: el mensaje guardado tiene _blocks", hist[1].get("_blocks", [{}])[0].get("signature") == "FIRMA-PASO-1", hist[1])
seg = s.pedidos[1]["body"]["messages"]
check("agente: el 2.º pedido devuelve el thinking firmado del 1.º", seg[1]["content"][0] == {"type": "thinking", "thinking": "leo",
      "signature": "FIRMA-PASO-1"}, seg[1])
check("agente: y el resultado de la herramienta como tool_result", seg[2]["content"][0]["type"] == "tool_result"
      and seg[2]["content"][0]["tool_use_id"] == "toolu_P", seg[2])
check("CONTROL: sin keep, _blocks no sale del programa (DeepSeek, OpenAI, local)",
      all("_blocks" not in m for m in ag.api_messages(hist)), ag.api_messages(hist))
s.close()

# ================================================================ 6. providers
check("provider_of/api_model: DeepSeek sin prefijo", providers.provider_of("deepseek-v4-pro") == "deepseek"
      and providers.api_model("deepseek-v4-pro") == "deepseek-v4-pro")
check("provider_of/api_model: Claude y OpenAI con prefijo",
      providers.provider_of("anthropic:claude-opus-5-5") == "anthropic" and providers.api_model("anthropic:claude-opus-5-5") == "claude-opus-5-5"
      and providers.provider_of("openai:gpt-5") == "openai" and providers.api_model("openai:gpt-5") == "gpt-5")
check("límites de OpenAI: gana el prefijo más largo", providers.openai_limits("gpt-4.1-mini") == (1047576, 32768)
      and providers.openai_limits("gpt-4o-2024") == (128000, 16384))
check("límites de OpenAI: desconocido -> contexto 0, salida acotada", providers.openai_limits("davinci") == (0, providers.OPENAI_UNKNOWN_OUT))

mods_oa = {"data": [{"id": i} for i in ("gpt-5", "gpt-4o", "o3-mini", "gpt-4o-audio-preview", "text-embedding-3-large",
                                        "dall-e-3", "gpt-4o-realtime-preview", "whisper-1", "o1", "gpt-3.5-turbo-instruct")]}
s = Srv([], get={"/v1/models": mods_oa})
providers.OPENAI_BASE = s.url
got = providers.list_models("openai", "OK")
ids = sorted(e["id"] for e in got)
check("OpenAI: solo modelos de chat, con prefijo", ids == ["openai:gpt-4o", "openai:gpt-5", "openai:o1", "openai:o3-mini"], ids)
check("OpenAI: esfuerzo solo en los de razonamiento", {e["id"]: bool(e["efforts"]) for e in got}
      == {"openai:gpt-5": True, "openai:gpt-4o": False, "openai:o3-mini": True, "openai:o1": True})
check("OpenAI: cada entrada es remota y de su proveedor", all(e["kind"] == "remote" and e["provider"] == "openai" for e in got))
check("OpenAI: Bearer", {k.lower(): v for k, v in s.pedidos[0]["headers"].items()}.get("authorization") == "Bearer OK")
s.close()

page1 = {"data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "max_input_tokens": 1000000, "max_tokens": 128000,
                   "capabilities": {"thinking": {"types": {"adaptive": {"supported": True}}},
                                    "effort": {"low": {"supported": True}, "high": {"supported": True}, "max": {"supported": False}}}}],
         "has_more": True, "last_id": "claude-opus-5-5"}
page2 = {"data": [{"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "max_input_tokens": 200000, "max_tokens": 64000,
                   "capabilities": {}}], "has_more": False, "last_id": "claude-haiku-4-5"}
s = Srv([], get={"/v1/models": [page1, page2]})
claudeapi.BASE = s.url
got = providers.list_models("anthropic", "K")
check("Claude: pagina hasta has_more=false", [e["id"] for e in got] == ["anthropic:claude-opus-5-5", "anthropic:claude-haiku-4-5"]
      and "after_id=claude-opus-5-5" in s.pedidos[1]["path"], (got, [p["path"] for p in s.pedidos]))
check("Claude: capacidades leídas de la API", got[0]["efforts"] == ["low", "high"] and got[0]["adaptive"] is True
      and got[0]["context"] == 1000000 and got[0]["max_out"] == 128000 and got[1]["adaptive"] is False and got[1]["efforts"] == [], got)
check("Claude: x-api-key, no Bearer", {k.lower(): v for k, v in s.pedidos[0]["headers"].items()}.get("x-api-key") == "K")
check("check_key de Claude cuenta los modelos", providers.check_key("anthropic", "K")["text"].startswith("2 modelos"))
s.close()
claudeapi.BASE = "https://api.anthropic.com/v1"

e = {"id": "anthropic:claude-haiku-4-5", "provider": "anthropic", "kind": "remote", "efforts": [], "max_out": 64000, "adaptive": False}
st = providers.make_stream(e, "K", [{"role": "user", "content": "x"}], effort="high", max_tokens=200000)
check("make_stream: Claude, id sin prefijo, tope y esfuerzo que el modelo no acepta fuera",
      isinstance(st, claudeapi.ClaudeStream) and st._body["model"] == "claude-haiku-4-5" and st._body["max_tokens"] == 64000
      and "output_config" not in st._body and "thinking" not in st._body, st._body)
e = {"id": "openai:gpt-5", "provider": "openai", "kind": "remote", "efforts": ["low", "medium", "high"], "max_out": 128000}
st = providers.make_stream(e, "K", [{"role": "user", "content": "x"}], effort="high", max_tokens=32768)
body = json.loads(st._req.data)
check("make_stream: OpenAI usa max_completion_tokens y reasoning_effort",
      body.get("max_completion_tokens") == 32768 and "max_tokens" not in body and body.get("reasoning_effort") == "high"
      and st._req.full_url.endswith("/chat/completions") and st._who == "OpenAI" and st._retries == dsapi.ChatStream.OPEN_RETRIES, body)
e = {"id": "deepseek-v4-pro", "provider": "deepseek", "kind": "remote", "efforts": ["high"]}
st = providers.make_stream(e, "K", [{"role": "user", "content": "x"}], effort="high", max_tokens=32768)
body = json.loads(st._req.data)
check("CONTROL: DeepSeek sigue igual que antes", st._req.full_url == dsapi.BASE + "/chat/completions"
      and body["model"] == "deepseek-v4-pro" and body["max_tokens"] == 32768 and st._who == "DeepSeek", body)

print()
print("FALLAS:", fallas or "ninguna")
sys.exit(1 if fallas else 0)
