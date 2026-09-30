"""Actualizador de la carpeta portable: baja la última release de GitHub y reemplaza app\\ por la versión nueva.

Uso:   Actualizar.bat  (en la raíz de la carpeta portable; corre este archivo con runtime\\python.exe)

Solo toca app\\ y VERSION.txt. data\\ (configuración, sesiones, key), Models\\, runtime\\ y bin\\ no se tocan. La app\\
anterior queda al lado como app.respaldo-<fecha>-antes-<versión>, y si algo falla a mitad de camino se vuelve a ella.

build_portable.py usa es_archivo_de_app() y version_txt() de acá: qué entra en app\\ se decide en un solo lugar.
"""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = "elcoprofago/DeepSeekChat"
API_ULTIMA = f"https://api.github.com/repos/{REPO}/releases/latest"
MARKER = ".build_portable"
ICONO = "asterisc.ico"
IMAGENES = ("b-env.png", "b-stop.png")      # botón de enviar / detener de la ventana
EXCLUIR_PREFIJO = "test_"
EXCLUIR = ("build_portable.py",)

APP = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(APP)

# Lanzadores .bat de la raíz de la carpeta portable. Los escribe build_portable.py y, si faltan, crear_lanzadores():
# una carpeta armada antes de 1.0.0.8 (cuando la app se llamaba DeepSeek Chat) solo tiene DeepSeekChat.bat/.exe, y
# actualizar reemplaza app\ y nada más. Los viejos siguen andando: app\DeepSeekChat.pyw queda como puente.
LANZADOR = "@echo off\r\nstart \"\" \"%~dp0runtime\\pythonw.exe\" -I \"%~dp0app\\CodeAgent.pyw\"\r\n"
LANZADOR_CONSOLA = ("@echo off\r\n\"%~dp0runtime\\python.exe\" -I \"%~dp0app\\CodeAgent.pyw\"\r\n"
                    "echo.\r\necho (la aplicacion se cerro; codigo %errorlevel%)\r\npause\r\n")
LANZADORES = {"CodeAgent.bat": LANZADOR, "CodeAgent-consola.bat": LANZADOR_CONSOLA}


def es_archivo_de_app(nombre):
    """True si un archivo de la raíz del repo va a app\\ en la carpeta portable."""
    if nombre.startswith(EXCLUIR_PREFIJO) or nombre in EXCLUIR:
        return False
    return nombre.endswith((".py", ".pyw")) or nombre == ICONO or nombre in IMAGENES


def version_txt(v):
    """Primera línea: la que compara selftest.py. Al actualizar solo app\\ hay que reescribirlo también."""
    return (f"CodeAgent {v}\r\n\r\nVersion de esta carpeta. La que corre de verdad es la del titulo de la "
            "ventana;\r\nsi no coinciden, Diagnostico.bat lo avisa.\r\n")


def ocr_txt(raiz):
    """Línea de VERSION.txt sobre el OCR: se mira el disco, así sigue siendo cierta al actualizar solo app\\."""
    if os.path.isfile(os.path.join(raiz, "bin", "tesseract", "tesseract.exe")):
        return "OCR de imagenes: incluido (bin\\tesseract, espanol e ingles).\r\n"
    return "OCR de imagenes: no incluido en este portable.\r\n"


def escribir_version_txt(raiz, v):
    with open(os.path.join(raiz, "VERSION.txt"), "w", encoding="ascii", newline="") as f:
        f.write(version_txt(v) + "\r\n" + ocr_txt(raiz))


def crear_lanzadores(raiz):
    """Crea los .bat de CodeAgent que falten en la raíz portable. Nunca pisa ni borra uno existente. Devuelve los creados."""
    creados = []
    for nombre, texto in LANZADORES.items():
        p = os.path.join(raiz, nombre)
        if not os.path.exists(p):
            with open(p, "w", encoding="ascii", newline="") as f:
                f.write(texto)
            creados.append(nombre)
    return creados


def clave(v):
    """'1.0.0.10' -> (1, 0, 0, 10), para comparar versiones como números y no como texto."""
    return tuple(int(x) for x in v.strip().lstrip("vV").split("."))


def version_instalada():
    with open(os.path.join(APP, "version.py"), encoding="utf-8") as f:
        for linea in f:
            if linea.startswith("VERSION = "):
                return linea.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("app\\version.py no tiene la línea VERSION")


def procesos_de_esta_carpeta():
    """PIDs de la app corriendo desde el runtime\\ de esta carpeta (sin contar este proceso)."""
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    pids = (ctypes.c_ulong * 4096)()
    usado = ctypes.c_ulong()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(usado)):
        return []
    runtime = os.path.normcase(os.path.join(RAIZ, "runtime")) + os.sep
    propios = []
    for pid in pids[:usado.value // ctypes.sizeof(ctypes.c_ulong)]:
        if pid in (0, os.getpid()):
            continue
        h = k32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.c_ulong(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                if os.path.normcase(buf.value).startswith(runtime):
                    propios.append(pid)
        finally:
            k32.CloseHandle(h)
    return propios


def pedir(url, destino=None):
    req = urllib.request.Request(url, headers={"User-Agent": "CodeAgent-actualizador",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if destino is None:
            return json.loads(r.read().decode("utf-8"))
        with open(destino, "wb") as f:
            shutil.copyfileobj(r, f)


def armar_app_nueva(zip_path, dest, v_esperada):
    """Extrae del zip del código fuente solo lo que va en app\\ y comprueba que sea la versión anunciada."""
    with zipfile.ZipFile(zip_path) as z:
        n = 0
        for info in z.infolist():
            partes = info.filename.split("/")
            # el zip de GitHub trae todo dentro de una carpeta <usuario>-<repo>-<commit>/; app\ sale de su raíz
            if len(partes) != 2 or not partes[1] or not es_archivo_de_app(partes[1]):
                continue
            with z.open(info) as src, open(os.path.join(dest, partes[1]), "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    # DeepSeekChat.pyw: el puente que abren los DeepSeekChat.exe/.bat de las carpetas armadas antes de 1.0.0.8
    for obligatorio in ("CodeAgent.pyw", "DeepSeekChat.pyw", "deepseek_chat.py", "version.py", "actualizar.py"):
        if not os.path.isfile(os.path.join(dest, obligatorio)):
            raise RuntimeError(f"la release no trae {obligatorio}: no se instala")
    with open(os.path.join(dest, "version.py"), encoding="utf-8") as f:
        v_zip = next((l.split("=", 1)[1].strip().strip("\"'") for l in f if l.startswith("VERSION = ")), "")
    if clave(v_zip) != clave(v_esperada):
        raise RuntimeError(f"la release se llama {v_esperada} pero su version.py dice {v_zip}: no se instala")
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as f:
        f.write("Carpeta generada por build_portable.py; se reemplaza entera al rearmar. No pongas nada tuyo acá.\n")
    return n


def main():
    print("CodeAgent - actualizador\n")
    if not os.path.isfile(os.path.join(RAIZ, "portable.flag")):
        print("Esta no es una carpeta portable (falta portable.flag). No se toca nada.")
        return 1
    actual = version_instalada()
    print(f"Versión instalada: {actual}")
    print("Consultando la última versión publicada...")
    try:
        rel = pedir(API_ULTIMA)
    except Exception as e:          # sin red, GitHub caído, límite de consultas: se informa y no se toca nada
        print(f"No se pudo consultar GitHub: {e}")
        return 1
    nueva = str(rel.get("tag_name", "")).lstrip("vV")
    print(f"Última versión publicada: {nueva}")
    if clave(nueva) <= clave(actual):
        for b in crear_lanzadores(RAIZ):
            print(f"Creado {b} (el lanzador con el nombre nuevo de la aplicación).")
        print("\nYa tenés la última versión. No hay nada que hacer.")
        return 0

    corriendo = procesos_de_esta_carpeta()
    if corriendo:
        print(f"\nCodeAgent está abierto desde esta carpeta (proceso {', '.join(map(str, corriendo))}).")
        print("Cerralo y volvé a correr Actualizar.bat.")
        return 1

    tmp = tempfile.mkdtemp(prefix="dschat_update_")
    try:
        zip_path = os.path.join(tmp, "release.zip")
        print("Descargando...")
        pedir(rel["zipball_url"], zip_path)
        nueva_app = os.path.join(RAIZ, "app.new")
        if os.path.exists(nueva_app):
            shutil.rmtree(nueva_app)
        os.makedirs(nueva_app)
        n = armar_app_nueva(zip_path, nueva_app, nueva)
        respaldo = os.path.join(RAIZ, f"app.respaldo-{time.strftime('%Y%m%d-%H%M%S')}-antes-{actual}")
        os.rename(APP, respaldo)
        try:
            os.rename(nueva_app, APP)
            escribir_version_txt(RAIZ, nueva)
        except OSError:
            if os.path.exists(APP):
                shutil.rmtree(APP)
            os.rename(respaldo, APP)
            raise
    except Exception as e:
        print(f"\nFALLÓ la actualización: {e}\nLa versión {actual} sigue instalada, sin cambios.")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nListo: {actual} -> {nueva} ({n} archivos).")
    for b in crear_lanzadores(RAIZ):
        print(f"Creado {b} (el lanzador con el nombre nuevo de la aplicación).")
    print(f"La versión anterior quedó en {os.path.basename(respaldo)} (se puede borrar cuando confirmes que anda).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
