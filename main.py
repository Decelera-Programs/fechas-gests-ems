import os
from datetime import datetime
import logging
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ───────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ATTIO_TOKEN = os.getenv("ATTIO_TOKEN")
BASE_URL = "https://api.attio.com/v2"

# Listas de EM's soportadas: list_id -> api_slug de la lista
EM_LISTS = {
    "142410f3-47fe-4852-b445-6af86afd2e40": "em_s_menorca",
    "d8f6c4ed-0d1b-46b5-848b-fa8aea579922": "em_s_mexico",
}

def get_day_from_iso(date_str: str) -> Optional[str]:
    """Extrae el número del día en una cadena de texto"""
    if not date_str:
        return None
    try:
        clean_date = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_date)
        return str(dt.day)
    except (ValueError, TypeError):
        return None
    
def to_attio_date(date_str: str) -> Optional[str]:
    if not date_str:
        return None
    try:
        clean = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

# ───────────────────────────────────────────────────────────────────────────
# CLIENTE DE LA API
# ───────────────────────────────────────────────────────────────────────────

class AttioClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
            }

    async def get_entry_dates(self, client: httpx.AsyncClient, list_slug: str, entry_id: str):
        url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()

        data = resp.json().get("data", {})
        entry_values = data.get("entry_values", {})

        # Función interna rápida para extraer valores de forma segura
        def safe_extract(field_name):
            values = entry_values.get(field_name, [])
            # Verificamos que la lista exista y tenga al menos un elemento
            if isinstance(values, list) and len(values) > 0:
                return values[0].get("value")
            return None

        arrival = safe_extract("arrival_date")
        departure = safe_extract("departure_date")

        return arrival, departure

    async def get_associated_person(self, client: httpx.AsyncClient, record_id: str):
        url = f"{BASE_URL}/objects/ems/records/{record_id}"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()

        values = resp.json().get("data", {}).get("values", {})
        person_list = values.get("associated_person", [{}])
        return person_list[0].get("target_record_id")

    async def ensure_day_status_option(self, client: httpx.AsyncClient, attribute_slug: str, day: Optional[str]):
        """Los campos arrival_day_status/departure_day_status son de tipo 'status':
        Attio rechaza valores que no existan ya como opción, no los crea solos.
        Aquí comprobamos si el día ya existe como opción y, si no, la creamos."""
        if not day:
            return

        url = f"{BASE_URL}/lists/guest_management/attributes/{attribute_slug}/statuses"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()

        existing_titles = {status.get("title") for status in resp.json().get("data", [])}
        if day in existing_titles:
            return

        logger.info(f"Creando opción de status '{day}' en {attribute_slug}")
        create_resp = await client.post(url, headers=self.headers, json={"data": {"title": day}})
        create_resp.raise_for_status()

    async def upsert_guest_entry(self, client: httpx.AsyncClient, person_id: str, arrival: str, departure: str):
        url = f"{BASE_URL}/lists/guest_management/entries"
        payload = {
            "data": {
                "parent_record_id": person_id,
                "parent_object": "people",
                "entry_values": {
                }
            }
        }

        if arrival:
            arrival_day = get_day_from_iso(arrival)
            await self.ensure_day_status_option(client, "arrival_day_status", arrival_day)
            payload["data"]["entry_values"]["arrival_date_58"] = [{"value": to_attio_date(arrival)}]
            payload["data"]["entry_values"]["arrival_day_status"] = [{"value": arrival_day}]
        if departure:
            departure_day = get_day_from_iso(departure)
            await self.ensure_day_status_option(client, "departure_day_status", departure_day)
            payload["data"]["entry_values"]["departure_date_1"] = [{"value": to_attio_date(departure)}]
            payload["data"]["entry_values"]["departure_day_status"] = [{"value": departure_day}]

        resp = await client.put(url, headers=self.headers, json=payload)
        resp.raise_for_status()
        return resp.json()

# ───────────────────────────────────────────────────────────────────────────
# APP
# ───────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Fecha de EM's a Guests Management")
attio = AttioClient(ATTIO_TOKEN)

# 1. Extraemos la lógica en una función separada async
async def process_webhook(list_slug: str, entry_id: str, parent_record_id: str):
    try:
        async with httpx.AsyncClient() as client:
            arrival, departure = await attio.get_entry_dates(client, list_slug, entry_id)
            person_id = await attio.get_associated_person(client, parent_record_id)

            if not person_id:
                raise ValueError("No se encontró persona asociada")

            result = await attio.upsert_guest_entry(client, person_id, arrival, departure)
            logger.info(f"Sincronización exitosa para person_id: {person_id}")

    except httpx.HTTPStatusError as e:
        logger.error(f"Error de API de Attio: {e.response.text}")
    except Exception as e:
        logger.exception("Error inesperado procesando el webhook")


# 2. En el endpoint, añadimos la tarea y retornamos de inmediato
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    events = payload.get("events", [])

    if not events:
        return {"status": "no events"}

    event = events[0]
    actor_type = event.get("actor", {}).get("type")
    list_id = event.get("id", {}).get("list_id")
    entry_id = event.get("id", {}).get("entry_id")
    parent_record_id = event.get("id", {}).get("record_id")

    list_slug = EM_LISTS.get(list_id)
    if actor_type != "workspace-member" or list_slug is None:
        logger.info(f"Evento ignorado: Actor={actor_type}, List={list_id}")
        return {"status": "ignored"}

    # Registramos la tarea y respondemos inmediatamente
    background_tasks.add_task(process_webhook, list_slug, entry_id, parent_record_id)
    return {"status": "accepted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)