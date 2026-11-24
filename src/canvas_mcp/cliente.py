"""Cliente mínimo de la API REST de Canvas LMS, solo con la biblioteca estándar.

Configuración por variables de entorno:
  CANVAS_BASE_URL    p. ej. https://miuni.instructure.com (obligatoria)
  CANVAS_TOKEN       token de acceso personal de Canvas (o bien CANVAS_TOKEN_FILE)
  CANVAS_TOKEN_FILE  ruta a un fichero que contiene solo el token
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ErrorConfig(Exception):
    """Falta CANVAS_BASE_URL o el token."""


def base_url() -> str:
    b = os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/")
    if not b:
        raise ErrorConfig("falta la variable CANVAS_BASE_URL (p. ej. https://miuni.instructure.com)")
    if not b.startswith(("http://", "https://")):
        b = "https://" + b
    return b


def token() -> str:
    """Se lee en cada petición: si se renueva el token no hace falta reiniciar el servidor."""
    t = os.environ.get("CANVAS_TOKEN", "").strip()
    if not t and os.environ.get("CANVAS_TOKEN_FILE"):
        try:
            with open(os.path.expanduser(os.environ["CANVAS_TOKEN_FILE"]), encoding="utf-8") as f:
                t = f.read().strip()
        except OSError as e:
            raise ErrorConfig(f"no se puede leer CANVAS_TOKEN_FILE ({e.strerror})") from None
    if not t:
        raise ErrorConfig("falta el token: define CANVAS_TOKEN o CANVAS_TOKEN_FILE")
    return t


class _SinAuthFuera(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones, pero no reenvía el token a otro host (Canvas manda los ficheros a S3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        nuevo = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nuevo is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            nuevo.headers.pop("Authorization", None)
            nuevo.unredirected_hdrs.pop("Authorization", None)
        return nuevo


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_abrir = urllib.request.build_opener(_SinAuthFuera).open
_abrir_sin_redir = urllib.request.build_opener(_SinRedireccion).open


def _url(path: str) -> str:
    return path if path.startswith("http") else f"{base_url()}/api/v1/{path.lstrip('/')}"


def _siguiente(link: str | None) -> str | None:
    """URL rel="next" de la cabecera Link de Canvas."""
    for parte in (link or "").split(","):
        if 'rel="next"' in parte:
            m = re.search(r"<([^>]+)>", parte)
            if m:
                return m.group(1)
    return None


def _cabeceras(url: str) -> dict:
    # Solo se manda el token a la propia instancia de Canvas.
    if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(base_url()).netloc:
        return {"Authorization": f"Bearer {token()}", "Accept": "application/json"}
    return {"Accept": "application/json"}


def get(path: str, paginate: bool = True, timeout: float = 25):
    """GET a /api/v1/<path>. Devuelve list o dict; sigue la paginación. Error -> dict con _error."""
    url = _url(path)
    if "per_page=" not in url:
        url += ("&" if "?" in url else "?") + "per_page=100"
    out: list = []
    while url:
        req = urllib.request.Request(url, headers=_cabeceras(url))
        try:
            with _abrir(req, timeout=timeout) as r:
                cuerpo, link = r.read().decode("utf-8", "replace"), r.headers.get("Link")
        except urllib.error.HTTPError as e:
            cuerpo, link = e.read().decode("utf-8", "replace"), None
        except (urllib.error.URLError, OSError) as e:
            return {"_error": type(getattr(e, "reason", e)).__name__}
        try:
            d = json.loads(cuerpo)
        except ValueError:
            return {"_error": cuerpo[:200] or "respuesta no JSON"}
        if isinstance(d, dict):
            return d
        out.extend(d)
        url = _siguiente(link) if paginate else None
    return out


def send(method: str, path: str, data: list[tuple[str, str]], timeout: float = 60):
    """POST/PUT form-urlencoded a /api/v1/<path>. Devuelve (status, json | dict con _error)."""
    url = _url(path)
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers=_cabeceras(url), method=method)
    try:
        with _abrir(req, timeout=timeout) as r:
            cuerpo, st = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        cuerpo, st = e.read().decode("utf-8", "replace"), e.code
    except (urllib.error.URLError, OSError) as e:
        return 0, {"_error": type(getattr(e, "reason", e)).__name__}
    try:
        return st, json.loads(cuerpo)
    except ValueError:
        return st, {"_error": cuerpo[:200]}


def download(url: str, dest: str, timeout: float = 120) -> bool:
    """Descarga un fichero (sigue la redirección a S3 sin reenviar el token)."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    try:
        with _abrir(urllib.request.Request(url, headers=_cabeceras(url)), timeout=timeout) as r, \
                open(tmp, "wb") as f:
            while True:
                bloque = r.read(1 << 16)
                if not bloque:
                    break
                f.write(bloque)
        os.replace(tmp, dest)
    except (urllib.error.URLError, OSError):
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    return os.path.getsize(dest) > 0


def multipart(campos: dict, ruta: str) -> tuple[bytes, str]:
    """Cuerpo multipart/form-data con los campos de Canvas y el fichero al final (Canvas lo exige así)."""
    limite = uuid.uuid4().hex
    nombre = os.path.basename(ruta)
    mime = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
    partes = []
    for k, v in campos.items():
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    with open(ruta, "rb") as f:
        contenido = f.read()
    partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="file"; filename="{nombre}"\r\n'
                  f"Content-Type: {mime}\r\n\r\n".encode() + contenido + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={limite}"


def upload(slot: dict, ruta: str, timeout: float = 900):
    """Paso 2 de la subida de ficheros: envía el fichero a upload_url y confirma.

    Devuelve el JSON del fichero creado (con id) o un dict con _error."""
    cuerpo, tipo = multipart(slot.get("upload_params") or {}, ruta)
    url = slot["upload_url"]
    req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": tipo}, method="POST")
    try:
        with _abrir_sin_redir(req, timeout=timeout) as r:
            texto, st, loc = r.read().decode("utf-8", "replace"), r.status, r.headers.get("Location")
    except urllib.error.HTTPError as e:
        texto, st, loc = e.read().decode("utf-8", "replace"), e.code, e.headers.get("Location")
    except (urllib.error.URLError, OSError) as e:
        return {"_error": type(getattr(e, "reason", e)).__name__}
    if 300 <= st < 400 and loc:
        return get(loc, paginate=False)  # confirmación de la subida (flujo con redirección)
    try:
        d = json.loads(texto)
    except ValueError:
        return {"_error": f"HTTP {st}"}
    if isinstance(d, dict) and d.get("location") and not d.get("id"):
        return get(d["location"], paginate=False)
    return d
