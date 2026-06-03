# discord_bot.py
"""
Bot de Discord para OSM — Fase 1
Comandos: /panel, /timers, /tactics [slot], /standings [slot]
"""
import os
import sys
import json
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")   # Opcional: sync instantáneo
DISCORD_OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))
OSM_USER_ID      = os.getenv("OSM_USER_ID")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST"),
    "port":     os.getenv("DB_PORT"),
    "dbname":   os.getenv("DB_NAME"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

DISCORD_ALERT_CHANNEL_ID = int(os.getenv("DISCORD_ALERT_CHANNEL_ID", "0"))
TIMER_WARNING_MINUTES    = int(os.getenv("TIMER_WARNING_MINUTES", "30"))
TIMER_CHECK_MINUTES      = int(os.getenv("TIMER_CHECK_MINUTES", "20"))
# Si un evento de entrenamiento/estadio empieza en menos de este tiempo,
# se espera antes de lanzar la automatización (para aprovechar los timers reducidos)
EVENT_DELAY_HOURS        = int(os.getenv("EVENT_DELAY_HOURS", "2"))

OSM_COLOR   = 0x22D3EE   # Cyan del tema OSM
ERROR_COLOR = 0xFF6B6B

# Caché en memoria para el scrape de timers (evita doble scrape entre /timers y notifs)
_last_scrape_time:   Optional[datetime]   = None
_last_scrape_result: list[dict]           = []

# Estado de notificaciones: {"{slot}_{type}": seconds_last_seen}
_timer_state: dict[str, int] = {}
_warned:      set[str]       = set()

# Alternancia de estadio: { league_name: 'training' | 'pitch' } — próxima parte a ampliar
_stadium_next_part: dict[str, str] = {}

# Cola de entrenamiento: { league_name: { coach_title: player_name } }
# Persistida en training_queue.json — el bot la carga al arrancar.
TRAINING_QUEUE_FILE = "training_queue.json"
_training_queue: dict[str, dict[str, str]] = {}

def _load_training_queue():
    global _training_queue
    try:
        if os.path.exists(TRAINING_QUEUE_FILE):
            with open(TRAINING_QUEUE_FILE, encoding="utf-8") as f:
                _training_queue = json.load(f)
            print(f"✅ Cola de entrenamiento cargada ({sum(len(v) for v in _training_queue.values())} entradas)")
    except Exception as e:
        print(f"⚠️ No se pudo cargar {TRAINING_QUEUE_FILE}: {e}")

def _save_training_queue():
    try:
        with open(TRAINING_QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(_training_queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ No se pudo guardar {TRAINING_QUEUE_FILE}: {e}")

def _set_queued_player(league_name: str, coach_title: str, player_name: Optional[str]):
    if league_name not in _training_queue:
        _training_queue[league_name] = {}
    if player_name:
        _training_queue[league_name][coach_title] = player_name
    else:
        _training_queue[league_name].pop(coach_title, None)
    _save_training_queue()

# Cola de transferibles: { league_name: [name1, name2, ...] }
# Lista plana — sin restricción por posición. Persistida en transfer_queue.json.
TRANSFER_QUEUE_FILE = "transfer_queue.json"
_transfer_queue: dict[str, list[str]] = {}

def _load_transfer_queue():
    global _transfer_queue
    try:
        if os.path.exists(TRANSFER_QUEUE_FILE):
            with open(TRANSFER_QUEUE_FILE, encoding="utf-8") as f:
                _transfer_queue = json.load(f)
            total = sum(len(v) for v in _transfer_queue.values())
            print(f"✅ Cola de transferibles cargada ({total} candidatos)")
    except Exception as e:
        print(f"⚠️ No se pudo cargar {TRANSFER_QUEUE_FILE}: {e}")

def _save_transfer_queue():
    try:
        with open(TRANSFER_QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(_transfer_queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ No se pudo guardar {TRANSFER_QUEUE_FILE}: {e}")

def _get_transfer_candidates(league_name: str) -> list[str]:
    return _transfer_queue.get(league_name, [])

def _set_transfer_candidates(league_name: str, names: list[str]):
    if names:
        _transfer_queue[league_name] = names
    else:
        _transfer_queue.pop(league_name, None)
    _save_transfer_queue()

_COACH_TO_POS = {
    "Attacking Coach":   "A",
    "Defending Coach":   "D",
    "Midfielder Coach":  "M",
    "Goalkeeping Coach": "G",
}
_COACH_EMOJI = {
    "Attacking Coach":   "⚡",
    "Defending Coach":   "🛡️",
    "Midfielder Coach":  "🔄",
    "Goalkeeping Coach": "🧤",
}


# ── CLIENTE ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


# ── ACCESO A BD (sync, se ejecuta en thread) ─────────────────────────────────

def _db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor_factory = psycopg2.extras.DictCursor
    return conn


def _get_active_leagues(user_id: str) -> list[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    l.id            AS league_id,
                    l.name          AS league_name,
                    ul.standings,
                    ul.managers_by_team,
                    ul.last_scraped_at
                FROM user_leagues ul
                JOIN leagues l ON l.id = ul.league_id
                WHERE ul.user_id = %s AND ul.is_active = TRUE
                ORDER BY l.name;
            """, (user_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _get_all_active_slots(user_id: str) -> list[dict]:
    """Combina ligas de la BD con slots activos del timer cache (para ligas nuevas no sincronizadas)."""
    try:
        leagues = _get_active_leagues(user_id)
    except Exception:
        leagues = []
    db_names = {lg["league_name"].lower() for lg in leagues}
    for slot in _last_scrape_result:
        ln = slot.get("league_name", "")
        if ln and ln.lower() not in db_names:
            leagues.append({
                "league_id":       None,
                "league_name":     ln,
                "standings":       None,
                "managers_by_team": None,
                "last_scraped_at": None,
            })
            db_names.add(ln.lower())
    return leagues


def _get_latest_tactics(league_id: int) -> Optional[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM match_tactics
                WHERE league_id = %s
                ORDER BY scraped_at DESC
                LIMIT 1;
            """, (league_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _get_next_match_task(user_id: str, league_id: int) -> Optional[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT scheduled_at, metadata
                FROM scheduled_scrape_tasks
                WHERE user_id = %s
                  AND task_type = 'tactics_scrape'
                  AND status    = 'pending'
                  AND (metadata->>'league_id')::int = %s
                ORDER BY scheduled_at
                LIMIT 1;
            """, (user_id, league_id))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _get_referee_for_league(user_id: str, league_id: int) -> dict:
    """Lee árbitro + jornada del próximo partido desde el metadata de la tarea programada."""
    task = _get_next_match_task(user_id, league_id)
    if not task:
        return {}
    meta = task.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return {}
    return {
        "referee_name":       meta.get("referee_name") or meta.get("referee"),
        "referee_strictness": meta.get("referee_strictness") or meta.get("strictness"),
        "matchday":           meta.get("matchday"),
    }


def _get_recent_sales(league_id: int, limit: int = 30) -> list[dict]:
    """Historial de ventas para alimentar al agente de transferibles."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT player_name, manager_name, transaction_type,
                       position, round, final_price, created_at
                FROM transfers
                WHERE league_id = %s AND transaction_type = 'sale'
                ORDER BY created_at DESC
                LIMIT %s;
            """, (league_id, limit))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _get_standings_for_league(league_id: int) -> list[dict]:
    """Clasificación actual de la liga (para el agente táctico)."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT standings FROM user_leagues
                WHERE league_id = %s
                ORDER BY last_scraped_at DESC NULLS LAST
                LIMIT 1;
            """, (league_id,))
            row = cur.fetchone()
            if not row:
                return []
            standings = row["standings"]
            if isinstance(standings, str):
                try:
                    return json.loads(standings)
                except Exception:
                    return []
            return standings or []
    finally:
        conn.close()


def _get_my_team_name(league: dict) -> str:
    """Devuelve el nombre del equipo del usuario en esta liga buscando en managers_by_team."""
    username = os.getenv("MI_USUARIO", "").lower().strip()
    if not username:
        return ""
    mgrs = league.get("managers_by_team") or {}
    if isinstance(mgrs, str):
        try:
            mgrs = json.loads(mgrs)
        except Exception:
            mgrs = {}
    if not isinstance(mgrs, dict):
        return ""
    for team_name, manager_name in mgrs.items():
        if (manager_name or "").lower().strip() == username:
            return team_name
    return ""


def _get_recent_matches_db(league_id: int, opponent_name: str, limit: int = 5) -> list[dict]:
    """Últimos partidos del rival desde la tabla matches."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            pattern = f"%{opponent_name}%"
            cur.execute("""
                SELECT round, home_team, home_goals, away_team, away_goals, played_at
                FROM matches
                WHERE league_id = %s
                  AND (lower(home_team) LIKE lower(%s)
                       OR lower(away_team) LIKE lower(%s))
                ORDER BY round DESC
                LIMIT %s;
            """, (league_id, pattern, pattern, limit))
            rows = cur.fetchall()
            results = []
            for r in rows:
                is_home   = opponent_name.lower() in (r["home_team"] or "").lower()
                my_goals  = r["home_goals"] if is_home else r["away_goals"]
                opp_goals = r["away_goals"] if is_home else r["home_goals"]
                result    = "W" if my_goals > opp_goals else ("L" if my_goals < opp_goals else "D")
                results.append({
                    "round":               r["round"],
                    "home_team":           r["home_team"],
                    "away_team":           r["away_team"],
                    "score":               f"{r['home_goals']}-{r['away_goals']}",
                    "is_home":             is_home,
                    "result_for_opponent": result,
                    "my_goals":            my_goals,
                    "opp_goals":           opp_goals,
                })
            return results
    except Exception as e:
        print(f"⚠️ _get_recent_matches_db: {e}")
        return []
    finally:
        conn.close()


def _get_recent_transfers(league_id: int, limit: int = 8) -> list[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT player_name, manager_name, transaction_type,
                       position, round, final_price, created_at
                FROM transfers
                WHERE league_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
            """, (league_id, limit))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _get_osm_credentials(user_id: str) -> tuple[Optional[str], Optional[str]]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT osm_username, osm_password FROM public.get_credentials_for_user(%s);",
                (user_id,)
            )
            row = cur.fetchone()
            if not row:
                return None, None
            return row["osm_username"], row["osm_password"]
    finally:
        conn.close()


# ── SCRAPERS EN VIVO (sync → se lanza en thread) ─────────────────────────────

def _scrape_timers_sync(user_id: str) -> list[dict]:
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from scraper_timers import get_timers_all_slots

    username, password = _get_osm_credentials(user_id)
    if not username:
        print("❌ Sin credenciales para scrape de timers.")
        return []

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = get_timers_all_slots(page)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de timers: {e}")
        return []
    finally:
        conn.close()


def _scrape_settactics_sync(user_id: str, league_name: str, kwargs: dict) -> dict:
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_tactics import set_tactics_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"success": False, "changed": [], "errors": ["no_credentials"]}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = set_tactics_for_slot(page, league_name, **kwargs)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de tácticas: {e}")
        return {"success": False, "changed": [], "errors": [str(e)]}
    finally:
        conn.close()


def _scrape_setlineup_sync(user_id: str, league_name: str, formation: str) -> dict:
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_lineup import set_lineup_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"success": False, "formation": formation, "errors": ["no_credentials"]}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = set_lineup_for_slot(page, league_name, formation)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de lineup: {e}")
        return {"success": False, "formation": formation, "errors": [str(e)]}
    finally:
        conn.close()


def _scrape_renewtraining_sync(user_id: str, league_name: str) -> dict:
    """Renueva entrenamiento para un solo slot (usado por /renewtraining manual)."""
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_training import renew_training_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"claimed": [], "started": [], "errors": ["no_credentials"]}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            queued = _training_queue.get(league_name) or None
            result = renew_training_for_slot(page, league_name, queued_players=queued)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de training: {e}")
        return {"claimed": [], "started": [], "errors": [str(e)]}
    finally:
        conn.close()


def _scrape_renewtraining_batch_sync(user_id: str, renewals: list[tuple[str, str]]) -> dict[str, dict]:
    """
    Renueva entrenamientos para múltiples slots en UNA sola sesión de Playwright.
    renewals: lista de (team_name, league_name)
    Devuelve dict { team_name: result_dict }
    """
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_training import renew_training_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {team: {"claimed": [], "started": [], "errors": ["no_credentials"]}
                for team, _ in renewals}

    conn = _db()
    results = {}
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            for team, league_name in renewals:
                print(f"  [batch training] Renovando: {team} ({league_name})")
                try:
                    queued = _training_queue.get(league_name) or None
                    results[team] = renew_training_for_slot(page, league_name, queued_players=queued)
                except Exception as e:
                    print(f"  ❌ Error renovando {team}: {e}")
                    results[team] = {"claimed": [], "started": [], "errors": [str(e)]}
            context.close()
            browser.close()
    except Exception as e:
        print(f"❌ Error en batch training: {e}")
        for team, _ in renewals:
            if team not in results:
                results[team] = {"claimed": [], "started": [], "errors": [str(e)]}
    finally:
        conn.close()
    return results


def _scrape_upgradestadium_sync(user_id: str, league_name: str,
                                 preferred_parts: list[str] | None = None) -> dict:
    """Upgrade de estadio para un slot (usado por /upgradestadium manual)."""
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_stadium import upgrade_stadium_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"claimed": [], "started": [], "skipped": [], "errors": ["no_credentials"],
                "cf": 0.0, "savings": 0.0}
    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = upgrade_stadium_for_slot(page, league_name, preferred_parts)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en upgrade estadio: {e}")
        return {"claimed": [], "started": [], "skipped": [], "errors": [str(e)],
                "cf": 0.0, "savings": 0.0}
    finally:
        conn.close()


def _scrape_upgradestadium_batch_sync(user_id: str,
                                       renewals: list[tuple[str, str, list | None]]) -> dict[str, dict]:
    """
    Upgrade de estadio para múltiples slots en UNA sola sesión.
    renewals: lista de (team_name, league_name, preferred_parts_or_None)
    """
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_stadium import upgrade_stadium_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {t: {"claimed": [], "started": [], "skipped": [], "errors": ["no_credentials"],
                    "cf": 0.0, "savings": 0.0} for t, _, _ in renewals}
    conn = _db()
    results = {}
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            for team, league_name, preferred in renewals:
                print(f"  [batch stadium] {team} ({league_name})")
                try:
                    results[team] = upgrade_stadium_for_slot(page, league_name, preferred)
                except Exception as e:
                    print(f"  ❌ Error stadium {team}: {e}")
                    results[team] = {"claimed": [], "started": [], "skipped": [],
                                     "errors": [str(e)], "cf": 0.0, "savings": 0.0}
            context.close()
            browser.close()
    except Exception as e:
        print(f"❌ Error en batch stadium: {e}")
        for team, _, _ in renewals:
            if team not in results:
                results[team] = {"claimed": [], "started": [], "skipped": [],
                                 "errors": [str(e)], "cf": 0.0, "savings": 0.0}
    finally:
        conn.close()
    return results


def _scrape_filltransferlist_sync(user_id: str, league_name: str) -> dict:
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_transferlist import fill_transferlist_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"max_slots": 4, "filled_before": 0, "added": [], "skipped": [],
                "errors": ["no_credentials"]}

    candidates = _get_transfer_candidates(league_name)
    if not candidates:
        return {"max_slots": 4, "filled_before": 0, "added": [], "skipped": [],
                "errors": ["no_candidates_configured"]}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = fill_transferlist_for_slot(page, league_name, candidates)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en fill transferlist: {e}")
        return {"max_slots": 4, "filled_before": 0, "added": [], "skipped": [],
                "errors": [str(e)]}
    finally:
        conn.close()


def _scrape_filltransferlist_batch_sync(
    user_id: str, renewals: list[tuple[str, str]]
) -> dict[str, dict]:
    """Rellena la lista de transferibles de múltiples slots en una sola sesión."""
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from action_set_transferlist import fill_transferlist_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {team: {"added": [], "errors": ["no_credentials"]} for team, _ in renewals}

    conn = _db()
    results = {}
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            for team, league_name in renewals:
                candidates = _get_transfer_candidates(league_name)
                if not candidates:
                    results[team] = {"added": [], "errors": ["no_candidates"]}
                    continue
                try:
                    results[team] = fill_transferlist_for_slot(page, league_name, candidates)
                except Exception as e:
                    print(f"  ❌ Error fill transfer {team}: {e}")
                    results[team] = {"added": [], "errors": [str(e)]}
            context.close()
            browser.close()
    except Exception as e:
        print(f"❌ Error en batch fill transfer: {e}")
        for team, _ in renewals:
            if team not in results:
                results[team] = {"added": [], "errors": [str(e)]}
    finally:
        conn.close()
    return results


def _scrape_squad_sync(user_id: str, league_name: str) -> dict:
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from scraper_squad import get_squad_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"players": [], "team_name": "", "league_name": league_name,
                "matchday": None, "error": "no_credentials"}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = get_squad_for_slot(page, league_name)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de plantilla: {e}")
        return {"players": [], "team_name": "", "league_name": league_name,
                "matchday": None, "error": str(e)}
    finally:
        conn.close()


def _scrape_spy_sync(user_id: str, league_name: str) -> dict:
    """Activa el slot y ejecuta spy_for_slot (inicia spy o lee resultados)."""
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from scraper_data_analyst import spy_for_slot

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"action": "error", "team_name": None, "error": "no_credentials"}

    conn = _db()
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            result = spy_for_slot(page, league_name)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en spy: {e}")
        return {"action": "error", "team_name": None, "error": str(e)}
    finally:
        conn.close()


def _run_agent_transfer_sync(
    user_id: str, league_name: str, league_id: int
) -> dict:
    """
    Scrapa la plantilla actual, consulta el historial de ventas en BD y ejecuta
    el agente LLM para decidir qué jugadores poner como candidatos de venta.
    Actualiza _transfer_queue y lo persiste en transfer_queue.json.
    Returns: { candidates, reasoning, error? }
    """
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from scraper_squad import get_squad_for_slot
    from agent_transfer import analyze_squad_for_transfers

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"candidates": [], "reasoning": "", "error": "no_credentials"}

    # Obtener datos de BD (no necesitan navegador)
    recent_sales = _get_recent_sales(league_id)
    current_candidates = _get_transfer_candidates(league_name)

    # Scraping de plantilla actual
    conn = _db()
    squad = []
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            slot_data = get_squad_for_slot(page, league_name)
            squad = slot_data.get("players", [])
            context.close()
            browser.close()
    except Exception as e:
        conn.close()
        return {"candidates": [], "reasoning": "", "error": f"scrape_failed:{e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not squad:
        return {"candidates": [], "reasoning": "", "error": "empty_squad"}

    # Ejecutar agente LLM
    try:
        result = analyze_squad_for_transfers(
            team_name=league_name,
            squad=squad,
            recent_sales=recent_sales,
            current_listed=[],
            current_candidates=current_candidates,
        )
    except Exception as e:
        return {"candidates": [], "reasoning": "", "error": f"llm_failed:{e}"}

    # Persistir decisión del agente
    candidates = result.get("candidates", [])
    if candidates:
        _set_transfer_candidates(league_name, candidates)

    return result


def _run_agent_tactics_sync(
    user_id: str, league_name: str, league_id: int, opponent_name: str
) -> dict:
    """
    Obtiene datos de la BD y del scraper para ejecutar el agente táctico.
    Returns: { formation, game_plan, ... , reasoning, error? }
    """
    from playwright.sync_api import sync_playwright
    from utils import login_with_session_cache, launch_playwright_browser
    from scraper_squad import get_squad_for_slot
    from agent_tactics import analyze_tactics

    username, password = _get_osm_credentials(user_id)
    if not username:
        return {"error": "no_credentials", "reasoning": ""}

    standings       = _get_standings_for_league(league_id)
    current_tactics = _get_latest_tactics(league_id) or {}

    conn = _db()
    squad = []
    try:
        with sync_playwright() as p:
            browser = launch_playwright_browser(p, headless=True)
            context, page = login_with_session_cache(browser, conn, user_id, username, password)
            slot_data = get_squad_for_slot(page, league_name)
            squad = slot_data.get("players", [])
            context.close()
            browser.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return {"error": f"scrape_failed:{e}", "reasoning": ""}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not squad:
        return {"error": "empty_squad", "reasoning": ""}

    try:
        return analyze_tactics(
            my_team=league_name,
            my_squad=squad,
            my_current_tactics=current_tactics,
            standings=standings,
            opponent_name=opponent_name,
        )
    except Exception as e:
        return {"error": f"llm_failed:{e}", "reasoning": ""}


async def _get_timers_cached() -> list[dict]:
    """Devuelve los timers scrapeados, reutilizando resultado si tiene menos de TIMER_CHECK_MINUTES."""
    global _last_scrape_time, _last_scrape_result
    now = _utcnow()
    max_age = TIMER_CHECK_MINUTES * 60 * 0.9
    if _last_scrape_time and (now - _last_scrape_time).total_seconds() < max_age:
        return _last_scrape_result
    result = await asyncio.to_thread(_scrape_timers_sync, OSM_USER_ID)
    _last_scrape_time   = now
    _last_scrape_result = result
    return result


# ── HELPERS DE FORMATO ────────────────────────────────────────────────────────

def _fmt_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "Iniciado"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    return " ".join(parts) or "< 1m"


def _has_upcoming_bonus_event(slot_events: list[dict], event_type: str) -> tuple[bool, str]:
    """
    Devuelve (True, event_title) si hay un evento de tipo training/stadium que
    AÚN NO empezó pero empieza en menos de EVENT_DELAY_HOURS horas.
    Si el evento ya está activo, devuelve False: hay que renovar AHORA con
    los timers reducidos, no esperar.
    """
    from scraper_events import get_upcoming_bonus_events

    # ── 1. Foro OSM (más fiable: tiene nombre, tipo y fechas exactas) ─────────
    # Solo bloquear si el evento todavía no empezó (is_active=False).
    # Si ya está activo los timers ya son reducidos → renovar ahora.
    forum_events = [ev for ev in get_upcoming_bonus_events(event_type, within_hours=EVENT_DELAY_HOURS)
                    if not ev.get("is_active")]
    if forum_events:
        return True, forum_events[0]["name"]

    # ── 2. KO en vivo del dashboard (fallback si el foro no está disponible) ──
    threshold = EVENT_DELAY_HOURS * 3600
    kws_training = ["training", "entrenamiento", "coach", "progression", "skill"]
    kws_stadium  = ["stadium", "estadio", "expansion", "build", "construction",
                    "capacity", "infraestructura"]
    kws = kws_training if event_type == "training" else kws_stadium
    for ev in slot_events:
        title = ev.get("title", "").lower()
        secs  = ev.get("seconds", 0)
        if any(k in title for k in kws) and 0 < secs <= threshold:
            return True, ev.get("title", "")

    return False, ""


def _get_stadium_preferred(league_name: str) -> list[str]:
    """Devuelve orden de prioridad training↔pitch alternado. Capacity nunca se inicia automáticamente."""
    first  = _stadium_next_part.get(league_name, "training")
    second = "pitch" if first == "training" else "training"
    return [first, second]


def _time_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return "nunca"
    # Normalizar a aware-UTC independientemente de si psycopg2 devuelve naive o aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = _utcnow() - dt
    s = int(diff.total_seconds())
    if s < 3600:  return f"hace {s // 60}m"
    if s < 86400: return f"hace {s // 3600}h"
    return f"hace {diff.days}d"


def _parse_json_field(field) -> list:
    if isinstance(field, list):
        return field
    if isinstance(field, str):
        try:
            return json.loads(field)
        except Exception:
            return []
    return field or []


# ── EMBEDS ────────────────────────────────────────────────────────────────────

def embed_panel(leagues: list[dict], user_id: str) -> discord.Embed:
    embed = discord.Embed(title="⚽  OSM Panel", color=OSM_COLOR, timestamp=_utcnow())

    if not leagues:
        embed.description = "No hay ligas activas. Ejecuta un scrape primero (`run_update_for_user.py`)."
        return embed

    for i, lg in enumerate(leagues, 1):
        scraped = _time_ago(lg.get("last_scraped_at"))
        next_line = ""

        task = _get_next_match_task(user_id, lg["league_id"])
        if task:
            sched_at = task["scheduled_at"]
            if sched_at.tzinfo is None:
                sched_at = sched_at.replace(tzinfo=timezone.utc)
            remaining = (sched_at - _utcnow()).total_seconds()
            # scheduled_at = partido + 5 min de buffer, restamos ese buffer
            partido_en = max(0, int(remaining) - 300)
            meta = task.get("metadata") or {}
            jornada = meta.get("matchday", "?")
            next_line = f"\n📅 Jornada **{jornada}** · Simula en **{_fmt_seconds(partido_en)}**"

        embed.add_field(
            name=f"Slot {i} — {lg['league_name']}",
            value=f"🕒 Datos actualizados {scraped}{next_line}",
            inline=False,
        )

    embed.set_footer(text="Selecciona un slot para más opciones")
    return embed


def embed_timers(slot: dict) -> discord.Embed:
    team     = slot.get("team_name", "Equipo")
    league   = slot.get("league_name", "")
    timers   = slot.get("timers", [])
    matchday = slot.get("matchday")

    md_str = ""
    if matchday:
        cur, tot = matchday["current"], matchday["total"]
        md_str = f"  ·  📅 Jornada **{cur}/{tot}**" + (" ✅" if matchday["finished"] else "")

    embed = discord.Embed(
        title=f"⏱  Timers — {team}",
        description=f"Liga: **{league}**{md_str}",
        color=OSM_COLOR,
        timestamp=_utcnow(),
    )

    if not timers:
        embed.add_field(name="Sin datos", value="No se pudieron leer los timers del dashboard.", inline=False)
        return embed

    for t in timers:
        typ      = t.get("type", "unknown")
        seconds  = t.get("seconds", 0)
        is_ready = t.get("is_ready", False)
        countdown = t.get("countdown", "")

        # Timers sin clasificar y sin countdown no aportan info útil → omitir
        if typ == "unknown" and seconds == 0 and not is_ready:
            continue

        emoji = t.get("emoji", "⏱️")
        # Para eventos: usar el título real en lugar de "Evento" genérico
        if typ == "event" and t.get("event_title"):
            label = t["event_title"]
        else:
            label = t.get("label_es") or t.get("label") or (
                "Otros timers" if typ == "unknown" else "Timer"
            )

        if is_ready or countdown.lower() in ("listo", "ready", ""):
            value = "✅ Listo"
        elif seconds > 0:
            value = f"⏳ {_fmt_seconds(seconds)}"
        else:
            value = countdown or "N/A"

        embed.add_field(name=f"{emoji} {label}", value=value, inline=True)

    return embed


_REF_EMOJI = {
    "Very Lenient": "🟢🟢",
    "Lenient":      "🟢",
    "Average":      "🟡",
    "Strict":       "🔴",
    "Very Strict":  "🔴🔴",
}

def embed_tactics(tactics: Optional[dict], referee: Optional[dict] = None) -> discord.Embed:
    if not tactics:
        return discord.Embed(
            title="Sin tácticas registradas",
            description="Ejecuta un scrape para capturar las tácticas actuales.",
            color=ERROR_COLOR,
        )

    team    = tactics.get("team_name", "Equipo")
    round_n = tactics.get("round", "?")
    scraped = _time_ago(tactics.get("scraped_at"))

    # Jornada desde referee si el scrape de tácticas no la trae
    jornada = round_n
    if referee and referee.get("matchday"):
        jornada = referee["matchday"]

    embed = discord.Embed(
        title=f"🎯  Tácticas — {team}",
        description=f"Jornada **{jornada}** · Capturado {scraped}",
        color=OSM_COLOR,
    )

    embed.add_field(name="📋 Plan de Juego", value=tactics.get("game_plan") or "N/A", inline=True)
    embed.add_field(name="⚡ Tackling",      value=tactics.get("tackling")  or "N/A", inline=True)
    embed.add_field(name="​",                value="​",                               inline=True)

    p   = tactics.get("pressure",  50)
    men = tactics.get("mentality", 50)
    tem = tactics.get("tempo",     50)
    embed.add_field(
        name="📊 Sliders",
        value=f"Presión: **{p}** · Mentalidad: **{men}** · Tempo: **{tem}**",
        inline=False,
    )

    embed.add_field(name="⬆️ Delanteros",  value=tactics.get("forwards_tactic")    or "N/A", inline=True)
    embed.add_field(name="➡️ Mediocampos", value=tactics.get("midfielders_tactic") or "N/A", inline=True)
    embed.add_field(name="⬇️ Defensas",    value=tactics.get("defenders_tactic")   or "N/A", inline=True)

    offside = "Sí ✅" if tactics.get("offside_trap") else "No"
    embed.add_field(name="🚩 Offside Trap", value=offside,                         inline=True)
    embed.add_field(name="🎯 Marcaje",      value=tactics.get("marking") or "N/A", inline=True)

    # Árbitro del próximo partido
    if referee:
        ref_name   = referee.get("referee_name")
        ref_strict = referee.get("referee_strictness")
        if ref_name or ref_strict:
            emoji = _REF_EMOJI.get(ref_strict, "⚖️")
            embed.add_field(
                name  = "🧑‍⚖️ Árbitro próximo partido",
                value = f"{ref_name or '?'}  {emoji} {ref_strict or '?'}",
                inline = False,
            )

    return embed


def embed_standings(league: dict) -> discord.Embed:
    standings = _parse_json_field(league.get("standings"))

    embed = discord.Embed(
        title=f"📊  Clasificación — {league['league_name']}",
        color=OSM_COLOR,
    )

    if not standings:
        embed.description = "No hay datos de clasificación. Ejecuta un scrape primero."
        return embed

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines  = []
    for pos, team in enumerate(standings[:12], 1):
        club    = (team.get("Club", "?"))[:18]
        pts     = team.get("Points", team.get("Pts", "?"))
        played  = team.get("Played", team.get("P", "?"))
        mgr     = team.get("Manager", "CPU")
        prefix  = medals.get(pos, f"`{pos:>2}.`")
        lines.append(f"{prefix} **{club}** — {pts}pts ({played}PJ) _{mgr}_")

    embed.description = "\n".join(lines)
    return embed


def embed_transfers(transfers: list[dict], league_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"💰  Fichajes — {league_name}", color=OSM_COLOR)

    if not transfers:
        embed.description = "Sin fichajes registrados."
        return embed

    lines = []
    for t in transfers:
        icon   = "🔴" if t.get("transaction_type") == "sale" else "🟢"
        player = (t.get("player_name") or "?")[:16]
        pos    = t.get("position", "?")
        price  = t.get("final_price", 0)
        mgr    = (t.get("manager_name") or "CPU")[:12]
        lines.append(f"{icon} **{player}** ({pos}) — {price}M · _{mgr}_")

    embed.description = "\n".join(lines[:8])
    return embed


def embed_spy_results(spy: dict, league_name: str) -> discord.Embed:
    """Embed con los resultados del spy: tácticas + plantilla + últimos partidos."""
    rival = spy.get("team_name", "Rival")
    mgr   = spy.get("manager", "")
    embed = discord.Embed(
        title=f"🔍  Análisis — {rival}",
        description=f"Liga: **{league_name}**{f'  ·  Manager: _{mgr}_' if mgr else ''}",
        color=0xF97316,
        timestamp=_utcnow(),
    )

    # Tácticas
    t = spy.get("tactics") or {}
    if t:
        formation = t.get("formation", "?")
        gp        = t.get("game_plan", "?")
        tackling  = t.get("tackling", "?")
        p_val = t.get("pressure", "?")
        m_val = t.get("mentality", "?")
        te_val = t.get("tempo", "?")
        fwd = t.get("fwd", "?")
        mid = t.get("mid", "?")
        dfn = t.get("def", "?")
        offside = "Sí ✅" if t.get("offside") else "No"
        embed.add_field(
            name="🎯 Tácticas",
            value=(
                f"**{formation}** · {gp} · {tackling}\n"
                f"Presión: {p_val} · Mental: {m_val} · Tempo: {te_val}\n"
                f"DEL: {fwd} · MED: {mid} · DEF: {dfn} · Offside: {offside}"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="🎯 Tácticas", value="_No disponibles aún_", inline=False)

    # Últimos partidos
    matches = spy.get("last_matches") or []
    if matches:
        lines = []
        for m in matches:
            venue  = "🏠" if m.get("home") else "✈️"
            opp    = m.get("opponent", "?")
            score  = m.get("score", "?")
            result = m.get("result", "")
            rnd    = m.get("round", "")
            result_icon = "✅" if result in ("W", "win")    else \
                          "❌" if result in ("L", "loss")   else \
                          "➖" if result in ("D", "draw")   else ""
            rnd_str = f"J{rnd} " if rnd else ""
            lines.append(f"{rnd_str}{venue} vs **{opp}** {score} {result_icon}")
        embed.add_field(name="📋 Últimos partidos", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="📋 Últimos partidos", value="_No disponibles aún_", inline=False)

    # Plantilla del rival (top 11 por stat)
    squad = spy.get("squad") or []
    if squad:
        starters = [p for p in squad if p.get("in_lineup")]
        display  = starters[:11] if starters else sorted(
            squad, key=lambda p: max(p.get("stat_att",0), p.get("stat_def",0), p.get("stat_ovr",0)),
            reverse=True
        )[:11]
        lines = []
        for p in display:
            name = p.get("name", "?")
            age  = p.get("age", 0)
            att  = p.get("stat_att", 0)
            def_ = p.get("stat_def", 0)
            ovr  = p.get("stat_ovr", 0)
            lines.append(f"**{name}** ({age}) · A{att}/D{def_}/O{ovr}")
        embed.add_field(
            name=f"👕 Plantilla ({len(squad)} jugadores, mostrando 11 mejores)",
            value="\n".join(lines) or "_Sin datos_",
            inline=False,
        )
    else:
        embed.add_field(name="👕 Plantilla", value="_No disponibles aún_", inline=False)

    return embed


def embed_rival_standings(rival_name: str, league_name: str,
                           standings: list[dict], my_team: str) -> discord.Embed:
    """Embed con posición del rival en la clasificación."""
    rival_row = None
    my_row    = None
    for row in standings:
        club = row.get("Club", "")
        if rival_name.lower() in club.lower():
            rival_row = row
        if my_team.lower() in club.lower():
            my_row = row

    embed = discord.Embed(
        title=f"📊  Rival — {rival_name}",
        description=f"Liga: **{league_name}**",
        color=0xF97316,
    )

    if rival_row:
        pos    = next((i+1 for i, r in enumerate(standings) if r.get("Club","") == rival_row.get("Club","")), "?")
        pts    = rival_row.get("Points", rival_row.get("Pts", "?"))
        played = rival_row.get("Played", rival_row.get("P", "?"))
        w      = rival_row.get("Won",   rival_row.get("W", "?"))
        d      = rival_row.get("Drew", rival_row.get("Drawn", rival_row.get("D", "?")))
        l      = rival_row.get("Lost",  rival_row.get("L", "?"))
        gf     = rival_row.get("GF", "?")
        ga     = rival_row.get("GA", "?")
        mgr    = rival_row.get("Manager", "CPU")
        embed.add_field(
            name=f"#{pos} {rival_name}",
            value=f"Manager: _{mgr}_\n{pts}pts · {played}PJ · {w}V {d}E {l}D · {gf}:{ga}",
            inline=False,
        )
    else:
        embed.add_field(name=rival_name, value="_No encontrado en la clasificación_", inline=False)

    if my_row:
        pos = next((i+1 for i, r in enumerate(standings) if r.get("Club","") == my_row.get("Club","")), "?")
        pts = my_row.get("Points", my_row.get("Pts", "?"))
        embed.add_field(name=f"📍 Mi equipo #{pos}", value=f"{my_team} · {pts}pts", inline=True)

    if standings:
        lines = []
        medals = {1:"🥇", 2:"🥈", 3:"🥉"}
        for i, row in enumerate(standings[:5], 1):
            club = row.get("Club","?")[:16]
            pts  = row.get("Points", row.get("Pts","?"))
            icon = medals.get(i, f"`{i}.`")
            marker = " ←" if rival_name.lower() in club.lower() else ""
            lines.append(f"{icon} {club} {pts}pts{marker}")
        embed.add_field(name="Top 5", value="\n".join(lines), inline=True)

    embed.set_footer(text="Usa /spy para análisis completo de tácticas y plantilla (timer 1h)")
    return embed


def _fmt_market_value(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)) and raw > 0:
        if raw >= 1_000_000:
            return f"{raw / 1_000_000:.1f}M"
        if raw >= 1_000:
            return f"{raw / 1_000:.0f}k"
    return ""


def embed_squad(slot: dict) -> discord.Embed:
    team     = slot.get("team_name", "Equipo")
    league   = slot.get("league_name", "")
    matchday = slot.get("matchday")
    players  = slot.get("players", [])

    md_str = ""
    if matchday:
        cur, tot = matchday["current"], matchday["total"]
        md_str = f"  ·  📅 Jornada **{cur}/{tot}**" + (" ✅" if matchday["finished"] else "")

    embed = discord.Embed(
        title=f"👕  Plantilla — {team}",
        description=f"Liga: **{league}**{md_str} · {len(players)} jugadores",
        color=OSM_COLOR,
        timestamp=_utcnow(),
    )

    if not players:
        embed.add_field(name="Sin datos", value="No se pudo leer la plantilla.", inline=False)
        return embed

    groups: dict[str, list] = {"A": [], "M": [], "D": [], "G": []}
    for p in players:
        pos = p.get("position", "")
        if pos in groups:
            groups[pos].append(p)

    pos_emoji = {"A": "⚡", "M": "🔄", "D": "🛡️", "G": "🧤"}
    pos_name  = {"A": "Delanteros", "M": "Mediocampos", "D": "Defensas", "G": "Porteros"}
    # Stat más relevante por posición: att para delanteros, def para defensas/porteros, ovr para meds
    main_stat_key = {"A": "stat_att", "M": "stat_ovr", "D": "stat_def", "G": "stat_def"}

    for pos_key in ("A", "M", "D", "G"):
        grp = groups[pos_key]
        if not grp:
            continue

        lines = []
        for p in grp:
            num  = p.get("squad_number", "")
            name = p.get("name", "?")
            sp   = p.get("specific_position", "")
            att  = p.get("stat_att", 0)
            def_ = p.get("stat_def", 0)
            fit  = p.get("fitness",  0)
            mor  = p.get("morale",   0)
            main = p.get(main_stat_key[pos_key], 0)

            # Estado como iconos compactos
            status = ""
            if p.get("is_injured"):      status += "🏥"
            if p.get("is_suspended"):    status += "🚫"
            if p.get("in_training"):     status += "🏃"
            if p.get("in_lineup"):       status += "🔵"
            elif p.get("in_selection"):  status += "📋"
            if p.get("is_in_form"):      status += "⚡"
            if p.get("is_world_star"):   status += "⭐"
            yc = p.get("yellow_cards", 0)
            if yc > 0:                   status += "🟡" * min(yc, 2)

            num_str = f"#{num}" if num else " "
            line = f"`{num_str:>3}` **{name}** {sp} · {main} (A{att}/D{def_}) · {fit}%/{mor}% {status}".rstrip()
            lines.append(line)

        field_val = "\n".join(lines)
        if len(field_val) > 1024:
            field_val = field_val[:1020] + "…"

        embed.add_field(
            name=f"{pos_emoji[pos_key]} {pos_name[pos_key]} ({len(grp)})",
            value=field_val,
            inline=False,
        )

    embed.set_footer(text="🔵 titular  📋 suplente  🏃 entrenando  🏥 lesionado  🚫 suspendido  ⚡ en forma  ⭐ world star")
    return embed


# ── NOTIFICACIONES AUTOMÁTICAS ────────────────────────────────────────────────

@tasks.loop(minutes=TIMER_CHECK_MINUTES)
async def _timer_alert_loop():
    """Revisa timers periódicamente y envía alertas al canal configurado."""
    if not DISCORD_ALERT_CHANNEL_ID:
        return
    channel = client.get_channel(DISCORD_ALERT_CHANNEL_ID)
    if not channel:
        return

    try:
        slots = await _get_timers_cached()
    except Exception as e:
        print(f"  ⚠️ [notifs] Scrape fallido: {e}")
        return

    # Acumular acciones — se procesan en una sola sesión de Playwright al final.
    training_to_renew:  list[tuple[str, str]]            = []  # (team, league)
    stadium_to_upgrade: list[tuple[str, str, list|None]] = []  # (team, league, preferred_parts)
    # Rastrear qué slots tienen timer de estadio activo (no es necesario iniciar proactivamente)
    slots_with_stadium_timer: set[int] = set()

    for slot in slots:
        slot_idx    = slot["slot_index"]
        team        = slot["team_name"]
        league_name = slot.get("league_name", "")
        matchday    = slot.get("matchday") or {}
        slot_events = slot.get("events", [])

        for timer in slot["timers"]:
            typ      = timer["type"]
            seconds  = timer["seconds"]
            is_ready = timer["is_ready"]
            emoji    = timer["emoji"]
            label    = timer["label_es"]
            key      = f"{slot_idx}_{typ}"
            prev     = _timer_state.get(key, seconds + 1)

            # Timer listo
            done_key = f"{key}_done"
            if is_ready and done_key not in _warned:
                await channel.send(f"✅ **{emoji} {label}** listo en **{team}**!")
                _warned.add(done_key)

                if typ == "training" and league_name:
                    if matchday.get("finished", False):
                        md = matchday
                        await channel.send(
                            f"⏸ Temporada terminada (**{md['current']}/{md['total']}**) en **{team}** — entrenamiento omitido."
                        )
                    else:
                        has_ev, ev_title = _has_upcoming_bonus_event(slot_events, "training")
                        if has_ev:
                            await channel.send(
                                f"⏳ **{team}**: evento de entrenamiento próximo — **{ev_title}** "
                                f"(en < {EVENT_DELAY_HOURS}h). Esperando para aprovechar timers reducidos."
                            )
                        else:
                            training_to_renew.append((team, league_name))

                elif typ == "stadium" and league_name:
                    has_ev, ev_title = _has_upcoming_bonus_event(slot_events, "stadium")
                    if has_ev:
                        await channel.send(
                            f"⏳ **{team}**: evento de estadio próximo — **{ev_title}** "
                            f"(en < {EVENT_DELAY_HOURS}h). Esperando para aprovechar timers reducidos."
                        )
                    else:
                        stadium_to_upgrade.append((team, league_name, _get_stadium_preferred(league_name)))

            # Registrar que este slot tiene un timer de estadio (en curso o listo)
            if typ == "stadium":
                slots_with_stadium_timer.add(slot_idx)

            # Advertencia: timer bajo el umbral (solo una vez)
            warn_key = f"{key}_warn"
            if 0 < seconds <= TIMER_WARNING_MINUTES * 60 and warn_key not in _warned:
                await channel.send(
                    f"⚠️ **{emoji} {label}** expira en **{_fmt_seconds(seconds)}** → _{team}_"
                )
                _warned.add(warn_key)

            # Timer reiniciado (nuevo ciclo) → limpiar estado
            if seconds > prev + 300:
                _warned.discard(done_key)
                _warned.discard(warn_key)

            _timer_state[key] = seconds

        # ── Detección de spy completado (OSM elimina el timer al terminar) ────
        # El timer de Analista de datos desaparece de la lista cuando termina.
        # Detectamos la transición: estaba activo (seconds > 0) → ahora ausente.
        spy_key      = f"{slot_idx}_spy"
        spy_done_key = f"{spy_key}_done"
        current_types = {t["type"] for t in slot["timers"]}
        if ("spy" not in current_types
                and _timer_state.get(spy_key, 0) > 0
                and spy_done_key not in _warned):
            await channel.send(
                f"🕵️ **Analista de datos** completado en **{team}**!\n"
                f"Usa `/spy` para leer tácticas y plantilla del rival."
            )
            _warned.add(spy_done_key)
            _timer_state[spy_key] = 0

    # ── Detección proactiva de estadio sin timer ─────────────────────────────
    # Si un slot tiene temporada activa y NO tiene ningún timer de estadio
    # (nada en construcción) → verificar si hay partes disponibles para ampliar.
    for slot in slots:
        slot_idx    = slot["slot_index"]
        team        = slot["team_name"]
        league_name = slot.get("league_name", "")
        matchday    = slot.get("matchday") or {}
        slot_events = slot.get("events", [])
        if (slot_idx not in slots_with_stadium_timer
                and league_name
                and not matchday.get("finished", False)
                and not any(lg == league_name for _, lg, _ in stadium_to_upgrade)):
            has_ev, ev_title = _has_upcoming_bonus_event(slot_events, "stadium")
            if has_ev:
                # No añadir al batch — esperar el evento
                pass
            else:
                stadium_to_upgrade.append((team, league_name, None))

    # ── Renovar entrenamientos en una sola sesión de Playwright ──────────────
    if training_to_renew:
        teams_str = ", ".join(t for t, _ in training_to_renew)
        await channel.send(f"🔄 Auto-renovando entrenamiento: **{teams_str}**...")
        try:
            batch_results = await asyncio.to_thread(
                _scrape_renewtraining_batch_sync, OSM_USER_ID, training_to_renew
            )
            for team, result in batch_results.items():
                claimed = result.get("claimed", [])
                started = result.get("started", [])
                errs    = result.get("errors", [])
                if claimed or started:
                    lines = [f"✅ **Entrenamiento renovado** en **{team}**"]
                    for c in claimed:
                        lines.append(f"  🏁 Terminó: **{c.get('player','?')}** ({c.get('title','?')})")
                    for s in started:
                        lines.append(f"  ▶️ Iniciado: **{s.get('player','?')}** ({s.get('title','?')})")
                    await channel.send("\n".join(lines))
                else:
                    await channel.send(
                        f"⚠️ No se renovó entrenamiento en **{team}**"
                        + (f": `{', '.join(errs)}`" if errs else "")
                    )
        except Exception as e:
            await channel.send(f"❌ Error en renovación de entrenamientos: {e}")

    # ── Upgrade de estadios en una sola sesión de Playwright ─────────────────
    if stadium_to_upgrade:
        teams_str = ", ".join(t for t, _, _ in stadium_to_upgrade)
        await channel.send(f"🏟️ Auto-actualizando estadio: **{teams_str}**...")
        try:
            batch_results = await asyncio.to_thread(
                _scrape_upgradestadium_batch_sync, OSM_USER_ID, stadium_to_upgrade
            )
            for team, result in batch_results.items():
                await channel.send(_fmt_stadium_result(result, team))
                # Actualizar alternancia: si se inició training→próximo es pitch, y viceversa
                for s in result.get("started", []):
                    if s.get("type") in ("training", "pitch"):
                        league = next((lg for t, lg, _ in stadium_to_upgrade if t == team), None)
                        if league:
                            _stadium_next_part[league] = "pitch" if s["type"] == "training" else "training"
        except Exception as e:
            await channel.send(f"❌ Error en upgrade de estadios: {e}")


@tasks.loop(hours=2)
async def _transferlist_loop():
    """Cada 2 horas verifica si la lista de transferibles está llena y rellena los huecos."""
    if not DISCORD_ALERT_CHANNEL_ID:
        return
    channel = client.get_channel(DISCORD_ALERT_CHANNEL_ID)
    if not channel:
        return

    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
    except Exception as e:
        print(f"  ⚠️ [transfer loop] No se pudieron obtener ligas: {e}")
        return

    # Construir lista de ligas a procesar: tienen candidatos configurados y temporada activa
    to_fill: list[tuple[str, str]] = []  # (team_name, league_name)
    for lg in leagues:
        league_name = lg["league_name"]
        candidates  = _get_transfer_candidates(league_name)
        if not candidates:
            continue
        to_fill.append((league_name, league_name))  # team_name = league_name aquí

    if not to_fill:
        return

    try:
        batch_results = await asyncio.to_thread(
            _scrape_filltransferlist_batch_sync, OSM_USER_ID, to_fill
        )
        for team, result in batch_results.items():
            added  = result.get("added", [])
            errors = result.get("errors", [])
            if added:
                await channel.send(_fmt_transferlist_result(result, team))
            elif "no_candidates" not in errors and "pool_exhausted" not in errors:
                if errors:
                    print(f"  ⚠️ [transfer loop] {team}: {errors}")
    except Exception as e:
        print(f"  ❌ [transfer loop] Error en batch: {e}")


@tasks.loop(hours=24)
async def _agent_transfer_loop():
    """
    Una vez al día el agente LLM analiza la plantilla y el historial de ventas
    para decidir autónomamente qué jugadores poner como candidatos de venta.
    Actualiza transfer_queue.json y notifica en Discord.
    """
    if not DISCORD_ALERT_CHANNEL_ID:
        return
    channel = client.get_channel(DISCORD_ALERT_CHANNEL_ID)
    if not channel:
        return

    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
    except Exception as e:
        print(f"  ⚠️ [agent transfer] No se pudieron obtener ligas: {e}")
        return

    for lg in leagues:
        league_name = lg["league_name"]
        league_id   = lg["league_id"]

        # No analizar temporadas terminadas
        # (matchday info no está en _get_active_leagues pero lo chequeamos vía timers cache si existe)
        try:
            result = await asyncio.to_thread(
                _run_agent_transfer_sync, OSM_USER_ID, league_name, league_id
            )

            if result.get("error"):
                print(f"  ⚠️ [agent transfer] {league_name}: {result['error']}")
                continue

            candidates = result.get("candidates", [])
            reasoning  = result.get("reasoning", "")

            if candidates:
                names_str = ", ".join(f"**{n}**" for n in candidates)
                msg = (
                    f"🤖 **Agente de Transferibles — {league_name}**\n"
                    f"Candidatos actualizados: {names_str}\n"
                    f"_{reasoning}_"
                )
                await channel.send(msg)
                print(f"  ✓ [agent transfer] {league_name}: {candidates}")

        except Exception as e:
            print(f"  ❌ [agent transfer] Error en {league_name}: {e}")


# ── VIEWS (botones interactivos) ──────────────────────────────────────────────

class PanelView(discord.ui.View):
    def __init__(self, leagues: list[dict]):
        super().__init__(timeout=180)
        self.leagues = leagues

        # Un Select con los slots activos
        if leagues:
            options = [
                discord.SelectOption(
                    label=f"Slot {i + 1}: {lg['league_name'][:25]}",
                    value=str(i),
                    description="Ver tácticas, clasificación y fichajes",
                )
                for i, lg in enumerate(leagues[:4])
            ]
            sel = discord.ui.Select(
                placeholder="Selecciona un slot…",
                options=options,
                custom_id="slot_select",
            )
            sel.callback = self._on_select
            self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        idx    = int(interaction.data["values"][0])
        league = self.leagues[idx]
        embed  = discord.Embed(
            title=f"⚽ Slot {idx + 1} — {league['league_name']}",
            description="¿Qué quieres ver?",
            color=OSM_COLOR,
        )
        await interaction.response.send_message(
            embed=embed,
            view=SlotDetailView(league),
            ephemeral=True,
        )

    @discord.ui.button(label="⏱ Timers en Vivo", style=discord.ButtonStyle.primary, row=1)
    async def btn_timers(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            slots = await asyncio.to_thread(_scrape_timers_sync, OSM_USER_ID)
            if not slots:
                await interaction.followup.send("No se pudieron obtener timers. Revisa los logs.", ephemeral=True)
                return
            for slot_data in slots:
                await interaction.followup.send(embed=embed_timers(slot_data))
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


class SlotDetailView(discord.ui.View):
    def __init__(self, league: dict):
        super().__init__(timeout=180)
        self.league = league

    @discord.ui.button(label="🎯 Tácticas",      style=discord.ButtonStyle.secondary)
    async def btn_tactics(self, interaction: discord.Interaction, _: discord.ui.Button):
        league_id = self.league["league_id"]
        tactics, referee = await asyncio.gather(
            asyncio.to_thread(_get_latest_tactics,     league_id),
            asyncio.to_thread(_get_referee_for_league, OSM_USER_ID, league_id),
        )
        await interaction.response.send_message(embed=embed_tactics(tactics, referee), ephemeral=True)

    @discord.ui.button(label="📊 Clasificación", style=discord.ButtonStyle.secondary)
    async def btn_standings(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(embed=embed_standings(self.league), ephemeral=True)

    @discord.ui.button(label="💰 Fichajes",      style=discord.ButtonStyle.secondary)
    async def btn_transfers(self, interaction: discord.Interaction, _: discord.ui.Button):
        transfers = await asyncio.to_thread(_get_recent_transfers, self.league["league_id"])
        await interaction.response.send_message(
            embed=embed_transfers(transfers, self.league["league_name"]),
            ephemeral=True,
        )


class TrainingQueueSelect(discord.ui.Select):
    """Select dropdown para programar el jugador de un tipo de coach específico."""

    def __init__(self, coach_type: str, players: list[dict],
                 league_name: str, current_queued: Optional[str]):
        self.coach_type  = coach_type
        self.league_name = league_name

        pos = _COACH_TO_POS.get(coach_type, "")
        pos_players = [p for p in players if p.get("position") == pos]

        stat_key = {"A": "stat_att", "M": "stat_ovr", "D": "stat_def", "G": "stat_def"}.get(pos, "stat_ovr")

        options = [
            discord.SelectOption(
                label="🔄 Sin cambio (reutiliza el último)",
                value="__keep__",
                default=(current_queued is None),
            ),
            discord.SelectOption(
                label="❌ Limpiar programación",
                value="__clear__",
            ),
        ]
        for p in pos_players[:23]:
            name   = p.get("name", "?")
            sp     = p.get("specific_position", "")
            age    = p.get("age", 0)
            stat   = p.get(stat_key, 0)
            fit    = p.get("fitness", 0)
            flags  = ("🏥" if p.get("is_injured") else "") + ("🏃" if p.get("in_training") else "")
            label  = f"{flags}{name} ({sp})"[:100]
            desc   = f"{age} años · Stat {stat} · Fit {fit}%"[:100]
            options.append(discord.SelectOption(
                label=label,
                description=desc,
                value=name,
                default=(name == current_queued),
            ))

        emoji = _COACH_EMOJI.get(coach_type, "🏋️")
        super().__init__(
            placeholder=f"{emoji} {coach_type}",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected in ("__keep__", "__clear__"):
            _set_queued_player(self.league_name, self.coach_type, None)
            msg = (f"✅ **{self.coach_type}**: reutilizará el último jugador."
                   if selected == "__keep__"
                   else f"✅ **{self.coach_type}**: programación borrada.")
        else:
            _set_queued_player(self.league_name, self.coach_type, selected)
            msg = f"✅ **{self.coach_type}**: próxima sesión → **{selected}**"
        await interaction.response.send_message(msg, ephemeral=True)


class TrainingQueueView(discord.ui.View):
    def __init__(self, league_name: str, players: list[dict]):
        super().__init__(timeout=120)
        for coach_type in ("Attacking Coach", "Defending Coach",
                           "Midfielder Coach", "Goalkeeping Coach"):
            current = _training_queue.get(league_name, {}).get(coach_type)
            self.add_item(TrainingQueueSelect(coach_type, players, league_name, current))


class TransferQueueSelect(discord.ui.Select):
    """
    Select único con todos los jugadores de la plantilla.
    Sin restricción de posición — el usuario elige hasta 6 candidatos libres.
    Jugadores ordenados por stat principal ascendente (los más débiles primero,
    que son los más candidatos a vender).
    """

    def __init__(self, players: list[dict], league_name: str, current_names: list[str]):
        self.league_name = league_name
        current_set = {n.lower() for n in current_names}

        _POS_EMOJI   = {"A": "⚡", "M": "🔄", "D": "🛡️", "G": "🧤"}
        _STAT_KEY    = {"A": "stat_att", "M": "stat_ovr", "D": "stat_def", "G": "stat_def"}

        def main_stat(p: dict) -> int:
            return p.get(_STAT_KEY.get(p.get("position", ""), "stat_ovr"), 0)

        sorted_players = sorted(players, key=main_stat)  # ascendente = más débiles primero

        options = [discord.SelectOption(
            label="🚫 Limpiar candidatos",
            value="__clear__",
        )]
        for p in sorted_players[:24]:  # máx 25 opciones (24 jugadores + clear)
            name  = p.get("name", "?")
            sp    = p.get("specific_position", "")
            age   = p.get("age", 0)
            pos   = p.get("position", "")
            stat  = main_stat(p)
            val   = _fmt_market_value(p.get("value", 0))
            flags = ("🏥" if p.get("is_injured") else "") + ("🏃" if p.get("in_training") else "")
            emoji = _POS_EMOJI.get(pos, "")
            options.append(discord.SelectOption(
                label=f"{flags}{emoji}{name} ({sp})"[:100],
                description=f"{age} años · Stat {stat} · {val}"[:100],
                value=name,
                default=(name.lower() in current_set),
            ))

        super().__init__(
            placeholder="Elige los candidatos a transferir (máx 6, cualquier posición)",
            options=options,
            min_values=1,
            max_values=min(len(sorted_players), 6),
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values
        if "__clear__" in selected:
            _set_transfer_candidates(self.league_name, [])
            await interaction.response.send_message(
                "✅ Lista de candidatos limpiada.", ephemeral=True
            )
        else:
            _set_transfer_candidates(self.league_name, selected)
            names_str = ", ".join(f"**{n}**" for n in selected)
            await interaction.response.send_message(
                f"✅ Candidatos guardados: {names_str}", ephemeral=True
            )


class TransferQueueView(discord.ui.View):
    def __init__(self, league_name: str, players: list[dict]):
        super().__init__(timeout=180)
        current = _transfer_queue.get(league_name, [])
        self.add_item(TransferQueueSelect(players, league_name, current))


class AgentTacticsApplyView(discord.ui.View):
    """Botones para aplicar o descartar la recomendación táctica del agente."""

    def __init__(self, league_name: str, tactics: dict):
        super().__init__(timeout=120)
        self.league_name = league_name
        self.tactics     = tactics

    @discord.ui.button(label="✅ Aplicar táctica", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        # Filtrar solo los campos que acepta set_tactics_for_slot
        kwargs = {k: v for k, v in self.tactics.items()
                  if k in ("game_plan", "tackling", "pressure", "mentality", "tempo",
                            "marking", "forwards_tactic", "midfielders_tactic",
                            "defenders_tactic", "offside_trap")}
        result = await asyncio.to_thread(
            _scrape_settactics_sync, OSM_USER_ID, self.league_name, kwargs
        )
        self.stop()
        if result.get("success"):
            changed = ", ".join(result.get("changed", []))
            await interaction.followup.send(
                f"✅ Táctica aplicada en **{self.league_name}**: {changed}", ephemeral=True
            )
        else:
            errors = ", ".join(result.get("errors", []))
            await interaction.followup.send(
                f"⚠️ No se pudo aplicar la táctica: `{errors}`", ephemeral=True
            )

    @discord.ui.button(label="❌ Descartar", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Recomendación descartada.", ephemeral=True)
        self.stop()


class TacticsConfirmView(discord.ui.View):
    def __init__(self, league_name: str, kwargs: dict):
        super().__init__(timeout=60)
        self.league_name = league_name
        self.kwargs      = kwargs

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            _scrape_settactics_sync, OSM_USER_ID, self.league_name, self.kwargs
        )
        self.stop()
        if result["success"]:
            changed = ", ".join(result["changed"])
            await interaction.followup.send(f"✅ Tácticas aplicadas: **{changed}**", ephemeral=True)
        else:
            errors  = ", ".join(result["errors"])
            changed = ", ".join(result["changed"]) if result["changed"] else "ninguno"
            await interaction.followup.send(
                f"⚠️ Aplicados: **{changed}**\nErrores: `{errors}`\n"
                "Revisa los logs o usa `/tactics` para verificar el estado actual.",
                ephemeral=True,
            )

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Cancelado.", ephemeral=True)
        self.stop()


class StadiumUpgradeView(discord.ui.View):
    def __init__(self, league_name: str, slot_name: str, preferred_parts: list[str]):
        super().__init__(timeout=60)
        self.league_name    = league_name
        self.slot_name      = slot_name
        self.preferred_parts = preferred_parts

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            _scrape_upgradestadium_sync, OSM_USER_ID, self.league_name, self.preferred_parts
        )
        self.stop()
        await interaction.followup.send(
            _fmt_stadium_result(result, self.slot_name), ephemeral=True
        )

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Cancelado.", ephemeral=True)
        self.stop()


def _fmt_stadium_result(result: dict, team: str) -> str:
    claimed  = result.get("claimed", [])
    started  = result.get("started", [])
    skipped  = result.get("skipped", [])
    errors   = result.get("errors", [])
    cf       = result.get("cf", 0)
    savings  = result.get("savings", 0)

    if not claimed and not started and not errors:
        reasons = ", ".join(f"{t}({r})" for t, r in skipped) if skipped else "nada disponible"
        return f"ℹ️ Sin cambios en estadio de **{team}**: {reasons}"

    lines = [f"🏟️ **Estadio actualizado — {team}**"]
    for c in claimed:
        lines.append(f"  ✅ Completado: **{c}**")
    for s in started:
        lines.append(f"  🔨 Iniciado: **{s['type']}** ({s['name']}) — coste {s['cost']:,.0f}")
    for t, r in skipped:
        lines.append(f"  ⏭️ Saltado: {t} ({r})")
    if errors:
        lines.append(f"  ❌ Errores: `{', '.join(errors)}`")
    lines.append(f"  💰 CF={cf:,.0f}  Savings={savings:,.0f}")
    return "\n".join(lines)


def _fmt_transferlist_result(result: dict, team: str) -> str:
    added   = result.get("added", [])
    skipped = result.get("skipped", [])
    errors  = result.get("errors", [])
    filled  = result.get("filled_before", 0)
    maxs    = result.get("max_slots", 4)

    if not added and not errors:
        return f"ℹ️ Lista de transferibles completa en **{team}** ({filled}/{maxs})"
    if not added:
        err = ", ".join(errors)
        return f"⚠️ No se pudo añadir jugadores en **{team}**: `{err}`"

    lines = [f"💰 **Lista de transferibles actualizada — {team}** ({filled}→{filled+len(added)}/{maxs})"]
    for name in added:
        lines.append(f"  ✅ Añadido: **{name}**")
    if skipped:
        lines.append(f"  ⏭️ Sin candidatos disponibles: {len(skipped)} omitidos")
    if errors:
        lines.append(f"  ❌ Errores: `{', '.join(errors)}`")
    return "\n".join(lines)


class LineupConfirmView(discord.ui.View):
    def __init__(self, league_name: str, formation: str):
        super().__init__(timeout=60)
        self.league_name = league_name
        self.formation   = formation

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            _scrape_setlineup_sync, OSM_USER_ID, self.league_name, self.formation
        )
        self.stop()
        if result["success"]:
            improved_str = "✅ Jugadores aplicados" if result.get("improved") else "⚠️ Jugadores no aplicados (revisa manualmente)"
            await interaction.followup.send(
                f"✅ Formación: **{result['formation']}**\n{improved_str}", ephemeral=True
            )
        else:
            errors = ", ".join(result["errors"])
            await interaction.followup.send(
                f"⚠️ No se pudo aplicar la formación.\nErrores: `{errors}`",
                ephemeral=True,
            )

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Cancelado.", ephemeral=True)
        self.stop()


# ── GUARD ─────────────────────────────────────────────────────────────────────

def _is_owner(interaction: discord.Interaction) -> bool:
    return DISCORD_OWNER_ID == 0 or interaction.user.id == DISCORD_OWNER_ID


# ── AUTOCOMPLETE Y CHOICES ───────────────────────────────────────────────────

async def _slot_autocomplete(
    _interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Devuelve los equipos activos como opciones para el parámetro slot.
    Incluye ligas del timer cache aunque no estén en la BD todavía."""
    try:
        leagues = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
    except Exception:
        return []
    return [
        app_commands.Choice(name=lg["league_name"][:100], value=lg["league_name"][:100])
        for lg in leagues[:4]
        if not current or current.lower() in lg["league_name"].lower()
    ][:25]


_GAMEPLAN_CHOICES = [
    app_commands.Choice(name="Shoot on sight", value="Shoot on sight"),
    app_commands.Choice(name="Long ball",       value="Long ball"),
    app_commands.Choice(name="Counter-attack",  value="Counter-attack"),
    app_commands.Choice(name="Wing play",       value="Wing play"),
    app_commands.Choice(name="Passing game",    value="Passing game"),
]
_TACKLING_CHOICES = [
    app_commands.Choice(name="Careful",    value="Careful"),
    app_commands.Choice(name="Normal",     value="Normal"),
    app_commands.Choice(name="Reckless",   value="Reckless"),
    app_commands.Choice(name="Aggressive", value="Aggressive"),
]
_MARKING_CHOICES = [
    app_commands.Choice(name="Zonal marking", value="Zonal marking"),
    app_commands.Choice(name="Man marking",   value="Man marking"),
]
_FWD_TACTIC_CHOICES = [
    app_commands.Choice(name="Attack only",      value="Attack only"),
    app_commands.Choice(name="Support midfield", value="Support midfield"),
    app_commands.Choice(name="Drop deep",        value="Drop deep"),
]
_MID_TACTIC_CHOICES = [
    app_commands.Choice(name="Protect the defence", value="Protect the defence"),
    app_commands.Choice(name="Push forward",        value="Push forward"),
    app_commands.Choice(name="Stay in position",    value="Stay in position"),
]
_DEF_TACTIC_CHOICES = [
    app_commands.Choice(name="Defend deep",          value="Defend deep"),
    app_commands.Choice(name="Attacking full-backs",  value="Attacking full-backs"),
    app_commands.Choice(name="Support midfield",      value="Support midfield"),
]
_OFFSIDE_CHOICES = [
    app_commands.Choice(name="Yes", value="Yes"),
    app_commands.Choice(name="No",  value="No"),
]


def _slot_idx(slot_str: str, leagues: list[dict]) -> int | None:
    """Convierte el valor del autocomplete (nombre o índice numérico) a índice validado."""
    try:
        idx = int(slot_str)
        return idx if 0 <= idx < len(leagues) else None
    except (ValueError, TypeError):
        pass
    # Búsqueda por nombre (substring, case-insensitive)
    s = slot_str.lower()
    for i, lg in enumerate(leagues):
        if s in lg["league_name"].lower() or lg["league_name"].lower() in s:
            return i
    return None


# ── SLASH COMMANDS ────────────────────────────────────────────────────────────

@tree.command(name="panel", description="Panel principal con el estado de tus equipos OSM")
async def cmd_panel(interaction: discord.Interaction):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        await interaction.followup.send(
            embed=embed_panel(leagues, OSM_USER_ID),
            view=PanelView(leagues),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="timers", description="Lee timers en tiempo real desde OSM (abre navegador ~30s)")
async def cmd_timers(interaction: discord.Interaction):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        slots = await asyncio.to_thread(_scrape_timers_sync, OSM_USER_ID)
        if not slots:
            await interaction.followup.send("No se pudieron obtener timers. Revisa los logs del servidor.")
            return
        for slot_data in slots:
            matchday = slot_data.get("matchday") or {}
            if matchday.get("finished"):
                cur, tot = matchday["current"], matchday["total"]
                team   = slot_data.get("team_name", "Equipo")
                league = slot_data.get("league_name", "")
                embed  = discord.Embed(
                    title=f"⏱  Timers — {team}",
                    description=f"Liga: **{league}** · 📅 Jornada **{cur}/{tot}**\n\n⛔ Temporada terminada — sin acciones automáticas.",
                    color=ERROR_COLOR,
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(embed=embed_timers(slot_data))
    except Exception as e:
        await interaction.followup.send(f"❌ Error al leer timers: {e}")


@tree.command(name="tactics", description="Tácticas del próximo partido de un equipo")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_tactics(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado. Usa el autocompletado para seleccionarlo.")
            return
        league  = leagues[idx]
        tactics, referee = await asyncio.gather(
            asyncio.to_thread(_get_latest_tactics,       league["league_id"]),
            asyncio.to_thread(_get_referee_for_league,   OSM_USER_ID, league["league_id"]),
        )
        await interaction.followup.send(embed=embed_tactics(tactics, referee))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="standings", description="Clasificación de un equipo")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_standings(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        await interaction.followup.send(embed=embed_standings(leagues[idx]))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="fichajes", description="Últimos fichajes de un equipo")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_fichajes(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        lg        = leagues[idx]
        transfers = await asyncio.to_thread(_get_recent_transfers, lg["league_id"])
        await interaction.followup.send(embed=embed_transfers(transfers, lg["league_name"]))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="settactics", description="Cambia las tácticas de un equipo (abre navegador ~30s)")
@app_commands.describe(
    slot       = "Selecciona tu equipo",
    gameplan   = "Plan de juego",
    tackling   = "Tipo de entrada",
    pressure   = "Presión (0-100)",
    mentality  = "Mentalidad (0-100)",
    tempo      = "Tempo (0-100)",
    marking    = "Marcaje",
    fwd        = "Táctica de delanteros",
    mid        = "Táctica de mediocampistas",
    defenders  = "Táctica de defensas",
    offside    = "Trampa del offside",
)
@app_commands.autocomplete(slot=_slot_autocomplete)
@app_commands.choices(
    gameplan = _GAMEPLAN_CHOICES,
    tackling = _TACKLING_CHOICES,
    marking  = _MARKING_CHOICES,
    fwd      = _FWD_TACTIC_CHOICES,
    mid      = _MID_TACTIC_CHOICES,
    defenders = _DEF_TACTIC_CHOICES,
    offside   = _OFFSIDE_CHOICES,
)
async def cmd_settactics(
    interaction : discord.Interaction,
    slot        : str           = "0",
    gameplan    : Optional[str] = None,
    tackling    : Optional[str] = None,
    pressure    : Optional[int] = None,
    mentality   : Optional[int] = None,
    tempo       : Optional[int] = None,
    marking     : Optional[str] = None,
    fwd         : Optional[str] = None,
    mid         : Optional[str] = None,
    defenders   : Optional[str] = None,
    offside     : Optional[str] = None,
):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    # Validar numéricos
    for field, val, label in [
        ("pressure",  pressure,  "Presión"),
        ("mentality", mentality, "Mentalidad"),
        ("tempo",     tempo,     "Tempo"),
    ]:
        if val is not None and not (0 <= val <= 100):
            await interaction.response.send_message(
                f"❌ **{label}** debe estar entre 0 y 100 (recibido: {val}).",
                ephemeral=True,
            )
            return

    kwargs: dict = {}
    if gameplan:              kwargs["game_plan"]           = gameplan
    if tackling:              kwargs["tackling"]            = tackling
    if pressure  is not None: kwargs["pressure"]            = pressure
    if mentality is not None: kwargs["mentality"]           = mentality
    if tempo     is not None: kwargs["tempo"]               = tempo
    if marking:               kwargs["marking"]             = marking
    if fwd:                   kwargs["forwards_tactic"]     = fwd
    if mid:                   kwargs["midfielders_tactic"]  = mid
    if defenders:             kwargs["defenders_tactic"]    = defenders
    if offside:               kwargs["offside_trap"]        = (offside == "Yes")

    if not kwargs:
        await interaction.response.send_message(
            "Selecciona al menos un parámetro a cambiar.", ephemeral=True
        )
        return

    try:
        leagues   = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
        idx       = _slot_idx(slot, leagues)
        slot_name = leagues[idx]["league_name"] if idx is not None else "Equipo"
    except Exception:
        idx, slot_name = 0, "Equipo"

    if idx is None:
        await interaction.response.send_message("Equipo no encontrado.", ephemeral=True)
        return

    _LABEL_ES = {
        "game_plan":          "Plan de juego",
        "tackling":           "Tackling",
        "pressure":           "Presión",
        "mentality":          "Mentalidad",
        "tempo":              "Tempo",
        "marking":            "Marcaje",
        "forwards_tactic":    "Delanteros",
        "midfielders_tactic": "Mediocampistas",
        "defenders_tactic":   "Defensas",
        "offside_trap":       "Offside trap",
    }
    def _fmt_v(v) -> str:
        if isinstance(v, bool):
            return "Sí" if v else "No"
        return str(v)
    lines = [f"• **{_LABEL_ES.get(k, k)}**: `{_fmt_v(v)}`" for k, v in kwargs.items()]
    embed = discord.Embed(
        title       = f"⚙️ Cambiar Tácticas — {slot_name}",
        description = "¿Confirmas los siguientes cambios?\n\n" + "\n".join(lines),
        color       = OSM_COLOR,
    )
    await interaction.response.send_message(
        embed     = embed,
        view      = TacticsConfirmView(league_name=slot_name, kwargs=kwargs),
        ephemeral = True,
    )


@tree.command(name="renewtraining", description="Renueva los entrenamientos terminados de un equipo (~30s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_renewtraining(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    try:
        leagues   = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
        idx       = _slot_idx(slot, leagues)
        slot_name = leagues[idx]["league_name"] if idx is not None else "Equipo"
    except Exception:
        idx, slot_name = 0, "Equipo"

    if idx is None:
        await interaction.response.send_message("Equipo no encontrado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    result = await asyncio.to_thread(_scrape_renewtraining_sync, OSM_USER_ID, slot_name)

    claimed = result.get("claimed", [])
    started = result.get("started", [])
    errors  = result.get("errors", [])

    if claimed or started:
        lines = [f"✅ **Entrenamientos renovados — {slot_name}**"]
        for c in claimed:
            p = c.get("player") or "?"
            t = c.get("title") or f"slot {c.get('slot','?')}"
            lines.append(f"  🏁 Terminó: **{p}** ({t})")
        for s in started:
            p = s.get("player") or "?"
            t = s.get("title") or f"slot {s.get('slot','?')}"
            lines.append(f"  ▶️ Iniciado: **{p}** ({t})")
        if errors:
            lines.append(f"  ⚠️ Errores: `{', '.join(errors)}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    else:
        err_str = f": `{', '.join(errors)}`" if errors else " (no había entrenamientos listos o vacíos)"
        await interaction.followup.send(
            f"⚠️ No se renovaron entrenamientos en **{slot_name}**{err_str}",
            ephemeral=True,
        )


@tree.command(name="events", description="Muestra el calendario de eventos OSM del mes")
async def cmd_events(interaction: discord.Interaction):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        from scraper_events import fetch_events, format_events_for_discord
        events = await asyncio.to_thread(fetch_events)
        text = format_events_for_discord(events)
        embed = discord.Embed(
            title="📅 Eventos OSM — Este mes",
            description=text,
            color=OSM_COLOR,
            timestamp=_utcnow(),
        )
        embed.set_footer(text="Fuente: forum.onlinesoccermanager.com")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error obteniendo eventos: {e}", ephemeral=True)


_STADIUM_PART_CHOICES = [
    app_commands.Choice(name="Entradas (Capacity)",           value="capacity"),
    app_commands.Choice(name="Campo (Pitch)",                 value="pitch"),
    app_commands.Choice(name="Entrenamiento (Training)",      value="training"),
    app_commands.Choice(name="Auto (mejor disponible)",       value="auto"),
]


@tree.command(name="upgradestadium", description="Amplía el estadio de un equipo (~45s)")
@app_commands.describe(
    slot = "Selecciona tu equipo",
    part = "Parte del estadio a ampliar",
)
@app_commands.autocomplete(slot=_slot_autocomplete)
@app_commands.choices(part=_STADIUM_PART_CHOICES)
async def cmd_upgradestadium(
    interaction: discord.Interaction,
    slot: str = "0",
    part: str = "auto",
):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    try:
        leagues   = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
        idx       = _slot_idx(slot, leagues)
        slot_name = leagues[idx]["league_name"] if idx is not None else "Equipo"
    except Exception:
        idx, slot_name = 0, "Equipo"

    if idx is None:
        await interaction.response.send_message("Equipo no encontrado.", ephemeral=True)
        return

    preferred = None if part == "auto" else [part]
    part_label = dict(capacity="Entradas", pitch="Campo", training="Entrenamiento").get(part, "Auto")

    embed = discord.Embed(
        title       = f"🏟️ Ampliar Estadio — {slot_name}",
        description = f"¿Confirmas ampliar **{part_label}**?\n\n"
                      "• Se verificará el saldo disponible\n"
                      "• Si el dinero está en Savings se transferirá temporalmente\n"
                      "• Al finalizar el saldo se devuelve a Savings",
        color       = OSM_COLOR,
    )
    await interaction.response.send_message(
        embed = embed,
        view  = StadiumUpgradeView(league_name=slot_name, slot_name=slot_name,
                                    preferred_parts=preferred),
        ephemeral = True,
    )


_FORMATION_CHOICES = [
    app_commands.Choice(name=f, value=f)
    for f in [
        "4-3-3 A", "4-3-3 B", "4-5-1",   "4-2-3-1",
        "4-4-2 A", "4-4-2 B", "3-2-5",   "3-2-3-2",
        "3-3-4 A", "3-3-4 B", "3-4-3 A", "3-4-3 B",
        "3-3-2-2", "3-5-2",   "4-2-4 A", "4-2-4 B",
        "5-2-3 A", "5-2-3 B", "5-3-2",   "5-3-1-1",
        "5-4-1 A", "5-4-1 B", "6-3-1 A", "6-3-1 B",
    ]
]


@tree.command(name="setlineup", description="Cambia la formación de un equipo (~30s)")
@app_commands.describe(
    slot      = "Selecciona tu equipo",
    formation = "Formación a aplicar",
)
@app_commands.autocomplete(slot=_slot_autocomplete)
@app_commands.choices(formation=_FORMATION_CHOICES)
async def cmd_setlineup(
    interaction: discord.Interaction,
    slot:        str,
    formation:   str,
):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    try:
        leagues   = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
        idx       = _slot_idx(slot, leagues)
        slot_name = leagues[idx]["league_name"] if idx is not None else "Equipo"
    except Exception:
        idx, slot_name = 0, "Equipo"

    if idx is None:
        await interaction.response.send_message("Equipo no encontrado.", ephemeral=True)
        return

    embed = discord.Embed(
        title       = f"🗂️ Cambiar Formación — {slot_name}",
        description = f"¿Confirmas cambiar la formación a **{formation}**?",
        color       = OSM_COLOR,
    )
    await interaction.response.send_message(
        embed = embed,
        view  = LineupConfirmView(league_name=slot_name, formation=formation),
        ephemeral = True,
    )


@tree.command(name="spy", description="Inicia el espionaje del próximo rival o lee resultados si ya terminó (~30s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_spy(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        league_name = leagues[idx]["league_name"]
        result      = await asyncio.to_thread(_scrape_spy_sync, OSM_USER_ID, league_name)

        action    = result.get("action")
        team_name = result.get("team_name") or "Rival"
        error     = result.get("error")

        # Si DataAnalyst no pudo leer el estado pero hay un timer de spy activo,
        # el spy está en curso — no mostrar error.
        if action == "error":
            spy_timer_active = any(
                t.get("type") == "spy" and t.get("seconds", 0) > 0
                for slot in _last_scrape_result
                if slot.get("league_name", "") == league_name
                for t in slot.get("timers", [])
            )
            if spy_timer_active:
                action = "in_progress"
            else:
                await interaction.followup.send(f"❌ Error: `{error}`")

        elif action == "started":
            cost = result.get("start_result", {}).get("cost", 0)
            cost_str = f" (coste: {cost:,})" if cost else " (gratuito)"
            embed = discord.Embed(
                title=f"🔍  Spy iniciado — {team_name}",
                description=f"Liga: **{league_name}**\n\n"
                             f"El espionaje tardará **~1 hora** en completarse.{cost_str}\n"
                             f"Cuando termine el timer, usa `/spy` de nuevo para leer los resultados.",
                color=0xF97316,
                timestamp=_utcnow(),
            )
            await interaction.followup.send(embed=embed)

        elif action == "in_progress":
            embed = discord.Embed(
                title=f"⏳  Spy en curso — {team_name}",
                description=f"Liga: **{league_name}**\n\nEl espionaje aún no terminó.\n"
                             f"Vuelve a usar `/spy` cuando el timer de **Ojeador** quede en ✅ Listo.",
                color=OSM_COLOR,
            )
            await interaction.followup.send(embed=embed)

        elif action == "results":
            spy_data = result.get("spy_result", {})
            await interaction.followup.send(embed=embed_spy_results(spy_data, league_name))

        else:
            await interaction.followup.send(f"Estado desconocido: `{action}`")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="rival", description="Análisis del próximo rival: clasificación y datos disponibles")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_rival(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        league      = leagues[idx]
        league_name = league["league_name"]
        league_id   = league["league_id"]

        # Datos de clasificación (rápido, sin navegador)
        standings = await asyncio.to_thread(_get_standings_for_league, league_id)

        # Scrapeamos DataAnalyst para leer el nombre del próximo rival
        # y ver si hay resultados de spy disponibles
        result = await asyncio.to_thread(_scrape_spy_sync, OSM_USER_ID, league_name)
        rival_name = result.get("team_name", "")
        action     = result.get("action")

        # Si DataAnalyst no pudo leer el rival pero hay un timer de spy activo
        # en el cache de timers, inferir que el spy está en curso.
        if not rival_name or action == "error":
            spy_timer_active = any(
                t.get("type") == "spy" and t.get("seconds", 0) > 0
                for slot in _last_scrape_result
                if slot.get("league_name", "") == league_name
                for t in slot.get("timers", [])
            )
            if spy_timer_active:
                action = "in_progress"

        # Últimos partidos del rival desde la BD (datos ya guardados previamente)
        last_matches: list[dict] = []
        if rival_name:
            last_matches = await asyncio.to_thread(_get_recent_matches_db, league_id, rival_name)

        if action == "results" and result.get("spy_result"):
            # Spy completo → análisis completo con tácticas + plantilla
            spy_data = result["spy_result"]
            if last_matches and not spy_data.get("last_matches"):
                spy_data["last_matches"] = last_matches
            await interaction.followup.send(embed=embed_spy_results(spy_data, league_name))
        else:
            if not rival_name:
                rival_name = "próximo rival"

            my_team = _get_my_team_name(league)
            embed = embed_rival_standings(rival_name, league_name, standings, my_team)

            # Últimos partidos (siempre disponibles, sin spy)
            if last_matches:
                lines = []
                for m in last_matches:
                    venue  = "🏠" if m.get("is_home") else "✈️"
                    opp    = m.get("away_team") if m.get("is_home") else m.get("home_team")
                    score  = m.get("score", "?")
                    res    = m.get("result_for_opponent", "")
                    icon   = {"W": "✅", "D": "➖", "L": "❌"}.get(res, "")
                    rnd    = m.get("round", "")
                    rnd_s  = f"J{rnd} " if rnd else ""
                    lines.append(f"{rnd_s}{venue} vs **{opp}** `{score}` {icon}")
                embed.add_field(
                    name=f"📋 Últimos {len(last_matches)} partidos de {rival_name}",
                    value="\n".join(lines),
                    inline=False,
                )

            # Nota sobre el spy
            if action == "started":
                extra = "\n🔍 Espionaje iniciado — usa `/spy` en ~1h para tácticas y plantilla."
            elif action == "in_progress":
                extra = "\n⏳ Espionaje en curso — usa `/spy` cuando el timer de **Ojeador** quede en ✅."
            else:
                extra = "\n💡 Usa `/spy` para obtener tácticas y plantilla completa del rival."
            embed.description = (embed.description or "") + extra

            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="settransferqueue", description="Configura qué jugadores poner en venta automáticamente (~30s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_settransferqueue(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        league_name = leagues[idx]["league_name"]
        slot_data   = await asyncio.to_thread(_scrape_squad_sync, OSM_USER_ID, league_name)
        players     = slot_data.get("players", [])
        team_name   = slot_data.get("team_name", league_name)

        candidates = _get_transfer_candidates(league_name)
        if candidates:
            cand_str = ", ".join(f"**{n}**" for n in candidates)
            cand_text = f"Candidatos actuales: {cand_str}"
        else:
            cand_text = "_Sin candidatos configurados_"

        embed = discord.Embed(
            title=f"💰  Candidatos Transferibles — {team_name}",
            description=(
                "Selecciona los jugadores que el bot pondrá en venta automáticamente "
                "cuando haya slots vacíos en la lista de transferibles.\n"
                "El bot mantiene siempre la lista llena (4 normal, 6 en Transfer Madness).\n"
                "Sin restricción de posición — puedes elegir cualquier combinación.\n\n"
                f"**Estado actual:** {cand_text}"
            ),
            color=OSM_COLOR,
        )
        await interaction.followup.send(
            embed=embed,
            view=TransferQueueView(league_name=league_name, players=players),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="filltransferlist", description="Rellena la lista de transferibles ahora con los candidatos configurados (~45s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_filltransferlist(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.", ephemeral=True)
            return
        league_name = leagues[idx]["league_name"]
        candidates  = _get_transfer_candidates(league_name)
        if not candidates:
            await interaction.followup.send(
                f"⚠️ No hay candidatos configurados para **{league_name}**. "
                "Usa `/settransferqueue` para configurarlos.",
                ephemeral=True,
            )
            return
        result = await asyncio.to_thread(_scrape_filltransferlist_sync, OSM_USER_ID, league_name)
        await interaction.followup.send(
            _fmt_transferlist_result(result, league_name), ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="agentransfer", description="Agente IA: analiza la plantilla y decide candidatos de venta (~45s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_agentransfer(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        league_name = leagues[idx]["league_name"]
        league_id   = leagues[idx]["league_id"]

        result = await asyncio.to_thread(
            _run_agent_transfer_sync, OSM_USER_ID, league_name, league_id
        )

        if result.get("error"):
            await interaction.followup.send(f"❌ Error del agente: `{result['error']}`")
            return

        candidates = result.get("candidates", [])
        reasoning  = result.get("reasoning", "")

        embed = discord.Embed(
            title=f"🤖  Agente de Transferibles — {league_name}",
            description=reasoning or "Análisis completado.",
            color=OSM_COLOR,
            timestamp=_utcnow(),
        )
        if candidates:
            embed.add_field(
                name="💰 Candidatos seleccionados",
                value="\n".join(f"• **{n}**" for n in candidates),
                inline=False,
            )
            embed.set_footer(text="Transfer queue actualizado automáticamente")
        else:
            embed.add_field(name="Sin candidatos", value="El agente no encontró jugadores para vender.", inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="agenttactics", description="Agente IA: recomienda tácticas contra un rival (~45s)")
@app_commands.describe(
    slot     = "Selecciona tu equipo",
    opponent = "Nombre del equipo rival",
)
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_agenttactics(interaction: discord.Interaction, slot: str = "0", opponent: str = ""):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    if not opponent:
        await interaction.response.send_message("Indica el nombre del equipo rival.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues     = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx         = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado.")
            return
        league_name = leagues[idx]["league_name"]
        league_id   = leagues[idx]["league_id"]

        result = await asyncio.to_thread(
            _run_agent_tactics_sync, OSM_USER_ID, league_name, league_id, opponent
        )

        if result.get("error"):
            await interaction.followup.send(f"❌ Error del agente: `{result['error']}`")
            return

        reasoning = result.get("reasoning", "")
        embed = discord.Embed(
            title=f"🤖  Táctica recomendada vs {opponent}",
            description=f"_{reasoning}_" if reasoning else "Análisis completado.",
            color=OSM_COLOR,
            timestamp=_utcnow(),
        )
        embed.add_field(name="🗂️ Formación",      value=result.get("formation", "?"),      inline=True)
        embed.add_field(name="📋 Plan de juego",  value=result.get("game_plan", "?"),      inline=True)
        embed.add_field(name="⚡ Tackling",        value=result.get("tackling", "?"),       inline=True)
        p   = result.get("pressure",  50)
        men = result.get("mentality", 50)
        tem = result.get("tempo",     50)
        embed.add_field(name="📊 Sliders",
                        value=f"Presión: **{p}** · Mentalidad: **{men}** · Tempo: **{tem}**",
                        inline=False)
        embed.add_field(name="⬆️ Delanteros",  value=result.get("forwards_tactic", "?"),    inline=True)
        embed.add_field(name="➡️ Medios",       value=result.get("midfielders_tactic", "?"), inline=True)
        embed.add_field(name="⬇️ Defensas",     value=result.get("defenders_tactic", "?"),  inline=True)
        embed.add_field(name="🎯 Marcaje",       value=result.get("marking", "?"),           inline=True)
        embed.add_field(name="🚩 Offside",       value="Sí ✅" if result.get("offside_trap") else "No", inline=True)

        view = AgentTacticsApplyView(league_name=league_name, tactics=result)
        await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="queuetraining", description="Programa qué jugadores entrenarán en la próxima sesión (~30s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_queuetraining(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues = await asyncio.to_thread(_get_all_active_slots, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado. Usa el autocompletado para seleccionarlo.")
            return
        league_name = leagues[idx]["league_name"]
        slot_data   = await asyncio.to_thread(_scrape_squad_sync, OSM_USER_ID, league_name)
        players     = slot_data.get("players", [])
        team_name   = slot_data.get("team_name", league_name)

        queue = _training_queue.get(league_name, {})
        status_lines = []
        for coach in ("Attacking Coach", "Defending Coach", "Midfielder Coach", "Goalkeeping Coach"):
            emoji  = _COACH_EMOJI[coach]
            queued = queue.get(coach)
            status_lines.append(
                f"{emoji} **{coach}**: → **{queued}**" if queued
                else f"{emoji} **{coach}**: reutiliza el último jugador"
            )

        embed = discord.Embed(
            title=f"🏋️  Programar Entrenamientos — {team_name}",
            description=(
                "Selecciona quién entrenará en la **próxima** sesión de cada tipo.\n"
                "El ajuste se guarda y se aplica en cada renovación automática.\n\n"
                + "\n".join(status_lines)
            ),
            color=OSM_COLOR,
        )
        await interaction.followup.send(
            embed=embed,
            view=TrainingQueueView(league_name=league_name, players=players),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="squad", description="Lee la plantilla completa de un equipo en tiempo real (~30s)")
@app_commands.describe(slot="Selecciona tu equipo")
@app_commands.autocomplete(slot=_slot_autocomplete)
async def cmd_squad(interaction: discord.Interaction, slot: str = "0"):
    if not _is_owner(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx = _slot_idx(slot, leagues)
        if idx is None:
            await interaction.followup.send("Equipo no encontrado. Usa el autocompletado para seleccionarlo.")
            return
        league_name = leagues[idx]["league_name"]
        slot_data = await asyncio.to_thread(_scrape_squad_sync, OSM_USER_ID, league_name)
        if slot_data.get("error") and not slot_data.get("players"):
            await interaction.followup.send(f"❌ Error al leer plantilla: `{slot_data['error']}`")
            return
        await interaction.followup.send(embed=embed_squad(slot_data))
    except Exception as e:
        await interaction.followup.send(f"❌ Error al leer plantilla: {e}")


# ── EVENTOS ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    _load_training_queue()
    _load_transfer_queue()
    print(f"✅ Bot conectado como {client.user} (ID: {client.user.id})")

    if DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"✅ Comandos sincronizados en servidor {DISCORD_GUILD_ID} (instantáneo)")
    else:
        await tree.sync()
        print("✅ Comandos sincronizados globalmente (puede tardar hasta 1h en aparecer)")

    print(f"   Owner ID: {DISCORD_OWNER_ID or 'sin restricción (cualquiera puede usar el bot)'}")
    print(f"   OSM User: {OSM_USER_ID}")

    if DISCORD_ALERT_CHANNEL_ID:
        if not _timer_alert_loop.is_running():
            _timer_alert_loop.start()
        if not _transferlist_loop.is_running():
            _transferlist_loop.start()
        # Loop del agente IA solo si está explícitamente habilitado
        if os.getenv("ENABLE_AGENT_LOOP", "false").lower() in ("true", "1"):
            if not _agent_transfer_loop.is_running():
                _agent_transfer_loop.start()
            print(f"🤖 Loop de agente de transferibles activado (cada 24h)")
        else:
            print(f"🤖 Agente IA: loop desactivado (ENABLE_AGENT_LOOP=false). Usa /agentransfer manualmente.")
        print(f"🔔 Alertas activadas → canal {DISCORD_ALERT_CHANNEL_ID} cada {TIMER_CHECK_MINUTES}m (aviso a {TIMER_WARNING_MINUTES}m)")
        print(f"💰 Loop de transferibles activado (cada 2h)")
    else:
        print("🔕 Alertas desactivadas (DISCORD_ALERT_CHANNEL_ID no configurado)")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:  missing.append("DISCORD_BOT_TOKEN")
    if not OSM_USER_ID:    missing.append("OSM_USER_ID")

    if missing:
        print(f"❌ Variables de entorno faltantes: {', '.join(missing)}")
        print("   Configúralas en .env y vuelve a ejecutar.")
        sys.exit(1)

    if DISCORD_OWNER_ID == 0:
        print("⚠️  DISCORD_OWNER_ID no configurado — cualquier usuario puede usar el bot.")

    print("🤖 Iniciando bot de Discord OSM...")
    client.run(DISCORD_TOKEN)
