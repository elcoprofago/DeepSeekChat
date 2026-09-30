"""Gasto del mes en Claude (claudeapi.month_summary: informe de costos de la Admin API + lo de hoy calculado del
informe de uso con la tabla de precios) y botón de adjuntar arriba de
Enviar. Red simulada con un servidor HTTP local: nada sale a internet.
Uso: python test_costo_claude.py
"""
import json
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import claudeapi
import dsapi
import providers

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


# ---------------------------------------------------------------- funciones puras

check("month_start: primer día del mes, UTC",
      claudeapi.month_start(datetime(2026, 9, 29, 23, 59, tzinfo=timezone.utc)) == "2026-09-01T00:00:00Z")
check("usd con separador de miles y 2 decimales", providers.usd(1234.5) == "US$ 1,234.50", providers.usd(1234.5))

# el uso real de hoy (30/9/2026, Sonnet 5.5) tal como lo devolvió usage_report; la página de facturación pasó de
# US$ 5 a 3,62 con él
FILA_REAL = {"uncached_input_tokens": 56, "cache_creation": {"ephemeral_5m_input_tokens": 247462,
             "ephemeral_1h_input_tokens": 0}, "cache_read_input_tokens": 1472630, "output_tokens": 47600,
             "server_tool_use": {"web_search_requests": 0}, "model": "claude-sonnet-5-5", "service_tier": "standard",
             "context_window": "0-200k", "inference_geo": "global", "speed": "standard", "api_key_id": None,
             "workspace_id": None}


def fila(**cambios):
    r = json.loads(json.dumps(FILA_REAL))
    r.update(cambios)
    return r


def costo(r):
    try:
        return claudeapi.row_cost(r)
    except claudeapi.Unpriced as e:
        return e


def cerca(a, b):
    return isinstance(a, float) and abs(a - b) < 1e-9


check("fila real de hoy: US$ 1.389293", cerca(costo(FILA_REAL), 1.389293), costo(FILA_REAL))
MIL = {"uncached_input_tokens": 1_000_000, "cache_creation": {}, "cache_read_input_tokens": 0, "output_tokens": 0}
check("modelo con fecha al final: precio del modelo base",
      cerca(costo(dict(MIL, model="claude-haiku-4-5-20251001")), 1.0), costo(dict(MIL, model="claude-haiku-4-5-20251001")))
check("modelo desconocido: sin precio", isinstance(costo(fila(model="claude-nuevo-9")), claudeapi.Unpriced))
check("batch: mitad de precio", cerca(costo(fila(service_tier="batch")), 1.389293 / 2), costo(fila(service_tier="batch")))
check("nivel priority: sin precio (no está en la lista)", isinstance(costo(fila(service_tier="priority")), claudeapi.Unpriced))
check("región us en un modelo 4.6+: x1,1", cerca(costo(fila(inference_geo="us")), 1.389293 * 1.1))
check("región us en Opus 4.1: sin precio", isinstance(costo(dict(MIL, model="claude-opus-4-1", inference_geo="us")),
                                                        claudeapi.Unpriced))
check("región desconocida: sin precio", isinstance(costo(fila(inference_geo="eu")), claudeapi.Unpriced))
check("modo rápido en Opus 5.5: 8 US$ el millón de entrada",
      cerca(costo(dict(MIL, model="claude-opus-5-5", speed="fast")), 8.0))
check("modo rápido en Sonnet 5.5: sin precio", isinstance(costo(fila(speed="fast")), claudeapi.Unpriced))
check("contexto largo en un 4.6+: precio normal", cerca(costo(fila(context_window="200k-1M")), 1.389293))
check("contexto largo en Sonnet 4.5: sin precio",
      isinstance(costo(dict(MIL, model="claude-sonnet-4-5", context_window="200k-1M")), claudeapi.Unpriced))
check("escritura de caché de 1 h: 2x la entrada",
      cerca(costo(dict(MIL, uncached_input_tokens=0, model="claude-opus-5-5",
                       cache_creation={"ephemeral_1h_input_tokens": 1_000_000})), 8.0))
check("búsqueda web: 1 centavo cada una",
      cerca(costo(fila(server_tool_use={"web_search_requests": 3})), 1.389293 + 0.03))
check("campo de tokens nuevo con valor: sin precio", isinstance(costo(fila(audio_tokens=5)), claudeapi.Unpriced))
check("control: campo de tokens nuevo en 0 no molesta", cerca(costo(fila(audio_tokens=0)), 1.389293))
check("herramienta de servidor nueva con uso: sin precio",
      isinstance(costo(fila(server_tool_use={"otra_requests": 1})), claudeapi.Unpriced))
check("textos de la barra: normal",
      providers.claude_cost_texts({"closed": 2.0, "today": 1.389293, "total": 3.389293, "unpriced": [],
                                   "check": "ok", "check_calc": 2.0, "check_reported": 2.0})[0]
      == "US$ 3.39 gastado este mes (hoy US$ 1.39)")

# ---------------------------------------------------------------- servidor simulado

pedidos = []
REAL_401 = "The Admin API requires an Admin API key or an organization-scoped API key."
modo = {"v": "ok"}
uso = {}      # bucket_width -> filas del informe de uso
dias = {}     # "AAAA-MM-DD" -> centavos del informe de costos (modo "dias")

PAG1 = {"data": [{"starting_at": "2026-09-01T00:00:00Z", "ending_at": "2026-09-02T00:00:00Z",
                  "results": [{"amount": "123.45", "currency": "USD"}, {"amount": "10", "currency": "USD"}]},
                 {"starting_at": "2026-09-02T00:00:00Z", "ending_at": "2026-09-03T00:00:00Z", "results": []}],
        "has_more": True, "next_page": "pag_2"}
PAG2 = {"data": [{"starting_at": "2026-09-03T00:00:00Z", "ending_at": "2026-09-04T00:00:00Z",
                  "results": [{"amount": "1101.55", "currency": "USD"}]}],
        "has_more": False, "next_page": None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        pedidos.append((u.path, q, dict(self.headers)))
        if modo["v"] == "403":
            body, code = {"type": "error", "error": {"type": "permission_error", "message": "no admin"}}, 403
        elif modo["v"] == "401":      # lo que contesta Claude de verdad a una key común (sk-ant-api03), 30/9/2026
            body, code = {"type": "error", "error": {"type": "authentication_error", "message": REAL_401}}, 401
        elif modo["v"] == "500":
            body, code = {"type": "error", "error": {"type": "api_error", "message": "boom"}}, 500
        elif modo["v"] == "basura":
            body, code = {"data": [{"results": [{"amount": "no-es-numero"}]}], "has_more": False}, 200
        elif u.path.endswith("/usage_report/messages"):
            filas = uso.get(q.get("bucket_width", ["?"])[0], [])
            body, code = {"data": [{"starting_at": q["starting_at"][0], "results": filas}], "has_more": False}, 200
        elif modo["v"] == "dias":
            body, code = {"data": [{"starting_at": d + "T00:00:00Z", "results": [{"amount": str(c)}]}
                                   for d, c in dias.items()], "has_more": False}, 200
        else:
            body, code = (PAG2 if q.get("page") == ["pag_2"] else PAG1), 200
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE_REAL = claudeapi.BASE
claudeapi.BASE = f"http://127.0.0.1:{srv.server_port}/v1"
AHORA = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

usd = claudeapi.month_cost("sk-ant-admin01-x", now=AHORA)
check("suma todas las páginas y buckets (centavos -> dólares)", abs(usd - 12.35) < 1e-9, usd)
check("dos pedidos (siguió next_page)", len(pedidos) == 2, pedidos)
path, q, hd = pedidos[0]
check("ruta del informe de costos", path == "/v1/organizations/cost_report", path)
check("desde el primer día del mes", q.get("starting_at") == ["2026-09-01T00:00:00Z"], q)
check("pide el mes entero en una página (limit=31)", q.get("limit") == ["31"], q)
check("primera página sin 'page' (control)", "page" not in q, q)
check("segunda página con el cursor", pedidos[1][1].get("page") == ["pag_2"], pedidos[1][1])
hd = {k.lower(): v for k, v in hd.items()}
check("manda la key en x-api-key", hd.get("x-api-key") == "sk-ant-admin01-x", hd)
check("manda anthropic-version", hd.get("anthropic-version") == claudeapi.API_VERSION, hd)

modo["v"] = "403"
try:
    claudeapi.month_cost("sk-ant-comun", now=AHORA)
    check("403 levanta ApiError", False)
except dsapi.ApiError as e:
    check("403 levanta ApiError con status", e.status == 403, (e.status, e))
modo["v"] = "401"
try:
    claudeapi.month_cost("sk-ant-api03-comun", now=AHORA)
    check("401 levanta ApiError", False)
except dsapi.ApiError as e:
    check("401 del informe: no dice «inválida o vencida» (la key sirve para chatear)", "inválida" not in str(e), e)
    check("401 del informe: dice que falta acceso y trae el texto de Claude",
          "no tiene acceso al informe de costos" in str(e) and REAL_401 in str(e) and e.status == 401, e)
modo["v"] = "500"
try:
    claudeapi.month_cost("sk-ant-admin01-x", now=AHORA)
    check("500 levanta ApiError", False)
except dsapi.ApiError as e:
    check("control: un 500 no se reescribe como falta de acceso", "acceso" not in str(e) and e.status == 500, e)
modo["v"] = "basura"
try:
    claudeapi.month_cost("sk-ant-admin01-x", now=AHORA)
    check("monto ilegible levanta ApiError", False)
except dsapi.ApiError as e:
    check("monto ilegible levanta ApiError", "ilegible" in str(e), e)
modo["v"] = "ok"
pedidos.clear()
check("month_cost pide hasta hoy 00:00 UTC (hoy no está cerrado)",
      claudeapi.month_cost("k", now=AHORA) and pedidos[0][1].get("ending_at") == ["2026-09-29T00:00:00Z"], pedidos[:1])

# month_summary: días cerrados del informe de costos + hoy calculado; ayer verifica los precios
modo["v"] = "dias"
dias.clear()
dias.update({"2026-09-01": 500, "2026-09-28": 138.9293})         # ayer: lo mismo que calcula FILA_REAL
uso.update({"1d": [FILA_REAL], "1h": [FILA_REAL, fila(output_tokens=0, cache_read_input_tokens=0,
                                                      cache_creation={}, uncached_input_tokens=1_000_000)]})
pedidos.clear()
s = claudeapi.month_summary("sk-ant-admin01-x", now=AHORA)
check("resumen: precios verificados contra ayer", s["check"] == "ok", s)
check("resumen: hoy = suma de las filas por hora", abs(s["today"] - (1.389293 + 2.0)) < 1e-9, s)
check("resumen: total = días cerrados + hoy", abs(s["total"] - (6.389293 + 3.389293)) < 1e-6, s)
hora = [q for pth, q, h in pedidos if pth.endswith("usage_report/messages") and q.get("bucket_width") == ["1h"]]
check("uso por hora: desde hoy 00:00 hasta ahora",
      hora and hora[0]["starting_at"] == ["2026-09-29T00:00:00Z"] and hora[0]["ending_at"] == ["2026-09-29T12:00:00Z"],
      hora)
check("uso: agrupado por modelo, nivel, región, contexto y velocidad",
      hora and sorted(hora[0].get("group_by[]", [])) == sorted(["model", "service_tier", "inference_geo",
                                                                 "context_window", "speed"]), hora)
hb = [{k.lower(): v for k, v in h.items()} for pth, q, h in pedidos if pth.endswith("usage_report/messages")]
check("uso: con el beta del modo rápido", hb and all(claudeapi.BETA_FAST in h.get("anthropic-beta", "") for h in hb), hb)
cp = [q for pth, q, h in pedidos if pth.endswith("cost_report")]
check("control: el informe de costos sin beta ni group_by", cp and "group_by[]" not in cp[0], cp)

dias["2026-09-28"] = 150                                         # Anthropic cobró otra cosa: la tabla está vieja
s = claudeapi.month_summary("k", now=AHORA)
check("precios que no coinciden: 'diferencia' y hoy no se suma",
      s["check"] == "diferencia" and abs(s["total"] - 6.5) < 1e-9, s)
bar, log, lvl = providers.claude_cost_texts(s)
check("precios que no coinciden: la barra lo dice y el log es WARN",
      "precios desactualizados" in bar and "US$ 6.50" in bar and lvl == "WARN" and claudeapi.PRICES_URL in log, (bar, log))

del dias["2026-09-28"]                                           # el informe de costos todavía no trae ayer
s = claudeapi.month_summary("k", now=AHORA)
check("ayer sin informar: se usa lo calculado", s["check"] == "ayer pendiente"
      and abs(s["closed"] - (5 + 1.389293)) < 1e-9, s)

uso["1h"] = [FILA_REAL, fila(model="claude-nuevo-9")]
s = claudeapi.month_summary("k", now=AHORA)
bar, log, lvl = providers.claude_cost_texts(s)
check("uso sin precio: no se suma y se nombra", s["unpriced"] == ["modelo claude-nuevo-9"]
      and abs(s["today"] - 1.389293) < 1e-9 and "sin precio" in bar and "claude-nuevo-9" in log and lvl == "WARN",
      (s, bar, log))

uso.clear()
dias.clear()
pedidos.clear()
s = claudeapi.month_summary("k", now=datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc))
check("día 1: sin días cerrados del mes", s["closed"] == 0 and s["check"] == "sin uso ayer", s)
cp = [q for pth, q, h in pedidos if pth.endswith("cost_report")]
check("día 1: ayer (del mes anterior) igual se pide para verificar precios",
      cp and cp[0]["starting_at"] == ["2026-09-30T00:00:00Z"], cp)
modo["v"] = "ok"

# check_key usa el reloj real: ayer es el de hoy
dias.update({(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"): 138.9293})
modo["v"] = "dias"
uso.update({"1d": [FILA_REAL], "1h": [FILA_REAL]})
r = providers.check_key(providers.ADMIN, "sk-ant-admin01-x")
check("check_key de la Admin key informa el gasto con hoy", r["available"] and "US$ 2.78" in r["text"]
      and "hoy US$ 1.39" in r["text"], r)
modo["v"] = "ok"
check("la Admin key no es un proveedor de modelos (control)", providers.ADMIN not in providers.ORDER
      and providers.ADMIN in providers.KEY_SLOTS)
claudeapi.BASE = BASE_REAL

# ---------------------------------------------------------------- en la ventana

import deepseek_chat as dc  # noqa: E402

dsapi.list_models = lambda key: dsapi.FALLBACK_MODELS
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}
providers.list_models = lambda p, key: providers.fallback(p)
llamadas = []
respuesta = {"v": 12.35}


def fake_summary(key, now=None):
    llamadas.append(key)
    v = respuesta["v"]
    if isinstance(v, Exception):
        raise v
    return {"closed": v - 1, "today": 1.0, "total": v, "unpriced": [], "check": "ok", "check_calc": 1.0,
            "check_reported": 1.0}


claudeapi.month_summary = fake_summary
abiertas = []
dc.webbrowser.open = lambda url: abiertas.append(url)

cfg = dsapi.Config(tempfile.mkdtemp(prefix="dschat_costo_"))
cfg.set_session_key("sk-deepseek")
cfg.set_session_key("sk-ant-comun", provider="anthropic")
root = tk.Tk()
root.geometry("1100x700")
app = dc.App(root, cfg, interactive=False)


def pump(cond, timeout=8):
    fin = time.time() + timeout
    while time.time() < fin:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


pump(lambda: False, 0.5)
app.sess.model = "anthropic:claude-opus-5-5"
check("con la key común consulta el gasto al arrancar", pump(lambda: "sk-ant-comun" in llamadas), llamadas)
app._update_status()
txt = app.balance_lbl.cget("text")
check("la barra muestra lo gastado en el mes, hoy incluido", "US$ 12.35 gastado este mes (hoy US$ 1.00)" in txt, txt)
check("y dice que el disponible está en la consola", "disponible" in txt and "consola" in txt, txt)
check("log: gasto de Claude", "Gasto de Claude este mes: US$ 12.35" in app.logp.text())

n = len(llamadas)
app.refresh_balance()
pump(lambda: False, 0.3)
check("no vuelve a consultar antes del minuto", len(llamadas) == n, llamadas)
app.refresh_balance(force=True)
check("refrescar (force) consulta aunque no pasó el minuto", pump(lambda: len(llamadas) == n + 1), llamadas)

app._balance_click()
check("clic en el saldo abre la facturación de Claude", abiertas == [claudeapi.BILLING_URL], abiertas)

# key común sin permiso: aviso, y no se la vuelve a probar sola
respuesta["v"] = dsapi.ApiError("La API key no tiene permiso para esto", status=403)
app.refresh_balance(force=True)
check("sin permiso: la barra dice que no está disponible por API",
      pump(lambda: "gasto: no disponible por API" in app.balance_lbl.cget("text")), app.balance_lbl.cget("text"))
check("sin permiso: la barra sigue ofreciendo el clic a la consola", "clic → consola" in app.balance_lbl.cget("text"))
warn = [l[0] for l in app.logp.lines if l[1] == "WARN"]
check("log: WARN con el error de Claude, la Admin key de organización y la página",
      warn and "no tiene permiso" in warn[-1] and "organización" in warn[-1] and claudeapi.BILLING_URL in warn[-1]
      and "Ámbito: Organización" in warn[-1], warn)
n = len(llamadas)
app._claude_cost_t = 0
app.refresh_balance()
pump(lambda: False, 0.3)
check("la key común rechazada no se reintenta sola", len(llamadas) == n, llamadas)

# con Admin key: se usa esa y no la común
respuesta["v"] = 7.0
cfg.set_session_key("sk-ant-admin01-y", provider=providers.ADMIN)
app.key_changed()
check("con Admin key consulta con ella", pump(lambda: llamadas and llamadas[-1] == "sk-ant-admin01-y"), llamadas)
check("gasto nuevo en la barra", pump(lambda: "US$ 7.00" in app.balance_lbl.cget("text")), app.balance_lbl.cget("text"))
respuesta["v"] = dsapi.ApiError("Falla del servidor de Claude", status=500)
app.refresh_balance(force=True)
check("error con Admin key: se ve como error", pump(lambda: "gasto: error" in app.balance_lbl.cget("text")),
      app.balance_lbl.cget("text"))

# con un modelo local: la barra muestra igual los saldos ya consultados (antes decía solo «n/a (modelo local)»)
respuesta["v"] = 7.0
app.refresh_balance(force=True)
pump(lambda: "US$ 7.00" in app.claude_cost_text and app.balance_text == "US$ 4.20")
app.sess.model = "local:X:\\no-existe\\modelo.gguf"
app._update_status()
txt = app.balance_lbl.cget("text")
check("modelo local: dice que es local", "modelo local" in txt, txt)
check("modelo local: muestra el saldo de DeepSeek", "DeepSeek US$ 4.20" in txt, txt)
check("modelo local: muestra el gasto de Claude", "Claude US$ 7.00 gastado este mes" in txt, txt)
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 5.00"}
app.refresh_balance(force=True)
check("modelo local: refrescar actualiza la barra", pump(lambda: "DeepSeek US$ 5.00" in app.balance_lbl.cget("text")),
      app.balance_lbl.cget("text"))
dsapi.get_balance = lambda key: {"available": True, "text": "US$ 4.20"}
app.refresh_balance(force=True)
pump(lambda: app.balance_text == "US$ 4.20")
bt, ct = app.balance_text, app.claude_cost_text
app.balance_text, app.claude_cost_text = "—", ""
app._update_status()
check("control: sin saldos consultados queda solo «n/a (modelo local)»",
      app.balance_lbl.cget("text") == "Saldo: n/a (modelo local)", app.balance_lbl.cget("text"))
app.balance_text, app.claude_cost_text = bt, ct

# control: con un modelo de DeepSeek sigue el saldo de DeepSeek, y el clic no abre nada
app.sess.model = "deepseek-v4-pro"
abiertas.clear()
app._update_status()
txt = app.balance_lbl.cget("text")
check("control: DeepSeek sigue mostrando su saldo", "US$ 4.20" in txt and "gastado" not in txt, txt)
app._balance_click()
check("control: con DeepSeek el clic no abre la consola", abiertas == [], abiertas)

# ---------------------------------------------------------------- botón de adjuntar

root.update()
check("adjuntar y enviar en la misma columna", app.btn_plus.master is app.btn_send.master)
check("adjuntar va arriba de enviar", app.btn_plus.winfo_y() + app.btn_plus.winfo_height() <= app.btn_send.winfo_y(),
      (app.btn_plus.winfo_y(), app.btn_plus.winfo_height(), app.btn_send.winfo_y()))
check("adjuntar lleva el ícono del clip", bool(app.img_clip) and str(app.btn_plus.cget("image")) == str(app.img_clip))
check("la columna está a la derecha del cuadro de texto", app.send_col.winfo_x() > app.input.winfo_x())
abrio = []
app.add_files = lambda: abrio.append(1)
app.btn_plus.configure(command=lambda: app.add_files())
app.btn_plus.invoke()
check("el clip abre el selector de archivos", abrio == [1])
root.geometry("520x600")
pump(lambda: False, 0.4)
check("ventana angosta: los botones siguen enteros", app.send_col.winfo_width() >= app.btn_send.winfo_reqwidth()
      and app.send_col.winfo_x() + app.send_col.winfo_width() <= app.bottom.winfo_width() + 1,
      (app.send_col.winfo_x(), app.send_col.winfo_width(), app.bottom.winfo_width()))

app.on_close()
srv.shutdown()
print()
print("TODO OK" if not fallas else f"{len(fallas)} FALLA(S): {fallas}")
sys.exit(1 if fallas else 0)
