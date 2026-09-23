"""Colores y estilos ttk. Un solo lugar para la app, los diálogos y los tests."""
from tkinter import ttk

THEMES = {
    "claro": dict(bg="#f2f4f7", panel="#ffffff", fg="#1c2733", muted="#66727f", user="#0b5cad",
                  bot="#17743f", code_bg="#eceff3", reason="#8a94a0", accent="#0b5cad",
                  accent_fg="#ffffff", err="#b3261e", border="#d3d9e0", accent_text="#0b5cad", meter_fill="#b6d3f2",
                  final="#0b7a8f"),
    # Azul nocturno estilo VSCode: tres capas de azul (ventana < paneles < campos) y acentos en azul claro / cian.
    "oscuro": dict(bg="#14202e", panel="#1c2d42", fg="#d4e1f0", muted="#8aa1bb", user="#5cb3ff",
                   bot="#4fd1c5", code_bg="#0d1826", reason="#7f96b0", accent="#0e639c",
                   accent_fg="#ffffff", err="#f48771", border="#2c4260", accent_text="#6cb6ff", meter_fill="#0e639c",
                   final="#8be9fd"),
}


def pick(name):
    return THEMES.get(name, THEMES["claro"])


def apply_styles(root, t):
    """Configura los estilos ttk con la paleta t (usa el tema 'clam', que sí respeta los colores)."""
    s = ttk.Style(root)
    if s.theme_use() != "clam":
        s.theme_use("clam")
    root.configure(bg=t["bg"])
    s.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"], font=("Segoe UI", 10))
    s.configure("TFrame", background=t["bg"])
    s.configure("TLabel", background=t["bg"], foreground=t["fg"])
    s.configure("Err.TLabel", background=t["bg"], foreground=t["err"])
    s.configure("Ok.TLabel", background=t["bg"], foreground=t["bot"])
    s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
    s.configure("Title.TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 10, "bold"))
    s.configure("TButton", background=t["panel"], foreground=t["fg"], bordercolor=t["border"], padding=(10, 4))
    s.map("TButton", background=[("active", t["border"]), ("disabled", t["bg"])], foreground=[("disabled", t["muted"])])
    s.configure("Accent.TButton", background=t["accent"], foreground=t["accent_fg"])
    s.map("Accent.TButton", background=[("active", t["accent"]), ("disabled", t["border"])],
          foreground=[("disabled", t["muted"])])
    s.configure("Tool.TButton", padding=(6, 3))
    s.configure("TCombobox", fieldbackground=t["panel"], background=t["panel"], foreground=t["fg"],
                arrowcolor=t["fg"], bordercolor=t["border"], selectbackground=t["panel"], selectforeground=t["fg"])
    s.map("TCombobox", fieldbackground=[("readonly", t["panel"]), ("disabled", t["bg"])],
          foreground=[("readonly", t["fg"]), ("disabled", t["muted"])])
    s.configure("TEntry", fieldbackground=t["panel"], foreground=t["fg"], insertcolor=t["fg"], bordercolor=t["border"])
    s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
    s.configure("TRadiobutton", background=t["bg"], foreground=t["fg"])
    s.map("TRadiobutton", foreground=[("disabled", t["muted"])])
    s.configure("TSpinbox", fieldbackground=t["panel"], foreground=t["fg"], background=t["panel"])
    s.configure("TNotebook", background=t["bg"], bordercolor=t["border"])
    s.configure("TNotebook.Tab", background=t["bg"], foreground=t["fg"], padding=(10, 4))
    s.map("TNotebook.Tab", background=[("selected", t["panel"])])
    s.configure("TPanedwindow", background=t["bg"])
    s.configure("Sash", background=t["border"], sashthickness=5)
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=t["panel"], troughcolor=t["bg"], bordercolor=t["bg"], arrowcolor=t["fg"])
    s.configure("Treeview", background=t["panel"], fieldbackground=t["panel"], foreground=t["fg"], bordercolor=t["border"],
                rowheight=22)
    s.map("Treeview", background=[("selected", t["accent"])], foreground=[("selected", t["accent_fg"])])
    s.configure("Treeview.Heading", background=t["bg"], foreground=t["muted"], bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", t["panel"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["accent_fg"])
    return s
