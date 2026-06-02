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
    for sel in [
        "#spy-team-list",
        "[data-bind*='nextOpponentTeamPartial']",
        "[data-bind*='teamsPartial']",
        "[data-bind*='spyInstructionPartial']",
        "[data-bind*='spyInstruction']",
        "[data-bind*='tacticsPartial']",
        "[data-bind*='activeSpyTeam']",
        "[data-bind*='hasOngoingSpyInstruction']",
        "[data-bind*='claimSpyInstruction']",   # spy completado sin reclamar
        ".countdowntimer-panel",                # panel del timer de spy
    ]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            print(f"  ✓ DataAnalist cargado ({sel})")
            return True
        except Exception:
            pass
    # URL fallback: si la URL es correcta, esperar KO bindings genéricos
    try:
        if "DataAnalist" in page.url or "dataanalist" in page.url.lower():
            print(f"  ℹ️ DataAnalist URL ok — esperando KO bindings...")
            time.sleep(3)
            try:
                page.wait_for_selector("[data-bind]", timeout=6000, state="attached")
                print(f"  ✓ DataAnalist KO bindings detectados")
            except Exception:
                print(f"  ⚠️ DataAnalist sin KO bindings — aceptando por URL")
            return True
    except Exception:
        pass
    return False


def _navigate(page: Page) -> bool:
    try:
        if "DataAnalist" in page.url or "dataanalist" in page.url.lower():
            if _loaded(page, timeout=3000):
                return True
    except Exception:
        pass

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
        print(f"  → Navegando a {DATA_ANALYST_URL}...")
        page.goto(DATA_ANALYST_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        handle_popups(page)
        loaded = _loaded(page)
        print(f"  → _loaded={loaded}  url={page.url}")
        return loaded
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
        result = page.evaluate("""
            () => {
                function v(obs) { return typeof obs === 'function' ? obs() : obs; }
                function safeB(obs) { try { return !!v(obs); } catch(e) { return false; } }

                function teamInfoFromItem(item) {
                    if (!item) return null;
                    const name = v(item.name) || '';
                    if (!name) return null;
                    const mgr = v(item.managerPartial);
                    const spy = v(item.spyInstructionPartial);
                    return {
                        name,
                        manager_name:       mgr ? (v(mgr.name) || '') : '',
                        has_spy_running:    safeB(item.hasOngoingSpyInstruction),
                        spy_done:           !!spy,
                        on_secret_training: safeB(item.onSecretTraining),
                    };
                }

                // Estrategia A: ko.dataFor directo sobre elementos con bindings de equipo.
                // No depende del root viewmodel — más robusto cuando ko.contextFor(body) falla.
                const seenNames = new Set();
                const teams = [];
                let nextOppInfo = null;
                let activeSpyName = null;

                const teamSelectors = [
                    '[data-bind*="hasOngoingSpyInstruction"]',
                    '[data-bind*="spyInstructionPartial"]',
                    '[data-bind*="nextOpponentTeamPartial"]',
                    '#spy-team-list [data-bind*="name"]',
                ];
                for (const sel of teamSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        try {
                            const item = ko.dataFor(el);
                            if (!item) return;
                            const info = teamInfoFromItem(item);
                            if (!info || seenNames.has(info.name)) return;
                            seenNames.add(info.name);
                            teams.push(info);
                            if (info.has_spy_running) activeSpyName = info.name;
                        } catch(e) {}
                    });
                    if (teams.length > 0) break;
                }

                // Estrategia B: root viewmodel como segundo intento
                if (!teams.length || !nextOppInfo) {
                    try {
                        const spyEl = document.querySelector('#spy-team-list') || document.body;
                        const ctx   = ko.contextFor(spyEl);
                        const root  = ctx && (ctx.$root || ctx.$data);
                        if (root) {
                            const nextOppKO = v(root.nextOpponentTeamPartial);
                            if (nextOppKO) {
                                const info = teamInfoFromItem(nextOppKO);
                                if (info && info.name) nextOppInfo = info;
                            }
                            if (!activeSpyName) {
                                const as = v(root.activeSpyTeam);
                                if (as) activeSpyName = v(as.name) || null;
                            }
                            if (!teams.length) {
                                const teamsP = v(root.teamsPartial);
                                if (teamsP && typeof teamsP.getOrderedBySpy === 'function') {
                                    teamsP.getOrderedBySpy().forEach(t => {
                                        const info = teamInfoFromItem(t);
                                        if (info && !seenNames.has(info.name)) {
                                            seenNames.add(info.name);
                                            teams.push(info);
                                        }
                                    });
                                }
                            }
                        }
                    } catch(e) {}
                }

                // Si hay equipos de la estrategia A pero no nextOppInfo, inferir
                if (!nextOppInfo && teams.length > 0) {
                    nextOppInfo = teams.find(t => t.has_spy_running || t.spy_done) || teams[0];
                }

                // Fallback DOM
                if (!nextOppInfo || !nextOppInfo.name) {
                    const topRow = document.querySelector('#spy-team-list .row-grid-fix-top');
                    if (topRow) {
                        const nameEl = topRow.querySelector('.club-name');
                        if (nameEl && nameEl.innerText.trim()) {
                            nextOppInfo = {
                                name:            nameEl.innerText.trim(),
                                manager_name:    '',
                                has_spy_running: !topRow.querySelector('.panel.clickable'),
                                spy_done:        false, _source: 'dom',
                            };
                        }
                    }
                }
                if (!activeSpyName) {
                    const clubEl = document.querySelector(
                        '#spy-team-list .club-name, .dataanalist-spy-header .club-name'
                    );
                    if (clubEl && clubEl.innerText.trim()) activeSpyName = clubEl.innerText.trim();
                }
                if (!teams.length) {
                    document.querySelectorAll('#spy-team-list .row-grid-fix-bottom .panel.clickable').forEach(p => {
                        const n = p.querySelector('.club-name');
                        if (n) teams.push({ name: n.innerText.trim(), manager_name: '',
                                            has_spy_running: false, spy_done: false, _source: 'dom' });
                    });
                }

                // Detectar botón "Complete" del spy terminado-sin-reclamar
                let spyNeedsClaim = false;
                const claimBtn = document.querySelector('button[data-bind*="claimSpyInstruction"]');
                if (claimBtn && claimBtn.offsetParent !== null) {
                    spyNeedsClaim = true;
                    // Obtener nombre del equipo via contexto KO del botón
                    try {
                        const ctx = ko.contextFor(claimBtn);
                        if (ctx && ctx.$parents && ctx.$parents.length > 1) {
                            const teamObj = ctx.$parents[1];
                            const tname = v(teamObj.name) || v(teamObj.teamName) || '';
                            if (tname && !activeSpyName) activeSpyName = tname;
                        }
                    } catch(e) {}
                }

                return { next_opponent: nextOppInfo, teams, active_spy_team: activeSpyName,
                         spy_needs_claim: spyNeedsClaim };
            }
        """) or {"next_opponent": None, "teams": [], "active_spy_team": None, "spy_needs_claim": False}
        nxt = result.get("next_opponent") or {}
        print(f"  → DataAnalist state: next='{nxt.get('name','')}' spy_done={nxt.get('spy_done')} active='{result.get('active_spy_team')}' teams={len(result.get('teams',[]))}")
        # Diagnóstico cuando no se encuentra nada — muestra qué bindings hay en la página
        if not result.get("teams") and not nxt.get("name"):
            try:
                diag = page.evaluate("""
                    () => {
                        const els = Array.from(document.querySelectorAll('[data-bind]'));
                        const binds = els.map(e => e.getAttribute('data-bind').trim().slice(0,90)).slice(0, 12);
                        const koKeys = [];
                        let i = 0;
                        for (const el of els) {
                            if (i++ > 20) break;
                            try {
                                const d = ko.dataFor(el);
                                if (d && typeof d === 'object') {
                                    const keys = Object.keys(d).join(',');
                                    if (keys && !koKeys.includes(keys)) koKeys.push(keys.slice(0,120));
                                }
                            } catch(e) {}
                        }
                        return { bindCount: els.length, firstBinds: binds, koKeysets: koKeys.slice(0,4) };
                    }
                """) or {}
                print(f"  🔍 [diag] bindCount={diag.get('bindCount')} firstBinds={diag.get('firstBinds')} koKeysets={diag.get('koKeysets')}")
            except Exception as de:
                print(f"  🔍 [diag error] {de}")
        return result
    except Exception as e:
        print(f"  ⚠️ get_data_analyst_state: {e}")
        return {"next_opponent": None, "teams": [], "active_spy_team": None,
                "error": str(e)}


# ── RECLAMAR SPY COMPLETADO ───────────────────────────────────────────────────

def _claim_spy(page: Page) -> bool:
    """
    Clickea el botón 'Complete' cuando el timer del spy terminó pero aún no fue reclamado.
    Retorna True si se hizo clic con éxito.
    """
    try:
        btn = page.locator('button[data-bind*="claimSpyInstruction"]').first
        if btn.count() == 0 or not btn.is_visible(timeout=2000):
            return False
        print("  → Reclamando spy completado (botón Complete)...")
        btn.click()
        time.sleep(3)
        handle_popups(page)
        print("  ✓ Spy reclamado")
        return True
    except Exception as e:
        print(f"  ⚠️ _claim_spy: {e}")
        return False


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

    # Spy terminado pero sin reclamar → clickear "Complete" primero
    if state.get("spy_needs_claim"):
        team_name_claim = state.get("active_spy_team") or ""
        if _claim_spy(page):
            # Re-leer estado después de reclamar
            state = get_data_analyst_state(page)
            if not state.get("active_spy_team") and team_name_claim:
                state["active_spy_team"] = team_name_claim

    next_opp    = state.get("next_opponent") or {}
    active_spy  = state.get("active_spy_team")
    opp_name    = next_opp.get("name", "")

    # ¿Hay resultados de spy disponibles?
    if read_results_if_done:
        # Si el spy fue reclamado, buscar resultados por el nombre guardado
        candidates = ([next_opp] + state.get("teams", []))
        if active_spy and not any(t.get("name") == active_spy for t in candidates if t):
            candidates.append({"name": active_spy, "spy_done": True})
        for team in candidates:
            if team and team.get("spy_done"):
                result = get_spy_results(page, team.get("name"))
                if not result.get("error"):
                    return {"action": "results", "team_name": team["name"],
                            "spy_result": result}

    # ¿Hay un spy en curso?
    if active_spy:
        return {"action": "in_progress", "team_name": active_spy}

    # ¿El próximo rival ya tiene spy en ejecución?
    if next_opp.get("has_spy_running"):
        return {"action": "in_progress", "team_name": opp_name}

    # Sin rival conocido no podemos iniciar spy
    if not opp_name:
        return {"action": "error", "team_name": None, "error": "no_next_opponent"}

    # Iniciar spy en el próximo rival
    start_result = start_spy(page, opp_name)
    if start_result["started"]:
        return {"action": "started", "team_name": opp_name,
                "start_result": start_result}
    return {"action": "error", "team_name": opp_name,
            "error": start_result.get("error")}
