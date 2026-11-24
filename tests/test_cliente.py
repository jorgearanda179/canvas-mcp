"""Pruebas del cliente HTTP contra un servidor local (sin salir a internet)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from canvas_mcp import cliente
from canvas_mcp import server as S

VISTO = []  # (host, ruta, Authorization)


class Falso(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, d, extra=None):
        cuerpo = json.dumps(d).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        host = self.headers.get("Host")
        VISTO.append((host, self.path, self.headers.get("Authorization")))
        if self.path.startswith("/api/v1/courses?"):
            if "page=2" in self.path:
                return self._json([{"id": 2, "course_code": "FIS-102-A", "name": "Fisica"}])
            sig = f"<http://{host}/api/v1/courses?page=2&per_page=100>; rel=\"next\""
            return self._json([{"id": 1, "course_code": "MAT-101-A", "name": "Matematicas"}], {"Link": sig})
        if self.path.startswith("/files/1/download"):
            # Canvas manda el fichero a otro host (S3): el token no debe viajar allí
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{OTRO.server_port}/s3/fichero")
            self.end_headers()
            return
        if self.path == "/s3/fichero":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"contenido")
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"errors":[{"message":"not found"}],"status":"not_found"}')


def _arranca():
    s = HTTPServer(("127.0.0.1", 0), Falso)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


SRV, OTRO = _arranca(), _arranca()


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    VISTO.clear()
    S._cache.clear()
    monkeypatch.setenv("CANVAS_BASE_URL", f"http://localhost:{SRV.server_port}")
    monkeypatch.setenv("CANVAS_TOKEN", "token-de-prueba")
    monkeypatch.delenv("CANVAS_COURSES", raising=False)
    monkeypatch.delenv("CANVAS_CODIGO_REGEX", raising=False)


def test_paginacion_y_token():
    d = cliente.get("courses?enrollment_state=active")
    assert [c["id"] for c in d] == [1, 2]
    assert all(a == "Bearer token-de-prueba" for _, _, a in VISTO)


def test_error_404_devuelve_dict():
    d = cliente.get("no/existe")
    assert S._is_err(d) and S._errtxt(d) == "not_found"


def test_descarga_no_reenvia_token_a_otro_host(tmp_path):
    dest = tmp_path / "f.pdf"
    assert cliente.download(f"http://localhost:{SRV.server_port}/files/1/download", str(dest))
    assert dest.read_bytes() == b"contenido"
    auth = {ruta: a for _, ruta, a in VISTO}
    assert auth["/files/1/download"] == "Bearer token-de-prueba"
    assert auth["/s3/fichero"] is None


def test_falta_configuracion(monkeypatch):
    monkeypatch.delenv("CANVAS_TOKEN")
    monkeypatch.delenv("CANVAS_TOKEN_FILE", raising=False)
    assert "falta el token" in S._api("courses")["_error"]
    assert S.cursos().startswith("Error: no se pudieron leer los cursos de Canvas (configuración: falta el token")
    monkeypatch.delenv("CANVAS_BASE_URL")
    with pytest.raises(cliente.ErrorConfig):
        cliente.base_url()


def test_token_desde_fichero(monkeypatch, tmp_path):
    f = tmp_path / "token.txt"
    f.write_text("  del-fichero\n")
    monkeypatch.delenv("CANVAS_TOKEN")
    monkeypatch.setenv("CANVAS_TOKEN_FILE", str(f))
    assert cliente.token() == "del-fichero"


def test_cursos_filtro_y_codigo_corto(monkeypatch):
    assert "MAT-101-A [1] MAT-101-A Matematicas" in S.cursos()
    S._cache.clear()
    monkeypatch.setenv("CANVAS_CODIGO_REGEX", r"^([A-Z]+-\d+)")
    monkeypatch.setenv("CANVAS_COURSES", "FIS-102")
    assert S.cursos() == "FIS-102 [2] FIS-102-A Fisica"


def test_multipart(tmp_path):
    f = tmp_path / "memoria.pdf"
    f.write_bytes(b"%PDF-1.4")
    cuerpo, tipo = cliente.multipart({"key": "a/b", "acl": "private"}, str(f))
    limite = tipo.split("boundary=")[1]
    assert cuerpo.index(b'name="key"') < cuerpo.index(b'name="file"; filename="memoria.pdf"')
    assert b"Content-Type: application/pdf" in cuerpo and cuerpo.endswith(f"--{limite}--\r\n".encode())
