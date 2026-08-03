"""
narsil_registro_inmutable.py — Registro inmutable compartido de NARSIL.

Cada evento se encadena con el hash del anterior (igual que un blockchain
simple): si alguien edita un evento a mano en el registro, la cadena deja
de cuadrar a partir de ese punto, y verificar_integridad() lo detecta.

ACTUALIZADO (03/08/2026): ahora persiste en SQLite (la misma narsil.db
que ya usan narsil_manual.py y narsil_sistema.py) en vez de vivir solo en
una lista en memoria. Antes, cada reinicio del proceso en Render (nuevo
despliegue, caída, ciclo de sueño/despertar) borraba todo el historial de
auditoría — ya no.

Compatibilidad: si se instancia sin db_path, se comporta exactamente como
antes (solo en memoria) — útil para tests que no quieran tocar disco.

Módulos que lo usan hoy:
- aegis_monitor.py — paros de seguridad de TALOS (unidad = ID de la
  máquina física)
- narsil_auth.py — intentos de login/alta (unidad = nombre de
  usuario o token de la solicitud)

Ambos usos están probados en test_registro_inmutable.py.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RegistroInmutable:
    db_path: Optional[str] = None
    eventos: list = field(default_factory=list)
    _ultimo_hash: str = "0" * 64

    def __post_init__(self):
        if self.db_path:
            self._inicializar_tabla()
            self._cargar_desde_bd()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _inicializar_tabla(self):
        conn = self._conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS registro_inmutable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL, unidad TEXT NOT NULL, causa TEXT NOT NULL,
            momento REAL NOT NULL, hash_anterior TEXT NOT NULL, hash_evento TEXT NOT NULL
        )""")
        conn.commit()
        conn.close()

    def _cargar_desde_bd(self):
        conn = self._conn()
        filas = conn.execute(
            "SELECT tipo, unidad, causa, momento, hash_anterior, hash_evento "
            "FROM registro_inmutable ORDER BY id"
        ).fetchall()
        conn.close()
        self.eventos = [dict(zip(
            ["tipo", "unidad", "causa", "momento", "hash_anterior", "hash_evento"], f
        )) for f in filas]
        self._ultimo_hash = self.eventos[-1]["hash_evento"] if self.eventos else "0" * 64

    def registrar(self, tipo: str, unidad: str, causa: str) -> dict:
        momento = time.time()
        payload = f"{self._ultimo_hash}|{tipo}|{unidad}|{causa}|{momento}"
        hash_evento = hashlib.sha256(payload.encode()).hexdigest()
        evento = {
            "tipo": tipo, "unidad": unidad, "causa": causa,
            "momento": momento, "hash_anterior": self._ultimo_hash,
            "hash_evento": hash_evento,
        }
        self.eventos.append(evento)
        self._ultimo_hash = hash_evento
        if self.db_path:
            conn = self._conn()
            conn.execute(
                "INSERT INTO registro_inmutable "
                "(tipo, unidad, causa, momento, hash_anterior, hash_evento) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tipo, unidad, causa, momento, evento["hash_anterior"], hash_evento),
            )
            conn.commit()
            conn.close()
        return evento

    def verificar_integridad(self) -> bool:
        """Recalcula la cadena de hashes; si alguien editó un evento a
        mano, la cadena deja de coincidir a partir de ese punto."""
        anterior = "0" * 64
        for ev in self.eventos:
            payload = f"{anterior}|{ev['tipo']}|{ev['unidad']}|{ev['causa']}|{ev['momento']}"
            if hashlib.sha256(payload.encode()).hexdigest() != ev["hash_evento"]:
                return False
            anterior = ev["hash_evento"]
        return True

    def eventos_de(self, unidad: str) -> list[dict]:
        """Filtra el historial completo por una unidad concreta (una
        máquina física, o un nombre de usuario/token de autenticación)."""
        return [e for e in self.eventos if e["unidad"] == unidad]
