"""Sesiones: cada conversación con su carpeta de trabajo, modelo, historial y diario de cambios, en un JSON propio.

Regla: nunca se pierde una conversación. Borrar una sesión la mueve a _papelera\\; un archivo ilegible se aparta con
la extensión .dañado en lugar de ignorarse o pisarse.
"""
import datetime
import json
import os
import secrets
import shutil
import tempfile

VERSION = 1


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class Session:
    def __init__(self, workspace="", model="", effort="", approval="ask"):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.id = f"{stamp}-{secrets.token_hex(2)}"
        self.title = ""
        self.created = self.updated = _now()
        self.workspace = workspace
        self.model = model
        self.effort = effort
        self.approval = approval
        self.messages = []
        self.totals = {"in": 0, "out": 0, "reason": 0, "hit": 0}
        self.journal = []
        self.version = VERSION

    FIELDS = ("id", "title", "created", "updated", "workspace", "model", "effort", "approval", "messages", "totals",
              "journal", "version")

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not isinstance(d.get("messages"), list):
            raise ValueError("no es una sesión válida")
        s = cls()
        for k in cls.FIELDS:
            if k in d:
                setattr(s, k, d[k])
        s.totals = {**{"in": 0, "out": 0, "reason": 0, "hit": 0}, **(s.totals if isinstance(s.totals, dict) else {})}
        if not isinstance(s.journal, list):
            s.journal = []
        return s

    def auto_title(self):
        """El título por defecto sale del primer mensaje del usuario; uno puesto a mano no se pisa."""
        if self.title:
            return
        for m in self.messages:
            if m.get("role") == "user":
                txt = " ".join(str(m.get("content") or "").split())
                if txt.startswith("Archivo adjunto:"):
                    txt = "(archivos adjuntos)"
                self.title = (txt[:48] + "…") if len(txt) > 48 else txt
                return

    @property
    def display_title(self):
        return self.title or "Sesión nueva"


class SessionStore:
    def __init__(self, directory):
        self.dir = directory
        self.trash = os.path.join(directory, "_papelera")
        self.warnings = []           # archivos ilegibles apartados en esta ejecución

    def _path(self, sid):
        if not sid or any(c in sid for c in "\\/:*?\"<>|") or sid.startswith("."):
            raise ValueError("id de sesión inválido")
        return os.path.join(self.dir, sid + ".json")

    def save(self, s):
        """Guarda de forma atómica. Una sesión sin mensajes no se escribe (evita llenar la lista de vacías)."""
        if not s.messages:
            return False
        s.auto_title()
        s.updated = _now()
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(s.to_dict(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path(s.id))
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True

    def _read(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Session.from_dict(json.load(f))
        except (OSError, ValueError) as e:
            bad = path + ".dañado"
            try:
                os.replace(path, bad)
                self.warnings.append(f"{os.path.basename(path)} estaba dañada ({e}); quedó apartada como {os.path.basename(bad)}.")
            except OSError:
                self.warnings.append(f"{os.path.basename(path)} no se pudo leer ({e}).")
            return None

    def load(self, sid):
        p = self._path(sid)
        return self._read(p) if os.path.isfile(p) else None

    def list(self):
        """Sesiones ordenadas de la más reciente a la más vieja."""
        out = []
        if not os.path.isdir(self.dir):
            return out
        for n in os.listdir(self.dir):
            if n.endswith(".json") and os.path.isfile(os.path.join(self.dir, n)):
                s = self._read(os.path.join(self.dir, n))
                if s is not None:
                    out.append(s)
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def delete(self, sid):
        """A la papelera, no al vacío. Devuelve la ruta donde quedó."""
        p = self._path(sid)
        if not os.path.isfile(p):
            return None
        os.makedirs(self.trash, exist_ok=True)
        dst = os.path.join(self.trash, os.path.basename(p))
        if os.path.exists(dst):
            dst = os.path.join(self.trash, f"{datetime.datetime.now():%H%M%S}-{os.path.basename(p)}")
        shutil.move(p, dst)
        return dst


def export_markdown(s, label="Asistente"):
    """La sesión como Markdown legible: para guardarla, compartirla o pegarla en otro lado."""
    import chatview

    out = [f"# {s.display_title}", ""]
    if s.workspace:
        out.append(f"- Carpeta: `{s.workspace}`")
    if s.model:
        out.append(f"- Modelo: `{s.model}`")
    out += [f"- Creada: {s.created}", f"- Última actividad: {s.updated}", ""]
    for m in s.messages:
        role = m.get("role")
        if role == "user":
            text, files = chatview.split_user_content(str(m.get("content") or ""))
            out += ["## Vos", "", text or "(sin texto)"]
            out += [f"- adjunto: {f}" for f in files]
            out.append("")
        elif role == "assistant":
            out += [f"## {label}", ""]
            if m.get("content"):
                out += [m["content"], ""]
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"]["arguments"])
                except ValueError:
                    args = c["function"]["arguments"]
                out.append("- `" + chatview.summarize_call(c["function"]["name"], args).replace("`", "'") + "`")
            if m.get("tool_calls"):
                out.append("")
        elif role == "tool":
            body = str(m.get("content") or "")
            if len(body) > 2000:
                body = body[:2000] + "\n… (recortado)"
            out += ["```", body, "```", ""]
    return "\n".join(out).rstrip() + "\n"
