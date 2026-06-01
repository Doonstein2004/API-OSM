# OSM Discord Bot — Contexto del Proyecto

## Qué es esto

Bot de Discord que automatiza la gestión de equipos en **Online Soccer Manager (OSM)**,
un juego de manager de fútbol online. El bot corre en un **Orange Pi 5 (8GB RAM)** y
controla el navegador vía Playwright para interactuar con OSM como si fuera un humano.

Objetivo a largo plazo: evolucionar hacia un **sistema de agentes autónomos** que tomen
decisiones de gestión (ventas, tácticas, entrenamientos) basadas en análisis de datos,
con mínima intervención del usuario.

---

## Stack tecnológico

- **Python 3.12+** con `uv` como gestor de paquetes
- **discord.py** — bot y slash commands
- **Playwright** — automatización del navegador (Chromium headless)
- **Knockout.js (KO.js)** — el frontend de OSM. Siempre preferir acceso via `ko.contextFor()` sobre DOM scraping
- **PostgreSQL** — almacenamiento de datos de ligas, tácticas, fichajes, sesiones
- **psycopg2** — driver PostgreSQL
- **httpx** — llamadas HTTP directas (LLM client)
- **Ollama** (local, ARM64) — LLM en el Orange Pi 5. Modelo recomendado: `phi3.5` (~2.3GB RAM)
- **Anthropic API** — fallback cloud para el LLM

---

## Arquitectura de archivos

```
discord_bot.py          ← Entry point. Comandos, loops, embeds, storage
utils.py                ← Login, session cache, popup handler, navegación
scraper_*.py            ← Extracción de datos (solo lectura)
action_*.py             ← RPA que modifica estado en OSM (escritura)
agent_*.py              ← Agentes IA (análisis + decisión via LLM)
llm_client.py           ← Abstracción LLM (Ollama / Anthropic)
training_queue.json     ← Config persistente: jugadores programados por coach
transfer_queue.json     ← Config persistente: candidatos a venta
DOCS.md                 ← Documentación completa para el usuario
CLAUDE.md               ← Este archivo
.env.example            ← Template de variables de entorno
```

---

## Patrones del codebase

### 1. Todo lo que abre el navegador es sync y corre en thread
```python
# En discord_bot.py, SIEMPRE:
result = await asyncio.to_thread(_scrape_something_sync, user_id, league_name)

# La función sync tiene este patrón:
def _scrape_something_sync(user_id, league_name):
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = do_something(page, league_name)
            context.close(); browser.close()
            return result
    finally:
        conn.close()
```

### 2. KO.js primero, DOM fallback
```python
# scraper_squad.py y otros: siempre intentar KO primero
players = _extract_players_ko(page)
if not players:
    players = _extract_players_dom(page)
```

Para acceder al KO viewmodel:
```js
const root = ko.contextFor(document.body)?.$root;
// O para un elemento específico:
const ctx = ko.contextFor(document.querySelector('#squad-table'));
const root = ctx.$root || ctx.$data;
```

Observables son funciones en KO: `v(obs.name)` donde `v = (o) => typeof o === 'function' ? o() : o`.

### 3. Activación de slots
```python
# Patrón estándar para activar un slot por league_name:
from utils import click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

page.goto(CAREER_URL, ...)
wait_for_visible_slots(page)
slots = page.locator(".career-teamslot")
for i in range(slots.count()):
    _, t_league, _ = get_slot_info(slots.nth(i))
    if league_name.lower() in (t_league or "").lower():
        click_slot_and_wait_for_dashboard(page, i)
        break
```

### 4. Storage persistente
```python
# Mismo patrón para training_queue.json y transfer_queue.json:
_queue: dict = {}

def _load_queue():
    global _queue
    if os.path.exists(FILE):
        with open(FILE, encoding="utf-8") as f:
            _queue = json.load(f)

def _save_queue():
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(_queue, f, ensure_ascii=False, indent=2)
```

### 5. Loops background
```python
@tasks.loop(minutes=N)
async def _some_loop():
    if not DISCORD_ALERT_CHANNEL_ID: return
    channel = client.get_channel(DISCORD_ALERT_CHANNEL_ID)
    # ... lógica async usando asyncio.to_thread() para sync calls
```

### 6. Slash commands
```python
@tree.command(name="cmd", description="...")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_something(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        # ... lógica
        await interaction.followup.send(embed=some_embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
```

---

## Convenciones

- **Sin comentarios obvios.** Solo documentar el "por qué" no obvio.
- **Sin manejo de errores para casos imposibles.** Confiar en garantías del framework.
- **KO.js viewmodel > DOM** para leer datos de OSM.
- **Batch > individual** cuando hay múltiples slots. Una sola sesión Playwright para N equipos.
- **Documentar en DOCS.md** cualquier nueva feature antes de cerrar el trabajo.
- **Validar sintaxis** con `python -c "import ast; ast.parse(open('file.py', encoding='utf-8').read())"` después de cada edición.

---

## Estado actual del proyecto (Jun 2026)

### Funcionalidades completas y en producción
- Sistema de timers (lectura + alertas automáticas)
- Auto-renovación de entrenamientos (con cola de jugadores programados)
- Auto-upgrade de estadio
- Gestión de lista de transferibles (manual + auto cada 2h)
- Análisis de rival: últimos partidos + spy de /DataAnalist
- Plantilla en tiempo real (/squad)
- Tácticas y formación (lectura y escritura via RPA)
- Calendario de eventos OSM

### En desarrollo / pendiente verificación
- `get_spy_results()` en `scraper_data_analyst.py`: los observables KO dentro de `spyInstructionPartial()` son especulativos. Necesitan verificación con el DOM real post-spy (el spy tarda 1h en completarse).
- La tabla de resultados de `/League/Results` puede tener variaciones de estructura entre ligas.

### Arquitectura de agentes (futuro)
La visión es que los agentes (`agent_transfer.py`, `agent_tactics.py`) corran autónomamente en el Orange Pi 5 usando Ollama (modelo local, sin costo de API). Por ahora están disponibles como comandos manuales. El loop autónomo (`_agent_transfer_loop`) se activa con `ENABLE_AGENT_LOOP=true` en `.env` una vez que Ollama esté instalado y configurado.

**Para activar en Orange Pi 5:**
```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull phi3.5          # ~2.3GB RAM — deja margen para Playwright
# en .env:
ENABLE_AGENT_LOOP=true
LLM_PROVIDER=ollama
```

---

## Variables de entorno clave

Ver `.env.example` para la lista completa. Las más importantes:

```bash
# OSM
MI_USUARIO=...
MI_CONTRASENA=...

# BD PostgreSQL
DB_HOST=... DB_PORT=5432 DB_NAME=... DB_USER=... DB_PASSWORD=...

# Discord
DISCORD_BOT_TOKEN=...
DISCORD_OWNER_ID=...          # Tu user ID — solo tú usas el bot
DISCORD_ALERT_CHANNEL_ID=...  # Canal para alertas automáticas
OSM_USER_ID=...               # UUID del usuario en tabla users

# Timers
TIMER_CHECK_MINUTES=20        # Frecuencia del loop de alertas
TIMER_WARNING_MINUTES=5       # Aviso anticipado de timers

# Agentes (desactivados por defecto)
ENABLE_AGENT_LOOP=false
LLM_PROVIDER=auto
OLLAMA_MODEL=phi3.5
```

---

## Notas para Claude

1. **El juego se llama OSM** (Online Soccer Manager). Las URLs son `en.onlinesoccermanager.com`.
2. **KO.js es el framework del frontend** de OSM. Acceder via `ko.contextFor()` es más fiable que DOM scraping.
3. **El bot tiene hasta 4 slots** (4 equipos gestionados simultáneamente). El código itera sobre `NUM_SLOTS = 4`.
4. **Las temporadas tienen jornadas** (matchday). `matchday.finished = current >= total`. Cuando la temporada termina, no se deben ejecutar automatizaciones (entrenamiento, estadio).
5. **Los eventos de OSM reducen los timers** (ej. "Extreme Training" reduce timers de entrenamiento). El bot espera si un evento está próximo (`EVENT_DELAY_HOURS`).
6. **El scraper de timers** clasifica por keywords (texto + clases CSS + data-bind). Si un timer no aparece, revisar `_KEYWORD_MAP` en `scraper_timers.py`.
7. **La sesión del navegador se cachea** en `user_browser_sessions` de PostgreSQL. TTL 18h. Si hay errores de autenticación, limpiar esta tabla.
8. **Transfer Madness** es un evento que permite 6 jugadores en la lista de transferibles (en vez de 4). El bot lo detecta automáticamente via `maxPlayersOnTransferlist()`.
