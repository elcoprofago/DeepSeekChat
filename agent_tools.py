"""Herramientas del agente: leer, buscar, editar y ejecutar dentro de una carpeta de trabajo.

Sin ventana ni red: se prueba sola. Todo lo que el modelo puede hacerle al disco pasa por acá.

Reglas de fondo:
  * Nada fuera de la carpeta de trabajo (se resuelve el camino real, así que un junction no sirve de puerta).
  * Antes de pisar un archivo se guarda una copia; cada cambio queda en un diario y se puede deshacer. Vale también
    para lo que pisa un comando (copy, move, `>`...), en los casos que overwrite_targets reconoce.
  * Los comandos destructivos o interactivos se bloquean siempre, aunque el usuario haya dado permiso total.
  * Borrar se hace con delete_path, no con `del`/`rm`: pide permiso, no sigue enlaces y deja lo borrado en una copia que se restaura.
  * Toda salida que vuelve al modelo va truncada: un log gigante no puede reventar el contexto.
"""
import ctypes
import difflib
import filecmp
import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time

MAX_READ_BYTES = 1024 * 1024
DEFAULT_READ_LINES = 400
MAX_OUTPUT_CHARS = 12000
MAX_LIST_ENTRIES = 300
MAX_SEARCH_MATCHES = 200
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_DELETE_BYTES = 500 * 1024 * 1024      # lo que se borra se conserva en una copia: más que esto no se borra desde acá
IGNORE_DIRS = {".git", "node_modules", ".next", "__pycache__", ".venv", "venv", ".dschat-backup", ".mypy_cache",
               ".pytest_cache", "dist", "build", ".turbo", ".vercel"}


class ToolError(Exception):
    """Falla esperable de una herramienta: se devuelve al modelo como texto, no rompe el bucle."""


# ---------------------------------------------------------------- filtro de comandos

_BLOCK = [
    (r"\bgit\s+rm\b|\brimraf\b", "borrar archivos o carpetas"),
    (r"\bri\s+-r", "borrar archivos o carpetas (PowerShell)"),
    (r"\bformat(\.com)?\s+[a-z]:", "formatear un volumen"),
    (r"\b(diskpart|bcdedit|bootrec|bcdboot|vssadmin|cipher\s+/w|takeown|icacls|sfc|dism)\b", "tocar disco, arranque o permisos del sistema"),
    (r"\breg(\.exe)?\s+(add|delete|import|load|unload|restore|copy|save)\b", "escribir en el registro"),
    (r"\b(set-itemproperty|new-itemproperty|remove-itemproperty)\b", "escribir en el registro (PowerShell)"),
    (r"\bsc(\.exe)?\s+(delete|config|stop|create)\b", "modificar servicios"),
    (r"\b(shutdown|restart-computer|stop-computer)\b", "apagar o reiniciar"),
    (r"\b(taskkill|stop-process|wmic)\b", "terminar procesos"),
    (r"\bnet\s+(user|localgroup|stop)\b", "modificar usuarios o servicios"),
    (r"\bgit\s+(push|clean|rebase|checkout\s+--|stash\s+(drop|clear)|branch\s+-d|filter-branch|gc\s+--prune)", "operación de git que pierde o publica trabajo"),
    (r"\brobocopy\b.*(/mir|/purge|/move)", "robocopy que borra o mueve"),
    (r"\b(npm|yarn|pnpm)\s+(publish|unpublish)\b|\bvercel\b|\bgh\s+(repo\s+delete|release|pr\s+merge)\b", "publicar o desplegar"),
    (r"\bsupabase\s+(db\s+(push|reset)|link)\b|\bdrop\s+(table|database)\b|\btruncate\s+table\b", "tocar una base de datos"),
    (r"\b(invoke-expression|iex)\b|-enc(odedcommand)?\b|\bset-executionpolicy\b", "ejecutar código opaco o cambiar la política de ejecución"),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b.*\|\s*(sh|bash|iex|powershell|cmd)", "ejecutar lo que se descarga"),
    (r"\b(pause|notepad|explorer|mspaint|taskmgr|regedit|mmc)\b", "abre una ventana o pide interacción"),
    (r"^\s*(python|python3|py|node|cmd|powershell|pwsh|bash|sh|ipython)(\.exe)?\s*$", "abre un intérprete interactivo"),
    (r"^\s*start\b", "abre un programa aparte"),
]
_BLOCK_RE = [(re.compile(p, re.I), why) for p, why in _BLOCK]

# Borrar: `del` es además una palabra del español («Informe del mes.txt», «Resumen del día»). En un comando de cmd solo
# cuenta en posición de comando: al principio, tras un separador, `(`, comillas, `do`, `else`, `call`, `/c`, `-exec`,
# `xargs`, `sudo`, `=` o un `if exist X`/`if errorlevel N`/`if a==b`. Si lo que se ejecuta es un intérprete (PowerShell,
# bash, wsl, ssh...), dentro de él se busca en cualquier lugar, como antes: su sintaxis no se sigue desde acá.
_DEL_WORDS = r"(?P<w>rm|rmdir|rd|del|erase|remove-item|ri)(?:\.exe)?(?=$|[\s;&|)\"'/])"
_ARG = r"""(?:"[^"]*"|[^\s"]+)"""
_DEL_AT_CMD = re.compile(
    r"""(?:^|[&|;(\n{="'`]|\b(?:do|else|then|call|sudo)\s|/[ck]\s|-exec\s|\bxargs(?:\s+-\S+)*\s|"""
    r"""\bif\s+(?:/i\s+)?(?:not\s+)?(?:exist\s+""" + _ARG + r"""|errorlevel\s+\d+|defined\s+\S+|""" + _ARG + r"""\s*==\s*""" + _ARG +
    r"""|""" + _ARG + r"""\s+(?:equ|neq|lss|leq|gtr|geq)\s+""" + _ARG + r""")\s)\s*@?""" + _DEL_WORDS, re.I)
_DEL_ANYWHERE = re.compile(r"\b" + _DEL_WORDS, re.I)
_SHELLS = {"powershell", "pwsh", "cmd", "bash", "sh", "wsl", "ssh", "busybox", "env", "runas", "sudo", "git-bash", "zsh"}


def _deletes(code):
    """El motivo si la vista de código (ver _code_view) borra con del/rm/rd/erase/Remove-Item, o None."""
    for seg, _ in _cmd_segments(code):
        s = seg.lstrip(" \t@(")
        first = re.match(r"""["']?([^\s"'/<>]+)""", s)
        name = os.path.basename(first.group(1)).lower() if first else ""
        name = name[:-4] if name.endswith(".exe") else name
        m = (_DEL_ANYWHERE if name in _SHELLS else _DEL_AT_CMD).search(s)
        if m:
            return "borrar archivos o carpetas" + (" (PowerShell)" if m.group("w").lower() in ("remove-item", "ri") else "")
    return None


# El mensaje de un commit es texto, no un comando: «uso del proceso» no es un `del`. Solo se le quita eso; cualquier
# otra cosa entre comillas (p. ej. `powershell -c "del x"`) se sigue revisando.
_COMMIT_MSG = re.compile(r"""(?<![\w-])(?:-m|--message)(?:\s+|=)?(?:"[^"]*"|'[^']*')""", re.I)
# `git rm --cached` solo saca el archivo del índice y lo deja en disco: hace falta para no versionar algo commiteado por error.
_GIT_RM_CACHED = re.compile(r"\bgit\s+rm\b(?=[^;&|\n]*\s--cached\b)", re.I)
_GIT_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n]")
_GIT_RESET_LOSES = re.compile(r"--(hard|merge|keep)\b", re.I)
_GIT_STAGED_ONLY = re.compile(r"(--staged\b|\s-S\b)", re.I)
_GIT_TOUCHES_TREE = re.compile(r"(--worktree\b|\s-W\b)", re.I)


def _git_index_risk(command):
    """`git reset` y `git restore` solo pierden trabajo si tocan el árbol de trabajo. Sacar cosas del índice
    (`reset HEAD`, `restore --staged`) es lo que hace falta para armar un commit y no borra nada."""
    for seg in _GIT_SEGMENT_SPLIT.split(command):
        if re.search(r"\bgit\b.*\breset\b", seg, re.I) and _GIT_RESET_LOSES.search(seg):
            return "operación de git que pierde o publica trabajo"
        if re.search(r"\bgit\b.*\brestore\b", seg, re.I) and (not _GIT_STAGED_ONLY.search(seg) or _GIT_TOUCHES_TREE.search(seg)):
            return "operación de git que pierde o publica trabajo"
    return None


# En cmd, `programa | more` devuelve el código de salida de `more`, no el de `programa`: un fallo queda como «0».
_PIPE_MASKS_EXIT = re.compile(r"\|\s*(more|findstr|find|tee|head|tail|sort|select-string)\b", re.I)


def cmd_path(path):
    """El PATH para run_command, que promete cmd de Windows: si una carpeta anterior a System32 tapa find o sort (el
    usr\\bin de Git, cuando la app se abre desde Git Bash), System32 pasa adelante. Medido el 2026-09-28: con ese PATH,
    `find /c "archivo"` era el find de Unix recorriendo todo C: hasta el timeout de 60 s. Si nada lo tapa, no cambia."""
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    parts = [p for p in path.split(os.pathsep) if p]
    norm = [os.path.normcase(os.path.normpath(p)) for p in parts]
    i = norm.index(os.path.normcase(sys32)) if os.path.normcase(sys32) in norm else len(parts)
    if any(os.path.isfile(os.path.join(p, exe)) for p in parts[:i] for exe in ("find.exe", "sort.exe")):
        return os.pathsep.join([sys32] + parts[:i] + parts[i + 1:])
    return path


def exit_code_text(code):
    """Windows devuelve el código como entero sin signo de 32 bits. Caso real (sesión TEMPORALES, 2026-09-28): el
    modelo recibió «4294770688» y no supo qué era; como -196608 / 0xFFFD0000 se puede reconocer y buscar."""
    if code is not None and code >= 0x80000000:
        return f"{code} = {code - 0x100000000} = 0x{code:08X}"
    return str(code)


# ---------------------------------------------------------------- vista de código: el texto que nunca se ejecuta, fuera
# Caso real (sesión TEMPORALES, 2026-09-28): «patrones protegidos del sistema» dentro de un comentario, o `findstr
# /C:"Remove-Item" x.ps1` para buscar en un script, se bloqueaban como «borrar archivos». El filtro revisa ahora solo lo
# que se ejecuta: se quitan los argumentos de echo/findstr y los textos de PowerShell que son datos (comentarios, y
# literales asignados a una variable, al principio de una instrucción o pasados a Set-Content, Write-Host...). Cualquier
# otro texto entre comillas se sigue revisando, porque puede ser un comando (`cmd /c "del x"`, `& 'Remove-Item' x`).
# Es una red contra errores, no contra un modelo que quiera esquivarla: un .bat escrito con write_file ya la esquiva.

_CMD_TEXT_ONLY = {"echo", "rem", "title", "findstr"}      # sus argumentos son texto. `find` no: el de Unix tiene -delete y -exec
_PS_DATA_CMDS = {"set-content", "add-content", "out-file", "write-host", "write-output", "write-warning", "write-error",
                 "write-verbose", "write", "echo", "select-string", "sls", "sc", "ac"}
_PS_START = re.compile(r"""^\s*["']?((?:powershell|pwsh)(?:\.exe)?)["']?\s""", re.I)
_PS_CMD_ARG = re.compile(r"""\s-(?:c|command)(?:\s+|$)""", re.I)


def _cmd_segments(command):
    """[(segmento, separador que lo sigue)] como parte cmd.exe: solo las comillas dobles protegen, y `^` escapa.
    Las simples NO son comillas para cmd (`echo it's & del x` ejecuta el del)."""
    out, cur, q, i = [], [], False, 0
    while i < len(command):
        ch = command[i]
        if ch == "^" and not q and i + 1 < len(command):
            cur.append(command[i:i + 2])
            i += 2
            continue
        if ch == '"':
            q = not q
        elif not q and ch in "&|\n" and not (ch == "&" and cur and cur[-1].endswith(">")):
            sep = command[i:i + 2] if command[i:i + 2] in ("&&", "||") else ch
            out.append(("".join(cur), sep))
            cur = []
            i += len(sep)
            continue
        cur.append(ch)
        i += 1
    out.append(("".join(cur), ""))
    return out


def _ps_code(script):
    """El script de PowerShell sin los textos que son datos (ver arriba). Lo que no entiende lo deja como está."""
    out, i, n = [], 0, len(script)
    head, assign, prev = None, False, ""       # primera palabra de la instrucción, si ya hubo un `=`, último carácter útil
    while i < n:
        ch = script[i]
        if ch in ";\n|{}":
            head, assign, prev = None, False, ch
            out.append(ch)
            i += 1
            continue
        if script.startswith("<#", i):
            j = script.find("#>", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        if ch == "#" and script[i - 1:i] in ("", " ", "\t", ";", "(", "{", "\n"):
            j = script.find("\n", i)
            i = n if j < 0 else j
            continue
        here = re.compile(r"@(['\"])[ \t]*\r?\n").match(script, i) if ch == "@" else None
        if ch in "'\"" or here:
            if here:
                qc = here.group(1)
                end = re.compile(r"\r?\n" + qc + "@").search(script, here.end())
                j = n if end is None else end.end()
                body = script[i:j]
            else:
                qc, j = ch, i + 1
                while j < n:
                    if script[j] == "`" and qc == '"':
                        j += 2
                        continue
                    if script[j] == qc:
                        if j + 1 < n and script[j + 1] == qc:      # '' o "" dentro del texto
                            j += 2
                            continue
                        break
                    j += 1
                j = min(j + 1, n)
                body = script[i:j]
            called = prev in ("&", ".")                             # & 'Remove-Item' x: el texto ES el comando
            data = not head or assign or head in _PS_DATA_CMDS
            if data and not called and not (qc == '"' and "$(" in body):
                out.append(" '' ")
            else:
                out.append(body)
            if head is None:
                head = ""                                           # la instrucción empezó con un texto
            prev = qc
            i = j
            continue
        if ch == "=" and head and head.startswith("$") and script[i - 1:i] not in ("-", "!", "<", ">") and script[i + 1:i + 2] != "=":
            assign = True
        if head is None and not ch.isspace() and ch not in "(@":
            m = re.match(r"[^\s;|{}()='\"]+", script[i:])
            if m:
                head = m.group(0).lower()
                out.append(m.group(0))
                prev = m.group(0)[-1]
                i += m.end()
                continue
        out.append(ch)
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


def _code_view(command, depth=0):
    """El comando con el texto que nunca se ejecuta quitado (ver arriba). Conserva los separadores."""
    parts = []
    for seg, sep in _cmd_segments(command):
        s = seg.lstrip(" \t@(")
        pre = seg[:len(seg) - len(s)]
        word = re.match(r"""[^\s"'/<>]+""", s)
        name = word.group(0).lower() if word else ""
        name = name[:-4] if name.endswith(".exe") else name
        if name in _CMD_TEXT_ONLY or re.match(r"echo[.:(]", name):
            seg = pre + name
        elif name == "cmd" and depth < 3:
            m = re.search(r"\s/[ck]\s+(.*)$", s, re.I | re.S)
            if m:
                inner = m.group(1).strip()
                if len(inner) >= 2 and inner[0] == inner[-1] == '"':
                    inner = inner[1:-1]
                seg = pre + s[:m.start(1)] + _code_view(inner, depth + 1)
        elif _PS_START.match(s):
            m = _PS_CMD_ARG.search(s)
            if m:
                script, rest = s[m.end():], ""
                if script.startswith('"'):
                    # el script entre comillas termina en la primera " sin \ delante; lo que sigue (`> log.txt`, otros
                    # argumentos) se revisa entero, sin quitarle nada
                    k = re.search(r'(?<!\\)"', script[1:])
                    script, rest = (script[1:k.start() + 1], script[k.start() + 2:]) if k else (script[1:], "")
                seg = pre + s[:m.end()] + _ps_code(script.replace('\\"', '"')) + " " + rest
        parts.append(seg + sep)
    return "".join(parts)


def check_command(command):
    """Devuelve el motivo si el comando no debe ejecutarse nunca, o None si puede seguir al pedido de permiso."""
    if not command or not command.strip():
        return "comando vacío"
    if re.search(r"\bgit\b.*\bcommit\b", command, re.I):
        command = _COMMIT_MSG.sub(" ", command)
    command = _GIT_RM_CACHED.sub("git untrack", command)
    command = _code_view(command)
    why = _deletes(command)
    if why:
        return why
    for rx, why in _BLOCK_RE:
        if rx.search(command):
            return why
    return _git_index_risk(command)


# ---------------------------------------------------------------- sobrescrituras de comandos
# Un comando de consola puede pisar un archivo sin pasar por write_file (caso real, sesión cronometro 2026-09-26:
# `copy /b A + B + C dist\Cronometro_v021.exe` reemplazó el exe con aprobación "all" y sin copia). No se bloquea
# —copiar es trabajo normal—: se reconoce el destino y se guarda una copia antes, para que «Deshacer» lo alcance.
# Es un reconocimiento de lo común (copy/move/cp/mv, Copy-Item/Move-Item, Set-Content/Out-File/Add-Content, `>`
# y `>>`), no un intérprete de cmd: variables, comodines, xcopy/robocopy y lo que escribe un programa quedan afuera.

_PS_SWITCHES = {"-force", "-recurse", "-append", "-nonewline", "-passthru", "-whatif", "-confirm", "-noclobber", "-container"}
_COPY_CMDS = {"copy", "move", "cp", "mv", "copy-item", "move-item", "cpi", "mi"}
_WRITE_CMDS = {"set-content", "out-file", "add-content", "sc", "ac"}
_CD_CMDS = {"cd", "chdir", "pushd", "set-location", "sl"}
_PS_INNER = re.compile(r"""\b(?:powershell|pwsh)(?:\.exe)?\b[^"']*?-c(?:ommand)?\s+(["'])(.*?)\1""", re.I | re.S)


def _split_segments(command):
    """Parte en comandos simples por && || & | ; y saltos de línea, fuera de comillas. `2>&1` no es un separador."""
    segs, cur, q, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if q:
            q = None if ch == q else q
        elif ch in "\"'":
            q = ch
        elif ch in "&|;\n" and not (ch == "&" and cur and cur[-1] == ">"):
            segs.append("".join(cur))
            cur = []
            if command[i:i + 2] in ("&&", "||"):
                i += 1
            i += 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _redirects(seg):
    """Saca las redirecciones `>`/`>>` fuera de comillas. Devuelve (segmento sin ellas, destinos a archivo)."""
    rest, targets, q, i = [], [], None, 0
    while i < len(seg):
        ch = seg[i]
        if q:
            q = None if ch == q else q
        elif ch in "\"'":
            q = ch
        elif ch == ">":
            if rest and rest[-1].isdigit():
                rest.pop()                                   # 2>, 1>
            i += 2 if seg[i:i + 2] == ">>" else 1
            m = re.match(r"""\s*("[^"]*"|'[^']*'|[^\s&|<>]+)""", seg[i:])
            if m:
                t = m.group(1).strip("\"'")
                if not t.startswith("&") and t.lower() not in ("nul", "con", "$null"):
                    targets.append(t)
                i += m.end()
            continue
        rest.append(ch)
        i += 1
    return "".join(rest), targets


def _tokens(seg):
    return [t.strip("\"'") for t in re.findall(r""""[^"]*"|'[^']*'|[^\s"']+""", seg)]


def _positional(args):
    """(posicionales, {parámetro PowerShell: valor}). `/b` y compañía son opciones de cmd; `-X valor` de PowerShell."""
    pos, named, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("/") and len(a) <= 3:
            pass
        elif a.startswith("-") and len(a) > 1:
            if a.lower() not in _PS_SWITCHES and i + 1 < len(args) and not args[i + 1].startswith("-"):
                named[a.lower()] = args[i + 1]
                i += 1
        elif a != "+":
            pos += [p for p in a.split("+") if p]            # copy A+B dest
        i += 1
    return pos, named


def overwrite_targets(command, cwd):
    """Rutas absolutas que el comando puede sobrescribir, según lo que se reconoce (ver arriba)."""
    out = []
    for m in _PS_INNER.finditer(command):
        out += overwrite_targets(m.group(2), cwd)
    for seg in _split_segments(command):
        seg, reds = _redirects(seg)
        out += [os.path.join(cwd, t) for t in reds]
        toks = _tokens(seg)
        if not toks:
            continue
        name, (pos, named) = toks[0].lower(), _positional(toks[1:])
        if name.endswith(".exe"):
            name = name[:-4]
        if name in _CD_CMDS and pos:
            cwd = os.path.join(cwd, pos[-1])
        elif name in _COPY_CMDS:
            src = pos[:-1] if "-destination" not in named and len(pos) > 1 else pos
            src = [named["-path"]] if "-path" in named else [named["-literalpath"]] if "-literalpath" in named else src
            dest = named.get("-destination") or (pos[-1] if len(pos) > 1 else ".")
            dest = os.path.join(cwd, dest)
            if os.path.isdir(dest):
                out += [os.path.join(dest, os.path.basename(s.rstrip("\\/"))) for s in src]
            else:
                out.append(dest)
        elif name in _WRITE_CMDS:
            t = named.get("-path") or named.get("-filepath") or named.get("-literalpath") or (pos[0] if pos else None)
            if t:
                out.append(os.path.join(cwd, t))
    seen, res = set(), []
    for p in out:
        if "*" in p or "?" in p:
            continue
        k = os.path.normcase(os.path.normpath(p))
        if k not in seen:
            seen.add(k)
            res.append(os.path.normpath(p))
    return res


# ---------------------------------------------------------------- borrado

def _is_link(p):
    """Symlink o junction (punto de reanálisis). Borrarlo, o copiarlo a otro volumen, podría alcanzar lo que apunta fuera."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return os.path.islink(p) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _inside(p, root):
    """p es root o está debajo. La raíz de una unidad (`C:\\`) ya termina en separador: sumarle otro hacía que nada
    quedara adentro (caso real, sesión TEMPORALES 2026-09-28: con la carpeta C:\\ se listaba todo y no se podía
    escribir ni leer nada)."""
    p, root = os.path.normcase(p), os.path.normcase(root)
    return p == root or p.startswith(root if root.endswith(os.sep) else root + os.sep)


def _tree_stats(p):
    """(archivos, bytes, hay_enlaces) de un archivo o carpeta, sin entrar nunca en un enlace."""
    if _is_link(p):
        return 0, 0, True
    if not os.path.isdir(p):
        return 1, os.path.getsize(p), False
    files = size = 0
    for d, dirs, fs in os.walk(p, followlinks=False):
        if any(_is_link(os.path.join(d, n)) for n in dirs + fs):
            return files, size, True
        for f in fs:
            files += 1
            try:
                size += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return files, size, False


def format_bytes(n):
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


# ---------------------------------------------------------------- texto y archivos

def _oem_codepage():
    try:
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return "cp850"


def decode_output(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(_oem_codepage(), "replace")


def truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} caracteres omitidos ...]\n{text[-tail:]}"


def read_text(path):
    """Devuelve (texto normalizado a \\n, codificación, fin de línea original). Rechaza binarios."""
    size = os.path.getsize(path)
    if size > MAX_READ_BYTES:
        raise ToolError(f"file is {size // 1024} KB; the limit is {MAX_READ_BYTES // 1024} KB (use search or read a line range of a smaller file)")
    with open(path, "rb") as f:
        raw = f.read()
    utf16 = raw.startswith(b"\xff\xfe")     # lo que produce `>` de PowerShell; "utf-16" conserva el BOM al reescribir
    if b"\x00" in raw[:8192] and not utf16:
        raise ToolError("file looks binary, not text")
    enc = "utf-16" if utf16 else "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        if utf16:
            raise ToolError("cannot decode file as text")
        enc = "cp1252"
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            raise ToolError("cannot decode file as text")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), enc, eol


def write_text(path, text, enc="utf-8", eol="\n"):
    data = text.replace("\r\n", "\n")
    if eol == "\r\n":
        data = data.replace("\n", "\r\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".dschat.tmp"
    with open(tmp, "wb") as f:
        f.write(data.encode(enc))
    os.replace(tmp, path)


class ToolBox:
    """Las herramientas de una sesión sobre una carpeta de trabajo.

    confirm(kind, titulo, detalle) -> bool  lo llama el agente antes de escribir, borrar o ejecutar; kind es 'edit', 'delete' o 'command'.
    approval: 'ask' pregunta todo, 'edits' deja editar solo (borrar sigue preguntando), 'all' no pregunta (el filtro de comandos sigue activo).
    """

    def __init__(self, root, confirm=None, approval="ask", backup_dir=None, journal=None):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise ToolError(f"workspace folder does not exist: {root}")
        self.confirm = confirm
        self.approval = approval
        self.backup_dir = backup_dir
        self.journal = journal if journal is not None else []   # [{path, backup|None, when}]
        self._proc = None
        self.extra_path = []        # carpetas que se AGREGAN al final del PATH de los comandos (p. ej. el Python del pendrive)
        self.specs = SPECS

    # ------------------------------------------------------------ caminos

    def resolve(self, rel, must_exist=True):
        rel = (rel or ".").strip().strip('"')
        p = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
        p = os.path.realpath(p)
        if not _inside(p, self.root):
            raise ToolError(f"path '{rel}' is outside the workspace folder ({self.root}). Use a path relative to it, "
                            f"like 'folder/file.txt'; nothing outside it can be read or written")
        parts = os.path.relpath(p, self.root).split(os.sep)
        if parts[0] == ".git" or parts[0] == ".dschat-backup":
            raise ToolError(f"'{parts[0]}' is off-limits")
        if must_exist and not os.path.exists(p):
            raise ToolError(f"'{rel}' does not exist")
        return p

    def rel(self, p):
        return os.path.relpath(p, self.root).replace(os.sep, "/")

    # ------------------------------------------------------------ ejecución

    def execute(self, name, args, cancel=None):
        """Ejecuta una herramienta y devuelve SIEMPRE texto. Los errores vuelven como 'ERROR: ...' para que el modelo se corrija."""
        fn = getattr(self, "tool_" + str(name), None)
        if fn is None or name not in {s["function"]["name"] for s in SPECS}:
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(s['function']['name'] for s in SPECS)}"
        if not isinstance(args, dict):
            return "ERROR: arguments must be a JSON object"
        try:
            if name == "run_command":
                return fn(cancel=cancel, **args)
            return fn(**args)
        except ToolError as e:
            return f"ERROR: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except OSError as e:
            return f"ERROR: {e}"

    def _ask(self, kind, title, detail):
        if self.approval == "all" or (kind == "edit" and self.approval == "edits"):
            return True
        if self.confirm is None:
            return False
        return bool(self.confirm(kind, title, detail))

    # ------------------------------------------------------------ lectura

    def tool_list_dir(self, path=".", depth=1):
        base = self.resolve(path)
        if not os.path.isdir(base):
            raise ToolError(f"'{path}' is not a folder")
        depth = max(1, min(int(depth), 3))
        out, count = [], 0

        def walk(d, level):
            nonlocal count
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError as e:
                out.append("  " * level + f"[cannot read: {e}]")
                return
            for e in entries:
                if count >= MAX_LIST_ENTRIES:
                    return
                count += 1
                isdir = e.is_dir(follow_symlinks=False)
                if isdir and e.name in IGNORE_DIRS:
                    out.append("  " * level + e.name + "/  [skipped]")
                    continue
                if isdir:
                    out.append("  " * level + e.name + "/")
                    if level + 1 < depth:
                        walk(e.path, level + 1)
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    out.append("  " * level + f"{e.name}  ({size} bytes)")
        walk(base, 0)
        head = f"{self.rel(base) if base != self.root else '.'}/"
        tail = f"\n[list truncated at {MAX_LIST_ENTRIES} entries]" if count >= MAX_LIST_ENTRIES else ""
        return head + "\n" + "\n".join(out) + tail if out else head + "\n(empty)"

    def tool_read_file(self, path, offset=1, limit=DEFAULT_READ_LINES):
        p = self.resolve(path)
        if os.path.isdir(p):
            raise ToolError(f"'{path}' is a folder; use list_dir")
        text, _, _ = read_text(p)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        offset = max(1, int(offset))
        limit = max(1, min(int(limit), 2000))
        chunk = lines[offset - 1: offset - 1 + limit]
        body = truncate("\n".join(chunk))
        end = offset - 1 + len(chunk)
        more = f" Use offset={end + 1} to continue." if end < total else ""
        return f"[{self.rel(p)}: lines {offset}-{end} of {total}.{more}]\n{body}"

    def tool_search(self, pattern, path=".", glob=None):
        base = self.resolve(path)
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            raise ToolError(f"invalid regular expression: {e}")
        hits, files = [], 0
        files_iter = [base] if os.path.isfile(base) else self._walk_files(base)
        for fp in files_iter:
            if glob and not fnmatch.fnmatch(os.path.basename(fp), glob):
                continue
            try:
                if os.path.getsize(fp) > MAX_READ_BYTES:
                    continue
                text, _, _ = read_text(fp)
            except (ToolError, OSError):
                continue
            files += 1
            for i, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    hits.append(f"{self.rel(fp)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_MATCHES:
                        return "\n".join(hits) + f"\n[stopped at {MAX_SEARCH_MATCHES} matches; narrow the search]"
        return "\n".join(hits) if hits else f"no matches in {files} text files"

    def _walk_files(self, base):
        for d, dirs, fs in os.walk(base):
            dirs[:] = sorted(x for x in dirs if x not in IGNORE_DIRS)
            for f in sorted(fs):
                yield os.path.join(d, f)

    # ------------------------------------------------------------ escritura

    def _free_name(self, tail):
        """Ruta nueva en la carpeta de copias: fecha y hora + tail, con -2, -3... si ya existe. Sin esto, dos copias del
        mismo archivo en el mismo segundo (deshacer y volver a cambiar) se pisaban, y se perdía la anterior."""
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{tail}"
        dst, n = os.path.join(self.backup_dir, stem), 2
        while os.path.lexists(dst):
            dst, n = os.path.join(self.backup_dir, f"{stem}-{n}"), n + 1
        return dst

    def _backup(self, p):
        """Copia el archivo antes de tocarlo. Devuelve la ruta de la copia (None si el archivo es nuevo)."""
        if not os.path.exists(p):
            return None
        if not self.backup_dir:
            raise ToolError("no backup folder configured; refusing to overwrite without a way to undo")
        os.makedirs(self.backup_dir, exist_ok=True)
        dst = self._free_name(f"{len(self.journal):03d}-{self.rel(p).replace('/', '__')}")
        shutil.copy2(p, dst)
        if os.path.getsize(dst) != os.path.getsize(p):
            raise ToolError("backup verification failed; file not modified")
        return dst

    def _commit_change(self, p, new_text, enc, eol, title, old_text):
        diff = "".join(difflib.unified_diff(
            (old_text or "").splitlines(True), new_text.splitlines(True),
            fromfile=self.rel(p) if old_text is not None else "(nuevo)", tofile=self.rel(p), n=2))
        if not self._ask("edit", title, diff or "(sin diferencias)"):
            return "DENIED: the user did not allow this change. Do not retry the same change; ask the user what they want."
        backup = self._backup(p)
        write_text(p, new_text, enc, eol)
        self.journal.append({"path": p, "backup": backup, "when": time.strftime("%Y-%m-%d %H:%M:%S")})
        return None

    def tool_write_file(self, path, content):
        p = self.resolve(path, must_exist=False)
        if os.path.isdir(p):
            raise ToolError(f"'{path}' is a folder")
        if len(content.encode("utf-8")) > MAX_READ_BYTES * 4:
            raise ToolError("content too large")
        if os.path.exists(p):
            old, enc, eol = read_text(p)
        else:
            old, enc, eol = None, "utf-8", "\n"
        denied = self._commit_change(p, content, enc, eol, f"{'Sobrescribir' if old is not None else 'Crear'} {self.rel(p)}", old)
        if denied:
            return denied
        return f"OK: {'overwrote' if old is not None else 'created'} {self.rel(p)} ({len(content)} chars)"

    def tool_edit_file(self, path, old, new, replace_all=False):
        p = self.resolve(path)
        if os.path.isdir(p):
            raise ToolError(f"'{path}' is a folder")
        if not old:
            raise ToolError("'old' must not be empty (use write_file to create a file)")
        text, enc, eol = read_text(p)
        old_n, new_n = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        n = text.count(old_n)
        if n == 0:
            raise ToolError("'old' text not found. Read the file again and copy the text exactly, including indentation")
        if n > 1 and not replace_all:
            raise ToolError(f"'old' appears {n} times; add surrounding lines to make it unique, or pass replace_all=true")
        updated = text.replace(old_n, new_n) if replace_all else text.replace(old_n, new_n, 1)
        denied = self._commit_change(p, updated, enc, eol, f"Editar {self.rel(p)}", text)
        if denied:
            return denied
        return f"OK: edited {self.rel(p)} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})"

    # ------------------------------------------------------------ borrado

    def tool_delete_path(self, path):
        raw = (path or "").strip().strip('"')
        raw = raw if os.path.isabs(raw) else os.path.join(self.root, raw)
        if _is_link(raw):
            raise ToolError(f"'{path}' is a symlink or junction; it is not deleted from here (the user can remove it by hand)")
        p = self.resolve(path)
        if os.path.normcase(p) == os.path.normcase(self.root):
            raise ToolError("refusing to delete the workspace folder itself")
        if not self.backup_dir:
            raise ToolError("no backup folder configured; refusing to delete without a way to undo")
        files, size, links = _tree_stats(p)
        if links:
            raise ToolError(f"'{path}' contains a symlink or junction; it is not deleted from here, because following it could reach "
                            "outside the workspace. Tell the user so they can deal with it by hand")
        if size > MAX_DELETE_BYTES:
            raise ToolError(f"'{path}' is too big to keep a restorable copy ({size // 2**20} MB, limit {MAX_DELETE_BYTES // 2**20} MB)")
        isdir = os.path.isdir(p)
        detail = (f"{self.rel(p)}\n{'Carpeta' if isdir else 'Archivo'}: {files} archivo{'s' if files != 1 else ''}, {format_bytes(size)}.\n"
                  "Se guarda una copia: «Deshacer cambio» lo restaura.")
        if not self._ask("delete", f"Borrar {self.rel(p)}", detail):
            return "DENIED: the user did not allow this deletion. Do not retry it; ask the user what they want."
        os.makedirs(self.backup_dir, exist_ok=True)
        dst = self._free_name(f"{len(self.journal):03d}-borrado-{self.rel(p).replace('/', '__')}")
        try:
            shutil.move(p, dst)
        except OSError:
            if os.path.exists(p) and os.path.exists(dst):      # la copia quedó a medias y el original sigue entero: se descarta la copia
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    os.remove(dst)
            raise
        if os.path.exists(p) or _tree_stats(dst)[:2] != (files, size):
            if not os.path.exists(p):
                shutil.move(dst, p)
            raise ToolError("the copy kept for undo does not match what was deleted; nothing was removed")
        self.journal.append({"path": p, "backup": dst, "deleted": True, "when": time.strftime("%Y-%m-%d %H:%M:%S")})
        return f"OK: deleted {self.rel(p)} ({files} file{'s' if files != 1 else ''}, {format_bytes(size)}); the user can restore it with undo"

    def _undo_delete(self, e):
        p, b = e["path"], e["backup"]
        if not os.path.exists(b):
            self.journal.append(e)
            return f"No se puede deshacer: falta la copia {b}"
        if os.path.lexists(p):
            self.journal.append(e)
            return f"No se puede restaurar {self.rel(p)}: ya hay algo con ese nombre. Movelo o renombralo y volvé a intentar."
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.isdir(b):
            shutil.copytree(b, p, symlinks=True)
        else:
            shutil.copy2(b, p)
        return f"Restaurado {self.rel(p)} (lo había borrado el agente)."

    # ------------------------------------------------------------ comandos

    def tool_run_command(self, command, timeout=DEFAULT_TIMEOUT, cancel=None):
        why = check_command(command)
        if why:
            hint = (" To delete a file or folder inside the workspace use the delete_path tool: it asks the user and keeps a copy they can restore."
                    if why.startswith("borrar") else "")
            return (f"BLOCKED by the safety filter ({why}). This command is never run automatically. "
                    "Tell the user what you wanted to do so they can do it themselves. If a harmless word in a file name or text "
                    "triggered it, rephrase: for a commit message, write it to a file and use `git commit -F file`." + hint)
        pisa = self._overwritable(command)
        detalle = command + ("\n\nSobrescribe (se guarda una copia antes; «Deshacer cambio» la restaura): "
                             + ", ".join(self.rel(p) for p in pisa) if pisa else "")
        if not self._ask("command", "Ejecutar comando", detalle):
            return "DENIED: the user did not allow this command. Do not retry it; ask the user what they want."
        copias = [(p, self._backup(p)) for p in pisa]
        try:
            result = self._run(command, timeout, cancel)
        finally:
            guardadas = self._keep_changed(copias)
        if guardadas:
            result += ("\n[CodeAgent kept a copy of what this command overwrote: " + ", ".join(guardadas)
                       + ". The user can restore it with undo.]")
        return result

    def _overwritable(self, command):
        """Archivos existentes dentro de la carpeta de trabajo que el comando puede pisar (ver overwrite_targets)."""
        res = []
        for t in overwrite_targets(command, self.root):
            try:
                p = self.resolve(t)
            except ToolError:
                continue                     # fuera de la carpeta, inexistente o prohibido: nada que respaldar desde acá
            if os.path.isfile(p) and not _is_link(p) and os.path.getsize(p) <= MAX_DELETE_BYTES:
                res.append(p)
        return res

    def _keep_changed(self, copias):
        """Tras el comando: al diario van solo los archivos que de verdad cambiaron; las copias de los intactos se borran."""
        guardadas = []
        for p, b in copias:
            if os.path.isfile(p) and filecmp.cmp(p, b, shallow=False):
                os.remove(b)
                continue
            self.journal.append({"path": p, "backup": b, "when": time.strftime("%Y-%m-%d %H:%M:%S")})
            guardadas.append(self.rel(p))
        return guardadas

    def _run(self, command, timeout, cancel):
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Los programas de Python y Node escriben UTF-8 si se les pide; los comandos propios de cmd (dir, type)
        # escriben en la página OEM. Se intenta UTF-8 estricto y, si no es válido, OEM.
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env["PATH"] = cmd_path(env.get("PATH", ""))
        if self.extra_path:
            # al final: si la máquina ya tiene su propio Python o Node, ese gana; los del pendrive son el respaldo
            env["PATH"] = os.pathsep.join([env.get("PATH", "")] + [p for p in self.extra_path if os.path.isdir(p)])
        try:
            proc = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as e:
            raise ToolError(f"could not start the command: {e}")
        self._proc = proc
        chunks = []

        def pump():
            for raw in iter(lambda: proc.stdout.read(4096), b""):
                chunks.append(raw)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        started, killed = time.time(), None
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                killed = "cancelled by the user"
                break
            if time.time() - started > timeout:
                killed = f"timed out after {timeout}s"
                break
            time.sleep(0.05)
        if killed:
            self._kill(proc)
        t.join(2)
        self._proc = None
        out = decode_output(b"".join(chunks)).replace("\r\n", "\n")
        status = f"[killed: {killed}]" if killed else f"[exit code {exit_code_text(proc.returncode)}]"
        if not killed and _PIPE_MASKS_EXIT.search(command):
            status += (" (WARNING: the command has a pipe, so this is the exit code of its LAST part, not of the program you ran; "
                       "a failure may be hidden. Run it again without the pipe to know the real result.)")
        return f"{status}\n{truncate(out.strip())}" if out.strip() else status

    @staticmethod
    def _kill(proc):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------ deshacer

    def undo_last(self):
        """Revierte el último cambio del diario. Devuelve un texto para mostrar."""
        if not self.journal:
            return "No hay cambios para deshacer."
        e = self.journal.pop()
        if e.get("deleted"):
            return self._undo_delete(e)
        p = e["path"]
        if e["backup"]:
            if not os.path.exists(e["backup"]):
                self.journal.append(e)
                return f"No se puede deshacer: falta la copia {e['backup']}"
            saved = self._save_before_undo(p)
            shutil.copy2(e["backup"], p)
            return f"Restaurado {self.rel(p)} a su versión anterior." + (f" Lo que había antes de deshacer quedó en {saved}." if saved else "")
        if os.path.exists(p):
            saved = self._save_before_undo(p)
            os.remove(p)
            return f"Se eliminó {self.rel(p)} (lo había creado el agente)." + (f" Su contenido quedó en {saved}." if saved else "")
        return f"{self.rel(p)} ya no existe; no hay nada que deshacer."

    def _save_before_undo(self, p):
        """Si el usuario editó el archivo a mano después del cambio del agente, deshacer lo pisaría: se guarda antes."""
        if not (self.backup_dir and os.path.isfile(p)):
            return None
        os.makedirs(self.backup_dir, exist_ok=True)
        dst = self._free_name(f"antes-de-deshacer-{self.rel(p).replace('/', '__')}")
        shutil.copy2(p, dst)
        return dst


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


SPECS = [
    _fn("list_dir", "List files and folders in a folder of the workspace. Paths are relative to the workspace root.",
        {"path": {"type": "string", "description": "Folder, default '.'"},
         "depth": {"type": "integer", "description": "Levels to show, 1-3, default 1"}}, []),
    _fn("read_file", "Read a text file. Returns up to 400 lines from 'offset'; use offset/limit for long files.",
        {"path": {"type": "string"}, "offset": {"type": "integer", "description": "First line, default 1"},
         "limit": {"type": "integer", "description": "Max lines, default 400"}}, ["path"]),
    _fn("search", "Search a regular expression (case-insensitive) in the text files under a path. Returns path:line: text.",
        {"pattern": {"type": "string"}, "path": {"type": "string", "description": "File or folder, default '.'"},
         "glob": {"type": "string", "description": "Only file names matching this, e.g. '*.py'"}}, ["pattern"]),
    _fn("edit_file", "Replace exact text in an existing file. 'old' must match exactly once (or pass replace_all). Read the file first.",
        {"path": {"type": "string"}, "old": {"type": "string", "description": "Exact text to replace"},
         "new": {"type": "string", "description": "Replacement text"},
         "replace_all": {"type": "boolean", "description": "Replace every occurrence"}}, ["path", "old", "new"]),
    _fn("write_file", "Create a new file or fully overwrite one, with its whole content in one call (any language, any "
        "quotes). Prefer edit_file for small changes to existing files.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("delete_path", "Delete a file or a folder (with everything in it) inside the workspace. The user is asked first and can undo it. "
        "Only delete what the user asked you to delete. Symlinks and junctions are never deleted.",
        {"path": {"type": "string", "description": "File or folder to delete"}}, ["path"]),
    _fn("run_command", "Run a command in Windows cmd.exe (not bash: no cat, ls, rm, grep) in the workspace root, e.g. tests "
        "or a build. No interactive programs. Not for writing files: use write_file. Destructive commands are blocked "
        "(use delete_path to delete files).",
        {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 60, max 600"}}, ["command"]),
]


def parse_args(raw):
    """Los argumentos de una llamada llegan como texto JSON; puede venir vacío, cortado o mal formado."""
    if raw is None or not str(raw).strip():
        return {}
    try:
        v = json.loads(raw)
    except ValueError as e:
        raise ToolError(f"arguments are not valid JSON ({e}); resend the call with valid JSON")
    if not isinstance(v, dict):
        raise ToolError("arguments must be a JSON object")
    return v
