"""
narsil_registro_inmutable.py — Registro inmutable compartido de NARSIL.

Extraído de aegis_monitor.py (sección 6 de NARSIL_Arquitectura.odt:
"gobernanza y trazabilidad") para que deje de ser una copia paralela y
pase a ser una única implementación real que varios módulos comparten.

Cada evento se encadena con el hash del anterior (igual que un blockchain
simple): si alguien edita un evento a mano en el registro, la cadena deja
de cuadrar a partir de ese punto, y verificar_integridad() lo detecta.

Módulos que lo usan hoy:
  - aegis_monitor.py   — paros de seguridad de TALOS (unidad = ID de la
                         máquina física)
  - narsil_auth.py     — intentos de login/alta (unidad = nombre de
                         usuario o token de la solicitud)

Al compartir la misma clase, un evento de AEGIS y un evento de login
pueden convivir en el MISMO registro (misma cadena de hashes), si así se
decide instanciar un único RegistroInmutable para toda la aplicación —
o mantenerse en registros separados si se prefiere no mezclar dominios.
Ambos usos están probados en test_registro_inmutable.py.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class RegistroInmutable:
    eventos: list = field(default_factory=list)
    _ultimo_hash: str = "0" * 64

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
        return evento

    def verificar_integridad(self) -> bool:
        """Recalcula la cadena de hashes; si alguien editó un evento a mano,
        la cadena deja de coincidir a partir de ese punto."""
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
