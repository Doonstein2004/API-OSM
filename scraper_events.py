# scraper_events.py
"""
Lee el calendario de eventos del foro de OSM y devuelve eventos estructurados.
URL: https://forum.onlinesoccermanager.com/topic/67089/monthly-weekend-events-schedule

Eventos relevantes para la automatización:
  training  → Extreme Training, Superfast Trainer, Training Talents, etc.
              (timers de entrenamiento reducidos, ej. 2h en vez de 8h)
  stadium   → Booming Stadium
              (timers de estadio reducidos, ej. 4h en vez de 18h)
"""
import re
import time as _time
from datetime import datetime, timezone

import requests

EVENTS_URL = "https://forum.onlinesoccermanager.com/topic/67089/monthly-weekend-events-schedule"
_USER_AGENT = "Mozilla/5.0 (compatible; OSMBot/1.0)"

# Duración reducida (horas) que cada tipo de evento aplica a las acciones automáticas
EVENT_REDUCED_HOURS: dict[str, float] = {
    "training": 2.0,
    "stadium":  4.0,
}

# Clasificación de eventos por palabras clave en el nombre
_TYPE_KEYWORDS: list[tuple[list[str], str]] = [
    (["extreme training", "superfast trainer", "training talent",
      "guaranteed training", "intense friendl", "training boost",
      "entrenamiento"], "training"),
    (["booming stadium", "stadium boost", "estadio"], "stadium"),
    (["transfer madness", "transfer"], "transfer"),
    (["golden oldies", "legends", "legend"], "legends"),
    (["friendl"], "training"),  # Intense Friendlies también da bonus de training
]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # Español
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# ── Caché en memoria ──────────────────────────────────────────────────────────
_cache_events:    list[dict] = []
_cache_fetched_at: float     = 0.0
CACHE_SECONDS = 6 * 3600  # re-fetch cada 6 horas


def _classify(name: str) -> str:
    n = name.lower()
    for keywords, etype in _TYPE_KEYWORDS:
        if any(k in n for k in keywords):
            return etype
    return "generic"


def _parse_date_str(date_str: str, ref_year: int) -> tuple[datetime | None, datetime | None]:
    """
    Convierte strings tipo 'June 3', 'June 6-7', 'June 13 - 14' a (start_dt, end_dt) UTC.
    Asume que el evento empieza a las 00:00 UTC y termina a las 23:59 UTC del último día.
    """
    date_str = date_str.strip()
    m = re.match(
        r'([A-Za-záéíóúñ]+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})|'
        r'([A-Za-záéíóúñ]+)\s+(\d{1,2})',
        date_str, re.IGNORECASE,
    )
    if not m:
        return None, None

    if m.group(1):  # Range: "June 6-7"
        month_name = m.group(1).lower()
        start_day  = int(m.group(2))
        end_day    = int(m.group(3))
    else:           # Single: "June 3"
        month_name = m.group(4).lower()
        start_day  = int(m.group(5))
        end_day    = start_day

    month_num = _MONTHS.get(month_name)
    if not month_num:
        return None, None

    try:
        start = datetime(ref_year, month_num, start_day, 0, 0, 0, tzinfo=timezone.utc)
        end   = datetime(ref_year, month_num, end_day, 23, 59, 59, tzinfo=timezone.utc)
        return start, end
    except ValueError:
        return None, None


def _extract_reduced_hours(descriptions: list[str], event_type: str) -> float | None:
    """Lee las horas reducidas de las descripciones del evento."""
    for desc in descriptions:
        d = desc.lower()
        # "2H normal trainers", "Shorter Training timers: 2H", "Shorter Stadium timers (4H)"
        m = re.search(r'(\d+(?:\.\d+)?)\s*h\b', d)
        if m:
            return float(m.group(1))
    return EVENT_REDUCED_HOURS.get(event_type)


def _parse_html(html: str, now: datetime) -> list[dict]:
    """
    Estructura real del foro OSM:
      <h4><strong>OSM Events schedule: MONTH YEAR</strong></h4>
      <ul>
        <li><strong>DD - DD MONTH:</strong> Event Name
          <ul><li>description</li>...</ul>
        </li>
        ...
      </ul>
    """
    TAG_STRIP = re.compile(r'<[^>]+>')
    events = []

    # ── 1. Encontrar el año en el encabezado ──────────────────────────────────
    year_m = re.search(
        r'OSM\s+Events\s+schedule[^<]*?(\d{4})', html, re.IGNORECASE
    )
    year = int(year_m.group(1)) if year_m else now.year

    # ── 2. Extraer el bloque <ul> principal que sigue al encabezado ───────────
    # Buscar la primera lista después del título "OSM Events schedule"
    title_idx = html.lower().find('osm events schedule')
    if title_idx < 0:
        return []
    ul_start = html.find('<ul>', title_idx)
    if ul_start < 0:
        return []

    # Encontrar el </ul> de cierre del mismo nivel
    depth = 0
    ul_end = ul_start
    i = ul_start
    while i < len(html):
        tag = re.match(r'<(/?)ul\b', html[i:], re.I)
        if tag:
            if tag.group(1) == '/':
                depth -= 1
                if depth == 0:
                    ul_end = i + html[i:].index('>') + 1
                    break
            else:
                depth += 1
        i += 1

    block = html[ul_start:ul_end]

    # ── 3. Parsear cada <li> de primer nivel ─────────────────────────────────
    # Patrón: <strong>DATE:</strong> EVENT NAME
    # Fecha: "03 June" o "06 - 07 June" (día primero, luego mes)
    date_name_re = re.compile(
        r'<strong>\s*([\d\s\-–]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)'
        r'\s*:?\s*</strong>\s*(.*?)(?=<ul>|</li>)',
        re.DOTALL | re.IGNORECASE,
    )

    # Extraer también las descripciones del <ul> anidado
    nested_ul_re = re.compile(r'<ul>(.*?)</ul>', re.DOTALL | re.IGNORECASE)
    li_content_re = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL | re.IGNORECASE)

    # Iterar los <li> de primer nivel del bloque
    # Los <li> de primer nivel contienen <strong>DATE</strong> + nombre + <ul> nested
    top_li_re = re.compile(r'<li>(.*?)</li>(?=\s*(?:<li>|</ul>))', re.DOTALL | re.IGNORECASE)
    for li_m in top_li_re.finditer(block):
        li_content = li_m.group(1)

        # Buscar fecha y nombre en el <strong> de primer nivel
        dn_m = date_name_re.search(li_content)
        if not dn_m:
            continue

        raw_date = dn_m.group(1).strip()
        raw_name = TAG_STRIP.sub('', dn_m.group(2)).strip()
        if not raw_name:
            # Nombre podría estar después del </strong>
            after_strong = li_content[dn_m.end():]
            raw_name = TAG_STRIP.sub('', after_strong.split('<')[0]).strip()

        # Extraer descripciones del <ul> anidado
        descriptions = []
        nested_m = nested_ul_re.search(li_content)
        if nested_m:
            for desc_m in li_content_re.finditer(nested_m.group(1)):
                desc = TAG_STRIP.sub('', desc_m.group(1)).strip()
                if desc:
                    descriptions.append(desc)

        # Parsear la fecha: "03 June" o "06 - 07 June"
        # Normalizar: extraer número(s) de día y nombre de mes
        date_norm = re.sub(r'\s+', ' ', raw_date).strip()
        # Formatos: "03 June" / "06 - 07 June" / "06-07 June"
        date_m = re.match(
            r'(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)|'  # range: 06-07 June
            r'(\d{1,2})\s+([A-Za-z]+)',                        # single: 03 June
            date_norm, re.IGNORECASE,
        )
        if not date_m:
            continue

        if date_m.group(1):  # range
            start_day  = int(date_m.group(1))
            end_day    = int(date_m.group(2))
            month_name = date_m.group(3).lower()
        else:               # single
            start_day  = int(date_m.group(4))
            end_day    = start_day
            month_name = date_m.group(5).lower()

        month_num = _MONTHS.get(month_name[:3]) or _MONTHS.get(month_name)
        if not month_num:
            continue

        try:
            start_dt = datetime(year, month_num, start_day, 0,  0,  0, tzinfo=timezone.utc)
            end_dt   = datetime(year, month_num, end_day,  23, 59, 59, tzinfo=timezone.utc)
        except ValueError:
            continue

        event_type = _classify(raw_name)
        reduced    = _extract_reduced_hours(descriptions, event_type)
        if reduced is not None:
            EVENT_REDUCED_HOURS[event_type] = reduced  # actualizar con el valor real

        ev = _make_event(raw_name, " | ".join(descriptions), start_dt, end_dt, now)
        if ev:
            ev["reduced_hours"] = reduced
            events.append(ev)

    return events


def _make_event(name: str, description: str, start_dt: datetime,
                end_dt: datetime, now: datetime) -> dict | None:
    if not name or not start_dt:
        return None
    secs_to_start = int((start_dt - now).total_seconds())
    secs_to_end   = int((end_dt   - now).total_seconds())
    is_active     = secs_to_start <= 0 <= secs_to_end
    event_type    = _classify(name)
    return {
        "name":          name,
        "type":          event_type,
        "description":   description,
        "start_dt":      start_dt.isoformat(),
        "end_dt":        end_dt.isoformat(),
        "seconds_until_start": max(0, secs_to_start),
        "seconds_until_end":   max(0, secs_to_end),
        "is_active":     is_active,
        "reduced_hours": EVENT_REDUCED_HOURS.get(event_type),
    }


def fetch_events(force: bool = False) -> list[dict]:
    """
    Devuelve la lista de eventos del mes actual, ordenados por fecha.
    Usa caché de 6 horas para no saturar el foro.
    """
    global _cache_events, _cache_fetched_at
    now = datetime.now(timezone.utc)

    if not force and _cache_fetched_at and (_time.time() - _cache_fetched_at) < CACHE_SECONDS:
        return _cache_events

    print(f"  [events] Obteniendo calendario de eventos...")
    try:
        resp = requests.get(
            EVENTS_URL, timeout=15,
            headers={"User-Agent": _USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"  ⚠️ [events] fetch_events error: {e}")
        return _cache_events  # devolver caché anterior si hay

    events = _parse_html(html, now)
    # Ordenar por fecha de inicio
    events.sort(key=lambda e: e["seconds_until_start"])
    _cache_events    = events
    _cache_fetched_at = _time.time()
    print(f"  [events] {len(events)} eventos encontrados")
    return events


def get_upcoming_bonus_events(event_type: str, within_hours: float = 2.0) -> list[dict]:
    """
    Devuelve eventos del tipo indicado que empiezan en menos de `within_hours` horas,
    o que están activos ahora. Excluye eventos ya terminados.
    """
    threshold = within_hours * 3600
    events = fetch_events()
    return [
        ev for ev in events
        if ev["type"] == event_type
        and ev.get("seconds_until_end", 0) > 0   # excluir eventos ya terminados
        and (ev["is_active"] or ev["seconds_until_start"] <= threshold)
    ]


def get_active_event(event_type: str) -> dict | None:
    """Devuelve el evento activo del tipo indicado, si lo hay."""
    for ev in fetch_events():
        if ev["type"] == event_type and ev["is_active"]:
            return ev
    return None


def format_events_for_discord(events: list[dict]) -> str:
    """Formatea la lista de eventos para mostrar en Discord."""
    if not events:
        return "No hay eventos próximos disponibles."

    type_emoji = {
        "training": "💪",
        "stadium":  "🏟️",
        "transfer": "💰",
        "legends":  "⭐",
        "generic":  "🎮",
    }
    lines = []
    for ev in events:
        emoji = type_emoji.get(ev["type"], "🎮")
        name  = ev["name"]
        reduced = f" *(timers: {ev['reduced_hours']:.0f}h)*" if ev.get("reduced_hours") else ""
        if ev["is_active"]:
            remaining = ev["seconds_until_end"]
            h, m = divmod(remaining // 60, 60)
            lines.append(f"{emoji} **{name}**{reduced} — 🟢 Activo (termina en {h}h {m}m)")
        else:
            secs = ev["seconds_until_start"]
            if secs < 3600:
                when = f"en {secs // 60}m"
            elif secs < 86400:
                h, m = divmod(secs // 60, 60)
                when = f"en {h}h {m}m"
            else:
                days = secs // 86400
                hrs  = (secs % 86400) // 3600
                when = f"en {days}d {hrs}h"
            desc = ev.get("description", "")
            desc_short = f" — _{desc[:60]}_" if desc else ""
            lines.append(f"{emoji} **{name}**{reduced} — {when}{desc_short}")
    return "\n".join(lines)
