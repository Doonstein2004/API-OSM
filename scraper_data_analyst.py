# scraper_data_analyst.py
"""
Accede a la herramienta Data Analyst de OSM (/DataAnalist).

Flujo completo:
  1. get_data_analyst_state(page)   → lee próximo rival + estado del spy
  2. start_spy(page)                → inicia el espionaje (timer ~1h)
  3. get_spy_results(page)          → lee resultados una vez que el timer terminó

Datos del spy (cuando está disponible):
  - Tácticas del rival (formación, plan de juego, sliders)
  - Plantilla del rival (jugadores con stats)
  - Historial de partidos recientes (últimos 5)
"""
import time
from playwright.sync_api import Page
from utils import handle_popups

DATA_ANALYST_URL = "https://en.onlinesoccermanager.com/DataAnalist"


def _loaded(page: Page, timeout: int = 12000) -> bool:
    for sel in ["#spy-team-list", "[data-bind*='nextOpponentTeamPartial']",
                "[data-bind*='teamsPartial']"]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate(page: Page) -> bool:
    for sel in ["a[href='/DataAnalist']", "a[href*='DataAnalist']",
                "a:has-text('Data Analyst')", "a:has-text('Analysis')"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _loaded(page):
                    return True
        except Exception:
            pass

    try:
        page.goto(DATA_ANALYST_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        handle_popups(page)
        return _loaded(page)
    except Exception as e:
        print(f"  ⚠️ No se pudo navegar a DataAnalist: {e}")
        return False


# ── LECTURA DE ESTADO ─────────────────────────────────────────────────────────

def get_data_analyst_state(page: Page) -> dict:
    """
    Lee el estado actual de la página /DataAnalist via KO.js.

    Returns:
    {
        "next_opponent": { name, manager_name, has_spy_running, spy_done },
        "teams": [{ name, manager_name, has_spy_running, spy_done }],
        "active_spy_team": str | None,   # equipo siendo espiado ahora
    }
    """
    if not _navigate(page):
        return {"next_opponent": None, "teams": [], "active_spy_team": None,
                "error": "page_not_loaded"}

    time.sleep(1)
    handle_popups(page)

    try:
        return page.evaluate("""
            () => {
                function v(obs) { return typeof obs === 'function' ? obs() : obs; }
                function safeB(obs) { try { return !!v(obs); } catch(e) { return false; } }
                function teamInfoKO(t) {
                    if (!t) return null;
                    const mgr = v(t.managerPartial);
                    const spy = v(t.spyInstructionPartial);
                    return {
                        name:               v(t.name) || '',
                        manager_name:       mgr ? (v(mgr.name) || '') : '',
                        has_spy_running:    safeB(t.hasOngoingSpyInstruction),
                        spy_done:           !!spy,
                        on_secret_training: safeB(t.onSecretTraining),
                    };
                }

                // Buscar root KO con múltiples estrategias
                function findKORoot() {
                    const selectors = [
                        '#spy-team-list',
                        '[data-bind*="nextOpponentTeam"]',
                        '[data-bind*="teamsPartial"]',
                        '[data-bind*="spyTeam"]',
                        '[data-bind*="activeSpyTeam"]',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const ctx = ko.contextFor(el);
                        if (!ctx) continue;
                        const r = ctx.$root || ctx.$data;
                        if (r && (typeof r.nextOpponentTeamPartial !== 'undefined' ||
                                  typeof r.teamsPartial !== 'undefined')) return r;
                    }
                    const bodyCtx = ko.contextFor(document.body);
                    return bodyCtx ? (bodyCtx.$root || bodyCtx.$data) : null;
                }
                const root = findKORoot();

                let nextOppInfo = null;
                let activeSpyName = null;
                let teams = [];

                if (root) {
                    const nextOppKO = v(root.nextOpponentTeamPartial);
                    if (nextOppKO) nextOppInfo = teamInfoKO(nextOppKO);

                    const activeSpy = v(root.activeSpyTeam);
                    if (activeSpy) activeSpyName = v(activeSpy.name) || null;

                    const teamsP = v(root.teamsPartial);
                    if (teamsP && typeof teamsP.getOrderedBySpy === 'function') {
                        teams = teamsP.getOrderedBySpy().map(teamInfoKO).filter(Boolean);
                    }
                }

                // Fallback DOM: leer el próximo rival directamente del HTML
                // El próximo rival está en .row-grid-fix-top (primer bloque grande)
                // Los demás equipos están en .row-grid-fix-bottom (grilla de miniaturas)
                if (!nextOppInfo || !nextOppInfo.name) {
                    const topRow = document.querySelector(
                        '#spy-team-list .row-grid-fix-top'
                    );
                    if (topRow) {
                        const nameEl = topRow.querySelector('.club-name');
                        const mgrEl  = topRow.querySelector(
                            '.club-text-container-managername, [data-bind*="managerPartial"] [data-bind*="name"]'
                        );
                        const panel  = topRow.querySelector('.panel.clickable');
                        const spyRunning = !panel; // si no hay panel clickable, el spy está en curso
                        if (nameEl && nameEl.innerText.trim()) {
                            nextOppInfo = {
                                name:               nameEl.innerText.trim(),
                                manager_name:       mgrEl ? mgrEl.innerText.trim() : '',
                                has_spy_running:    spyRunning,
                                spy_done:           false,
                                on_secret_training: false,
                                _source:            'dom',
                            };
                        }
                    }
                }

                // Fallback DOM para los equipos de la liga (grilla inferior)
                if (!teams.length) {
                    const bottomPanels = document.querySelectorAll(
                        '#spy-team-list .row-grid-fix-bottom .panel.clickable'
                    );
                    bottomPanels.forEach(panel => {
                        const nameEl = panel.querySelector('.club-name');
                        const mgrEl  = panel.querySelector('[data-bind*="name"]');
                        if (nameEl) {
                            teams.push({
                                name:               nameEl.innerText.trim(),
                                manager_name:       mgrEl ? mgrEl.innerText.trim() : '',
                                has_spy_running:    false,
                                spy_done:           false,
                                on_secret_training: false,
                                _source:            'dom',
                            });
                        }
                    });
                }

                return {
                    next_opponent:   nextOppInfo,
                    teams:           teams,
                    active_spy_team: activeSpyName,
                };
            }
        """) or {"next_opponent": None, "teams": [], "active_spy_team": None}
    except Exception as e:
        print(f"  ⚠️ get_data_analyst_state: {e}")
        return {"next_opponent": None, "teams": [], "active_spy_team": None,
                "error": str(e)}


# ── INICIAR ESPIONAJE ─────────────────────────────────────────────────────────

def start_spy(page: Page, team_name: str | None = None) -> dict:
    """
    Inicia el espionaje del próximo rival (o del equipo especificado en team_name).
    Navega a /DataAnalist, hace clic en el equipo y confirma el modal.

    Returns: { started, team_name, cost, error? }
    """
    if not _navigate(page):
        return {"started": False, "team_name": team_name, "cost": 0,
                "error": "page_not_loaded"}

    time.sleep(1)
    handle_popups(page)

    # Si team_name no fue provisto, necesitamos leer el estado para descubrirlo.
    # Cuando viene de spy_for_slot con team_name ya conocido, saltamos la llamada redundante.
    if not team_name:
        state = get_data_analyst_state(page)
        active = state.get("active_spy_team")
        if active:
            print(f"  ℹ️ Spy ya activo en '{active}' — no se puede iniciar otro")
            return {"started": False, "team_name": active, "cost": 0,
                    "error": f"spy_already_active:{active}"}
        next_opp = state.get("next_opponent")
        if not next_opp:
            return {"started": False, "team_name": None, "cost": 0,
                    "error": "no_next_opponent"}
        team_name = next_opp["name"]

    print(f"  → Iniciando spy en: {team_name!r}")

    # Hacer clic en el equipo via KO spyTeam()
    try:
        clicked = page.evaluate(f"""
            (function() {{
                const root = ko.contextFor(document.body)?.$root;
                if (!root) return null;
                function v(obs) {{ return typeof obs === 'function' ? obs() : obs; }}
                const target = {repr(team_name.lower())};

                // Intentar con nextOpponentTeamPartial primero
                const nextOpp = v(root.nextOpponentTeamPartial);
                if (nextOpp && (v(nextOpp.name) || '').toLowerCase() === target) {{
                    if (!root.activeSpyTeam()) {{
                        root.spyTeam(nextOpp);
                        return v(nextOpp.name);
                    }}
                }}

                // Buscar en teamsPartial
                const teamsP = v(root.teamsPartial);
                if (teamsP && typeof teamsP.getOrderedBySpy === 'function') {{
                    for (const t of teamsP.getOrderedBySpy()) {{
                        const n = (v(t.name) || '').toLowerCase();
                        if (n === target || n.includes(target) || target.includes(n)) {{
                            if (!root.activeSpyTeam()) {{
                                root.spyTeam(t);
                                return v(t.name);
                            }}
                        }}
                    }}
                }}

                // DOM fallback: click en el panel del equipo
                const panels = document.querySelectorAll('.panel.theme-stepover-0.clickable');
                for (const panel of panels) {{
                    const nameEl = panel.querySelector('.club-name');
                    if (nameEl && nameEl.innerText.trim().toLowerCase().includes(target)) {{
                        panel.click();
                        return nameEl.innerText.trim();
                    }}
                }}
                return null;
            }})()
        """)
        if not clicked:
            return {"started": False, "team_name": team_name, "cost": 0,
                    "error": "team_not_found_or_spy_active"}
        print(f"  ✓ Clic en equipo: {clicked!r}")
        time.sleep(2)
    except Exception as e:
        return {"started": False, "team_name": team_name, "cost": 0,
                "error": f"click_failed:{e}"}

    # Confirmar en el modal: click en okAction o en el botón "Start"
    try:
        confirmed = page.evaluate("""
            (function() {
                // Intentar via KO okAction en el modal
                const modal = document.querySelector('.modal.in, .modal[style*="display: block"]');
                if (modal) {
                    try {
                        const ctx = ko.contextFor(modal);
                        const vm  = ctx && (ctx.$data || ctx.$root);
                        if (vm && typeof vm.okAction === 'function') {
                            const cost = typeof vm.spyClubFundsCost === 'function'
                                ? vm.spyClubFundsCost() : 0;
                            vm.okAction();
                            return { ok: true, cost };
                        }
                    } catch(e) {}
                }
                // DOM fallback: click en botón "Start" del modal
                const btn = document.querySelector('.modal .btn-primary, .modal .btn-new.btn-primary');
                if (btn && btn.offsetParent) {
                    btn.click();
                    return { ok: true, cost: 0 };
                }
                return { ok: false, cost: 0 };
            })()
        """)
        if confirmed and confirmed.get("ok"):
            print(f"  ✓ Spy iniciado — timer ~1h")
            time.sleep(1)
            handle_popups(page)
            return {"started": True, "team_name": team_name,
                    "cost": confirmed.get("cost", 0)}
        else:
            return {"started": False, "team_name": team_name, "cost": 0,
                    "error": "modal_confirm_failed"}
    except Exception as e:
        return {"started": False, "team_name": team_name, "cost": 0,
                "error": f"confirm_failed:{e}"}


# ── LEER RESULTADOS DEL SPY ───────────────────────────────────────────────────

def get_spy_results(page: Page, team_name: str | None = None) -> dict:
    """
    Lee los resultados del espionaje una vez que el timer terminó.
    Si team_name es None, lee el primer equipo con spy completado.

    Returns:
    {
        "team_name":   str,
        "manager":     str,
        "tactics":     { formation, game_plan, tackling, pressure, mentality, tempo, ... },
        "squad":       [ { name, specific_position, stat_att, stat_def, stat_ovr, age } ],
        "last_matches": [ { round, opponent, result, score } ],
        "error":       str | None,
    }
    """
    if not _navigate(page):
        return {"team_name": None, "tactics": {}, "squad": [], "last_matches": [],
                "error": "page_not_loaded"}

    time.sleep(1)
    handle_popups(page)

    try:
        return page.evaluate(f"""
            (function() {{
                const root = ko.contextFor(document.body)?.$root;
                if (!root) return {{ team_name: null, error: 'no_root' }};

                function v(obs) {{ return typeof obs === 'function' ? obs() : obs; }}
                function safeBool(obs) {{ try {{ return !!v(obs); }} catch(e) {{ return false; }} }}

                const target = {repr((team_name or '').lower())};

                // Encontrar el equipo con spy completado
                let spyInstruction = null;
                let spyTeamName    = null;
                let managerName    = null;

                function checkTeam(t) {{
                    if (!t) return false;
                    const spy = v(t.spyInstructionPartial);
                    if (!spy) return false;
                    const n = v(t.name) || '';
                    if (!target || n.toLowerCase().includes(target) || target.includes(n.toLowerCase())) {{
                        spyInstruction = spy;
                        spyTeamName    = n;
                        const mgr = v(t.managerPartial);
                        managerName = mgr ? (v(mgr.name) || '') : '';
                        return true;
                    }}
                    return false;
                }}

                // Primero: next opponent
                if (checkTeam(v(root.nextOpponentTeamPartial))) {{}}
                else {{
                    // Buscar en todos los equipos
                    const teamsP = v(root.teamsPartial);
                    if (teamsP && typeof teamsP.getOrderedBySpy === 'function') {{
                        for (const t of teamsP.getOrderedBySpy()) {{
                            if (checkTeam(t)) break;
                        }}
                    }}
                }}

                if (!spyInstruction) {{
                    return {{ team_name: spyTeamName, error: 'spy_not_done_or_not_found',
                              tactics: {{}}, squad: [], last_matches: [] }};
                }}

                // ── Tácticas del rival ──────────────────────────────────────
                const tactics = {{}};
                try {{
                    const tp = v(spyInstruction.tacticsPartial) || v(spyInstruction.tactics);
                    if (tp) {{
                        tactics.game_plan  = v(tp.gamePlan)   || v(tp.game_plan)  || '';
                        tactics.tackling   = v(tp.tackling)   || '';
                        tactics.pressure   = v(tp.pressure)   || v(tp.tacticPressure)  || 50;
                        tactics.mentality  = v(tp.mentality)  || v(tp.tacticMentality) || 50;
                        tactics.tempo      = v(tp.tempo)      || v(tp.tacticTempo)     || 50;
                        tactics.marking    = v(tp.marking)    || '';
                        tactics.formation  = v(tp.formation)  || v(tp.formationName)   || '';
                        tactics.fwd        = v(tp.forwardsTactic)    || v(tp.lineTacticAtt) || '';
                        tactics.mid        = v(tp.midfieldersTactic) || v(tp.lineTacticMid) || '';
                        tactics.def        = v(tp.defendersTactic)   || v(tp.lineTacticDef) || '';
                        tactics.offside    = safeBool(tp.offsideTrap);
                    }}
                }} catch(e) {{}}

                // ── Plantilla del rival ─────────────────────────────────────
                const squad = [];
                try {{
                    const pp = v(spyInstruction.playersGroupablePartial) ||
                               v(spyInstruction.squadPartial);
                    if (pp) {{
                        const getGroups = pp.getPlayers || pp.getOrderedGroups;
                        const groups = typeof getGroups === 'function' ? getGroups.call(pp) : [];
                        for (const group of groups) {{
                            const items = typeof group.players.getItems === 'function'
                                ? group.players.getItems() : [];
                            for (const p of items) {{
                                squad.push({{
                                    name:              v(p.name) || '',
                                    specific_position: '',  // requires PlayerSpecificPosition enum
                                    stat_att:          v(p.statAtt) || 0,
                                    stat_def:          v(p.statDef) || 0,
                                    stat_ovr:          v(p.statOvr) || 0,
                                    age:               v(p.age) || 0,
                                    in_lineup:         safeBool(p.isInLineup),
                                }});
                            }}
                        }}
                    }}
                }} catch(e) {{}}

                // ── Últimos partidos ────────────────────────────────────────
                const lastMatches = [];
                try {{
                    const results = v(spyInstruction.recentResults) ||
                                    v(spyInstruction.matchResults)  ||
                                    v(spyInstruction.lastResults);
                    if (Array.isArray(results)) {{
                        for (const r of results.slice(0, 5)) {{
                            lastMatches.push({{
                                round:    v(r.round) || v(r.matchday) || '',
                                opponent: v(r.opponentName) || v(r.opponent) || '',
                                score:    v(r.score) || '',
                                result:   v(r.result) || '',
                                home:     !!v(r.isHome),
                            }});
                        }}
                    }}
                }} catch(e) {{}}

                return {{
                    team_name:    spyTeamName,
                    manager:      managerName,
                    tactics:      tactics,
                    squad:        squad,
                    last_matches: lastMatches,
                    error:        null,
                }};
            }})()
        """) or {"team_name": None, "tactics": {}, "squad": [], "last_matches": [],
                 "error": "evaluation_failed"}
    except Exception as e:
        print(f"  ⚠️ get_spy_results: {e}")
        return {"team_name": team_name, "tactics": {}, "squad": [], "last_matches": [],
                "error": str(e)}


# ── ÚLTIMOS PARTIDOS DEL RIVAL (desde la página de resultados de la liga) ────

def get_opponent_recent_matches(page: Page, opponent_name: str, limit: int = 5) -> list[dict]:
    """
    Navega a /League/Results y extrae los últimos `limit` partidos en los que
    aparece `opponent_name` como local o visitante.

    No depende del spy — funciona inmediatamente con datos de la liga.

    Returns: [{ round, home_team, away_team, score, result_for_opponent, is_home }]
    """
    RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"

    # Intentar navegar via SPA primero
    for sel in ["a[href*='League/Results']", "a[href*='/Results']"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                try:
                    page.wait_for_selector("table.table-sticky", timeout=8000)
                    break
                except Exception:
                    pass
        except Exception:
            pass
    else:
        try:
            page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            page.wait_for_selector("table.table-sticky", timeout=10000)
        except Exception as e:
            print(f"  ⚠️ get_opponent_recent_matches: no se pudo cargar resultados: {e}")
            return []

    time.sleep(1)
    handle_popups(page)

    try:
        matches = page.evaluate(f"""
            (function() {{
                const target = {repr(opponent_name.lower())};
                const results = [];

                // Buscar en todas las tablas de resultados (puede haber varias por jornada)
                const rows = document.querySelectorAll('table.table-sticky tr:not(.thead)');
                for (const row of rows) {{
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (cells.length < 3) continue;

                    // Estructura típica: [round/jornada] [home_team] [score] [away_team]
                    // o [home_team] [score] [away_team] dependiendo de la vista
                    const texts = cells.map(c => c.innerText.trim());

                    // Identificar score (contiene -)
                    let scoreIdx = texts.findIndex(t => /^\\d+\\s*-\\s*\\d+$/.test(t));
                    if (scoreIdx === -1) scoreIdx = texts.findIndex(t => /^\\d+-\\d+$/.test(t));
                    if (scoreIdx === -1) continue;

                    const homeTeam  = texts[scoreIdx - 1] || '';
                    const awayTeam  = texts[scoreIdx + 1] || '';
                    const score     = texts[scoreIdx];
                    const roundText = texts[0] !== homeTeam ? texts[0] : '';

                    const homeL = homeTeam.toLowerCase();
                    const awayL = awayTeam.toLowerCase();

                    if (!homeL.includes(target) && !awayL.includes(target)) continue;

                    const isHome   = homeL.includes(target);
                    const myGoals  = isHome ? parseInt(score.split('-')[0]) : parseInt(score.split('-')[1]);
                    const oooGoals = isHome ? parseInt(score.split('-')[1]) : parseInt(score.split('-')[0]);
                    const result   = myGoals > oooGoals ? 'W' : myGoals < oooGoals ? 'L' : 'D';

                    results.push({{
                        round:      roundText,
                        home_team:  homeTeam,
                        away_team:  awayTeam,
                        score:      score,
                        is_home:    isHome,
                        result_for_opponent: result,
                        my_goals:   myGoals,
                        opp_goals:  oooGoals,
                    }});
                }}
                return results;
            }})()
        """) or []

        # Los resultados suelen estar en orden cronológico inverso (más recientes primero)
        recent = matches[:limit]
        print(f"  ✓ {len(recent)} partido(s) de {opponent_name!r} encontrados")
        return recent

    except Exception as e:
        print(f"  ⚠️ get_opponent_recent_matches eval: {e}")
        return []


# ── ORQUESTADOR: START + RESULTADOS EN UN SOLO SLOT ──────────────────────────

def spy_for_slot(
    page: Page,
    league_name: str,
    read_results_if_done: bool = True,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
) -> dict:
    """
    Activa el slot de la liga indicada y:
      - Si no hay spy activo → inicia spy en el próximo rival
      - Si el spy ya terminó → lee los resultados
      - Si el spy está en curso → devuelve estado con timer pendiente

    Returns:
    {
        "action":      "started" | "results" | "in_progress" | "already_active",
        "team_name":   str,
        "spy_result":  dict | None,    # solo si action == "results"
        "start_result": dict | None,   # solo si action == "started"
    }
    """
    from utils import click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    slots = page.locator(".career-teamslot")
    target_idx = None
    for i in range(slots.count()):
        _, t_league, _ = get_slot_info(slots.nth(i))
        if t_league and league_name.lower() in t_league.lower():
            target_idx = i
            print(f"  ✓ Slot encontrado: DOM {i} → '{t_league}'")
            break

    if target_idx is None:
        return {"action": "error", "team_name": None,
                "error": f"slot_not_found:{league_name}"}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"action": "error", "team_name": None,
                "error": "slot_activation_failed"}

    # Leer estado del DataAnalyst
    state = get_data_analyst_state(page)
    if state.get("error"):
        return {"action": "error", "team_name": None,
                "error": state["error"]}

    next_opp    = state.get("next_opponent") or {}
    active_spy  = state.get("active_spy_team")
    opp_name    = next_opp.get("name", "")

    # Siempre incluimos los últimos partidos del rival desde la página de resultados
    # (esto funciona sin spy, inmediatamente)
    last_matches = []
    if opp_name:
        last_matches = get_opponent_recent_matches(page, opp_name)

    # ¿Hay resultados de spy disponibles?
    if read_results_if_done:
        for team in [next_opp] + state.get("teams", []):
            if team and team.get("spy_done"):
                result = get_spy_results(page, team.get("name"))
                if not result.get("error"):
                    # Enriquecer con los últimos partidos del resultado de resultados de liga
                    if last_matches and not result.get("last_matches"):
                        result["last_matches"] = last_matches
                    return {"action": "results", "team_name": team["name"],
                            "spy_result": result, "last_matches": last_matches}

    # ¿Hay un spy en curso?
    if active_spy:
        return {"action": "in_progress", "team_name": active_spy,
                "last_matches": last_matches}

    # ¿El próximo rival ya tiene spy en ejecución?
    if next_opp.get("has_spy_running"):
        return {"action": "in_progress", "team_name": opp_name,
                "last_matches": last_matches}

    # Sin rival conocido no podemos iniciar spy
    if not opp_name:
        return {"action": "error", "team_name": None,
                "error": "no_next_opponent", "last_matches": last_matches}

    # Iniciar spy en el próximo rival
    start_result = start_spy(page, opp_name)
    if start_result["started"]:
        return {"action": "started", "team_name": opp_name,
                "start_result": start_result, "last_matches": last_matches}
    return {"action": "error", "team_name": opp_name,
            "error": start_result.get("error"), "last_matches": last_matches}
