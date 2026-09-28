"""El explorador de la carpeta de trabajo, al modo del de VSCode: árbol que se abre carpeta por carpeta.

Carga perezosa: una carpeta se lee recién cuando se despliega, así que abrir un proyecto con node_modules no cuelga la
ventana. El árbol solo lee; nunca modifica nada del disco.
"""
import os
import tkinter as tk
from tkinter import ttk

HIDE = {".git", ".dschat-backup", "__pycache__", ".mypy_cache", ".pytest_cache", ".vs"}
MAX_CHILDREN = 1000
_DUMMY = "\0"          # hijo falso para que una carpeta cerrada muestre su flecha de despliegue


def shorten_path(p, n=38):
    return p if len(p) <= n else "…" + p[-(n - 1):]


class Explorer:
    def __init__(self, master, on_open, on_attach, on_pick_folder):
        self.on_open, self.on_attach, self.on_pick_folder = on_open, on_attach, on_pick_folder
        self.root_path = ""
        self.kinds = {}            # iid (ruta) -> 'dir' | 'file' | 'more'
        self.frame = ttk.Frame(master)

        head = ttk.Frame(self.frame)
        head.pack(fill="x", pady=(0, 2))
        self.title = ttk.Label(head, text="Explorador", font=("Segoe UI", 10, "bold"))
        self.title.pack(side="left")
        self.btn_refresh = ttk.Button(head, text="⟳", width=3, command=self.refresh)
        self.btn_refresh.pack(side="right")
        self.btn_open = ttk.Button(head, text="Abrir carpeta…", command=on_pick_folder)
        self.btn_open.pack(side="right", padx=4)
        self.path_lbl = ttk.Label(self.frame, text="Sin carpeta abierta", style="Muted.TLabel")
        self.path_lbl.pack(fill="x", pady=(0, 4))

        box = ttk.Frame(self.frame)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, show="tree", selectmode="browse", style="Side.Treeview")
        sb = ttk.Scrollbar(box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("dir", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("more", foreground="#888888")
        self.tree.bind("<<TreeviewOpen>>", self._on_open_node)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_double)
        self.tree.bind("<Button-3>", self._on_right)
        self.menu = tk.Menu(self.tree, tearoff=0)
        self._menu_path = None

    # ------------------------------------------------------------ carga

    def set_root(self, path):
        self.root_path = os.path.abspath(path) if path else ""
        self._rebuild()

    def _rebuild(self):
        self.tree.delete(*self.tree.get_children())
        self.kinds.clear()
        if not self.root_path:
            self.title.configure(text="Explorador")
            self.path_lbl.configure(text="Sin carpeta abierta")
            return
        self.title.configure(text=os.path.basename(self.root_path.rstrip("\\/")) or self.root_path)
        self.path_lbl.configure(text=shorten_path(self.root_path))
        if not os.path.isdir(self.root_path):
            self.path_lbl.configure(text="La carpeta no existe (¿pendrive desconectado?)")
            return
        self._fill("", self.root_path)

    def _fill(self, parent, path):
        try:
            entries = sorted((e for e in os.scandir(path) if e.name not in HIDE),
                             key=lambda e: (not self._is_dir(e), e.name.lower()))
        except OSError as e:
            self.tree.insert(parent, "end", iid=path + _DUMMY + "err", text=f"(no se puede leer: {e.strerror or e})", tags=("more",))
            return
        for n, e in enumerate(entries):
            if n >= MAX_CHILDREN:
                self.tree.insert(parent, "end", iid=path + _DUMMY + "more", text=f"… {len(entries) - MAX_CHILDREN} más", tags=("more",))
                break
            iid = os.path.normpath(e.path)
            if self._is_dir(e):
                self.kinds[iid] = "dir"
                self.tree.insert(parent, "end", iid=iid, text=e.name, tags=("dir",))
                self.tree.insert(iid, "end", iid=iid + _DUMMY, text="")
            else:
                self.kinds[iid] = "file"
                self.tree.insert(parent, "end", iid=iid, text=e.name)

    @staticmethod
    def _is_dir(e):
        try:
            return e.is_dir(follow_symlinks=False)
        except OSError:
            return False

    def _expand(self, iid):
        kids = self.tree.get_children(iid)
        if len(kids) == 1 and kids[0] == iid + _DUMMY:
            self.tree.delete(kids[0])
            self._fill(iid, iid)

    def _on_open_node(self, _e):
        iid = self.tree.focus()
        if iid and self.kinds.get(iid) == "dir":
            self._expand(iid)

    def _all_items(self, parent=""):
        for i in self.tree.get_children(parent):
            yield i
            yield from self._all_items(i)

    def refresh(self):
        """Vuelve a leer el disco conservando las carpetas abiertas, la selección y el desplazamiento."""
        if not self.root_path:
            return
        opened = [i for i in self._all_items() if self.kinds.get(i) == "dir" and self.tree.item(i, "open")]
        sel = self.tree.selection()
        y = self.tree.yview()[0]
        self._rebuild()
        for p in sorted(opened, key=len):            # las de arriba primero: un hijo solo existe si su padre se expandió
            if self.tree.exists(p) and self.kinds.get(p) == "dir":
                self._expand(p)
                self.tree.item(p, open=True)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.tree.yview_moveto(y)

    # ------------------------------------------------------------ interacción

    def selected_file(self):
        sel = self.tree.selection()
        return sel[0] if sel and self.kinds.get(sel[0]) == "file" else None

    def _on_double(self, _e):
        p = self.selected_file()
        if p:
            self.on_open(p)
            return "break"

    def _on_right(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid or iid not in self.kinds:
            return
        self.tree.selection_set(iid)
        self._menu_path = iid
        self.menu.delete(0, "end")
        if self.kinds[iid] == "file":
            self.menu.add_command(label="Abrir", command=lambda: self.on_open(iid))
            self.menu.add_command(label="Adjuntar al chat", command=lambda: self.on_attach(iid))
        self.menu.add_command(label="Copiar ruta relativa", command=lambda: self._copy_rel(iid))
        self.menu.add_separator()
        self.menu.add_command(label="Actualizar", command=self.refresh)
        self.menu.tk_popup(e.x_root, e.y_root)

    def _copy_rel(self, iid):
        rel = os.path.relpath(iid, self.root_path).replace(os.sep, "/")
        self.tree.clipboard_clear()
        self.tree.clipboard_append(rel)
