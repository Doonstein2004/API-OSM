# scraper_squad.py
"""
Extrae la plantilla completa del equipo desde /Squad para cada slot de carrera.
Campos por jugador: nombre, número, posición, edad, nacionalidad, att/def/ovr,
                    fitness, morale, goles, valor, estado (lineup/entreno/lesión/etc.)
"""
import time
from playwright.sync_api import Page
from utils import handle_popups, click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

SQUAD_URL = "https://en.onlinesoccermanager.com/Squad"


def _squad_loaded(page: Page, timeout: int = 12000) -> bool:
    for sel in ["#squad-table", "[data-bind*='playersGroupablePartial']"]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate_to_squad(page: Page) -> bool:
    for sel in ["a[href='/Squad']", "a[href*='/Squad']",
                "a:has-text('Squad')", "a:has-text('Equipo')"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _squad_loaded(page):
                    print(f"  ✓ Squad cargado vía SPA ({sel})")
                    return True
        except Exception:
            pass

    print("  → Fallback: page.goto(/Squad)...")
    try:
        page.goto(SQUAD_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        handle_popups(page)
        if _squad_loaded(page):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")
    return False


def _extract_players_ko(page: Page) -> list[dict]:
    """Extrae todos los jugadores via KO.js viewmodel (fuente de verdad)."""
    try:
        return page.evaluate("""
            () => {
                const squadEl = document.querySelector('#squad-table') ||
                                document.querySelector('.tab-pane#squad');
                if (!squadEl) return [];

                const ctx = ko.contextFor(squadEl);
                if (!ctx) return [];
                const root = ctx.$root || ctx.$data;
                if (!root) return [];

                const groupable = typeof root.playersGroupablePartial === 'function'
                    ? root.playersGroupablePartial() : root.playersGroupablePartial;
                if (!groupable || typeof groupable.getPlayers !== 'function') return [];

                const PP  = window.PlayerPosition         || {};
                const PSP = window.PlayerSpecificPosition || {};

                function posKey(val) {
                    if (PP.A !== undefined && val === PP.A) return 'A';
                    if (PP.M !== undefined && val === PP.M) return 'M';
                    if (PP.D !== undefined && val === PP.D) return 'D';
                    if (PP.G !== undefined && val === PP.G) return 'G';
                    return String(val);
                }
                function specLabel(val) {
                    if (PSP.GK  !== undefined && val === PSP.GK)  return 'GK';
                    if (PSP.RB  !== undefined && val === PSP.RB)  return 'RB';
                    if (PSP.CB  !== undefined && val === PSP.CB)  return 'CB';
                    if (PSP.LB  !== undefined && val === PSP.LB)  return 'LB';
                    if (PSP.RM  !== undefined && val === PSP.RM)  return 'RM';
                    if (PSP.CDM !== undefined && val === PSP.CDM) return 'CDM';
                    if (PSP.CM  !== undefined && val === PSP.CM)  return 'CM';
                    if (PSP.CAM !== undefined && val === PSP.CAM) return 'CAM';
                    if (PSP.LM  !== undefined && val === PSP.LM)  return 'LM';
                    if (PSP.RF  !== undefined && val === PSP.RF)  return 'RW';
                    if (PSP.ST  !== undefined && val === PSP.ST)  return 'ST';
                    if (PSP.LF  !== undefined && val === PSP.LF)  return 'LW';
                    return '';
                }
                function v(obs) { return typeof obs === 'function' ? obs() : obs; }
                // Algunos métodos KO pueden fallar en ciertos jugadores (ej: isWorldStar llama
                // a getPlayerWorldStarLevel que no siempre existe). Usamos try/catch por flag.
                function safeBool(obs) { try { return !!v(obs); } catch(e) { return false; } }

                const players = [];
                for (const group of groupable.getPlayers()) {
                    const pos = posKey(group.position);
                    const items = typeof group.players.getItems === 'function'
                        ? group.players.getItems() : [];
                    for (const p of items) {
                        const nat = v(p.nationality) || {};
                        players.push({
                            name:              v(p.name)              || '',
                            squad_number:      v(p.squadOrLineupNumber) ?? '',
                            position:          pos,
                            specific_position: specLabel(p.specificPosition),
                            age:               v(p.age)              || 0,
                            nationality_code:  (v(nat.code) || '').toLowerCase(),
                            nationality_name:  v(nat.name) || '',
                            stat_att:          v(p.statAtt)           || 0,
                            stat_def:          v(p.statDef)           || 0,
                            stat_ovr:          v(p.statOvr)           || 0,
                            fitness:           v(p.fitness)           || 0,
                            morale:            v(p.morale)            || 0,
                            goals:             v(p.goals)             || 0,
                            value:             v(p.value)             || 0,
                            in_lineup:         safeBool(p.isInLineup),
                            in_selection:      safeBool(p.isInSelection),
                            in_training:       safeBool(p.isInTraining),
                            is_injured:        safeBool(p.isInjured),
                            is_suspended:      safeBool(p.isSuspended),
                            is_in_form:        safeBool(p.isInForm),
                            is_world_star:     safeBool(p.isWorldStar),
                            is_legend:         safeBool(p.isLegend),
                            yellow_cards:      v(p.yellowCards) || 0,
                        });
                    }
                }
                return players;
            }
        """) or []
    except Exception as e:
        print(f"  ⚠️ _extract_players_ko: {e}")
        return []


def _extract_players_dom(page: Page) -> list[dict]:
    """Fallback DOM-based extraction cuando KO viewmodel no está disponible."""
    try:
        return page.evaluate(r"""
            () => {
                const players = [];
                const table = document.querySelector('#squad-table');
                if (!table) return [];

                const posMap = {
                    'forwards': 'A', 'midfielders': 'M',
                    'defenders': 'D', 'goalkeepers': 'G'
                };
                let currentPos = '';

                for (const section of table.querySelectorAll('thead, tbody')) {
                    if (section.tagName === 'THEAD') {
                        const th = section.querySelector('th');
                        if (th) currentPos = posMap[th.innerText.trim().toLowerCase()] || currentPos;
                        continue;
                    }
                    for (const row of section.querySelectorAll('tr.player-table-row')) {
                        const nameEl = row.querySelector('span[data-bind="text: name"]');
                        if (!nameEl) continue;

                        const shirtEl  = row.querySelector('.icon-shirt span');
                        const squadNum = shirtEl ? shirtEl.innerText.trim() : '';

                        const posTd   = row.querySelector('td.text-right');
                        const specPos = posTd ? posTd.innerText.trim() : '';

                        const ageTd = row.querySelector('td[data-bind*="age"]');
                        const age   = ageTd ? parseInt(ageTd.innerText) || 0 : 0;

                        const natSpan = row.querySelector('span[class*="flag-icon-"]');
                        let natCode = '';
                        if (natSpan) {
                            const m = natSpan.className.match(/flag-icon-([a-z]{2,3})/);
                            natCode = m ? m[1] : '';
                        }

                        function statFromBind(key) {
                            const el = row.querySelector('td[data-bind*="' + key + '"]');
                            return el ? parseInt(el.innerText) || 0 : 0;
                        }
                        function progressPct(td) {
                            if (!td) return 0;
                            const bar = td.querySelector('.progress-bar');
                            if (!bar) return 0;
                            const title = bar.getAttribute('title') || '';
                            const m = title.match(/(\d+)/);
                            return m ? parseInt(m[1]) : 0;
                        }

                        const fitTd   = row.querySelector('td[data-bind*="fitness"]');
                        const morTd   = row.querySelector('td[data-bind*="morale"]');
                        const goalsTd = row.querySelector('td[data-bind*="goals"]');
                        const valueEl = row.querySelector('.club-funds-amount');

                        const shirtDiv   = row.querySelector('.icon-shirt');
                        const inLineup   = shirtDiv ? shirtDiv.classList.contains('icon-shirt-blue') : false;
                        const inTraining = shirtDiv ? shirtDiv.classList.contains('icon-shirt-training') : false;
                        const inSel      = shirtDiv ? (shirtDiv.classList.contains('icon-shirt-grey') && !inLineup) : false;

                        players.push({
                            name:              nameEl.innerText.trim(),
                            squad_number:      squadNum,
                            position:          currentPos,
                            specific_position: specPos,
                            age,
                            nationality_code:  natCode,
                            nationality_name:  '',
                            stat_att:          statFromBind('statAtt'),
                            stat_def:          statFromBind('statDef'),
                            stat_ovr:          statFromBind('statOvr'),
                            fitness:           progressPct(fitTd),
                            morale:            progressPct(morTd),
                            goals:             goalsTd ? parseInt(goalsTd.innerText) || 0 : 0,
                            value:             valueEl  ? valueEl.innerText.trim() : '',
                            in_lineup:         inLineup,
                            in_selection:      inSel,
                            in_training:       inTraining,
                            is_injured:        !!row.querySelector('.icon-player-injured'),
                            is_suspended:      !!row.querySelector('.icon-player-suspension'),
                            is_in_form:        row.classList.contains('in-form-table-row'),
                            is_world_star:     row.classList.contains('world-star-table-row'),
                            is_legend:         row.classList.contains('legend-table-row'),
                            yellow_cards:      row.querySelectorAll('.icon-player-yellowcard').length,
                        });
                    }
                }
                return players;
            }
        """) or []
    except Exception as e:
        print(f"  ⚠️ _extract_players_dom: {e}")
        return []


def get_squad(page: Page) -> list[dict]:
    """
    Lee la plantilla completa desde /Squad.
    Asume que el browser ya está en el contexto del equipo correcto (slot activado).
    """
    if not _navigate_to_squad(page):
        print("  ❌ No se pudo cargar /Squad")
        return []

    time.sleep(1.5)
    handle_popups(page)

    try:
        page.wait_for_selector("#squad-table tr.player-table-row", timeout=10000)
    except Exception:
        print("  ⚠️ Tabla de jugadores tardó en cargar, continuando...")

    players = _extract_players_ko(page)
    if not players:
        print("  ⚠️ KO extract vacío — usando DOM fallback")
        players = _extract_players_dom(page)

    print(f"  ✓ {len(players)} jugadores extraídos")
    return players


def get_squad_for_slot(
    page: Page,
    league_name: str,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
) -> dict:
    """
    Activa el slot de la liga indicada y devuelve la plantilla.
    Análogo a renew_training_for_slot.
    Returns: { slot_index, team_name, league_name, matchday, players, error? }
    """
    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    slots = page.locator(".career-teamslot")
    count = slots.count()
    target_idx  = None
    target_team = ""
    target_md   = None

    for i in range(count):
        t_name, t_league, t_md = get_slot_info(slots.nth(i))
        if t_league and league_name.lower() in t_league.lower():
            target_idx  = i
            target_team = t_name or ""
            target_md   = t_md
            print(f"  ✓ Slot encontrado: DOM {i} → '{t_league}'")
            break

    if target_idx is None:
        return {"slot_index": -1, "team_name": "", "league_name": league_name,
                "matchday": None, "players": [], "error": f"slot_not_found:{league_name}"}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"slot_index": target_idx, "team_name": target_team,
                "league_name": league_name, "matchday": target_md,
                "players": [], "error": "slot_activation_failed"}

    players = get_squad(page)
    return {
        "slot_index":  target_idx,
        "team_name":   target_team,
        "league_name": league_name,
        "matchday":    target_md,
        "players":     players,
    }


def get_squad_all_slots(page: Page, num_slots: int = 4) -> list[dict]:
    """
    Itera los slots de carrera y extrae la plantilla de cada equipo.
    Returns: list de dicts con { slot_index, team_name, league_name, matchday, players }
    """
    results = []
    CAREER_URL = "https://en.onlinesoccermanager.com/Career"

    for i in range(num_slots):
        print(f"\n--- Slot #{i + 1}: Leyendo plantilla ---")

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

        slots = page.locator(".career-teamslot")
        for _ in range(8):
            if slots.count() > i:
                break
            time.sleep(1)

        if slots.count() <= i:
            print(f"  ℹ️ Slot #{i + 1} no existe. Fin.")
            break

        team_name, league_name, matchday = get_slot_info(slots.nth(i))
        if not team_name:
            print(f"  ℹ️ Slot #{i + 1} vacío o no disponible. Saltando.")
            continue

        if not click_slot_and_wait_for_dashboard(page, i):
            print(f"  ❌ No se pudo activar el slot {i + 1}.")
            continue

        players = get_squad(page)
        results.append({
            "slot_index":  i,
            "team_name":   team_name,
            "league_name": league_name,
            "matchday":    matchday,
            "players":     players,
        })

    print(f"\n✅ Plantillas extraídas de {len(results)} slot(s).")
    return results
