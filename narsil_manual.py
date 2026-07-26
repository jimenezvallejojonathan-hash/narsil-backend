"""
NARSIL - Modulo de entrada manual y base de datos de referencia
===================================================================
Consola interactiva para crear estimaciones HIPOTETICAS (no verificadas)
de mallas de sensores forestales introduciendo todos los parametros a mano,
respaldada por una base de datos SQLite de referencia (modelos de
combustible Rothermel/Scott-Burgan, catalogo de costes de equipos).

Este modulo es el complemento manual de narsil_sistema.py (modo real con
GIS/DEM/dron). Comparten la misma base de datos: cada proyecto manual queda
marcado como origen "manual" y, cuando en el futuro se disponga de capas GIS
o telemetria de dron reales para esa misma zona, se pueden vincular al mismo
proyecto y marcarlo como origen "verificado", sin perder el historial.

Uso:
    python3 narsil_manual.py            -> abre el menu interactivo
    python3 narsil_manual.py --demo     -> crea un proyecto de ejemplo
                                            sin pedir datos por teclado
                                            (para pruebas / verificacion)

Base de datos: narsil.db (SQLite, se crea junto a este script si no existe)
"""

import sqlite3
import argparse
import math
import csv
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narsil.db")


# ---------------------------------------------------------------------------
# 1. ESQUEMA Y DATOS DE REFERENCIA
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS fuel_models (
    codigo TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    grupo TEXT NOT NULL,
    multiplicador REAL NOT NULL,
    descripcion TEXT
);

CREATE TABLE IF NOT EXISTS topografia_ref (
    codigo TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    multiplicador REAL NOT NULL,
    descripcion TEXT
);

CREATE TABLE IF NOT EXISTS catalogo_costes (
    item TEXT PRIMARY KEY,
    descripcion TEXT,
    coste_usd REAL NOT NULL,
    unidad TEXT,
    actualizado TEXT
);

CREATE TABLE IF NOT EXISTS proyectos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    fecha TEXT NOT NULL,
    origen TEXT NOT NULL DEFAULT 'manual',
    area_ha REAL,
    pct_complejo REAL,
    fuel_simple TEXT,
    fuel_complejo TEXT,
    topografia TEXT,
    spacing_base_m REAL,
    estrategia TEXT,
    aero_radio_km REAL,
    notas TEXT
);

CREATE TABLE IF NOT EXISTS resultados (
    proyecto_id INTEGER PRIMARY KEY,
    sensores_simple INTEGER,
    sensores_complejo INTEGER,
    aerostatos INTEGER,
    gateways INTEGER,
    coste_inicial REAL,
    coste_anual REAL,
    FOREIGN KEY(proyecto_id) REFERENCES proyectos(id)
);

CREATE TABLE IF NOT EXISTS fuentes_datos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proyecto_id INTEGER NOT NULL,
    tipo TEXT NOT NULL,
    ruta_archivo TEXT,
    fecha_captura TEXT,
    estado TEXT DEFAULT 'pendiente',
    notas TEXT,
    FOREIGN KEY(proyecto_id) REFERENCES proyectos(id)
);
"""

FUEL_MODELS_SEED = [
    ("GR1", "Pasto bajo",               "simple",   1.5,  "Rothermel GR1 - pasto corto, terreno abierto"),
    ("GR2", "Pasto alto",               "simple",   1.2,  "Rothermel GR2 - pasto denso/alto"),
    ("SH1", "Matorral bajo disperso",   "simple",   1.0,  "Scott-Burgan SH1 - matorral ralo"),
    ("SH5", "Matorral denso",           "complejo", 1.0,  "Scott-Burgan SH5 - matorral denso, carga media"),
    ("TL5", "Encinar / dehesa",         "complejo", 0.65, "Scott-Burgan TL5 - hojarasca latifolias"),
    ("TU5", "Pinar con sotobosque",     "complejo", 0.6,  "Scott-Burgan TU5 - coniferas + sotobosque"),
    ("SH7", "Eucaliptal denso",         "complejo", 0.5,  "Scott-Burgan SH7 - matorral/arbolado denso, alta carga"),
    ("NB9", "Rocoso / pendiente fuerte","complejo", 0.45, "Terreno no combustible pero con obstaculos de senal"),
]

TOPOGRAFIA_SEED = [
    ("REG", "Regular",          1.0, "Pendiente < 10%, sin obstaculos de linea de vista"),
    ("OND", "Ondulado",         0.8, "Pendiente 10-30%, obstaculos ocasionales"),
    ("ESC", "Escarpado (>30%)", 0.6, "Pendiente > 30%, perdida frecuente de linea de vista"),
]

COSTES_SEED = [
    ("sensor",              "Sensor ambiental (temp/humedad/gas) tipo Silvanet", 120,   "USD/ud"),
    ("gateway",             "Gateway LoRaWAN + terminal Starlink",               850,   "USD/ud"),
    ("aerostato",           "Aerostato cautivo con camara termica",              15000, "USD/ud"),
    ("aerostato_mes",       "Mantenimiento + operacion aerostato",               500,   "USD/mes/ud"),
    ("instalacion_sensor",  "Mano de obra instalacion por sensor",               15,    "USD/ud"),
    ("instalacion_gateway", "Mano de obra instalacion por gateway",              200,   "USD/ud"),
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicializar_bd():
    conn = get_conn()
    conn.executescript(SCHEMA)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fuel_models")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO fuel_models (codigo, nombre, grupo, multiplicador, descripcion) VALUES (?,?,?,?,?)",
            FUEL_MODELS_SEED)
    cur.execute("SELECT COUNT(*) FROM topografia_ref")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO topografia_ref (codigo, nombre, multiplicador, descripcion) VALUES (?,?,?,?)",
            TOPOGRAFIA_SEED)
    cur.execute("SELECT COUNT(*) FROM catalogo_costes")
    if cur.fetchone()[0] == 0:
        hoy = datetime.now().strftime("%Y-%m-%d")
        cur.executemany(
            "INSERT INTO catalogo_costes (item, descripcion, coste_usd, unidad, actualizado) VALUES (?,?,?,?,?)",
            [(i, d, c, u, hoy) for (i, d, c, u) in COSTES_SEED])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 2. LOGICA DE CALCULO (misma que el simulador manual, ahora respaldada por BD)
# ---------------------------------------------------------------------------
def calcular_estimacion(area_ha, pct_complejo, fuel_simple_mult, fuel_complejo_mult,
                         topo_mult, spacing_base_m, estrategia, costes,
                         aero_radio_km=4.0, gw_radio_km=2.0):
    area_complex_ha = area_ha * pct_complejo
    area_simple_ha = area_ha * (1 - pct_complejo)

    spacing_simple = spacing_base_m * fuel_simple_mult
    spacing_complex = spacing_base_m * fuel_complejo_mult * topo_mult
    if estrategia == "aerostatos":
        spacing_complex *= 3

    sensores_simple = max(0, math.ceil((area_simple_ha * 10000) / (spacing_simple ** 2))) if area_simple_ha > 0 else 0
    sensores_complejo = max(0, math.ceil((area_complex_ha * 10000) / (spacing_complex ** 2))) if area_complex_ha > 0 else 0

    aerostatos = 0
    if estrategia == "aerostatos" and area_complex_ha > 0:
        coverage_ha = math.pi * aero_radio_km ** 2 * 100
        aerostatos = max(1, math.ceil(area_complex_ha / coverage_ha))

    gw_coverage_ha = math.pi * gw_radio_km ** 2 * 100
    gateways = max(1, math.ceil(area_ha / gw_coverage_ha))

    coste_inicial = (
        sensores_simple * costes["sensor"]
        + sensores_complejo * costes["sensor"]
        + aerostatos * costes["aerostato"]
        + gateways * costes["gateway"]
        + (sensores_simple + sensores_complejo) * costes.get("instalacion_sensor", 0)
        + gateways * costes.get("instalacion_gateway", 0)
    )
    coste_anual = aerostatos * costes["aerostato_mes"] * 12

    return dict(
        sensores_simple=sensores_simple, sensores_complejo=sensores_complejo,
        aerostatos=aerostatos, gateways=gateways,
        coste_inicial=round(coste_inicial, 2), coste_anual=round(coste_anual, 2),
    )


def costes_dict_desde_bd(conn):
    cur = conn.execute("SELECT item, coste_usd FROM catalogo_costes")
    return {item: coste for item, coste in cur.fetchall()}


# ---------------------------------------------------------------------------
# 3. OPERACIONES DE PROYECTO
# ---------------------------------------------------------------------------
def crear_proyecto(conn, nombre, area_ha, pct_complejo, fuel_simple_cod, fuel_complejo_cod,
                    topo_cod, spacing_base_m, estrategia, aero_radio_km=4.0, notas=""):
    cur = conn.cursor()
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute("""INSERT INTO proyectos
        (nombre, fecha, origen, area_ha, pct_complejo, fuel_simple, fuel_complejo,
         topografia, spacing_base_m, estrategia, aero_radio_km, notas)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (nombre, fecha, "manual", area_ha, pct_complejo, fuel_simple_cod, fuel_complejo_cod,
         topo_cod, spacing_base_m, estrategia, aero_radio_km, notas))
    proyecto_id = cur.lastrowid

    fuel_simple_mult = conn.execute(
        "SELECT multiplicador FROM fuel_models WHERE codigo=?", (fuel_simple_cod,)).fetchone()[0]
    fuel_complejo_mult = conn.execute(
        "SELECT multiplicador FROM fuel_models WHERE codigo=?", (fuel_complejo_cod,)).fetchone()[0]
    topo_mult = conn.execute(
        "SELECT multiplicador FROM topografia_ref WHERE codigo=?", (topo_cod,)).fetchone()[0]

    costes = costes_dict_desde_bd(conn)
    resultado = calcular_estimacion(area_ha, pct_complejo, fuel_simple_mult, fuel_complejo_mult,
                                     topo_mult, spacing_base_m, estrategia, costes, aero_radio_km)

    cur.execute("""INSERT INTO resultados
        (proyecto_id, sensores_simple, sensores_complejo, aerostatos, gateways, coste_inicial, coste_anual)
        VALUES (?,?,?,?,?,?,?)""",
        (proyecto_id, resultado["sensores_simple"], resultado["sensores_complejo"],
         resultado["aerostatos"], resultado["gateways"], resultado["coste_inicial"], resultado["coste_anual"]))
    conn.commit()
    return proyecto_id, resultado


def registrar_fuente_datos(conn, proyecto_id, tipo, ruta_archivo, notas=""):
    cur = conn.cursor()
    cur.execute("""INSERT INTO fuentes_datos (proyecto_id, tipo, ruta_archivo, fecha_captura, estado, notas)
        VALUES (?,?,?,?,?,?)""",
        (proyecto_id, tipo, ruta_archivo, datetime.now().strftime("%Y-%m-%d"), "pendiente", notas))
    conn.execute("UPDATE proyectos SET origen='verificado_gis (parcial)' WHERE id=? AND origen='manual'",
                 (proyecto_id,))
    conn.commit()


def listar_proyectos(conn):
    return conn.execute("""SELECT p.id, p.nombre, p.fecha, p.origen, p.area_ha,
        r.sensores_simple + r.sensores_complejo AS total_sensores,
        r.gateways, r.aerostatos, r.coste_inicial
        FROM proyectos p LEFT JOIN resultados r ON r.proyecto_id = p.id
        ORDER BY p.id DESC""").fetchall()


def exportar_informe(conn, proyecto_id, out_path):
    p = conn.execute("SELECT * FROM proyectos WHERE id=?", (proyecto_id,)).fetchone()
    cols_p = [d[0] for d in conn.execute("SELECT * FROM proyectos WHERE id=?", (proyecto_id,)).description]
    r = conn.execute("SELECT * FROM resultados WHERE proyecto_id=?", (proyecto_id,)).fetchone()
    cols_r = [d[0] for d in conn.execute("SELECT * FROM resultados WHERE proyecto_id=?", (proyecto_id,)).description]
    fuentes = conn.execute("SELECT tipo, ruta_archivo, estado FROM fuentes_datos WHERE proyecto_id=?",
                            (proyecto_id,)).fetchall()
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["campo", "valor"])
        for c, v in zip(cols_p, p):
            w.writerow([c, v])
        for c, v in zip(cols_r, r):
            w.writerow([c, v])
        w.writerow([])
        w.writerow(["fuentes_datos_tipo", "ruta", "estado"])
        for f_ in fuentes:
            w.writerow(f_)


# ---------------------------------------------------------------------------
# 4. CONSOLA INTERACTIVA
# ---------------------------------------------------------------------------
def pedir_float(msg, default=None):
    s = input(f"{msg}" + (f" [{default}]: " if default is not None else ": ")).strip()
    if s == "" and default is not None:
        return float(default)
    return float(s)


def pedir_opcion(msg, opciones):
    print(msg)
    for cod, nombre, *_ in opciones:
        print(f"  {cod} - {nombre}")
    while True:
        s = input("Codigo: ").strip().upper()
        if s in [o[0] for o in opciones]:
            return s
        print("Codigo no valido, intenta de nuevo.")


def menu_ver_catalogo(conn):
    print("\n-- Modelos de combustible (Rothermel/Scott-Burgan) --")
    for row in conn.execute("SELECT codigo, nombre, grupo, multiplicador, descripcion FROM fuel_models"):
        print(f"  [{row[0]}] {row[1]} ({row[2]}) x{row[3]} - {row[4]}")
    print("\n-- Topografia --")
    for row in conn.execute("SELECT codigo, nombre, multiplicador, descripcion FROM topografia_ref"):
        print(f"  [{row[0]}] {row[1]} x{row[2]} - {row[3]}")
    print("\n-- Catalogo de costes --")
    for row in conn.execute("SELECT item, descripcion, coste_usd, unidad FROM catalogo_costes"):
        print(f"  {row[0]}: {row[2]} {row[3]} - {row[1]}")


def menu_editar_coste(conn):
    item = input("Item a editar (ver catalogo con opcion 1): ").strip()
    existe = conn.execute("SELECT 1 FROM catalogo_costes WHERE item=?", (item,)).fetchone()
    if not existe:
        print("Ese item no existe en el catalogo.")
        return
    nuevo = pedir_float("Nuevo coste USD")
    conn.execute("UPDATE catalogo_costes SET coste_usd=?, actualizado=? WHERE item=?",
                 (nuevo, datetime.now().strftime("%Y-%m-%d"), item))
    conn.commit()
    print("Actualizado.")


def menu_nuevo_proyecto(conn):
    nombre = input("Nombre del proyecto: ").strip()
    area_ha = pedir_float("Superficie total (ha)")
    pct_complejo = pedir_float("Porcentaje de zona compleja (0-1, ej 0.5)", 0.5)
    fuel_simple = pedir_opcion("Modelo de combustible zona simple:",
        conn.execute("SELECT codigo, nombre FROM fuel_models WHERE grupo='simple'").fetchall())
    fuel_complejo = pedir_opcion("Modelo de combustible zona compleja:",
        conn.execute("SELECT codigo, nombre FROM fuel_models WHERE grupo='complejo'").fetchall())
    topo = pedir_opcion("Topografia:", conn.execute("SELECT codigo, nombre FROM topografia_ref").fetchall())
    spacing = pedir_float("Separacion base de sensores (m)", 100)
    estrategia = input("Estrategia zona compleja [densa/aerostatos] (densa): ").strip().lower() or "densa"
    aero_radio = 4.0
    if estrategia == "aerostatos":
        aero_radio = pedir_float("Radio de cobertura por aerostato (km)", 4.0)
    notas = input("Notas (opcional): ").strip()

    pid, resultado = crear_proyecto(conn, nombre, area_ha, pct_complejo, fuel_simple, fuel_complejo,
                                     topo, spacing, estrategia, aero_radio, notas)
    print(f"\nProyecto #{pid} creado. Resultado:")
    for k, v in resultado.items():
        print(f"  {k}: {v}")
    return pid


def menu_listar(conn):
    filas = listar_proyectos(conn)
    print("\nID  Nombre               Fecha            Origen              Area(ha)  Sensores  GW  Aero  Coste inicial")
    for f in filas:
        print(f"{f[0]:<3} {str(f[1])[:20]:<20} {f[2]:<16} {f[3]:<19} {f[4]:<9} {f[5]:<9} {f[6]:<3} {f[7]:<5} {f[8]}")


def menu_registrar_fuente(conn):
    pid = int(input("ID del proyecto al que vincular la fuente de datos: ").strip())
    tipo = input("Tipo [dem/uso_suelo/lidar/termico/multiespectral]: ").strip()
    ruta = input("Ruta del archivo (o descripcion si aun no existe): ").strip()
    notas = input("Notas: ").strip()
    registrar_fuente_datos(conn, pid, tipo, ruta, notas)
    print("Fuente registrada. El proyecto queda marcado como 'verificado_gis (parcial)'.")


def menu_exportar(conn):
    pid = int(input("ID del proyecto a exportar: ").strip())
    out = f"/mnt/user-data/outputs/narsil_proyecto_{pid}.csv"
    exportar_informe(conn, pid, out)
    print(f"Informe exportado a {out}")


def menu_principal():
    inicializar_bd()
    conn = get_conn()
    opciones = {
        "1": ("Ver catalogo de referencia (combustible, topografia, costes)", menu_ver_catalogo),
        "2": ("Editar un coste del catalogo", menu_editar_coste),
        "3": ("Crear nuevo proyecto (estimacion manual)", menu_nuevo_proyecto),
        "4": ("Listar proyectos guardados", menu_listar),
        "5": ("Registrar fuente de datos GIS/dron para un proyecto", menu_registrar_fuente),
        "6": ("Exportar informe de un proyecto a CSV", menu_exportar),
        "0": ("Salir", None),
    }
    while True:
        print("\n===== NARSIL - modulo manual =====")
        for k, (desc, _) in opciones.items():
            print(f"  {k}. {desc}")
        eleccion = input("Elige opcion: ").strip()
        if eleccion == "0":
            break
        accion = opciones.get(eleccion)
        if not accion:
            print("Opcion no valida.")
            continue
        try:
            accion[1](conn)
        except Exception as e:
            print(f"Error: {e}")
    conn.close()


def modo_demo():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    inicializar_bd()
    conn = get_conn()
    print("== NARSIL modulo manual: proyecto de ejemplo (no interactivo) ==\n")
    pid, resultado = crear_proyecto(
        conn, nombre="Finca ejemplo - ladera norte", area_ha=350, pct_complejo=0.6,
        fuel_simple_cod="GR2", fuel_complejo_cod="TU5", topo_cod="OND",
        spacing_base_m=100, estrategia="aerostatos", aero_radio_km=4.0,
        notas="Estimacion hipotetica, datos no verificados")
    print(f"Proyecto #{pid} creado.")
    for k, v in resultado.items():
        print(f"  {k}: {v}")

    registrar_fuente_datos(conn, pid, "dem", "(pendiente de subir DEM real)",
                            notas="A la espera de vuelo LiDAR")
    print("\nFuente de datos GIS registrada (estado: pendiente).")

    menu_listar(conn)
    out = "/mnt/user-data/outputs/narsil_proyecto_demo.csv"
    exportar_informe(conn, pid, out)
    print(f"\nInforme exportado a {out}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="crea un proyecto de ejemplo sin pedir datos por teclado")
    args = parser.parse_args()
    if args.demo:
        modo_demo()
    else:
        menu_principal()
