"""Gasto del mes en Claude (claudeapi.month_cost, informe de costos de la Admin API) y botón de adjuntar arriba de
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
from datetime import datetime, timezone
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

# ---------------------------------------------------------------- servidor simulado

pedidos = []
modo = {"v": "ok"}

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
        elif modo["v"] == "basura":
            body, code = {"data": [{"results": [{"amount": "no-es-numero"}]}], "has_more": False}, 200
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
modo["v"] = "basura"
try:
    claudeapi.month_cost("sk-ant-admin01-x", now=AHORA)
    check("monto ilegible levanta ApiError", False)
except dsapi.ApiError as e:
    check("monto ilegible levanta ApiError", "ilegible" in str(e), e)
modo["v"] = "ok"

r = providers.check_key(providers.ADMIN, "sk-ant-admin01-x")
check("check_key de la Admin key informa el gasto", r["available"] and "US$ 12.35" in r["text"], r)
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


def fake_cost(key, now=None):
    llamadas.append(key)
    v = respuesta["v"]
    if isinstance(v, Exception):
        raise v
    return v


claudeapi.month_cost = fake_cost
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
check("la barra muestra lo gastado en el mes", "US$ 12.35 gastado este mes" in txt, txt)
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
check("sin permiso: pide una Admin key", pump(lambda: "Admin key" in app.balance_lbl.cget("text")), app.balance_lbl.cget("text"))
check("log: WARN que explica la Admin key", any(l[1] == "WARN" and "Admin" in l[0] for l in app.logp.lines))
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
