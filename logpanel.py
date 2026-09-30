"""Panel de log de CodeAgent: lo que la aplicación va haciendo, en texto tipo consola sobre fondo negro.

Formato copiado del «Log del proceso» de PostOCRNormalizer: una línea por evento «HH:MM:SS - mensaje», Consolas,
color por nivel (OK verde, WARN caqui, ERROR naranja rojizo, SPINNER cian, INFO blanco), barras de progreso
[████░░░░] y líneas que se reescriben en el lugar (spinner) sin comerse las líneas normales. Ctrl+L limpia.

Se puede minimizar (▼) y desacoplar en una ventana propia (⧉), para llevarla a otro monitor; cerrar esa ventana
lo vuelve a acoplar. El contenido vive en self.lines y se redibuja en la vista que esté activa: tk no puede mover un
widget de una ventana a otra.
"""
import threading
import time
import tkinter as tk
from tkinter import ttk

TITULO = "CodeAgent - Log"
FONDO = "#000000"
FUENTE = ("Consolas", 10)
COLORES = {"INFO": "#ffffff", "OK": "#90ee90", "WARN": "#f0e68c", "ERROR": "#ff4500", "SPINNER": "#00ffff"}
SPIN = "|/-\\"
ANCHO_BARRA = 24
MAX_LINEAS = 3000
GEOMETRIA_SUELTA = "900x360"


def barra(frac, ancho=ANCHO_BARRA):
    """[████░░░░]: frac entre 0 y 1 (se recorta si se pasa)."""
    n = round(max(0.0, min(1.0, frac)) * ancho)
    return "█" * n + "░" * (ancho - n)


def progreso(etiqueta, actual, total, spin=""):
    """Una línea de progreso como la de PostOCRNormalizer: «OCR [████░░] 42%  (5/12)  |»."""
    frac = actual / total if total else 0.0
    txt = f"{etiqueta} [{barra(frac)}] {round(frac * 100):3d}%  ({actual:,}/{total:,})".replace(",", ".")
    return txt + (f"  {spin}" if spin else "")


class LogPanel:
    def __init__(self, root, pane, cfg, post=None, follow=None, fit=None):
        """pane: el PanedWindow vertical donde va acoplado. post(fn): corre fn en el hilo de la ventana (para loguear
        desde otros hilos). follow(): True si hay que bajar al final aunque la persona haya subido (app trabajando).
        fit(geo): ajusta una geometría guardada al escritorio actual (multimonitor), o None si no sirve."""
        self.root, self.pane, self.cfg = root, pane, cfg
        self.post = post or root.after_idle
        self.follow = follow or (lambda: False)
        self.fit = fit or (lambda g: None)
        self.lines = []             # [texto, nivel, transitoria, protegida]
        self.minimized = False
        self.win = None             # la ventana suelta, si está desacoplada
        self._spin_i = 0
        self._height = int(cfg["log_height"])
        self.frame = self._make_view(pane, detached=False)
        self.view = self.frame.view

    # ------------------------------------------------------------ vistas

    def _make_view(self, parent, detached):
        f = ttk.Frame(parent)
        head = ttk.Frame(f, padding=(0, 4, 0, 2))
        head.pack(fill="x")
        ttk.Label(head, text="Log", style="Title.TLabel").pack(side="left")
        btn_min = None
        if not detached:
            btn_min = ttk.Button(head, text="▼", width=3, command=self.toggle_minimized)
            btn_min.pack(side="right")
        ttk.Button(head, text="⧈ Acoplar" if detached else "⧉ Desacoplar",
                   command=self.dock if detached else self.detach).pack(side="right", padx=4)
        ttk.Button(head, text="Limpiar", command=self.clear).pack(side="right")
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, bg=FONDO, fg=COLORES["INFO"], font=FUENTE, wrap="word", relief="flat", bd=0, padx=6, pady=4,
                      insertbackground=COLORES["INFO"], selectbackground="#264f78", selectforeground="#ffffff",
                      highlightthickness=0, height=8, state="disabled", cursor="xterm")
        sb = ttk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        for lvl, color in COLORES.items():
            txt.tag_configure(lvl, foreground=color)
        menu = tk.Menu(txt, tearoff=0)
        menu.add_command(label="Copiar", command=lambda: self._copy(txt, sel=True))
        menu.add_command(label="Copiar todo", command=lambda: self._copy(txt, sel=False))
        menu.add_separator()
        menu.add_command(label="Limpiar  (Ctrl+L)", command=self.clear)
        txt.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        txt.bind("<Button-1>", lambda e: txt.focus_set())      # deshabilitado no toma el foco solo: sin foco no hay Ctrl+C
        txt.bind("<Control-l>", lambda e: (self.clear(), "break")[1])
        txt.bind("<Control-L>", lambda e: (self.clear(), "break")[1])
        f.view = txt
        f.head, f.body, f.btn_min = head, body, btn_min
        return f

    def _copy(self, txt, sel):
        try:
            data = txt.get("sel.first", "sel.last") if sel else txt.get("1.0", "end-1c")
        except tk.TclError:           # «Copiar» sin nada seleccionado
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(data)

    def _render(self):
        v = self.view
        v.configure(state="normal")
        v.delete("1.0", "end")
        for text, lvl, _t, _p in self.lines:
            v.insert("end", text + "\n", lvl)
        v.configure(state="disabled")
        v.see("end")

    # ------------------------------------------------------------ escribir

    def log(self, msg, level="INFO", overwrite=False, protect=False):
        """Agrega una línea «HH:MM:SS - msg». overwrite=True reemplaza la última si también fue escrita con overwrite y
        no quedó protegida (así un spinner no se come las líneas normales); protect=True la deja fija.
        Se puede llamar desde cualquier hilo."""
        line = f"{time.strftime('%H:%M:%S')} - {msg}"
        level = level if level in COLORES else "INFO"
        if threading.current_thread() is threading.main_thread():
            self._append(line, level, overwrite, protect)
        else:
            self.post(lambda: self._append(line, level, overwrite, protect))

    def spin(self):
        """El siguiente cuadro del spinner (| / - \\)."""
        self._spin_i = (self._spin_i + 1) % len(SPIN)
        return SPIN[self._spin_i]

    def _append(self, line, level, overwrite, protect):
        v = self.view
        try:
            at_end = v.yview()[1] >= 0.999
        except tk.TclError:
            return                      # la ventana ya se cerró
        v.configure(state="normal")
        last = self.lines[-1] if self.lines else None
        if overwrite and last is not None and last[2] and not last[3]:
            n = len(self.lines)
            v.delete(f"{n}.0", f"{n + 1}.0")
            v.insert(f"{n}.0", line + "\n", level)
            self.lines[-1] = [line, level, True, protect]
        else:
            v.insert("end-1c", line + "\n", level)
            self.lines.append([line, level, overwrite, protect])
        extra = len(self.lines) - MAX_LINEAS
        if extra > 0:
            del self.lines[:extra]
            v.delete("1.0", f"{extra + 1}.0")
        v.configure(state="disabled")
        if at_end or self.follow():
            v.see("end")

    def clear(self):
        self.lines = []
        self._render()
        self.log("Log limpiado manualmente (Ctrl+L).")

    def text(self):
        """Todo el log como texto (para tests y para copiar)."""
        return "\n".join(l[0] for l in self.lines)

    # ------------------------------------------------------------ minimizar

    def toggle_minimized(self):
        self.set_minimized(not self.minimized)

    def set_minimized(self, value):
        if self.win is not None or value == self.minimized:
            return
        f = self.frame
        if value:
            self._height = self._docked_height() or self._height
            f.body.pack_forget()
            f.btn_min.configure(text="▲")
            self._set_height(self._min_height())
        else:
            f.body.pack(fill="both", expand=True)
            f.btn_min.configure(text="▼")
            self._set_height(self._height)
        self.minimized = value
        self.view.see("end")

    def _docked_height(self):
        try:
            return self.pane.winfo_height() - self.pane.sashpos(0) if self.pane.winfo_ismapped() else 0
        except tk.TclError:
            return 0

    def _set_height(self, h):
        """Deja el panel con h píxeles de alto, moviendo el separador (el resto queda para la ventana de trabajo)."""
        total = self.pane.winfo_height()
        if total > 1:
            self.pane.sashpos(0, max(120, total - int(h)))

    def _min_height(self):
        """Alto del panel minimizado: la barra de título entera más el grosor del separador (si no, se corta abajo)."""
        self.root.update_idletasks()
        return self.frame.head.winfo_reqheight() + self._sash_thickness()

    def _sash_thickness(self):
        try:
            return int(ttk.Style(self.root).lookup("Sash", "-sashthickness") or 5) + 2
        except (tk.TclError, ValueError):
            return 7

    def place(self):
        """Primera ubicación del separador, cuando la ventana ya tiene tamaño (la llama la app con after)."""
        if self.win is not None:
            return
        if self.cfg["log_minimized"]:
            self.set_minimized(True)
        else:
            self._set_height(self._height)

    # ------------------------------------------------------------ desacoplar

    def detach(self):
        if self.win is not None:
            return
        if not self.minimized:
            self._height = self._docked_height() or self._height
        self.pane.forget(self.frame)
        w = self.win = tk.Toplevel(self.root)
        w.title(TITULO)
        w.geometry(self.fit(self.cfg["log_geometry"]) or GEOMETRIA_SUELTA)
        w.minsize(360, 160)
        w.protocol("WM_DELETE_WINDOW", self.dock)
        w.configure(bg=FONDO)
        vf = self._make_view(w, detached=True)
        vf.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.view = vf.view
        self._render()
        self.cfg["log_detached"] = True

    def dock(self):
        if self.win is None:
            return
        self._save_win_geometry()
        self.win.destroy()
        self.win = None
        self.view = self.frame.view
        self.pane.add(self.frame, weight=0)
        self._render()
        self.cfg["log_detached"] = False
        self.root.update_idletasks()
        if self.minimized:
            self._set_height(self._min_height())
        else:
            self._set_height(self._height)

    def _save_win_geometry(self):
        try:
            if self.win is not None and self.win.state() == "normal":
                self.cfg["log_geometry"] = self.win.geometry()
        except tk.TclError:
            pass

    def save_state(self):
        """Deja en cfg cómo quedó el panel (la app guarda cfg al cerrar)."""
        if self.win is None and not self.minimized:
            self._height = self._docked_height() or self._height
        self._save_win_geometry()
        self.cfg["log_height"] = max(60, int(self._height))
        self.cfg["log_minimized"] = self.minimized
        self.cfg["log_detached"] = self.win is not None
