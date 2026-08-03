"""
aegis_monitor.py — Referencia técnica del módulo AEGIS (NARSIL_Arquitectura.odt v0.2)

AEGIS es el "supervisor transversal de seguridad": no decide, no ejecuta
misiones, y NO forma parte de la cadena SOLOMON -> IRIS -> TALOS. Su unica
potestad es forzar un paro seguro sobre TALOS ante:

1. Perdida de señal / perdida de control (heartbeat de la unidad).
2. Manipulacion detectada (comando sin firma valida o fuera de origen).
3. Fallo de integridad de datos (telemetria corrupta o fisicamente
   implausible).

Requisito de diseño (seccion 4 y 7 de NARSIL_Arquitectura.odt): AEGIS debe
ser fisica/logicamente independiente de la cadena de decision. Por eso esta
implementacion de referencia NUNCA importa ni llama a un modulo "solomon"
o "iris": solo conoce el heartbeat, los comandos firmados y la telemetria
cruda de la unidad, y un canal propio de parada (stop_relay), separado del
canal de ordenes normal.

Este archivo es una PRUEBA DE CONCEPTO ejecutable, no el codigo final que
correria sobre TALOS en campo (que dependera del hardware real elegido).
Sirve para validar que los tres criterios de disparo son implementables y
verificables, y para dejarle a CLOUD un punto de partida concreto.

ACTUALIZADO (03/08/2026): el registro de paros de seguridad ahora persiste
en la misma narsil.db compartida con narsil_manual.py y narsil_sistema.py
(antes vivía solo en memoria y se perdía en cada reinicio de Render).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from narsil_registro_inmutable import RegistroInmutable

# Misma base de datos que ya comparten narsil_manual.py y narsil_sistema.py
# — no se introduce ningún almacén nuevo.
_DB_COMPARTIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narsil.db")

# ---------------------------------------------------------------------------
# 1. Registro inmutable (seccion 6 de la arquitectura): se importa de
#    narsil_registro_inmutable.py, el mismo módulo que también usa
#    narsil_auth.py para el login/alta de usuarios. Ya persiste en disco.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2. Criterio 1 — Perdida de señal / perdida de control
# ---------------------------------------------------------------------------
class VigilanteHeartbeat:
    """Cada unidad TALOS debe enviar un heartbeat cada `intervalo_esperado_s`.
    Si no llega ninguno en `timeout_s`, se considera perdida de señal."""

    def __init__(self, intervalo_esperado_s: float = 2.0, factor_timeout: float = 3.0):
        self.intervalo_esperado_s = intervalo_esperado_s
        self.timeout_s = intervalo_esperado_s * factor_timeout
        self._ultimo_heartbeat: dict[str, float] = {}

    def registrar_heartbeat(self, unidad: str, momento: Optional[float] = None):
        self._ultimo_heartbeat[unidad] = momento if momento is not None else time.time()

    def hay_perdida_de_senal(self, unidad: str, ahora: Optional[float] = None) -> bool:
        ahora = ahora if ahora is not None else time.time()
        ultimo = self._ultimo_heartbeat.get(unidad)
        if ultimo is None:
            return True  # nunca se ha visto la unidad -> tratar como perdida
        return (ahora - ultimo) > self.timeout_s


# ---------------------------------------------------------------------------
# 3. Criterio 2 — Manipulacion detectada
# ---------------------------------------------------------------------------
class VerificadorManipulacion:
    """Todo comando dirigido a TALOS debe llegar firmado con HMAC usando una
    clave que solo conoce la cadena IRIS -> TALOS legitima. Si la firma no
    coincide, o el origen declarado no es IRIS, se considera manipulacion."""

    def __init__(self, clave_secreta: bytes):
        self._clave = clave_secreta

    def firmar(self, comando: str) -> str:
        return hmac.new(self._clave, comando.encode(), hashlib.sha256).hexdigest()

    def hay_manipulacion(self, comando: str, firma_recibida: str, origen_declarado: str) -> bool:
        if origen_declarado != "IRIS":
            return True
        firma_esperada = self.firmar(comando)
        return not hmac.compare_digest(firma_esperada, firma_recibida)


# ---------------------------------------------------------------------------
# 4. Criterio 3 — Fallo de integridad de datos
# ---------------------------------------------------------------------------
class VerificadorIntegridadTelemetria:
    """Comprueba que la telemetria de una unidad es fisicamente plausible:
    checksum correcto y sin saltos de posicion/velocidad imposibles dado el
    tiempo transcurrido desde la ultima lectura valida."""

    def __init__(self, velocidad_max_m_s: float = 3.0):
        self.velocidad_max_m_s = velocidad_max_m_s
        self._ultima_lectura: dict[str, tuple[float, float, float]] = {}  # unidad -> (t, x, y)

    @staticmethod
    def checksum_valido(payload: str, checksum_recibido: str) -> bool:
        return hashlib.sha256(payload.encode()).hexdigest()[:8] == checksum_recibido

    def hay_fallo_integridad(self, unidad: str, payload: str, checksum: str,
                              x: float, y: float, momento: Optional[float] = None) -> tuple[bool, str]:
        momento = momento if momento is not None else time.time()
        if not self.checksum_valido(payload, checksum):
            return True, "checksum de telemetria invalido"
        anterior = self._ultima_lectura.get(unidad)
        self._ultima_lectura[unidad] = (momento, x, y)
        if anterior is None:
            return False, ""
        t0, x0, y0 = anterior
        dt = max(momento - t0, 1e-6)
        distancia = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
        velocidad_implicita = distancia / dt
        if velocidad_implicita > self.velocidad_max_m_s:
            return True, f"salto de posicion fisicamente implausible ({velocidad_implicita:.1f} m/s > {self.velocidad_max_m_s} m/s)"
        return False, ""


# ---------------------------------------------------------------------------
# 5. AEGIS: agrega los tres criterios, actua por su cuenta, notifica
# ---------------------------------------------------------------------------
class Aegis:
    def __init__(self, clave_secreta: bytes, notificar: Callable[[str, str, str], None]):
        self.heartbeat = VigilanteHeartbeat()
        self.manipulacion = VerificadorManipulacion(clave_secreta)
        self.integridad = VerificadorIntegridadTelemetria()
        self.registro = RegistroInmutable(db_path=_DB_COMPARTIDA)
        self._notificar = notificar
        self._paradas: dict[str, bool] = {}

    def unidad_parada(self, unidad: str) -> bool:
        return self._paradas.get(unidad, False)

    def _forzar_paro(self, unidad: str, causa: str):
        # Canal propio, independiente de SOLOMON/IRIS/TALOS: aqui se
        # llamaria al rele fisico de corte de la unidad, no a su API de
        # ordenes normal.
        self._paradas[unidad] = True
        evento = self.registro.registrar("paro_seguro", unidad, causa)
        self._notificar(unidad, causa, evento["hash_evento"])
        return evento

    def evaluar_heartbeat(self, unidad: str, ahora: Optional[float] = None):
        if self.heartbeat.hay_perdida_de_senal(unidad, ahora):
            return self._forzar_paro(unidad, "perdida de señal / perdida de control")
        return None

    def evaluar_comando(self, unidad: str, comando: str, firma: str, origen: str):
        if self.manipulacion.hay_manipulacion(comando, firma, origen):
            return self._forzar_paro(unidad, "manipulacion detectada")
        return None

    def evaluar_telemetria(self, unidad: str, payload: str, checksum: str, x: float, y: float, momento=None):
        fallo, causa = self.integridad.hay_fallo_integridad(unidad, payload, checksum, x, y, momento)
        if fallo:
            return self._forzar_paro(unidad, f"fallo de integridad de datos: {causa}")
        return None
