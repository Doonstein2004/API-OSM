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

OSM_COLOR   = 0x22D3EE   # Cyan del tema OSM
ERROR_COLOR = 0xFF6B6B

# Caché en memoria para el scrape de timers (evita doble scrape entre /timers y notifs)
_last_scrape_time:   Optional[datetime]   = None
_last_scrape_result: list[dict]           = []

# Estado de notificaciones: {"{slot}_{type}": seconds_last_seen}
_timer_state: dict[str, int] = {}
_warned:      set[str]       = set()


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


def _scrape_settactics_sync(user_id: str, slot_index: int, kwargs: dict) -> dict:
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
            result = set_tactics_for_slot(page, slot_index, **kwargs)
            context.close()
            browser.close()
            return result
    except Exception as e:
        print(f"❌ Error en scrape de tácticas: {e}")
        return {"success": False, "changed": [], "errors": [str(e)]}
    finally:
        conn.close()


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
            sched_at = task["scheduled_at"].replace(tzinfo=None)
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
    team   = slot.get("team_name", "Equipo")
    league = slot.get("league_name", "")
    timers = slot.get("timers", [])

    embed = discord.Embed(
        title=f"⏱  Timers — {team}",
        description=f"Liga: **{league}**",
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
        # Para unknowns con countdown activo, etiquetar genéricamente
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

    for slot in slots:
        slot_idx = slot["slot_index"]
        team     = slot["team_name"]
        for timer in slot["timers"]:
            typ      = timer["type"]
            seconds  = timer["seconds"]
            is_ready = timer["is_ready"]
            emoji    = timer["emoji"]
            label    = timer["label_es"]
            key      = f"{slot_idx}_{typ}"
            prev     = _timer_state.get(key, seconds + 1)

            # Timer expiró: estaba corriendo y ahora está listo
            done_key = f"{key}_done"
            if is_ready and prev > 60 and done_key not in _warned:
                await channel.send(f"✅ **{emoji} {label}** listo en **{team}**!")
                _warned.add(done_key)

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
    async def btn_timers(self, interaction: discord.Interaction, button: discord.ui.Button):
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


class TacticsConfirmView(discord.ui.View):
    def __init__(self, slot_index: int, kwargs: dict):
        super().__init__(timeout=60)
        self.slot_index = slot_index
        self.kwargs     = kwargs

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("No autorizado.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await asyncio.to_thread(
            _scrape_settactics_sync, OSM_USER_ID, self.slot_index, self.kwargs
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


# ── GUARD ─────────────────────────────────────────────────────────────────────

def _is_owner(interaction: discord.Interaction) -> bool:
    return DISCORD_OWNER_ID == 0 or interaction.user.id == DISCORD_OWNER_ID


# ── AUTOCOMPLETE Y CHOICES ───────────────────────────────────────────────────

async def _slot_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Devuelve los equipos activos como opciones para el parámetro slot."""
    try:
        leagues = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
    except Exception:
        return []
    return [
        app_commands.Choice(name=lg["league_name"][:100], value=str(i))
        for i, lg in enumerate(leagues[:4])
        if not current or current.lower() in lg["league_name"].lower()
    ][:25]


_GAMEPLAN_CHOICES = [
    app_commands.Choice(name="Normal",     value="Normal"),
    app_commands.Choice(name="Attacking",  value="Attacking"),
    app_commands.Choice(name="Defensive",  value="Defensive"),
    app_commands.Choice(name="Counter",    value="Counter"),
    app_commands.Choice(name="Long Ball",  value="Long Ball"),
    app_commands.Choice(name="Possession", value="Possession"),
]
_TACKLING_CHOICES = [
    app_commands.Choice(name="Easy",       value="Easy"),
    app_commands.Choice(name="Normal",     value="Normal"),
    app_commands.Choice(name="Hard",       value="Hard"),
    app_commands.Choice(name="Aggressive", value="Aggressive"),
]
_MARKING_CHOICES = [
    app_commands.Choice(name="Zonal",      value="Zonal"),
    app_commands.Choice(name="Man-to-man", value="Man-to-man"),
]
_FORMATION_CHOICES = [
    app_commands.Choice(name="4-4-2",   value="4-4-2"),
    app_commands.Choice(name="4-3-3",   value="4-3-3"),
    app_commands.Choice(name="4-5-1",   value="4-5-1"),
    app_commands.Choice(name="3-5-2",   value="3-5-2"),
    app_commands.Choice(name="5-3-2",   value="5-3-2"),
    app_commands.Choice(name="4-2-3-1", value="4-2-3-1"),
    app_commands.Choice(name="4-1-4-1", value="4-1-4-1"),
    app_commands.Choice(name="3-4-3",   value="3-4-3"),
    app_commands.Choice(name="5-4-1",   value="5-4-1"),
    app_commands.Choice(name="4-4-1-1", value="4-4-1-1"),
    app_commands.Choice(name="3-6-1",   value="3-6-1"),
]


def _slot_idx(slot_str: str, leagues: list[dict]) -> int | None:
    """Convierte el valor string del autocomplete a índice validado."""
    try:
        idx = int(slot_str)
    except (ValueError, TypeError):
        return None
    return idx if 0 <= idx < len(leagues) else None


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
    slot      = "Selecciona tu equipo",
    gameplan  = "Plan de juego",
    tackling  = "Tipo de entrada",
    formation = "Formación",
    pressure  = "Presión (0-100)",
    mentality = "Mentalidad (0-100)",
    tempo     = "Tempo (0-100)",
    marking   = "Marcaje",
)
@app_commands.autocomplete(slot=_slot_autocomplete)
@app_commands.choices(
    gameplan  = _GAMEPLAN_CHOICES,
    tackling  = _TACKLING_CHOICES,
    formation = _FORMATION_CHOICES,
    marking   = _MARKING_CHOICES,
)
async def cmd_settactics(
    interaction : discord.Interaction,
    slot        : str           = "0",
    gameplan    : Optional[str] = None,
    tackling    : Optional[str] = None,
    formation   : Optional[str] = None,
    pressure    : Optional[int] = None,
    mentality   : Optional[int] = None,
    tempo       : Optional[int] = None,
    marking     : Optional[str] = None,
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
    if gameplan:              kwargs["game_plan"] = gameplan
    if tackling:              kwargs["tackling"]  = tackling
    if formation:             kwargs["formation"] = formation
    if pressure  is not None: kwargs["pressure"]  = pressure
    if mentality is not None: kwargs["mentality"] = mentality
    if tempo     is not None: kwargs["tempo"]     = tempo
    if marking:               kwargs["marking"]   = marking

    if not kwargs:
        await interaction.response.send_message(
            "Selecciona al menos un parámetro a cambiar.", ephemeral=True
        )
        return

    try:
        leagues   = await asyncio.to_thread(_get_active_leagues, OSM_USER_ID)
        idx       = _slot_idx(slot, leagues)
        slot_name = leagues[idx]["league_name"] if idx is not None else "Equipo"
    except Exception:
        idx, slot_name = 0, "Equipo"

    if idx is None:
        await interaction.response.send_message("Equipo no encontrado.", ephemeral=True)
        return

    _LABEL_ES = {
        "game_plan": "Plan de juego", "tackling": "Tackling", "formation": "Formación",
        "pressure": "Presión", "mentality": "Mentalidad", "tempo": "Tempo", "marking": "Marcaje",
    }
    lines = [f"• **{_LABEL_ES.get(k, k)}**: `{v}`" for k, v in kwargs.items()]
    embed = discord.Embed(
        title       = f"⚙️ Cambiar Tácticas — {slot_name}",
        description = "¿Confirmas los siguientes cambios?\n\n" + "\n".join(lines),
        color       = OSM_COLOR,
    )
    await interaction.response.send_message(
        embed     = embed,
        view      = TacticsConfirmView(slot_index=idx, kwargs=kwargs),
        ephemeral = True,
    )


# ── EVENTOS ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
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
        print(f"🔔 Alertas activadas → canal {DISCORD_ALERT_CHANNEL_ID} cada {TIMER_CHECK_MINUTES}m (aviso a {TIMER_WARNING_MINUTES}m)")
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
