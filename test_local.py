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
