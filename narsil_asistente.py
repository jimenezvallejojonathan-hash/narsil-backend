"""
narsil_asistente.py — Proxy real hacia la API de Claude para el asistente
IRIS de narsil_control.html.

Por qué existe: narsil_control.html llamaba directamente a
https://api.anthropic.com/v1/messages desde el propio navegador. Eso solo
funciona dentro del entorno de artefactos de Claude (donde la clave de API
se gestiona por detrás automáticamente); fuera de ahí (archivo abierto en
local, o desplegado en cualquier hosting), esa llamada no puede funcionar
nunca — no hay ninguna clave de API real detrás, y el navegador la
bloquea. Este módulo es la pieza que faltaba: un endpoint propio que SÍ
tiene la clave real (guardada como variable de entorno, nunca en el
código ni en el navegador) y reenvía la petición a Anthropic por el lado
del servidor.

Protegido con el mismo NARSIL_API_TOKEN que ya usa el resto del backend,
para que no sea un proxy abierto que cualquiera pueda usar a tu costa.

Variables de entorno relevantes:
  ANTHROPIC_API_KEY  — tu clave real de console.anthropic.com (sk-ant-...)
  NARSIL_API_TOKEN   — el mismo token que ya protege /proyectos, etc.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

try:
    import httpx
except ImportError:
    httpx = None

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    sys.stderr.write(
        "\nAVISO: ANTHROPIC_API_KEY no está definida. El asistente IRIS "
        "(texto y voz) no podrá responder hasta que la definas con tu "
        "clave real de console.anthropic.com.\n\n"
    )

API_TOKEN = os.environ.get("NARSIL_API_TOKEN", "")

router = APIRouter(prefix="/asistente", tags=["asistente"])


def _verificar_auth(authorization: Optional[str]):
    if not API_TOKEN:
        return  # sin token configurado -> igual que el resto del backend en modo desarrollo
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Token inválido o ausente")


class PeticionAsistente(BaseModel):
    system: str
    mensaje: str


@router.post("/mensaje")
def enviar_mensaje(p: PeticionAsistente, authorization: Optional[str] = Header(None)):
    _verificar_auth(authorization)

    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY no configurada en el backend")
    if httpx is None:
        raise HTTPException(status_code=500, detail="httpx no disponible en el backend")

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 500,
                "system": p.system,
                "messages": [{"role": "user", "content": p.mensaje}],
            },
            timeout=30.0,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con Anthropic: {e}")

    if r.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Anthropic respondió {r.status_code}: {r.text[:300]}")

    datos = r.json()
    texto = "\n".join(bloque.get("text", "") for bloque in datos.get("content", []) if bloque.get("type") == "text").strip()
    return {"texto": texto}
