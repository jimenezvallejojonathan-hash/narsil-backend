"""
narsil_auth.py — Autenticación y altas de usuario con aprobación bloqueante
por móvil (NARSIL_control.html), por DOS vías redundantes: ntfy.sh (push)
y WhatsApp vía CallMeBot.

ACTUALIZADO (03/08/2026):
- El login de NEO ya NO comprueba contraseña. La única puerta real es tu
  aprobación explícita desde el móvil ("si yo no autorizo no entra
  nadie"). Para que esto sea seguro y no solo cómodo, se limita cuántas
  solicitudes de login se pueden crear en poco tiempo (ver
  LIMITE_SOLICITUDES_LOGIN más abajo) — sin esto, cualquiera podría
  generarte avisos de aprobación sin parar hasta que tocaras "aprobar"
  por costumbre o por error (el mismo patrón usado en el hackeo de Uber
  en 2022, conocido como "fatiga de MFA").
- El registro de auditoría ya no vive solo en memoria: usa la misma
  narsil.db que comparten narsil_manual.py y narsil_sistema.py, así que
  sobrevive a los reinicios de Render.

Cubre dos flujos:
A) Login de Administrador (NEO) — sin contraseña, bloqueado hasta
   aprobación por móvil.
B) Alta de un usuario nuevo (rol "usuario") — con contraseña propia
   (huella SHA-256, nunca en texto plano), bloqueada hasta aprobación
   por móvil. El aviso que llega al teléfono SOLO contiene el nombre
   elegido — LA CONTRASEÑA NUNCA SE ENVÍA POR SMS, WHATSAPP NI NINGÚN
   OTRO CANAL. Es una norma fija, no configurable.

Variables de entorno relevantes:
  NARSIL_NTFY_TOPIC        — tema privado de ntfy.sh (tú lo eliges).
                             Trátalo como una contraseña: quien conozca
                             el nombre exacto puede publicar avisos ahí.
  NARSIL_BACKEND_URL       — URL pública de este backend
  NARSIL_CALLMEBOT_PHONE   — tu número de WhatsApp (con prefijo, sin '+')
  NARSIL_CALLMEBOT_APIKEY  — clave que te da CallMeBot al activarlo tú
                             mismo (envía "I allow callmebot to send me
                             messages" al +34 644 51 95 23 desde tu
                             propio WhatsApp; te responde con la clave)
  NARSIL_LIMITE_LOGIN_INTENTOS  — máximo de solicitudes de login por
                             ventana de tiempo (por defecto 3)
  NARSIL_LIMITE_LOGIN_VENTANA_S — duración de esa ventana en segundos
                             (por defecto 600 = 10 minutos)

NARSIL_ADMIN_PASSWORD_HASH ya NO se usa — puedes borrarla de las
variables de entorno en Render.

AUDITORÍA REAL: cada evento de login/alta (solicitud creada, aprobada,
rechazada, caducada, bloqueada por límite) queda registrado en el mismo
RegistroInmutable (narsil_registro_inmutable.py) que ya usa AEGIS para
los paros de seguridad de TALOS, ahora persistido en narsil.db.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from narsil_registro_inmutable import RegistroInmutable

try:
    import httpx
except ImportError:  # se resuelve en requirements.txt del backend
    httpx = None

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
ADMIN_USUARIO = "NEO"

NTFY_TOPIC = os.environ.get("NARSIL_NTFY_TOPIC", "")
BACKEND_URL = os.environ.get("NARSIL_BACKEND_URL", "http://localhost:8000")
CALLMEBOT_PHONE = os.environ.get("NARSIL_CALLMEBOT_PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("NARSIL_CALLMEBOT_APIKEY", "")

CADUCIDAD_SEGUNDOS = 5 * 60  # 5 minutos

# Límite de solicitudes de login en ventana de tiempo, para que nadie
# pueda generarte avisos de aprobación sin parar (fatiga de MFA).
LIMITE_SOLICITUDES_LOGIN = int(os.environ.get("NARSIL_LIMITE_LOGIN_INTENTOS", "3"))
VENTANA_LIMITE_SEGUNDOS = int(os.environ.get("NARSIL_LIMITE_LOGIN_VENTANA_S", "600"))
_historial_intentos_login: list[float] = []


def _demasiados_intentos_recientes() -> bool:
    ahora = time.time()
    global _historial_intentos_login
    _historial_intentos_login = [t for t in _historial_intentos_login
                                  if ahora - t < VENTANA_LIMITE_SEGUNDOS]
    return len(_historial_intentos_login) >= LIMITE_SOLICITUDES_LOGIN


def _registrar_intento_login():
    _historial_intentos_login.append(time.time())


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Almacén en memoria de solicitudes pendientes (login de admin y altas de
# usuario), suficiente para un volumen pequeño; si se despliega con varios
# workers, mover a la base de datos real.
# ---------------------------------------------------------------------------
@dataclass
class SolicitudLogin:
    token: str
    creado_en: float
    estado: str = "pendiente"  # pendiente | aprobado | rechazado | caducado
    session_token: Optional[str] = None


@dataclass
class SolicitudRegistro:
    token: str
    nombre: str
    password_hash: str
    creado_en: float
    estado: str = "pendiente"  # pendiente | aprobado | rechazado | caducado


_solicitudes: dict[str, SolicitudLogin] = {}
_solicitudes_registro: dict[str, SolicitudRegistro] = {}
_sesiones_validas: set[str] = set()

# Mismo RegistroInmutable que usa AEGIS para los paros de seguridad de
# TALOS — persistido en la misma narsil.db que narsil_manual.py y
# narsil_sistema.py, así que sobrevive a los reinicios de Render.
_DB_COMPARTIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narsil.db")
registro_auditoria = RegistroInmutable(db_path=_DB_COMPARTIDA)


def _purgar_caducadas():
    ahora = time.time()
    for s in _solicitudes.values():
        if s.estado == "pendiente" and (ahora - s.creado_en) > CADUCIDAD_SEGUNDOS:
            s.estado = "caducado"
            registro_auditoria.registrar("login_caducado", ADMIN_USUARIO, f"token {s.token[:8]}... sin resolver en {CADUCIDAD_SEGUNDOS}s")
    for s in _solicitudes_registro.values():
        if s.estado == "pendiente" and (ahora - s.creado_en) > CADUCIDAD_SEGUNDOS:
            s.estado = "caducado"
            registro_auditoria.registrar("registro_caducado", s.nombre, f"token {s.token[:8]}... sin resolver en {CADUCIDAD_SEGUNDOS}s")


# ---------------------------------------------------------------------------
# Envío de avisos — dos canales independientes. Ninguno de los dos recibe
# nunca la contraseña ni su huella: solo texto descriptivo y enlaces de
# aprobar/rechazar.
# ---------------------------------------------------------------------------
def _enviar_ntfy(titulo: str, mensaje: str, url_aprobar: str, url_rechazar: str) -> bool:
    if not NTFY_TOPIC or httpx is None:
        return False
    try:
        r = httpx.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=mensaje.encode("utf-8"),
            headers={
                "Title": titulo,
                "Priority": "urgent",
                "Actions": (
                    f"http, Aprobar, {url_aprobar}, method=GET; "
                    f"http, Rechazar, {url_rechazar}, method=GET"
                ),
            },
            timeout=5.0,
        )
        if r.status_code >= 300:
            sys.stderr.write(f"AVISO: ntfy.sh respondió {r.status_code}: {r.text[:300]}\n")
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"AVISO: fallo real al enviar aviso a ntfy.sh: {type(e).__name__}: {e}\n")
        return False


def _enviar_callmebot(mensaje: str) -> bool:
    """WhatsApp vía CallMeBot. No soporta botones de acción: el mensaje debe
    incluir los enlaces de aprobar/rechazar como texto plano para que se
    pueda tocar directamente desde WhatsApp."""
    if not CALLMEBOT_PHONE or not CALLMEBOT_APIKEY or httpx is None:
        return False
    try:
        url = (
            "https://api.callmebot.com/whatsapp.php"
            f"?phone={CALLMEBOT_PHONE}&apikey={CALLMEBOT_APIKEY}"
            f"&text={urllib.parse.quote(mensaje)}"
        )
        httpx.get(url, timeout=5.0)
        return True
    except Exception:
        return False


def _avisar_dual(titulo: str, mensaje_ntfy: str, mensaje_callmebot: str,
                  url_aprobar: str, url_rechazar: str) -> dict:
    return {
        "ntfy_enviado": _enviar_ntfy(titulo, mensaje_ntfy, url_aprobar, url_rechazar),
        "whatsapp_enviado": _enviar_callmebot(mensaje_callmebot),
    }


# ---------------------------------------------------------------------------
# Router FastAPI — se incluye desde narsil_api.py con app.include_router(...)
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/auth", tags=["autenticacion"])


class PeticionLogin(BaseModel):
    usuario: str
    password: str = ""  # ya no se usa para validar; se acepta por compatibilidad con la consola


class PeticionRegistro(BaseModel):
    nombre: str
    password: str


@router.post("/login")
def solicitar_login(p: PeticionLogin):
    _purgar_caducadas()

    # Ya no se comprueba ninguna contraseña: la única puerta real es tu
    # aprobación explícita desde el móvil. 'usuario' solo filtra
    # solicitudes obviamente erróneas, no es una barrera de seguridad.
    if p.usuario.strip().upper() != ADMIN_USUARIO:
        raise HTTPException(status_code=401, detail="Usuario desconocido")

    if _demasiados_intentos_recientes():
        registro_auditoria.registrar(
            "login_bloqueado_por_limite", ADMIN_USUARIO,
            f"más de {LIMITE_SOLICITUDES_LOGIN} solicitudes en {VENTANA_LIMITE_SEGUNDOS}s",
        )
        raise HTTPException(
            status_code=429,
            detail=f"Demasiadas solicitudes de acceso recientes. Espera unos minutos "
                   f"antes de volver a intentarlo (límite: {LIMITE_SOLICITUDES_LOGIN} "
                   f"cada {VENTANA_LIMITE_SEGUNDOS // 60} min).",
        )
    _registrar_intento_login()

    token = secrets.token_urlsafe(24)
    _solicitudes[token] = SolicitudLogin(token=token, creado_en=time.time())
    registro_auditoria.registrar("login_solicitado", ADMIN_USUARIO, f"token {token[:8]}...")

    url_aprobar = f"{BACKEND_URL}/auth/aprobar/{token}"
    url_rechazar = f"{BACKEND_URL}/auth/rechazar/{token}"
    avisos = _avisar_dual(
        titulo="NARSIL - solicitud de acceso de administrador",
        mensaje_ntfy="Alguien intenta entrar como NEO (Administrador) en NARSIL. Aprueba SOLO si has sido tú.",
        mensaje_callmebot=(
            "🔐 NARSIL: alguien intenta entrar como *NEO* (Administrador).\n"
            f"Aprobar: {url_aprobar}\nRechazar: {url_rechazar}"
        ),
        url_aprobar=url_aprobar, url_rechazar=url_rechazar,
    )
    return {"token": token, "caduca_en_segundos": CADUCIDAD_SEGUNDOS, **avisos}


@router.get("/estado/{token}")
def consultar_estado(token: str):
    _purgar_caducadas()
    s = _solicitudes.get(token)
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    respuesta = {"estado": s.estado}
    if s.estado == "aprobado":
        respuesta["session_token"] = s.session_token
    return respuesta


@router.get("/aprobar/{token}")
def aprobar(token: str):
    s = _solicitudes.get(token)
    if not s:
        return {"ok": False, "motivo": "Solicitud no encontrada o ya resuelta"}
    _purgar_caducadas()
    if s.estado != "pendiente":
        return {"ok": False, "motivo": f"Esta solicitud ya está '{s.estado}', no se puede aprobar de nuevo"}
    s.estado = "aprobado"
    s.session_token = secrets.token_urlsafe(32)
    _sesiones_validas.add(s.session_token)
    registro_auditoria.registrar("login_aprobado", ADMIN_USUARIO, f"token {token[:8]}... aprobado desde móvil")
    return {"ok": True, "mensaje": "Acceso aprobado. Ya puedes volver a la consola NARSIL."}


@router.get("/rechazar/{token}")
def rechazar(token: str):
    s = _solicitudes.get(token)
    if not s:
        return {"ok": False, "motivo": "Solicitud no encontrada o ya resuelta"}
    _purgar_caducadas()
    if s.estado != "pendiente":
        return {"ok": False, "motivo": f"Esta solicitud ya está '{s.estado}'"}
    s.estado = "rechazado"
    registro_auditoria.registrar("login_rechazado", ADMIN_USUARIO, f"token {token[:8]}... rechazado desde móvil")
    return {"ok": True, "mensaje": "Acceso rechazado."}


@router.get("/verificar-sesion/{session_token}")
def verificar_sesion(session_token: str):
    return {"valida": session_token in _sesiones_validas}


@router.get("/auditoria")
def consultar_auditoria(unidad: Optional[str] = None):
    """Devuelve el historial de eventos de login/alta (el mismo
    RegistroInmutable que usa AEGIS) y si la cadena de hashes sigue
    íntegra. `unidad` filtra por nombre de usuario (p.ej. 'NEO' o el
    nombre de la persona que se dio de alta)."""
    eventos = (
        registro_auditoria.eventos_de(unidad) if unidad else registro_auditoria.eventos
    )
    return {"integro": registro_auditoria.verificar_integridad(), "eventos": eventos}


# ---------------------------------------------------------------------------
# Alta de usuario nuevo — misma lógica de bloqueo, aviso dual, y la
# contraseña NUNCA viaja en el mensaje del aviso, solo el nombre.
# ---------------------------------------------------------------------------
@router.post("/registro")
def solicitar_registro(p: PeticionRegistro):
    _purgar_caducadas()
    nombre = p.nombre.strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="Falta el nombre")
    if not p.password:
        raise HTTPException(status_code=400, detail="Falta la contraseña")
    if nombre.upper() == ADMIN_USUARIO:
        raise HTTPException(status_code=400, detail="Ese nombre está reservado para el administrador")

    token = secrets.token_urlsafe(24)
    _solicitudes_registro[token] = SolicitudRegistro(
        token=token, nombre=nombre, password_hash=hash_password(p.password), creado_en=time.time(),
    )
    registro_auditoria.registrar("registro_solicitado", nombre, f"token {token[:8]}...")

    url_aprobar = f"{BACKEND_URL}/auth/registro/aprobar/{token}"
    url_rechazar = f"{BACKEND_URL}/auth/registro/rechazar/{token}"
    avisos = _avisar_dual(
        titulo="NARSIL - alta de usuario nuevo",
        mensaje_ntfy=f"'{nombre}' quiere darse de alta en NARSIL como usuario. Aprueba solo si lo reconoces.",
        mensaje_callmebot=(
            f"👤 NARSIL: *{nombre}* quiere darse de alta como usuario.\n"
            f"Aprobar: {url_aprobar}\nRechazar: {url_rechazar}"
        ),
        url_aprobar=url_aprobar, url_rechazar=url_rechazar,
    )
    return {"token": token, "caduca_en_segundos": CADUCIDAD_SEGUNDOS, **avisos}


@router.get("/registro/estado/{token}")
def consultar_estado_registro(token: str):
    _purgar_caducadas()
    s = _solicitudes_registro.get(token)
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    respuesta = {"estado": s.estado, "nombre": s.nombre}
    if s.estado == "aprobado":
        respuesta["password_hash"] = s.password_hash
    return respuesta


@router.get("/registro/aprobar/{token}")
def aprobar_registro(token: str):
    s = _solicitudes_registro.get(token)
    if not s:
        return {"ok": False, "motivo": "Solicitud no encontrada o ya resuelta"}
    _purgar_caducadas()
    if s.estado != "pendiente":
        return {"ok": False, "motivo": f"Esta solicitud ya está '{s.estado}', no se puede aprobar de nuevo"}
    s.estado = "aprobado"
    registro_auditoria.registrar("registro_aprobado", s.nombre, f"token {token[:8]}... aprobado desde móvil")
    return {"ok": True, "mensaje": f"Alta de '{s.nombre}' aprobada."}


@router.get("/registro/rechazar/{token}")
def rechazar_registro(token: str):
    s = _solicitudes_registro.get(token)
    if not s:
        return {"ok": False, "motivo": "Solicitud no encontrada o ya resuelta"}
    _purgar_caducadas()
    if s.estado != "pendiente":
        return {"ok": False, "motivo": f"Esta solicitud ya está '{s.estado}'"}
    s.estado = "rechazado"
    registro_auditoria.registrar("registro_rechazado", s.nombre, f"token {token[:8]}... rechazado desde móvil")
    return {"ok": True, "mensaje": f"Alta de '{s.nombre}' rechazada."}
