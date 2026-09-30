"""Versión de la aplicación: se ve en el título de la ventana, en Configuración, en el diagnóstico y en VERSION.txt
de la carpeta portable, para saber qué versión tiene cada pendrive o instalación sin compararlos archivo por archivo.

Se sube en cada cambio de la aplicación (el último número; los demás, cuando el cambio lo amerite). El hook
hooks/pre-commit bloquea un commit que toca archivos de la aplicación sin subir esta línea.
"""
VERSION = "1.0.0.11"
