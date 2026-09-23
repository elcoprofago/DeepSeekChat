"""La transcripción del chat: un Text de tkinter con formato (markdown básico, código, razonamiento plegable,
llamadas a herramientas con su resultado plegable). Sirve igual para lo que llega en vivo y para recargar una sesión."""
import json
import os
import re
import tkinter as tk
from tkinter import ttk

FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")
ATTACH = re.compile(r"(?:^|\n\n)Archivo adjunto: ([^\n]*)\n")


def miles(n):
    return f"{int(n):,}".replace(",", ".")


def summarize_call(name, args):
    """Una línea legible para la llamada de una herramienta. args puede ser un dict o el texto crudo (JSON roto)."""
    if not isinstance(args, dict):
        return f"{name}  (argumentos ilegibles)"

    def cut(s, n=90):
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[:n - 1] + "…"
    if name == "run_command":
        return "$ " + cut(args.get("command", ""), 160)
    if name == "list_dir":
        return f"list_dir  {args.get('path', '.')}"
    if name == "read_file":
        extra = f"  (desde la línea {args['offset']})" if args.get("offset") not in (None, 1) else ""
        return f"read_file  {args.get('path', '')}{extra}"
    if name == "search":
        return f"search  “{cut(args.get('pattern', ''), 50)}” en {args.get('path', '.')}"
    if name == "edit_file":
        return f"edit_file  {args.get('path', '')}"
    if name == "write_file":
        return f"write_file  {args.get('path', '')}  ({len(str(args.get('content', '')))} caracteres)"
    return f"{name}  {cut(json.dumps(args, ensure_ascii=False), 100)}"


def result_is_problem(result):
    return result.startswith(("ERROR", "DENIED", "BLOCKED"))


def split_user_content(content):
    """(texto, [nombres de archivos adjuntos]) a partir del contenido que armó dsapi.build_message."""
    found = list(ATTACH.finditer(content))
    if not found:
        return content.strip(), []
    return content[:found[0].start()].strip(), [m.group(1).strip() for m in found]


class ChatView:
    def __init__(self, master, copy_cb):
        self.copy_cb = copy_cb
        self.frame = ttk.Frame(master)
        self.text = tk.Text(self.frame, wrap="word", state="disabled", relief="flat", padx=12, pady=10, height=8,
                            borderwidth=0, highlightthickness=1, cursor="arrow", spacing1=2, spacing3=2)
        self.sb = ttk.Scrollbar(self.frame, command=self.text.yview)
        self.text.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.t = None
        self._rid = 0
        self._last_role = None
        self.streaming = False

    # ------------------------------------------------------------ tema

    def apply_theme(self, t, fs):
        self.t = t
        c = self.text
        c.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], highlightbackground=t["border"],
                    highlightcolor=t["border"], font=("Segoe UI", fs), selectbackground=t["accent"], selectforeground=t["accent_fg"])
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
        c.tag_configure("tool", foreground=t.get("accent_text", t["accent"]), font=("Consolas", fs - 1), lmargin1=14, lmargin2=28, spacing1=4)
        c.tag_configure("toolres", foreground=t["muted"], font=("Consolas", fs - 1), lmargin1=28, lmargin2=28)
        c.tag_configure("toolbad", foreground=t["err"], font=("Consolas", fs - 1), lmargin1=28, lmargin2=28)
        c.tag_configure("tooltoggle", foreground=t["reason"], font=("Segoe UI", fs - 2, "italic", "underline"), lmargin1=28)
        c.tag_configure("toolbody", foreground=t["muted"], font=("Consolas", fs - 2), lmargin1=40, lmargin2=40)
        c.tag_raise("sel")

    # ------------------------------------------------------------ escritura básica

    def at_bottom(self):
        return self.text.yview()[1] > 0.97

    def put(self, text, *tags):
        c = self.text
        stick = self.at_bottom()
        c.configure(state="normal")
        c.insert("end", text, tags)
        c.configure(state="disabled")
        if stick:
            c.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._last_role = None
        self.streaming = False

    def get_text(self):
        return self.text.get("1.0", "end")

    def note(self, text, error=False, *extra_tags):
        self.put(text + "\n", "error" if error else "note", "aviso", *extra_tags)

    def clear_tagged(self, tag):
        """Borra lo marcado con un tag (de atrás hacia adelante para no correr los índices)."""
        c = self.text
        c.configure(state="normal")
        rng = c.tag_ranges(tag)
        for i in range(len(rng) - 2, -1, -2):
            c.delete(rng[i], rng[i + 1])
        c.configure(state="disabled")

    # ------------------------------------------------------------ markdown

    def _inline(self, text, base=()):
        for tok in INLINE.split(text):
            if not tok:
                continue
            if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                self.put(tok[2:-2], "bold", *base)
            elif tok.startswith("`") and tok.endswith("`") and len(tok) > 2:
                self.put(tok[1:-1], "inline", *base)
            else:
                self.put(tok, *base)

    def markdown(self, text):
        pos = 0
        for m in FENCE.finditer(text):
            if m.start() > pos:
                self._inline(text[pos:m.start()])
            lang, code = m.group(1).strip(), m.group(2).rstrip("\n")
            c = self.text
            c.configure(state="normal")
            btn = tk.Button(c, text="Copiar código", command=lambda code=code: self.copy_cb(code), relief="flat",
                            bg=self.t["border"], fg=self.t["fg"], activebackground=self.t["accent"],
                            activeforeground=self.t["accent_fg"], font=("Segoe UI", 8), cursor="hand2", padx=6, pady=0)
            c.insert("end", "\n")
            c.window_create("end", window=btn)
            c.insert("end", f"  {lang}\n" if lang else "\n", "codelang")
            c.configure(state="disabled")
            self.put(code + "\n", "code")
            pos = m.end()
        if pos < len(text):
            self._inline(text[pos:])

    # ------------------------------------------------------------ bloques plegables

    def _foldable(self, header, body, header_tags, body_tags, prefix):
        self._rid += 1
        tg, bd = f"{prefix}t{self._rid}", f"{prefix}b{self._rid}"
        c = self.text
        self.put(header + "\n", *header_tags, tg)
        self.put(body.rstrip() + "\n", *body_tags, bd)
        c.tag_configure(bd, elide=True)
        c.tag_bind(tg, "<Button-1>", lambda e: c.tag_configure(bd, elide=not bool(int(c.tag_cget(bd, "elide") or 0))))
        c.tag_bind(tg, "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind(tg, "<Leave>", lambda e: c.configure(cursor="arrow"))

    def reasoning_block(self, reasoning):
        self._foldable(f"▸ Razonamiento ({miles(len(reasoning))} caracteres) — clic para mostrar u ocultar",
                       reasoning.strip() + "\n", ("rtoggle",), ("reason",), "r")

    # ------------------------------------------------------------ mensajes

    def user(self, text, file_names):
        self.put("\nVos\n", "hdr_user")
        if text:
            self.put(text + "\n")
        for n in file_names:
            self.put(f"📎 {os.path.basename(n)}\n", "note")
        self._last_role = "user"

    def assistant_header(self, label):
        if self._last_role != "assistant":
            self.put(f"\n{label}\n", "hdr_bot")
        self._last_role = "assistant"

    def assistant_body(self, reasoning, content, note=None):
        if reasoning:
            self.reasoning_block(reasoning)
        if content:
            self.markdown(content)
            self.put("\n")
        if note:
            self.put(note + "\n", "note")

    def tool_call(self, name, args):
        self.put("› " + summarize_call(name, args) + "\n", "tool")

    def tool_result(self, result):
        lines = result.strip().splitlines() or [""]
        first = lines[0][:200]
        bad = result_is_problem(result)
        self.put(f"↳ {first}\n", "toolbad" if bad else "toolres")
        if len(lines) > 1:
            body = "\n".join(lines[1:])
            if len(body) > 4000:
                body = body[:4000] + "\n… (recortado en pantalla; el modelo recibió el resultado completo)"
            self._foldable(f"   ▸ ver detalle ({len(lines) - 1} líneas más)", body, ("tooltoggle",), ("toolbody",), "x")

    # ------------------------------------------------------------ streaming de un paso

    def begin_stream(self):
        c = self.text
        c.mark_set("as", "end-1c")
        c.mark_gravity("as", "left")
        self.streaming = True

    def stream_piece(self, kind, val):
        self.put(val, "reason" if kind == "reasoning" else ())

    def clear_stream(self):
        """Borra lo escrito desde begin_stream. Sin un paso abierto no hace nada: la marca 'as' de un paso anterior
        seguiría existiendo y borraría contenido ya definitivo."""
        if not self.streaming:
            return
        self.streaming = False
        c = self.text
        c.configure(state="normal")
        # hasta end-1c y no end: si el rango empieza al inicio de línea, tkinter borra también el salto de línea
        # anterior y el encabezado quedaría pegado al texto que sigue.
        c.delete("as", "end-1c")
        c.configure(state="disabled")

    # ------------------------------------------------------------ recarga de una sesión

    def render_session(self, messages, label):
        """Dibuja un historial guardado con el mismo aspecto que tuvo en vivo."""
        self.clear()
        for m in messages:
            role = m.get("role")
            if role == "user":
                text, files = split_user_content(str(m.get("content") or ""))
                self.user(text, files)
            elif role == "assistant":
                self.assistant_header(label)
                self.assistant_body(m.get("_reasoning", ""), m.get("content") or "")
                for c in m.get("tool_calls") or []:
                    name, raw = c["function"]["name"], c["function"]["arguments"]
                    try:
                        args = json.loads(raw)
                    except ValueError:
                        args = raw
                    self.tool_call(name, args)
            elif role == "tool":
                self.tool_result(str(m.get("content") or ""))
        self.text.see("end")
