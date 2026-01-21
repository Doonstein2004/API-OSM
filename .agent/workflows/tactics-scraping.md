---
description: Cómo funciona el sistema de scraping de tácticas
---

# Sistema de Scraping de Tácticas OSM

Este documento explica el flujo completo del sistema de extracción de tácticas para Online Soccer Manager.

## Arquitectura

El sistema consta de los siguientes componentes:

### 1. Scrapers

- **`scraper_tactics.py`**: Extrae las tácticas actuales de la página `/Tactics`:
  - Game Plan (Plan de Juego)
  - Tackling (Agresividad)
  - Sliders: Pressure, Mentality/Style, Tempo
  - Line Tactics: Forwards, Midfielders, Defenders
  - Offside Trap
  - Marking

- **`scraper_next_match.py`**: Extrae información del próximo partido:
  - Matchday (Jornada actual)
  - Countdown (Tiempo restante hasta el partido)
  - Árbitro y su nivel de estrictez
  - Calcula el timestamp para ejecutar el scraping de tácticas

### 2. Base de Datos

**Tabla `match_tactics`**:
```sql
- id: SERIAL PRIMARY KEY
- user_id: UUID
- league_id: INTEGER
- round: INTEGER
- team_name: VARCHAR
- game_plan: VARCHAR (Long ball, Passing game, Wing play, Counter-attack, Shoot on sight)
- tackling: VARCHAR (Careful, Normal, Aggressive, Reckless)
- pressure: INTEGER (0-100)
- mentality: INTEGER (0-100)
- tempo: INTEGER (0-100)
- forwards_tactic: VARCHAR (Drop deep, Support midfield, Attack only)
- midfielders_tactic: VARCHAR (Protect the defence, Stay in position, Push forward)
- defenders_tactic: VARCHAR (Defend deep, Attacking full-backs, Support midfield)
- offside_trap: BOOLEAN
- marking: VARCHAR (Zonal marking, Man marking)
- scraped_at: TIMESTAMP
```

**Tabla `scheduled_scrape_tasks`**:
```sql
- id: SERIAL PRIMARY KEY
- user_id: UUID
- task_type: VARCHAR ('tactics_scrape')
- scheduled_at: TIMESTAMP
- status: VARCHAR ('pending', 'completed', 'failed')
- metadata: JSONB
- created_at: TIMESTAMP
- executed_at: TIMESTAMP
```

### 3. Scripts de Ejecución

- **`run_update_for_user.py`**: Flujo principal de actualización
  - Scrapea datos principales (mercado, clasificación, resultados)
  - Obtiene info del próximo partido
  - Scrapea tácticas actuales
  - Programa tareas futuras de tácticas

- **`run_scheduled_tactics.py`**: Ejecuta tareas programadas
  - Se ejecuta periódicamente (ej: cada 5 minutos)
  - Busca tareas pendientes cuyo `scheduled_at` ya pasó
  - Ejecuta el scraping de tácticas para esas ligas/jornadas

## Flujo de Ejecución

```
1. Usuario ejecuta run_update_for_user.py
   │
   ├── Login en OSM
   │
   ├── Scrape datos principales
   │   ├── Mercado (transfer list, historial)
   │   ├── Clasificación y valores
   │   └── Resultados de partidos
   │
   ├── Obtener info próximo partido (para cada slot)
   │   ├── Jornada actual
   │   └── Tiempo restante (countdown)
   │
   ├── Scrape tácticas ACTUALES
   │   └── Guarda en match_tactics con jornada actual
   │
   └── Programar scrapes FUTUROS
       └── Crea tareas en scheduled_scrape_tasks
           con scheduled_at = partido_start + 5 min

2. Cada 5 minutos: run_scheduled_tactics.py
   │
   ├── Buscar tareas pendientes donde scheduled_at <= NOW
   │
   └── Para cada tarea:
       ├── Login en OSM
       ├── Navegar al slot correspondiente
       ├── Scrape tácticas
       ├── Guardar en match_tactics
       └── Marcar tarea como completada
```

## API Endpoints

### Consultar Tácticas
```
GET /api/leagues/{league_id}/tactics
GET /api/leagues/{league_id}/tactics?round=5
```

### Consultar Tareas Programadas
```
GET /api/scheduled-tasks
GET /api/scheduled-tasks?status=pending
GET /api/scheduled-tasks?task_type=tactics_scrape
```

### Ejecutar Tareas Manualmente
```
POST /run-scheduled-tactics
Headers: X-API-Key: <api_key>
```

### Próximos Partidos de Usuario
```
GET /api/next-matches/{user_id}
Headers: X-API-Key: <api_key>
```

## Ejecución Automática con GitHub Actions

El sistema usa GitHub Actions integrado con Vercel para orquestar el scraping:

### Flujo Completo

```
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions: cron_trigger.yml (cada 3 horas)                    │
│  └── Llama a Vercel: /api/cron/trigger-scrapes                      │
│      └── Dispara scrape_data.yml para cada usuario                  │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions: scrape_data.yml                                    │
│  └── run_update_for_user.py                                         │
│      ├── Scrapea datos principales + tácticas actuales              │
│      └── Programa tareas en scheduled_scrape_tasks                  │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions: tactics_cron_trigger.yml (cada 10 minutos)         │
│  └── Llama a Vercel: /api/cron/trigger-tactics                      │
│      └── Si hay tareas pendientes → Dispara scrape_tactics.yml      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions: scrape_tactics.yml                                 │
│  └── run_scheduled_tactics.py                                       │
│      └── Procesa todas las tareas pendientes de tácticas            │
└─────────────────────────────────────────────────────────────────────┘
```

### Archivos de Configuración

| Archivo | Descripción |
|---------|-------------|
| `.github/workflows/cron_trigger.yml` | Cron cada 3h → dispara scrapes principales |
| `.github/workflows/scrape_data.yml` | Ejecuta scraping completo por usuario |
| `.github/workflows/tactics_cron_trigger.yml` | Cron cada 10min → verifica tareas de tácticas |
| `.github/workflows/scrape_tactics.yml` | Ejecuta tareas de tácticas pendientes |
| `api/cron/trigger-scrapes.js` | API Vercel para disparar scrapes por usuario |
| `api/cron/trigger-tactics.js` | API Vercel para disparar scraper de tácticas |

### Secrets Requeridos en GitHub

- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `GITHUB_PAT` (para disparar workflows)
- `CRON_SECRET` (autenticación de endpoints)
- `DISCORD_WEBHOOK_URL` (notificaciones)
- `FIREBASE_ADMIN_JSON` (para notificaciones push)

### Secrets Requeridos en Vercel

- `POSTGRES_URL_NON_POOLING` (conexión a BD)
- `CRON_SECRET` (autenticación)
- `GITHUB_PAT` (para disparar workflows)

### Nuevo Secret a Añadir

En GitHub Actions, añade el secret:
- `VERCEL_TACTICS_CRON_URL`: URL del endpoint `/api/cron/trigger-tactics`

## Valores de Tácticas

### Game Plan
- Long ball
- Passing game
- Wing play
- Counter-attack
- Shoot on sight

### Tackling
- Careful
- Normal
- Aggressive
- Reckless

### Forwards Tactic
- Drop deep
- Support midfield
- Attack only

### Midfielders Tactic
- Protect the defence
- Stay in position
- Push forward

### Defenders Tactic
- Defend deep
- Attacking full-backs
- Support midfield

### Offside Trap
- Yes / No

### Marking
- Zonal marking
- Man marking
