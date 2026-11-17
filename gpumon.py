r"""Botón «Monitor GPU»: abre gputaskman (monitor de GPU de terminal) en una consola nueva.

No depende de ningún otro repo: busca, en este orden, el que trae la carpeta portable (bin\gputaskman\, lo pone
build_portable.py), un gputaskman.exe junto al programa o en la carpeta de arriba, un gputaskman instalado en el PATH, y si no hay ninguno lo corre con «uvx gputaskman»,
que lo baja de PyPI. Si tampoco hay uv, find_command devuelve None y la ventana dice qué falta.
"""
import os
import shutil
import subprocess

FALTA = ("No se encontró gputaskman.\n\nOpciones: poner gputaskman.exe (ejecutable portátil de "
         "github.com/shanselman/gputaskman/releases) junto a CodeAgent, o instalar uv "
         "(https://docs.astral.sh/uv/) para que se baje solo con «uvx gputaskman».")


def find_command(app_dir, which=shutil.which):
    raiz = os.path.dirname(app_dir)
    for exe in (os.path.join(raiz, "bin", "gputaskman", "gputaskman.exe"),
                os.path.join(app_dir, "gputaskman.exe"), os.path.join(raiz, "gputaskman.exe")):
        if os.path.isfile(exe):
            return [exe]
    exe = which("gputaskman")
    if exe:
        return [exe]
    uvx = which("uvx")
    if uvx:
        return [uvx, "gputaskman"]
    return None


def launch(app_dir):
    """Abre el monitor en consola nueva. Devuelve None si arrancó, o el texto de qué falta."""
    cmd = find_command(app_dir)
    if not cmd:
        return FALTA
    # si gputaskman falla (sin internet para uvx, sin GPU NVIDIA...), la consola se cerraría antes de leer el error
    linea = subprocess.list2cmdline(cmd) + ' || (echo. & echo gputaskman termino con error. & pause)'
    subprocess.Popen(f'cmd /c "{linea}"', creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
    return None
