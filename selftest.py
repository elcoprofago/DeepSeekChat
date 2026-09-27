"""Diagnóstico: comprueba en ESTA máquina lo que DeepSeek Chat necesita, sin abrir la aplicación.

Se corre con Diagnostico.bat (desde el pendrive, en Windows o en WinPE). Cada línea es OK, AVISO (algo opcional
no está) o FALLA (la aplicación no va a funcionar bien). Guarda el informe en la carpeta de datos.
Salida: 0 si no hay FALLA.
"""
import os
import platform
import subprocess
import sys
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

if sys.stdout is not None and not sys.stdout.isatty():      # redirigido a un archivo o tubería: que no salga en cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

lineas = []
fallas = 0


def out(estado, texto):
    global fallas
    if estado == "FALLA":
        fallas += 1
    lineas.append(f"{estado:<6} {texto}")
    try:
        print(lineas[-1], flush=True)
    except UnicodeEncodeError:
        print(lineas[-1].encode("ascii", "replace").decode(), flush=True)


def probar(nombre, fn, critico=True):
    try:
        r = fn()
        out("OK", f"{nombre}" + (f": {r}" if r else ""))
        return True
    except Exception as e:      # noqa: BLE001 — el diagnóstico existe para mostrar cualquier falla
        out("FALLA" if critico else "AVISO", f"{nombre}: {type(e).__name__}: {e}")
        if os.environ.get("DSCHAT_TRACE"):
            traceback.print_exc()
        return False


out("INFO", f"Python {sys.version.split()[0]} en {sys.executable}")
out("INFO", f"Windows {platform.version()} ({platform.machine()})")
out("INFO", f"Carpeta de la aplicación: {APP}")


def version_app():
    import version
    v = version.VERSION
    txt = os.path.join(os.path.dirname(APP), "VERSION.txt")
    if os.path.isfile(txt):
        with open(txt, encoding="utf-8", errors="replace") as f:
            dice = f.readline().strip()
        if dice != f"DeepSeek Chat {v}":
            raise RuntimeError(f"app\\ es {v} pero VERSION.txt dice «{dice}» (se actualizó app\\ sin reescribir VERSION.txt)")
    return v


probar("Versión", version_app, critico=False)
out("INFO", "sys.path: " + " | ".join(sys.path))


def winpe():
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\MiniNT"))
        return "SÍ: estás en WinPE (entorno de rescate)"
    except OSError:
        return "no (Windows normal)"


probar("¿Es WinPE?", winpe, critico=False)


def imports():
    import ctypes, hashlib, json, queue, ssl, threading, urllib.request  # noqa: E401,F401
    import tkinter
    import _tkinter
    return f"tkinter {tkinter.TkVersion}, {ssl.OPENSSL_VERSION}"


probar("Módulos de Python (tkinter, ssl, ctypes…)", imports)


def tcl_tk():
    import tkinter as tk
    r = tk.Tk()
    try:
        r.withdraw()
        lib = r.tk.eval("info library")
        ver = r.tk.eval("info patchlevel")
        r.update_idletasks()
        return f"Tcl/Tk {ver}, biblioteca en {lib}"
    finally:
        r.destroy()


probar("Ventana Tk (crear y destruir)", tcl_tk)


def fuentes():
    import tkinter as tk
    from tkinter import font
    r = tk.Tk()
    try:
        r.withdraw()
        fam = set(font.families(r))
    finally:
        r.destroy()
    falta = [f for f in ("Segoe UI", "Consolas") if f not in fam]
    if falta:
        raise RuntimeError("faltan " + ", ".join(falta) + " (la app usa otra de reemplazo; se ve distinta pero funciona)")
    return "Segoe UI y Consolas presentes" + ("" if "Segoe UI Emoji" in fam else "; sin Segoe UI Emoji (íconos 📎 ⚙ pueden verse como cuadros)")


probar("Fuentes", fuentes, critico=False)

try:
    import dsapi
    import localmodels
    out("OK", "Módulos de la aplicación importan")
except Exception as e:      # noqa: BLE001
    out("FALLA", f"Módulos de la aplicación: {type(e).__name__}: {e}")
    dsapi = localmodels = None

if dsapi:
    def datos():
        cfg = dsapi.Config()
        p = os.path.join(cfg.dir, "_prueba_escritura.tmp")
        with open(p, "w", encoding="utf-8") as f:
            f.write("ñ")
        with open(p, encoding="utf-8") as f:
            assert f.read() == "ñ"
        os.remove(p)
        return f"{'portable' if cfg.portable else 'instalado'}; datos en {cfg.dir}"

    probar("Carpeta de datos escribible", datos)

    def dpapi():
        assert dsapi.unprotect(dsapi.protect("prueba")) == "prueba"
        return "funciona"

    probar("DPAPI (guardar la key atada al usuario)", dpapi, critico=False)

    def contrasena():
        import secret
        blob = secret.encrypt("prueba-123", "clave1234") if hasattr(secret, "encrypt") else None
        if blob is None:
            import hashlib
            hashlib.scrypt(b"x", salt=b"y" * 16, n=2 ** 14, r=8, p=1, dklen=32)
            return "scrypt disponible"
        assert secret.decrypt(blob, "clave1234") == "prueba-123"
        return "cifrado con contraseña funciona"

    probar("Key con contraseña (scrypt)", contrasena)

    def servidor():
        exe = localmodels.find_llama_server(dsapi.Config()["llama_server_path"])
        if not exe:
            raise FileNotFoundError("no se encontró llama-server.exe (solo hace falta para modelos locales)")
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        salida = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        if r.returncode != 0:
            raise RuntimeError(f"salió con código {r.returncode}: {' / '.join(salida[-2:])}")
        return f"{exe} ({salida[0] if salida else 'sin versión'})"

    probar("llama-server.exe arranca", servidor, critico=False)

    def modelos():
        cfg = dsapi.Config()
        m = localmodels.scan_models(localmodels.default_model_dirs(cfg))
        if not m:
            raise FileNotFoundError("no hay archivos .gguf en Models\\ ni en las carpetas configuradas")
        return f"{len(m)} encontrados: " + ", ".join(x["name"] for x in m[:4])

    probar("Modelos locales", modelos, critico=False)

    def red():
        import urllib.error
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(dsapi.BASE + "/models"), timeout=10)
        except urllib.error.HTTPError as e:
            return f"api.deepseek.com responde (HTTP {e.code}: sin key es lo esperado)"
        return "api.deepseek.com responde"

    probar("Conexión a DeepSeek", red, critico=False)

out("INFO", "Resultado: " + ("hay FALLAS que impiden usar la aplicación" if fallas else "la aplicación debería funcionar (revisá los AVISO)"))
try:
    d = dsapi.Config().dir if dsapi else APP
    with open(os.path.join(d, "diagnostico.txt"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + "\n".join(lineas) + "\n")
    print("Informe guardado en", os.path.join(d, "diagnostico.txt"))
except OSError as e:
    print("No se pudo guardar el informe:", e)
sys.exit(1 if fallas else 0)
