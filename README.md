# canvas-mcp

Servidor [MCP](https://modelcontextprotocol.io) para que un asistente de IA (Claude Desktop, Claude Code, Cursor o cualquier cliente MCP) consulte tu Canvas LMS como alumno. Con él puede ver tareas pendientes, notas, material, anuncios, foros, calendario, grupos y mensajes, y también entregar tareas, aunque solo con tu confirmación explícita.

- Usa tu propio token de Canvas y corre en tu ordenador: los datos no pasan por ningún servidor intermedio.
- Sus respuestas son texto plano compacto (una línea por elemento) en lugar del JSON de Canvas, para que el asistente gaste menos tokens.
- Guarda las respuestas en una caché en memoria y consulta los cursos en paralelo. Si un curso falla, el resto sigue respondiendo.
- No necesita más dependencias que el SDK de MCP: el cliente HTTP es solo biblioteca estándar.

## Herramientas (14)

El parámetro `asignatura` acepta el id del curso, su código o parte del nombre. Si va vacío, se consultan todas las asignaturas activas.

| Herramienta | Qué devuelve |
|---|---|
| `cursos()` | Asignaturas activas: código, id y nombre |
| `tareas(asignatura, dias=14, solo_pendientes=True)` | Tareas próximas con su fecha, los días que faltan, el estado de tu entrega y la nota |
| `tarea(tarea_id, asignatura, completo=False)` | El enunciado en texto limpio, los adjuntos, la rúbrica, los tipos de entrega, las extensiones permitidas, los intentos y tu grupo |
| `mi_entrega(tarea_id)` | Estado, intento, nota, adjuntos, comentarios del profesor y rúbrica evaluada |
| `notas(asignatura)` | La nota que calcula Canvas para el curso y las notas publicadas de cada tarea |
| `anuncios(asignatura, dias=7, desde, completo)` | Los últimos anuncios de los profesores |
| `material(asignatura, buscar)` | Los módulos y sus elementos, marcando los bloqueados. `buscar` filtra por nombre en todas las asignaturas |
| `descargar(fichero_id, destino)` | Descarga un fichero de Canvas |
| `foros(asignatura, tema_id)` | Los temas de debate y, si pasas `tema_id`, sus respuestas |
| `calendario(dias=14)` | Los eventos del calendario de Canvas |
| `grupos()` | Tus grupos y sus miembros |
| `mensajes(conversacion_id, n=10, no_leidos)` | La bandeja de entrada de Canvas, en solo lectura: no marca nada como leído |
| `entregar(...)` | Entrega una URL, un texto o ficheros (ver «Candado de escritura») |
| `comentar_entrega(...)` | Añade un comentario a tu entrega (ver «Candado de escritura») |

### Candado de escritura

`entregar` y `comentar_entrega` funcionan por defecto con `dry_run=True`: solo devuelven lo que se enviaría y no mandan nada. Ese resumen indica:

- la tarea y el tipo de entrega;
- los ficheros y su tamaño;
- si la entrega llegaría tarde;
- si crearía un intento nuevo;
- y, si la tarea es de grupo, para qué grupo cuenta.

Para enviar de verdad hacen falta `dry_run=False` **y** `confirmacion="ENTREGAR <tarea_id>"` (para comentar, `"COMENTAR <tarea_id>"`). Con cualquier otro valor la respuesta es `NO ENVIADO` y no se hace ninguna petición. Antes de enviar se comprueba que la tarea admita ese tipo de entrega y esa extensión de fichero.

## Instalación

Necesitas Python 3.10 o superior.

```bash
git clone https://github.com/jorgearanda179/canvas-mcp.git
cd canvas-mcp
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
pip install -e .
```

### 1. Crea un token de Canvas

En Canvas, entra en **Cuenta → Configuraciones → Integraciones aprobadas → + Nuevo token de acceso**. Ponle un propósito (por ejemplo, «canvas-mcp») y una fecha de caducidad, y cópialo: Canvas solo lo enseña una vez.

> Algunas universidades no dejan crear tokens a los alumnos. Si no ves el botón, este servidor no podrá conectarse. Antes de usarlo, revisa las normas de uso de tu centro.

### 2. Comprueba que conecta

```bash
# Linux/macOS:
export CANVAS_BASE_URL=https://tu-universidad.instructure.com
export CANVAS_TOKEN=tu-token
# Windows:
set CANVAS_BASE_URL=https://tu-universidad.instructure.com
set CANVAS_TOKEN=tu-token

canvas-mcp --check   # lista tus cursos activos y sale
```

### 3. Añádelo a tu cliente MCP

**Claude Desktop.** Edita `claude_desktop_config.json` (en *Configuración → Desarrollador → Editar configuración*):

```json
{
  "mcpServers": {
    "canvas": {
      "command": "/ruta/a/canvas-mcp/.venv/bin/canvas-mcp",
      "env": {
        "CANVAS_BASE_URL": "https://tu-universidad.instructure.com",
        "CANVAS_TOKEN": "tu-token"
      }
    }
  }
}
```

En Windows, el comando es `C:\\ruta\\a\\canvas-mcp\\.venv\\Scripts\\canvas-mcp.exe`.

**Claude Code:**

```bash
claude mcp add canvas --env CANVAS_BASE_URL=https://tu-universidad.instructure.com \
  --env CANVAS_TOKEN=tu-token -- /ruta/a/canvas-mcp/.venv/bin/canvas-mcp
```

Para otros clientes, el servidor usa transporte stdio: el comando es `canvas-mcp` (o `python -m canvas_mcp`) con las mismas variables de entorno.

## Configuración

| Variable | Obligatoria | Descripción |
|---|---|---|
| `CANVAS_BASE_URL` | sí | La dirección de tu Canvas, por ejemplo `https://tu-universidad.instructure.com` |
| `CANVAS_TOKEN` | sí (o la siguiente) | Tu token de acceso personal |
| `CANVAS_TOKEN_FILE` | | Ruta a un fichero que solo contiene el token, para no ponerlo en la configuración. Se lee en cada petición, así que al renovar el token no hace falta reiniciar |
| `CANVAS_COURSES` | | Ids o códigos de curso separados por comas, para limitarlo a ciertas asignaturas. Por defecto usa todas las activas |
| `CANVAS_CODIGO_REGEX` | | Expresión regular que saca un código corto del `course_code`. Por ejemplo, `-(\d{4})-` convierte `2627-ABC-3-4168-A` en `4168` |
| `CANVAS_TZ` | | Zona horaria de las fechas, por ejemplo `Europe/Madrid`. Por defecto usa la del sistema |
| `CANVAS_DOWNLOAD_DIR` | | Carpeta de descargas. Por defecto `~/Downloads/canvas` |
| `CANVAS_MCP_TIMEOUT` | | Segundos de espera por petición. Por defecto 25 |

Tienes un ejemplo en [`.env.example`](.env.example). El token nunca debe acabar en el repositorio: `.env` y `token.txt` ya están en `.gitignore`.

## Seguridad y privacidad

- El token solo se envía a tu `CANVAS_BASE_URL`. Las descargas, que Canvas redirige a su almacenamiento (S3), siguen la redirección sin reenviar el token.
- No se guarda nada en disco salvo los ficheros que pidas descargar. La caché vive en memoria y desaparece al cerrar el servidor.
- `mensajes` lee la bandeja sin marcar los mensajes como leídos.
- Todo lo que devuelven las herramientas llega al modelo de IA que uses. Ten en cuenta su política de privacidad.

## Desarrollo

```bash
pip install -e ".[dev]"
pytest -q
```

Las pruebas no salen a internet: la API de Canvas se sustituye por datos falsos y el cliente HTTP se prueba contra un servidor local. Comprueban la paginación, que el token no salga de Canvas en las redirecciones y que el candado no envíe nada sin confirmación.

## Licencia

[MIT](LICENSE). Proyecto independiente, sin relación con Instructure.
