r"""Botón «Monitor GPU»: abre gputaskman (monitor de GPU de terminal) en una consola nueva.

No depende de ningún otro repo. Busca, en este orden: el que trae la carpeta portable (bin\gputaskman\, lo pone
build_portable.py), un gputaskman.exe junto al programa o en la carpeta de arriba, el que este módulo ya bajó a la
carpeta de datos, y uno instalado en el PATH. Si no hay ninguno, baja una sola vez el ejecutable portátil oficial
(versión fija, SHA-256 comprobado) a la carpeta de datos. No usa Python ni uvx: «uvx gputaskman» fallaba en esta PC
porque no llegaba a files.pythonhosted.org.
"""
import hashlib
import os
import shutil
import subprocess
import urllib.request
import zipfile

VERSION = "0.4.3"
ZIP = f"gputaskman-{VERSION}-windows-x64.zip"
URL = f"https://github.com/shanselman/gputaskman/releases/download/v{VERSION}/{ZIP}"
SHA256 = "eeca21f9d4ee7bb892c731682356db153f13438b0897f869bedb72fafa3dae98"


class GpuMonError(Exception):
    pass


def data_exe(data_dir):
    return os.path.join(data_dir, f"gputaskman-{VERSION}", "gputaskman.exe")


def find_command(app_dir, data_dir="", which=shutil.which):
    raiz = os.path.dirname(app_dir)
    cands = [os.path.join(raiz, "bin", "gputaskman", "gputaskman.exe"),
             os.path.join(app_dir, "gputaskman.exe"), os.path.join(raiz, "gputaskman.exe")]
    if data_dir:
        cands.append(data_exe(data_dir))
    for exe in cands:
        if os.path.isfile(exe):
            return [exe]
    exe = which("gputaskman")
    return [exe] if exe else None


def download_zip(dest_zip):
    """Baja el zip oficial y comprueba su SHA-256; si no coincide lo borra y lanza GpuMonError."""
    os.makedirs(os.path.dirname(dest_zip), exist_ok=True)
    try:
        urllib.request.urlretrieve(URL, dest_zip + ".part")
    except OSError as e:
        raise GpuMonError(f"No se pudo bajar gputaskman de {URL}: {e}") from e
    os.replace(dest_zip + ".part", dest_zip)
    check_zip(dest_zip)


def check_zip(z):
    with open(z, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    if h != SHA256:
        os.remove(z)
        raise GpuMonError(f"{ZIP} no coincide con su SHA-256 publicado (bajado {h}); se borró.")


def extract(z, dst):
    """Descomprime el contenido de la carpeta raíz del zip en dst; devuelve cuántos archivos."""
    raiz = ZIP[:-4] + "/"
    n = 0
    with zipfile.ZipFile(z) as zf:
        for i in zf.infolist():
            if i.is_dir() or not i.filename.startswith(raiz) or ".." in i.filename:
                continue
            dest = os.path.join(dst, *i.filename[len(raiz):].split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(i) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    if not os.path.isfile(os.path.join(dst, "gputaskman.exe")):
        raise GpuMonError(f"{ZIP} no trae gputaskman.exe")
    return n


def install(data_dir):
    """Baja y deja gputaskman en la carpeta de datos (una sola vez). Devuelve la ruta del .exe."""
    dst = os.path.dirname(data_exe(data_dir))
    z = os.path.join(data_dir, ZIP)
    download_zip(z)
    tmp = dst + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        extract(z, tmp)
        os.replace(tmp, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(z):
            os.remove(z)
    return data_exe(data_dir)


def launch(cmd):
    # si gputaskman falla (sin GPU NVIDIA, etc.), la consola se cerraría antes de que se pueda leer el error
    linea = subprocess.list2cmdline(cmd) + ' || (echo. & echo gputaskman termino con error. & pause)'
    subprocess.Popen(f'cmd /c "{linea}"', creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
