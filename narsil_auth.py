"""
narsil_auth.py — Autenticación y altas de usuario con aprobación bloqueante
por móvil (NARSIL_control.html), por DOS vías redundantes: ntfy.sh (push)
y WhatsApp vía CallMeBot.

Cubre dos flujos, con el mismo principio de fondo:

  A) Login de Administrador (NEO) — una sola cuenta válida, contraseña
     comparada por huella SHA-256, bloqueado hasta aprobación por móvil.

  B) Alta de un usuario nuevo (rol "usuario") — ahora también con
     contraseña propia (huella SHA-256, nunca en texto plano), bloqueada
     hasta aprobación por móvil. El aviso que llega al teléfono SOLO
     contiene el nombre elegido — LA CONTRASEÑA NUNCA SE ENVÍA POR SMS,
     WHATSAPP NI NINGÚN OTRO CANAL. Es una norma fija, no configurable:
     un canal de mensajería no es un lugar seguro para una contraseña,
     tenga o no tenga cifrado el resto del sistema.

Variables de entorno relevantes:
  NARSIL_ADMIN_PASSWORD_HASH  — huella SHA-256 de la contraseña de NEO
  NARSIL_NTFY_TOPIC           — tema privado de ntfy.sh (tú lo eliges)
  NARSIL_BACKEND_URL          — URL pública de este backend
  NARSIL_CALLMEBOT_PHONE      — tu número de WhatsApp (con prefijo, sin '+')
  NARSIL_CALLMEBOT_APIKEY     — clave que te da CallMeBot al activarlo tú
                                 mismo (envía "I allow callmebot to send me
                                 messages" al +34 644 51 95 23 desde tu
                                 propio WhatsApp; te responde con la clave)

AUDITORÍA REAL (26/07/2026): cada evento de login/alta (solicitud creada,
aprobada, rechazada, caducada) queda registrado en el mismo
RegistroInmutable (narsil_registro_inmutable.py) que ya usa AEGIS para los
paros de seguridad de TALOS — no es una copia parecida, es literalmente la
misma clase e implementación, importada desde el mismo sitio. Esto permite
comprobar en cualquier momento que nadie ha alterado a mano el historial de
accesos, con la misma verificación de cadena de hashes que ya se probó con
AEGIS.
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

_HASH_RESPALDO_DESARROLLO = hashlib.sha256("NARSIL2026@#".encode()).hexdigest()
ADMIN_PASSWORD_HASH = os.environ.get("NARSIL_ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH:
    sys.stderr.write(
        "\nAVISO: NARSIL_ADMIN_PASSWORD_HASH no está definida. Usando la huella "
        "de la contraseña acordada como respaldo de desarrollo. Antes de desplegar "
        "en producción, genera tu propia huella y defínela como variable de entorno:\n"
        "    python3 -c \"import hashlib;print(hashlib.sha256(b'TU_CONTRASEÑA').hexdigest())\"\n\n"
    )
    ADMIN_PASSWORD_HASH = _HASH_RESPALDO_DESARROLLO

NTFY_TOPIC = os.environ.get("NARSIL_NTFY_TOPIC", "")
BACKEND_URL = os.environ.get("NARSIL_BACKEND_URL", "http://localhost:8000")
CALLMEBOT_PHONE = os.environ.get("NARSIL_CALLMEBOT_PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("NARSIL_CALLMEBOT_APIKEY", "")

CADUCIDAD_SEGUNDOS = 5 * 60  # 5 minutos


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
# TALOS — aquí registramos cada evento real de login/alta, con la unidad
# siendo el nombre de usuario (nunca la contraseña ni su huella).
registro_auditoria = RegistroInmutable()


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
        httpx.post(
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
        return True
    except Exception:
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
    password: str


class PeticionRegistro(BaseModel):
    nombre: str
    password: str


@router.post("/login")
def solicitar_login(p: PeticionLogin):
    _purgar_caducadas()
    if p.usuario != ADMIN_USUARIO or hash_password(p.password) != ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    token = secrets.token_urlsafe(24)
    _solicitudes[token] = SolicitudLogin(token=token, creado_en=time.time())
    registro_auditoria.registrar("login_solicitado", ADMIN_USUARIO, f"token {token[:8]}...")
    url_aprobar = f"{BACKEND_URL}/auth/aprobar/{token}"
    url_rechazar = f"{BACKEND_URL}/auth/rechazar/{token}"
    avisos = _avisar_dual(
        titulo="NARSIL — solicitud de acceso de administrador",
        mensaje_ntfy="Alguien intenta entrar como NEO (Administrador) en NARSIL. Aprueba solo si has sido tú.",
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
        titulo="NARSIL — alta de usuario nuevo",
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
        # Se devuelve la HUELLA (nunca la contraseña) para que el cliente
        # guarde el usuario ya dado de alta en window.storage.
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

