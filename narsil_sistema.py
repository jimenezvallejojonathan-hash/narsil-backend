"""
NARSIL - Sistema de optimizacion de mallas de sensores forestales
====================================================================
Modo "datos reales": ingiere DEM (elevacion) y capa de uso de suelo/vegetacion
para calcular automaticamente donde colocar sensores, gateways LoRaWAN+Starlink
y aerostatos, en funcion de la pendiente real y el modelo de combustible
Rothermel/Scott-Burgan de cada celda del terreno.

Este script funciona en dos modos:
  1) MODO REAL: si le pasas un DEM (GeoTIFF) y una capa de uso de suelo
     (GeoTIFF con codigos de clase), usa rasterio para leerlos.
  2) MODO DEMO: si no hay archivos reales, genera un terreno sintetico de
     ejemplo para que puedas ver el sistema funcionando de principio a fin.

IMPORTANTE: el modo demo es una simulacion con datos inventados, NO una
estimacion real de ningun terreno. Sirve para validar la logica del sistema
antes de conectarlo a datos GIS/dron verificados.

Dependencias: numpy, scipy, matplotlib (incluidas). rasterio es opcional,
solo se necesita para leer GeoTIFFs reales (pip install rasterio).
"""

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import csv
import os
import sqlite3
import argparse
from datetime import datetime

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

# Base de datos compartida con narsil_manual.py (mismo directorio por defecto)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narsil.db")

# Puente entre los codigos de la BD (GR1, TU5...) y las claves de
# FUEL_MULTIPLIER usadas mas abajo en este script (misma escala de valores)
CODE_TO_FUELNAME = {
    "GR1": "pasto_bajo", "GR2": "pasto_alto", "SH1": "matorral_disperso",
    "SH5": "matorral_denso", "TL5": "encinar", "TU5": "pinar",
    "SH7": "eucaliptal", "NB9": "rocoso",
}

# ---------------------------------------------------------------------------
# Modelos de combustible Rothermel/Scott-Burgan -> multiplicador de espaciado
# (mismo criterio usado en el simulador manual: >1 = se puede espaciar mas,
#  <1 = hace falta mas densidad de sensores)
# ---------------------------------------------------------------------------
FUEL_MULTIPLIER = {
    "pasto_bajo":        1.5,   # GR1
    "pasto_alto":        1.2,   # GR2
    "matorral_disperso":  1.0,   # SH1
    "matorral_denso":    1.0,   # SH5
    "encinar":           0.65,  # TL5
    "pinar":              0.6,   # TU5
    "eucaliptal":        0.5,   # SH7
    "rocoso":             0.45,
}

DEFAULT_COSTS = dict(
    sensor=120,        # USD por sensor
    gateway=850,       # USD por gateway + terminal Starlink
    aerostato=15000,   # USD por aerostato
    aerostato_mes=500, # USD/mes mantenimiento + operacion por aerostato
)


# ---------------------------------------------------------------------------
# 1. CARGA DE DATOS
# ---------------------------------------------------------------------------
def cargar_datos_reales(dem_path, landuse_path, landuse_map):
    """Lee un DEM y una capa de uso de suelo reales via rasterio.
    landuse_map: dict {codigo_raster: nombre_clase_fuel_model}
    """
    if not HAS_RASTERIO:
        raise RuntimeError("rasterio no esta instalado. pip install rasterio --break-system-packages")
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(float)
        cellsize = src.res[0]
        transform = src.transform
    with rasterio.open(landuse_path) as src:
        landuse_codes = src.read(1)
    landuse = np.empty(landuse_codes.shape, dtype=object)
    for code, name in landuse_map.items():
        landuse[landuse_codes == code] = name
    landuse[landuse == None] = "matorral_denso"  # valor por defecto si falta codigo
    return dem, landuse, cellsize, transform


def cargar_proyecto_bd(proyecto_id, db_path=DB_PATH):
    """Lee un proyecto creado con narsil_manual.py de la base de datos
    compartida. Devuelve un dict con todos sus parametros."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT nombre, area_ha, pct_complejo, fuel_simple, fuel_complejo, "
        "topografia, spacing_base_m, estrategia, aero_radio_km FROM proyectos "
        "WHERE id=?", (proyecto_id,)).fetchone()
    conn.close()
    if row is None:
        raise ValueError(f"No existe el proyecto #{proyecto_id} en {db_path}")
    nombre, area_ha, pct_complejo, fuel_simple, fuel_complejo, topo, spacing, estrategia, aero_radio = row
    return dict(nombre=nombre, area_ha=area_ha, pct_complejo=pct_complejo,
                fuel_simple=fuel_simple, fuel_complejo=fuel_complejo,
                topografia=topo, spacing_base_m=spacing, estrategia=estrategia,
                aero_radio_km=aero_radio)


def generar_terreno_desde_proyecto(proyecto, n=120, seed=7):
    """Genera un terreno SINTETICO cuya mezcla de vegetacion y pendiente
    respeta los parametros elegidos a mano en el proyecto de la BD
    (pct_complejo, modelos de combustible simple/complejo, topografia).
    Sigue sin ser un terreno real: es el puente entre la estimacion manual
    y el motor de optimizacion, hasta que haya un DEM/uso de suelo real que
    cargar con cargar_datos_reales().
    """
    rng = np.random.default_rng(seed)
    cellsize = 20
    topo_amplitud = {"REG": 15, "OND": 35, "ESC": 70}.get(proyecto["topografia"], 35)
    x = np.linspace(0, 6, n)
    y = np.linspace(0, 6, n)
    xx, yy = np.meshgrid(x, y)
    dem = (
        300
        + topo_amplitud * np.sin(xx) * np.cos(yy * 0.7)
        + topo_amplitud * 0.5 * np.sin(xx * 2.3 + yy)
        + rng.normal(0, 2, (n, n))
    )

    fuel_simple_nombre = CODE_TO_FUELNAME.get(proyecto["fuel_simple"], "pasto_bajo")
    fuel_complejo_nombre = CODE_TO_FUELNAME.get(proyecto["fuel_complejo"], "matorral_denso")

    landuse = np.empty((n, n), dtype=object)
    pct = proyecto["pct_complejo"]
    # banda de zona compleja proporcional a pct_complejo, resto zona simple
    for i in range(n):
        for j in range(n):
            r = np.sqrt((i - n * 0.5) ** 2 + (j - n * 0.5) ** 2) / (n * 0.7)
            landuse[i, j] = fuel_complejo_nombre if r < pct else fuel_simple_nombre

    cellsize = 20
    return dem, landuse, cellsize


def generar_terreno_demo(n=120, cellsize=20, seed=7):
    """Genera un terreno SINTETICO de ejemplo (NO real) para probar el sistema.
    n x n celdas de 'cellsize' metros -> area total = (n*cellsize/100)^2 ... ha
    """
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 6, n)
    y = np.linspace(0, 6, n)
    xx, yy = np.meshgrid(x, y)
    dem = (
        300
        + 35 * np.sin(xx) * np.cos(yy * 0.7)
        + 18 * np.sin(xx * 2.3 + yy)
        + rng.normal(0, 2, (n, n))
    )
    # zonas de uso de suelo: banda central = pasto, resto mezcla de arbolado
    landuse = np.empty((n, n), dtype=object)
    for i in range(n):
        for j in range(n):
            r = np.sqrt((i - n * 0.3) ** 2 + (j - n * 0.7) ** 2)
            if r < n * 0.22:
                landuse[i, j] = "pasto_bajo"
            elif (i + j) % 37 < 12:
                landuse[i, j] = "pinar"
            elif (i * 2 + j) % 41 < 10:
                landuse[i, j] = "encinar"
            elif (i - j) % 29 < 8:
                landuse[i, j] = "eucaliptal"
            else:
                landuse[i, j] = "matorral_denso"
    return dem, landuse, cellsize


# ---------------------------------------------------------------------------
# 2. CALCULO DE COMPLEJIDAD DEL TERRENO
# ---------------------------------------------------------------------------
def calcular_complejidad(dem, landuse, cellsize):
    gy, gx = np.gradient(dem, cellsize)
    pendiente_pct = np.sqrt(gx ** 2 + gy ** 2) * 100
    slope_factor = np.clip(1.0 - pendiente_pct / 100.0, 0.35, 1.0)

    fuel_mult = np.vectorize(lambda c: FUEL_MULTIPLIER.get(c, 0.8))(landuse)
    complejidad = fuel_mult * slope_factor  # 0-1.5 aprox; mas bajo = mas dificil
    return complejidad, pendiente_pct


# ---------------------------------------------------------------------------
# 3. OPTIMIZACION DE LA MALLA (greedy set-cover con radio variable)
# ---------------------------------------------------------------------------
def optimizar_malla(complejidad, cellsize, spacing_base_m=100,
                     candidate_step=4, aerostato_umbral=0.55,
                     aerostato_radio_km=4.0, target_coverage=0.97):
    n = complejidad.shape[0]
    covered = np.zeros_like(complejidad, dtype=bool)

    # radio de cobertura de cada celda candidata en numero de celdas
    radio_m = spacing_base_m * complejidad / 1.5  # normalizado
    radio_celdas = np.clip((radio_m / cellsize) / 2, 1, None)

    # zonas tan complejas que se recomienda aerostato en vez de malla densa
    zona_aero = complejidad < aerostato_umbral

    candidatos = [(i, j) for i in range(0, n, candidate_step)
                  for j in range(0, n, candidate_step) if not zona_aero[i, j]]

    sensores = []
    total_celdas = np.sum(~zona_aero)
    max_iter = len(candidatos)
    it = 0
    while candidatos and it < max_iter:
        it += 1
        best, best_gain, best_mask = None, -1, None
        for (i, j) in candidatos:
            r = int(round(radio_celdas[i, j]))
            i0, i1 = max(0, i - r), min(n, i + r + 1)
            j0, j1 = max(0, j - r), min(n, j + r + 1)
            mask = ~covered[i0:i1, j0:j1] & ~zona_aero[i0:i1, j0:j1]
            gain = mask.sum()
            if gain > best_gain:
                best, best_gain, best_mask = (i, j, i0, i1, j0, j1), gain, mask
        if best is None or best_gain <= 0:
            break
        i, j, i0, i1, j0, j1 = best
        covered[i0:i1, j0:j1][best_mask] = True
        sensores.append((i, j))
        candidatos.remove((i, j))
        if covered[~zona_aero].sum() / max(total_celdas, 1) >= target_coverage:
            break

    # aerostatos: numero total proporcional al area compleja, repartidos
    # espacialmente por k-means (no por componente conexa, para evitar
    # fragmentar la zona en cientos de micro-regiones)
    aerostatos = []
    if zona_aero.any():
        from scipy.cluster.vq import kmeans2
        coords = np.column_stack(np.where(zona_aero)).astype(float)
        area_complex_ha = len(coords) * (cellsize ** 2) / 10000
        coverage_ha = np.pi * aerostato_radio_km ** 2 * 100
        n_clusters = max(1, int(np.ceil(area_complex_ha / coverage_ha)))
        n_clusters = min(n_clusters, len(coords))
        centroids, _ = kmeans2(coords, n_clusters, minit="++", seed=7)
        aerostatos = [(c[0], c[1], 1) for c in centroids]

    # gateways: clustering simple de los sensores (grid de 2km por defecto)
    gw_radio_celdas = 2000 / cellsize
    gateways = []
    sensores_restantes = set(sensores)
    while sensores_restantes:
        p = next(iter(sensores_restantes))
        cercanos = [s for s in sensores_restantes
                    if np.hypot(s[0] - p[0], s[1] - p[1]) <= gw_radio_celdas]
        gy_, gx_ = np.mean([s[0] for s in cercanos]), np.mean([s[1] for s in cercanos])
        gateways.append((gy_, gx_))
        sensores_restantes -= set(cercanos)

    return sensores, gateways, aerostatos, zona_aero, covered


# ---------------------------------------------------------------------------
# 4. INFORME DE COSTES
# ---------------------------------------------------------------------------
def calcular_costes(sensores, gateways, aerostatos, costs=DEFAULT_COSTS):
    n_aero = sum(a[2] for a in aerostatos)
    upfront = (len(sensores) * costs["sensor"]
               + len(gateways) * costs["gateway"]
               + n_aero * costs["aerostato"])
    anual = n_aero * costs["aerostato_mes"] * 12
    return dict(n_sensores=len(sensores), n_gateways=len(gateways),
                n_aerostatos=n_aero, coste_inicial=upfront, coste_anual=anual)


# ---------------------------------------------------------------------------
# 5. VISUALIZACION Y EXPORTACION
# ---------------------------------------------------------------------------
COLOR_LANDUSE = {
    "pasto_bajo": "#c0dd97", "pasto_alto": "#97c459",
    "matorral_disperso": "#fac775", "matorral_denso": "#ef9f27",
    "encinar": "#639922", "pinar": "#0f6e56",
    "eucaliptal": "#994a1a", "rocoso": "#888780",
}

def guardar_mapa(dem, landuse, sensores, gateways, aerostatos, cellsize, out_png):
    n = landuse.shape[0]
    rgb = np.zeros((n, n, 3))
    for cls, hexcol in COLOR_LANDUSE.items():
        h = hexcol.lstrip("#")
        rgb_val = tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
        rgb[landuse == cls] = rgb_val

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.imshow(rgb, origin="upper")
    ax.contour(dem, colors="black", alpha=0.15, linewidths=0.5, levels=8)

    if sensores:
        sy, sx = zip(*sensores)
        ax.scatter(sx, sy, c="#185fa5", s=14, label="Sensor", zorder=5)
    if gateways:
        gy, gx = zip(*gateways)
        ax.scatter(gx, gy, c="#e34948", marker="^", s=90, label="Gateway/Starlink", zorder=6)
    for (ay, ax_, cnt) in aerostatos:
        ax.add_patch(Circle((ax_, ay), radius=8, fill=False, edgecolor="#534ab7", linewidth=1.5))
        ax.scatter([ax_], [ay], c="#534ab7", marker="*", s=160, zorder=7,
                   label="Aerostato" if (ay, ax_, cnt) == aerostatos[0] else None)

    ax.set_title("NARSIL - malla optimizada (DEMO, datos sinteticos)")
    ax.legend(loc="upper right", fontsize=8)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def exportar_csv(sensores, gateways, aerostatos, cellsize, out_csv):
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tipo", "fila", "columna", "x_m", "y_m", "n_unidades"])
        for (i, j) in sensores:
            w.writerow(["sensor", i, j, j * cellsize, i * cellsize, 1])
        for (i, j) in gateways:
            w.writerow(["gateway", round(i, 1), round(j, 1), j * cellsize, i * cellsize, 1])
        for (i, j, n) in aerostatos:
            w.writerow(["aerostato", round(i, 1), round(j, 1), j * cellsize, i * cellsize, n])


def guardar_resultado_gis_bd(proyecto_id, costes, mapa_path, csv_path, db_path=DB_PATH):
    """Guarda el resultado de una corrida del motor de optimizacion (real o
    sintetica-seeded) vinculada al proyecto manual original, y marca sus
    fuentes de datos pendientes como 'procesado'."""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS resultados_gis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proyecto_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        n_sensores INTEGER, n_gateways INTEGER, n_aerostatos INTEGER,
        coste_inicial REAL, coste_anual REAL,
        mapa_path TEXT, csv_path TEXT,
        FOREIGN KEY(proyecto_id) REFERENCES proyectos(id)
    )""")
    conn.execute("""INSERT INTO resultados_gis
        (proyecto_id, fecha, n_sensores, n_gateways, n_aerostatos, coste_inicial, coste_anual, mapa_path, csv_path)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (proyecto_id, datetime.now().strftime("%Y-%m-%d %H:%M"),
         costes["n_sensores"], costes["n_gateways"], costes["n_aerostatos"],
         costes["coste_inicial"], costes["coste_anual"], mapa_path, csv_path))
    conn.execute("UPDATE fuentes_datos SET estado='procesado' WHERE proyecto_id=? AND estado='pendiente'",
                 (proyecto_id,))
    conn.commit()
    conn.close()


def ejecutar_para_proyecto(proyecto_id, db_path=DB_PATH):
    """Pipeline completo: lee un proyecto manual de la BD, genera el terreno
    (sintetico-seeded o real si ya hay rasterio+archivos conectados),
    optimiza la malla y guarda mapa+csv+registro de vuelta en la BD."""
    proyecto = cargar_proyecto_bd(proyecto_id, db_path)
    print(f"Proyecto #{proyecto_id}: {proyecto['nombre']}  ({proyecto['area_ha']} ha)")

    dem, landuse, cellsize = generar_terreno_desde_proyecto(proyecto, n=120)
    complejidad, pendiente = calcular_complejidad(dem, landuse, cellsize)
    print(f"Pendiente media: {pendiente.mean():.1f}%  (max {pendiente.max():.1f}%)")

    sensores, gateways, aerostatos, zona_aero, covered = optimizar_malla(
        complejidad, cellsize, spacing_base_m=proyecto["spacing_base_m"],
        aerostato_radio_km=proyecto.get("aero_radio_km") or 4.0)

    costes = calcular_costes(sensores, gateways, aerostatos)
    print("Resultado del motor de optimizacion (vs. estimacion manual original):")
    for k, v in costes.items():
        print(f"  {k}: {v}")

    mapa_path = f"/mnt/user-data/outputs/narsil_proyecto_{proyecto_id}_mapa.png"
    csv_path = f"/mnt/user-data/outputs/narsil_proyecto_{proyecto_id}_sensores.csv"
    guardar_mapa(dem, landuse, sensores, gateways, aerostatos, cellsize, mapa_path)
    exportar_csv(sensores, gateways, aerostatos, cellsize, csv_path)
    guardar_resultado_gis_bd(proyecto_id, costes, mapa_path, csv_path, db_path)
    print(f"\nMapa: {mapa_path}\nCSV: {csv_path}\nResultado vinculado al proyecto #{proyecto_id} en {db_path}")


# ---------------------------------------------------------------------------
# EJECUCION DEMO
# ---------------------------------------------------------------------------
def main_demo_generico():
    print("NARSIL - modo demo (terreno sintetico, NO datos reales verificados)\n")

    dem, landuse, cellsize = generar_terreno_demo(n=120, cellsize=20)
    area_ha = (dem.shape[0] * cellsize / 100) * (dem.shape[1] * cellsize / 100)
    print(f"Terreno sintetico: {dem.shape[0]}x{dem.shape[1]} celdas de {cellsize}m -> {area_ha:.0f} ha")

    complejidad, pendiente = calcular_complejidad(dem, landuse, cellsize)
    print(f"Pendiente media sintetica: {pendiente.mean():.1f}%  (max {pendiente.max():.1f}%)")

    sensores, gateways, aerostatos, zona_aero, covered = optimizar_malla(
        complejidad, cellsize, spacing_base_m=100)

    costes = calcular_costes(sensores, gateways, aerostatos)
    print("\nResultado de la optimizacion:")
    for k, v in costes.items():
        print(f"  {k}: {v}")

    guardar_mapa(dem, landuse, sensores, gateways, aerostatos, cellsize,
                 "/mnt/user-data/outputs/narsil_demo_mapa.png")
    exportar_csv(sensores, gateways, aerostatos, cellsize,
                 "/mnt/user-data/outputs/narsil_demo_sensores.csv")
    print("\nMapa y CSV guardados en /mnt/user-data/outputs/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--proyecto", type=int, default=None,
                         help="ID de un proyecto creado con narsil_manual.py a procesar")
    parser.add_argument("--db", type=str, default=DB_PATH, help="ruta a narsil.db")
    args = parser.parse_args()
    if args.proyecto is not None:
        ejecutar_para_proyecto(args.proyecto, db_path=args.db)
    else:
        main_demo_generico()
