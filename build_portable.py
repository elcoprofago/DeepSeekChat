"""Arma la carpeta portable de DeepSeek Chat (para copiar a un pendrive).

Uso:   python build_portable.py [--out RUTA] [--llama-bin RUTA] [--sin-ucrt] [--sin-exe] [--sin-verificar]

Resultado (por defecto en dist\\DeepSeekChat):
    app\\       los .py de la aplicación (sin tests ni este script), su ícono y las imágenes de botones
    runtime\\   un Python recortado y propio (tkinter incluido), sin pip ni site-packages
    bin\\       llama-server.exe y sus DLL, para los modelos locales
    Models\\    aquí van los .gguf (no se toca al rearmar)
    data\\      configuración, sesiones, key cifrada, logs (no se toca al rearmar)
    DeepSeekChat.exe  lanzador nativo con el ícono (se compila de launcher\\ con las Build Tools de Visual Studio;
                      si no están, se avisa y queda solo el .bat, que hace lo mismo)
    portable.flag, DeepSeekChat.bat, DeepSeekChat-consola.bat, Diagnostico.bat, LEEME.txt
    VERSION.txt       qué versión tiene esta carpeta, para saberlo sin abrir la aplicación

Seguridad: app\\, runtime\\ y bin\\ llevan un archivo marcador; solo se reemplazan si lo tienen. Si existe una carpeta
con ese nombre SIN marcador, el script se detiene en vez de pisarla. data\\ y Models\\ nunca se borran ni se reemplazan.
"""
import argparse
import fnmatch
import glob
import os
import shutil
import subprocess
import sys
import time

import version

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = ".build_portable"
ICON = "asterisc.ico"
IMAGES = ("b-env.png", "b-stop.png")      # botón de enviar / detener de la ventana

APP_EXCLUDE = ("test_*.py", "build_portable.py", "__pycache__")
LIB_EXCLUDE_DIRS = {"site-packages", "test", "idlelib", "turtledemo", "ensurepip", "venv", "pydoc_data"}
DLL_EXCLUDE = ("_test*", "_ctypes_test*", "*.ico", "*.cat")
LLAMA_KEEP = ("llama-server.exe", "llama-server-impl.dll", "llama.dll", "llama-common.dll", "mtmd.dll", "ggml*.dll",
              "libomp*.dll", "msvcp140*.dll", "vcruntime140*.dll", "cublas*.dll", "cudart*.dll", "LICENSE*")
VC_RUNTIME = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
# Primero el build con CUDA: con una placa NVIDIA el modelo corre en la GPU (5 a 12 veces más rápido, medido); sin
# ella ggml-cuda.dll no carga y queda la CPU. El de USBagent es solo CPU (el pendrive de WinPE no tiene drivers de GPU).
LLAMA_CANDIDATES = (
    r"E:\llama-server",
    r"C:\llama-server",
    r"F:\source\repos\USBagent\bin",
    r"C:\Users\Rodolfo\source\repos\USBagent\bin",
    r"E:\Users\Rodolfo\source\repos\USBagent\bin",
)


def say(msg):
    print(msg, flush=True)


def die(msg):
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def rmtree_forced(path):
    def onerr(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onerror=onerr)


def is_managed(path):
    return os.path.isfile(os.path.join(path, MARKER))


def replace_managed(dest, fill):
    """Llena dest.new con fill(carpeta) y lo cambia por dest, solo si dest no existe o es nuestro (tiene marcador)."""
    if os.path.exists(dest) and not is_managed(dest):
        die(f"{dest} existe y no tiene el marcador {MARKER}: no lo creó este script, no se toca.")
    new, old = dest + ".new", dest + ".old"
    for leftover in (new, old):
        if os.path.exists(leftover):
            if not is_managed(leftover):
                die(f"{leftover} existe y no es de este script: no se toca.")
            rmtree_forced(leftover)
    os.makedirs(new)
    with open(os.path.join(new, MARKER), "w", encoding="utf-8") as f:
        f.write("Carpeta generada por build_portable.py; se reemplaza entera al rearmar. No pongas nada tuyo acá.\n")
    fill(new)
    if os.path.exists(dest):
        os.rename(dest, old)
    try:
        os.rename(new, dest)
    except OSError:
        if os.path.exists(old):
            os.rename(old, dest)
        raise
    if os.path.exists(old):
        rmtree_forced(old)


def copy_matching(src_dir, dst_dir, patterns=None, exclude=()):
    os.makedirs(dst_dir, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p):
            continue
        if patterns and not any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in patterns):
            continue
        if any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in exclude):
            continue
        shutil.copy2(p, os.path.join(dst_dir, name))
        n += 1
    return n


def find_ucrt():
    base = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Windows Kits", "10", "Redist")
    hits = sorted(glob.glob(os.path.join(base, "*", "ucrt", "DLLs", "x64")), reverse=True)
    return hits[0] if hits else None


def copy_ucrt(ucrt, dst):
    return copy_matching(ucrt, dst, patterns=("api-ms-win-crt-*.dll", "ucrtbase.dll"))


def fill_app(dst):
    n = 0
    for name in sorted(os.listdir(HERE)):
        p = os.path.join(HERE, name)
        if os.path.isfile(p) and (name.endswith(".py") or name.endswith(".pyw") or name == ICON or name in IMAGES):
            if any(fnmatch.fnmatch(name, pat) for pat in APP_EXCLUDE):
                continue
            shutil.copy2(p, os.path.join(dst, name))
            n += 1
    say(f"  app: {n} archivos")


def make_runtime_filler(py, ucrt):
    def fill(dst):
        for name in ("python.exe", "pythonw.exe", "python314.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
            src = os.path.join(py, name)
            if not os.path.isfile(src):
                die(f"falta {src} en la instalación de Python origen")
            shutil.copy2(src, dst)
        n = copy_matching(os.path.join(py, "DLLs"), os.path.join(dst, "DLLs"), exclude=DLL_EXCLUDE)
        lib_src, lib_dst = os.path.join(py, "Lib"), os.path.join(dst, "Lib")
        shutil.copytree(lib_src, lib_dst, ignore=lambda d, names: [x for x in names if x in LIB_EXCLUDE_DIRS and os.path.normcase(d) == os.path.normcase(lib_src)])
        for sub in ("tcl8.6", "tk8.6", "tcl8", "dde1.4", "reg1.3"):
            s = os.path.join(py, "tcl", sub)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(dst, "tcl", sub))
        if ucrt:
            copy_ucrt(ucrt, dst)
        say(f"  runtime: {n} DLL/pyd, Lib recortada, Tcl/Tk" + (", UCRT local" if ucrt else ", SIN UCRT local"))
    return fill


def make_bin_filler(src, ucrt):
    def fill(dst):
        n = copy_matching(src, dst, patterns=LLAMA_KEEP)
        if not os.path.isfile(os.path.join(dst, "llama-server.exe")):
            die(f"no hay llama-server.exe en {src}")
        # el build CUDA oficial no trae el runtime de VC++: se toma de otro build para no depender del equipo destino
        for dll in VC_RUNTIME:
            if not os.path.isfile(os.path.join(dst, dll)):
                orig = next((os.path.join(c, dll) for c in LLAMA_CANDIDATES if os.path.isfile(os.path.join(c, dll))), None)
                if orig:
                    shutil.copy2(orig, os.path.join(dst, dll))
                    n += 1
                else:
                    say(f"  AVISO: bin\\ sin {dll}; depende del runtime de VC++ del equipo destino.")
        if os.path.isfile(os.path.join(dst, "ggml-cuda.dll")):
            say("  bin: build con CUDA (usa la GPU NVIDIA si hay; si no, la CPU)")
        if ucrt:
            copy_ucrt(ucrt, dst)
        say(f"  bin: {n} archivos de llama.cpp" + (", UCRT local" if ucrt else ", SIN UCRT local"))
    return fill


def find_vcvars():
    """vcvars64.bat de alguna instalación de Visual Studio / Build Tools con el compilador de C++, o None."""
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Microsoft Visual Studio",
                           "Installer", "vswhere.exe")
    if not os.path.isfile(vswhere):
        return None
    r = subprocess.run([vswhere, "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                        "-property", "installationPath"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        bat = os.path.join(line.strip(), "VC", "Auxiliary", "Build", "vcvars64.bat")
        if os.path.isfile(bat):
            return bat
    return None


def build_launcher(out):
    """Compila launcher\\DeepSeekChat.c con el ícono embebido y lo deja como out\\DeepSeekChat.exe. Devuelve True si lo logró."""
    src = os.path.join(HERE, "launcher")
    vcvars = find_vcvars()
    if not vcvars:
        say("  AVISO: no hay Build Tools de Visual Studio con C++; se arma sin DeepSeekChat.exe (queda DeepSeekChat.bat).")
        return False
    tmp = os.path.join(out, "launcher.tmp")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    # un .bat intermedio: vcvars64 tiene que correr en el mismo cmd que rc y cl, y así no hay que pelear con comillas
    script = os.path.join(tmp, "compilar.bat")
    write_text(script, (
        "@echo off\r\n"
        f'call "{vcvars}" >nul || exit /b 1\r\n'
        f'cd /d "{src}" || exit /b 1\r\n'
        f'rc /nologo /fo "{tmp}\\DeepSeekChat.res" DeepSeekChat.rc || exit /b 1\r\n'
        f'cl /nologo /O1 /MT /utf-8 /W3 /D_CRT_SECURE_NO_WARNINGS DeepSeekChat.c "{tmp}\\DeepSeekChat.res" '
        f'/Fo"{tmp}\\\\" /Fe"{tmp}\\DeepSeekChat.exe" /link /SUBSYSTEM:WINDOWS user32.lib || exit /b 1\r\n'), newline="")
    r = subprocess.run(["cmd", "/d", "/c", script], capture_output=True, text=True, encoding="oem", errors="replace")
    exe = os.path.join(tmp, "DeepSeekChat.exe")
    if r.returncode != 0 or not os.path.isfile(exe):
        say(f"  AVISO: no se pudo compilar DeepSeekChat.exe (código {r.returncode}); queda DeepSeekChat.bat.\n"
            + (r.stdout + r.stderr).strip())
        return False
    shutil.copy2(exe, os.path.join(out, "DeepSeekChat.exe"))
    shutil.rmtree(tmp)
    say(f"  DeepSeekChat.exe: {os.path.getsize(os.path.join(out, 'DeepSeekChat.exe')) // 1024} KB, con el ícono embebido")
    return True


LAUNCHER = "@echo off\r\nstart \"\" \"%~dp0runtime\\pythonw.exe\" -I \"%~dp0app\\DeepSeekChat.pyw\"\r\n"
LAUNCHER_CONSOLE = ("@echo off\r\n\"%~dp0runtime\\python.exe\" -I \"%~dp0app\\DeepSeekChat.pyw\"\r\n"
                    "echo.\r\necho (la aplicacion se cerro; codigo %errorlevel%)\r\npause\r\n")
DIAGNOSTICO = ("@echo off\r\n\"%~dp0runtime\\python.exe\" -I \"%~dp0app\\selftest.py\"\r\n"
               "echo.\r\necho El informe quedo en la carpeta data\\diagnostico.txt\r\npause\r\n")

LEEME = """DeepSeek Chat portable
======================

Doble clic en DeepSeekChat.exe (DeepSeekChat.bat hace lo mismo, por si el .exe faltara).
La version esta en VERSION.txt y en el titulo de la ventana.

- Todo lo que la aplicacion guarda (configuracion, sesiones, tu API key cifrada, registros) queda en la carpeta data\\
  de esta misma carpeta. No escribe en el equipo donde la enchufes (ni en %APPDATA%).
- La API key: en modo portable se recomienda guardarla con contrasena (Configuracion). La opcion "atada a este Windows"
  no sirve para llevar la key a otra PC porque el cifrado depende del usuario de Windows.
- Modelos locales: copia archivos .gguf a la carpeta Models\\ (o a Models\\ en la raiz del pendrive). Aparecen solos en
  el selector de modelos. El motor es bin\\llama-server.exe.
- Si algo no arranca: Diagnostico.bat revisa el equipo y guarda un informe en data\\diagnostico.txt.
  Si la ventana no llega a abrirse, los errores quedan en data\\error.log. DeepSeekChat-consola.bat muestra los errores
  en pantalla.
- runtime\\ es un Python propio recortado (sin pip). bin\\ y runtime\\ se pueden regenerar con build_portable.py;
  data\\ y Models\\ son tuyos y el script nunca los toca.

Limites conocidos
- Probado en Windows 10/11 normal. WinPE (Ventoy/Hiren's) no se pudo verificar desde aqui: si en WinPE no abre,
  corre Diagnostico.bat y mira que linea dice FALLA.
- Los modelos locales necesitan RAM libre suficiente para el tamano del .gguf (mas el contexto).
"""

MODELS_README = ("Copia aqui los modelos locales (archivos .gguf).\r\nAparecen en el selector de modelos al abrir la aplicacion "
                 "o con el boton de recargar.\r\nSolo sirven modelos de conversacion (no los de embeddings ni los mmproj).\r\n")


def write_text(path, text, newline=None):
    with open(path, "w", encoding="ascii" if newline else "utf-8", newline=newline) as f:
        f.write(text)


def version_txt():
    """Primera línea: la que compara selftest.py. Al actualizar solo app\\ en un pendrive hay que reescribirlo también."""
    return (f"DeepSeek Chat {version.VERSION}\r\n\r\nVersion de esta carpeta. La que corre de verdad es la del titulo de la "
            "ventana;\r\nsi no coinciden, Diagnostico.bat lo avisa.\r\n")


def write_version_txt(out):
    write_text(os.path.join(out, "VERSION.txt"), version_txt(), newline="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "dist", "DeepSeekChat"))
    ap.add_argument("--llama-bin", default="")
    ap.add_argument("--sin-ucrt", action="store_true", help="no copiar el UCRT local (Windows 10 y 11 ya lo traen)")
    ap.add_argument("--sin-exe", action="store_true", help="no compilar el lanzador DeepSeekChat.exe")
    ap.add_argument("--sin-verificar", action="store_true", help="no correr el autodiagnóstico sobre lo armado")
    a = ap.parse_args()

    out = os.path.abspath(a.out)
    py = sys.base_prefix
    if not os.path.isfile(os.path.join(py, "python.exe")) or not os.path.isdir(os.path.join(py, "tcl", "tcl8.6")):
        die(f"{py} no parece una instalación completa de Python con tkinter")
    say(f"Origen de Python: {py}  ({sys.version.split()[0]})")
    say(f"Destino: {out}")

    llama = a.llama_bin or next((c for c in LLAMA_CANDIDATES if os.path.isfile(os.path.join(c, "llama-server.exe"))), "")
    if a.llama_bin and not os.path.isfile(os.path.join(llama, "llama-server.exe")):
        die(f"no hay llama-server.exe en {llama}")
    ucrt = None if a.sin_ucrt else find_ucrt()
    if not a.sin_ucrt and not ucrt:
        say("  AVISO: no se encontró el UCRT redistribuible del Windows SDK; el paquete depende del UCRT del equipo destino.")

    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    replace_managed(os.path.join(out, "app"), fill_app)
    replace_managed(os.path.join(out, "runtime"), make_runtime_filler(py, ucrt))
    if llama:
        replace_managed(os.path.join(out, "bin"), make_bin_filler(llama, ucrt))
    else:
        say("  AVISO: no se encontró llama-server.exe; se arma sin bin\\ (solo DeepSeek en la nube). Usá --llama-bin.")

    for d in ("data", "Models"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    readme = os.path.join(out, "Models", "LEEME.txt")
    if not os.path.exists(readme):
        write_text(readme, MODELS_README, newline="")
    write_text(os.path.join(out, "portable.flag"), "Presente = la aplicacion guarda todo en data\\ y no en %APPDATA%.\r\n", newline="")
    write_text(os.path.join(out, "DeepSeekChat.bat"), LAUNCHER, newline="")
    write_text(os.path.join(out, "DeepSeekChat-consola.bat"), LAUNCHER_CONSOLE, newline="")
    write_text(os.path.join(out, "Diagnostico.bat"), DIAGNOSTICO, newline="")
    write_text(os.path.join(out, "LEEME.txt"), LEEME.replace("\n", "\r\n"), newline="")    # sin newline="" quedaba \r\r\n
    write_version_txt(out)
    if not a.sin_exe:
        build_launcher(out)
    say(f"Armado en {time.time() - t0:.1f} s")

    total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(out) for f in fs)
    say(f"Tamaño total: {total / 1024 / 1024:.1f} MB")

    if a.sin_verificar:
        return
    say("Verificando (autodiagnóstico con PATH reducido y -I)...")
    env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
           "TEMP": os.environ.get("TEMP", ""), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([os.path.join(out, "runtime", "python.exe"), "-I", os.path.join(out, "app", "selftest.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=out, timeout=180)
    print(r.stdout)
    if r.stderr.strip():
        print(r.stderr)
    say(f"selftest terminó con código {r.returncode}")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
