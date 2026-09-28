"""OCR de imágenes con Tesseract, para los adjuntos que llegan desde el celular (MovilDeep).

DeepSeek no recibe imágenes: se le pasa el texto que tengan. Se llama a tesseract.exe directo con subprocess (sin
pytesseract: el runtime portable no tiene site-packages), en español e inglés, con un tope de tiempo y sin abrir consola.

Dónde se busca tesseract.exe, en orden:
  1. bin\\tesseract\\ de la carpeta portable (lo copia build_portable.py);
  2. las instalaciones conocidas: %ProgramFiles%\\Tesseract-OCR y la de esta máquina (E:\\Archivos de programa\\Tesseract),
     cada una solo si el archivo existe de verdad.
"""
import os
import subprocess

LANGS = "spa+eng"
TIMEOUT = 60
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif")
CREATE_NO_WINDOW = 0x08000000


class OcrUnavailable(Exception):
    pass


class OcrTimeout(Exception):
    pass


class OcrError(Exception):
    pass


def known_installs():
    """Carpetas de instalaciones conocidas de Tesseract (existan o no; se filtra después)."""
    out = []
    pf = os.environ.get("ProgramFiles")
    if pf:
        out.append(os.path.join(pf, "Tesseract-OCR"))
    out.append(r"E:\Archivos de programa\Tesseract")
    return out


def find_tesseract(portable_root=None, extra=()):
    """Ruta de tesseract.exe o None. portable_root: la raíz de la carpeta portable (o None si no es portable)."""
    dirs = []
    if portable_root:
        dirs.append(os.path.join(portable_root, "bin", "tesseract"))
    dirs.extend(extra)
    dirs.extend(known_installs())
    for d in dirs:
        exe = os.path.join(d, "tesseract.exe")
        if os.path.isfile(exe) and os.path.isdir(os.path.join(d, "tessdata")):
            return exe
    return None


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def run(exe, image_path, timeout=TIMEOUT):
    """Texto de la imagen (puede ser vacío). OcrTimeout si tarda más de timeout; OcrError si Tesseract falla."""
    if not exe or not os.path.isfile(exe):
        raise OcrUnavailable("OCR no disponible en esta PC")
    tessdata = os.path.join(os.path.dirname(exe), "tessdata")
    cmd = [exe, image_path, "stdout", "-l", LANGS, "--tessdata-dir", tessdata]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise OcrTimeout("OCR: tiempo agotado") from None
    except OSError as e:
        raise OcrError(f"OCR: no se pudo ejecutar Tesseract ({e})") from None
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OcrError("OCR: Tesseract falló" + (f" ({detail[-1]})" if detail else f" (código {r.returncode})"))
    return r.stdout.decode("utf-8", "replace").replace("\r\n", "\n").strip()
