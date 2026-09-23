"""DeepSeek Chat: ventana mínima para usar la API de DeepSeek sin línea de comandos."""
import datetime
import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dsapi

TITULO = "DeepSeek Chat"
SIN_EFFORT = "(por defecto)"

TEMAS = {
    "claro": dict(bg="#f2f4f7", panel="#ffffff", fg="#1c2733", muted="#66727f", user="#0b5cad",
                  bot="#17743f", code_bg="#eceff3", reason="#8a94a0", accent="#0b5cad",
                  accent_fg="#ffffff", err="#b3261e", border="#d3d9e0"),
    "oscuro": dict(bg="#1a1e23", panel="#232930", fg="#e4e8ec", muted="#93a0ad", user="#6cb2ff",
                   bot="#6fd39a", code_bg="#1a1e23", reason="#7d8895", accent="#3a86d6",
                   accent_fg="#ffffff", err="#ff8a80", border="#37404a"),
}

FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")


def miles(n):
    return f"{int(n):,}".replace(",", ".")


class App:
    def __init__(self, root, cfg=None):
        self.root = root
        self.cfg = cfg or dsapi.Config()
        self.messages = []          # historial que se manda a la API (sin prompt de sistema)
        self.attachments = []       # rutas adjuntas al próximo mensaje
        self.models = list(dsapi.FALLBACK_MODELS)
        self.busy = False
        self.stream = None
        self.q = None
        self.cur = None
        self.totals = {"in": 0, "out": 0, "reason": 0, "hit": 0}
        self.last_usage = None
        self.balance_text = "—"
        self.transcript_path = None
        self._rid = 0
        self._ui = queue.Queue()    # tkinter no es seguro entre hilos: los hilos dejan acá lo que quieren mostrar

        root.title(TITULO)
        root.geometry("960x720")
        root.minsize(640, 480)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()
        self.apply_theme()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_ui()

        if self.cfg.load_warning:
            self._system_note(self.cfg.load_warning, error=True)
        if self.cfg.api_key:
            self.refresh_models()
            self.refresh_balance()
        else:
            self._nokey_note("Falta la API key. Abrí ⚙ Configuración para cargarla.")
            root.after(300, self.open_settings)

    # ------------------------------------------------------------ construcción

    def _build(self):
        r = self.root
        self.top = ttk.Frame(r, padding=(10, 8))
        self.top.pack(fill="x")
        ttk.Label(self.top, text="Modelo").pack(side="left")
        self.model_var = tk.StringVar(value=self.cfg["model"])
        self.model_cb = ttk.Combobox(self.top, textvariable=self.model_var, state="readonly", width=22)
        self.model_cb.pack(side="left", padx=(4, 12))
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self.on_model_change())
        ttk.Label(self.top, text="Effort").pack(side="left")
        self.effort_var = tk.StringVar(value=self.cfg["effort"] or SIN_EFFORT)
        self.effort_cb = ttk.Combobox(self.top, textvariable=self.effort_var, state="readonly", width=12)
        self.effort_cb.pack(side="left", padx=(4, 12))
        self.effort_cb.bind("<<ComboboxSelected>>", lambda e: self.on_effort_change())
        self.btn_cfg = ttk.Button(self.top, text="⚙ Configuración", command=self.open_settings)
        self.btn_cfg.pack(side="right")
        ttk.Button(self.top, text="Copiar respuesta", command=self.copy_last).pack(side="right", padx=6)
        ttk.Button(self.top, text="Exportar", command=self.export).pack(side="right")
        ttk.Button(self.top, text="Nuevo chat", command=self.new_chat).pack(side="right", padx=6)
        self._refresh_model_widgets()

        self.mid = ttk.Frame(r)
        self.chat = tk.Text(self.mid, wrap="word", state="disabled", relief="flat", padx=12, pady=10, height=8,
                            borderwidth=0, highlightthickness=1, cursor="arrow", spacing1=2, spacing3=2)
        sb = ttk.Scrollbar(self.mid, command=self.chat.yview)
        self.chat.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)

        self.attach_bar = ttk.Frame(r, padding=(10, 4, 10, 0))

        self.bottom = ttk.Frame(r, padding=(10, 6, 10, 4))
        self.btn_plus = ttk.Button(self.bottom, text="+", width=3, command=self.add_files)
        self.btn_plus.pack(side="left", anchor="s", padx=(0, 8))
        self.input = tk.Text(self.bottom, height=4, wrap="word", relief="flat", padx=8, pady=6,
                             highlightthickness=1, undo=True)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)
        self.btn_send = ttk.Button(self.bottom, text="Enviar", width=9, command=self.on_send_click)
        self.btn_send.pack(side="left", anchor="s", padx=(8, 0))

        self.status = ttk.Frame(r, padding=(10, 0, 10, 8))
        self.tokens_lbl = ttk.Label(self.status, text="")
        self.tokens_lbl.pack(side="left")
        self.balance_btn = ttk.Button(self.status, text="⟳", width=3, command=self.refresh_balance)
        self.balance_btn.pack(side="right")
        self.balance_lbl = ttk.Label(self.status, text="Saldo: —")
        self.balance_lbl.pack(side="right", padx=6)
        self.state_lbl = ttk.Label(self.status, text="Enter envía · Shift+Enter salto de línea")
        self.state_lbl.pack(side="right", padx=14)
        self._update_tokens_label()

        # Orden de empaquetado: primero lo que siempre debe verse (abajo), el chat al final con lo que sobre.
        # Si el chat se empaquetara antes, en una ventana chica empujaría fuera de pantalla el saldo y la entrada.
        self.status.pack(side="bottom", fill="x")
        self.bottom.pack(side="bottom", fill="x")
        self.attach_bar.pack(side="bottom", fill="x")
        self.mid.pack(fill="both", expand=True, padx=10)

    def apply_theme(self):
        t = TEMAS.get(self.cfg["theme"], TEMAS["claro"])
        self.t = t
        fs = int(self.cfg["font_size"])
        s = self.style
        self.root.configure(bg=t["bg"])
        s.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"], font=("Segoe UI", 10))
        s.configure("TFrame", background=t["bg"])
        s.configure("TLabel", background=t["bg"], foreground=t["fg"])
        s.configure("Err.TLabel", background=t["bg"], foreground=t["err"])
        s.configure("Ok.TLabel", background=t["bg"], foreground=t["bot"])
        s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
        s.configure("TButton", background=t["panel"], foreground=t["fg"], bordercolor=t["border"], padding=(10, 4))
        s.map("TButton", background=[("active", t["border"]), ("disabled", t["bg"])],
              foreground=[("disabled", t["muted"])])
        s.configure("Accent.TButton", background=t["accent"], foreground=t["accent_fg"])
        s.map("Accent.TButton", background=[("active", t["accent"]), ("disabled", t["border"])])
        s.configure("TCombobox", fieldbackground=t["panel"], background=t["panel"], foreground=t["fg"],
                    arrowcolor=t["fg"], bordercolor=t["border"], selectbackground=t["panel"], selectforeground=t["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", t["panel"])], foreground=[("readonly", t["fg"])])
        s.configure("TEntry", fieldbackground=t["panel"], foreground=t["fg"], insertcolor=t["fg"], bordercolor=t["border"])
        s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        s.configure("TSpinbox", fieldbackground=t["panel"], foreground=t["fg"], background=t["panel"])
        s.configure("Vertical.TScrollbar", background=t["panel"], troughcolor=t["bg"], bordercolor=t["bg"], arrowcolor=t["fg"])
        self.root.option_add("*TCombobox*Listbox.background", t["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.btn_send.configure(style="Accent.TButton")
        self.chat.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"],
                            highlightcolor=t["border"], font=("Segoe UI", fs), selectbackground=t["accent"], selectforeground=t["accent_fg"])
        self.input.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"],
                             highlightcolor=t["accent"], font=("Segoe UI", fs))
        c = self.chat
        c.tag_configure("hdr_user", foreground=t["user"], font=("Segoe UI", fs, "bold"), spacing1=10)
        c.tag_configure("hdr_bot", foreground=t["bot"], font=("Segoe UI", fs, "bold"), spacing1=10)
        c.tag_configure("note", foreground=t["muted"], font=("Segoe UI", fs - 1, "italic"))
        c.tag_configure("error", foreground=t["err"], font=("Segoe UI", fs, "bold"))
        c.tag_configure("reason", foreground=t["reason"], font=("Segoe UI", fs - 1, "italic"))
        c.tag_configure("rtoggle", foreground=t["reason"], font=("Segoe UI", fs - 1, "italic", "underline"))
        c.tag_configure("bold", font=("Segoe UI", fs, "bold"))
        c.tag_configure("inline", font=("Consolas", fs), background=t["code_bg"])
        c.tag_configure("code", font=("Consolas", fs), background=t["code_bg"], lmargin1=16, lmargin2=16,
                        rmargin=16, spacing1=0, spacing3=0)
        c.tag_configure("codelang", foreground=t["muted"], font=("Consolas", fs - 2), background=t["code_bg"], lmargin1=16)
        c.tag_raise("sel")

    # ------------------------------------------------------------ puente hilos -> ventana

    def post(self, fn):
        """Se puede llamar desde cualquier hilo; fn corre en el hilo de la ventana."""
        self._ui.put(fn)

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
                    pass  # el widget al que apuntaba ya no existe (ventana o diálogo cerrado)
        finally:
            try:
                self.root.after(40, self._drain_ui)  # siempre se reprograma, pase lo que pase con fn
            except tk.TclError:
                pass  # la ventana principal se cerró

    # ------------------------------------------------------------ modelos / effort

    def _model(self):
        return next((m for m in self.models if m["id"] == self.model_var.get()), None)

    def _refresh_model_widgets(self):
        ids = [m["id"] for m in self.models]
        cur = self.model_var.get()
        if cur not in ids:
            ids.insert(0, cur)  # el guardado en la config se conserva aunque el servidor ya no lo liste
        self.model_cb.configure(values=ids)
        m = self._model()
        efforts = (m["efforts"] if m else []) or []
        self.effort_cb.configure(values=[SIN_EFFORT] + efforts)
        if self.effort_var.get() not in [SIN_EFFORT] + efforts:
            self.effort_var.set(SIN_EFFORT)

    def on_model_change(self):
        self.cfg["model"] = self.model_var.get()
        self._refresh_model_widgets()
        self.cfg["effort"] = "" if self.effort_var.get() == SIN_EFFORT else self.effort_var.get()
        self.cfg.save()

    def on_effort_change(self):
        self.cfg["effort"] = "" if self.effort_var.get() == SIN_EFFORT else self.effort_var.get()
        self.cfg.save()

    def current_effort(self):
        v = self.effort_var.get()
        return None if v == SIN_EFFORT else v

    def refresh_models(self):
        key = self.cfg.api_key
        if not key:
            return

        def work():
            try:
                res = dsapi.list_models(key)
            except dsapi.ApiError as e:
                self.post(lambda: self._set_state(f"No se pudo listar modelos: {e}", error=True))
                return
            self.post(lambda: self._models_loaded(res))
        threading.Thread(target=work, daemon=True).start()

    def _models_loaded(self, res):
        self.models = res
        self._refresh_model_widgets()

    # ------------------------------------------------------------ saldo y tokens

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
        self.balance_lbl.configure(text=f"Saldo: {txt}")

    def _add_usage(self, u):
        self.last_usage = u
        self.totals["in"] += u.get("prompt_tokens", 0)
        self.totals["out"] += u.get("completion_tokens", 0)
        self.totals["reason"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        self.totals["hit"] += u.get("prompt_cache_hit_tokens", 0)
        self._update_tokens_label()

    def _update_tokens_label(self):
        t = self.totals
        txt = f"Sesión: {miles(t['in'])} entrada · {miles(t['out'])} salida"
        if t["reason"]:
            txt += f" ({miles(t['reason'])} de razonamiento)"
        if self.last_usage:
            m = self._model()
            ctx = m["context"] if m else 0
            usado = self.last_usage.get("prompt_tokens", 0)
            txt += f"  │  Contexto: {miles(usado)}" + (f" de {miles(ctx)}" if ctx else "")
        self.tokens_lbl.configure(text=txt)

    def _set_state(self, txt, error=False):
        # por estilo y no por color fijo: así sigue al tema si se cambia después
        self.state_lbl.configure(text=txt, style="Err.TLabel" if error else "TLabel")

    # ------------------------------------------------------------ adjuntos

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Adjuntar archivos al chat")
        self.attach_paths(paths)

    def attach_paths(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p in self.attachments:
                continue
            try:
                dsapi.read_text_file(p)
            except (dsapi.AttachError, OSError) as e:
                messagebox.showwarning(TITULO, str(e), parent=self.root)
                continue
            self.attachments.append(p)
        self._redraw_attachments()

    def _redraw_attachments(self):
        for w in self.attach_bar.winfo_children():
            w.destroy()
        for p in self.attachments:
            chip = ttk.Frame(self.attach_bar, style="TFrame")
            chip.pack(side="left", padx=(0, 6))
            kb = max(1, os.path.getsize(p) // 1024) if os.path.exists(p) else 0
            ttk.Label(chip, text=f"📎 {os.path.basename(p)} ({kb} KB)").pack(side="left")
            ttk.Button(chip, text="✕", width=2, command=lambda q=p: self.remove_attachment(q)).pack(side="left", padx=(2, 0))

    def remove_attachment(self, p):
        if p in self.attachments:
            self.attachments.remove(p)
        self._redraw_attachments()

    # ------------------------------------------------------------ render del chat

    def _at_bottom(self):
        return self.chat.yview()[1] > 0.97

    def _put(self, text, *tags):
        c = self.chat
        stick = self._at_bottom()
        c.configure(state="normal")
        c.insert("end", text, tags)
        c.configure(state="disabled")
        if stick:
            c.see("end")

    def _system_note(self, text, error=False):
        self._put(text + "\n", "error" if error else "note")

    def _nokey_note(self, text, error=False):
        # marcada con su propio tag para poder retirarla cuando la key se guarda (deja de ser verdad)
        self._put(text + "\n", "error" if error else "note", "nokey")

    def _clear_nokey_notes(self):
        c = self.chat
        c.configure(state="normal")
        rng = c.tag_ranges("nokey")
        for i in range(len(rng) - 2, -1, -2):  # de atrás hacia adelante para no correr los índices
            c.delete(rng[i], rng[i + 1])
        c.configure(state="disabled")

    def _inline(self, text, base=()):
        for tok in INLINE.split(text):
            if not tok:
                continue
            if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                self._put(tok[2:-2], "bold", *base)
            elif tok.startswith("`") and tok.endswith("`") and len(tok) > 2:
                self._put(tok[1:-1], "inline", *base)
            else:
                self._put(tok, *base)

    def _render_markdown(self, text):
        pos = 0
        for m in FENCE.finditer(text):
            if m.start() > pos:
                self._inline(text[pos:m.start()])
            lang, code = m.group(1).strip(), m.group(2).rstrip("\n")
            c = self.chat
            c.configure(state="normal")
            btn = tk.Button(c, text="Copiar código", command=lambda code=code: self._copy(code), relief="flat",
                            bg=self.t["border"], fg=self.t["fg"], activebackground=self.t["accent"],
                            activeforeground=self.t["accent_fg"], font=("Segoe UI", 8), cursor="hand2", padx=6, pady=0)
            c.insert("end", "\n")
            c.window_create("end", window=btn)
            c.insert("end", f"  {lang}\n" if lang else "\n", "codelang")
            c.configure(state="disabled")
            self._put(code + "\n", "code")
            pos = m.end()
        if pos < len(text):
            self._inline(text[pos:])

    def _render_user(self, text, files):
        self._put("\nVos\n", "hdr_user")
        if text:
            self._put(text + "\n")
        for p in files:
            self._put(f"📎 {os.path.basename(p)}\n", "note")

    # ------------------------------------------------------------ envío

    def _on_enter(self, e):
        self.on_send_click()
        return "break"

    def on_send_click(self):
        if self.busy:
            self.cancel()
        else:
            self.send()

    def send(self, text=None):
        if self.busy:
            return
        if text is None:
            text = self.input.get("1.0", "end-1c")
        text = text.strip()
        if not text and not self.attachments:
            return
        key = self.cfg.api_key
        if not key:
            self._nokey_note("Falta la API key. Abrí ⚙ Configuración.", error=True)
            self.open_settings()
            return
        files = list(self.attachments)
        try:
            content = dsapi.build_message(text, files)
        except (dsapi.AttachError, OSError) as e:
            messagebox.showerror(TITULO, str(e), parent=self.root)
            return
        self._pending_restore = (text, files)
        self.messages.append({"role": "user", "content": content})
        self._render_user(text, files)
        self.input.delete("1.0", "end")
        self.attachments = []
        self._redraw_attachments()

        api_msgs = list(self.messages)
        sp = self.cfg["system_prompt"].strip()
        if sp:
            api_msgs.insert(0, {"role": "system", "content": sp})
        self._rid += 1
        self.q = queue.Queue()
        self.stream = dsapi.ChatStream(key, self.model_var.get(), api_msgs, self.current_effort(), self.cfg["max_tokens"])
        self.cur = {"content": "", "reasoning": "", "finish": None, "rid": self._rid, "started": False}
        self._set_busy(True)
        self._put("\nDeepSeek\n", "hdr_bot")
        c = self.chat
        c.mark_set("as", "end-1c")
        c.mark_gravity("as", "left")
        threading.Thread(target=self._worker, args=(self.stream, self.q), daemon=True).start()
        self._poll()

    def _worker(self, stream, q):
        try:
            for ev in stream:
                q.put(ev)
            q.put(("end", None))
        except Exception as e:
            if stream._cancelled:
                q.put(("end", None))  # cortar a pedido no es una falla, sea cual sea la excepción que deje el socket
            elif isinstance(e, dsapi.ApiError):
                q.put(("error", str(e)))
            else:
                q.put(("error", f"Error inesperado: {e!r}"))  # cualquier otra cosa se muestra, no se traga

    def _poll(self):
        if self.q is None:
            return
        done = False
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind in ("content", "reasoning"):
                    self._stream_piece(kind, val)
                elif kind == "usage":
                    self._add_usage(val)
                elif kind == "finish":
                    self.cur["finish"] = val
                elif kind == "end":
                    self._finish(cancelled=self.stream is not None and self.stream._cancelled)
                    done = True
                    break
                elif kind == "error":
                    self._fail(val)
                    done = True
                    break
        except queue.Empty:
            pass
        if not done:
            self.root.after(30, self._poll)

    def _stream_piece(self, kind, val):
        self.cur[kind] += val
        self._set_state("Razonando…" if kind == "reasoning" else "Escribiendo…")
        self._put(val, "reason" if kind == "reasoning" else ())

    def _clear_stream_area(self):
        c = self.chat
        c.configure(state="normal")
        # hasta end-1c y no end: si el rango empieza al inicio de línea, tkinter borra también el
        # salto de línea anterior y el encabezado "DeepSeek" quedaría pegado al texto que sigue.
        c.delete("as", "end-1c")
        c.configure(state="disabled")

    def _finish(self, cancelled):
        cur = self.cur
        self._clear_stream_area()
        # se re-dibuja completo, ahora con formato de código y razonamiento plegado
        note = None
        if cur["finish"] == "length":
            note = "⚠ La respuesta se cortó por el límite de tokens de salida (ajustable en Configuración)."
        if cancelled:
            note = "■ Interrumpido."
        if cur["content"] or cur["reasoning"]:
            self._render_final_body(cur, note)
        if cur["content"]:
            self.messages.append({"role": "assistant", "content": cur["content"]})
            self._autosave()
        else:
            # sin respuesta: se saca el mensaje del usuario para poder reintentar sin duplicarlo
            self._undo_last_user()
        self._end_request()

    def _reasoning_block(self, reasoning, rid):
        tg, bd = f"rt{rid}", f"rb{rid}"
        self._put(f"▸ Razonamiento ({miles(len(reasoning))} caracteres) — clic para mostrar u ocultar\n", "rtoggle", tg)
        self._put(reasoning.strip() + "\n\n", "reason", bd)
        c = self.chat
        c.tag_configure(bd, elide=True)
        c.tag_bind(tg, "<Button-1>", lambda e: c.tag_configure(bd, elide=not bool(int(c.tag_cget(bd, "elide") or 0))))
        c.tag_bind(tg, "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind(tg, "<Leave>", lambda e: c.configure(cursor="arrow"))

    def _render_final_body(self, cur, note):
        # el encabezado "DeepSeek" ya está impreso antes de la marca "as"
        if cur["reasoning"]:
            self._reasoning_block(cur["reasoning"], cur["rid"])
        self._render_markdown(cur["content"])
        self._put("\n")
        if note:
            self._put(note + "\n", "note")

    def _fail(self, msg):
        self._clear_stream_area()
        self._put(f"✖ {msg}\n", "error")
        self._undo_last_user()
        self._end_request()

    def _undo_last_user(self):
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        text, files = getattr(self, "_pending_restore", ("", []))
        if text and not self.input.get("1.0", "end-1c").strip():
            self.input.insert("1.0", text)
        for p in files:
            if p not in self.attachments:
                self.attachments.append(p)
        self._redraw_attachments()

    def _end_request(self):
        self.q = None
        self.stream = None
        self._set_busy(False)
        self._set_state("Enter envía · Shift+Enter salto de línea")
        self.refresh_balance()

    def _set_busy(self, busy):
        self.busy = busy
        self.btn_send.configure(text="Detener" if busy else "Enviar")
        self.btn_plus.configure(state="disabled" if busy else "normal")
        self.model_cb.configure(state="disabled" if busy else "readonly")
        self.effort_cb.configure(state="disabled" if busy else "readonly")
        if busy:
            self._set_state("Enviando…")

    def cancel(self):
        if self.stream is not None:
            self.stream.cancel()
            # el hilo emitirá "end" al soltar el socket; si estaba bloqueado en la lectura, lo forzamos
            q = self.q
            if q is not None:
                q.put(("end", None))

    # ------------------------------------------------------------ acciones de la barra

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_state("Copiado al portapapeles")

    def copy_last(self):
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                self._copy(m["content"])
                return
        self._set_state("Todavía no hay respuestas para copiar")

    def new_chat(self):
        if self.busy:
            self.cancel()
        self.messages = []
        self.transcript_path = None
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self._set_state("Chat nuevo. La conversación anterior quedó en el historial.")

    def _transcript(self):
        lines = [f"# Conversación — {datetime.datetime.now():%Y-%m-%d %H:%M}\n", f"Modelo: {self.model_var.get()}\n"]
        for m in self.messages:
            lines.append(f"\n## {'Vos' if m['role'] == 'user' else 'DeepSeek'}\n\n{m['content']}\n")
        return "\n".join(lines)

    def _autosave(self):
        """Cada respuesta completa queda en disco: 'Nuevo chat' o cerrar la ventana no pierden nada."""
        try:
            os.makedirs(self.cfg.history_dir, exist_ok=True)
            if not self.transcript_path:
                self.transcript_path = os.path.join(self.cfg.history_dir, f"{datetime.datetime.now():%Y%m%d-%H%M%S}.md")
            with open(self.transcript_path, "w", encoding="utf-8") as f:
                f.write(self._transcript())
        except OSError as e:
            self._set_state(f"No se pudo guardar el historial: {e}", error=True)

    def export(self):
        if not self.messages:
            self._set_state("No hay conversación para exportar")
            return
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".md", initialfile="conversacion.md",
                                         filetypes=[("Markdown", "*.md"), ("Texto", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self._transcript())
            self._set_state(f"Exportado a {p}")

    def on_close(self):
        if self.stream is not None:
            self.stream.cancel()
        self.root.destroy()

    # ------------------------------------------------------------ configuración

    def open_settings(self):
        SettingsDialog(self)

    def key_changed(self):
        self._clear_nokey_notes()
        self.refresh_models()
        self.refresh_balance()


class SettingsDialog:
    def __init__(self, app):
        self.app = app
        t = app.t
        self.win = tk.Toplevel(app.root)
        w = self.win
        w.title("Configuración")
        w.configure(bg=t["bg"])
        w.transient(app.root)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="API key de DeepSeek", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        self.key_state = ttk.Label(f, text="")
        self.key_state.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(f, textvariable=self.key_var, show="•", width=52)
        self.key_entry.grid(row=2, column=0, columnspan=3, sticky="we")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Mostrar", variable=self.show_var, command=self._toggle_show).grid(row=3, column=0, sticky="w", pady=6)
        self.btn_test = ttk.Button(f, text="Probar y guardar", style="Accent.TButton", command=self.test_and_save)
        self.btn_test.grid(row=3, column=1, sticky="e", pady=6, padx=6)
        ttk.Button(f, text="Borrar key", command=self.clear_key).grid(row=3, column=2, sticky="e", pady=6)
        self.key_msg = ttk.Label(f, text="", wraplength=430, justify="left")
        self.key_msg.grid(row=4, column=0, columnspan=3, sticky="w")

        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="we", pady=12)

        ttk.Label(f, text="Instrucciones de sistema (se envían con cada pedido)", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=3, sticky="w")
        self.sys_text = tk.Text(f, height=5, width=56, wrap="word", relief="flat", padx=6, pady=4, highlightthickness=1,
                                bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"], font=("Segoe UI", 10))
        self.sys_text.grid(row=7, column=0, columnspan=3, sticky="we", pady=(4, 10))
        self.sys_text.insert("1.0", app.cfg["system_prompt"])

        opts = ttk.Frame(f)
        opts.grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(opts, text="Máx. tokens de salida").grid(row=0, column=0, sticky="w")
        self.max_var = tk.IntVar(value=int(app.cfg["max_tokens"]))
        ttk.Spinbox(opts, from_=1024, to=393216, increment=1024, textvariable=self.max_var, width=9).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(opts, text="Tema").grid(row=0, column=2)
        self.theme_var = tk.StringVar(value=app.cfg["theme"])
        ttk.Combobox(opts, textvariable=self.theme_var, values=list(TEMAS), state="readonly", width=8).grid(row=0, column=3, padx=(6, 18))
        ttk.Label(opts, text="Tamaño de letra").grid(row=0, column=4)
        self.font_var = tk.IntVar(value=int(app.cfg["font_size"]))
        ttk.Spinbox(opts, from_=8, to=22, textvariable=self.font_var, width=4).grid(row=0, column=5, padx=6)

        ttk.Label(f, text=f"Historial de conversaciones: {app.cfg.history_dir}", style="Muted.TLabel", wraplength=460).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))
        bf = ttk.Frame(f)
        bf.grid(row=10, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Abrir carpeta del historial", command=self.open_history).pack(side="left", padx=6)
        ttk.Button(bf, text="Guardar opciones", style="Accent.TButton", command=self.save_options).pack(side="left", padx=6)
        ttk.Button(bf, text="Cerrar", command=w.destroy).pack(side="left")

        self._refresh_key_state()
        w.update_idletasks()
        w.geometry(f"+{app.root.winfo_rootx() + 80}+{app.root.winfo_rooty() + 60}")
        w.grab_set()
        self.key_entry.focus_set()

    def _toggle_show(self):
        self.key_entry.configure(show="" if self.show_var.get() else "•")

    def _refresh_key_state(self):
        key = self.app.cfg.api_key
        if key:
            self.key_state.configure(text=f"Guardada: {dsapi.Config.mask(key)} (cifrada con tu usuario de Windows)", style="TLabel")
        elif self.app.cfg.has_key():
            self.key_state.configure(text="Hay una key guardada pero no se pudo descifrar; cargá una nueva.", style="Err.TLabel")
        else:
            self.key_state.configure(text="No hay key guardada.", style="TLabel")

    def _msg(self, text, error=False):
        self.key_msg.configure(text=text, style="Err.TLabel" if error else "Ok.TLabel")

    def restyle(self):
        """Los widgets de tk puro (no ttk) no siguen el estilo: hay que repintarlos si cambia el tema."""
        t = self.app.t
        self.win.configure(bg=t["bg"])
        self.sys_text.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"])

    def test_and_save(self):
        key = self.key_var.get().strip()
        if not key:
            self._msg("Pegá una key primero.", error=True)
            return
        self.btn_test.configure(state="disabled")
        self._msg("Probando contra DeepSeek…")

        def work():
            try:
                b = dsapi.get_balance(key)
                res = (b, None)
            except dsapi.ApiError as e:
                res = (None, e)
            self.app.post(lambda: self._test_done(key, *res))  # si el diálogo se cerró, _drain_ui ignora el TclError
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, key, balance, err):
        self.btn_test.configure(state="normal")
        if err is not None:
            self._msg(f"No se guardó. {err}", error=True)
            return
        if not balance["available"]:
            self._msg("La key es válida pero DeepSeek informa la cuenta como no disponible. No se guardó.", error=True)
            return
        try:
            self.app.cfg.set_api_key(key)
        except OSError as e:
            self._msg(f"No se pudo guardar: {e}", error=True)
            return
        self.key_var.set("")
        self._refresh_key_state()
        self._msg(f"Key válida y guardada. Saldo: {balance['text']}")
        self.app.key_changed()

    def clear_key(self):
        if messagebox.askyesno(TITULO, "¿Borrar la API key guardada?", parent=self.win):
            self.app.cfg.set_api_key("")
            self._refresh_key_state()
            self._msg("Key borrada.")

    def save_options(self):
        c = self.app.cfg
        try:
            c["max_tokens"] = max(256, min(393216, int(self.max_var.get())))
            c["font_size"] = max(8, min(22, int(self.font_var.get())))
        except (tk.TclError, ValueError):
            self._msg("Tamaños inválidos.", error=True)
            return
        c["theme"] = self.theme_var.get()
        c["system_prompt"] = self.sys_text.get("1.0", "end-1c")
        c.save()
        self.app.apply_theme()
        self.restyle()
        self._msg("Opciones guardadas.")

    def open_history(self):
        os.makedirs(self.app.cfg.history_dir, exist_ok=True)
        os.startfile(self.app.cfg.history_dir)


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
