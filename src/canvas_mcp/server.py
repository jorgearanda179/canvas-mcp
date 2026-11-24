"""Servidor MCP (stdio) para Canvas LMS desde la cuenta de un alumno.

Salida en texto plano, una línea por elemento. Caché en memoria con TTL y peticiones en paralelo.
Escrituras (entregar, comentar_entrega) con candado: dry_run por defecto + confirmación explícita.
"""
from __future__ import annotations

import html
import os
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import cliente

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _zona():
    nombre = os.environ.get("CANVAS_TZ", "").strip()
    if nombre and ZoneInfo is not None:
        try:
            return ZoneInfo(nombre)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo  # zona horaria del sistema


TZ = _zona()
DESCARGAS = os.path.expanduser(os.environ.get("CANVAS_DOWNLOAD_DIR", "~/Downloads/canvas"))
T_API = float(os.environ.get("CANVAS_MCP_TIMEOUT", "25"))  # s por petición GET
TTL_CORTO, TTL_LARGO = 300, 3600  # tareas/anuncios 5 min; cursos/módulos/grupos 1 h

_NET = ThreadPoolExecutor(12, thread_name_prefix="net")   # peticiones HTTP
_FAN = ThreadPoolExecutor(8, thread_name_prefix="fan")    # reparto por curso


# ---------------------------------------------------------------- acceso a Canvas
def _is_err(d) -> bool:
    if isinstance(d, dict):
        return ("_error" in d or "errors" in d or d.get("status") in ("unauthorized", "not_found")
                or (set(d) <= {"message", "error_report_id"} and "message" in d))
    return d is None


def _errtxt(d) -> str:
    if isinstance(d, dict):
        if "_error" in d:
            return str(d["_error"])[:120] or "respuesta no JSON"
        errs = d.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            return (d.get("status") or errs[0].get("message") or "error")[:80]
        if isinstance(errs, dict):
            return str(errs)[:120]
        return str(d.get("status") or d.get("message") or "error")[:80]
    return "sin respuesta"


def _api(path: str, paginate: bool = True):
    """GET con timeout duro. Nunca lanza: error -> dict con _error."""
    try:
        cliente.base_url(), cliente.token()
    except cliente.ErrorConfig as e:
        return {"_error": f"configuración: {e}"}
    fut = _NET.submit(cliente.get, path, paginate, T_API)
    try:
        return fut.result(timeout=T_API + 5)
    except Exception as e:
        return {"_error": f"timeout {T_API:.0f}s" if type(e).__name__ == "TimeoutError" else type(e).__name__}


def _send(method: str, path: str, data: list[tuple[str, str]]):
    try:
        return cliente.send(method, path, data)
    except cliente.ErrorConfig as e:
        return 0, {"_error": f"configuración: {e}"}


def _download(url: str, dest: str) -> bool:
    return cliente.download(url, dest)


# ---------------------------------------------------------------- caché
_cache: dict = {}
_klocks: dict = defaultdict(threading.Lock)
_glock = threading.Lock()


def _cached(key, ttl, fn):
    """Caché con TTL y un candado por clave (dos peticiones iguales a la vez = una sola)."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _glock:
        lk = _klocks[key]
    with lk:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        v = fn()
        if not _is_err(v):
            _cache[key] = (time.time(), v)
        return v


def _invalida(*prefijos):
    for k in list(_cache):
        if isinstance(k, tuple) and k[0] in prefijos:
            _cache.pop(k, None)


def _fan(cids, fn):
    """Ejecuta fn(cid) en paralelo; un curso que falla no tumba al resto."""
    futs = {cid: _FAN.submit(fn, cid) for cid in cids}
    out = {}
    for cid, f in futs.items():
        try:
            out[cid] = f.result(timeout=T_API * 2 + 5)
        except Exception as e:
            out[cid] = {"_error": type(e).__name__}
    return out


# ---------------------------------------------------------------- asignaturas
def _canvas_cursos():
    return _cached(("canvas_cursos",), TTL_LARGO,
                   lambda: _api("courses?enrollment_state=active&include[]=total_scores"))


def _codigo_corto(code: str) -> str:
    """CANVAS_CODIGO_REGEX (opcional) extrae un código corto del course_code; si no, se usa entero."""
    rx = os.environ.get("CANVAS_CODIGO_REGEX", "")
    if rx:
        try:
            m = re.search(rx, code)
        except re.error:
            m = None
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return code


def _cargar_cursos():
    """{cid: {"cod", "code", "nombre"}} de los cursos activos (CANVAS_COURSES filtra por id o código)."""
    d = _canvas_cursos()
    if not isinstance(d, list):
        return d
    filtro = {x.strip() for x in os.environ.get("CANVAS_COURSES", "").split(",") if x.strip()}
    out = {}
    for c in d:
        if not isinstance(c, dict) or "id" not in c or c.get("access_restricted_by_date"):
            continue
        code = c.get("course_code") or str(c["id"])
        if filtro and str(c["id"]) not in filtro and code not in filtro and _codigo_corto(code) not in filtro:
            continue
        out[int(c["id"])] = {"cod": _codigo_corto(code), "code": code, "nombre": c.get("name") or code}
    return out


class Fallo(Exception):
    pass


def _cursos() -> dict:
    v = _cached(("cursos",), TTL_LARGO, _cargar_cursos)
    if _is_err(v) or not isinstance(v, dict):
        raise Fallo(f"no se pudieron leer los cursos de Canvas ({_errtxt(v)})")
    return v


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


def _resolve(asignatura: str = "") -> list[int]:
    cur = _cursos()
    a = str(asignatura or "").strip()
    if not a:
        return list(cur)
    if a.isdigit():
        n = int(a)
        hit = [c for c, v in cur.items() if c == n or v["cod"] == a]
        hit = hit or [c for c, v in cur.items() if a in v["code"]]
    else:
        hit = [c for c, v in cur.items() if _norm(a) in _norm(v["nombre"]) or _norm(a) in _norm(v["code"])]
    if not hit:
        raise Fallo(f"Asignatura '{a}' no encontrada. Activas: "
                    + ", ".join(f"{v['cod']} {v['nombre']}" for v in cur.values()))
    return hit


def _cod(cid) -> str:
    try:
        return _cursos().get(int(cid), {}).get("cod", str(cid))
    except Fallo:
        return str(cid)


def _asgs(cid):
    return _cached(("asg", cid), TTL_CORTO,
                   lambda: _api(f"courses/{cid}/assignments?include[]=submission&order_by=due_at"))


def _buscar_tarea(tarea_id: int, asignatura: str = ""):
    """(cid, assignment) buscando en la cache de tareas; si hace falta, en todas las asignaturas."""
    cids = _resolve(asignatura)
    res = _fan(cids, _asgs)
    for cid, lst in res.items():
        if isinstance(lst, list):
            for a in lst:
                if a.get("id") == int(tarea_id):
                    return cid, a
    for cid in cids:  # tarea no listada (p. ej. sin publicar en la lista): pedirla directa
        d = _api(f"courses/{cid}/assignments/{int(tarea_id)}?include[]=submission")
        if not _is_err(d):
            return cid, d
    raise Fallo(f"Tarea {tarea_id} no encontrada en " + ", ".join(_cod(c) for c in cids))


# ---------------------------------------------------------------- formato
_DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]


def _now():
    return datetime.now(TZ)


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        return None


def _f(dt, hora=True) -> str:
    if not dt:
        return "sin fecha"
    return f"{_DIAS[dt.weekday()]} {dt:%d/%m}" + (f" {dt:%H:%M}" if hora else "")


def _rel(dt) -> str:
    if not dt:
        return ""
    d = (dt.date() - _now().date()).days
    return {0: "hoy", 1: "manana", -1: "ayer"}.get(d, f"{d:+d}d")


def _g(x) -> str:
    if x is None:
        return "-"
    try:
        x = float(x)
        return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)


def _kb(n) -> str:
    if not n:
        return "?"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def _txt(h: str, limite: int = 0) -> str:
    """HTML de Canvas -> texto plano legible (conserva URLs externas)."""
    if not h:
        return ""
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", h)

    def _a(m):
        href, t = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        fm = re.search(r"/files/(\d+)", href)
        if fm:
            return f"{t} [fichero {fm.group(1)}]"
        if href.startswith("http") and href not in t:
            return f"{t} ({href})"
        return t
    h = re.sub(r'(?is)<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', _a, h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "\n- ", h)
    h = re.sub(r"(?i)</(p|div|h\d|tr|ul|ol|table|blockquote|pre)>", "\n", h)
    h = html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in h.splitlines()]
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if limite and len(t) > limite:
        t = t[:limite].rsplit(" ", 1)[0] + f" [...+{len(t) - limite} car.]"
    return t


def _linea(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _nom(a) -> str:
    return " ".join((a.get("name") or "").split())


def _estado(a, con_estado=True, con_pts=True) -> str:
    """'graded late nota 9/10' · con_estado=False omite unsubmitted (listas de pendientes)."""
    s = a.get("submission") or {}
    st = s.get("workflow_state") or "unsubmitted"
    p = [st] if con_estado or st != "unsubmitted" else []
    p += [f for f in ("late", "missing", "excused") if s.get(f)]
    pts = a.get("points_possible")
    if s.get("score") is not None:
        p.append(f"nota {_g(s['score'])}/{_g(pts)}" if con_pts else f"nota {_g(s['score'])}")
    elif pts and con_pts:
        p.append(f"{_g(pts)} pts")
    return " ".join(p) or "pendiente"


def _cerrada(a) -> bool:
    lk = _dt(a.get("lock_at"))
    return bool(lk and lk < _now())


def _pendiente(a) -> bool:
    s = a.get("submission") or {}
    return (s.get("workflow_state") or "unsubmitted") == "unsubmitted" and not s.get("excused")


_SIN_ONLINE = {"none", "on_paper", "not_graded", "external_tool", "online_quiz"}


# ---------------------------------------------------------------- MCP
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("canvas", instructions=(
    "Canvas LMS del usuario (alumno). asignatura = id del curso, codigo o parte del nombre; vacio = todas."),
    log_level="WARNING")


def tool(fn):
    """Registra la herramienta sin outputSchema (la salida es texto) y atrapa los Fallo."""
    import functools

    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except Fallo as e:
            return f"Error: {e}"
    mcp.add_tool(w, structured_output=False)
    return w


@tool
def cursos() -> str:
    """Asignaturas activas: codigo, id de Canvas, codigo de curso y nombre."""
    return "\n".join(f"{v['cod']} [{cid}] {v['code']} {v['nombre']}" for cid, v in _cursos().items()) \
        or "Sin cursos activos."


@tool
def tareas(asignatura: str = "", dias: int = 14, solo_pendientes: bool = True) -> str:
    """Tareas pendientes y proximas (fecha local, estado de mi entrega, nota).

    dias: horizonte hacia delante. solo_pendientes=False incluye entregadas y pasadas."""
    cids = _resolve(asignatura)
    ahora, lim = _now(), _now() + timedelta(days=dias)
    filas, errs = [], []
    for cid, lst in _fan(cids, _asgs).items():
        if not isinstance(lst, list):
            errs.append(f"{_cod(cid)}: sin datos ({_errtxt(lst)})")
            continue
        for a in lst:
            due = _dt(a.get("due_at"))
            if due and due > lim:
                continue
            pend = _pendiente(a)
            if solo_pendientes and not pend:
                continue
            extra = []
            if pend and due and due < ahora and not (a.get("submission") or {}).get("missing"):
                extra.append("ATRASADA")
            if _cerrada(a):
                extra.append("cerrada")
            if set(a.get("submission_types") or []) <= _SIN_ONLINE:
                extra.append("sin entrega online")
            filas.append((due or datetime.max.replace(tzinfo=TZ),
                          f"{_cod(cid)} [{a['id']}] {_linea(_nom(a), 70)} · {_f(due)}"
                          + (f" ({_rel(due)})" if due else "")
                          + f" · {_estado(a, con_estado=not solo_pendientes)}"
                          + (" · " + ", ".join(extra) if extra else "")))
    filas.sort(key=lambda x: x[0])
    cab = f"{len(filas)} {'pendientes' if solo_pendientes else 'tareas'} hasta {_f(lim, False)}:"
    return "\n".join([cab] + [f for _, f in filas] + errs)


def _fichero(fid):
    return _cached(("file", int(fid)), TTL_LARGO, lambda: _api(f"files/{int(fid)}"))


def _grupos():
    return _cached(("grupos",), TTL_LARGO, lambda: _api("users/self/groups"))


def _miembros(gid):
    return _cached(("miembros", gid), TTL_LARGO, lambda: _api(f"groups/{gid}/users"))


def _mi_grupo(cat_id):
    gs = _grupos()
    if not isinstance(gs, list):
        return f"? ({_errtxt(gs)})"
    g = next((g for g in gs if g.get("group_category_id") == cat_id), None)
    if not g:
        return "sin grupo"
    m = _miembros(g["id"])
    nombres = ", ".join(u.get("short_name") or u.get("name", "?") for u in m) if isinstance(m, list) else "?"
    return f"{g['name']} [{g['id']}]: {nombres}"


@tool
def tarea(tarea_id: int, asignatura: str = "", completo: bool = False) -> str:
    """Detalle de una tarea: enunciado limpio, adjuntos, rubrica, tipos de entrega, grupo.

    completo=True no recorta el enunciado (por defecto 2500 caracteres)."""
    cid, a = _buscar_tarea(tarea_id, asignatura)
    due, s = _dt(a.get("due_at")), a.get("submission") or {}
    ab, ci = _dt(a.get("unlock_at")), _dt(a.get("lock_at"))
    p = [f"vence {_f(due)}" + (f" ({_rel(due)})" if due else ""), f"{_g(a.get('points_possible'))} pts"]
    if ab and ab > _now():
        p.append(f"abre {_f(ab)}")
    if ci and ci != due:
        p.append(f"cierra {_f(ci)}")
    p.append(_estado(a, con_pts=False) + (f" {_f(_dt(s.get('submitted_at')))}" if s.get("submitted_at") else ""))
    p.append("entrega " + ("/".join(a.get("submission_types") or []) or "-"))
    if a.get("allowed_extensions"):
        p.append("ext " + "/".join(a["allowed_extensions"]))
    if a.get("allowed_attempts") not in (None, -1):
        p.append(f"intentos {a['allowed_attempts']}")
    if a.get("group_category_id"):
        g = _mi_grupo(a["group_category_id"])
        p.append(g if g == "sin grupo" else f"grupo {g}")
    out = [f"{_cod(cid)} [{a['id']}] {_nom(a)}", ", ".join(p)]
    if a.get("locked_for_user"):
        out.append("Bloqueada: " + _linea(a.get("lock_explanation") or "", 140))
    desc = a.get("description") or ""
    ids = list(dict.fromkeys(re.findall(r"/files/(\d+)", desc)))
    txt = _txt(desc, 0 if completo else 2500) or "(vacio)"
    if ids:  # tamano y estado de cada adjunto, en linea con el enunciado
        fs = _fan(ids, _fichero)
        sueltos = []
        for i in ids:
            f = fs.get(i)
            info = (f"{_kb(f.get('size'))}" + (", BLOQUEADO" if f.get("locked_for_user") else "")
                    if not _is_err(f) else f"sin acceso: {_errtxt(f)}")
            if f"[fichero {i}]" in txt:
                txt = txt.replace(f"[fichero {i}]", f"[fichero {i}, {info}]")
            else:
                sueltos.append(f"[{i}] {f.get('display_name', '?') if not _is_err(f) else ''} ({info})")
        if sueltos:
            txt += "\nAdjuntos: " + "; ".join(sueltos)
    out.append("Enunciado: " + txt)
    if a.get("rubric"):
        out.append("Rubrica:")
        for c in a["rubric"]:
            out.append(f"- {_linea(c.get('description'), 80)} ({_g(c.get('points'))} pts)"
                       + (f": {_linea(c.get('long_description'), 120)}" if c.get("long_description") else ""))
    return "\n".join(out)


@tool
def mi_entrega(tarea_id: int, asignatura: str = "") -> str:
    """Mi entrega de una tarea: estado, adjuntos, url, comentarios del profesor, nota y rubrica."""
    cid, a = _buscar_tarea(tarea_id, asignatura)
    s = _api(f"courses/{cid}/assignments/{int(tarea_id)}/submissions/self"
             "?include[]=submission_comments&include[]=rubric_assessment")
    if _is_err(s):
        return f"Error leyendo la entrega: {_errtxt(s)}"
    st = s.get("workflow_state")
    out = [f"{_cod(cid)} [{a['id']}] {_nom(a)}",
           f"Estado: {st}" + (" late" if s.get("late") else "") + (" missing" if s.get("missing") else "")
           + (" excused" if s.get("excused") else "")
           + (f" · entregada {_f(_dt(s.get('submitted_at')))}" if s.get("submitted_at") else " · sin entregar")
           + (f" · intento {s.get('attempt')}" if s.get("attempt") else "")
           + (f" · nota {_g(s.get('score'))}/{_g(a.get('points_possible'))}" if s.get("score") is not None
              else (f" · nota {s.get('grade')}" if s.get("grade") else ""))
           + (f" (calificada {_f(_dt(s.get('graded_at')), False)})" if s.get("graded_at") and st == "graded" else "")]
    if s.get("url"):
        out.append(f"URL entregada: {s['url']}")
    if s.get("body"):
        out.append("Texto: " + _linea(_txt(s["body"]), 300))
    for f in s.get("attachments") or []:
        out.append(f"Adjunto [{f.get('id')}] {f.get('display_name')} ({_kb(f.get('size'))})")
    com = s.get("submission_comments") or []
    if com:
        out.append(f"Comentarios ({len(com)}):")
        for c in com:
            out.append(f"- {_f(_dt(c.get('created_at')), False)} {c.get('author_name', '?')}: "
                       + _linea(c.get("comment"), 400))
    ra = s.get("rubric_assessment") or {}
    if ra:
        crit = {c.get("id"): c for c in a.get("rubric") or []}
        out.append("Rubrica evaluada:")
        for k, v in ra.items():
            c = crit.get(k, {})
            out.append(f"- {_linea(c.get('description', k), 60)}: {_g(v.get('points'))}/{_g(c.get('points'))}"
                       + (f" — {_linea(v.get('comments'), 200)}" if v.get("comments") else ""))
    return "\n".join(out)


@tool
def notas(asignatura: str = "") -> str:
    """Calificaciones: nota actual del curso en Canvas y notas publicadas por tarea."""
    cids = _resolve(asignatura)
    tot = _canvas_cursos()
    sc = {}
    if isinstance(tot, list):
        for c in tot:
            e = next((e for e in c.get("enrollments") or [] if e.get("type") == "student"), {})
            sc[c["id"]] = e
    res = _fan(cids, _asgs)
    out = []
    for cid in cids:
        v, e = _cursos()[cid], sc.get(cid, {})
        cab = f"{v['cod']} {v['nombre']}"
        if e.get("computed_current_score") is not None or e.get("computed_final_score") is not None:
            cab += f" · Canvas: actual {_g(e.get('computed_current_score'))}% · final {_g(e.get('computed_final_score'))}%"
        out.append(cab)
        lst = res.get(cid)
        if not isinstance(lst, list):
            out.append(f"  (sin datos: {_errtxt(lst)})")
            continue
        n = 0
        for a in lst:
            s = a.get("submission") or {}
            if s.get("score") is not None or (s.get("grade") and s.get("workflow_state") == "graded"):
                n += 1
                out.append(f"  {_linea(_nom(a), 60)}: {_g(s.get('score')) if s.get('score') is not None else s.get('grade')}"
                           f"/{_g(a.get('points_possible'))}" + (" late" if s.get("late") else ""))
        if not n:
            out.append("  (sin notas publicadas)")
    return "\n".join(out)


@tool
def anuncios(asignatura: str = "", dias: int = 7, desde: str = "", completo: bool = False) -> str:
    """Ultimos anuncios de los profesores. desde=AAAA-MM-DD sustituye a dias; completo=texto entero."""
    cids = _resolve(asignatura)
    ini = desde or (_now() - timedelta(days=dias)).date().isoformat()
    fin = (_now() + timedelta(days=1)).date().isoformat()
    ctx = "&".join(f"context_codes[]=course_{c}" for c in cids)
    d = _cached(("anun", tuple(cids), ini), TTL_CORTO,
                lambda: _api(f"announcements?{ctx}&start_date={ini}&end_date={fin}"))
    if _is_err(d):
        return f"Error: {_errtxt(d)}"
    if not d:
        return f"Sin anuncios desde {ini}."
    out = []
    for a in sorted(d, key=lambda x: x.get("posted_at") or "", reverse=True):
        cid = int((a.get("context_code") or "course_0").split("_")[1])
        cuerpo = _txt(a.get("message")) if completo else _linea(_txt(a.get("message")), 150)
        out.append(f"{_f(_dt(a.get('posted_at')))} {_cod(cid)} {a.get('title')} "
                   f"({a.get('user_name', '?')}): {cuerpo}")
    return "\n".join(out)


def _modulos(cid):
    return _cached(("mod", cid), TTL_LARGO,
                   lambda: _api(f"courses/{cid}/modules?include[]=items&include[]=content_details"))


_TIPO = {"File": "fichero", "Page": "pagina", "Assignment": "tarea", "ExternalUrl": "url", "Quiz": "quiz",
         "Discussion": "foro", "ExternalTool": "lti", "SubHeader": "--"}


@tool
def material(asignatura: str = "", buscar: str = "") -> str:
    """Modulos e items de una asignatura (marca los bloqueados). buscar filtra por nombre, sin asignatura = todas."""
    if not asignatura and not buscar:
        return "Error: indica asignatura o buscar."
    cids = _resolve(asignatura)
    q = _norm(buscar)
    out, errs = [], []
    for cid, mods in _fan(cids, _modulos).items():
        if not isinstance(mods, list):
            errs.append(f"{_cod(cid)}: sin acceso a modulos ({_errtxt(mods)})")
            continue
        if not q:
            n = sum(len(m.get("items") or []) for m in mods)
            out.append(f"{_cod(cid)} {_cursos()[cid]['nombre']}: {len(mods)} modulos, {n} items")
        for m in mods:
            mlock = m.get("state") == "locked" or bool(_dt(m.get("unlock_at")) and _dt(m.get("unlock_at")) > _now())
            mtxt = m.get("name", "") + (f" (BLOQUEADO hasta {_f(_dt(m.get('unlock_at')), False)})"
                                        if mlock and m.get("unlock_at") else " (BLOQUEADO)" if mlock else "")
            if not q:
                out.append(f"# {mtxt}")
            for it in m.get("items") or []:
                if q and q not in _norm(it.get("title")) and q not in _norm(m.get("name")):
                    continue
                cd = it.get("content_details") or {}
                tipo = _TIPO.get(it.get("type"), it.get("type", "?"))
                ref = (f" [{it.get('content_id')}]" if it.get("type") in ("File", "Assignment", "Quiz", "Discussion")
                       else "")
                extra = (" (bloqueado)" if cd.get("locked_for_user") else "") + \
                        (f" {it.get('external_url')}" if it.get("external_url") else "")
                txt = f"{tipo}{ref} {it.get('title')}{extra}"
                out.append(f"{_cod(cid)} · {_linea(mtxt, 40)} › {txt}" if q else f"  {txt}")
    if q and not out:
        out.append(f"Nada con '{buscar}'.")
    return "\n".join(out + errs)


@tool
def descargar(fichero_id: int, destino: str = "") -> str:
    """Descarga un fichero de Canvas por id. destino = carpeta o ruta; vacio = CANVAS_DOWNLOAD_DIR."""
    f = _fichero(fichero_id)
    if _is_err(f):
        return f"Error: fichero {fichero_id} sin acceso ({_errtxt(f)})"
    if f.get("locked_for_user") or not f.get("url"):
        return f"Bloqueado: {f.get('display_name')} aun no esta liberado por el profesor."
    nombre = f.get("display_name") or f.get("filename") or str(fichero_id)
    dest = destino or os.path.join(DESCARGAS, nombre)
    if destino and (destino.endswith("/") or os.path.isdir(destino)):
        dest = os.path.join(destino, nombre)
    dest = os.path.abspath(os.path.expanduser(dest))
    if os.path.exists(dest) and os.path.getsize(dest) == f.get("size"):
        return f"Ya estaba: {dest} ({_kb(f.get('size'))})"
    if os.path.exists(dest):
        b, e = os.path.splitext(dest)
        dest = f"{b} (Canvas){e}"
    ok = _download(f["url"], dest)
    if not ok:
        return f"Error descargando {nombre}."
    tam = os.path.getsize(dest)
    aviso = "" if not f.get("size") or tam == f["size"] else f" OJO: tamano {tam} != {f['size']}"
    return f"OK {dest} ({_kb(tam)}){aviso}"


@tool
def foros(asignatura: str = "", tema_id: int = 0) -> str:
    """Foros de debate (sin anuncios). Con tema_id lee las entradas de ese tema."""
    cids = _resolve(asignatura)
    temas = _fan(cids, lambda c: _cached(("foros", c), TTL_CORTO, lambda: _api(f"courses/{c}/discussion_topics")))
    if tema_id:
        cid = next((c for c, l in temas.items() if isinstance(l, list)
                    and any(t.get("id") == tema_id for t in l)), cids[0] if len(cids) == 1 else None)
        if cid is None:
            return f"Error: tema {tema_id} no encontrado; indica la asignatura."
        v = _api(f"courses/{cid}/discussion_topics/{tema_id}/view")
        if _is_err(v):
            return f"Error: {_errtxt(v)}"
        nom = {p.get("id"): p.get("display_name") for p in v.get("participants") or []}
        out = []

        def rec(es, nivel):
            for e in es or []:
                if not e.get("deleted"):
                    out.append("  " * nivel + f"- {_f(_dt(e.get('created_at')), False)} "
                               f"{nom.get(e.get('user_id'), '?')}: {_linea(_txt(e.get('message')), 300)}")
                rec(e.get("replies"), nivel + 1)
        rec(v.get("view"), 0)
        return "\n".join(out) or "Tema sin respuestas."
    out, errs = [], []
    for cid, l in temas.items():
        if not isinstance(l, list):
            errs.append(f"{_cod(cid)}: sin acceso ({_errtxt(l)})")
            continue
        for t in l:
            out.append(f"{_f(_dt(t.get('last_reply_at') or t.get('posted_at')), False)} {_cod(cid)} [{t.get('id')}] "
                       f"{t.get('title')} · {t.get('discussion_subentry_count', 0)} resp"
                       + (f" ({t['unread_count']} sin leer)" if t.get("unread_count") else ""))
    return "\n".join(out + errs) or "Sin foros en esas asignaturas."


@tool
def calendario(dias: int = 14) -> str:
    """Eventos del calendario de Canvas (no tareas) de las asignaturas activas en los proximos dias."""
    cids = _resolve("")
    ini, fin = _now().date().isoformat(), (_now() + timedelta(days=dias)).date().isoformat()
    ctx = "&".join(f"context_codes[]=course_{c}" for c in cids)
    d = _cached(("cal", ini, fin), TTL_CORTO,
                lambda: _api(f"calendar_events?type=event&{ctx}&start_date={ini}&end_date={fin}"))
    if _is_err(d):
        return f"Error: {_errtxt(d)}"
    if not d:
        return f"Sin eventos de calendario hasta {fin} (las entregas estan en tareas)."
    out = []
    for e in sorted(d, key=lambda x: x.get("start_at") or ""):
        cid = int((e.get("context_code") or "course_0").split("_")[1]) if "course_" in (e.get("context_code") or "") else 0
        a, b = _dt(e.get("start_at")), _dt(e.get("end_at"))
        out.append(f"{_f(a)}" + (f"-{b:%H:%M}" if b and not e.get("all_day") else "")
                   + f" {_cod(cid) if cid else ''} {e.get('title')}"
                   + (f" ({e['location_name']})" if e.get("location_name") else ""))
    return "\n".join(out)


@tool
def grupos() -> str:
    """Mis grupos de Canvas con su asignatura y miembros."""
    gs = _grupos()
    if _is_err(gs):
        return f"Error: {_errtxt(gs)}"
    ms = _fan([g["id"] for g in gs], _miembros)
    out = []
    for g in gs:
        m = ms.get(g["id"])
        nombres = ", ".join(u.get("short_name") or u.get("name", "?") for u in m) if isinstance(m, list) else "?"
        out.append(f"{_cod(g.get('course_id')) if g.get('course_id') in _cursos() else g.get('context_name', '')}"
                   f" · {g.get('name')} [{g['id']}] cat {g.get('group_category_id')}: {nombres}")
    return "\n".join(out) or "Sin grupos."


@tool
def mensajes(conversacion_id: int = 0, n: int = 10, no_leidos: bool = False) -> str:
    """Bandeja de entrada de Canvas (solo lectura, no marca como leido). Con conversacion_id la lee entera."""
    if conversacion_id:
        c = _api(f"conversations/{conversacion_id}?auto_mark_as_read=false", paginate=False)
        if _is_err(c):
            return f"Error: {_errtxt(c)}"
        nom = {p.get("id"): p.get("name") for p in c.get("participants") or []}
        out = [f"{c.get('subject')} ({c.get('context_name', '')})"]
        for m in reversed(c.get("messages") or []):
            out.append(f"- {_f(_dt(m.get('created_at')))} {nom.get(m.get('author_id'), '?')}: "
                       + _linea(m.get("body"), 1500)
                       + (f" [adjuntos: {', '.join(x.get('display_name', '') for x in m['attachments'])}]"
                          if m.get("attachments") else ""))
        return "\n".join(out)
    scope = "unread" if no_leidos else "inbox"
    n = max(1, min(int(n), 50))
    d = _cached(("msg", scope, n), TTL_CORTO,
                lambda: _api(f"conversations?scope={scope}&per_page={n}", paginate=False))
    if _is_err(d):
        return f"Error: {_errtxt(d)}"
    out = []
    for c in d[:n]:
        quien = ", ".join(p.get("name", "") for p in (c.get("participants") or [])[:2])
        out.append(f"{_f(_dt(c.get('last_message_at')), False)} [{c.get('id')}]"
                   + (" NO LEIDO" if c.get("workflow_state") == "unread" else "")
                   + f" {c.get('subject') or '(sin asunto)'} — {_linea(quien, 50)}: {_linea(c.get('last_message'), 110)}")
    return "\n".join(out) or "Bandeja vacia."


# ---------------------------------------------------------------- escritura (con candado)
def _subir(cid, aid, ruta):
    """Pasos 1 y 2 de la subida: pedir hueco y enviar el fichero. Devuelve (file_id, None) o (None, error)."""
    import mimetypes
    nombre, tam = os.path.basename(ruta), os.path.getsize(ruta)
    mime = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
    st, slot = _send("POST", f"courses/{cid}/assignments/{aid}/submissions/self/files",
                     [("name", nombre), ("size", str(tam)), ("content_type", mime)])
    if _is_err(slot) or "upload_url" not in slot:
        return None, f"hueco de subida rechazado ({st} {_errtxt(slot)})"
    d = cliente.upload(slot, ruta)
    if _is_err(d) or not d.get("id"):
        return None, f"subida fallida ({_errtxt(d)})"
    return d["id"], None


@tool
def entregar(tarea_id: int, url: str = "", texto: str = "", ficheros: list[str] | None = None,
             comentario: str = "", asignatura: str = "", dry_run: bool = True, confirmacion: str = "") -> str:
    """Entrega una tarea (url, texto o ficheros locales). Solo con la confirmacion explicita del usuario en el chat.

    Por defecto dry_run: muestra lo que se enviaria. Envio real: dry_run=False y confirmacion="ENTREGAR <tarea_id>"."""
    cid, a = _buscar_tarea(tarea_id, asignatura)
    aid = a["id"]
    modos = [m for m, v in (("online_url", url), ("online_text_entry", texto), ("online_upload", ficheros)) if v]
    if len(modos) != 1:
        return "Error: indica exactamente uno de url, texto o ficheros."
    modo, tipos = modos[0], a.get("submission_types") or []
    problemas = []
    if modo not in tipos:
        problemas.append(f"la tarea no admite {modo} (admite: {', '.join(tipos)})")
    if url and not re.match(r"https?://", url):
        problemas.append("la url debe empezar por http(s)://")
    rutas = []
    for r in ficheros or []:
        p = os.path.abspath(os.path.expanduser(r))
        if not os.path.isfile(p):
            problemas.append(f"no existe {p}")
            continue
        ext = os.path.splitext(p)[1].lstrip(".").lower()
        perm = [e.lower() for e in a.get("allowed_extensions") or []]
        if perm and ext not in perm:
            problemas.append(f"{os.path.basename(p)}: extension .{ext} no permitida ({', '.join(perm)})")
        rutas.append(p)
    due, s = _dt(a.get("due_at")), a.get("submission") or {}
    plan = [f"Tarea: {_cod(cid)} [{aid}] {_nom(a)} · vence {_f(due)}"
            + (f" ({_rel(due)})" if due else "")]
    if due and due < _now():
        plan.append("OJO: ya vencio, llegaria TARDE (late).")
    if _cerrada(a) or a.get("locked_for_user"):
        plan.append("OJO: Canvas la marca cerrada/bloqueada; probablemente rechace el envio.")
    if modo == "online_url":
        plan.append(f"Tipo: online_url · {url}")
    elif modo == "online_text_entry":
        plan.append(f"Tipo: online_text_entry · {len(texto)} caracteres: {_linea(texto, 200)}")
    else:
        plan.append("Tipo: online_upload · " + "; ".join(
            f"{os.path.basename(p)} ({_kb(os.path.getsize(p))})" for p in rutas))
    plan.append(f"Comentario: {_linea(comentario, 300)}" if comentario else "Comentario: (ninguno)")
    if s.get("workflow_state") in ("submitted", "graded", "pending_review"):
        plan.append(f"Ya hay entrega ({s.get('workflow_state')}, intento {s.get('attempt') or 1}): esto crea un intento nuevo.")
    if a.get("group_category_id"):
        plan.append(f"De grupo: cuenta para {_mi_grupo(a['group_category_id'])}")
    if problemas:
        return "NO SE PUEDE ENVIAR:\n- " + "\n- ".join(problemas) + "\n" + "\n".join(plan)
    clave = f"ENTREGAR {aid}"
    if dry_run or confirmacion.strip() != clave:
        cab = "DRY-RUN, no se ha enviado nada." if dry_run else f"NO ENVIADO: falta confirmacion=\"{clave}\"."
        return "\n".join([cab] + plan + [f"Para enviar: dry_run=False y confirmacion=\"{clave}\" (solo con el OK explicito del usuario)."])
    # --- envio real
    datos = [("submission[submission_type]", modo)]
    if modo == "online_url":
        datos.append(("submission[url]", url))
    elif modo == "online_text_entry":
        datos.append(("submission[body]", texto))
    else:
        for p in rutas:
            fid, err = _subir(cid, aid, p)
            if err:
                return f"NO ENVIADO: {os.path.basename(p)}: {err}. La entrega no se ha creado."
            datos.append(("submission[file_ids][]", str(fid)))
    if comentario:
        datos.append(("comment[text_comment]", comentario))
    st, r = _send("POST", f"courses/{cid}/assignments/{aid}/submissions", datos)
    _invalida("asg")
    if _is_err(r):
        return f"Error al entregar (HTTP {st}): {_errtxt(r)}"
    v = _api(f"courses/{cid}/assignments/{aid}/submissions/self")
    if _is_err(v):
        return f"Enviada (HTTP {st}) pero no he podido releerla: {_errtxt(v)}"
    adj = "; ".join(f"{x.get('display_name')} ({_kb(x.get('size'))})" for x in v.get("attachments") or [])
    return (f"ENVIADA {_cod(cid)} [{aid}]: workflow_state={v.get('workflow_state')} late={v.get('late')} "
            f"intento={v.get('attempt')} {_f(_dt(v.get('submitted_at')))}"
            + (f" url={v.get('url')}" if v.get("url") else "") + (f" adjuntos: {adj}" if adj else ""))


@tool
def comentar_entrega(tarea_id: int, texto: str, asignatura: str = "", dry_run: bool = True,
                     confirmacion: str = "") -> str:
    """Anade un comentario a mi entrega. Solo con la confirmacion explicita del usuario en el chat.

    Envio real: dry_run=False y confirmacion="COMENTAR <tarea_id>"."""
    cid, a = _buscar_tarea(tarea_id, asignatura)
    aid, clave = a["id"], f"COMENTAR {a['id']}"
    if not texto.strip():
        return "Error: el comentario esta vacio."
    plan = f"Comentario en {_cod(cid)} [{aid}] {_nom(a)}: {_linea(texto, 400)}"
    if dry_run or confirmacion.strip() != clave:
        cab = "DRY-RUN, no se ha enviado nada." if dry_run else f"NO ENVIADO: falta confirmacion=\"{clave}\"."
        return f"{cab}\n{plan}\nPara enviar: dry_run=False y confirmacion=\"{clave}\" (solo con el OK explicito del usuario)."
    st, r = _send("PUT", f"courses/{cid}/assignments/{aid}/submissions/self", [("comment[text_comment]", texto)])
    if _is_err(r):
        return f"Error al comentar (HTTP {st}): {_errtxt(r)}"
    v = _api(f"courses/{cid}/assignments/{aid}/submissions/self?include[]=submission_comments")
    com = (v.get("submission_comments") or []) if isinstance(v, dict) else []
    ult = com[-1] if com else {}
    return (f"COMENTADO {_cod(cid)} [{aid}] ({len(com)} comentarios). Ultimo: "
            f"{ult.get('author_name', '?')}: {_linea(ult.get('comment'), 200)}")


# ---------------------------------------------------------------- arranque
def _prewarm():
    """Calienta cursos y tareas en segundo plano para que la primera llamada sea rápida."""
    try:
        _fan(list(_cursos()), _asgs)
    except Exception:
        pass


def _limpia_schemas():
    """Quita los 'title' que genera pydantic (no aportan nada y cuestan tokens) y simplifica list | None."""
    def rec(o):
        if isinstance(o, dict):
            o.pop("title", None)
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)
    try:
        herramientas = mcp._tool_manager.list_tools()
    except AttributeError:  # otra versión del SDK: se deja el esquema tal cual
        return
    for t in herramientas:
        props = t.parameters.get("properties", {})
        rec(t.parameters)
        for p in props.values():
            if "anyOf" in p:
                arr = next((x for x in p["anyOf"] if x.get("type") == "array"), None)
                if arr:
                    p.pop("anyOf")
                    p.update(arr)


_limpia_schemas()
# El servidor no ofrece prompts ni resources: no se anuncian.
for _k in [k for k in getattr(getattr(mcp, "_lowlevel_server", None), "_request_handlers", {})
           if k.startswith(("prompts/", "resources/"))]:
    mcp._lowlevel_server._request_handlers.pop(_k, None)


def main():
    if "--check" in sys.argv:
        print(cursos())
        return
    if os.environ.get("CANVAS_MCP_PREWARM", "1") != "0":
        _t = threading.Timer(2.0, _prewarm)
        _t.daemon = True
        _t.start()
    mcp.run()
    # Al cerrarse stdin, salir ya: no esperar a hilos del pool con peticiones lentas.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
