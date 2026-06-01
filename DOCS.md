# OSM Discord Bot — Documentación

Bot de Discord para automatizar y monitorear equipos en **Online Soccer Manager (OSM)**.
Usa Playwright para controlar el navegador como un humano, extrayendo datos y ejecutando acciones
directamente en `en.onlinesoccermanager.com`. Diseñado para correr en **Orange Pi 5 (8GB RAM)**.

---

## Índice

1. [Arquitectura](#1-arquitectura)
2. [Configuración `.env`](#2-configuración-env)
3. [Puesta en marcha](#3-puesta-en-marcha)
4. [Comandos slash](#4-comandos-slash)
5. [Automatización en background](#5-automatización-en-background)
6. [Cola de entrenamiento](#6-cola-de-entrenamiento)
7. [Cola de transferibles](#7-cola-de-transferibles)
8. [Análisis de rival (Spy)](#8-análisis-de-rival-spy)
9. [Agentes IA (opcional)](#9-agentes-ia-opcional)
10. [Módulos Python](#10-módulos-python)
11. [Base de datos](#11-base-de-datos)
12. [Flujos de datos](#12-flujos-de-datos)
13. [Iconos de referencia](#13-iconos-de-referencia)

---

## 1. Arquitectura

```
Discord User
     │  slash command
     ▼
discord_bot.py  ──────────────────────────────────────────────────────┐
  │  asyncio.to_thread()                                               │
  ▼                                                                    │
Playwright (Chromium headless)                              PostgreSQL │
  │  login / session cache (18h TTL)                        (ligas,   │
  ▼                                                          tácticas, │
onlinesoccermanager.com                                      fichajes) │
  │  DOM scraping + KO.js viewmodel                                    │
  │  + RPA (click, fill, navigate)                                     │
  ▼                                                                    │
scraper_*.py / action_*.py ◄──────────────────────────────────────────┘

                    [Futuro]
discord_bot.py → agent_*.py → LLM (Ollama local / Anthropic API)
```

**Principios:**
- Cada comando con navegador corre en `asyncio.to_thread` (no bloquea Discord).
- La sesión de Playwright se cachea en BD hasta 18h para evitar login repetido.
- Las acciones batch reusan la misma sesión (1 Chromium para N equipos).
- KO.js viewmodel se prefiere sobre DOM scraping (más fiable).

---

## 2. Configuración `.env`

Copia `.env.example` a `.env` y completa los valores.

### Credenciales y conexiones

| Variable | Obligatoria | Descripción |
|---|---|---|
| `MI_USUARIO` | ✅ | Username OSM |
| `MI_CONTRASENA` | ✅ | Contraseña OSM |
| `DB_HOST` | ✅ | Host PostgreSQL |
| `DB_PORT` | ✅ | Puerto (normalmente `5432`) |
| `DB_NAME` | ✅ | Base de datos |
| `DB_USER` | ✅ | Usuario PostgreSQL |
| `DB_PASSWORD` | ✅ | Contraseña PostgreSQL |

### Discord

| Variable | Obligatoria | Descripción |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Token del bot (Discord Developer Portal) |
| `DISCORD_GUILD_ID` | ⬜ | ID del servidor. Si se define, comandos instantáneos. Vacío = global (~1h). |
| `DISCORD_OWNER_ID` | ⬜ | Tu ID de usuario Discord. Vacío = cualquiera puede usar el bot. |
| `OSM_USER_ID` | ✅ | UUID del usuario OSM en tabla `users` |
| `DISCORD_ALERT_CHANNEL_ID` | ⬜ | Canal para alertas automáticas. Vacío = sin alertas. |

### Timers y automatización

| Variable | Default | Descripción |
|---|---|---|
| `TIMER_WARNING_MINUTES` | `30` | Minutos antes de que expire un timer para avisar |
| `TIMER_CHECK_MINUTES` | `20` | Frecuencia del loop de alertas en minutos |
| `EVENT_DELAY_HOURS` | `2` | Horas de margen antes de un evento bonus para esperar antes de automatizar |

### Agentes IA (opcional)

| Variable | Default | Descripción |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `"ollama"` (local) / `"anthropic"` (nube) / `"auto"` (ollama primero, anthropic fallback) |
| `OLLAMA_HOST` | `http://localhost:11434` | URL del servidor Ollama |
| `OLLAMA_MODEL` | `phi3.5` | Modelo local (`phi3.5` ~2.3GB, `llama3.2:3b` ~2GB) |
| `ANTHROPIC_API_KEY` | `` | API key de Anthropic (fallback) |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Modelo Claude a usar |
| `ENABLE_AGENT_LOOP` | `false` | `true` para activar el loop diario autónomo de agente de transferibles |

---

## 3. Puesta en marcha

```bash
# Instalar dependencias
uv sync

# Instalar Chromium para Playwright
playwright install chromium

# Configurar variables de entorno
cp .env.example .env
# editar .env con los valores reales

# Iniciar el bot
uv run discord_bot.py
```

Al iniciar, el bot:
1. Carga `training_queue.json` y `transfer_queue.json` (si existen).
2. Conecta a Discord y sincroniza los slash commands.
3. Si `DISCORD_ALERT_CHANNEL_ID` está configurado: inicia loops de alertas, transferibles (cada 2h) y opcionalmente el agente IA (si `ENABLE_AGENT_LOOP=true`).

---

## 4. Comandos slash

Todos los comandos requieren ser el `DISCORD_OWNER_ID` si esa variable está configurada.
Los comandos con navegador tardan **~30-45 segundos**.

---

### /panel
Panel principal con el estado de todos tus equipos. Muestra última actualización, próxima jornada y selector para ver detalles de cada slot.

### /timers
Lee los timers activos en tiempo real desde OSM (~30s por equipo). Si la temporada está terminada, muestra un aviso y omite los timers.

| Timer | Icono | Descripción |
|---|---|---|
| Entrenamiento | 💪 | Tiempo hasta que termina la sesión |
| Próximo partido | ⚽ | Countdown al siguiente match |
| Ojeador | 🔍 | Timer del scout (incluye el spy del rival) |
| Médico | ⚕️ | Recuperación de lesionados |
| Abogado | ⚖️ | Timer del abogado |
| Estadio | 🏟️ | Construcción/ampliación en curso |
| Predicción | 🎯 | Predicción del partido |
| Recompensa diaria | 🎁 | Login reward countdown |
| Evento | 🌍 | Eventos globales activos |

### /squad
Lee la plantilla completa de un equipo en tiempo real (~30s). Jugadores agrupados por sección con stats, fitness, morale y estado.

**Iconos:** `🔵` titular · `📋` suplente · `🏃` entrenando · `🏥` lesionado · `🚫` suspendido · `⚡` en forma · `⭐` world star · `🟡` tarjeta amarilla

### /tactics
Muestra las tácticas actuales del equipo (datos de BD, no en tiempo real).

### /standings
Clasificación actual de la liga.

### /fichajes
Últimos 8 fichajes registrados en la liga.

### /events
Calendario de eventos OSM del mes (fetch del foro oficial, caché 6h).

---

### /settactics
Cambia las tácticas del equipo vía RPA (~30s). Muestra confirmación antes de aplicar.

**Parámetros:** `slot`, `gameplan`, `tackling`, `pressure` (0-100), `mentality` (0-100), `tempo` (0-100), `marking`, `fwd`, `mid`, `defenders`, `offside`

```
/settactics slot:La Liga gameplan:Counter-attack pressure:70 offside:Yes
```

### /setlineup
Cambia la formación y auto-rellena los mejores jugadores (~30s). Muestra confirmación.

**Formaciones disponibles (24):** 4-3-3 A/B, 4-5-1, 4-2-3-1, 4-4-2 A/B, 3-5-2, 3-4-3, 5-3-2, 5-4-1, 6-3-1, y más.

### /renewtraining
Reclama entrenamientos terminados e inicia nuevos (~30s). Si hay jugadores configurados en la [cola de entrenamiento](#6-cola-de-entrenamiento), los usa; si no, reutiliza el último jugador.

### /queuetraining
Programa qué jugadores entrenarán en la **próxima** sesión por cada tipo de coach (~30s, scrapa la plantilla).

| Coach | Posición | Stat principal |
|---|---|---|
| Attacking Coach | Delanteros (A) | ATT |
| Defending Coach | Defensas (D) | DEF |
| Midfielder Coach | Mediocampos (M) | OVR |
| Goalkeeping Coach | Porteros (G) | DEF |

La programación es **persistente** (guardada en `training_queue.json`) hasta que se cambie manualmente.

### /upgradestadium
Amplía el estadio (~45s). Reclama construcciones terminadas, gestiona el saldo CF/Savings automáticamente.

**Partes:** `auto` (mejor disponible) · `capacity` (entradas) · `pitch` (campo) · `training` (entrenamiento)

---

### /rival
Análisis del próximo rival. Combina datos inmediatos con spy opcional:

**Lo que siempre muestra (sin spy):**
- Próximo rival + manager + posición en la clasificación
- Últimos N partidos del rival (scraped de `/League/Results`, disponible inmediatamente)
- Estado del spy: si lo inicia automáticamente o si está en curso

**Cómo obtiene los últimos partidos:**
Navega a `/League/Results` del slot activo y extrae todas las filas de la tabla de resultados, filtrando las que contienen el nombre del rival. No depende del spy.

```
/rival slot:La Liga
→ Muestra: posición en tabla + últimos 5 partidos + estado del spy
```

### /spy
Gestiona el espionaje del próximo rival en `/DataAnalist` (~30s). Flujo inteligente en un solo comando:

| Estado | Comportamiento |
|---|---|
| Sin spy activo | Inicia el spy automáticamente |
| Spy en curso (timer ~1h) | Informa que hay que esperar |
| Spy terminado | Muestra tácticas + plantilla + últimos partidos |

**Nota sobre los datos del spy:** Los datos de tácticas y plantilla del rival se leen del KO.js viewmodel de OSM (`spyInstructionPartial()`). Los últimos partidos se obtienen de la página de resultados de la liga (no del spy). Los nombres exactos de los observables KO del spy requieren verificación con el DOM real post-spy.

---

### /settransferqueue
Configura los candidatos a venta automática (~30s, scrapa plantilla). Un único Select muestra todos los jugadores ordenados por stat ascendente (los más débiles primero). Se pueden elegir hasta 6 candidatos sin restricción de posición.

Guardado en `transfer_queue.json`. El bot los usará automáticamente cuando haya slots vacíos en la lista de transferibles.

### /filltransferlist
Rellena manualmente la lista de transferibles con los candidatos configurados (~45s).

### /agentransfer
**[Requiere Ollama/Anthropic]** Ejecuta el agente IA para analizar la plantilla e historial de ventas y decidir qué jugadores poner como candidatos (~45s). Actualiza `transfer_queue.json` automáticamente.

### /agenttactics
**[Requiere Ollama/Anthropic]** Recomienda tácticas contra un rival específico basándose en la clasificación, stats de plantilla y contexto del partido. Botón para aplicar directamente.

```
/agenttactics slot:La Liga opponent:IK Oddevold
```

---

## 5. Automatización en background

Requiere `DISCORD_ALERT_CHANNEL_ID` configurado.

### Loop de timers (cada `TIMER_CHECK_MINUTES` minutos, default 20)

Por cada slot activo:
- **Timer listo (✅):** Avisa en Discord. Ejecuta automatización si corresponde.
- **Timer bajo el umbral:** Aviso previo (una vez por ciclo).
- **Timer reiniciado:** Limpia el estado para avisar de nuevo.

### Auto-renovación de entrenamientos

Cuando timer de entrenamiento queda listo:
1. Si temporada terminada → omite y avisa
2. Si evento de entrenamiento próximo (< `EVENT_DELAY_HOURS`) → espera y avisa
3. Si todo OK → renueva en batch (1 sesión Playwright para N equipos)
4. Usa el jugador de la cola de entrenamiento si está configurado, si no el último que entrenó

### Auto-upgrade de estadio

Cuando timer de estadio queda listo, o cuando no hay ningún timer de estadio activo:
1. Si evento de estadio próximo → espera
2. Si todo OK → amplía en batch

### Loop de transferibles (cada 2 horas)

Por cada liga con candidatos configurados en `transfer_queue.json`:
- Lee el estado de la lista via KO (`maxPlayersOnTransferlist()`, `availableSellPlayerSlotsAmount()`)
- Si hay slots vacíos → agrega candidatos automáticamente
- El máximo es dinámico: 4 normal, 6 durante Transfer Madness (lo lee OSM via KO)

### Loop del agente IA (cada 24 horas, solo si `ENABLE_AGENT_LOOP=true`)

Por cada liga activa:
- Scrapa la plantilla actual
- Lee historial de ventas de la BD
- Envía al LLM para decidir candidatos óptimos
- Actualiza `transfer_queue.json` y notifica en Discord

---

## 6. Cola de entrenamiento

**Archivo:** `training_queue.json`

**Formato:**
```json
{
  "La Liga": {
    "Midfielder Coach": "Pedri",
    "Attacking Coach": "Lewandowski"
  }
}
```

**Flujo:**
1. `/queuetraining` → usuario elige jugador por tipo de coach
2. Se guarda en `training_queue.json`
3. En cada renovación (auto o `/renewtraining`): `queued_player = queue.get(coach_title) OR last_trained_player`
4. Persistente hasta que se cambie manualmente

---

## 7. Cola de transferibles

**Archivo:** `transfer_queue.json`

**Formato:**
```json
{
  "La Liga": ["Ramírez", "Anasmo", "W. Nilsson", "Borstam"]
}
```

**Flujo:**
1. `/settransferqueue` → usuario elige hasta 6 candidatos de toda la plantilla (sin restricción de posición)
2. Se guarda en `transfer_queue.json`
3. Loop cada 2h: si hay slots vacíos → agrega candidatos en orden
4. Si el jugador ya está en lista → lo omite y prueba el siguiente
5. Si el jugador no aparece en el modal (no encontrado) → lo omite y prueba el siguiente
6. El máximo de slots lo lee OSM dinámicamente (4 o 6 en Transfer Madness)

---

## 8. Análisis de rival (Spy)

### Fuente 1: Últimos partidos (inmediato, sin spy)

Navega a `/League/Results` y extrae la tabla de resultados de todas las jornadas. Filtra las filas donde aparece el nombre del rival (local o visitante). Devuelve los últimos 5 partidos con score, resultado y venue.

**Limitación:** Si el rival tiene un nombre con caracteres especiales o el nombre en la tabla no coincide exactamente, el filtro puede fallar. Se usa búsqueda parcial (incluye/es incluido por).

### Fuente 2: Spy de OSM (tácticas + plantilla, requiere 1h)

1. Navega a `/DataAnalist`
2. Lee el `nextOpponentTeamPartial()` via KO
3. Llama a `root.spyTeam(teamData)` para iniciar el spy
4. Confirma en el modal via `vm.okAction()`
5. El spy tarda 1 hora (se ve como timer de tipo `scout` en `/timers`)
6. Cuando termina: lee `spyInstructionPartial()` via KO para obtener tácticas y plantilla

**Estado de los observables KO post-spy:** Los nombres exactos de `recentResults`, `tacticsPartial`, `playersGroupablePartial` dentro de `spyInstructionPartial` son especulativos hasta inspeccionar el DOM real post-spy. Si los datos del spy salen vacíos, es necesario inspeccionar el KO viewmodel después de que el timer complete.

### Flujo del comando `/rival`

```
/rival slot:La Liga
  │
  ├─→ _get_standings_for_league() [BD, sin navegador] → posición en tabla
  │
  └─→ _scrape_spy_sync() [Playwright ~30s]
          │
          ├─→ get_data_analyst_state() → lee nextOpponent via KO
          ├─→ get_opponent_recent_matches() → scrapes /League/Results
          │
          ├─ spy terminado? → get_spy_results() → tácticas + plantilla
          ├─ spy en curso? → informa al usuario
          └─ sin spy? → inicia spy automáticamente
```

---

## 9. Agentes IA (opcional)

Los agentes requieren un LLM disponible. Por defecto están **desactivados** (`ENABLE_AGENT_LOOP=false`).

### Configuración en Orange Pi 5

```bash
# Instalar Ollama (soporta ARM64)
curl -fsSL https://ollama.ai/install.sh | sh

# Descargar modelo (~2.3GB, deja ~5.5GB libres para Playwright + bot)
ollama pull phi3.5

# Activar en .env
ENABLE_AGENT_LOOP=true
LLM_PROVIDER=ollama
```

### `llm_client.py` — Abstracción LLM

- `call_llm(prompt, system, temperature)` — Ollama primero, Anthropic fallback
- `call_llm_json()` — igual pero parsea respuesta como JSON
- `ollama_available()` — verifica si Ollama está corriendo
- Configurado via env vars: `LLM_PROVIDER`, `OLLAMA_HOST`, `OLLAMA_MODEL`

### Agente de transferibles (`agent_transfer.py`)

**Entrada:**
- Plantilla actual (scrapeada)
- Historial de ventas de la BD (últimas 30 ventas)
- Candidatos configurados actualmente

**Salida:**
```json
{ "candidates": ["Player1", "Player2", "Player3"], "reasoning": "..." }
```

**Criterios del prompt:**
1. Nunca incluir titulares, jugadores en entrenamiento o lesionados
2. Priorizar jugadores con status UNASSIGNED
3. Priorizar jugadores mayores (>28) con stats bajos
4. Mantener al menos 2 suplentes por posición clave

**Activación:** Manual via `/agentransfer` o automática (si `ENABLE_AGENT_LOOP=true`).

### Agente táctico (`agent_tactics.py`)

**Entrada:**
- Plantilla propia (stats promedio de starters)
- Clasificación actual (standings)
- Nombre del rival
- Tácticas propias actuales

**Salida:** Configuración táctica completa validada contra las opciones disponibles en OSM.

**Activación:** Solo manual via `/agenttactics [slot] [opponent]`.

---

## 10. Módulos Python

| Archivo | Propósito |
|---|---|
| `discord_bot.py` | Bot principal — comandos, loops, storage, embeds |
| `utils.py` | Login, session cache, handle_popups, navegación robusta |
| `scraper_timers.py` | Extrae timers del dropdown `#timers` del dashboard |
| `scraper_squad.py` | Extrae plantilla completa desde `/Squad` via KO.js |
| `scraper_data_analyst.py` | Spy de rival en `/DataAnalist` + últimos partidos |
| `scraper_tactics.py` | Extrae tácticas propias desde `/Tactics` |
| `scraper_events.py` | Calendario de eventos del foro OSM (caché 6h) |
| `scraper_match_results.py` | Resultados de partidos propios |
| `scraper_transfers.py` | Historial de fichajes de la liga |
| `scraper_table.py` | Clasificación de la liga |
| `scraper_values.py` | Valores de los equipos |
| `action_set_tactics.py` | RPA: cambia tácticas en `/Tactics` via KO.js |
| `action_set_lineup.py` | RPA: cambia formación en `/Lineup` + auto-mejora |
| `action_set_training.py` | RPA: reclama y renueva entrenamientos en `/Training` |
| `action_set_stadium.py` | RPA: gestiona ampliaciones de estadio |
| `action_set_transferlist.py` | RPA: añade jugadores a la lista de transferibles |
| `llm_client.py` | Abstracción LLM (Ollama / Anthropic) |
| `agent_transfer.py` | Agente: decide candidatos de venta via LLM |
| `agent_tactics.py` | Agente: recomienda tácticas via LLM |
| `run_update.py` | Batch scraper para actualizar BD (no usa Discord) |

---

## 11. Base de datos

| Tabla | Descripción |
|---|---|
| `users` | Usuarios con credenciales OSM |
| `leagues` | Ligas con standings y teams JSON |
| `user_leagues` | Relación usuario-liga, is_active, last_scraped_at |
| `match_tactics` | Tácticas propias scrapeadas por jornada |
| `transfers` | Historial de fichajes (transaction_type: sale/purchase) |
| `scheduled_scrape_tasks` | Tareas programadas (scrape post-partido) |
| `user_browser_sessions` | Caché de sesión Playwright (TTL 18h) |

---

## 12. Flujos de datos

### Sin navegador (BD)
```
/tactics   → match_tactics  → embed
/standings → user_leagues   → embed
/fichajes  → transfers      → embed
```

### Con navegador (scraping en tiempo real)
```
/timers → /Dashboard → scraper_timers → embed
/squad  → /Squad     → scraper_squad  → embed
/rival  → /DataAnalist + /League/Results → scraper_data_analyst → embed
/spy    → /DataAnalist → spy start/results → embed
```

### RPA (con confirmación Discord)
```
/settactics     → confirm → /Tactics    → action_set_tactics
/setlineup      → confirm → /Lineup     → action_set_lineup
/upgradestadium → confirm → /Stadium    → action_set_stadium
/renewtraining         → /Training  → action_set_training
/filltransferlist      → /TransferList → action_set_transferlist
```

### Loops background
```
_timer_alert_loop (cada 20min)
  → scrape timers → si listo:
      → batch training renewal (action_set_training)
      → batch stadium upgrade (action_set_stadium)

_transferlist_loop (cada 2h)
  → check /TransferList → si slots vacíos:
      → batch fill (action_set_transferlist)

_agent_transfer_loop (cada 24h, si ENABLE_AGENT_LOOP=true)
  → scrape squad → query BD → LLM → update transfer_queue.json
```

---

## 13. Iconos de referencia

### Estado de jugadores
| Icono | Significado |
|---|---|
| 🔵 | Titular en el once |
| 📋 | Suplente |
| 🏃 | En entrenamiento activo |
| 🏥 | Lesionado |
| 🚫 | Suspendido |
| ⚡ | En forma |
| ⭐ | World Star |
| 🟡 | Tarjeta amarilla |

### Timers
| Icono | Tipo |
|---|---|
| 💪 | Entrenamiento |
| ⚽ | Próximo partido |
| 🔍 | Ojeador / Spy rival |
| ⚕️ | Médico |
| ⚖️ | Abogado |
| 🏟️ | Estadio |
| 🎯 | Predicción |
| 🎁 | Recompensa diaria |
| 🌍 | Evento global |

### Resultados (partido rival)
| Icono | Significado |
|---|---|
| ✅ | Victoria del rival |
| ➖ | Empate |
| ❌ | Derrota del rival |
| 🏠 | Local |
| ✈️ | Visitante |
