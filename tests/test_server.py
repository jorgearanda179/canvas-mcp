"""Pruebas sin red: api() falso, cursos fijos y _send espiado (nunca se envia nada)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from canvas_mcp import server as S  # noqa: E402


def iso(dias, hora=21):
    d = datetime.now(timezone.utc).replace(hour=hora, minute=59, second=59, microsecond=0) + timedelta(days=dias)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


CURSOS = {1001: {"cod": "MAT-101", "code": "MAT-101-A", "nombre": "Matematicas I"},
          1002: {"cod": "FIS-102", "code": "FIS-102-A", "nombre": "Fisica General"}}

ASG_MAT = [
    {"id": 1, "name": "Numpy", "due_at": iso(-20), "points_possible": 10, "submission_types": ["online_upload"],
     "submission": {"workflow_state": "graded", "score": 9.5}, "html_url": "u1"},
    {"id": 2, "name": "  Regresion  lineal ", "due_at": iso(1), "points_possible": 10,
     "submission_types": ["online_upload"], "allowed_extensions": ["ipynb"],
     "description": '<p>Haz la <b>practica</b>&nbsp;y</p><p><a href="/courses/1001/files/555/download">enun.pdf</a></p>'
                    '<ul><li>uno</li></ul><a href="https://ej.com/x">web</a>',
     "rubric": [{"id": "c1", "description": "Codigo", "points": 6}, {"id": "c2", "description": "Memoria", "points": 4}],
     "submission": {"workflow_state": "unsubmitted"}, "html_url": "u2"},
    {"id": 3, "name": "Lejana", "due_at": iso(60), "points_possible": 10, "submission_types": ["online_upload"],
     "submission": {"workflow_state": "unsubmitted"}},
]
ASG_FIS = [
    {"id": 10, "name": "Parking", "due_at": iso(-30), "lock_at": iso(-30), "points_possible": 10,
     "submission_types": ["online_upload"], "submission": {"workflow_state": "unsubmitted", "missing": True}},
    {"id": 11, "name": "API grupo", "due_at": iso(3), "points_possible": 100, "submission_types": ["online_url"],
     "group_category_id": 77, "submission": {"workflow_state": "submitted", "attempt": 1}},
]

RUTAS = {
    "courses/1001/assignments?include[]=submission&order_by=due_at": ASG_MAT,
    "courses/1002/assignments?include[]=submission&order_by=due_at": ASG_FIS,
    "files/555": {"id": 555, "display_name": "enun.pdf", "size": 2048, "url": "https://x/dl", "locked_for_user": False},
    "files/666": {"id": 666, "display_name": "bloq.pdf", "size": 10, "locked_for_user": True},
    "users/self/groups": [{"id": 9, "name": "Equipo 3", "group_category_id": 77, "course_id": 1002}],
    "groups/9/users": [{"short_name": "Ana"}, {"short_name": "Luis"}],
    "courses/1001/assignments/2/submissions/self?include[]=submission_comments&include[]=rubric_assessment": {
        "workflow_state": "graded", "score": 8, "late": True, "submitted_at": iso(-1), "attempt": 2,
        "graded_at": iso(0), "attachments": [{"id": 42, "display_name": "r.ipynb", "size": 5000}],
        "submission_comments": [{"author_name": "Prof. Garcia", "created_at": iso(0), "comment": "Bien, revisa la memoria"}],
        "rubric_assessment": {"c1": {"points": 5, "comments": "ok"}, "c2": {"points": 3}}},
    "courses?enrollment_state=active&include[]=total_scores": [
        {"id": 1001, "course_code": "MAT-101-A",
         "enrollments": [{"type": "student", "computed_current_score": 95.0, "computed_final_score": 60.0}]},
        {"id": 1002, "course_code": "FIS-102-A", "enrollments": [{"type": "student"}]}],
    "courses/1001/modules?include[]=items&include[]=content_details": [
        {"name": "Tema 1", "state": "completed", "items": [
            {"type": "File", "content_id": 555, "title": "PCA-24.pdf", "content_details": {}},
            {"type": "ExternalUrl", "title": "Colab", "external_url": "https://colab"}]},
        {"name": "Tema 9", "state": "locked", "items": [
            {"type": "File", "content_id": 666, "title": "Final.pdf", "content_details": {"locked_for_user": True}}]}],
    "courses/1002/modules?include[]=items&include[]=content_details": {"status": "unauthorized", "errors": [{}]},
    "courses/1001/discussion_topics": [{"id": 5, "title": "Dudas", "posted_at": iso(-2), "discussion_subentry_count": 2,
                                         "unread_count": 1}],
    "courses/1002/discussion_topics": [],
    "courses/1001/discussion_topics/5/view": {"participants": [{"id": 1, "display_name": "Ana"}],
                                               "view": [{"user_id": 1, "created_at": iso(-1), "message": "<p>Hola</p>",
                                                         "replies": [{"user_id": 1, "message": "re"}]}]},
    "conversations?scope=inbox&per_page=10": [{"id": 7, "subject": "Aviso", "workflow_state": "unread",
                                               "last_message_at": iso(-1), "last_message": "No hay clase",
                                               "participants": [{"name": "Prof"}]}],
    "conversations/7?auto_mark_as_read=false": {"subject": "Aviso", "participants": [{"id": 3, "name": "Prof"}],
                                                "messages": [{"author_id": 3, "created_at": iso(-1), "body": "No hay clase"}]},
}
LLAMADAS = []


def fake_api(path, paginate=True):
    LLAMADAS.append(path)
    if path.startswith("announcements?"):
        return [{"id": 100, "title": "Cambio de aula", "context_code": "course_1001", "posted_at": iso(-1),
                 "user_name": "Prof. Garcia", "message": "<p>Manana en el <b>aula 17</b></p>"}]
    if path.startswith("calendar_events?"):
        return [{"title": "Examen", "context_code": "course_1002", "start_at": iso(4, 15), "end_at": iso(4, 17)}]
    return RUTAS.get(path, {"errors": [{"message": "not found"}], "status": "not_found"})


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    S._cache.clear()
    LLAMADAS.clear()
    monkeypatch.setattr(S, "_api", fake_api)
    monkeypatch.setattr(S, "_cargar_cursos", lambda: dict(CURSOS))
    enviados = []
    monkeypatch.setattr(S, "_send", lambda *a: enviados.append(a) or (200, {"workflow_state": "submitted"}))
    monkeypatch.setattr(S, "_subir", lambda *a: enviados.append(("SUBIR",) + a) or (99, None))

    def fake_download(url, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(b"x" * 2048)
        return True
    monkeypatch.setattr(S, "_download", fake_download)
    monkeypatch.setattr(S, "DESCARGAS", str(tmp_path / "descargas"))
    return enviados


def test_cursos():
    out = S.cursos()
    assert "MAT-101 [1001] MAT-101-A Matematicas I" in out
    assert len(out.splitlines()) == 2


def test_tareas_pendientes():
    out = S.tareas()
    assert "Regresion lineal" in out and "Numpy" not in out and "Lejana" not in out
    assert "[10] Parking" in out and "missing" in out and "cerrada" in out
    assert "(manana)" in out
    assert "API grupo" not in out  # ya entregada


def test_tareas_filtros():
    out = S.tareas(asignatura="matematicas", solo_pendientes=False, dias=90)
    assert "Numpy" in out and "nota 9.5/10" in out and "Lejana" in out and "Parking" not in out


def test_un_curso_falla_no_tumba(monkeypatch):
    def api(path, paginate=True):
        return {"_error": "timeout 25s"} if "1002" in path else fake_api(path)
    monkeypatch.setattr(S, "_api", api)
    out = S.tareas()
    assert "Regresion" in out and "FIS-102: sin datos (timeout 25s)" in out


def test_cache_evita_peticiones():
    S.tareas()
    n = len(LLAMADAS)
    S.tareas(asignatura="MAT-101")
    S.notas()
    assert len(LLAMADAS) == n + 1  # solo la peticion de totales de notas


def test_tarea_detalle():
    out = S.tarea(tarea_id=2)
    assert "Haz la practica y" in out and "- uno" in out and "web (https://ej.com/x)" in out
    assert "enun.pdf [fichero 555, 2 KB]" in out
    assert "Codigo (6 pts)" in out and "ext ipynb" in out and "grupo" not in out
    assert "<p>" not in out


def test_tarea_grupo():
    out = S.tarea(tarea_id=11)
    assert "grupo Equipo 3 [9]: Ana, Luis" in out


def test_tarea_inexistente():
    assert S.tarea(tarea_id=12345).startswith("Error: Tarea 12345 no encontrada")


def test_mi_entrega():
    out = S.mi_entrega(tarea_id=2)
    assert "graded late" in out and "nota 8/10" in out and "intento 2" in out
    assert "[42] r.ipynb" in out and "Prof. Garcia: Bien, revisa la memoria" in out
    assert "Codigo: 5/6 — ok" in out


def test_notas():
    out = S.notas()
    assert "MAT-101 Matematicas I · Canvas: actual 95% · final 60%" in out
    assert "Numpy: 9.5/10" in out and "(sin notas publicadas)" in out


def test_anuncios():
    out = S.anuncios(dias=3)
    assert "MAT-101 Cambio de aula (Prof. Garcia): Manana en el aula 17" in out
    assert any("context_codes[]=course_1001&context_codes[]=course_1002" in p for p in LLAMADAS)


def test_material_y_busqueda():
    out = S.material(asignatura="MAT-101")
    assert "# Tema 9 (BLOQUEADO)" in out and "fichero [666] Final.pdf (bloqueado)" in out
    assert "url Colab https://colab" in out
    out = S.material(buscar="pca")
    assert "MAT-101 · Tema 1 › fichero [555] PCA-24.pdf" in out and "FIS-102: sin acceso a modulos" in out
    assert S.material().startswith("Error")


def test_descargar(tmp_path):
    out = S.descargar(fichero_id=555)
    assert out.startswith("OK ") and os.path.join("descargas", "enun.pdf") in out
    assert S.descargar(fichero_id=555).startswith("Ya estaba")
    assert S.descargar(fichero_id=666).startswith("Bloqueado")
    otra = tmp_path / "otra"
    otra.mkdir()
    assert S.descargar(fichero_id=555, destino=str(otra)).startswith(f"OK {otra / 'enun.pdf'}")


def test_foros():
    assert "[5] Dudas · 2 resp (1 sin leer)" in S.foros()
    out = S.foros(tema_id=5)
    assert "Ana: Hola" in out and "  - " in out


def test_calendario_grupos_mensajes():
    assert "FIS-102 Examen" in S.calendario()
    assert "Equipo 3 [9] cat 77: Ana, Luis" in S.grupos()
    assert "[7] NO LEIDO Aviso" in S.mensajes()
    assert "Prof: No hay clase" in S.mensajes(conversacion_id=7)


def test_entregar_dry_run_por_defecto_no_envia(entorno, tmp_path):
    f = tmp_path / "r.ipynb"
    f.write_text("{}")
    out = S.entregar(tarea_id=2, ficheros=[str(f)], comentario="hola")
    assert out.startswith("DRY-RUN") and 'confirmacion="ENTREGAR 2"' in out and "r.ipynb" in out
    assert entorno == []


def test_entregar_sin_confirmacion_no_envia(entorno, tmp_path):
    f = tmp_path / "r.ipynb"
    f.write_text("{}")
    for conf in ("", "ENTREGAR 3", "entregar 2", "SI"):
        out = S.entregar(tarea_id=2, ficheros=[str(f)], dry_run=False, confirmacion=conf)
        assert out.startswith("NO ENVIADO")
    assert entorno == []


def test_entregar_validaciones(entorno, tmp_path):
    f = tmp_path / "r.pdf"
    f.write_text("x")
    assert "extension .pdf no permitida" in S.entregar(tarea_id=2, ficheros=[str(f)], dry_run=False,
                                                       confirmacion="ENTREGAR 2")
    assert "no admite online_url" in S.entregar(tarea_id=2, url="https://a.b")
    assert "exactamente uno" in S.entregar(tarea_id=2)
    assert "no existe" in S.entregar(tarea_id=2, ficheros=["/no/existe.ipynb"])
    assert entorno == []


def test_entregar_con_confirmacion_envia_y_relee(entorno, monkeypatch, tmp_path):
    f = tmp_path / "r.ipynb"
    f.write_text("{}")

    def api(path, paginate=True):
        if path == "courses/1001/assignments/2/submissions/self":
            return {"workflow_state": "submitted", "late": False, "attempt": 1, "submitted_at": iso(0),
                    "attachments": [{"display_name": "r.ipynb", "size": 2}]}
        return fake_api(path)
    monkeypatch.setattr(S, "_api", api)
    out = S.entregar(tarea_id=2, ficheros=[str(f)], comentario="va", dry_run=False, confirmacion="ENTREGAR 2")
    assert out.startswith("ENVIADA MAT-101 [2]: workflow_state=submitted late=False")
    assert entorno[0][0] == "SUBIR"
    metodo, ruta, datos = entorno[1]
    assert metodo == "POST" and ruta == "courses/1001/assignments/2/submissions"
    assert ("submission[submission_type]", "online_upload") in datos and ("submission[file_ids][]", "99") in datos
    assert ("comment[text_comment]", "va") in datos


def test_comentar_candado(entorno):
    assert S.comentar_entrega(tarea_id=2, texto="gracias").startswith("DRY-RUN")
    assert S.comentar_entrega(tarea_id=2, texto="gracias", dry_run=False,
                              confirmacion="ENTREGAR 2").startswith("NO ENVIADO")
    assert entorno == []
    S.comentar_entrega(tarea_id=2, texto="gracias", dry_run=False, confirmacion="COMENTAR 2")
    assert entorno == [("PUT", "courses/1001/assignments/2/submissions/self", [("comment[text_comment]", "gracias")])]


def test_asignatura_desconocida():
    assert S.tareas(asignatura="quimica").startswith("Error: Asignatura 'quimica' no encontrada")


def test_protocolo_mcp_y_schemas(entorno):
    from mcp.client import Client

    async def main():
        async with Client(S.mcp) as c:
            tools = (await c.list_tools()).tools
            r = await c.call_tool("entregar", {"tarea_id": 11, "url": "https://github.com/a/b"})
            return tools, r
    tools, r = asyncio.run(main())
    assert len(tools) == 14
    assert all("title" not in str(t.input_schema) for t in tools)
    assert all(t.output_schema is None for t in tools)
    ent = next(t for t in tools if t.name == "entregar")
    assert ent.input_schema["properties"]["dry_run"]["default"] is True
    assert ent.input_schema["properties"]["ficheros"]["type"] == "array"
    texto = getattr(r.content[0], "text", "")
    assert texto.startswith("DRY-RUN") and "Equipo 3" in texto
    assert entorno == []
