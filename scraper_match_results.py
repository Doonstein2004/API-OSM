import os
import time
import json
import re
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int, safe_navigate

load_dotenv()

# --- TABLA SELECTOR AMPLIADO ---
RESULTS_TABLE_SELECTORS = [
    "table.table-sticky",
    "#results-list table",
    "#matches-list table",
    "#fixtures-list table",
    ".league-results table",
]

def _navigate_to_league_tab_in_spa(page, tab_href: str, verify_selector: str, timeout_ms: int = 15000) -> bool:
    """
    Navega a una pestaña de la SPA de OSM usando el menú interno.
    """
    try:
        nav_link = page.locator(f"a[href*='{tab_href}']").first
        if nav_link.is_visible(timeout=3000):
            nav_link.click()
            try:
                page.wait_for_url(f"**{tab_href}**", timeout=8000)
            except: pass
            for sel in RESULTS_TABLE_SELECTORS + [verify_selector]:
                try:
                    page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
                    print(f"  ✓ Navegación SPA OK (selector '{sel}')")
                    return True
                except: continue
    except Exception as e:
        print(f"  ⚠️ Nav SPA falló ({e}). Usando fallback goto().")

    full_url = f"https://en.onlinesoccermanager.com{tab_href}"
    for attempt in range(3):
        try:
            page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            for sel in RESULTS_TABLE_SELECTORS + [verify_selector]:
                try:
                    page.wait_for_selector(sel, timeout=12000, state="visible")
                    print(f"  ✓ Fallback goto() OK (selector '{sel}')")
                    return True
                except: continue
            page.reload(wait_until="domcontentloaded")
            time.sleep(2)
        except Exception as e:
            time.sleep(3)
    return False

def get_match_results(page, scrape_future_fixtures=False):
    """
    Extrae los resultados. V4.2 - Navegación condicional por jornadas y calendario completo.
    """
    print("--- 🟢 EJECUTANDO SCRAPER MATCH RESULTS V4.2 ---")
    
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        all_leagues_matches = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Resultados - Slot #{i + 1} ---")
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            try:
                handle_popups(page)
                page.wait_for_selector(".career-teamslot", timeout=20000)
            except: 
                continue
                
            slot = page.locator(".career-teamslot").nth(i)
            
            from utils import get_slot_info
            team_name, league_name, _ = get_slot_info(slot)
            
            if not team_name:
                print(f"Slot #{i + 1} no es procesable (Searching/Unavailable/Empty). Saltando.")
                continue

            print(f"Procesando equipo: {team_name} en la liga {league_name}")

            from utils import click_slot_and_wait_for_dashboard
            if not click_slot_and_wait_for_dashboard(page, i):
                print(f"  ❌ No se pudo activar el slot {i+1}. Saltando.")
                continue

            
            try:
                tabs_to_visit = ["/League/Results"]
                if scrape_future_fixtures:
                    tabs_to_visit.append("/League/Fixtures")
                
                league_matches = []
                seen_matches = set()

                for tab_path in tabs_to_visit:
                    print(f"  - Navegando a {tab_path}...")
                    if not _navigate_to_league_tab_in_spa(page, tab_path, verify_selector="table.table-sticky"):
                        continue
                    
                    time.sleep(1)

                    is_fixtures = "/Fixtures" in tab_path
                    if is_fixtures and scrape_future_fixtures:
                        print("    📂 Iniciando escaneo completo de calendario...")
                        prev_btn = page.locator(".fixtures-matchday-nav-prev, .btn-prev").first
                        while prev_btn.count() > 0 and prev_btn.is_visible(timeout=500):
                            try:
                                prev_btn.click()
                                time.sleep(0.3)
                            except: break
                        time.sleep(0.5)

                    while True:
                        round_number = 0
                        try:
                            header_span = page.locator("th.text-center span[data-bind*='weekNr'], .matchday-title").first
                            if header_span.count() > 0:
                                txt = header_span.inner_text()
                                round_number = safe_int(re.search(r'(\d+)', txt).group(1)) if re.search(r'(\d+)', txt) else 0
                        except: pass
                        
                        match_rows_data = page.evaluate("""() => {
                            const tableSelectors = ['table.table-sticky', '#results-list table', '#matches-list table', '.league-results table', 'table'];
                            let tableEl = null;
                            for (const sel of tableSelectors) {
                                tableEl = document.querySelector(sel);
                                if (tableEl) break;
                            }
                            const rows = tableEl ? Array.from(tableEl.querySelectorAll('tbody tr')) : [];
                            let currentRound = 0;
                            const extracted = [];
                            for(let i=0; i<rows.length; i++){
                                const r = rows[i];
                                const headerEl = r.querySelector('td[colspan] span') || r.querySelector('td span') || r;
                                const txtFull = headerEl.innerText.trim();
                                const matchdayRegex = /(?:matchday|jornada|round|week|rodada|rnd)\\s*(\\d+)/i;
                                const m = txtFull.match(matchdayRegex);
                                if (m) {
                                    currentRound = parseInt(m[1], 10);
                                    continue;
                                }
                                const home = r.querySelector('.td-home .font-sm'), away = r.querySelector('.td-away .font-sm');
                                if(!home || !away) continue;
                                const scoreEl = r.querySelector('.match-score span') || r.querySelector('span[data-bind*="score"]');
                                let isPlayed = false, hGoals = 0, aGoals = 0;
                                if (scoreEl) {
                                    const txt = scoreEl.innerText.trim();
                                    if(txt.includes('-') && !txt.includes(':')) {
                                        const parts = txt.split('-');
                                        if(parts.length === 2) { isPlayed = true; hGoals = parseInt(parts[0]); aGoals = parseInt(parts[1]); }
                                    }
                                }
                                const hMgrEl = r.querySelector('.td-home .text-secondary'), aMgrEl = r.querySelector('.td-away .text-secondary');
                                extracted.push({
                                    idx: i, round: currentRound, is_played: isPlayed,
                                    home_team: home.innerText.trim(), away_team: away.innerText.trim(),
                                    home_manager: hMgrEl ? hMgrEl.innerText.trim() : "CPU",
                                    away_manager: aMgrEl ? aMgrEl.innerText.trim() : "CPU",
                                    home_goals: hGoals, away_goals: aGoals
                                });
                            }
                            return extracted;
                        }""")
                        
                        print(f"    - Jornada {round_number}: {len(match_rows_data)} partidos.")

                        for m_info in match_rows_data:
                            m_round = m_info.get("round") if m_info.get("round") > 0 else round_number
                            m_key = (m_info['home_team'], m_info['away_team'], m_round)
                            if m_key in seen_matches: continue
                            seen_matches.add(m_key)

                            if not scrape_future_fixtures and not m_info['is_played']: continue

                            match_obj = {
                                "round": m_round, "home_team": m_info['home_team'], "home_manager": m_info['home_manager'],
                                "away_team": m_info['away_team'], "away_manager": m_info['away_manager'],
                                "home_goals": m_info['home_goals'], "away_goals": m_info['away_goals'],
                                "is_played": m_info['is_played'], "referee": "", "referee_strictness": "",
                                "events": [], "statistics": {}, "ratings": {"home": [], "away": []}
                            }

                            if m_info['is_played']:
                                print(f"    🔍 Detalles: {m_info['home_team']} vs {m_info['away_team']}")
                                table_sel = "table.table-sticky"
                                for _sel in RESULTS_TABLE_SELECTORS:
                                    if page.locator(_sel).count() > 0:
                                        table_sel = _sel
                                        break
                                row_locator = page.locator(f"{table_sel} tbody tr").nth(m_info['idx'])
                                try:
                                    # Click con retry y verificación de modal
                                    row_locator.click(position={"x": 5, "y": 5}, force=True)
                                    
                                    # Esperar a que el modal aparezca y TENGA CONTENIDO
                                    # Esperamos al referee o a la tabla de eventos/stats
                                    try:
                                        page.wait_for_selector(".modal-content #match-details-referee, .modal-content .table-match-events, .modal-content #table-match-statistics", 
                                                              state="visible", timeout=5000)
                                        # Un pequeño respiro extra para que el AJAX termine de poblar todo
                                        time.sleep(1.2)
                                    except:
                                        print("      ⚠️ El contenido del modal tardó demasiado en aparecer.")

                                    details_data = page.evaluate(r"""() => {
                                        const modal = document.querySelector('.modal-content');
                                        if (!modal) return null;
                                        
                                        // Reintentar encontrar elementos si están vacíos (pequeño loop interno)
                                        let refName = "", strictness = "Unknown";
                                        const refDiv = modal.querySelector('#match-details-referee');
                                        if (refDiv) {
                                            const spanName = refDiv.querySelector('span[data-bind*="text: name"]');
                                            if(spanName) refName = spanName.innerText.trim();
                                            const icon = refDiv.querySelector('span.icon-referee');
                                            if(icon) {
                                                if(icon.classList.contains('verylenient')) strictness = 'Very Lenient';
                                                else if(icon.classList.contains('lenient')) strictness = 'Lenient';
                                                else if(icon.classList.contains('average')) strictness = 'Average';
                                                else if(icon.classList.contains('strict')) strictness = 'Strict';
                                                else if(icon.classList.contains('verystrict')) strictness = 'Very Strict';
                                            }
                                        }
                                        const events = [];
                                        Array.from(modal.querySelectorAll('table.table-match-events tbody tr')).forEach(r => {
                                            const minEl = r.querySelector('.td-event-home-minute span') || r.querySelector('.td-event-away-minute span');
                                            if(!minEl) return;
                                            const side = (r.querySelector('.td-event-home-names > div')?.children.length > 0) ? 'home' : 'away';
                                            let type = "other";
                                            const icon = r.querySelector('.td-event-home-icon span, .td-event-away-icon span');
                                            if (icon) {
                                                const cls = icon.className.toLowerCase();
                                                if (cls.includes('yellowcard')) type = 'yellow_card';
                                                else if (cls.includes('redcard')) type = 'red_card';
                                                else if (cls.includes('injury')) type = 'injury';
                                                else if (cls.includes('sub')) type = 'substitution';
                                                else if (cls.includes('penaltymiss')) type = 'penalty_miss';
                                                else if (cls.includes('goal')) type = 'goal';
                                            }
                                            const cell = r.querySelector(`.td-event-${side}-names`);
                                            const player = cell?.querySelector('.semi-bold')?.innerText.trim() || "";
                                            const detail = Array.from(cell?.querySelectorAll('div') || []).find(d => !d.classList.contains('semi-bold'))?.innerText.trim() || "";
                                            events.push({ minute: parseInt(minEl.innerText) || 0, side, type, player, detail });
                                        });
                                        const stats = {};
                                        Array.from(modal.querySelectorAll('#table-match-statistics tbody tr')).forEach(r => {
                                            const title = r.querySelector('.td-match-stat-title')?.innerText.trim();
                                            if(title) {
                                                const hEl = r.querySelector('.td-match-stat-home'), aEl = r.querySelector('.td-match-stat-away');
                                                let hVal = hEl?.innerText.trim() || "0", aVal = aEl?.innerText.trim() || "0";
                                                if (title !== 'Formation') { hVal = hVal.replace(/[^\d]/g, ''); aVal = aVal.replace(/[^\d]/g, ''); }
                                                if(title === 'Cards') {
                                                    const hY = hEl.querySelector('.icon-player-yellowcard')?.innerText.trim() || "0";
                                                    const hR = hEl.querySelector('.icon-player-redcard')?.innerText.trim() || "0";
                                                    const aY = aEl.querySelector('.icon-player-yellowcard')?.innerText.trim() || "0";
                                                    const aR = aEl.querySelector('.icon-player-redcard')?.innerText.trim() || "0";
                                                    hVal = `${hY} ${hR}`; aVal = `${aY} ${aR}`;
                                                }
                                                stats[title] = { home: hVal, away: aVal };
                                            }
                                        });
                                        const ratings = { home: [], away: [] };
                                        const exRat = (tbl, arr) => tbl?.querySelectorAll('tbody tr').forEach(tr => {
                                            const n = tr.querySelector('.td-playergrade-name .semi-bold')?.innerText.trim();
                                            const g = tr.querySelector('.playergrade span')?.innerText.trim();
                                            if(n && g) arr.push({ player: n, grade: g === '-' ? "0" : g });
                                        });
                                        exRat(modal.querySelector('.table-playergrades-home table'), ratings.home);
                                        exRat(modal.querySelector('.table-playergrades-away table'), ratings.away);
                                        return { referee: refName, strictness, events, stats, ratings };
                                    }""")
                                    if details_data:
                                        match_obj.update({
                                            'referee': details_data['referee'], 'referee_strictness': details_data['strictness'],
                                            'statistics': details_data['stats'], 'ratings': details_data['ratings'],
                                            'events': details_data['events']
                                        })
                                except: pass
                                try:
                                    close_btn = page.locator("button.close, [data-dismiss='modal']").first
                                    if close_btn.count() > 0 and close_btn.is_visible(timeout=500): close_btn.click()
                                    else: page.keyboard.press("Escape")
                                    page.wait_for_selector(".modal-content", state="hidden", timeout=1500)
                                except:
                                    page.evaluate("() => { document.querySelectorAll('.modal, .modal-backdrop').forEach(el => el.remove()); document.body.classList.remove('modal-open'); }")
                                    time.sleep(0.2)
                            league_matches.append(match_obj)

                        if is_fixtures and scrape_future_fixtures:
                            next_btn = page.locator(".fixtures-matchday-nav-next, .btn-next").first
                            if next_btn.count() > 0 and next_btn.is_visible(timeout=1000):
                                try: next_btn.click(); time.sleep(0.5)
                                except: break
                            else: break
                        else: break

                all_leagues_matches.append({"league_name": league_name, "team_name": team_name, "matches": league_matches})
            except Exception as e:
                print(f"  ❌ Error en slot {i}: {e}")
        return all_leagues_matches
    except Exception as e:
        print(f"❌ Error crítico en scraper: {e}")
        return []