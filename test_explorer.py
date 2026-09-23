"""explorer: carga perezosa, orden, ocultos, refresco que conserva lo abierto."""
import os
import tempfile
import tkinter as tk
from tkinter import ttk

import explorer

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


tmp = tempfile.mkdtemp(prefix="dschat_exp_")
ws = os.path.join(tmp, "proyecto con ñ")
for d in ("src/util", "docs", ".git/objects", "__pycache__", "vacia"):
    os.makedirs(os.path.join(ws, d))
for f in ("README.md", "src/main.py", "src/util/helpers.py", "docs/guia.md", ".git/HEAD", "zeta.txt", "Alfa.txt"):
    open(os.path.join(ws, f), "w").write("x")

abiertos, adjuntos, pidio_carpeta = [], [], []
root = tk.Tk()
ttk.Style(root).theme_use("clam")
ex = explorer.Explorer(root, abiertos.append, adjuntos.append, lambda: pidio_carpeta.append(1))
ex.frame.pack(fill="both", expand=True)
root.update()

check("sin carpeta: mensaje y árbol vacío", ex.tree.get_children() == () and "Sin carpeta" in ex.path_lbl.cget("text"))

ex.set_root(ws)
root.update()
top = [ex.tree.item(i, "text") for i in ex.tree.get_children()]
check("primer nivel: carpetas primero, luego archivos, sin distinguir mayúsculas", top == ["docs", "src", "vacia", "Alfa.txt", "README.md", "zeta.txt"], top)
check("oculta .git y __pycache__", ".git" not in top and "__pycache__" not in top, top)
check("el título es el nombre de la carpeta", ex.title.cget("text") == "proyecto con ñ", ex.title.cget("text"))

src = os.path.join(ws, "src")
check("carga perezosa: 'src' aún no leyó sus hijos (solo el falso)", ex.tree.get_children(src) == (src + "\0",), ex.tree.get_children(src))
ex.tree.focus(src)
ex.tree.item(src, open=True)
ex.tree.event_generate("<<TreeviewOpen>>")
root.update()
hijos = [ex.tree.item(i, "text") for i in ex.tree.get_children(src)]
check("al desplegar aparecen sus hijos, ordenados", hijos == ["util", "main.py"], hijos)

# se abre también src/util y se selecciona un archivo; después se crea un archivo nuevo y se refresca
util = os.path.join(src, "util")
ex.tree.focus(util)
ex.tree.item(util, open=True)
ex.tree.event_generate("<<TreeviewOpen>>")
root.update()
sel = os.path.join(util, "helpers.py")
ex.tree.selection_set(sel)
open(os.path.join(ws, "src", "nuevo.py"), "w").write("y")
ex.refresh()
root.update()
hijos = [ex.tree.item(i, "text") for i in ex.tree.get_children(src)]
check("refresh muestra el archivo creado desde afuera", "nuevo.py" in hijos, hijos)
check("refresh conserva abierto src y src/util", ex.tree.item(src, "open") and ex.tree.item(util, "open"))
check("refresh conserva la selección", ex.tree.selection() == (sel,), ex.tree.selection())
check("control: 'docs' sigue cerrada (no se abrió sola)", not ex.tree.item(os.path.join(ws, "docs"), "open"))

# borrar desde afuera un archivo abierto no rompe el refresco
os.remove(sel)
ex.refresh()
check("refresh tras borrar la selección no falla y la limpia", ex.tree.selection() == (), ex.tree.selection())

# doble clic / Enter sobre archivo abre; sobre carpeta no
ex.tree.selection_set(os.path.join(src, "main.py"))
ex.tree.focus(os.path.join(src, "main.py"))
ex._on_double(None)
check("Enter en un archivo llama a on_open con su ruta", abiertos == [os.path.join(src, "main.py")], abiertos)
ex.tree.selection_set(src)
ex._on_double(None)
check("control: sobre una carpeta no abre nada", len(abiertos) == 1, abiertos)

# límite de hijos
big = os.path.join(ws, "grande")
os.makedirs(big)
for i in range(explorer.MAX_CHILDREN + 5):
    open(os.path.join(big, f"f{i:04d}.txt"), "w").close()
ex.refresh()
ex.tree.focus(big)
ex._expand(big)
kids = ex.tree.get_children(big)
check(f"carpeta enorme: {explorer.MAX_CHILDREN} entradas + aviso '… 5 más'", len(kids) == explorer.MAX_CHILDREN + 1 and "5 más" in ex.tree.item(kids[-1], "text"), (len(kids), ex.tree.item(kids[-1], "text")))

# carpeta desaparecida (pendrive desconectado)
ex.set_root(os.path.join(tmp, "no existe"))
check("carpeta inexistente: avisa y no revienta", "no existe" in ex.path_lbl.cget("text") and ex.tree.get_children() == (), ex.path_lbl.cget("text"))

# el explorador no modificó nada
ex.set_root(ws)
check("el explorador no escribió nada (README intacto)", open(os.path.join(ws, "README.md")).read() == "x")

root.destroy()
print("\nFALLAS:", fallas if fallas else "ninguna")
raise SystemExit(1 if fallas else 0)
