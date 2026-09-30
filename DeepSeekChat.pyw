"""Puente para el nombre anterior de la aplicación (DeepSeek Chat, hasta 1.0.0.7): abre CodeAgent.pyw.

Lo abren los DeepSeekChat.exe/.bat de las carpetas portables armadas antes de 1.0.0.8, que Actualizar.bat no
reemplaza (solo toca app\\). Y el Actualizar.bat de 1.0.0.7 se niega a instalar una versión que no traiga este archivo.
En una carpeta portable, de paso crea CodeAgent.bat y CodeAgent-consola.bat si faltan (nunca pisa ni borra nada).
"""
import os
import runpy

APP = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(APP)

if os.path.isfile(os.path.join(RAIZ, "portable.flag")):
    try:
        import sys
        sys.path.insert(0, APP)
        import actualizar
        actualizar.crear_lanzadores(RAIZ)
    except Exception:       # noqa: BLE001 — es una comodidad: si falla, la aplicación abre igual
        pass

runpy.run_path(os.path.join(APP, "CodeAgent.pyw"), run_name="__main__")
