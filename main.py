import os
from datetime import datetime
import logging
from typing import Optional

from fastapi import FastAPI, Request, BackgroundTasks
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

# Edición de EM's con la que se sincroniza Guest Management, en ambos sentidos.
# Al cambiar de sede basta con tocar estas dos variables (o las env vars del deploy).
#   ACTIVE_PROGRAM -> valor del campo 'Program' (program_3) en el objeto ems
#   ACTIVE_EM_LIST -> api_slug de la lista de EM's de esa edición
ACTIVE_PROGRAM = os.getenv("ACTIVE_PROGRAM", "Mexico 2026")
ACTIVE_EM_LIST = os.getenv("ACTIVE_EM_LIST", "em_s_mexico")

# Todas las listas de EM's conocidas: list_id -> api_slug (para identificar el origen del webhook)
EM_LISTS = {
    "142410f3-47fe-4852-b445-6af86afd2e40": "em_s_menorca",
    "d8f6c4ed-0d1b-46b5-848b-fa8aea579922": "em_s_mexico",
}

# Guest Management (lista sobre el objeto people)
GUEST_MANAGEMENT_LIST_ID = "3ec780d4-5d83-4d2a-8e09-4f68bf749fa2"
GUEST_MANAGEMENT_SLUG = "guest_management"

# Fechas: atributo en la entrada de EM's  <->  atributo en la entrada de Guest Management
EM_TO_GM_DATE = {
    "arrival_date": "arrival_date_58",
    "departure_date": "departure_date_1",
}
GM_TO_EM_DATE = {gm: em for em, gm in EM_TO_GM_DATE.items()}

# Guest Management: atributo de fecha  ->  atributo 'status' con el día del mes
GM_DATE_TO_DAY_STATUS = {
    "arrival_date_58": "arrival_day_status",
    "departure_date_1": "departure_day_status",
}


def get_day_from_iso(date_str: Optional[str]) -> Optional[str]:
    """Día del mes ('1'..'31') a partir de una fecha ISO. None si no se puede parsear."""
    if not date_str:
        return None
    try:
        return str(datetime.fromisoformat(date_str.replace("Z", "+00:00")).day)
    except (ValueError, TypeError):
        return None


def to_attio_date(date_str: Optional[str]) -> Optional[str]:
    """Normaliza una fecha ISO a 'YYYY-MM-DD'. None si no se puede parsear."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# ───────────────────────────────────────────────────────────────────────────
# CLIENTE DE LA API
# ───────────────────────────────────────────────────────────────────────────
class AttioClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ---------- helpers ----------------------------------------------------

    @staticmethod
    def _first_value(entry_values: dict, slug: str):
        """Valor de un atributo dentro de entry_values (fecha, texto o 'status')."""
        vals = entry_values.get(slug, [])
        if isinstance(vals, list) and vals:
            first = vals[0]
            if isinstance(first, dict):
                if first.get("value") is not None:
                    return first.get("value")
                status = first.get("status")
                if isinstance(status, dict):
                    return status.get("title")
        return None

    @staticmethod
    def _wrap(values: dict) -> dict:
        """Da a cada valor el formato que espera Attio en entry_values:
        - status (arrival_day_status/departure_day_status): el título como string
        - fecha/texto: [{'value': 'x'}]"""
        status_slugs = set(GM_DATE_TO_DAY_STATUS.values())
        out: dict = {}
        for slug, v in values.items():
            out[slug] = v if slug in status_slugs else [{"value": v}]
        return out

    # ---------- lecturas -------------------------------------------------

    async def get_list_entry(self, client: httpx.AsyncClient, list_slug: str, entry_id: str) -> dict:
        url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def get_associated_person(self, client: httpx.AsyncClient, ems_record_id: str) -> Optional[str]:
        url = f"{BASE_URL}/objects/ems/records/{ems_record_id}"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()
        people = resp.json().get("data", {}).get("values", {}).get("associated_person", [])
        if isinstance(people, list) and people:
            return people[0].get("target_record_id")
        return None

    async def find_active_ems_records(self, client: httpx.AsyncClient, person_id: str) -> list:
        """ems records de la persona cuyo Program == ACTIVE_PROGRAM."""
        url = f"{BASE_URL}/objects/ems/records/query"
        payload = {
            "filter": {
                "associated_person": {"target_object": "people", "target_record_id": person_id},
                "program_3": ACTIVE_PROGRAM,
            },
            "limit": 50,
        }
        resp = await client.post(url, headers=self.headers, json=payload)
        resp.raise_for_status()
        out = []
        for rec in resp.json().get("data", []):
            rid = rec.get("id", {}).get("record_id")
            if rid:
                out.append(rid)
        return out

    async def _query_entries_by_path(self, client: httpx.AsyncClient, list_slug: str,
                                     path: list, constraints: dict) -> list:
        """Query de entradas de lista filtrando por un atributo del registro padre.
        Attio no acepta filtrar por `parent_record_id`; hay que usar un filtro 'path'.
        Devuelve las entradas completas (con entry_values y parent_record_id)."""
        url = f"{BASE_URL}/lists/{list_slug}/entries/query"
        payload = {"filter": {"path": path, "constraints": constraints}, "limit": 50}
        resp = await client.post(url, headers=self.headers, json=payload)
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def find_guest_entry_for_person(self, client: httpx.AsyncClient, person_id: str) -> Optional[dict]:
        entries = await self._query_entries_by_path(
            client, GUEST_MANAGEMENT_SLUG,
            [[GUEST_MANAGEMENT_SLUG, "parent_record"], ["people", "record_id"]],
            {"value": person_id},
        )
        return entries[0] if entries else None

    async def find_em_entries_for_person(self, client: httpx.AsyncClient, person_id: str) -> list:
        """Entradas de ACTIVE_EM_LIST cuyo registro `ems` padre tiene a esa persona en associated_person."""
        return await self._query_entries_by_path(
            client, ACTIVE_EM_LIST,
            [[ACTIVE_EM_LIST, "parent_record"], ["ems", "associated_person"]],
            {"target_object": "people", "target_record_id": person_id},
        )

    # ---------- escrituras ---------------------------------------------

    async def ensure_day_status_option(self, client: httpx.AsyncClient, attribute_slug: str, day: str):
        """arrival_day_status / departure_day_status son de tipo 'status': Attio solo acepta
        opciones ya existentes. Crea la opción del día si falta."""
        url = f"{BASE_URL}/lists/{GUEST_MANAGEMENT_SLUG}/attributes/{attribute_slug}/statuses"
        resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()
        titles = {s.get("title") for s in resp.json().get("data", [])}
        if day in titles:
            return
        logger.info(f"Creando opción de status '{day}' en {attribute_slug}")
        created = await client.post(url, headers=self.headers, json={"data": {"title": day}})
        created.raise_for_status()

    async def day_status_values(self, client: httpx.AsyncClient, date_updates: dict) -> dict:
        """{gm_date_slug: 'YYYY-MM-DD'} -> {day_status_slug: 'DD'}, asegurando la opción.
        Si Attio rechaza crear la opción, se omite ese día (no aborta la sincronización)."""
        out: dict = {}
        for gm_slug, date_val in date_updates.items():
            day_slug = GM_DATE_TO_DAY_STATUS.get(gm_slug)
            day = get_day_from_iso(date_val)
            if not (day_slug and day):
                continue
            try:
                await self.ensure_day_status_option(client, day_slug, day)
                out[day_slug] = day
            except httpx.HTTPStatusError as e:
                logger.error(f"No se pudo crear la opción de día '{day}' en {day_slug}: {e.response.text}")
        return out

    async def upsert_guest_entry(self, client: httpx.AsyncClient, person_id: str, values: dict):
        """Upsert (por persona) de la entrada de Guest Management. `values`: {slug: escalar}."""
        if not values:
            return None
        payload = {
            "data": {
                "parent_record_id": person_id,
                "parent_object": "people",
                "entry_values": self._wrap(values),
            }
        }
        resp = await client.put(f"{BASE_URL}/lists/{GUEST_MANAGEMENT_SLUG}/entries", headers=self.headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def patch_entry(self, client: httpx.AsyncClient, list_slug: str, entry_id: str, values: dict):
        """PATCH de una entrada concreta (solo toca los atributos indicados). `values`: {slug: escalar}."""
        if not values:
            return None
        url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
        resp = await client.patch(url, headers=self.headers, json={"data": {"entry_values": self._wrap(values)}})
        resp.raise_for_status()
        return resp.json()


# ───────────────────────────────────────────────────────────────────────────
# APP
# ───────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Sync de fechas EM's <-> Guest Management")
attio = AttioClient(ATTIO_TOKEN)


async def sync_em_to_guest(client: httpx.AsyncClient, list_slug: str, entry_id: str):
    """Dirección A: se editó una entrada de EM's (edición activa) -> Guest Management."""
    em_entry = await attio.get_list_entry(client, list_slug, entry_id)
    em_values = em_entry.get("entry_values", {})

    ems_record_id = em_entry.get("parent_record_id")
    if not ems_record_id:
        logger.warning(f"Entrada {entry_id} de {list_slug} sin parent_record_id")
        return

    person_id = await attio.get_associated_person(client, ems_record_id)
    if not person_id:
        logger.warning(f"ems {ems_record_id} sin associated_person; no se sincroniza")
        return

    gm_entry = await attio.find_guest_entry_for_person(client, person_id)
    gm_values = (gm_entry or {}).get("entry_values", {})

    # No-op guard: solo escribimos las fechas que realmente cambian.
    date_updates: dict = {}
    for em_slug, gm_slug in EM_TO_GM_DATE.items():
        new = to_attio_date(attio._first_value(em_values, em_slug))
        if new is None:
            continue  # fecha vacía en EM's -> no se toca la de Guest Management
        current = to_attio_date(attio._first_value(gm_values, gm_slug))
        if new != current:
            date_updates[gm_slug] = new

    if not date_updates:
        logger.info(f"Guest Management ya al día para persona {person_id}")
        return

    values = dict(date_updates)
    values.update(await attio.day_status_values(client, date_updates))
    await attio.upsert_guest_entry(client, person_id, values)
    logger.info(f"EM's -> Guest Management OK (persona {person_id}): {date_updates}")


async def sync_guest_to_em(client: httpx.AsyncClient, entry_id: str):
    """Dirección B: se editó una entrada de Guest Management -> EM's (edición activa)."""
    gm_entry = await attio.get_list_entry(client, GUEST_MANAGEMENT_SLUG, entry_id)
    gm_values = gm_entry.get("entry_values", {})

    person_id = gm_entry.get("parent_record_id")
    if not person_id:
        logger.warning(f"Entrada GM {entry_id} sin parent_record_id")
        return

    gm_dates: dict = {}  # gm_slug -> 'YYYY-MM-DD'
    for gm_slug in EM_TO_GM_DATE.values():
        val = to_attio_date(attio._first_value(gm_values, gm_slug))
        if val:
            gm_dates[gm_slug] = val

    # --- B.1  Guest Management -> entradas de EM's de esa persona en la edición activa
    active_ems = set(await attio.find_active_ems_records(client, person_id))
    if not active_ems:
        logger.info(f"Persona {person_id} sin ems en '{ACTIVE_PROGRAM}'; no se sincroniza a EM's")

    em_entries = await attio.find_em_entries_for_person(client, person_id) if active_ems else []
    # decisión: solo se tocan entradas ya existentes y del programa activo (no se crean)
    em_entries = [e for e in em_entries if e.get("parent_record_id") in active_ems]
    if active_ems and not em_entries:
        logger.info(f"Persona {person_id} sin entrada en {ACTIVE_EM_LIST}; no se crea nada")

    for em_entry in em_entries:
        em_values = em_entry.get("entry_values", {})
        em_entry_id = em_entry.get("id", {}).get("entry_id")

        updates: dict = {}
        for gm_slug, val in gm_dates.items():
            em_slug = GM_TO_EM_DATE[gm_slug]
            current = to_attio_date(attio._first_value(em_values, em_slug))
            if val != current:
                updates[em_slug] = val

        if updates:
            await attio.patch_entry(client, ACTIVE_EM_LIST, em_entry_id, updates)
            logger.info(f"Guest Management -> EM's OK (entry {em_entry_id}): {updates}")
        else:
            logger.info(f"EM's entry {em_entry_id} ya al día")

    # --- B.2  recalcular el día del mes en la propia entrada de Guest Management
    day_date_updates: dict = {}
    for gm_slug, val in gm_dates.items():
        day_slug = GM_DATE_TO_DAY_STATUS[gm_slug]
        day = get_day_from_iso(val)
        current_day = attio._first_value(gm_values, day_slug)
        if day and (current_day is None or str(current_day) != day):
            day_date_updates[gm_slug] = val

    if day_date_updates:
        day_values = await attio.day_status_values(client, day_date_updates)
        if day_values:
            await attio.patch_entry(client, GUEST_MANAGEMENT_SLUG, entry_id, day_values)
            logger.info(f"Guest Management día-status recalculado (persona {person_id}): {day_values}")


async def process_webhook(list_id: str, entry_id: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if list_id == GUEST_MANAGEMENT_LIST_ID:
                await sync_guest_to_em(client, entry_id)
            elif EM_LISTS.get(list_id) == ACTIVE_EM_LIST:
                await sync_em_to_guest(client, ACTIVE_EM_LIST, entry_id)
            else:
                logger.info(f"Lista {list_id} fuera de scope; se ignora")
    except httpx.HTTPStatusError as e:
        logger.error(f"Error de API de Attio: {e.response.text}")
    except Exception:
        logger.exception("Error inesperado procesando el webhook")


@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    events = payload.get("events", [])
    if not events:
        return {"status": "no events"}

    accepted = 0
    for event in events:
        actor_type = event.get("actor", {}).get("type")
        event_type = event.get("event_type", "") or ""
        ids = event.get("id", {})
        list_id = ids.get("list_id")
        entry_id = ids.get("entry_id")

        # Solo ediciones/altas hechas por una persona. Los cambios del propio servicio
        # (actor api-token) o del sistema se ignoran -> corta el bucle A <-> B.
        if actor_type != "workspace-member":
            continue
        if "delete" in event_type:
            continue
        if not entry_id:
            continue

        in_scope = (list_id == GUEST_MANAGEMENT_LIST_ID) or (EM_LISTS.get(list_id) == ACTIVE_EM_LIST)
        if not in_scope:
            continue

        background_tasks.add_task(process_webhook, list_id, entry_id)
        accepted += 1

    return {"status": "accepted", "tasks": accepted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
