"""Reintento al primer byte de ChatStream, contra un servidor de juguete que se cuelga a propósito (sin red real).

El servidor imita lo medido en deepseek-v4-pro: a veces acepta el pedido y no manda ni la cabecera HTTP.
"""
import http.server
import json
import threading
import time

import dsapi

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:200]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


class Srv:
    def __init__(self, cuelgues, pausa_tras_cabecera=0.0):
        self.cuelgues, self.pausa, self.pedidos = cuelgues, pausa_tras_cabecera, 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.pedidos += 1
                if outer.pedidos <= outer.cuelgues:
                    time.sleep(6)          # nunca contesta a tiempo
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                time.sleep(outer.pausa)    # pausa DESPUÉS de la cabecera (el modelo "piensa")
                for ch in ("Ho", "la"):
                    self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": ch}}]}) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def correr(srv, **kw):
    # base != dsapi.BASE se trata como local (sin reintento); se fuerzan los valores para ejercitar la lógica remota
    cs = dsapi.ChatStream("k", "m", [{"role": "user", "content": "x"}], base=srv.url, **kw)
    ev, err = [], None
    t0 = time.time()
    try:
        for e in cs:
            ev.append(e)
    except dsapi.ApiError as e:
        err = e
    return ev, err, time.time() - t0


# ---- 1. se cuelga una vez, el reintento entra
s = Srv(cuelgues=1)
ev, err, dt = correr(s, open_timeout=1, retries=2, timeout=10)
txt = "".join(v for k, v in ev if k == "content")
check("un cuelgue y reintento: llega la respuesta completa", err is None and txt == "Hola", (err, ev))
check("avisó del reintento", any(k == "notice" and "reintentando" in v for k, v in ev), ev)
check("hizo exactamente 2 pedidos", s.pedidos == 2, s.pedidos)
check("tardó ~1 s de espera, no los 6 s del cuelgue", dt < 4, dt)
s.close()

# ---- 2. se cuelga siempre: error claro tras agotar reintentos
s = Srv(cuelgues=99)
ev, err, dt = correr(s, open_timeout=1, retries=2, timeout=10, who="DeepSeek")
check("cuelgue permanente: error con timeout agotado y sugerencia", err is not None and "3 intentos" in str(err) and "flash" in str(err), err)
check("hizo 3 pedidos, no más", s.pedidos == 3, s.pedidos)
s.close()
# CONTROL: con otro proveedor no se sugiere un modelo de DeepSeek
s = Srv(cuelgues=99)
ev, err, dt = correr(s, open_timeout=1, retries=0, timeout=10, who="OpenAI")
check("CONTROL: cuelgue con OpenAI nombra a OpenAI y no sugiere deepseek-flash",
      err is not None and "OpenAI" in str(err) and "flash" not in str(err), err)
s.close()

# ---- 3. pausa larga tras la cabecera NO se corta con el timeout corto (el modelo razona un rato)
s = Srv(cuelgues=0, pausa_tras_cabecera=2.5)
ev, err, dt = correr(s, open_timeout=1, retries=2, timeout=10)
txt = "".join(v for k, v in ev if k == "content")
check("pausa de 2,5 s tras la cabecera con open_timeout de 1 s: no se corta", err is None and txt == "Hola", (err, ev))
check("y no reintentó (un solo pedido)", s.pedidos == 1, s.pedidos)
s.close()

# ---- 4. cancelar durante la espera de cabecera: no reintenta
s = Srv(cuelgues=99)
cs = dsapi.ChatStream("k", "m", [{"role": "user", "content": "x"}], base=s.url, open_timeout=1, retries=5, timeout=10)
threading.Timer(0.3, cs.cancel).start()
t0 = time.time()
try:
    ev = list(cs)
    e4 = None
except dsapi.ApiError as e:
    e4 = e
dt4 = time.time() - t0
time.sleep(1.2)
check("cancelar durante la espera: termina sin error ni eventos", e4 is None and ev == [], (e4, ev))
check("y termina enseguida, sin esperar la cabecera (open_timeout 1 s, cuelgue 6 s)", dt4 < 0.8, dt4)
check("no siguió reintentando tras cancelar", s.pedidos <= 2, s.pedidos)
s.close()

# ---- 5. un error HTTP real NO se reintenta (401 es 401)
class H401(http.server.BaseHTTPRequestHandler):
    n = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        H401.n += 1
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"bad key"}}')


h = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H401)
threading.Thread(target=h.serve_forever, daemon=True).start()
cs = dsapi.ChatStream("k", "m", [{"role": "user", "content": "x"}], base=f"http://127.0.0.1:{h.server_address[1]}/v1", open_timeout=1, retries=2)
try:
    list(cs)
    e5 = None
except dsapi.ApiError as e:
    e5 = e
check("401: error inmediato, sin reintentos", e5 is not None and e5.status == 401 and H401.n == 1, (e5, H401.n))
h.shutdown()

# ---- 6. valores por defecto: remoto corto y con reintentos; local sin cortar
r = dsapi.ChatStream("k", "m", [])
l = dsapi.ChatStream("", "m", [], base="http://127.0.0.1:1/v1", timeout=900)
check("por defecto contra DeepSeek: 25 s y 2 reintentos", (r._open_timeout, r._retries) == (25, 2), (r._open_timeout, r._retries))
check("por defecto contra local: sin corte propio ni reintentos", (l._open_timeout, l._retries) == (900, 0), (l._open_timeout, l._retries))

print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
