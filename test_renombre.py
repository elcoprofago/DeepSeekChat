"""El cambio de nombre DeepSeek Chat -> CodeAgent (1.0.0.8) sin romper las carpetas portables anteriores.

- El actualizador VIEJO (el actualizar.py de 1.0.0.7, sacado de git) acepta instalar este árbol.
- El actualizador nuevo exige CodeAgent.pyw y el puente DeepSeekChat.pyw.
- crear_lanzadores() crea los .bat que faltan y nunca pisa uno existente (control).
- El puente DeepSeekChat.pyw, corrido con Python de verdad en una raíz portable falsa, abre CodeAgent.pyw y crea los .bat.
- selftest acepta VERSION.txt con el nombre nuevo o el viejo, y avisa con cualquier otro (control).
Uso: python test_renombre.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import actualizar
import version

HERE = os.path.dirname(os.path.abspath(__file__))
V_VIEJA = "1.0.0.7"
fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


tmp = tempfile.mkdtemp(prefix="dschat_renombre_")


def zip_como_github(destino, quitar=()):
    """Un zip como el zipball de GitHub: todo dentro de <usuario>-<repo>-<commit>/."""
    with zipfile.ZipFile(destino, "w") as z:
        for n in sorted(os.listdir(HERE)):
            if os.path.isfile(os.path.join(HERE, n)) and n not in quitar:
                z.write(os.path.join(HERE, n), f"elcoprofago-DeepSeekChat-abc123/{n}")
    return destino


# --- el actualizador viejo, el que va a correr de verdad en los pendrives de 1.0.0.7
viejo_dir = os.path.join(tmp, "viejo")
os.makedirs(viejo_dir)
r = subprocess.run(["git", "show", "793fe17:actualizar.py"], cwd=HERE, capture_output=True)
check("premisa: se pudo sacar el actualizar.py de 1.0.0.7 de git", r.returncode == 0, r.stderr)
with open(os.path.join(viejo_dir, "actualizar_viejo.py"), "wb") as f:
    f.write(r.stdout)
sys.path.insert(0, viejo_dir)
import actualizar_viejo    # noqa: E402

check("premisa: el viejo exige DeepSeekChat.pyw", b'"DeepSeekChat.pyw"' in r.stdout)
zp = zip_como_github(os.path.join(tmp, "release.zip"))
dest = os.path.join(tmp, "app_viejo")
os.makedirs(dest)
try:
    n = actualizar_viejo.armar_app_nueva(zp, dest, version.VERSION)
    ok, err = True, ""
except Exception as e:      # noqa: BLE001
    ok, err, n = False, e, 0
check("el actualizador de 1.0.0.7 instala este árbol", ok, err)
check("...y trae CodeAgent.pyw, el puente y los módulos nuevos",
      all(os.path.isfile(os.path.join(dest, x)) for x in ("CodeAgent.pyw", "DeepSeekChat.pyw", "claudeapi.py", "providers.py")))
check("...sin tests", not any(x.startswith("test_") for x in os.listdir(dest)))

# --- el actualizador nuevo exige los dos lanzadores .pyw
for falta in ("CodeAgent.pyw", "DeepSeekChat.pyw"):
    zp2 = zip_como_github(os.path.join(tmp, f"sin-{falta}.zip"), quitar=(falta,))
    d2 = os.path.join(tmp, f"app_sin_{falta}")
    os.makedirs(d2)
    try:
        actualizar.armar_app_nueva(zp2, d2, version.VERSION)
        msg = ""
    except RuntimeError as e:
        msg = str(e)
    check(f"el actualizador nuevo rechaza una release sin {falta}", falta in msg, msg)
d3 = os.path.join(tmp, "app_nuevo")
os.makedirs(d3)
check("control: el actualizador nuevo instala la release completa", actualizar.armar_app_nueva(zp, d3, version.VERSION) > 0)

# --- crear_lanzadores: crea lo que falta, no pisa nada
raiz = os.path.join(tmp, "pendrive")
os.makedirs(os.path.join(raiz, "app"))
with open(os.path.join(raiz, "CodeAgent-consola.bat"), "w") as f:
    f.write("MIO")
with open(os.path.join(raiz, "DeepSeekChat.bat"), "w") as f:
    f.write("VIEJO")
creados = actualizar.crear_lanzadores(raiz)
check("crea solo el que falta", creados == ["CodeAgent.bat"], creados)
check("control: el existente no se pisa", open(os.path.join(raiz, "CodeAgent-consola.bat")).read() == "MIO")
check("control: el .bat viejo no se toca", open(os.path.join(raiz, "DeepSeekChat.bat")).read() == "VIEJO")
check("el .bat nuevo abre app\\CodeAgent.pyw con pythonw", "runtime\\pythonw.exe" in open(os.path.join(raiz, "CodeAgent.bat")).read()
      and "app\\CodeAgent.pyw" in open(os.path.join(raiz, "CodeAgent.bat")).read())
check("segunda vez: no crea nada", actualizar.crear_lanzadores(raiz) == [])

# --- el puente, con Python de verdad, en una raíz portable falsa
raiz2 = os.path.join(tmp, "pendrive2")
app2 = os.path.join(raiz2, "app")
os.makedirs(app2)
open(os.path.join(raiz2, "portable.flag"), "w").write("x")
for x in ("DeepSeekChat.pyw", "actualizar.py"):
    shutil.copy2(os.path.join(HERE, x), app2)
marca = os.path.join(tmp, "abrio.txt")
with open(os.path.join(app2, "CodeAgent.pyw"), "w", encoding="utf-8") as f:     # CodeAgent.pyw de mentira: deja una marca
    f.write(f"open({marca!r}, 'w').write(__name__ + '|' + __file__)\n")
r = subprocess.run([sys.executable, "-I", os.path.join(app2, "DeepSeekChat.pyw")], capture_output=True, text=True, timeout=60)
check("el puente termina bien", r.returncode == 0, r.stderr)
dijo = open(marca).read() if os.path.isfile(marca) else ""
check("el puente abre CodeAgent.pyw como __main__", dijo.startswith("__main__|") and dijo.endswith("CodeAgent.pyw"), dijo)
check("el puente crea los dos .bat en la raíz portable",
      all(os.path.isfile(os.path.join(raiz2, b)) for b in ("CodeAgent.bat", "CodeAgent-consola.bat")), os.listdir(raiz2))
# control: sin portable.flag no escribe nada fuera de app\
raiz3 = os.path.join(tmp, "instalado")
shutil.copytree(app2, os.path.join(raiz3, "app"))
r = subprocess.run([sys.executable, "-I", os.path.join(raiz3, "app", "DeepSeekChat.pyw")], capture_output=True, text=True, timeout=60)
check("control: sin portable.flag no crea .bat", r.returncode == 0 and os.listdir(raiz3) == ["app"], os.listdir(raiz3))

# --- selftest: los dos nombres valen, otro no
raiz4 = os.path.join(tmp, "pendrive_st")
os.makedirs(os.path.join(raiz4, "app"))
for x in ("selftest.py", "version.py"):
    shutil.copy2(os.path.join(HERE, x), os.path.join(raiz4, "app"))


def linea_version(primera):
    with open(os.path.join(raiz4, "VERSION.txt"), "w", encoding="ascii") as f:
        f.write(primera + "\n")
    r = subprocess.run([sys.executable, "-I", os.path.join(raiz4, "app", "selftest.py")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return next((l for l in r.stdout.splitlines() if "Versión" in l), r.stdout[-300:] + r.stderr[-300:])


v = version.VERSION
check("selftest acepta 'CodeAgent <v>'", linea_version(f"CodeAgent {v}").startswith("OK"))
check("selftest acepta 'DeepSeek Chat <v>' (lo escribe el actualizador de 1.0.0.7)", linea_version(f"DeepSeek Chat {v}").startswith("OK"))
check("control: selftest avisa con otra versión", linea_version(f"CodeAgent {V_VIEJA}").startswith("AVISO"))
check("VERSION.txt nuevo dice CodeAgent", actualizar.version_txt(v).startswith(f"CodeAgent {v}\r\n"))

shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
