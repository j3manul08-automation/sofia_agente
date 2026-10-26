# agent/brain.py — Cerebro del agente: conexion con OpenAI
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Responses de OpenAI.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger("agentkit")

# Si falta la key, el cliente no se crea pero el servidor igual arranca: el health check
# lo muestra, en vez de reventar en el import y dejar a Render reiniciando a ciegas.
_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
client = AsyncOpenAI(api_key=_API_KEY) if _API_KEY else None

# El modelo se cambia desde .env, sin tocar el codigo. No hay default a proposito:
# es una decision del dueño del negocio (costo vs. calidad), no del codigo.
MODELO = (os.getenv("OPENAI_MODEL") or "").strip()


def problema_de_configuracion() -> str | None:
    """Que le falta a la configuracion de OpenAI, o None si esta completa."""
    if client is None:
        return "Falta OPENAI_API_KEY"
    if not MODELO:
        return "Falta OPENAI_MODEL"
    return None

# Esfuerzo de razonamiento, solo para modelos que razonan. Es un bot de respuestas
# cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Vacio en el .env = no se manda el parametro.
ESFUERZOS_VALIDOS = ("none", "minimal", "low", "medium", "high", "xhigh")
ESFUERZO = (os.getenv("OPENAI_REASONING_EFFORT") or "").strip().lower()
if ESFUERZO and ESFUERZO not in ESFUERZOS_VALIDOS:
    logger.warning(
        f"OPENAI_REASONING_EFFORT='{ESFUERZO}' no es valido "
        f"({' | '.join(ESFUERZOS_VALIDOS)}); se ignora y no se envia el parametro."
    )
    ESFUERZO = ""

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# que razonan, el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS") or "4096")

# Los modelos que no razonan rechazan el parametro "reasoning". Si la primera llamada
# falla por eso, se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True

# Santo Domingo esta en UTC-4 todo el año (Republica Dominicana no usa horario de verano).
# Se usa un desfase fijo para no depender de que el contenedor tenga la base de zonas horarias.
ZONA_NEGOCIO = timezone(timedelta(hours=-4))
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

RAMOS = [
    "vehiculos", "salud_individual", "salud_colectivo", "vida", "hogar",
    "negocios", "fianzas", "viajes", "renta_vitalicia",
]

# Forma exacta de la respuesta del modelo. Con "strict" la API garantiza que el JSON
# cumple el esquema, asi que no hay que adivinar si vino bien armado.
# En modo strict todo campo debe estar en "required"; los opcionales se expresan como null.
ESQUEMA_RESPUESTA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mensaje", "estado", "ramo", "motivo_escalamiento", "datos", "faltantes"],
    "properties": {
        "mensaje": {"type": "string"},
        "estado": {
            "type": "string",
            "enum": ["saludo", "recoleccion", "cierre", "escalamiento", "silencio"],
        },
        "ramo": {"anyOf": [{"type": "string", "enum": RAMOS}, {"type": "null"}]},
        "motivo_escalamiento": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        # Lista de pares y no un objeto libre: el modo strict no acepta claves arbitrarias
        "datos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["campo", "valor"],
                "properties": {"campo": {"type": "string"}, "valor": {"type": "string"}},
            },
        },
        "faltantes": {"type": "array", "items": {"type": "string"}},
    },
}


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def contexto_de_fecha() -> str:
    """
    La fecha y hora actual en Santo Domingo, para agregar al final del system prompt.

    Sofia responde 24/7: con esto sabe si el cliente escribe dentro o fuera del horario
    de oficina y puede decirle cuando le van a dar seguimiento.
    """
    ahora = datetime.now(ZONA_NEGOCIO)
    return (
        "\n\n## Fecha y hora actual\n"
        f"Hoy es {DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} "
        f"de {ahora.year}, son las {ahora:%H:%M} (hora de Santo Domingo)."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_escalamiento() -> str:
    """El texto literal con el que Sofia pasa la conversacion a Erika."""
    return (cargar_config_prompts().get("mensaje_escalamiento") or "").strip()


def ya_se_escalo(historial: list[dict]) -> bool:
    """
    True si Sofia ya le paso esta conversacion a Erika (estado E5, silencio).

    Se decide en el codigo y no se le pregunta al modelo: es una regla fija, asi no
    depende de que el modelo la respete, y no se paga una llamada para no decir nada.
    """
    texto = obtener_mensaje_escalamiento()
    return bool(texto) and any(
        m["role"] == "assistant" and texto in m["content"] for m in historial
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro reasoning/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un error de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "reasoning" in texto or "effort" in texto


async def generar_respuesta(mensaje: str, historial: list[dict]) -> tuple[str, bool]:
    """
    Genera una respuesta con OpenAI (API de Responses).

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.

        Si "texto" viene vacio, Sofia decidio NO contestar (la conversacion ya se le
        paso a Erika). Quien llama no debe enviar nada.
    """
    global _soporta_esfuerzo

    # Va primero: despues de escalar no se contesta nada, ni siquiera el fallback
    if ya_se_escalo(historial):
        logger.info("Conversacion ya escalada a Erika: Sofia no responde")
        return "", False

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    problema = problema_de_configuracion()
    if problema:
        logger.error(f"No se puede llamar a OpenAI: {problema}")
        return obtener_mensaje_error(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt() + contexto_de_fecha()
    extras = {"reasoning": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        return await client.responses.create(
            model=MODELO,
            instructions=system_prompt,
            input=mensajes,
            max_output_tokens=MAX_TOKENS,
            # El historial lo guardamos nosotros (memory.py) y en cada llamada se manda
            # completo. No hace falta que OpenAI tambien guarde las conversaciones de
            # los clientes, que traen datos personales.
            store=False,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "respuesta_sofia",
                    "strict": True,
                    "schema": ESQUEMA_RESPUESTA,
                }
            },
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO} no acepta reasoning.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a OpenAI: {e2}")
                return obtener_mensaje_error(), False
        else:
            logger.error(f"Error llamando a OpenAI: {e}")
            return obtener_mensaje_error(), False

    detalles = getattr(respuesta, "incomplete_details", None)
    if getattr(respuesta, "status", None) == "incomplete" and detalles is not None:
        if getattr(detalles, "reason", None) == "max_output_tokens":
            logger.warning(
                f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
                "Si pasa seguido, sube OPENAI_MAX_OUTPUT_TOKENS o acorta el system prompt."
            )
        else:
            logger.warning(f"Respuesta incompleta: {getattr(detalles, 'reason', '?')}")

    # output_text junta solo los bloques de texto: la salida tambien puede traer
    # bloques de razonamiento, que no son para el cliente.
    try:
        resultado = json.loads(respuesta.output_text or "")
    except ValueError:
        # Pasa si la respuesta se corto a la mitad del JSON. Nunca se manda el JSON
        # crudo al cliente.
        logger.warning("OpenAI devolvio una respuesta que no es JSON valido")
        return obtener_mensaje_fallback(), False

    estado = resultado.get("estado")
    texto = (resultado.get("mensaje") or "").strip()

    uso = respuesta.usage
    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({uso.input_tokens if uso else '?'} in / {uso.output_tokens if uso else '?'} out) "
        f"— estado={estado} ramo={resultado.get('ramo')} faltantes={resultado.get('faltantes')}"
    )

    if estado == "silencio":
        return "", False

    if estado == "escalamiento":
        # Siempre el texto literal: es lo que ya_se_escalo() busca en el historial para
        # que Sofia deje de responder despues.
        texto = obtener_mensaje_escalamiento() or texto

    if estado in ("cierre", "escalamiento"):
        # Por ahora el expediente queda en el log. Hacerlo llegar a Erika es el paso siguiente.
        datos = {d["campo"]: d["valor"] for d in resultado.get("datos") or []}
        logger.info(
            f"EXPEDIENTE ({estado}) ramo={resultado.get('ramo')} "
            f"motivo={resultado.get('motivo_escalamiento')} datos={json.dumps(datos, ensure_ascii=False)}"
        )

    if not texto:
        logger.warning("OpenAI devolvio un mensaje vacio fuera del estado de silencio")
        return obtener_mensaje_fallback(), False

    return texto, True
