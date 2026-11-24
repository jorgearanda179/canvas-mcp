import os

# Antes de importar el servidor: sin precalentado y con fechas en UTC, igual en cualquier máquina.
os.environ["CANVAS_MCP_PREWARM"] = "0"
os.environ["CANVAS_TZ"] = "UTC"
