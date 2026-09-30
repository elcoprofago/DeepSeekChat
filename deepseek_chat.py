"""CodeAgent (antes DeepSeek Chat): agente de programación con ventana. Sesiones, explorador de carpetas, modelos por API
(DeepSeek, Claude, OpenAI) y locales.

Toda la lógica del agente vive en agent.py / agent_tools.py; acá solo está la ventana y el hilo que las conecta.
Regla de hilos: tkinter solo se toca desde el hilo principal. El hilo de trabajo deja lo que quiere mostrar en una
cola (self.post) y _drain_ui lo ejecuta.
"""
import ctypes
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

import agent as ag
import agent_tools as at
import chatview
import claudeapi
import dialogs
import dsapi
import explorer
import localmodels
import logpanel
import meter
import prompts
import providers
import remote
import sessions
import theme
import version

TITULO = "CodeAgent"
TITULO_VENTANA = f"CodeAgent v{version.VERSION} - © R.A. Sistemas - 2026"      # formato de USBagent, más la versión
ICONO = "asterisc.ico"      # junto a los .py; el mismo va embebido en el lanzador CodeAgent.exe
IMG_ENVIAR = "b-env.png"    # botón de enviar; junto a los .py
IMG_DETENER = "b-stop.png"  # el mismo botón mientras el agente trabaja
IMG_CLIP = "b-clip.png"     # adjuntar archivos, arriba del de enviar
CLAUDE_COST_EVERY = 60      # s entre consultas del gasto de Claude (la API pide no más de una por minuto); ⟳ no espera
GEOMETRIA_INICIAL = "1240x780"
SIN_EFFORT = "(por defecto)"
APPROVALS = {"ask": "Preguntar todo", "edits": "Editar sin preguntar", "all": "Todo sin preguntar"}
MUTATING = {"write_file", "edit_file", "delete_path", "run_command"}
CONTINUE_TEXT = "Seguí con lo que estabas haciendo, desde donde quedaste."
BUSY_SESSION_NOTE = "Hay un agente trabajando. Esperá a que termine o presioná «Detener» antes de cambiar de sesión."
BUSY_MODEL_NOTE = "Hay un agente trabajando. Esperá a que termine o presioná «Detener» antes de cambiar de modelo."
BUSY_POWER_NOTE = "Hay un agente trabajando. Esperá a que termine o presioná «Detener» antes de apagar o suspender la PC."
SHUTDOWN_DELAY = 60     # segundos entre el pedido del celular y el apagado; en ese tiempo se puede cancelar en la PC
SUSPEND_DELAY_MS = 3000  # la respuesta HTTP sale antes de que la PC se suspenda
REMOTE_BUDGET = 600000
LOCAL_TIMEOUT = 900
miles = chatview.miles


def system_power(action):
    """Lo único que toca de verdad la energía de la PC ("shutdown", "cancel" o "suspend"). Devuelve (ok, mensaje).
    Los tests la reemplazan por app.power_fn: nunca se apaga la PC en un test."""
    if action == "suspend":
        ok = ctypes.windll.powrprof.SetSuspendState(False, False, False)
        return bool(ok), "" if ok else f"Windows no suspendió la PC (error {ctypes.GetLastError()})."
    if action == "shutdown":
        args = ["shutdown", "/s", "/t", str(SHUTDOWN_DELAY), "/c", "Apagado pedido desde MovilDeep (CodeAgent)."]
    elif action == "cancel":
        args = ["shutdown", "/a"]
    else:
        return False, f"acción desconocida: {action}"
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"No se pudo ejecutar shutdown: {e}"
    if r.returncode != 0:
        return False, f"shutdown devolvió {r.returncode}: {(r.stderr or r.stdout).strip()}"
    return True, ""


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
        # los modelos por API de todos los proveedores con key; cada entrada lleva 'provider' (ver providers.py)
        self.remote_models = providers.fallback("deepseek") + [e for p in providers.ORDER[1:] if self.cfg.key(p)
                                                                   for e in providers.fallback(p)]
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
        self.power_fn = system_power     # reemplazable en los tests
        self._shutdown_win = None
        self._live = {"content": "", "rlen": 0}      # el paso en curso, para quien mira por el celular
        self._pending_confirm = None
        self._loading = None        # hora de inicio mientras se carga un modelo local (spinner del log)
        self._run_t0 = 0.0
        self._step = {"n": 0, "chars": 0, "t": 0.0}      # el paso en curso, para la línea viva del log
        self._confirm_seq = 0
        self._can_continue = False
        # cómo terminó la última ejecución, para MovilDeep: seq sube en 1 con cada fin (así un fin entre dos consultas no se pierde)
        self._run_seq, self._run_outcome, self._run_msg = 0, "", ""
        self._last_notice = ""
        self.last_usage = None
        self.balance_text = "—"
        self.claude_cost_text = ""       # gasto del mes en Claude (claudeapi.month_summary), para la barra de estado
        self._claude_cost_t = 0.0        # cuándo se consultó por última vez
        self._claude_cost_denied = ""    # key común que ya contestó «sin permiso»: no se la vuelve a probar sola
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
                self._drain_job = self.root.after(40, self._drain_ui)
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

        # arriba la zona de trabajo (sesiones/explorador + chat), abajo el panel de log; el separador se arrastra
        self.vpane = ttk.PanedWindow(r, orient="vertical")
        self.main = ttk.PanedWindow(self.vpane, orient="horizontal")
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
        self.sess_tree = ttk.Treeview(tbox, columns=("title", "folder"), show="headings", selectmode="browse", height=6,
                                     style="Side.Treeview")
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
        self.input = tk.Text(self.bottom, height=4, wrap="word", relief="flat", padx=8, pady=6, highlightthickness=1, undo=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)
        # imagen de enviar / detener; el texto queda guardado (con imagen, tk no lo muestra) y es el de respaldo si
        # falta alguna de las dos imágenes
        self.img_send = self._load_image(IMG_ENVIAR)
        self.img_stop = self._load_image(IMG_DETENER)
        self.img_clip = self._load_image(IMG_CLIP)
        # columna a la derecha del cuadro: adjuntar (clip) arriba de enviar, como en ExcelAgent
        self.send_col = ttk.Frame(self.bottom)
        self.send_col.pack(side="right", anchor="s", padx=(8, 0))
        if self.img_clip:
            self.btn_plus = tk.Button(self.send_col, text="+", image=self.img_clip, bd=0, relief="flat",
                                      highlightthickness=0, cursor="hand2", command=self.add_files)
        else:
            self.btn_plus = ttk.Button(self.send_col, text="+", width=9, command=self.add_files)
        self.btn_plus.pack(side="top", pady=(0, 4))
        if self.img_send and self.img_stop:
            self.btn_send = tk.Button(self.send_col, text="Enviar", image=self.img_send, bd=0,
                                      relief="flat", highlightthickness=0, cursor="hand2", command=self.on_send_click)
        else:
            self.btn_send = ttk.Button(self.send_col, text="Enviar", width=9, command=self.on_send_click)
        self.btn_send.pack(side="top")
        # el cuadro va último: se queda con el ancho que sobra y la columna de botones nunca se recorta
        self.input.pack(side="left", fill="x", expand=True)
        # solo se ve cuando el agente cortó por el tope de pasos: un clic y sigue, sin escribir nada
        self.btn_continue = ttk.Button(self.bottom, text="Continuar", width=10, command=self.continue_run,
                                       style="Continue.TButton")
        # cartel chico en el centro del chat que titila mientras el botón está a la vista; un clic hace lo mismo
        self.continue_flag = tk.Label(self.col, text="⏸  Tope de pasos: apretá «Continuar»", font=("Segoe UI", 10, "bold"),
                                      bg=theme.LIMON, fg=theme.NEGRO, padx=14, pady=6, bd=1, relief="solid", cursor="hand2")
        self.continue_flag.bind("<Button-1>", lambda e: self.continue_run())
        self._blink_job = None
        self._blink_on = True
        # a todo el ancho de la ventana, también debajo de sesiones/explorador (se empaqueta antes que self.main)
        self.status = ttk.Frame(r, padding=(10, 2, 10, 6))
        self.tokens_lbl = ttk.Label(self.status, text="")
        self.tokens_lbl.pack(side="left")
        ttk.Button(self.status, text="⟳", width=3, command=lambda: self.refresh_balance(force=True)).pack(side="right")
        self.balance_lbl = ttk.Label(self.status, text="Saldo: —")
        self.balance_lbl.pack(side="right", padx=6)
        self.balance_lbl.bind("<Button-1>", lambda e: self._balance_click())
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
        self.logp = logpanel.LogPanel(r, self.vpane, self.cfg, post=self.post, follow=lambda: self.busy,
                                      fit=lambda g: fit_geometry(g, virtual_screen(r), 360, 160))
        self.vpane.add(self.main, weight=1)
        self.vpane.add(self.logp.frame, weight=0)
        self.vpane.pack(fill="both", expand=True, padx=10)
        self.root.after(50, self._place_sash)
        self.root.after(80, self._place_log)

    def _place_log(self):
        try:
            self.root.update_idletasks()
            if self.cfg["log_detached"]:
                self.logp.detach()
            else:
                self.logp.place()
        except (tk.TclError, ValueError):
            pass

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

    def _load_image(self, name):
        """PhotoImage de un archivo junto a los .py, o None si falta o Tk no lo puede leer (entonces va el botón con texto)."""
        p = os.path.join(dsapi.APP_DIR, name)
        try:
            return tk.PhotoImage(master=self.root, file=p) if os.path.isfile(p) else None
        except tk.TclError:
            return None

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
        self.input.configure(bg=t["side"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"],
                             highlightcolor=t["accent"], font=("Segoe UI", fs))
        if isinstance(self.btn_send, tk.Button):
            self.btn_send.configure(bg=t["bg"], activebackground=t["bg"])
        else:
            self.btn_send.configure(style="Accent.TButton")
        if isinstance(self.btn_plus, tk.Button):
            self.btn_plus.configure(bg=t["bg"], activebackground=t["bg"])
        self.sess_tree.tag_configure("cur", font=("Segoe UI", 10, "bold"))
        self._dark_titlebar()
        self._draw_meter()

    # ------------------------------------------------------------ arranque

    def log(self, msg, level="INFO", overwrite=False, protect=False):
        """Una línea en el panel de log (logpanel.py). Se puede llamar desde cualquier hilo."""
        self.logp.log(msg, level, overwrite, protect)

    def _log_keys(self):
        estado = []
        for p in providers.ORDER:
            s = "cargada" if self.cfg.key(p) else "bloqueada con contraseña" if self.cfg.needs_unlock_of(p) else "sin cargar"
            estado.append(f"{providers.name_of(p)} {s}")
        self.log("API keys: " + " · ".join(estado) + ".")

    def _startup(self):
        self.log(f"CodeAgent {version.VERSION} iniciado.", "OK", protect=True)
        self.log(f"Configuración y sesiones en {self.cfg.dir}"
                 + (" (portable)." if os.path.isfile(os.path.join(os.path.dirname(dsapi.APP_DIR), "portable.flag")) else "."))
        self._log_keys()
        if self.cfg.load_warning:
            self.chat.note(self.cfg.load_warning, error=True)
            self.log(self.cfg.load_warning, "WARN")
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
            self.log(w, "WARN")
        self._key_prompt()
        if self.cfg["remote_enabled"]:
            ok, msg = self.start_remote()
            if not ok:
                self.chat.note(msg, error=True)
        self.refresh_models()
        self.refresh_balance()
        self.log("Buscando modelos locales (.gguf)…")
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        found = localmodels.scan_models(localmodels.default_model_dirs(self.cfg))
        self.post(lambda: self._set_local_models(found))

    def _provider(self):
        """El proveedor cuya key necesita el modelo actual. Con un modelo local, DeepSeek (el aviso de siempre)."""
        e = self._entry_for(self.sess.model) if self.sess and self.sess.model else None
        return self._prov(e) if e and e["kind"] == "remote" else "deepseek"

    @staticmethod
    def _prov(e):
        return e.get("provider") or providers.provider_of(e["id"])

    def _key_note(self):
        """Deja (o quita) el aviso gris de key. Idempotente: se puede llamar cuantas veces haga falta."""
        self.chat.clear_tagged("nokey")
        p = self._provider()
        if self.cfg.key(p):
            return
        name = providers.name_of(p)
        if self.cfg.needs_unlock_of(p):
            self.chat.note(f"La API key de {name} está guardada con contraseña. Desbloquéala para usar los modelos de {name} "
                           "(los locales funcionan sin key).", False, "nokey")
        else:
            self.chat.note(f"Falta la API key de {name}. Abrí ⚙ Configuración para cargarla (los modelos locales no la necesitan).",
                           False, "nokey")

    def _key_prompt(self):
        """Abre la ventana que corresponde al arrancar, sin insistir si ya hay un modelo local para trabajar."""
        if not self.interactive or self.cfg.key(self._provider()):
            return
        if self.cfg.locked():
            self.root.after(300, lambda: dialogs.UnlockDialog(self))
        elif not self.local_models and not any(self.cfg.key(p) for p in providers.ORDER):
            self.root.after(300, self.open_settings)

    def key_changed(self):
        self._log_keys()
        self._key_note()
        self.refresh_models()
        self.refresh_balance(force=True)

    # ------------------------------------------------------------ modelos

    def _entry_for(self, mid):
        """El modelo (remoto o local) de un id guardado. Si ya no está en las listas se fabrica uno, para no perder la sesión."""
        for m in self.remote_models:
            if m["id"] == mid:
                return {**m, "kind": "remote", "provider": self._prov(m)}
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
        p = providers.provider_of(mid)
        for m in providers.fallback(p):          # sin key o sin listar todavía: los datos conocidos del modelo
            if m["id"] == mid:
                return m
        return {"id": mid, "kind": "remote", "provider": p, "name": providers.api_model(mid), "efforts": [], "context": 0}

    def _display(self, entry):
        if entry["kind"] == "remote":
            p = self._prov(entry)
            return entry["id"] if p == "deepseek" else f"[{providers.name_of(p)}] {providers.api_model(entry['id'])}"
        if entry.get("missing"):
            return f"[local] {entry['name']} (no encontrado)"
        return f"[local] {entry['name']}" + (f" ({localmodels.format_size(entry['size'])})" if entry.get("size") else "")

    def _all_entries(self):
        return [{**m, "kind": "remote", "provider": self._prov(m)} for m in self.remote_models] + \
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
        if mid != self.sess.model:
            self.log(f"Modelo: {self.model_var.get()}")
        self.sess.model = mid
        self.cfg["model"] = mid
        try:
            self.cfg.save()
        except OSError:
            pass
        self._refresh_effort_widgets()
        self._key_note()
        self._update_status()
        self._persist()

    def on_effort_change(self):
        v = self.effort_var.get()
        self.sess.effort = "" if v in (SIN_EFFORT, "—") else v
        self.cfg["effort"] = self.sess.effort
        self._save_cfg()
        self._persist()

    def refresh_models(self):
        """Lista los modelos de cada proveedor con key, cada uno en su hilo. Los de un proveedor sin key salen de la
        lista (los de DeepSeek quedan: son los de siempre, y el aviso de key explica qué falta)."""
        for p in providers.ORDER:
            key = self.cfg.key(p)
            if not key:
                if p != "deepseek" and any(self._prov(m) == p for m in self.remote_models):
                    self._set_remote_models([], p)
                continue
            if p != "deepseek" and not any(self._prov(m) == p for m in self.remote_models):
                self._set_remote_models(providers.fallback(p), p)      # se ven ya, mientras llega la lista real

            def work(p=p, key=key):
                try:
                    res = providers.list_models(p, key)
                except dsapi.ApiError as e:
                    self.post(lambda: self._set_state(f"No se pudo listar los modelos de {providers.name_of(p)}: {e}", error=True))
                    return
                self.log(f"Modelos de {providers.name_of(p)}: {len(res)} disponibles.", "OK")
                self.post(lambda: self._set_remote_models(res, p))
            threading.Thread(target=work, daemon=True).start()

    def _set_remote_models(self, res, provider="deepseek"):
        """Reemplaza los modelos de un proveedor, conservando los de los demás (en el orden de providers.ORDER)."""
        keep = [m for m in self.remote_models if self._prov(m) != provider]
        new = [{**m, "kind": "remote", "provider": provider} for m in res]
        allm = keep + new
        self.remote_models = sorted(allm, key=lambda m: providers.ORDER.index(self._prov(m)))
        self._refresh_model_widgets()

    def _set_local_models(self, found):
        self.log(f"Modelos locales encontrados: {len(found)}.", "OK" if found else "INFO")
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
        self.log(f"Sesión abierta: «{s.display_title}»" + (f" · {len(s.messages)} mensajes" if s.messages else " (vacía)"))
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
        if e is None:
            return "DeepSeek"
        return providers.name_of(e.get("provider") or providers.provider_of(e["id"])) if e["kind"] == "remote" else e["name"]

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
        self.chat.note(BUSY_SESSION_NOTE)

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
        self.log(f"Carpeta de trabajo: {path}")
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
        if error:
            self.log(txt, "ERROR")

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
        self.balance_lbl.configure(text="Saldo: " + self.balance_status(),
                                   cursor="hand2" if self._balance_prov() == "anthropic" else "")
        if not self.busy:
            self._set_state("Enter envía · Shift+Enter salto de línea")

    def _balance_prov(self):
        """Proveedor del modelo en uso, o None si es local."""
        e = self._entry_for(self.sess.model)
        return None if e["kind"] == "local" else self._prov(e)

    def balance_status(self):
        """El saldo como lo muestra la barra de estado (sin «Saldo: »); también lo lee MovilDeep por /api/state.
        Claude: lo gastado en el mes, si la key lo permite; el disponible no lo da ninguna API (clic: la consola)."""
        p = self._balance_prov()
        if p == "anthropic":
            return (self.claude_cost_text or "gasto sin consultar") + " · disponible: clic → consola"
        if p == "deepseek":
            return self.balance_text
        # modelo local o proveedor sin saldo por API: igual se muestran los saldos ya consultados de las otras keys
        known = []
        if self.cfg.api_key and self.balance_text != "—":
            known.append(f"DeepSeek {self.balance_text}")
        if self.claude_cost_text:
            known.append(f"Claude {self.claude_cost_text}")
        head = "n/a (modelo local)" if p is None else f"no disponible por API ({providers.name_of(p)})"
        return " · ".join([head] + known)

    def _balance_click(self):
        if self._balance_prov() == "anthropic":
            webbrowser.open(claudeapi.BILLING_URL)

    def refresh_balance(self, force=False):
        """Saldo de DeepSeek y gasto del mes en Claude, en segundo plano. force: aunque no haya pasado el minuto."""
        self._refresh_claude_cost(force)
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

    def _refresh_claude_cost(self, force=False):
        """Con la Admin key si está cargada; si no, se prueba la key común (sirve si es personal y sin workspace)."""
        admin = self.cfg.key(providers.ADMIN)
        key = admin or self.cfg.key("anthropic")
        if not key:
            self.claude_cost_text = ""
            return
        now = time.time()
        if not force and (key == self._claude_cost_denied or now - self._claude_cost_t < CLAUDE_COST_EVERY):
            return
        self._claude_cost_t = now

        def work():
            try:
                res = (claudeapi.month_summary(key), None)
            except dsapi.ApiError as e:
                res = (None, e)
            self.post(lambda: self._claude_cost_loaded(key, bool(admin), *res))
        threading.Thread(target=work, daemon=True).start()

    def _claude_cost_loaded(self, key, admin, summary, err):
        if err is None:
            self._claude_cost_denied = ""
            self.claude_cost_text, msg, level = providers.claude_cost_texts(summary)
            self.log(f"{msg} El disponible solo figura en {claudeapi.BILLING_URL}.", level)
        elif not admin and getattr(err, "status", None) in (401, 403, 404):
            self._claude_cost_denied = key
            self.claude_cost_text = "gasto: no disponible por API"
            self.log(f"La key de Claude no puede leer el informe de costos ({err}). Ese informe pide una key "
                     "con ámbito «Organización» (Consola → Claves de API → Crear clave → Ámbito: Organización; "
                     "la cuenta tiene que ser de organización, no individual) o una Admin key "
                     "(sk-ant-admin01-…); cualquiera de las dos va en Configuración → «Claude (Admin)». "
                     "El saldo de créditos no lo da ninguna API: se ve en "
                     f"{claudeapi.BILLING_URL} (clic en el saldo de la barra).", "WARN")
        else:
            self.claude_cost_text = "gasto: error"
            self.log(f"Gasto de Claude: {err}", "ERROR")
        self._update_status()

    def _balance_loaded(self, txt):
        self.log(f"Saldo de DeepSeek: {txt}", "ERROR" if txt.startswith("error") else "INFO")
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

    @staticmethod
    def _with_uploads(text, uploads):
        """El texto con los avisos de los adjuntos del celular, y los pares (nombre, contenido) a incrustar."""
        notes = [u["note"] for u in uploads if u.get("note")]
        extra = [(u["att_name"], u["text"]) for u in uploads if u.get("text") is not None]
        return "\n\n".join([t for t in [text] + notes if t]), extra

    def remote_send(self, text, uploads=()):
        if self.busy:
            return False, "El agente está trabajando: esperá o detenelo."
        e = self._entry_for(self.sess.model)
        if e["kind"] == "remote" and not self.cfg.key(self._prov(e)):
            return False, f"Falta la API key de {providers.name_of(self._prov(e))} en la PC (o está bloqueada con contraseña)."
        full_text, extra = self._with_uploads(text, uploads)
        try:
            dsapi.build_message(full_text, [], extra)     # el tope de 2 MB entre todos: se avisa al celular, no a la PC
        except dsapi.AttachError as err:
            return False, str(err)
        self.log("Pedido recibido desde MovilDeep.")
        self.send(text, keep_draft=True, uploads=uploads)  # keep_draft: no toca lo que la persona tenga escrito en la PC
        return True, ""

    def remote_sessions(self):
        cur = self.sess
        out = [{"id": cur.id, "title": cur.display_title, "updated": cur.updated, "current": True}]
        for s in self.store.list():
            if s.id != cur.id:
                out.append({"id": s.id, "title": s.display_title, "updated": s.updated, "current": False})
        return out

    def remote_switch_session(self, sid):
        if self.busy:
            return False, BUSY_SESSION_NOTE
        if sid == self.sess.id:
            return True, ""
        self.switch_session(sid)
        if self.sess.id != sid:
            return False, "No se pudo abrir esa sesión (el archivo falta o estaba dañado)."
        return True, ""

    def remote_new_session(self):
        if self.busy:
            return False, BUSY_SESSION_NOTE
        if not self.sess.messages:
            return True, "La sesión abierta ya está vacía: se sigue en esa."
        self.new_session()
        return True, ""

    def remote_model_list(self):
        entries = self._all_entries()
        cur = self._entry_for(self.sess.model) if self.sess.model else None
        if cur and cur["id"] not in [e["id"] for e in entries]:
            entries.insert(0, cur)
        return [{"id": e["id"], "name": self._display(e), "kind": e["kind"], "current": bool(cur and e["id"] == cur["id"])}
                for e in entries]

    def remote_set_model(self, mid):
        if self.busy:
            return False, BUSY_MODEL_NOTE
        names = {m["id"]: m["name"] for m in self.remote_model_list()}
        if mid not in names:
            return False, "Ese modelo ya no está en la lista de la PC."
        self._refresh_model_widgets()
        self.model_var.set(names[mid])
        self.on_model_change()                   # la misma lógica que el selector de la PC
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

    def remote_power(self, action):
        if action not in ("shutdown", "suspend"):
            return False, "Acción desconocida: se puede apagar («shutdown») o suspender («suspend»)."
        if self.busy:
            return False, BUSY_POWER_NOTE
        self.log(f"MovilDeep pidió {'suspender' if action == 'suspend' else 'apagar'} la PC.", "WARN")
        if action == "suspend":
            self.chat.note("MovilDeep pidió suspender la PC: se suspende en 3 segundos.")
            self.root.after(SUSPEND_DELAY_MS, self._suspend_now)
            return True, ""
        ok, msg = self.power_fn("shutdown")
        if not ok:
            return False, msg
        self.chat.note(f"MovilDeep pidió apagar la PC: se apaga en {SHUTDOWN_DELAY} segundos.")
        self._shutdown_countdown()
        return True, ""

    def _suspend_now(self):
        ok, msg = self.power_fn("suspend")
        if not ok:
            self.chat.note(msg, error=True)

    def _shutdown_countdown(self):
        """Ventana siempre encima con la cuenta regresiva y «Cancelar apagado» (shutdown /a)."""
        if self._shutdown_win is not None:
            self._shutdown_win.destroy()
        w = self._shutdown_win = tk.Toplevel(self.root)
        w.title(TITULO)
        w.attributes("-topmost", True)
        w.resizable(False, False)
        lbl = ttk.Label(w, padding=(20, 16), font=("Segoe UI", 12))
        lbl.pack()
        left = [SHUTDOWN_DELAY]

        def close():
            if self._shutdown_win is w:
                self._shutdown_win = None
            w.destroy()

        def cancel():
            ok, msg = self.power_fn("cancel")
            self.chat.note("Apagado cancelado." if ok else msg, error=not ok)
            close()

        def tick():
            if self._shutdown_win is not w:
                return
            lbl.config(text=f"MovilDeep pidió apagar la PC.\nSe apaga en {left[0]} s.")
            if left[0] <= 0:
                return
            left[0] -= 1
            w.after(1000, tick)
        ttk.Button(w, text="Cancelar apagado", command=cancel).pack(pady=(0, 16))
        w.protocol("WM_DELETE_WINDOW", cancel)     # cerrar la ventana también cancela: nada se apaga sin verlo
        tick()

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
            self.log(f"Acceso remoto: no se pudo abrir el puerto {self.cfg['remote_port']}: {e}", "ERROR")
            return False, f"No se pudo abrir el puerto {self.cfg['remote_port']}: {e}"
        self.remote_srv = srv
        self.log(f"Acceso remoto (MovilDeep) activo en el puerto {self.cfg['remote_port']}.", "OK")
        return True, ""

    def stop_remote(self):
        if self.remote_srv is not None:
            self.remote_srv.stop()
            self.remote_srv = None
            self.log("Acceso remoto apagado.")

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
            self.btn_continue.pack(side="right", anchor="s", padx=(8, 0), before=self.send_col)
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

    def send(self, text=None, keep_draft=False, uploads=()):
        """Manda el texto del campo de entrada (o el indicado, para tests) junto con los adjuntos.
        Con keep_draft=True no toca lo que haya en el campo ni los adjuntos (así funciona «Continuar»).
        uploads: adjuntos subidos desde el celular (lo que arma remote.prepare_upload)."""
        if self.busy:
            return
        if text is None:
            text = self.input.get("1.0", "end-1c")
        text = text.strip()
        if not text and not self.attachments and not uploads:
            return
        s = self.sess
        entry = self._entry_for(s.model)
        if entry["kind"] == "remote" and not self.cfg.key(self._prov(entry)):
            self._key_note()
            if self.interactive:
                if self.cfg.needs_unlock_of(self._prov(entry)):
                    dialogs.UnlockDialog(self)
                else:
                    self.open_settings()
            return
        files = [] if keep_draft else list(self.attachments)
        full_text, extra = self._with_uploads(text, uploads)
        try:
            content = dsapi.build_message(full_text, files, extra)
        except (dsapi.AttachError, OSError) as e:
            self.warn(str(e))
            return
        n0 = len(s.messages)
        s.messages.append({"role": "user", "content": content})
        self.chat.user(full_text, files + [n for n, _ in extra])
        self._last_notice = ""
        if not keep_draft:
            self.input.delete("1.0", "end")
            self.attachments = []
            self._redraw_attachments()
        self._show_continue(False)
        self.cancel_ev = threading.Event()
        self._run_t0 = time.time()
        extra_txt = f" · esfuerzo {s.effort}" if s.effort else ""
        n_adj = len(files) + len(extra)
        self.log(f"Enviando a {self._display(entry)}{extra_txt} · permisos: {APPROVALS[s.approval]}"
                 + (f" · {n_adj} adjunto(s)" if n_adj else ""))
        self._set_busy(True)
        self.worker = threading.Thread(target=self._work, args=(s, entry, self.toolbox, n0, text, files, self.cancel_ev), daemon=True)
        self.worker.start()

    def _set_busy(self, busy):
        self.busy = busy
        self.btn_send.configure(text="Detener" if busy else "Enviar")
        if self.img_send and self.img_stop:
            self.btn_send.configure(image=self.img_stop if busy else self.img_send)
        # scroll automático al final mientras el agente trabaja; se libera cuando termina su respuesta
        self.chat.follow = busy
        if busy:
            self.chat.text.see("end")
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
        effort = s.effort if s.effort in (entry["efforts"] or []) else None   # el de un modelo local no viaja a la API
        return providers.make_stream(entry, self.cfg.key(self._prov(entry)), msgs, effort, self.cfg["max_tokens"], tools=tools)

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
                hit = u.get("prompt_cache_hit_tokens")
                if hit is None:      # OpenAI lo informa en otro lado; DeepSeek puede mandar los dos: no se suman
                    hit = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                s.totals["hit"] += hit
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
                t0 = self._loading = time.time()
                self.post(lambda: self._loading_tick(entry["name"], t0))
                try:
                    base = self._local_server().ensure(entry["path"], int(self.cfg["local_ctx"]), cancel,
                                                       reasoning=localmodels.reasoning_for(entry["path"], s.effort))
                finally:
                    self._loading = None
                self.log(f"Modelo local {entry['name']} listo ({time.time() - t0:.0f} s).", "OK", overwrite=True, protect=True)
            agent = ag.Agent(lambda msgs, tools: self._make_stream(entry, s, base, msgs, tools, toolbox), toolbox,
                             max_steps=self.max_steps, label=self._label_of(entry), **self._agent_budget(entry, toolbox),
                             keep=("_blocks",) if entry["kind"] == "remote" and self._prov(entry) == "anthropic" else ())
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

    def _loading_tick(self, name, t0):
        """Spinner en el log mientras llama-server carga el modelo (no informa porcentaje: se muestran los segundos)."""
        if self._loading is not t0:
            return
        self.log(f"Cargando el modelo local {name}… {time.time() - t0:3.0f} s  {self.logp.spin()}", "SPINNER", overwrite=True)
        self.root.after(250, lambda: self._loading_tick(name, t0))

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
            self._step = {"n": a[0] if a else self._step["n"] + 1, "chars": 0, "t": 0.0, "open": True, "usage": None}
            self.log(f"Paso {self._step['n']} · esperando al modelo…  {self.logp.spin()}", "SPINNER", overwrite=True)
        elif kind in ("reasoning", "content"):
            c.stream_piece(kind, a[0])
            if kind == "content":
                self._live["content"] = (self._live["content"] + a[0])[-6000:]
            else:
                self._live["rlen"] += len(a[0])
            self.meter.piece(a[0])
            self._set_state("Razonando…" if kind == "reasoning" else "Escribiendo…")
            st = self._step
            st["chars"] += len(a[0])
            now = time.monotonic()
            if now - st["t"] >= 0.2:          # el texto llega de a pedacitos: la línea viva se redibuja 5 veces por segundo
                st["t"] = now
                self.log(f"Paso {st['n']} · {'razonando' if kind == 'reasoning' else 'escribiendo'}  {self.logp.spin()}  "
                         f"{miles(st['chars'])} caracteres", "SPINNER", overwrite=True)
        elif kind == "usage":
            self.last_usage = a[0]
            self.meter.usage(a[0])
            self._update_status_keep_busy()
            if self._step.get("open"):
                self._step["usage"] = a[0]      # se escribe al cerrar el paso, debajo de su resumen
            else:
                self._log_usage(a[0])
        elif kind == "step_end":
            p = a[0]
            self.meter.step_end()
            self._live = {"content": "", "rlen": 0}
            c.clear_stream()
            note = "⚠ La respuesta se cortó por el límite de tokens de salida (ajustable en Configuración)." if p.get("finish") == "length" else None
            c.assistant_body(p.get("reasoning", ""), p.get("content", ""), note, final=not p.get("calls"))
            st = self._step
            st["open"] = False
            calls = len(p.get("calls") or [])
            self.log(f"Paso {st['n']} terminado · {miles(st['chars'])} caracteres"
                     + (f" · pide {calls} herramienta(s)" if calls else ""), "OK", overwrite=True, protect=True)
            if st.get("usage"):
                self._log_usage(st["usage"])
            if note:
                self.log("La respuesta se cortó por el límite de tokens de salida.", "WARN")
        elif kind == "tool_start":
            c.tool_call(a[1], a[2])
            self._set_state(f"Ejecutando {a[1]}…")
            args = a[2] if isinstance(a[2], str) else json.dumps(a[2], ensure_ascii=False)
            self.log(f"→ {a[1]} {self._short(args)}")
        elif kind == "tool_result":
            c.tool_result(a[2])
            if a[1] in MUTATING:
                self.explorer.refresh()
            self._set_state("Pensando…")
            res = str(a[2])
            self.log(f"← {a[1]}: {self._short(res)}", "ERROR" if res.startswith("ERROR") else "OK")
        elif kind == "notice":
            c.note("• " + a[0])
            self._last_notice = a[0]
            self.log(a[0], "WARN")

    @staticmethod
    def _short(txt, n=160):
        line = " ".join(str(txt).split())
        return line if len(line) <= n else line[:n - 1] + "…"

    def _log_usage(self, u):
        """Tokens del paso y cuánto del contexto del modelo ocupa (barra como las de PostOCRNormalizer)."""
        pin, pout = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        self.log(f"Tokens: {miles(pin)} entrada · {miles(pout)} salida")
        ctx = self._entry_for(self.sess.model).get("context") or 0
        if ctx and pin:
            self.log(logpanel.progreso("Contexto", pin, ctx), "WARN" if pin / ctx >= 0.8 else "SPINNER")

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
        self._step["open"] = False
        dur = f"{time.time() - self._run_t0:.1f} s" if self._run_t0 else ""
        if error:     # reemplaza la línea viva (spinner) si quedó una: el error es lo que importa
            self.log(f"✖ {self._short(error, 300)}", "ERROR", overwrite=True, protect=True)
        elif outcome == "cancelled":
            self.log(f"■ Interrumpido ({dur}).", "WARN", overwrite=True, protect=True)
        elif outcome == "done":
            self.log(f"Terminado en {dur}.", "OK", protect=True)
        else:
            self.log(f"Detenido ({outcome}) tras {dur}.", "WARN", protect=True)
        self._record_run(outcome, error)
        self._set_busy(False)
        # también si «Continuar» mismo falló (red caída): el botón vuelve a estar para reintentar
        self._show_continue((outcome == "max_steps" and not error) or bool(restore and restore[0] == CONTINUE_TEXT))
        self.refresh_sessions()
        self.explorer.refresh()
        self._retitle()
        e = self._entry_for(self.sess.model)
        if e["kind"] == "remote" and self._prov(e) == "deepseek":
            self.refresh_balance()

    def _record_run(self, outcome, error):
        if error:
            outcome, msg = "error", error
        elif outcome == "done":
            last = next((m for m in reversed(self.sess.messages) if m.get("role") == "assistant"), {})
            msg = str(last.get("content") or "").strip()
        elif outcome == "cancelled":
            msg = "Interrumpido."
        else:                                   # max_steps / loop: el aviso que dio el agente
            msg = self._last_notice
        self._run_seq += 1
        self._run_outcome = outcome or "error"
        self._run_msg = msg[:300]

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
        self.log("Deshacer: " + msg)
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
            self.logp.save_state()
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
        for job in ("_meter_job", "_drain_job"):
            try:
                self.root.after_cancel(getattr(self, job))
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
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodeAgent")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
