"""Colores y estilos ttk. Un solo lugar para la app, los diálogos y los tests."""
import os
from tkinter import ttk

THEMES = {
    # Azul nocturno estilo VSCode: capas de azul (ventana < chat < sesiones/explorador/entrada) y acentos en azul claro / cian.
    "Predeterminado": dict(dark=True, bg="#0c1520", panel="#1c2d42", side="#26405e", fg="#d4e1f0", muted="#8aa1bb", user="#5cb3ff",
                   bot="#4fd1c5", code_bg="#0d1826", reason="#7f96b0", accent="#0e639c",
                   accent_fg="#ffffff", err="#f48771", border="#2c4260", accent_text="#6cb6ff", meter_fill="#0e639c",
                   final="#8be9fd", chat_bg="#000000"),      # el chat, en negro: la conversación contrasta con los paneles
    # La terminal del Actualizador de Warlords (actualizador-warlords, Terminal.cs: Paleta), sin el cartel gigante:
    # fondo casi negro, letra monoespaciada y los colores vivos de su paleta (verde, cian, violeta, naranja).
    "Warlords": dict(dark=True, font="JetBrains Mono", chat_only=True, terminal=True, title="#f1fa8c", bg="#0e1016", panel="#1a1d27", side="#151821", fg="#d8dee9",
                     muted="#6b7289", user="#ffb86c", bot="#50fa7b", code_bg="#1a1d27", reason="#6b7289",
                     accent="#bd93f9", accent_fg="#0e1016", err="#ff5555", border="#3a3f50", accent_text="#8be9fd",
                     meter_fill="#50fa7b", final="#f1fa8c", chat_bg="#0e1016"),
}

# Nombres viejos guardados en la configuración -> el tema que les corresponde ahora.
ALIASES = {"oscuro": "Predeterminado", "claro": "Predeterminado"}   # «claro» se quitó en 1.0.0.19


# «Continuar» tiene que saltar a la vista en cualquier tema: amarillo limón con letra negra, igual en claro y oscuro.
LIMON = "#fff44f"
LIMON_ACTIVO = "#ffe61a"
NEGRO = "#000000"


def canonical(name):
    return ALIASES.get(name, name) if ALIASES.get(name, name) in THEMES else "Predeterminado"


def pick(name):
    return THEMES[canonical(name)]


APP_THEME = "Predeterminado"      # la ventana siempre usa este; un tema chat_only cambia solo el panel de respuestas
FONT_FILES = ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf")
_fonts_loaded = None


def load_fonts(folder):
    """Registra JetBrains Mono solo para este proceso (sin instalarla en Windows). True si quedó disponible."""
    global _fonts_loaded
    if _fonts_loaded is None:
        try:
            import ctypes
            n = 0
            for name in FONT_FILES:
                p = os.path.join(folder, name)
                if os.path.isfile(p):
                    n += ctypes.windll.gdi32.AddFontResourceExW(p, 0x10, 0)      # FR_PRIVATE
            _fonts_loaded = n > 0
        except (AttributeError, OSError):
            _fonts_loaded = False
    return _fonts_loaded


def chat_font(t, families=()):
    """La letra del panel de respuestas: la del tema si existe en el sistema; si no, Consolas (como el Actualizador)."""
    f = t.get("font", "Segoe UI")
    if families and f not in families:
        return "Consolas" if t.get("font") else "Segoe UI"
    return f


def ui_font(t):
    return t.get("font", "Segoe UI")


def apply_styles(root, t):
    """Configura los estilos ttk con la paleta t (usa el tema 'clam', que sí respeta los colores)."""
    s = ttk.Style(root)
    if s.theme_use() != "clam":
        s.theme_use("clam")
    root.configure(bg=t["bg"])
    s.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"], font=(ui_font(t), 10))
    s.configure("TFrame", background=t["bg"])
    s.configure("TLabel", background=t["bg"], foreground=t["fg"])
    s.configure("Err.TLabel", background=t["bg"], foreground=t["err"])
    s.configure("Ok.TLabel", background=t["bg"], foreground=t["bot"])
    s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
    s.configure("Title.TLabel", background=t["bg"], foreground=t["fg"], font=(ui_font(t), 10, "bold"))
    s.configure("TButton", background=t["panel"], foreground=t["fg"], bordercolor=t["border"], padding=(10, 4))
    s.map("TButton", background=[("active", t["border"]), ("disabled", t["bg"])], foreground=[("disabled", t["muted"])])
    s.configure("Accent.TButton", background=t["accent"], foreground=t["accent_fg"])
    s.map("Accent.TButton", background=[("active", t["accent"]), ("disabled", t["border"])],
          foreground=[("disabled", t["muted"])])
    s.configure("Continue.TButton", background=LIMON, foreground=NEGRO, bordercolor=NEGRO,
                font=("Segoe UI", 10, "bold"))
    s.map("Continue.TButton", background=[("active", LIMON_ACTIVO), ("disabled", t["border"])],
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
    # sesiones y explorador: un azul más claro que el del chat, para separarlos de la conversación
    s.configure("Side.Treeview", background=t["side"], fieldbackground=t["side"])
    s.configure("Treeview.Heading", background=t["bg"], foreground=t["muted"], bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", t["panel"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["accent_fg"])
    return s
