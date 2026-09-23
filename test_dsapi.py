"""Pruebas de la capa de API. Uso: python test_dsapi.py   (con DEEPSEEK_API_KEY para las pruebas en vivo)."""
import json
import os
import sys
import tempfile
import threading

import dsapi

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{detalle}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


tmp = tempfile.mkdtemp(prefix="dschat_test_")

# --- DPAPI: ida y vuelta, y control de que el texto plano NO queda en el archivo
c = dsapi.Config(os.path.join(tmp, "cfg"))
SECRETO = "sk-clave-de-prueba-1234567890abcdef"
c.set_api_key(SECRETO)
check("DPAPI ida y vuelta", c.api_key == SECRETO)
check("la key no está en texto plano en config.json", SECRETO not in open(c.path, encoding="utf-8").read())
c2 = dsapi.Config(os.path.join(tmp, "cfg"))
check("la key sobrevive a recargar", c2.api_key == SECRETO)
c2.set_api_key("")
check("borrar key", not c2.has_key() and c2.api_key == "")

# --- config dañada: se aparta, no se pisa
d = os.path.join(tmp, "roto")
os.makedirs(d)
with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
    f.write("{esto no es json")
c3 = dsapi.Config(d)
check("config dañada: avisa", "dañada" in c3.load_warning)
check("config dañada: conserva copia", os.path.exists(os.path.join(d, "config.json.dañado")))
check("config dañada: usa defaults", c3["model"] == "deepseek-v4-pro")

# --- adjuntos
a = os.path.join(tmp, "a.py")
open(a, "w", encoding="utf-8").write("x = 1\n")
m = dsapi.build_message("hola", [a])
check("adjunto embebido con nombre y bloque python", "a.py" in m and "```python" in m and "x = 1" in m)
check("no filtra la ruta completa", tmp not in m)
con_fence = os.path.join(tmp, "b.md")
open(con_fence, "w", encoding="utf-8").write("texto\n```\ncodigo\n```\n")
m2 = dsapi.build_message("", [con_fence])
check("archivo con ``` dentro usa valla más larga", "````" in m2)
binario = os.path.join(tmp, "c.bin")
open(binario, "wb").write(b"abc\x00def")
try:
    dsapi.build_message("x", [binario])
    check("rechaza binario", False)
except dsapi.AttachError:
    check("rechaza binario", True)
grande = os.path.join(tmp, "g.txt")
open(grande, "w").write("a" * (dsapi.MAX_FILE_BYTES + 1))
try:
    dsapi.build_message("x", [grande])
    check("rechaza archivo grande", False)
except dsapi.AttachError:
    check("rechaza archivo grande", True)
cp = os.path.join(tmp, "cp.txt")
open(cp, "wb").write("año".encode("cp1252"))
check("lee cp1252", "año" in dsapi.build_message("", [cp]))

# --- en vivo
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not KEY:
    print("(sin DEEPSEEK_API_KEY: se omiten las pruebas en vivo)")
else:
    try:
        dsapi.get_balance("sk-invalida")
        check("key inválida da ApiError 401", False)
    except dsapi.ApiError as e:
        check("key inválida da ApiError 401", e.status == 401, str(e))

    modelos = dsapi.list_models(KEY)
    check("lista modelos", len(modelos) >= 1 and all(m["efforts"] for m in modelos), str(modelos))
    b = dsapi.get_balance(KEY)
    check("saldo disponible", b["available"] and "US$" in b["text"], str(b))

    ev = list(dsapi.ChatStream(KEY, modelos[0]["id"], [{"role": "user", "content": "Responde solo la palabra: pingpong"}], effort="low", max_tokens=400))
    tipos = {k for k, _ in ev}
    txt = "".join(v for k, v in ev if k == "content")
    check("stream: contenido, uso y fin", {"content", "usage", "finish"} <= tipos and "pingpong" in txt.lower(), f"{tipos} {txt!r}")

    try:
        list(dsapi.ChatStream(KEY, modelos[0]["id"], [{"role": "user", "content": "hola"}], effort="zzz"))
        check("effort inválido da ApiError 422", False)
    except dsapi.ApiError as e:
        check("effort inválido da ApiError 422", e.status == 422, str(e))

    # cancelar a mitad de stream desde otro hilo
    s = dsapi.ChatStream(KEY, modelos[0]["id"], [{"role": "user", "content": "Cuenta del 1 al 300 separados por comas."}], effort="low", max_tokens=4000)
    recibidos = []
    def corre():
        try:
            for k, v in s:
                recibidos.append(k)
                if len(recibidos) == 5:
                    threading.Timer(0.0, s.cancel).start()
        except Exception as e:
            recibidos.append(("EXC", repr(e)))
    t = threading.Thread(target=corre); t.start(); t.join(30)
    check("cancelar corta el stream sin excepción", not t.is_alive() and not any(isinstance(x, tuple) for x in recibidos), str(recibidos[-3:]))

print()
print("TODO OK" if not fallas else f"FALLARON {len(fallas)}: {fallas}")
sys.exit(1 if fallas else 0)
