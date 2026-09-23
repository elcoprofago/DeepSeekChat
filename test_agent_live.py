"""Agente real (DeepSeek o modelo local) sobre un proyecto de juguete con un bug plantado.

Uso:  DEEPSEEK_API_KEY=... python test_agent_live.py [modelo] [base_url]
      modelo por defecto: deepseek-v4-pro.  Para un servidor local: python test_agent_live.py qwen http://127.0.0.1:8080/v1
Verifica el EFECTO (el archivo cambió y el programa da el resultado correcto), no lo que el modelo dice haber hecho.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time

import agent
import agent_tools as at
import dsapi

model = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4-pro"
base = sys.argv[2] if len(sys.argv) > 2 else dsapi.BASE
key = os.environ.get("DEEPSEEK_API_KEY", "") if base == dsapi.BASE else ""
if base == dsapi.BASE and not key:
    sys.exit("Falta DEEPSEEK_API_KEY")
local = base != dsapi.BASE

tmp = tempfile.mkdtemp(prefix="dschat_live_")
ws = os.path.join(tmp, "toy")
os.makedirs(os.path.join(ws, "tests"))
open(os.path.join(ws, "calc.py"), "w").write(
    "def promedio(xs):\n    return sum(xs) / (len(xs) + 1)\n\n\ndef main():\n    print(promedio([2, 4, 6]))\n\n\nif __name__ == '__main__':\n    main()\n")
open(os.path.join(ws, "tests", "test_calc.py"), "w").write(
    "import sys, os\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\nfrom calc import promedio\n\nassert promedio([2, 4, 6]) == 4, promedio([2, 4, 6])\nprint('TEST OK')\n")
open(os.path.join(ws, "CONTROL.txt"), "w").write("no me toques\n")
h_control = hashlib.sha256(open(os.path.join(ws, "CONTROL.txt"), "rb").read()).hexdigest()

pedidos = []


def confirm(kind, titulo, detalle):
    pedidos.append((kind, titulo))
    return True


tb = at.ToolBox(ws, confirm=confirm, approval="edits", backup_dir=os.path.join(tmp, "bk"))
SYS = ("You are a coding agent working inside the user's workspace folder. Inspect with the tools before answering; "
       "never guess file contents. Read a file before editing it. Answer in the user's language. Be concise.")
totales = {"in": 0, "out": 0}


def make_stream(msgs, tools):
    return dsapi.ChatStream(key, model, [{"role": "system", "content": SYS}] + msgs, None, 8192, base=base, tools=tools,
                            timeout=900 if local else 120)


log = []


def emit(kind, *a):
    if kind == "tool_start":
        log.append(f"  -> {a[1]}({str(a[2])[:110]})")
    elif kind == "tool_result":
        log.append(f"     = {a[2].splitlines()[0][:90] if a[2] else ''}")
    elif kind == "usage":
        totales["in"] += a[0].get("prompt_tokens", 0)
        totales["out"] += a[0].get("completion_tokens", 0)


msgs = [{"role": "user", "content": "El programa calc.py imprime un promedio incorrecto (debería dar 4.0 para [2,4,6]). "
                                    "Encontrá el bug, corrigilo, y comprobá con el test de la carpeta tests que pase."}]
t0 = time.time()
st = agent.Agent(make_stream, tb, max_steps=15).run(msgs, emit, threading.Event())
dt = time.time() - t0
print("\n".join(log))
print(f"\nestado={st}  pasos={len([m for m in msgs if m['role'] == 'assistant'])}  tokens in/out={totales['in']}/{totales['out']}  {dt:.0f}s  modelo={model}")
print("respuesta final:", (msgs[-1].get("content") or "")[:400].replace("\n", " "))

fallas = []


def check(n, c, d=""):
    print(("OK    " if c else "FALLA ") + n + (f"  [{d}]" if d and not c else ""))
    if not c:
        fallas.append(n)


check("terminó con respuesta final", st == "done")
check("el protocolo de mensajes quedó bien formado", agent.validate(msgs) == [], agent.validate(msgs))
r = subprocess.run([sys.executable, "calc.py"], cwd=ws, capture_output=True, text=True)
check("EFECTO: calc.py ahora imprime 4.0", r.stdout.strip() == "4.0", r.stdout + r.stderr)
r = subprocess.run([sys.executable, os.path.join("tests", "test_calc.py")], cwd=ws, capture_output=True, text=True)
check("EFECTO: el test pasa corrido por mí, no por el modelo", "TEST OK" in r.stdout, r.stdout + r.stderr)
check("dejó una copia previa de lo que editó", len(os.listdir(os.path.join(tmp, "bk"))) >= 1 if os.path.isdir(os.path.join(tmp, "bk")) else False)
check("CONTROL intacto", hashlib.sha256(open(os.path.join(ws, "CONTROL.txt"), "rb").read()).hexdigest() == h_control)
check("no tocó archivos fuera de calc.py", sorted(os.listdir(ws)) == sorted(["calc.py", "tests", "CONTROL.txt"]) or set(os.listdir(ws)) <= {"calc.py", "tests", "CONTROL.txt", "__pycache__"}, os.listdir(ws))
print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
