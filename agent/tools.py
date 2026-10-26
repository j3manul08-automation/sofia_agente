# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio.

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio le llega
al agente por el system prompt (config/prompts.yaml), asi que para CONTESTAR preguntas
no hace falta nada de aca. Este archivo es el lugar para las ACCIONES —registrar una
solicitud, avisarle a Erika— y conectarlas al ciclo de tool use de Claude es un paso aparte.

Hoy Sofia recoge los datos dentro de la conversacion y quedan en el historial de cada
cliente. Las funciones de abajo son la base para que, mas adelante, esas solicitudes
queden registradas aparte y le lleguen a Erika.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")

# Donde se guardan las solicitudes, una por linea en formato JSON.
# Ojo: en Render el disco del contenedor es efimero. Para produccion conviene
# pasar esto a la base de datos o a una planilla.
ARCHIVO_SOLICITUDES = Path("solicitudes.jsonl")

RAMOS = ("auto", "vida", "salud", "hogar", "negocios", "renta_vitalicia")
TIPOS_POSTVENTA = ("renovacion", "cambio", "reclamacion", "consulta")


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ── Solicitudes (leads y post-venta) ─────────────────────────────────────


def _guardar_solicitud(solicitud: dict) -> str:
    """Agrega la solicitud al archivo y retorna su id."""
    ahora = datetime.now(timezone.utc)
    solicitud_id = f"SOL-{ahora:%Y%m%d%H%M%S}"
    registro = {"id": solicitud_id, "creada": ahora.isoformat(), **solicitud}
    with ARCHIVO_SOLICITUDES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    logger.info(f"Solicitud registrada: {solicitud_id} ({solicitud.get('categoria')})")
    return solicitud_id


def registrar_lead(telefono: str, nombre: str, ramo: str, datos: dict) -> str:
    """
    Registra a alguien que quiere cotizar un seguro nuevo.

    ramo: auto | vida | salud | hogar | negocios | renta_vitalicia
    datos: lo que Sofia recogio (vehiculo, edades, tipo de propiedad, etc.)
    """
    if ramo not in RAMOS:
        logger.warning(f"Ramo desconocido en registrar_lead: {ramo}")
    return _guardar_solicitud(
        {
            "categoria": "cotizacion",
            "telefono": telefono,
            "nombre": nombre,
            "ramo": ramo,
            "datos": datos,
        }
    )


def crear_solicitud_postventa(telefono: str, nombre: str, tipo: str, datos: dict) -> str:
    """
    Registra una solicitud de un cliente que ya tiene poliza.

    tipo: renovacion | cambio | reclamacion | consulta
    datos: aseguradora, numero de poliza, que paso o que necesita, etc.
    """
    if tipo not in TIPOS_POSTVENTA:
        logger.warning(f"Tipo desconocido en crear_solicitud_postventa: {tipo}")
    return _guardar_solicitud(
        {
            "categoria": tipo,
            "telefono": telefono,
            "nombre": nombre,
            "datos": datos,
        }
    )


def escalar_a_erika(telefono: str, motivo: str) -> None:
    """
    Marca que el cliente necesita que Erika lo contacte en persona.

    TODO: hoy solo deja constancia en el log y en el archivo de solicitudes. El paso
    siguiente es que le llegue una notificacion a Erika (WhatsApp, correo, etc.).
    """
    _guardar_solicitud({"categoria": "escalamiento", "telefono": telefono, "motivo": motivo})
