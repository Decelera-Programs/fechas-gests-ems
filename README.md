# Fechas Gests EMs

Servicio FastAPI que sincroniza automáticamente las fechas de llegada y salida desde las listas **EM's Menorca** y **EM's Mexico** hacia la lista **Guest Management** en [Attio](https://attio.com/). Se ejecuta como un webhook: cuando un miembro del workspace edita una entrada en cualquiera de las listas de EM's, las fechas correspondientes se propagan a la persona asociada en Guest Management.

## ¿Qué problema resuelve?

En Attio mantenemos varias listas relacionadas:

- **EM's Menorca** (`em_s_menorca`) y **EM's Mexico** (`em_s_mexico`) — registros operativos de cada Encuentro de Mentores, ambos sobre el objeto `ems`, incluyendo `arrival_date` y `departure_date` de cada participante.
- **Guest Management** (`guest_management`) — vista centralizada de huéspedes (objeto `people`), donde los responsables de hospitality consultan las fechas.

Mantener estas listas sincronizadas a mano es propenso a errores. Este servicio escucha cambios en cualquiera de las listas de EM's y replica las fechas (más el día del mes, usado para vistas/agrupaciones rápidas) en la entrada de Guest Management de la persona asociada.

## Flujo

```
Attio (EM's Menorca | EM's Mexico)
        │
        │  webhook on entry update
        ▼
   POST /webhook
        │
        ├── Filtro: actor = workspace-member, list ∈ EM_LISTS
        │
        ├── GET  /lists/{em_s_menorca|em_s_mexico}/entries/{entry_id}  → arrival, departure
        ├── GET  /objects/ems/records/{record_id}                     → associated_person
        └── PUT  /lists/guest_management/entries                      → upsert por persona
                  · arrival_date_58, arrival_day_status
                  · departure_date_1, departure_day_status
```

El procesado se ejecuta en `BackgroundTasks` para devolver `202 accepted` al webhook inmediatamente y evitar reintentos por timeout.

## Endpoints

| Método | Ruta       | Descripción                                                                 |
| ------ | ---------- | --------------------------------------------------------------------------- |
| POST   | `/webhook` | Recibe el payload de Attio. Ignora eventos que no sean de un workspace-member sobre alguna de las listas EM's soportadas (EM_LISTS). |

Respuestas posibles:

- `{"status": "no events"}` — payload sin eventos.
- `{"status": "ignored"}` — evento fuera del scope (otro actor u otra lista).
- `{"status": "accepted"}` — evento aceptado; la sincronización corre en background.

## Stack

- **Python 3.10+**
- [FastAPI](https://fastapi.tiangolo.com/) — servidor HTTP asíncrono
- [httpx](https://www.python-httpx.org/) — cliente HTTP async hacia la API de Attio
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [python-dotenv](https://pypi.org/project/python-dotenv/) — carga de variables de entorno

## Configuración

Crea un archivo `.env` en la raíz del proyecto:

```env
ATTIO_TOKEN=tu_token_de_attio_aqui
```

El token debe tener permisos de lectura sobre:
- `lists/em_s_menorca/entries`
- `lists/em_s_mexico/entries`
- `objects/ems/records`
- `list_configuration:read` (para leer las opciones existentes de `arrival_day_status`/`departure_day_status`)

Y escritura sobre:
- `lists/guest_management/entries`
- `list_configuration:read-write` (para poder crear una nueva opción de día cuando la fecha cae en un día del mes que aún no existe como status)

> Las listas de EM's soportadas (`EM_LISTS`) están fijadas en [main.py](main.py) como un diccionario `list_id -> api_slug`. Para añadir una nueva sede (o si cambias de workspace), añade su entrada ahí.

## Instalación y ejecución local

```bash
# 1. Crear entorno virtual
python -m venv venv
source venv/bin/activate          # macOS / Linux
# .\venv\Scripts\activate         # Windows

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar .env (ver sección anterior)

# 4. Arrancar el servicio
python main.py
# o, equivalentemente:
uvicorn main:app --host 0.0.0.0 --port 8000
```

El servicio queda escuchando en `http://0.0.0.0:8000`.

## Exponer el webhook a Attio

En desarrollo, expón el puerto 8000 con [ngrok](https://ngrok.com/) o similar y registra la URL pública (`https://<algo>.ngrok.io/webhook`) en la configuración de webhooks de Attio para la lista EM's Menorca.

En producción, despliega tras un dominio HTTPS estable (Railway, Fly.io, Render, etc.) y registra esa URL.

## Mapeo de campos

| Origen (EM's Menorca / EM's Mexico) | Destino (Guest Management) | Transformación                       |
| ---------------------- | -------------------------- | ------------------------------------ |
| `arrival_date`         | `arrival_date_58`          | ISO → `YYYY-MM-DD`                   |
| `arrival_date`         | `arrival_day_status`       | ISO → día del mes (`"1"`–`"31"`)     |
| `departure_date`       | `departure_date_1`         | ISO → `YYYY-MM-DD`                   |
| `departure_date`       | `departure_day_status`     | ISO → día del mes (`"1"`–`"31"`)     |
| `associated_person[0]` | `parent_record_id`         | Se resuelve desde el objeto `ems`    |

Si una fecha viene vacía, simplemente no se incluye en el payload (no se sobrescribe).

`arrival_day_status`/`departure_day_status` son campos de tipo **status** en Attio: solo aceptan valores (días del mes) que ya existan como opción predefinida, y la API rechaza cualquier valor nuevo en vez de crearlo. Antes de cada upsert, el servicio comprueba (`GET /lists/guest_management/attributes/{attr}/statuses`) si el día ya existe como opción y, si no, la crea (`POST` al mismo endpoint) antes de escribir el valor. Esto es necesario porque las opciones actuales solo cubren los días usados hasta ahora en Menorca; EM's Mexico probablemente introducirá días nuevos.

## Estructura

```
.
├── main.py            # App FastAPI, cliente Attio y handler del webhook
├── requirements.txt   # Dependencias mínimas
├── .gitignore
└── README.md
```

## Notas operativas

- **Logs**: la app usa el logger estándar de Python en nivel `INFO`. Los errores de la API de Attio se registran con el cuerpo de la respuesta para facilitar el diagnóstico.
- **Idempotencia**: `PUT /lists/guest_management/entries` se comporta como upsert por `parent_record_id` (la persona). Reenviar el mismo evento es seguro.
- **Errores silenciosos**: como el procesado va en background, los fallos no devuelven `5xx` al webhook — revisa los logs si las fechas no aparecen en Guest Management.
