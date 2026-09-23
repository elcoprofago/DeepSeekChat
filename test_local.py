"""Modelos locales: escaneo (árbol de juguete) y servidor real (llama-server + un GGUF chico). Uso: python test_local.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import dsapi
import localmodels as lm

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


MB = 1024 * 1024
d = tempfile.mkdtemp(prefix="dschat_lm_")


def mk(rel, size=2 * MB):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.seek(size - 1)
        f.write(b"\0")
    return p


mk("A/chat-7b.gguf")
mk("A/sub/otro modelo ñ.gguf")
mk("A/sub/sub2/hondo.GGUF")                 # mayúsculas: también vale
mk("A/sub/sub2/sub3/sub4/muy_hondo.gguf")   # más allá de la profundidad: no
mk("A/qwen3-embedding-0.6b.gguf")           # embeddings: no
mk("A/mmproj-vision.gguf")                  # adaptador multimodal: no
mk("A/diminuto.gguf", size=1000)            # demasiado chico para ser un modelo: no
mk("A/notas.txt")                           # no es gguf
mk("B/chat-7b.gguf")                        # mismo nombre en otra carpeta: son dos modelos distintos
res = lm.scan_models([os.path.join(d, "A"), os.path.join(d, "A"), os.path.join(d, "B"), os.path.join(d, "no existe")])
nombres = [m["name"] for m in res]
check("encuentra los modelos de chat, incluido el anidado y con acentos", {"chat-7b", "otro modelo ñ", "hondo"} <= set(nombres), nombres)
check("excluye embeddings, mmproj, archivos diminutos y no-gguf", not any(w in n for n in nombres for w in ("embedding", "mmproj", "diminuto", "notas")), nombres)
check("respeta la profundidad máxima", "muy_hondo" not in nombres, nombres)
check("carpeta repetida no duplica; mismo nombre en otra carpeta sí cuenta", nombres.count("chat-7b") == 2 and len(nombres) == 4, nombres)
check("los ids llevan la ruta completa y el prefijo local:", all(m["id"] == "local:" + m["path"] and os.path.isfile(m["path"]) for m in res))
check("carpeta inexistente no rompe", lm.scan_models(["Z:\\nada\\aqui"]) == [])

srv_exe = lm.find_llama_server(r"C:\Users\Rodolfo\source\repos\USBagent\bin\llama-server.exe")
check("find_llama_server respeta la ruta configurada", srv_exe and srv_exe.lower().endswith("llama-server.exe"), srv_exe)
check("find_llama_server con ruta mala no devuelve esa ruta", lm.find_llama_server("C:\\no\\existe\\llama-server.exe") != "C:\\no\\existe\\llama-server.exe")
check("humaniza tamaños", lm.format_size(2383309920) == "2.2 GB" and lm.format_size(369358144) == "352 MB", (lm.format_size(2383309920), lm.format_size(369358144)))

# --- memoria: falla clara ANTES de lanzar, y explicación si llama-server muere por falta de memoria
GB = 1024 * MB
real = lm.memory_status()
check("memory_status mide algo coherente", real and 0 < real["commit_libre"] and real["ram_libre"] <= real["ram_total"], real)
modelo_grande = mk("G/grande-32b.gguf", size=18 * GB + 500 * MB)   # archivo disperso: no ocupa disco real
falso_exe = mk("bin/llama-server.exe", size=1000)                  # nunca se ejecuta: el chequeo corta antes
orig_status = lm.memory_status
try:
    lm.memory_status = lambda: {"ram_libre": 24 * GB, "ram_total": 47 * GB, "commit_libre": 4 * GB}
    srv_falso = lm.LocalServer(falso_exe, os.path.join(d, "logs_falso"), ctx=2048)
    try:
        srv_falso.ensure(modelo_grande)
        e = None
    except lm.LocalError as ex:
        e = str(ex)
    check("poco commit libre: LocalError antes de lanzar, con las cifras en GB", e is not None and "18.5 GB" in e and "4.0 GB" in e and "24.0 GB" in e and srv_falso.proc is None, e)
    lm.memory_status = lambda: {"ram_libre": 24 * GB, "ram_total": 47 * GB, "commit_libre": 40 * GB}
    try:
        lm.check_memory(modelo_grande)
        e = None
    except lm.LocalError as ex:
        e = ex
    check("CONTROL: con commit de sobra el chequeo deja pasar", e is None, e)
    lm.memory_status = lambda: None
    try:
        lm.check_memory(modelo_grande)
        e = None
    except lm.LocalError as ex:
        e = ex
    check("si no se puede medir la memoria, no bloquea", e is None, e)
    # llama-server que arranca y muere con el error de memoria: el mensaje debe explicar la causa real
    bat = os.path.join(d, "bin2", "llama-server.bat")
    os.makedirs(os.path.dirname(bat), exist_ok=True)
    open(bat, "w").write("@echo off\r\necho llama_model_load: error loading model: unable to allocate CPU_REPACK buffer\r\nexit /b 1\r\n")
    pequeno = mk("P/chico.gguf", size=3 * MB)
    lm.memory_status = lambda: {"ram_libre": 24 * GB, "ram_total": 47 * GB, "commit_libre": 40 * GB}   # pasa el chequeo previo
    srv_bat = lm.LocalServer(bat, os.path.join(d, "logs_bat"), ctx=2048)
    try:
        srv_bat.ensure(pequeno, timeout=30)
        e = None
    except lm.LocalError as ex:
        e = str(ex)
    check("muere por 'unable to allocate': el mensaje explica memoria comprometida y conserva el log", e is not None and "memoria comprometida" in e and "CPU_REPACK" in e, e)
    open(bat, "w").write("@echo off\r\necho tensor 'x' has wrong shape\r\nexit /b 1\r\n")
    try:
        srv_bat.ensure(pequeno, timeout=30)
        e = None
    except lm.LocalError as ex:
        e = str(ex)
    check("CONTROL: otra causa de muerte NO se atribuye a la memoria", e is not None and "memoria comprometida" not in e and "wrong shape" in e, e)
finally:
    lm.memory_status = orig_status

MODEL = r"E:\Models\Nvidia\Qwen2.5-0.5B-Instruct-Q3_K_L.gguf"
if not os.path.isfile(MODEL) or not srv_exe:
    print("(se omite la parte del servidor real: falta el modelo o llama-server)")
else:
    logd = os.path.join(d, "logs")
    s = lm.LocalServer(srv_exe, logd, ctx=2048)
    t0 = time.time()
    base = s.ensure(MODEL)
    check("arranca el servidor y /health responde", base and s.alive(), base)
    print(f"      (arranque: {time.time() - t0:.1f} s)")
    body = json.dumps({"model": "x", "messages": [{"role": "user", "content": "Decí solo la palabra: hola"}], "max_tokens": 16}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", data=body, headers={"Content-Type": "application/json"}), timeout=120).read())
    check("EFECTO: el modelo local contesta un pedido real", bool(r["choices"][0]["message"]["content"].strip()), r)
    pid, port = s.proc.pid, s.port
    check("ensure con el mismo modelo reutiliza el proceso", s.ensure(MODEL) == base and s.proc.pid == pid)
    s.ensure(MODEL, ctx=4096)
    check("cambiar el contexto reinicia (otro proceso)", s.proc.pid != pid and s.alive())
    ppid = s.proc.pid
    s.stop()
    time.sleep(0.5)
    check("stop apaga el proceso y libera el puerto", not s.alive() and subprocess.run(["tasklist", "/FI", f"PID eq {ppid}", "/NH"], capture_output=True, text=True).stdout.find(str(ppid)) < 0)
    # error: modelo que no existe / archivo que no es un modelo
    for bad, why in ((os.path.join(d, "no_esta.gguf"), "no existe"), (mk("basura.gguf", 3 * MB), "no es un modelo válido")):
        try:
            s.ensure(bad, timeout=60)
            e = None
        except lm.LocalError as ex:
            e = ex
        check(f"modelo que {why}: LocalError claro y sin procesos colgados", e is not None and not s.alive(), e)
    # cancelar la espera
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    try:
        s.ensure(MODEL, cancel=ev)
        e = None
    except lm.LocalError as ex:
        e = ex
    check("cancelar durante la carga: LocalError y proceso apagado", e is not None and "Cancelado" in str(e) and not s.alive(), e)

    # El hijo debe morir si el programa muere a la fuerza (Job Object). Se prueba con un padre desechable.
    padre = (f"import sys; sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r}); import localmodels as lm; "
             f"s = lm.LocalServer({srv_exe!r}, {logd!r}, ctx=2048); s.ensure({MODEL!r}); print(s.proc.pid, flush=True); "
             "import time; time.sleep(600)")
    p = subprocess.Popen([sys.executable, "-c", padre], stdout=subprocess.PIPE, text=True)
    hijo = int(p.stdout.readline())
    vivo_antes = subprocess.run(["tasklist", "/FI", f"PID eq {hijo}", "/NH"], capture_output=True, text=True).stdout.find(str(hijo)) >= 0
    subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True)
    time.sleep(1.5)
    vivo_despues = subprocess.run(["tasklist", "/FI", f"PID eq {hijo}", "/NH"], capture_output=True, text=True).stdout.find(str(hijo)) >= 0
    check("CONTROL: el hijo estaba vivo mientras el padre vivía", vivo_antes)
    check("EFECTO: matar al padre a la fuerza mata a llama-server (Job Object)", not vivo_despues)
    if vivo_despues:
        subprocess.run(["taskkill", "/F", "/PID", str(hijo)], capture_output=True)

shutil.rmtree(d, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
