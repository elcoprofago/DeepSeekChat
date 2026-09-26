"""DeepSeek Chat: agente de programación con ventana. Sesiones, explorador de carpetas, modelos de DeepSeek y locales.

Toda la lógica del agente vive en agent.py / agent_tools.py; acá solo está la ventana y el hilo que las conecta.
Regla de hilos: tkinter solo se toca desde el hilo principal. El hilo de trabajo deja lo que quiere mostrar en una
cola (self.post) y _drain_ui lo ejecuta.
"""
import ctypes
import json
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import agent as ag
import agent_tools as at
import chatview
import dialogs
import dsapi
import explorer
import localmodels
import meter
import prompts
import remote
import sessions
import theme

TITULO = "DeepSeek Chat"
TITULO_VENTANA = "DeepSeek Chat - © R.A. Sistemas - 2026"      # mismo formato que la ventana de USBagent
ICONO = "asterisc.ico"      # junto a los .py; el mismo va embebido en el lanzador DeepSeekChat.exe
GEOMETRIA_INICIAL = "1240x780"
SIN_EFFORT = "(por defecto)"
APPROVALS = {"ask": "Preguntar todo", "edits": "Editar sin preguntar", "all": "Todo sin preguntar"}
MUTATING = {"write_file", "edit_file", "delete_path", "run_command"}
CONTINUE_TEXT = "Seguí con lo que estabas haciendo, desde donde quedaste."
REMOTE_BUDGET = 600000
LOCAL_TIMEOUT = 900
miles = chatview.miles


def fit_geometry(geo, bounds, minw=860, minh=520):
    """Una geometría guardada ("ANCHOxALTO+X+Y") ajustada para que la ventana quede visible dentro de bounds
    (x, y, ancho, alto del escritorio virtual). None si lo guardado no sirve. Sirve para cuando cambia el monitor."""
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", geo or "")
    if not m:
        return None
    w, h, x, y = (int(v) for v in m.groups())
    bx, by, bw, bh = bounds
    w, h = max(minw, min(w, bw)), max(minh, min(h, bh))
    x = min(max(x, bx), bx + bw - w)
    y = min(max(y, by), by + bh - h)
    return f"{w}x{h}+{x}+{y}"


def virtual_screen(root):
    """(x, y, ancho, alto) de todo el escritorio, con todos los monitores; si Windows no lo informa, el monitor principal."""
    try:
        gm = ctypes.windll.user32.GetSystemMetrics
        x, y, w, h = gm(76), gm(77), gm(78), gm(79)
        if w > 0 and h > 0:
            return x, y, w, h
    except (AttributeError, OSError):
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class App:
    def __init__(self, root, cfg=None, stream_factory=None, interactive=True):
        """stream_factory(entry, mensajes_api, herramientas) -> stream: solo para tests. interactive=False evita los
        diálogos que bloquean (messagebox, ventanas automáticas); lo que habría sido un aviso queda en self.warnings."""
        self.root = root
        self.cfg = cfg or dsapi.Config()
        self._stream_factory = stream_factory
        self.interactive = interactive
        self.store = sessions.SessionStore(self.cfg.sessions_dir)
        self.remote_models = list(dsapi.FALLBACK_MODELS)
        self.local_models = []
        self._display_to_id = {}
        self.themes = theme.THEMES
        self.t = theme.pick(self.cfg["theme"])
        self.sess = None
        self.toolbox = None
        self.attachments = []
        self.warnings = []
        self.busy = False
        self.cancel_ev = threading.Event()
        self.agent = None
        self.max_steps = ag.DEFAULT_MAX_STEPS      # pasos por turno; atributo para poder probarlo con un tope chico
        self._cpt = {}      # caracteres por token medidos por modelo local: el turno siguiente arranca calibrado
        self.worker = None
        self._dialog = None
        self._server = None
        self._server_key = None
        self.remote_srv = None
        self._live = {"content": "", "rlen": 0}      # el paso en curso, para quien mira por el celular
        self._pending_confirm = None
        self._confirm_seq = 0
        self._can_continue = False
        self.last_usage = None
        self.balance_text = "—"
        self._suppress_select = False
        self._ui = queue.Queue()
        self.meter = meter.TokenMeter()

        root.title(TITULO_VENTANA)
        icono = os.path.join(dsapi.APP_DIR, ICONO)
        if os.path.isfile(icono):
            try:
                root.iconbitmap(default=icono)       # default=: también lo heredan los diálogos
            except tk.TclError:
                pass
        root.geometry(fit_geometry(self.cfg["win_geometry"], virtual_screen(root)) or GEOMETRIA_INICIAL)
        root.minsize(860, 520)
        if self.cfg["win_zoomed"]:
            root.after(0, lambda: root.state("zoomed"))
        theme.apply_styles(root, self.t)
        self._build()
        if not self.cfg["sidebar_visible"]:
            self.main.forget(self.side)
            self.sidebar_visible = False
        self.apply_theme()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_ui()
        self._meter_tick()
        self._startup()

    # ------------------------------------------------------------ puente hilos -> ventana

    def post(self, fn):
        """Se puede llamar desde cualquier hilo; fn corre en el hilo de la ventana."""
        self._ui.put(fn)

    def call_ui(self, fn, timeout=8):
        """Corre fn en el hilo de la ventana desde otro hilo y devuelve su resultado (o relanza su excepción)."""
        ev, box = threading.Event(), {}

        def run():
            try:
                box["r"] = fn()
            except Exception as e:                  # noqa: BLE001 — se le devuelve al que llamó
                box["e"] = e
            ev.set()
        self._ui.put(run)
        if not ev.wait(timeout):
            raise TimeoutError("la ventana no respondió")
        if "e" in box:
            raise box["e"]
        return box["r"]

    def _drain_ui(self):
        try:
            while True:
                try:
                    fn = self._ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except tk.TclError:
                    pass          # el widget al que apuntaba ya no existe
        finally:
            try:
                self.root.after(40, self._drain_ui)
            except tk.TclError:
                pass

    def warn(self, text):
        if self.interactive:
            messagebox.showwarning(TITULO, text, parent=self.root)
        else:
            self.warnings.append(text)

    # ------------------------------------------------------------ construcción

    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 8))
        top.pack(fill="x")
        self.btn_side = ttk.Button(top, text="☰", width=3, command=self.toggle_sidebar)
        self.btn_side.pack(side="left", padx=(0, 10))
        ttk.Label(top, text="Modelo").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=34)
        self.model_cb.pack(side="left", padx=(4, 12))
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self.on_model_change())
        ttk.Label(top, text="Effort").pack(side="left")
        self.effort_var = tk.StringVar(value=SIN_EFFORT)
        self.effort_cb = ttk.Combobox(top, textvariable=self.effort_var, state="readonly", width=12)
        self.effort_cb.pack(side="left", padx=(4, 12))
        self.effort_cb.bind("<<ComboboxSelected>>", lambda e: self.on_effort_change())
        ttk.Label(top, text="Permisos").pack(side="left")
        self.appr_var = tk.StringVar(value=APPROVALS["ask"])
        self.appr_cb = ttk.Combobox(top, textvariable=self.appr_var, state="readonly", width=20, values=list(APPROVALS.values()))
        self.appr_cb.pack(side="left", padx=(4, 0))
        self.appr_cb.bind("<<ComboboxSelected>>", lambda e: self.on_approval_change())
        ttk.Button(top, text="⚙ Configuración", command=self.open_settings).pack(side="right")
        self.btn_remote = ttk.Button(top, text="📱 Remoto", command=self.open_remote)
        self.btn_remote.pack(side="right", padx=6)
        ttk.Button(top, text="Exportar", command=self.export_current).pack(side="right", padx=6)
        self.btn_undo = ttk.Button(top, text="Deshacer cambio", command=self.undo)
        self.btn_undo.pack(side="right")

        self.main = ttk.PanedWindow(r, orient="horizontal")
        self.side = ttk.PanedWindow(self.main, orient="vertical")
        self.col = ttk.Frame(self.main)
        self.main.add(self.side, weight=0)
        self.main.add(self.col, weight=1)
        self.sidebar_visible = True

        # --- barra lateral: sesiones arriba, explorador abajo
        sess_box = ttk.Frame(self.side)
        head = ttk.Frame(sess_box)
        head.pack(fill="x", pady=(0, 2))
        ttk.Label(head, text="Sesiones", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="+ Nueva", command=self.new_session).pack(side="right")
        tbox = ttk.Frame(sess_box)
        tbox.pack(fill="both", expand=True)
        self.sess_tree = ttk.Treeview(tbox, columns=("title", "folder"), show="headings", selectmode="browse", height=6)
        self.sess_tree.heading("title", text="Sesión")
        self.sess_tree.heading("folder", text="Carpeta")
        self.sess_tree.column("title", width=150, stretch=True)
        self.sess_tree.column("folder", width=80, stretch=False)
        ssb = ttk.Scrollbar(tbox, command=self.sess_tree.yview)
        self.sess_tree.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.sess_tree.pack(side="left", fill="both", expand=True)
        self.sess_tree.bind("<<TreeviewSelect>>", self._on_session_select)
        self.sess_tree.bind("<Button-3>", self._on_session_right)
        self.sess_menu = tk.Menu(self.sess_tree, tearoff=0)
        self.explorer = explorer.Explorer(self.side, self.view_file, lambda p: self.attach_paths([p]), self.open_folder)
        self.side.add(sess_box, weight=1)
        self.side.add(self.explorer.frame, weight=2)

        # --- columna del chat
        self.chat = chatview.ChatView(self.col, self.copy_text)
        self.attach_bar = ttk.Frame(self.col, padding=(0, 4, 0, 0))
        self.folder_bar = ttk.Frame(self.col, padding=(0, 6, 0, 0))
        self.btn_folder = ttk.Button(self.folder_bar, text="📁 Abrir carpeta…", command=self.open_folder)
        self.btn_folder.pack(side="left")
        self.folder_lbl = ttk.Label(self.folder_bar, text="", anchor="w")
        self.folder_lbl.pack(side="left", padx=8, fill="x", expand=True)
        self.btn_clear = ttk.Button(self.folder_bar, text="Limpiar avisos", command=self.clear_notices)
        self.btn_clear.pack(side="right")
        self.bottom = ttk.Frame(self.col, padding=(0, 6, 0, 4))
        self.btn_plus = ttk.Button(self.bottom, text="+", width=3, command=self.add_files)
        self.btn_plus.pack(side="left", anchor="s", padx=(0, 8))
        self.input = tk.Text(self.bottom, height=4, wrap="word", relief="flat", padx=8, pady=6, highlightthickness=1, undo=True)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)
        self.btn_send = ttk.Button(self.bottom, text="Enviar", width=9, command=self.on_send_click)
        self.btn_send.pack(side="left", anchor="s", padx=(8, 0))
        # solo se ve cuando el agente cortó por el tope de pasos: un clic y sigue, sin escribir nada
        self.btn_continue = ttk.Button(self.bottom, text="Continuar", width=10, command=self.continue_run,
                                       style="Continue.TButton")
        # cartel chico en el centro del chat que titila mientras el botón está a la vista; un clic hace lo mismo
        self.continue_flag = tk.Label(self.col, text="⏸  Tope de pasos: apretá «Continuar»", font=("Segoe UI", 10, "bold"),
                                      bg=theme.LIMON, fg=theme.NEGRO, padx=14, pady=6, bd=1, relief="solid", cursor="hand2")
        self.continue_flag.bind("<Button-1>", lambda e: self.continue_run())
        self._blink_job = None
        self._blink_on = True
        self.status = ttk.Frame(self.col, padding=(0, 0, 0, 6))
        self.tokens_lbl = ttk.Label(self.status, text="")
        self.tokens_lbl.pack(side="left")
        ttk.Button(self.status, text="⟳", width=3, command=self.refresh_balance).pack(side="right")
        self.balance_lbl = ttk.Label(self.status, text="Saldo: —")
        self.balance_lbl.pack(side="right", padx=6)
        self.state_lbl = ttk.Label(self.status, text="")
        self.state_lbl.pack(side="right", padx=14)

        # medidor: estado + velocidad en vivo, y consumo acumulado de la jornada (mismo patrón que USBagent)
        self.meter_bar = ttk.Frame(self.col, padding=(0, 4, 0, 6))
        self.cv_state = tk.Canvas(self.meter_bar, width=260, height=24, highlightthickness=1, bd=0)
        self.cv_state.pack(side="left")
        self.cv_bar = tk.Canvas(self.meter_bar, height=24, highlightthickness=1, bd=0)
        self.cv_bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.cv_bar.bind("<Configure>", lambda e: self._draw_meter())
        self.cv_bar.bind("<Button-3>", self._meter_menu)
        self.cv_state.bind("<Button-3>", self._meter_menu)

        # lo que siempre debe verse va abajo y se empaqueta primero; el chat se queda con lo que sobre
        self.status.pack(side="bottom", fill="x")
        self.meter_bar.pack(side="bottom", fill="x")
        self.bottom.pack(side="bottom", fill="x")
        self.attach_bar.pack(side="bottom", fill="x")
        self.folder_bar.pack(side="bottom", fill="x")
        self.chat.frame.pack(fill="both", expand=True)
        self.main.pack(fill="both", expand=True, padx=10)
        self.root.after(50, self._place_sash)

    # ------------------------------------------------------------ medidor de tokens

    def _meter_tick(self):
        try:
            self._draw_meter()
            self._meter_job = self.root.after(250, self._meter_tick)
        except tk.TclError:
            pass

    def _meter_menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Reiniciar el contador de la jornada", command=self._meter_reset)
        m.tk_popup(e.x_root, e.y_root)

    def _meter_reset(self):
        self.meter.reset()
        self._draw_meter()

    def _draw_meter(self):
        t, m = self.t, self.meter
        cs = self.cv_state
        cs.delete("all")
        cs.configure(bg=t["panel"], highlightbackground=t["border"])
        if m.generating():
            dot, label = t["bot"], "Generando"
        elif self.busy:
            dot, label = "#e5c07b", "Trabajando…"
        else:
            dot, label = t["muted"], "Listo"
        r = m.rate()
        if r is None:
            speed = ""
        elif r[1]:
            speed = f"  ~{r[0]:.0f} tok/s"
        else:
            speed = f"  {r[0]:.0f} tok/s" if m.generating() or self.busy else f"  última: {r[0]:.0f} tok/s"
        cs.create_oval(9, 8, 17, 16, fill=dot, outline="")
        cs.create_text(26, 12, text=label + speed, anchor="w", fill=t["fg"], font=("Segoe UI", 9))

        cb = self.cv_bar
        cb.delete("all")
        cb.configure(bg=t["code_bg"], highlightbackground=t["border"])
        w = max(1, cb.winfo_width() - 2)
        budget = max(1, int(self.cfg["token_budget"]))
        total = m.total()
        frac = min(1.0, total / budget)
        if frac > 0:
            cb.create_rectangle(1, 1, 1 + w * frac, 24, fill=t["meter_fill"], outline="")
        txt = f"Jornada: {'~' if m.estimated() else ''}{miles(total)} tok · {frac * 100:.0f}% de {miles(budget)}"
        if m.acc_in or m.acc_out:
            txt += f"   (entrada {miles(m.acc_in)} · salida {miles(m.acc_out)})"
        cb.create_text(w / 2 + 1, 12, text=txt, fill=t["fg"], font=("Segoe UI", 9))

    def _dark_titlebar(self):
        """Barra de título oscura de Windows para el tema oscuro (Windows 10 1809+ / 11); si no se puede, no pasa nada."""
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            v = ctypes.c_int(1 if self.cfg["theme"] == "oscuro" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), ctypes.sizeof(v))
        except (AttributeError, OSError, tk.TclError):
            pass

    def _place_sash(self):
        try:
            self.main.sashpos(0, int(self.cfg["sidebar_w"]))
        except (tk.TclError, ValueError):
            pass

    def toggle_sidebar(self):
        if self.sidebar_visible:
            try:
                self.cfg["sidebar_w"] = max(160, self.main.sashpos(0))
            except tk.TclError:
                pass
            self.main.forget(self.side)
        else:
            self.main.insert(0, self.side, weight=0)
            self.root.after(20, self._place_sash)
        self.sidebar_visible = not self.sidebar_visible

    def apply_theme(self):
        self.t = t = theme.pick(self.cfg["theme"])
        theme.apply_styles(self.root, t)
        fs = int(self.cfg["font_size"])
        self.chat.apply_theme(t, fs)
        self.input.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"],
                             highlightcolor=t["accent"], font=("Segoe UI", fs))
        self.btn_send.configure(style="Accent.TButton")
        self.sess_tree.tag_configure("cur", font=("Segoe UI", 10, "bold"))
        self._dark_titlebar()
        self._draw_meter()

    # ------------------------------------------------------------ arranque

    def _startup(self):
        if self.cfg.load_warning:
            self.chat.note(self.cfg.load_warning, error=True)
        self._refresh_model_widgets()
        s = None
        if self.cfg["last_session"]:
            try:
                s = self.store.load(self.cfg["last_session"])
            except ValueError:
                s = None
        self._activate(s or self._blank_session(""))
        for w in self.store.warnings:
            self.chat.note(w, error=True)
        self._key_prompt()
        if self.cfg["remote_enabled"]:
            ok, msg = self.start_remote()
            if not ok:
                self.chat.note(msg, error=True)
        self.refresh_models()
        self.refresh_balance()
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        found = localmodels.scan_models(localmodels.default_model_dirs(self.cfg))
        self.post(lambda: self._set_local_models(found))

    def _key_note(self):
        """Deja (o quita) el aviso gris de key. Idempotente: se puede llamar cuantas veces haga falta."""
        self.chat.clear_tagged("nokey")
        if self.cfg.api_key:
            return
        if self.cfg.needs_unlock:
            self.chat.note("La API key está guardada con contraseña. Desbloquéala para usar los modelos de DeepSeek "
                           "(los locales funcionan sin key).", False, "nokey")
        else:
            self.chat.note("Falta la API key. Abrí ⚙ Configuración para cargarla (los modelos locales no la necesitan).", False, "nokey")

    def _key_prompt(self):
        """Abre la ventana que corresponde al arrancar, sin insistir si ya hay un modelo local para trabajar."""
        if not self.interactive or self.cfg.api_key:
            return
        if self.cfg.needs_unlock:
            self.root.after(300, lambda: dialogs.UnlockDialog(self))
        elif not self.local_models:
            self.root.after(300, self.open_settings)

    def key_changed(self):
        self._key_note()
        self.refresh_models()
        self.refresh_balance()

    # ------------------------------------------------------------ modelos

    def _entry_for(self, mid):
        """El modelo (remoto o local) de un id guardado. Si ya no está en las listas se fabrica uno, para no perder la sesión."""
        for m in self.remote_models:
            if m["id"] == mid:
                return {**m, "kind": "remote"}
        for m in self.local_models:
            if m["id"] == mid:
                return self._local_entry(m)
        if mid.startswith(localmodels.MODEL_ID_PREFIX):
            path = mid[len(localmodels.MODEL_ID_PREFIX):]
            base = os.path.basename(path)
            if not os.path.isfile(path):
                # el pendrive puede montarse con otra letra de unidad: el mismo archivo, hallado en otra carpeta
                same = [m for m in self.local_models if os.path.basename(m["path"]).lower() == base.lower()]
                if len(same) == 1:
                    return self._local_entry(same[0])
            return {"id": mid, "kind": "local", "name": base[:-5] if base.lower().endswith(".gguf") else base, "path": path,
                    "size": 0, "efforts": [], "context": int(self.cfg["local_ctx"]), "missing": not os.path.isfile(path)}
        return {"id": mid, "kind": "remote", "name": mid, "efforts": [], "context": 0}

    def _display(self, entry):
        if entry["kind"] == "remote":
            return entry["id"]
        if entry.get("missing"):
            return f"[local] {entry['name']} (no encontrado)"
        return f"[local] {entry['name']}" + (f" ({localmodels.format_size(entry['size'])})" if entry.get("size") else "")

    def _all_entries(self):
        return [{**m, "kind": "remote"} for m in self.remote_models] + \
               [self._local_entry(m) for m in self.local_models]

    def _local_entry(self, m):
        """Un modelo local como entrada del selector. Si su familia tiene interruptor de razonamiento, la barra lo
        ofrece en el lugar del esfuerzo de DeepSeek («(por defecto)» = lo que dice su perfil)."""
        efforts = list(localmodels.REASONING_CHOICES) if localmodels.reasoning_for(m["path"]) is not None else []
        return {**m, "kind": "local", "efforts": efforts, "context": int(self.cfg["local_ctx"])}

    def _refresh_model_widgets(self):
        entries = self._all_entries()
        cur = self._entry_for(self.sess.model) if self.sess and self.sess.model else None
        if cur and cur["id"] not in [e["id"] for e in entries]:
            entries.insert(0, cur)      # el de la sesión se conserva aunque el servidor ya no lo liste
        self._display_to_id = {self._display(e): e["id"] for e in entries}
        self.model_cb.configure(values=list(self._display_to_id))
        if cur:
            self.model_var.set(self._display(cur))
        self._refresh_effort_widgets()

    def _refresh_effort_widgets(self):
        entry = self._entry_for(self.sess.model) if self.sess and self.sess.model else None
        if entry is None or (entry["kind"] == "local" and not entry["efforts"]):
            self.effort_cb.configure(values=["—"], state="disabled")
            self.effort_var.set("—")
            return
        efforts = entry["efforts"] or []
        self.effort_cb.configure(values=[SIN_EFFORT] + efforts, state="readonly")
        self.effort_var.set(self.sess.effort if self.sess.effort in efforts else SIN_EFFORT)

    def _default_model_id(self):
        want = self.cfg["model"]
        if want.startswith(localmodels.MODEL_ID_PREFIX) or want in [m["id"] for m in self.remote_models]:
            return want
        return self.remote_models[0]["id"] if self.remote_models else want

    def on_model_change(self):
        mid = self._display_to_id.get(self.model_var.get())
        if not mid or self.busy:
            self._refresh_model_widgets()      # vuelve a mostrar el modelo real de la sesión
            return
        self.sess.model = mid
        self.cfg["model"] = mid
        try:
            self.cfg.save()
        except OSError:
            pass
        self._refresh_effort_widgets()
        self._update_status()
        self._persist()

    def on_effort_change(self):
        v = self.effort_var.get()
        self.sess.effort = "" if v in (SIN_EFFORT, "—") else v
        self.cfg["effort"] = self.sess.effort
        self._save_cfg()
        self._persist()

    def refresh_models(self):
        key = self.cfg.api_key
        if not key:
            return

        def work():
            try:
                res = dsapi.list_models(key)
            except dsapi.ApiError as e:
                self.post(lambda: self._set_state(f"No se pudo listar los modelos de DeepSeek: {e}", error=True))
                return
            self.post(lambda: self._set_remote_models(res))
        threading.Thread(target=work, daemon=True).start()

    def _set_remote_models(self, res):
        self.remote_models = res
        self._refresh_model_widgets()

    def _set_local_models(self, found):
        self.local_models = found
        self._refresh_model_widgets()
        self._update_status()

    def rescan_models(self):
        """Búsqueda síncrona (la usa el botón de Configuración). Devuelve la lista encontrada."""
        found = localmodels.scan_models(localmodels.default_model_dirs(self.cfg))
        self._set_local_models(found)
        return found

    # ------------------------------------------------------------ permisos

    def on_approval_change(self):
        inv = {v: k for k, v in APPROVALS.items()}
        val = inv.get(self.appr_var.get(), "ask")
        if val == "all" and self.interactive:
            if not messagebox.askokcancel(TITULO, "El agente podrá ejecutar comandos, modificar y borrar archivos de la carpeta de trabajo SIN preguntarte.\n\n"
                                          "Los comandos destructivos (formatear, tocar el registro, git push…) siguen bloqueados, "
                                          "y cada cambio o borrado se puede deshacer. ¿Continuar?", parent=self.root):
                self.appr_var.set(APPROVALS[self.sess.approval])
                return
        self._set_approval(val)

    def _set_approval(self, val):
        self.sess.approval = val
        self.appr_var.set(APPROVALS[val])
        if self.toolbox:
            self.toolbox.approval = val
        if val in ("ask", "edits") and self.cfg["approval"] != val:
            self.cfg["approval"] = val
            self._save_cfg()
        self._persist()

    # ------------------------------------------------------------ sesiones

    def _save_cfg(self):
        try:
            self.cfg.save()
        except OSError:
            pass

    def _blank_session(self, workspace):
        """Sesión nueva con los últimos permisos y effort elegidos. «Todo sin preguntar» NO se hereda: es una decisión
        por sesión (pide confirmación cada vez), así que una sesión nueva vuelve a «Preguntar todo»."""
        model = self._default_model_id()
        approval = self.cfg["approval"] if self.cfg["approval"] in ("ask", "edits") else "ask"
        effort = self.cfg["effort"] if self.cfg["effort"] in (self._entry_for(model).get("efforts") or []) else ""
        return sessions.Session(workspace=workspace, model=model, effort=effort, approval=approval)

    def _persist(self):
        """Guarda la sesión actual si tiene mensajes y está bien formada (cambios de modelo, permisos, nombre)."""
        if self.sess and self.sess.messages and not self.busy and not ag.validate(self.sess.messages):
            try:
                self.store.save(self.sess)
            except OSError as e:
                self.chat.note(f"No se pudo guardar la sesión: {e}", error=True)

    def _activate(self, s):
        """Deja a s como sesión actual: herramientas, explorador, selectores y transcripción."""
        fixed = ag.repair(s.messages)
        self.sess = s
        self.cfg["last_session"] = s.id
        try:
            self.cfg.save()
        except OSError:
            pass
        self.attachments = []
        self._redraw_attachments()
        self._show_continue(False)
        self.last_usage = None
        self._make_toolbox()
        self.explorer.set_root(s.workspace if os.path.isdir(s.workspace or "") else "")
        self._update_folder_bar()
        self._refresh_model_widgets()
        self.appr_var.set(APPROVALS.get(s.approval, APPROVALS["ask"]))
        self.chat.render_session(s.messages)
        if s.workspace and not os.path.isdir(s.workspace):
            self.chat.note(f"La carpeta de esta sesión ya no existe: {s.workspace}. Abrí otra con «Abrir carpeta…».", error=True)
        if fixed:
            self.chat.note("La sesión había quedado con una acción cortada a la mitad o incompleta; se reparó para poder seguir.")
        self._key_note()
        self._update_status()
        self._retitle()
        self.refresh_sessions()

    def _make_toolbox(self):
        s = self.sess
        self.toolbox = None
        if not (s.workspace and os.path.isdir(s.workspace)):
            return
        try:
            self.toolbox = at.ToolBox(s.workspace, confirm=self._confirm, approval=s.approval,
                                      backup_dir=os.path.join(self.cfg.dir, "backups", s.id), journal=s.journal)
        except at.ToolError as e:
            self.chat.note(str(e), error=True)
            return
        if self.cfg.portable:
            self.toolbox.extra_path = [os.path.join(self.cfg.root, "runtime")]

    def _label(self):
        return self._label_of(self._entry_for(self.sess.model) if self.sess and self.sess.model else None)

    @staticmethod
    def _label_of(e):
        return "DeepSeek" if e is None or e["kind"] == "remote" else e["name"]

    def _retitle(self):
        self.root.title(TITULO_VENTANA)     # fijo (pedido del usuario); la sesión y la carpeta ya se ven en la barra lateral y bajo el chat

    def refresh_sessions(self):
        tree = self.sess_tree
        self._suppress_select = True
        try:
            tree.delete(*tree.get_children())
            rows = list(self.store.list())
            if self.sess and self.sess.id not in {x.id for x in rows}:
                rows.insert(0, self.sess)          # la sesión en blanco aún no está en disco pero se ve
            for s in rows:
                # la actual se toma de memoria: puede tener un nombre o mensajes aún sin guardar
                s = self.sess if self.sess and s.id == self.sess.id else s
                tree.insert("", "end", iid=s.id, values=(s.display_title, os.path.basename((s.workspace or "").rstrip("\\/"))),
                            tags=("cur",) if self.sess and s.id == self.sess.id else ())
            if self.sess and tree.exists(self.sess.id):
                tree.selection_set(self.sess.id)
                tree.see(self.sess.id)
        finally:
            self.root.after_idle(lambda: setattr(self, "_suppress_select", False))

    def _busy_note(self):
        self.chat.note("Hay un agente trabajando. Esperá a que termine o presioná «Detener» antes de cambiar de sesión.")

    def new_session(self):
        if self.busy:
            self._busy_note()
            self.refresh_sessions()
            return
        if not self.sess.messages:      # la actual ya está vacía: no se apilan sesiones en blanco
            self.input.focus_set()
            return
        self._persist()
        self._activate(self._blank_session(self.sess.workspace))
        self.input.focus_set()

    def switch_session(self, sid):
        if self.sess and sid == self.sess.id:
            return
        if self.busy:
            self._busy_note()
            self.refresh_sessions()
            return
        try:
            s = self.store.load(sid)
        except ValueError:
            s = None
        if s is None:
            self.chat.note("No se pudo abrir esa sesión (el archivo falta o estaba dañado).", error=True)
            self.refresh_sessions()
            return
        self._persist()
        self._activate(s)

    def _on_session_select(self, _e):
        if self._suppress_select:
            return
        sel = self.sess_tree.selection()
        if sel:
            self.switch_session(sel[0])

    def _on_session_right(self, e):
        iid = self.sess_tree.identify_row(e.y)
        if not iid:
            return
        self.sess_menu.delete(0, "end")
        self.sess_menu.add_command(label="Renombrar…", command=lambda: self.rename_session_dialog(iid))
        self.sess_menu.add_command(label="Exportar…", command=lambda: self.export_session(iid))
        self.sess_menu.add_separator()
        self.sess_menu.add_command(label="Eliminar (va a la papelera)", command=lambda: self.delete_session(iid))
        self.sess_menu.tk_popup(e.x_root, e.y_root)

    def _session_by_id(self, sid):
        if self.sess and sid == self.sess.id:
            return self.sess
        try:
            return self.store.load(sid)
        except ValueError:
            return None

    def rename_session_dialog(self, sid):
        s = self._session_by_id(sid)
        if s is None:
            return
        new = simpledialog.askstring(TITULO, "Nuevo nombre de la sesión:", initialvalue=s.display_title, parent=self.root)
        if new is not None:
            self.rename_session(sid, new)

    def rename_session(self, sid, title):
        s = self._session_by_id(sid)
        if s is None:
            return False
        s.title = " ".join(title.split())[:80]
        if s.messages and not (s is self.sess and (self.busy or ag.validate(s.messages))):
            try:
                self.store.save(s)
            except OSError as e:
                self.chat.note(f"No se pudo guardar el nombre: {e}", error=True)
        if s is self.sess:
            self._retitle()
        self.refresh_sessions()
        return True

    def delete_session(self, sid):
        if self.busy and self.sess and sid == self.sess.id:
            self._busy_note()
            return
        if self.interactive and not messagebox.askyesno(TITULO, "¿Eliminar esta sesión?\n\nSe mueve a la carpeta _papelera de las "
                                                        "sesiones; no se borra del disco.", parent=self.root):
            return
        try:
            self.store.delete(sid)
        except (OSError, ValueError) as e:
            self.chat.note(f"No se pudo eliminar la sesión: {e}", error=True)
            return
        if self.sess and sid == self.sess.id:
            self._activate(self._blank_session(self.sess.workspace))
        else:
            self.refresh_sessions()

    def export_session(self, sid):
        s = self._session_by_id(sid)
        if s is None or not s.messages:
            self.chat.note("Esa sesión no tiene mensajes para exportar.")
            return None
        name = "".join("-" if c in '\\/:*?"<>|' else c for c in (s.display_title[:40] or "sesion")) + ".md"
        path = filedialog.asksaveasfilename(parent=self.root, title="Exportar sesión", defaultextension=".md", initialfile=name,
                                            filetypes=[("Markdown", "*.md"), ("Todos", "*.*")])
        return self.write_export(s, path) if path else None

    def export_current(self):
        return self.export_session(self.sess.id)

    def write_export(self, s, path):
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(sessions.export_markdown(s, chatview.UNKNOWN_MODEL))
        except OSError as e:
            self.chat.note(f"No se pudo exportar: {e}", error=True)
            return None
        self.chat.note(f"Exportada a {path}")
        return path

    # ------------------------------------------------------------ carpeta de trabajo

    def open_folder(self):
        d = filedialog.askdirectory(parent=self.root, title="Carpeta de trabajo del agente", mustexist=True)
        if d:
            self.use_folder(os.path.normpath(d))

    def use_folder(self, path):
        if not os.path.isdir(path):
            self.warn(f"No existe la carpeta: {path}")
            return
        if self.busy:
            self._busy_note()
            return
        if not self.sess.messages:
            self.sess.workspace = path
            self._make_toolbox()
            self.explorer.set_root(path)
            self._update_folder_bar()
            self._retitle()
            self.refresh_sessions()
            self.chat.note(f"Carpeta de trabajo: {path}")
        else:
            # la sesión ya tiene historia con otra carpeta: se abre una nueva para no mezclar proyectos
            self._persist()
            self._activate(self._blank_session(path))
            self.chat.note(f"Sesión nueva con la carpeta: {path}")

    def _update_folder_bar(self):
        ws = self.sess.workspace if self.sess else ""
        if ws and os.path.isdir(ws):
            self.folder_lbl.configure(text="Carpeta: " + ws, style="TLabel")
        else:
            self.folder_lbl.configure(text="Sin carpeta abierta: el agente no puede ver tus archivos", style="Err.TLabel")

    def clear_notices(self):
        """Borra de la vista los avisos y errores (solo lo que se ve: el historial de la sesión no se toca)."""
        self.chat.clear_tagged("aviso")
        self.state_lbl.configure(text="", style="TLabel")

    def view_file(self, path):
        dialogs.FileViewer(self, path, lambda p: self.attach_paths([p]))

    # ------------------------------------------------------------ adjuntos

    def add_files(self):
        self.attach_paths(filedialog.askopenfilenames(parent=self.root, title="Adjuntar archivos al chat"))

    def attach_paths(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p in self.attachments:
                continue
            try:
                dsapi.read_text_file(p)
            except (dsapi.AttachError, OSError) as e:
                self.warn(str(e))
                continue
            self.attachments.append(p)
        self._redraw_attachments()

    def _redraw_attachments(self):
        for w in self.attach_bar.winfo_children():
            w.destroy()
        for p in self.attachments:
            chip = ttk.Frame(self.attach_bar)
            chip.pack(side="left", padx=(0, 6))
            kb = max(1, os.path.getsize(p) // 1024) if os.path.exists(p) else 0
            ttk.Label(chip, text=f"📎 {os.path.basename(p)} ({kb} KB)").pack(side="left")
            ttk.Button(chip, text="✕", width=2, command=lambda q=p: self.remove_attachment(q)).pack(side="left", padx=(2, 0))

    def remove_attachment(self, p):
        if p in self.attachments:
            self.attachments.remove(p)
        self._redraw_attachments()

    # ------------------------------------------------------------ estado y contadores

    def _set_state(self, txt, error=False):
        self.state_lbl.configure(text=txt, style="Err.TLabel" if error else "TLabel")

    def _update_status(self):
        s = self.sess
        t = s.totals
        txt = f"Sesión: {miles(t['in'])} entrada · {miles(t['out'])} salida"
        if t["reason"]:
            txt += f" ({miles(t['reason'])} de razonamiento)"
        e = self._entry_for(s.model)
        if self.last_usage:
            ctx = e.get("context") or 0
            txt += f"  │  Contexto: {miles(self.last_usage.get('prompt_tokens', 0))}" + (f" de {miles(ctx)}" if ctx else "")
        self.tokens_lbl.configure(text=txt)
        self.balance_lbl.configure(text="Saldo: n/a (modelo local)" if e["kind"] == "local" else f"Saldo: {self.balance_text}")
        if not self.busy:
            self._set_state("Enter envía · Shift+Enter salto de línea")

    def refresh_balance(self):
        key = self.cfg.api_key
        if not key:
            return

        def work():
            try:
                b = dsapi.get_balance(key)
                txt = b["text"] + ("" if b["available"] else "  (cuenta no disponible)")
            except dsapi.ApiError as e:
                txt = f"error ({e})"
            self.post(lambda: self._balance_loaded(txt))
        threading.Thread(target=work, daemon=True).start()

    def _balance_loaded(self, txt):
        self.balance_text = txt
        self._update_status()

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def open_settings(self):
        dialogs.SettingsDialog(self)

    # ------------------------------------------------------------ envío

    def _on_enter(self, _e):
        if not self.busy:               # con el agente trabajando, Enter no debe cortarlo: se sigue escribiendo
            self.send()
        return "break"

    def on_send_click(self):
        if self.busy:
            self.cancel()
        else:
            self.send()

    # ------------------------------------------------------------ acceso remoto (celular)

    def remote_status(self):
        if self.busy:
            txt = self.state_lbl.cget("text")
            return txt or "Trabajando…"
        return "Listo"

    def remote_send(self, text):
        if self.busy:
            return False, "El agente está trabajando: esperá o detenelo."
        e = self._entry_for(self.sess.model)
        if e["kind"] == "remote" and not self.cfg.api_key:
            return False, "Falta la API key en la PC (o está bloqueada con contraseña)."
        self.send(text, keep_draft=True)         # keep_draft: no toca lo que la persona tenga escrito en la PC
        return True, ""

    def remote_cancel(self):
        if not self.busy:
            return False, "No hay nada en curso."
        self.cancel()
        return True, ""

    def remote_continue(self):
        if self.busy or not self._can_continue:
            return False, "No hay nada para continuar."
        self.continue_run()
        return True, ""

    def remote_confirm(self, cid, allow):
        p = self._pending_confirm
        if p is None or p["id"] != cid:
            return False, "Ese permiso ya se respondió (o venció)."
        res = "allow" if allow else "deny"
        d = self._dialog
        if d is not None:
            d.answer(res)                        # cierra también la ventana de la PC
        else:
            p["done"](res)
        return True, ""

    def start_remote(self):
        """Enciende el servidor. Devuelve (ok, mensaje). Genera el token la primera vez."""
        if self.remote_srv is not None:
            return True, ""
        if not self.cfg["remote_token"]:
            self.cfg["remote_token"] = remote.new_token()
        srv = remote.RemoteServer(remote.AppBridge(self), self.cfg["remote_token"], int(self.cfg["remote_port"]))
        try:
            srv.start()
        except OSError as e:
            return False, f"No se pudo abrir el puerto {self.cfg['remote_port']}: {e}"
        self.remote_srv = srv
        return True, ""

    def stop_remote(self):
        if self.remote_srv is not None:
            self.remote_srv.stop()
            self.remote_srv = None

    def set_remote(self, enabled):
        """Enciende o apaga y lo deja guardado. Devuelve (ok, mensaje)."""
        if enabled:
            ok, msg = self.start_remote()
        else:
            self.stop_remote()
            ok, msg = True, ""
        self.cfg["remote_enabled"] = bool(enabled and ok)
        try:
            self.cfg.save()
        except OSError as e:
            return False, f"No se pudo guardar la configuración: {e}"
        return ok, msg

    def regenerate_remote_token(self):
        """Invalida el enlace anterior (p. ej. si se compartió por error)."""
        self.cfg["remote_token"] = remote.new_token()
        was_on = self.remote_srv is not None
        self.stop_remote()
        try:
            self.cfg.save()
        except OSError:
            pass
        return self.start_remote() if was_on else (True, "")

    def open_remote(self):
        dialogs.RemoteDialog(self)

    def _local_server(self):
        exe = localmodels.find_llama_server(self.cfg["llama_server_path"])
        if not exe:
            raise localmodels.LocalError("No se encontró llama-server.exe (Configuración → Modelos locales).")
        key = (exe, int(self.cfg["local_ctx"]))
        if self._server is None or self._server_key != key:
            if self._server is not None:
                self._server.stop()
            self._server = localmodels.LocalServer(exe, os.path.join(self.cfg.dir, "logs"), key[1])
            self._server_key = key
        return self._server

    def _show_continue(self, show):
        self._can_continue = bool(show)
        if self._blink_job is not None:
            self.root.after_cancel(self._blink_job)
            self._blink_job = None
        if show:
            self.btn_continue.pack(side="left", anchor="s", padx=(8, 0), before=self.btn_send)
            self.continue_flag.place(in_=self.chat.frame, relx=0.5, rely=0.5, anchor="center")
            self.continue_flag.lift()
            self._blink_on = True
            self._blink()
        else:
            self.btn_continue.pack_forget()
            self.continue_flag.place_forget()

    def _blink(self):
        """Alterna los colores del cartel cada medio segundo (un destello por segundo: lejos del umbral de 3 por
        segundo que puede disparar crisis fotosensibles)."""
        try:
            bg, fg = (theme.LIMON, theme.NEGRO) if self._blink_on else (theme.NEGRO, theme.LIMON)
            self.continue_flag.configure(bg=bg, fg=fg)
            self._blink_on = not self._blink_on
            self._blink_job = self.root.after(500, self._blink)
        except tk.TclError:       # la ventana se cerró con el cartel a la vista
            self._blink_job = None

    def continue_run(self):
        """Retoma la tarea tras el tope de pasos. Lo que el usuario tenga escrito o adjunto queda intacto."""
        self.send(CONTINUE_TEXT, keep_draft=True)

    def send(self, text=None, keep_draft=False):
        """Manda el texto del campo de entrada (o el indicado, para tests) junto con los adjuntos.
        Con keep_draft=True no toca lo que haya en el campo ni los adjuntos (así funciona «Continuar»)."""
        if self.busy:
            return
        if text is None:
            text = self.input.get("1.0", "end-1c")
        text = text.strip()
        if not text and not self.attachments:
            return
        s = self.sess
        entry = self._entry_for(s.model)
        if entry["kind"] == "remote" and not self.cfg.api_key:
            self._key_note()
            if self.interactive:
                if self.cfg.needs_unlock:
                    dialogs.UnlockDialog(self)
                else:
                    self.open_settings()
            return
        files = [] if keep_draft else list(self.attachments)
        try:
            content = dsapi.build_message(text, files)
        except (dsapi.AttachError, OSError) as e:
            self.warn(str(e))
            return
        n0 = len(s.messages)
        s.messages.append({"role": "user", "content": content})
        self.chat.user(text, files)
        if not keep_draft:
            self.input.delete("1.0", "end")
            self.attachments = []
            self._redraw_attachments()
        self._show_continue(False)
        self.cancel_ev = threading.Event()
        self._set_busy(True)
        self.worker = threading.Thread(target=self._work, args=(s, entry, self.toolbox, n0, text, files, self.cancel_ev), daemon=True)
        self.worker.start()

    def _set_busy(self, busy):
        self.busy = busy
        self.btn_send.configure(text="Detener" if busy else "Enviar")
        self.btn_undo.configure(state="disabled" if busy else "normal")
        if busy:
            self._set_state("Pensando…")
        else:
            self._update_status()

    def cancel(self):
        if not self.busy:
            return
        self.cancel_ev.set()
        a = self.agent
        if a is not None:
            a.cancel_stream()
        self._set_state("Deteniendo…")
        self._dismiss_dialog()

    # ------------------------------------------------------------ hilo del agente

    def _make_stream(self, entry, s, base, api_msgs, tools, toolbox):
        system = prompts.system_prompt(toolbox.root if toolbox else "", self.cfg["system_prompt"])
        msgs = [{"role": "system", "content": system}] + api_msgs
        if self._stream_factory is not None:
            return self._stream_factory(entry, msgs, tools)
        if entry["kind"] == "local":
            return dsapi.ChatStream(None, entry["name"], msgs, None, self._local_max_tokens(), base=base, tools=tools, timeout=LOCAL_TIMEOUT)
        effort = s.effort if s.effort in (entry["efforts"] or []) else None   # el de un modelo local no viaja a DeepSeek
        return dsapi.ChatStream(self.cfg.api_key, entry["id"], msgs, effort, self.cfg["max_tokens"], tools=tools)

    def _local_max_tokens(self):
        """Tope de respuesta de un modelo local. Es también lo que se le reserva dentro de la ventana: el historial
        se recorta para que prompt + respuesta entren en local_ctx (si no, llama-server corta la respuesta a la mitad)."""
        return max(512, min(int(self.cfg["max_tokens"]), int(self.cfg["local_ctx"]) // 3))

    def _agent_budget(self, entry, toolbox):
        if entry["kind"] != "local":
            return {"budget_chars": REMOTE_BUDGET}
        system = prompts.system_prompt(toolbox.root if toolbox else "", self.cfg["system_prompt"])
        overhead = len(system) + (len(json.dumps(toolbox.specs, ensure_ascii=False)) if toolbox else 0)
        return {"ctx_tokens": int(self.cfg["local_ctx"]), "reserve_tokens": self._local_max_tokens(), "overhead_chars": overhead,
                "chars_per_token": self._cpt.get(entry.get("path") or entry["name"], ag.DEFAULT_CHARS_PER_TOKEN)}

    def _work(self, s, entry, toolbox, n0, text, files, cancel):
        outcome, error, agent = None, None, None

        def emit(kind, *a):
            if kind == "usage":
                u = a[0]
                s.totals["in"] += u.get("prompt_tokens", 0)
                s.totals["out"] += u.get("completion_tokens", 0)
                s.totals["reason"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                s.totals["hit"] += u.get("prompt_cache_hit_tokens", 0)
            elif kind in ("step_end", "tool_result") and not ag.validate(s.messages):
                try:
                    self.store.save(s)      # un corte de luz o un cierre brusco no pierde lo ya hecho
                except OSError as e:
                    self.post(lambda: self.chat.note(f"No se pudo guardar la sesión: {e}", error=True))
            self.post(lambda: self._handle(kind, *a))

        try:
            base = None
            if entry["kind"] == "local" and self._stream_factory is None:
                self.post(lambda: self._set_state("Cargando el modelo local (puede tardar un rato)…"))
                if entry.get("missing"):
                    raise localmodels.LocalError(f"No se encuentra el modelo {entry['path']} (¿está conectado el pendrive?).")
                base = self._local_server().ensure(entry["path"], int(self.cfg["local_ctx"]), cancel,
                                                   reasoning=localmodels.reasoning_for(entry["path"], s.effort))
            agent = ag.Agent(lambda msgs, tools: self._make_stream(entry, s, base, msgs, tools, toolbox), toolbox,
                             max_steps=self.max_steps, label=self._label_of(entry), **self._agent_budget(entry, toolbox))
            self.agent = agent
            outcome = agent.run(s.messages, emit, cancel)
        except Exception as e:                      # noqa: BLE001 — cualquier falla se muestra, ninguna se traga
            if cancel.is_set():
                outcome = "cancelled"
            elif isinstance(e, (dsapi.ApiError, localmodels.LocalError)):
                error = str(e)
            else:
                error = f"Error inesperado: {e!r}"
        finally:
            self.agent = None
            if agent is not None and agent.ctx_tokens:
                self._cpt[entry.get("path") or entry["name"]] = agent.chars_per_token
            ag.repair(s.messages)
            restore = None
            if (error or outcome == "cancelled") and len(s.messages) == n0 + 1 and s.messages[-1].get("role") == "user":
                s.messages.pop()                    # no hubo respuesta: se devuelve el texto para poder reintentar
                restore = (text, files)
            try:
                self.store.save(s)
            except OSError as e:
                self.post(lambda: self.chat.note(f"No se pudo guardar la sesión: {e}", error=True))
            self.post(lambda: self._run_finished(outcome, error, restore))

    def _confirm(self, kind, title, detail):
        """Corre en el hilo del agente: pide permiso en la ventana y espera la respuesta sin bloquear la interfaz."""
        ev, box = threading.Event(), {}
        self._confirm_seq += 1
        pending = {"id": self._confirm_seq, "kind": kind, "title": title, "detail": detail}

        def done(r):
            if "r" in box:                  # la PC y el celular pueden responder a la vez: gana la primera
                return
            box["r"] = r
            if self._pending_confirm is pending:
                self._pending_confirm = None
            ev.set()
        pending["done"] = done

        def show():
            try:
                self._dialog = dialogs.ConfirmDialog(self, kind, title, detail, done)
                self._pending_confirm = pending
            except tk.TclError:
                done("deny")
        self.post(show)
        while not ev.wait(0.1):
            if self.cancel_ev.is_set():
                self.post(self._dismiss_dialog)
                return False
        if box["r"] == "allow_session":
            if self.toolbox is not None:
                self.toolbox.approval = "edits"
            self.post(lambda: self._set_approval("edits"))
        return box["r"] != "deny"

    def _dismiss_dialog(self):
        d, self._dialog = self._dialog, None
        if d is not None:
            try:
                if d.win.winfo_exists():
                    d.dismiss()
            except tk.TclError:
                pass

    # ------------------------------------------------------------ eventos del agente (hilo de la ventana)

    def _handle(self, kind, *a):
        c = self.chat
        if kind == "step_begin":
            self._live = {"content": "", "rlen": 0}
            self.meter.step_begin()
            c.assistant_header(self._label())
            c.begin_stream()
            self._set_state("Pensando…")
        elif kind in ("reasoning", "content"):
            c.stream_piece(kind, a[0])
            if kind == "content":
                self._live["content"] = (self._live["content"] + a[0])[-6000:]
            else:
                self._live["rlen"] += len(a[0])
            self.meter.piece(a[0])
            self._set_state("Razonando…" if kind == "reasoning" else "Escribiendo…")
        elif kind == "usage":
            self.last_usage = a[0]
            self.meter.usage(a[0])
            self._update_status_keep_busy()
        elif kind == "step_end":
            p = a[0]
            self.meter.step_end()
            self._live = {"content": "", "rlen": 0}
            c.clear_stream()
            note = "⚠ La respuesta se cortó por el límite de tokens de salida (ajustable en Configuración)." if p.get("finish") == "length" else None
            c.assistant_body(p.get("reasoning", ""), p.get("content", ""), note, final=not p.get("calls"))
        elif kind == "tool_start":
            c.tool_call(a[1], a[2])
            self._set_state(f"Ejecutando {a[1]}…")
        elif kind == "tool_result":
            c.tool_result(a[2])
            if a[1] in MUTATING:
                self.explorer.refresh()
            self._set_state("Pensando…")
        elif kind == "notice":
            c.note("• " + a[0])

    def _update_status_keep_busy(self):
        txt = self.state_lbl.cget("text")
        self._update_status()
        if self.busy:
            self._set_state(txt)

    def _run_finished(self, outcome, error, restore):
        c = self.chat
        c.clear_stream()
        self._live = {"content": "", "rlen": 0}
        self.meter.step_end()          # un paso cortado por un error de red no se pierde del acumulado
        self._dismiss_dialog()
        if restore:
            # el mensaje del usuario ya no está en el historial: se redibuja sin él y su texto vuelve al campo de entrada
            c.render_session(self.sess.messages)
            text, files = restore
            if text and text != CONTINUE_TEXT and not self.input.get("1.0", "end-1c").strip():
                self.input.insert("1.0", text)
            for p in files:
                if p not in self.attachments:
                    self.attachments.append(p)
            self._redraw_attachments()
        if error:
            c.note("✖ " + error, error=True)
        elif outcome == "cancelled":
            c.note("■ Interrumpido.")
        self._set_busy(False)
        # también si «Continuar» mismo falló (red caída): el botón vuelve a estar para reintentar
        self._show_continue((outcome == "max_steps" and not error) or bool(restore and restore[0] == CONTINUE_TEXT))
        self.refresh_sessions()
        self.explorer.refresh()
        self._retitle()
        if self._entry_for(self.sess.model)["kind"] == "remote":
            self.refresh_balance()

    # ------------------------------------------------------------ deshacer

    def undo(self):
        if self.busy:
            return
        if self.toolbox is None:
            self.chat.note("No hay carpeta de trabajo abierta: no hay nada que deshacer.")
            return
        try:
            msg = self.toolbox.undo_last()
        except OSError as e:
            msg = f"No se pudo deshacer: {e}"
        self.chat.note("↶ " + msg)
        self.explorer.refresh()
        self._persist()

    # ------------------------------------------------------------ cierre

    def on_close(self):
        if self.busy:
            self.cancel_ev.set()
            a = self.agent
            if a is not None:
                a.cancel_stream()
            if self.worker is not None:
                self.worker.join(4)
        try:
            ag.repair(self.sess.messages)
            self.store.save(self.sess)
        except (OSError, AttributeError):
            pass
        try:
            if self.sidebar_visible:
                self.cfg["sidebar_w"] = max(160, self.main.sashpos(0))
            self.cfg["sidebar_visible"] = self.sidebar_visible
            estado = self.root.state()
            self.cfg["win_zoomed"] = estado == "zoomed"
            if estado == "normal":         # maximizada o minimizada, la geometría no es la de trabajo: se conserva la anterior
                self.cfg["win_geometry"] = self.root.geometry()
            self.cfg.save()
        except (tk.TclError, OSError):
            pass
        self.stop_remote()
        if self._server is not None:
            self._server.stop()
        try:
            self.root.after_cancel(self._meter_job)
        except (tk.TclError, AttributeError):
            pass
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        # sin identidad propia, la barra de tareas agrupa la ventana bajo pythonw.exe y muestra su ícono, no el nuestro
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeepSeekChat")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
