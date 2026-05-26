# scraper_timers.py
"""
Lee todos los timers activos del dashboard de OSM para cada slot.
Timers: Próximo partido, Entrenamiento, Ojeador, Médico, Abogado, Estadio,
        Predicción, Recompensa diaria, Evento.
"""
import time
from playwright.sync_api import Page
from utils import handle_popups, click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info
from scraper_next_match import parse_countdown, extract_next_match_from_dashboard


# Mapeo de palabras clave (texto visible + clases CSS + data-bind) → tipo canónico
_KEYWORD_MAP = {
    # Texto visible — entrenadores (OSM muestra el tipo de coach, no "training")
    "attacking coach":      "training",
    "defending coach":      "training",
    "midfielder coach":     "training",
    "goalkeeping coach":    "training",
    "training":             "training",
    "entrenamiento":        "training",
    # Scout
    "scout":                "scout",
    "ojeador":              "scout",
    # Médico
    "medical":              "medical",
    "médico":               "medical",
    "doctor":               "medical",
    # Abogado
    "lawyer":               "lawyer",
    "abogado":              "lawyer",
    # Estadio
    "stadium":              "stadium",
    "estadio":              "stadium",
    # Próximo partido
    "next match":           "next_match",
    "próximo partido":      "next_match",
    # Transferencia
    "transfer":             "transfer",
    "transferencia":        "transfer",
    # Predicción de partido
    "predict":              "match_prediction",
    "match prediction":     "match_prediction",
    "predicción":           "match_prediction",
    # Recompensa diaria
    "login reward":         "daily_login",
    "next login":           "daily_login",
    "recompensa":           "daily_login",
    # Evento
    "world 2026":           "event",
    # Clases CSS / ids de OSM
    "icon-training":        "training",
    "icon-timer-scout":     "scout",
    "icon-scout":           "scout",
    "icon-team":            "training",
    "icon-medical":         "medical",
    "icon-lawyer":          "lawyer",
    "icon-stadium":         "stadium",
    "icon-match":           "next_match",
    "next-match":           "next_match",
    "icon-bosscoin":        "daily_login",
    "icon-matchprediction": "match_prediction",
    "event-timer":          "event",
    # data-bind de Knockout.js
    "trainingtimer":        "training",
    "scouttimer":           "scout",
    "medicaltimer":         "medical",
    "lawyertimer":          "lawyer",
    "stadiumtimer":         "stadium",
    "secondsremaining":     "next_match",
}

TIMER_EMOJI = {
    "training":         "💪",
    "scout":            "🔍",
    "medical":          "⚕️",
    "lawyer":           "⚖️",
    "stadium":          "🏟️",
    "next_match":       "⚽",
    "transfer":         "💰",
    "match_prediction": "🎯",
    "daily_login":      "🎁",
    "event":            "🌍",
    "unknown":          "⏱️",
}

TIMER_LABEL_ES = {
    "training":         "Entrenamiento",
    "scout":            "Ojeador",
    "medical":          "Médico",
    "lawyer":           "Abogado",
    "stadium":          "Estadio",
    "next_match":       "Próximo partido",
    "transfer":         "Transferencia",
    "match_prediction": "Predicción",
    "daily_login":      "Recompensa diaria",
    "event":            "Evento",
    "unknown":          "Timer",
}


def _classify(text: str, meta: str = "") -> str:
    combined = (text + " " + meta).lower()
    for kw, typ in _KEYWORD_MAP.items():
        if kw in combined:
            return typ
    return "unknown"


def _extract_countdown(text: str) -> tuple[str, int]:
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(u in line.lower() for u in ["d ", "h ", "m ", "s", "day", "hour", "min"]):
            secs = parse_countdown(line)
            if secs > 0:
                return line, secs
    if any(w in text.lower() for w in ["ready", "listo", "disponible", "available"]):
        return "Listo", 0
    return "", 0


def _build_timer(raw_text: str, meta: str = "") -> dict:
    typ = _classify(raw_text, meta)
    countdown, seconds = _extract_countdown(raw_text)

    label = ""
    for line in raw_text.split("\n"):
        line = line.strip()
        if line and not any(u in line.lower() for u in ["d ", "h ", "m ", "s"]):
            label = line
            break
    if not label:
        label = TIMER_LABEL_ES.get(typ, "")

    is_ready = (seconds == 0 and countdown.lower() in ("listo", "ready", "disponible", ""))

    return {
        "type":      typ,
        "label":     label,
        "label_es":  TIMER_LABEL_ES.get(typ, label or "Timer"),
        "emoji":     TIMER_EMOJI.get(typ, "⏱️"),
        "countdown": countdown or ("Listo" if is_ready else "N/A"),
        "seconds":   seconds,
        "is_ready":  is_ready,
    }


def _deduplicate(timers: list[dict]) -> list[dict]:
    """Un timer por tipo. Si hay varios del mismo tipo, prefiere el de mayor countdown."""
    seen: dict[str, dict] = {}
    for t in sorted(timers, key=lambda x: x["seconds"], reverse=True):
        if t["type"] not in seen:
            seen[t["type"]] = t
    return list(seen.values())


# ── EXTRACCIÓN PRINCIPAL ──────────────────────────────────────────────────────

def get_all_timers_for_slot(page: Page) -> list[dict]:
    """
    Lee el dropdown #timers del dashboard actual y devuelve todos los timers.
    Asume que el browser ya está en el dashboard del equipo.
    """
    timers = []

    try:
        handle_popups(page)

        btn = page.locator("#timers")
        if btn.count() == 0:
            print("  ⚠️ #timers no encontrado — usando fallback de próximo partido")
            return _fallback_next_match(page)

        try:
            btn.first.scroll_into_view_if_needed()
            btn.first.click(force=True, timeout=5000)
        except Exception as e:
            print(f"  ⚠️ No se pudo abrir #timers: {e}")
            return _fallback_next_match(page)

        time.sleep(1.5)

        raw_items = page.evaluate("""
            () => {
                const btn = document.querySelector('#timers');
                if (!btn) return [];

                // Localizar el dropdown-menu abierto
                let menu = null;
                const parent = btn.closest('.dropdown, li.dropdown, .btn-group');
                if (parent) menu = parent.querySelector('.dropdown-menu');
                if (!menu) {
                    for (const m of document.querySelectorAll('.dropdown-menu')) {
                        const s = window.getComputedStyle(m);
                        if (s.display !== 'none' && s.visibility !== 'hidden' && m.offsetParent) {
                            menu = m; break;
                        }
                    }
                }
                if (!menu) return [];

                const items = [];

                // 1. Próximo partido — contenedor especial fuera de la lista
                const nm = menu.querySelector('.next-match-container, .nextround-timer');
                if (nm) {
                    const cdSpan = nm.querySelector('span[data-bind*="secondsRemaining"], span.timer-time');
                    if (cdSpan && cdSpan.innerText.trim()) {
                        items.push({
                            text: 'Next match ' + cdSpan.innerText.trim(),
                            meta: 'next-match secondsremaining'
                        });
                    }
                }

                // 2. Lista de timers activos — solo ul.hidden-xs para evitar duplicados con visible-xs
                const listUl = menu.querySelector('ul.hidden-xs') || menu.querySelector('ul:not(.visible-xs)');
                if (listUl) {
                    listUl.querySelectorAll('li.border.clickable, li.clickable').forEach(li => {
                        const titleSpan = li.querySelector('span[data-bind*="title"], span[data-bind*="text: title"]');
                        const timerSpan = li.querySelector('span.timer-time, span[data-bind*="secondsRemaining"]');
                        const iconSpan  = li.querySelector('span[class*="icon-"]');
                        const title     = titleSpan ? titleSpan.innerText.trim() : '';
                        const countdown = timerSpan  ? timerSpan.innerText.trim()  : '';
                        const iconClass = iconSpan   ? iconSpan.className           : '';
                        if (title || countdown) {
                            items.push({
                                text: (title + ' ' + countdown).trim(),
                                meta: iconClass.toLowerCase()
                            });
                        }
                    });
                }

                // 3. Timers de evento (p. ej. "World 2026 is coming")
                menu.querySelectorAll('.event-timer').forEach(el => {
                    const timerSpan = el.querySelector('.timer-time, span[data-bind*="secondsRemaining"]');
                    const titleEl   = el.querySelector('.title, .bold');
                    if (timerSpan && timerSpan.innerText.trim()) {
                        items.push({
                            text: (titleEl ? titleEl.innerText.trim() + ' ' : '') + timerSpan.innerText.trim(),
                            meta: 'event-timer'
                        });
                    }
                });

                // 4. Timers inline fuera de <li> y fuera de .event-timer:
                //    Daily Login Reward, Match Prediction
                menu.querySelectorAll('.row.timer').forEach(row => {
                    if (row.closest('.event-timer') || row.closest('li.border') || row.closest('.nextround-timer')) return;
                    const timerSpan = row.querySelector('span.timer-time, span[data-bind*="secondsRemaining"]');
                    const iconSpan  = row.querySelector('span[class*="icon-"]');
                    const boldDiv   = row.querySelector('.bold');
                    if (timerSpan && timerSpan.innerText.trim()) {
                        items.push({
                            text: (boldDiv ? boldDiv.innerText.trim() + ' ' : '') + timerSpan.innerText.trim(),
                            meta: iconSpan ? iconSpan.className.toLowerCase() : ''
                        });
                    }
                });

                // 5. Fallback: si todavía no hay nada, barrer todos los spans con countdown visibles
                if (items.length === 0) {
                    const sel = [
                        'span[data-bind*="secondsRemaining"]',
                        'span.timer-time',
                        'span[data-bind*="countdown"]',
                    ].join(',');
                    menu.querySelectorAll(sel).forEach(span => {
                        if (!span.offsetParent) return;
                        const container = span.closest(
                            'li, .timer-item, .timer-row, [class*="timer"], [class*="next-match"]'
                        ) || span.parentElement;
                        const text = container ? container.innerText.trim() : span.innerText.trim();
                        const meta = (
                            (container ? container.className : '') + ' ' +
                            (span.getAttribute('data-bind') || '')
                        ).toLowerCase();
                        if (text) items.push({ text, meta });
                    });
                }

                return items;
            }
        """)

        for item in raw_items:
            t = _build_timer(item.get("text", ""), item.get("meta", ""))
            timers.append(t)

        if timers:
            timers = _deduplicate(timers)

        if not timers:
            print("  ⚠️ Sin timers — usando fallback de próximo partido")
            timers = _fallback_next_match(page)

        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            pass

        print(f"  ✓ {len(timers)} timer(s) leídos")

    except Exception as e:
        print(f"  ❌ Error en get_all_timers_for_slot: {e}")

    return timers


def _fallback_next_match(page: Page) -> list[dict]:
    try:
        info = extract_next_match_from_dashboard(page)
        if info.get("countdown_text"):
            return [{
                "type":      "next_match",
                "label":     "Próximo partido",
                "label_es":  "Próximo partido",
                "emoji":     "⚽",
                "countdown": info["countdown_text"],
                "seconds":   info.get("seconds_remaining", 0),
                "is_ready":  False,
            }]
    except Exception as e:
        print(f"  ⚠️ Fallback también falló: {e}")
    return []


# ── ORQUESTADOR POR SLOTS ─────────────────────────────────────────────────────

def get_timers_all_slots(page: Page, num_slots: int = 4) -> list[dict]:
    """
    Itera los slots de carrera y extrae los timers de cada uno.

    Returns:
        list de dicts: slot_index, team_name, league_name, timers: list[dict]
    """
    results = []
    CAREER_URL = "https://en.onlinesoccermanager.com/Career"

    for i in range(num_slots):
        print(f"\n--- Slot #{i + 1}: Leyendo timers ---")

        try:
            if not page.url.endswith("/Career"):
                page.goto(CAREER_URL, wait_until="domcontentloaded", timeout=30000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=30000)

            if not wait_for_visible_slots(page, timeout=20000):
                print(f"  ❌ No se encontraron slots. Saltando.")
                continue
            time.sleep(1)
        except Exception as nav_err:
            print(f"  ⚠️ Error navegando a Career: {nav_err}")
            continue

        handle_popups(page)

        # Esperar a que el slot i sea visible — OSM carga los slots de forma progresiva
        slots = page.locator(".career-teamslot")
        for _wait in range(8):
            if slots.count() > i:
                break
            time.sleep(1)

        if slots.count() <= i:
            print(f"  ℹ️ Slot #{i + 1} no existe. Fin.")
            break

        team_name, league_name = get_slot_info(slots.nth(i))
        if not team_name:
            print(f"  ℹ️ Slot #{i + 1} vacío o no disponible. Saltando.")
            continue

        if not click_slot_and_wait_for_dashboard(page, i):
            print(f"  ❌ No se pudo activar el slot {i + 1}.")
            continue

        # Forzar carga completa del Dashboard para que KO.js inicialice todos los bindings.
        # La navegación SPA (click en slot) deja la página en estado parcial donde el
        # dropdown #timers abre pero retorna vacío.
        DASHBOARD_URL = "https://en.onlinesoccermanager.com/Dashboard"
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#timers", timeout=15000)
            handle_popups(page)
            time.sleep(1)
        except Exception as dash_err:
            print(f"  ⚠️ No se pudo forzar /Dashboard: {dash_err}")

        slot_timers = get_all_timers_for_slot(page)
        results.append({
            "slot_index":  i,
            "team_name":   team_name,
            "league_name": league_name,
            "timers":      slot_timers,
        })

    print(f"\n✅ Timers extraídos de {len(results)} slot(s).")
    return results
