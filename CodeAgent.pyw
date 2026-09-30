"""Lanzador de doble clic: abre CodeAgent sin ventana de consola.

Con pythonw no hay consola donde ver un error, así que todo fallo (al arrancar o dentro de un botón) se escribe en
error.log (carpeta de datos del programa; si no se puede, en %TEMP%) y se muestra en una ventana.
"""
import os
import sys
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

# pythonw no tiene stdout/stderr: cualquier print() o aviso de una librería reventaría con AttributeError.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _log_dirs():
    for d in (APP, os.path.dirname(APP)):
        if os.path.isfile(os.path.join(d, "portable.flag")):
            yield os.path.join(d, "data")
    yield os.environ.get("TEMP") or os.environ.get("TMP") or APP


def _report(titulo, texto):
    """Escribe el error en error.log y lo muestra. Nunca lanza: es el último recurso."""
    donde = None
    for d in _log_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "error.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {titulo} ===\n{texto}\n")
            donde = p
            break
        except OSError:
            continue
    try:
        import ctypes
        msg = texto.strip().splitlines()[-1] if texto.strip() else titulo
        ctypes.windll.user32.MessageBoxW(
            0, f"{titulo}\n\n{msg}\n\n" + (f"Detalle en: {donde}" if donde else "No se pudo guardar el detalle."),
            "CodeAgent", 0x10)
    except Exception:       # noqa: BLE001
        pass


try:
    import tkinter

    def _cb_exc(self, exc, val, tb):
        _report("Error dentro de la aplicación (sigue abierta)", "".join(traceback.format_exception(exc, val, tb)))

    tkinter.Tk.report_callback_exception = _cb_exc

    import deepseek_chat
    deepseek_chat.main()
except SystemExit:
    raise
except BaseException:       # noqa: BLE001
    _report("La aplicación no pudo arrancar", traceback.format_exc())
    sys.exit(1)
