"""
NARSIL API - backend HTTP para conectar la consola narsil_gis.html
con el motor de optimizacion real (narsil_sistema.py) y la base de datos
compartida (narsil.db, gestionada tambien por narsil_manual.py).

Ejecutar localmente:
    pip install fastapi uvicorn --break-system-packages
    uvicorn narsil_api:app --host 0.0.0.0 --port 8000

Endpoints:
    GET  /catalogo                      -> modelos de combustible, topografia, costes
    POST /proyectos                     -> crea un proyecto manual (misma logica que narsil_manual.py)
    GET  /proyectos                     -> lista todos los proyectos con su ultimo resultado
    GET  /proyectos/{id}                -> detalle de un proyecto
    POST /proyectos/{id}/ejecutar       -> corre el motor de optimizacion real sobre ese proyecto
                                            y devuelve sensores/gateways/aerostatos/costes + mapa en base64
    POST /proyectos/{id}/fuente-datos   -> registra una fuente GIS/dron pendiente

SEGURIDAD (obligatoria, revisada 25/07/2026 tras auditoria del proyecto):

1. NARSIL_API_TOKEN es ahora OBLIGATORIO. Si no esta definido como variable
   de entorno, la API se niega a arrancar (falla al importar el modulo, no
   solo en tiempo de peticion). Definelo asi antes de arrancar:
       export NARSIL_API_TOKEN='un-token-largo-y-aleatorio'
   En Render: Settings -> Environment -> añade NARSIL_API_TOKEN.

2. CORS ya no acepta "*" por defecto. Los origenes permitidos se leen de la
   variable de entorno NARSIL_ALLOWED_ORIGINS (una lista separada por comas,
   p.ej. "https://mi-consola-narsil.onrender.com,https://narsil.es").
   Si no se define, se usa una lista de origenes de desarrollo local
   (localhost/127.0.0.1 en varios puertos habituales) para que puedas seguir
   probando en tu maquina sin exponer la API al dominio publico por error.
   En cuanto tengas el dominio real donde se sirva narsil_gis.html /
   narsil_control.html, define NARSIL_ALLOWED_ORIGINS con ese dominio exacto.

Los endpoints /catalogo y /salud siguen siendo publicos sin token a proposito
(son datos de referencia sin informacion sensible: modelos de combustible,
topografia, costes de catalogo, y un ping de salud). Todo lo que crea, lista
o ejecuta proyectos exige el token.
"""

import os
import sys
import base64
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import narsil_manual as nm
import narsil_sistema as ns
import narsil_auth
import narsil_asistente

# --- autenticacion obligatoria ----------------------------------------------
# La API no arranca sin NARSIL_API_TOKEN definido. Esto se comprueba al
# importar el modulo (fallo inmediato y visible en el log de despliegue),
# no de forma silenciosa la primera vez que llega una peticion.
AUTH_TOKEN = os.environ.get("NARSIL_API_TOKEN")
if not AUTH_TOKEN:
    sys.stderr.write(
        "\n"
        "ERROR DE ARRANQUE: falta la variable de entorno NARSIL_API_TOKEN.\n"
        "Por seguridad, NARSIL API ya no arranca sin un token configurado.\n"
        "Define uno antes de arrancar, por ejemplo:\n"
        "    export NARSIL_API_TOKEN='un-token-largo-y-aleatorio'\n"
        "En Render: Settings -> Environment -> añade NARSIL_API_TOKEN.\n\n"
    )
    raise RuntimeError("NARSIL_API_TOKEN no definido: arranque abortado por seguridad.")

# --- CORS: origenes permitidos, configurables por entorno -------------------
# Por defecto (si no se define NARSIL_ALLOWED_ORIGINS), solo se permite
# desarrollo local. Define NARSIL_ALLOWED_ORIGINS en cuanto tengas el
# dominio real de produccion.
_DEFAULT_DEV_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://localhost:8000", "http://127.0.0.1:8000",
]
_origins_env = os.environ.get("NARSIL_ALLOWED_ORIGINS", "").strip()
if _origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = _DEFAULT_DEV_ORIGINS
    sys.stderr.write(
        "\nAVISO: NARSIL_ALLOWED_ORIGINS no esta definida. Usando solo origenes de "
        "desarrollo local (" + ", ".join(_DEFAULT_DEV_ORIGINS) + ").\n"
        "Cuando tengas el dominio real donde se sirva la consola, define "
        "NARSIL_ALLOWED_ORIGINS con ese dominio exacto (lista separada por comas).\n\n"
    )

app = FastAPI(title="NARSIL API", version="0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Login de administrador con aprobación bloqueante por móvil (ver narsil_auth.py).
# Expone /auth/login, /auth/estado/{token}, /auth/aprobar/{token}, /auth/rechazar/{token}.
app.include_router(narsil_auth.router)
app.include_router(narsil_asistente.router)


def verificar_auth(authorization: Optional[str] = Header(None)):
    esperado = f"Bearer {AUTH_TOKEN}"
    if authorization != esperado:
        raise HTTPException(status_code=401, detail="Token invalido o ausente")


# ---------------------------------------------------------------------------
# Modelos de peticion
# ---------------------------------------------------------------------------
class NuevoProyecto(BaseModel):
    nombre: str
    area_ha: float
    pct_complejo: float          # 0-1
    fuel_simple: str             # codigo, p.ej. "GR1"
    fuel_complejo: str           # codigo, p.ej. "TU5"
    topografia: str              # codigo, p.ej. "OND"
    spacing_base_m: float = 100
    estrategia: str = "densa"    # "densa" | "aerostatos"
    aero_radio_km: float = 4.0
    notas: str = ""


class NuevaFuenteDatos(BaseModel):
    tipo: str                    # dem | uso_suelo | lidar | termico | multiespectral
    ruta_archivo: str
    notas: str = ""


# ---------------------------------------------------------------------------
# Arranque: asegura que la base de datos y su esquema existen
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    nm.inicializar_bd()


def get_conn():
    return nm.get_conn()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/catalogo")
def obtener_catalogo(_=None):
    conn = get_conn()
    fuel = [dict(zip(["codigo", "nombre", "grupo", "multiplicador", "descripcion"], row))
            for row in conn.execute("SELECT codigo, nombre, grupo, multiplicador, descripcion FROM fuel_models")]
    topo = [dict(zip(["codigo", "nombre", "multiplicador", "descripcion"], row))
            for row in conn.execute("SELECT codigo, nombre, multiplicador, descripcion FROM topografia_ref")]
    costes = [dict(zip(["item", "descripcion", "coste_usd", "unidad"], row))
              for row in conn.execute("SELECT item, descripcion, coste_usd, unidad FROM catalogo_costes")]
    conn.close()
    return {"fuel_models": fuel, "topografia": topo, "costes": costes}


@app.post("/proyectos")
def crear_proyecto(p: NuevoProyecto, authorization: Optional[str] = Header(None)):
    verificar_auth(authorization)
    conn = get_conn()
    try:
        pid, resultado = nm.crear_proyecto(
            conn, p.nombre, p.area_ha, p.pct_complejo, p.fuel_simple, p.fuel_complejo,
            p.topografia, p.spacing_base_m, p.estrategia, p.aero_radio_km, p.notas)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    return {"proyecto_id": pid, "resultado_manual": resultado}


@app.get("/proyectos")
def listar_proyectos(authorization: Optional[str] = Header(None)):
    verificar_auth(authorization)
    conn = get_conn()
    filas = nm.listar_proyectos(conn)
    conn.close()
    cols = ["id", "nombre", "fecha", "origen", "area_ha", "total_sensores", "gateways", "aerostatos", "coste_inicial"]
    return [dict(zip(cols, f)) for f in filas]


@app.get("/proyectos/{proyecto_id}")
def detalle_proyecto(proyecto_id: int, authorization: Optional[str] = Header(None)):
    verificar_auth(authorization)
    conn = get_conn()
    p = conn.execute("SELECT * FROM proyectos WHERE id=?", (proyecto_id,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    cols_p = [d[0] for d in conn.execute("SELECT * FROM proyectos WHERE id=?", (proyecto_id,)).description]
    r = conn.execute("SELECT * FROM resultados WHERE proyecto_id=?", (proyecto_id,)).fetchone()
    cols_r = [d[0] for d in conn.execute("SELECT * FROM resultados WHERE proyecto_id=?", (proyecto_id,)).description]
    fuentes = conn.execute("SELECT id, tipo, ruta_archivo, estado, notas FROM fuentes_datos WHERE proyecto_id=?",
                            (proyecto_id,)).fetchall()
    gis_runs = conn.execute(
        "SELECT fecha, n_sensores, n_gateways, n_aerostatos, coste_inicial, coste_anual, mapa_path, csv_path "
        "FROM resultados_gis WHERE proyecto_id=? ORDER BY id DESC", (proyecto_id,)).fetchall() \
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='resultados_gis'").fetchone() else []
    conn.close()
    return {
        "proyecto": dict(zip(cols_p, p)),
        "resultado_manual": dict(zip(cols_r, r)) if r else None,
        "fuentes_datos": [dict(zip(["id", "tipo", "ruta_archivo", "estado", "notas"], f)) for f in fuentes],
        "corridas_motor_real": [dict(zip(
            ["fecha", "n_sensores", "n_gateways", "n_aerostatos", "coste_inicial", "coste_anual", "mapa_path", "csv_path"],
            g)) for g in gis_runs],
    }


@app.post("/proyectos/{proyecto_id}/fuente-datos")
def registrar_fuente(proyecto_id: int, f: NuevaFuenteDatos, authorization: Optional[str] = Header(None)):
    verificar_auth(authorization)
    conn = get_conn()
    existe = conn.execute("SELECT 1 FROM proyectos WHERE id=?", (proyecto_id,)).fetchone()
    if not existe:
        conn.close()
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    nm.registrar_fuente_datos(conn, proyecto_id, f.tipo, f.ruta_archivo, f.notas)
    conn.close()
    return {"ok": True}


@app.post("/proyectos/{proyecto_id}/ejecutar")
def ejecutar_motor(proyecto_id: int, authorization: Optional[str] = Header(None)):
    """Corre el motor de optimizacion real (narsil_sistema.py) sobre el
    proyecto y devuelve el resultado + el mapa como imagen base64, para que
    el navegador lo pueda mostrar sin tener que descargar ningun archivo."""
    verificar_auth(authorization)
    try:
        proyecto = ns.cargar_proyecto_bd(proyecto_id, db_path=ns.DB_PATH)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    dem, landuse, cellsize = ns.generar_terreno_desde_proyecto(proyecto, n=120)
    complejidad, pendiente = ns.calcular_complejidad(dem, landuse, cellsize)
    sensores, gateways, aerostatos, zona_aero, covered = ns.optimizar_malla(
        complejidad, cellsize, spacing_base_m=proyecto["spacing_base_m"],
        aerostato_radio_km=proyecto.get("aero_radio_km") or 4.0)
    costes = ns.calcular_costes(sensores, gateways, aerostatos)

    mapa_path = f"/tmp/narsil_proyecto_{proyecto_id}_mapa.png"
    csv_path = f"/tmp/narsil_proyecto_{proyecto_id}_sensores.csv"
    ns.guardar_mapa(dem, landuse, sensores, gateways, aerostatos, cellsize, mapa_path)
    ns.exportar_csv(sensores, gateways, aerostatos, cellsize, csv_path)
    ns.guardar_resultado_gis_bd(proyecto_id, costes, mapa_path, csv_path, db_path=ns.DB_PATH)

    with open(mapa_path, "rb") as f:
        mapa_b64 = base64.b64encode(f.read()).decode("ascii")

    return {
        "proyecto_id": proyecto_id,
        "pendiente_media_pct": round(float(pendiente.mean()), 1),
        "pendiente_max_pct": round(float(pendiente.max()), 1),
        "resultado": costes,
        "mapa_base64": f"data:image/png;base64,{mapa_b64}",
    }


@app.get("/salud")
def salud():
    return {"status": "ok"}
