# Fechas EM's ⇄ Guest Management

Servicio FastAPI que mantiene sincronizadas **en los dos sentidos** las fechas de
llegada y salida entre la lista de EM's de la edición activa (**EM's Mexico**) y la
lista **Guest Management** en [Attio](https://attio.com/). Funciona como webhook:
cuando un miembro del workspace edita una fecha en cualquiera de las dos listas, el
cambio se propaga a la otra.

## ¿Qué problema resuelve?

En Attio conviven varias listas relacionadas:

- **EM's Mexico** (`em_s_mexico`) / **EM's Menorca** (`em_s_menorca`) — registros
  operativos de cada Encuentro de Mentores, sobre el objeto `ems`. Cada entrada
  tiene `arrival_date` y `departure_date`.
- **Guest Management** (`guest_management`) — vista de hospitality sobre el objeto
  `people`, con `arrival_date_58` / `departure_date_1` (y los `*_day_status`, que
  guardan el día del mes para agrupar).

Antes la sincronización era solo EM's → Guest Management y estaba rota (el webhook
no trae el `record_id` del padre, así que todas las llamadas fallaban). Ahora:

- Se lee el `parent_record_id` de la propia entrada, no del evento.
- La sincronización es **bidireccional**.
- Solo se escribe cuando la fecha **realmente cambia** (no-op guard).

## El enlace entre listas

La **persona** es la clave de unión:

```
entrada Guest Management ──parent_record──► PERSONA ◄──associated_person── ems record ──parent_record──► entrada EM's
                                                                              │
                                                                      program_3 = ACTIVE_PROGRAM
```

El campo obligatorio `program_3` del objeto `ems` (opción `"Mexico 2026"`) es el
desambiguador: una persona puede tener un `ems` de Menorca y otro de Mexico; solo
se toca el de la edición activa.

## Flujo

```
Attio (EM's Mexico  |  Guest Management)
        │  webhook: list-entry.created / updated
        ▼
   POST /webhook
        │  Filtro por evento: actor == workspace-member, no 'delete',
        │  lista ∈ {ACTIVE_EM_LIST, Guest Management}
        │  (los cambios del propio servicio van con actor api-token → se ignoran → sin bucles)
        │
        ├── Origen EM's ──────────────► Dirección A: sync_em_to_guest
        │     GET entry → parent ems → associated_person (persona)
        │     GET entrada GM de la persona → compara → PUT upsert GM
        │       · arrival_date_58 / departure_date_1
        │       · arrival_day_status / departure_day_status (crea la opción de día si falta)
        │
        └── Origen Guest Management ──► Dirección B: sync_guest_to_em
              GET entry → persona + fechas
              query ems (associated_person = persona AND program_3 = ACTIVE_PROGRAM)
              query entrada de ACTIVE_EM_LIST por parent_record_id
              compara → PATCH entrada EM's (arrival_date / departure_date)
              + recalcula arrival_day_status / departure_day_status en la propia entrada GM
```

Todo el procesado corre en `BackgroundTasks`: el webhook responde
`{"status": "accepted"}` de inmediato y evita reintentos por timeout.

## Configuración

`.env` en la raíz:

```env
ATTIO_TOKEN=tu_token_de_attio_aqui
# Opcionales (valores por defecto entre paréntesis):
ACTIVE_PROGRAM=Mexico 2026        # valor de program_3 en el objeto ems
ACTIVE_EM_LIST=em_s_mexico        # api_slug de la lista de EM's de esa edición
```

**Al cambiar de sede** (p. ej. Menorca 2027): actualiza `ACTIVE_PROGRAM` y
`ACTIVE_EM_LIST`. Nada más.

El token necesita:
- lectura: `lists/{em_s_mexico,guest_management}/entries`, `objects/ems/records`,
  `list_configuration:read`
- escritura: `lists/{em_s_mexico,guest_management}/entries`,
  `list_configuration:read-write` (para crear la opción de día que falte en los
  campos `*_day_status`)

## Webhook en Attio

Registrar `https://<dominio>/webhook` para los eventos
`list-entry.created` y `list-entry.updated` de **EM's Mexico** y **Guest
Management**. El servicio filtra por su cuenta, así que si el webhook manda más
listas no pasa nada (se ignoran).

## Mapeo de campos

| EM's (`em_s_mexico`) | Guest Management        | Transformación                  |
| -------------------- | ---------------------- | ------------------------------- |
| `arrival_date`       | `arrival_date_58`      | ISO → `YYYY-MM-DD`              |
| `departure_date`     | `departure_date_1`     | ISO → `YYYY-MM-DD`              |
| `arrival_date`       | `arrival_day_status`   | ISO → día del mes (`"1"`–`"31"`) |
| `departure_date`     | `departure_day_status` | ISO → día del mes (`"1"`–`"31"`) |

- Fecha vacía en el origen → no se incluye (no se sobrescribe la del destino).
- `*_day_status` son de tipo **status**: solo existen en Guest Management y solo en
  ese sentido. Si el día no existe como opción, el servicio la crea antes de
  escribir; si Attio rechaza crearla, se omite el día pero la fecha sí se escribe.

## Reglas de la dirección Guest Management → EM's

- Solo se actualiza el `ems` cuyo `program_3` == `ACTIVE_PROGRAM`.
- Solo se actualizan entradas de EM's **que ya existen** en `ACTIVE_EM_LIST` (no
  todo el mundo en Guest Management es EM; no se crean entradas nuevas).
- Si hay varios `ems` de la persona en la edición activa, se actualizan todas sus
  entradas presentes en la lista.

## Instalación y ejecución local

```bash
python -m venv venv
.\venv\Scripts\activate          # Windows
# source venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
# configurar .env
python main.py                   # o: uvicorn main:app --host 0.0.0.0 --port 8000
```

En desarrollo, expón el puerto 8000 con ngrok y registra la URL pública en Attio.
En producción se despliega en Railway (proyecto *Attio automations*, servicio
*fechas-gests-ems*).

## Estructura

```
.
├── main.py            # App FastAPI, cliente Attio y las dos direcciones de sync
├── requirements.txt
├── .gitignore
└── README.md
```

## Notas operativas

- **Logs**: logger estándar en `INFO`. Cada sync deja traza de qué persona/entrada
  y qué fechas se han escrito, o de por qué se ha omitido. Los errores de Attio se
  registran con el cuerpo de la respuesta.
- **Anti-bucle**: los eventos cuyo actor no es `workspace-member` se descartan.
  Como el servicio escribe con un token (actor `api-token`), sus propias
  escrituras no re-disparan la sincronización.
- **Idempotencia**: antes de escribir se compara con el valor actual; si coincide,
  no se hace la llamada.
- **Errores silenciosos**: el procesado va en background, los fallos no devuelven
  `5xx` al webhook. Si una fecha no se propaga, revisar los logs de Railway.
```
