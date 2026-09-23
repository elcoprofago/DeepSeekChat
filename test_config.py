"""Configuración portable y key con contraseña. Sin red."""
import os
import shutil
import sys
import tempfile

import dsapi
import secret

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:200]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


KEY = "sk-clave-de-prueba-1234567890abcdef"

# ---- secret.py
blob = secret.encrypt(KEY, "contraseña ñandú")
check("cifrar/descifrar con la contraseña correcta devuelve la key", secret.decrypt(blob, "contraseña ñandú") == KEY)
check("el dato cifrado no contiene la key en claro", KEY not in blob and "sk-" not in blob)
try:
    secret.decrypt(blob, "otra")
    malo = False
except secret.WrongPassword:
    malo = True
check("contraseña incorrecta: WrongPassword, no basura", malo)
check("dos cifrados de lo mismo son distintos (sal y nonce al azar)", secret.encrypt(KEY, "x") != secret.encrypt(KEY, "x"))
# alterar un solo byte del cifrado debe detectarse (la etiqueta MAC)
import base64
raw = bytearray(base64.b64decode(blob[len(secret.PREFIX):]))
raw[40] ^= 1
alt = secret.PREFIX + base64.b64encode(bytes(raw)).decode()
try:
    secret.decrypt(alt, "contraseña ñandú")
    detecta = False
except secret.WrongPassword:
    detecta = True
check("un byte alterado se detecta", detecta)
raw2 = bytearray(base64.b64decode(blob[len(secret.PREFIX):]))
raw2[0] = 30   # pide 2**30 de scrypt: no debe intentarse
try:
    secret.decrypt(secret.PREFIX + base64.b64encode(bytes(raw2)).decode(), "x")
    limita = False
except secret.WrongPassword:
    limita = True
check("un dato manipulado no puede exigir memoria sin límite", limita)
for basura in ("", "hola", secret.PREFIX + "!!!", secret.PREFIX + "AAAA"):
    try:
        secret.decrypt(basura, "x")
        ok = False
    except secret.WrongPassword:
        ok = True
    check(f"entrada basura {basura[:12]!r} -> WrongPassword", ok)
try:
    secret.encrypt(KEY, "")
    vacia = False
except ValueError:
    vacia = True
check("contraseña vacía rechazada", vacia)
check("key larga y con unicode", secret.decrypt(secret.encrypt("ñ" * 500, "p"), "p") == "ñ" * 500)

# ---- Config en modo instalado (carpeta explícita: no es portable)
d = tempfile.mkdtemp(prefix="dschat_cfg_")
c = dsapi.Config(os.path.join(d, "inst"))
check("con carpeta explícita no es portable", not c.portable and c.key_mode == "none" and c.api_key == "")
c.set_api_key(KEY)
c2 = dsapi.Config(os.path.join(d, "inst"))
check("DPAPI: la key vuelve sola al reabrir", c2.api_key == KEY and c2.key_mode == "dpapi" and not c2.needs_unlock)
txt = open(c.path, encoding="utf-8").read()
check("DPAPI: en el archivo no está la key", KEY not in txt)

# ---- Config con contraseña
c.set_api_key(KEY, password="pw")
c3 = dsapi.Config(os.path.join(d, "inst"))
check("con contraseña: al reabrir queda bloqueada", c3.key_mode == "password" and c3.needs_unlock and c3.api_key == "")
check("contraseña mala no desbloquea", c3.unlock("mala") is False and c3.api_key == "")
check("contraseña buena desbloquea", c3.unlock("pw") and c3.api_key == KEY and not c3.needs_unlock)
txt = open(c3.path, encoding="utf-8").read()
check("en el archivo no está la key ni queda la versión DPAPI", KEY not in txt and c3.data["api_key_enc"] == "")
c3.set_api_key("")
check("borrar la key limpia las dos variantes", not c3.has_key() and c3.api_key == "" and dsapi.Config(os.path.join(d, "inst")).key_mode == "none")

# ---- key solo de sesión
c4 = dsapi.Config(os.path.join(d, "ses"))
c4.set_session_key(KEY)
check("key de sesión: se usa pero no se escribe", c4.api_key == KEY and not c4.has_key() and not os.path.exists(c4.path))
c4.save()
check("key de sesión: ni siquiera tras save()", KEY not in open(c4.path, encoding="utf-8").read())

# ---- modo portable: se simula un directorio del programa con portable.flag
root = os.path.join(d, "Prog con espacios ñ")
app = os.path.join(root, "app")
os.makedirs(app)
for f in ("dsapi.py", "secret.py"):
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), app)
open(os.path.join(root, "portable.flag"), "w").write("")
prog = ("import sys; sys.path.insert(0, sys.argv[1]); import dsapi; c = dsapi.Config(); "
        "print(c.portable, c.dir); "
        "import sys as s\n"
        "try:\n c.set_api_key('k')\nexcept ValueError as e: print('EXIGE-PW')\n"
        "c.set_api_key('k', password='p'); print(c.key_mode)")
import subprocess
env = {k: v for k, v in os.environ.items() if k != "DSCHAT_CONFIG_DIR"}
r = subprocess.run([sys.executable, "-c", prog, app], capture_output=True, text=True, env=env, encoding="utf-8")
out = r.stdout.splitlines()
check("portable.flag en el padre de app\\: modo portable con data\\ junto al programa",
      out and out[0] == f"True {os.path.join(root, 'data')}", (r.stdout, r.stderr))
check("en portable, guardar sin contraseña se rechaza", "EXIGE-PW" in out, out)
check("en portable, con contraseña queda en modo password y en data\\", "password" in out and os.path.isfile(os.path.join(root, "data", "config.json")), out)
check("nada se escribió fuera de la carpeta del programa (control: %APPDATA% no cambió)",
      not os.path.exists(os.path.join(root, "..", "DeepSeekChat")))

# ---- config dañada: se aparta, no se pierde
p = os.path.join(d, "rota")
os.makedirs(p)
open(os.path.join(p, "config.json"), "w").write("{esto no es json")
c5 = dsapi.Config(p)
check("config dañada: aviso y copia apartada", c5.load_warning and os.path.exists(os.path.join(p, "config.json.dañado")))

check("defaults nuevos presentes", all(k in dsapi.Config.DEFAULTS for k in ("model_dirs", "llama_server_path", "local_ctx", "approval", "last_session")))
shutil.rmtree(d, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
