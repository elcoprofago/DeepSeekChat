"""Diagnóstico de conexión remota (remote.diagnose y el botón de RemoteDialog): sin red real ni comandos del
sistema de verdad, salvo los dos casos marcados "real" que sí abren un socket (para probar la lógica, no el SO)."""
import os
import shutil
import tempfile
import threading
import time
import tkinter as tk

import deepseek_chat as dc
import dialogs
import dsapi
import remote

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


def niveles(filas):
    return [n for n, _ in filas]


# ================================================================ 1. cada paso, por separado

# ---- puerto: uno real, abierto en localhost, y uno libre (nadie escucha)
srv_sock = __import__("socket").socket()
srv_sock.bind(("127.0.0.1", 0))
srv_sock.listen(1)
puerto_abierto = srv_sock.getsockname()[1]
puerto_libre = puerto_abierto + 1 if puerto_abierto < 65535 else puerto_abierto - 1
while True:
    s = __import__("socket").socket()
    try:
        s.bind(("127.0.0.1", puerto_libre))
        s.close()
        break
    except OSError:
        puerto_libre += 1
    finally:
        try:
            s.close()
        except OSError:
            pass

check("servidor: OK si algo escucha en el puerto", remote._diag_server(puerto_abierto)[0] == "OK")
check("servidor: FALLA si no hay nada escuchando", remote._diag_server(puerto_libre)[0] == "FALLA")
srv_sock.close()

# ---- _run: decodifica bytes aunque no sean UTF-8 (netsh en una Windows en español)
orig_run = __import__("subprocess").run


class _FakeCompleted:
    def __init__(self, out):
        self.stdout, self.stderr = out, b""


def fake_run_cp850(args, **kw):
    return _FakeCompleted("Perfil privado: Habilitada: Sí".encode("cp850"))


import subprocess as _sp
_sp.run = fake_run_cp850
try:
    out = remote._run(["lo-que-sea"])
finally:
    _sp.run = orig_run
check("_run: decodifica CP850 sin reventar", out is not None and "Sí" in out, out)


def fake_run_none(args, **kw):
    raise FileNotFoundError("no existe")


_sp.run = fake_run_none
try:
    check("_run: None si el comando no existe", remote._run(["lo-que-sea"]) is None)
finally:
    _sp.run = orig_run

# ---- perfil de red: se controla _run_ps (PowerShell), sin tocar la red real de la máquina
_orig_run_ps = remote._run_ps
remote._run_ps = lambda script, timeout=20: "Wi-Fi|Private\nEthernet|Public"
try:
    niv, txt = remote._diag_network_profile()
    check("perfil de red: OK si hay alguna Private/DomainAuthenticated", niv == "OK" and "Wi-Fi" in txt, txt)
finally:
    remote._run_ps = _orig_run_ps

remote._run_ps = lambda script, timeout=20: "Ethernet|Public"
try:
    niv, txt = remote._diag_network_profile()
    check("perfil de red: FALLA si todas son públicas", niv == "FALLA", txt)
finally:
    remote._run_ps = _orig_run_ps

remote._run_ps = lambda script, timeout=20: None
try:
    check("perfil de red: AVISO si no se pudo consultar", remote._diag_network_profile()[0] == "AVISO")
finally:
    remote._run_ps = _orig_run_ps

# ---- firewall: regla de entrada habilitada o no, para el ejecutable
remote._run_ps = lambda script, timeout=20: "Inbound|True|Allow"
try:
    check("firewall: OK con una regla de entrada habilitada", remote._diag_firewall("C:\\x\\pythonw.exe")[0] == "OK")
finally:
    remote._run_ps = _orig_run_ps

remote._run_ps = lambda script, timeout=20: ""
try:
    check("firewall: FALLA sin ninguna regla", remote._diag_firewall("C:\\x\\pythonw.exe")[0] == "FALLA")
finally:
    remote._run_ps = _orig_run_ps

remote._run_ps = lambda script, timeout=20: "Outbound|True|Allow"     # de salida, no de entrada: no cuenta
try:
    check("firewall: una regla de SALIDA no cuenta como protección de entrada", remote._diag_firewall("py.exe")[0] == "FALLA")
finally:
    remote._run_ps = _orig_run_ps

# ---- Tailscale: con y sin IP asignada
_orig_ts_ip, _orig_run = remote.tailscale_ip, remote._run
remote.tailscale_ip = lambda: "100.64.1.2"
remote._run = lambda args, timeout=5: "100.64.1.2   mipc   win   active; relay \"derp\""
try:
    check("tailscale: OK con IP asignada", remote._diag_tailscale()[0] == "OK")
finally:
    remote.tailscale_ip, remote._run = _orig_ts_ip, _orig_run

remote.tailscale_ip = lambda: ""
try:
    check("tailscale: AVISO sin IP de Tailscale", remote._diag_tailscale()[0] == "AVISO")
finally:
    remote.tailscale_ip = _orig_ts_ip

# ================================================================ 2. diagnose(): arma la lista completa
remote._diag_server = lambda port: ("OK", "puerto")
remote._diag_self_connect = lambda ip, port, token: ("OK", "self")
remote._diag_network_profile = lambda: ("OK", "red")
remote._diag_firewall = lambda exe: ("FALLA", "firewall")
remote._diag_tailscale = lambda: ("AVISO", "tailscale")
filas = remote.diagnose(8765, "tok", lan="192.168.1.5")
check("diagnose: un resultado por cada paso, en orden, con la IP primero",
      niveles(filas) == ["INFO", "OK", "OK", "OK", "FALLA", "AVISO"] and "192.168.1.5" in filas[0][1], filas)

# ================================================================ 3. el botón en RemoteDialog
tmp = tempfile.mkdtemp()
cfg = dsapi.Config(os.path.join(tmp, "cfg"))
cfg["remote_port"] = 0
root = tk.Tk()
app = dc.App(root, cfg, interactive=False)


def pump(cond=lambda: False, t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


d = dialogs.RemoteDialog(app)
d.diagnose()
check("sin servidor encendido: avisa y no corre nada", d.msg.cget("text").startswith("Encendé"))

app.set_remote(True)
d2 = dialogs.RemoteDialog(app)
remote.diagnose = lambda port, token, lan=None: [("OK", "todo bien"), ("FALLA", "ojo con esto")]
d2.diagnose()
check("mientras corre: el botón se deshabilita", str(d2.btn_diag["state"]) == "disabled")
ok = pump(lambda: str(d2.btn_diag["state"]) == "normal", 5)
check("EFECTO: al terminar, el botón se vuelve a habilitar", ok)
texto = d2.diag_box.get("1.0", "end")
check("EFECTO: el resultado se ve en el cuadro, con cada nivel", "todo bien" in texto and "ojo con esto" in texto, texto)

d2.win.destroy()
app.on_close()
shutil.rmtree(tmp, ignore_errors=True)

print("\nFALLAS:", fallas if fallas else "ninguna")
