import os
import time
import json
import re
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int, safe_navigate

load_dotenv()

def get_match_results(page, scrape_future_fixtures=False):
    """
    Extrae los resultados. V4.0 Robusta (Híbrida: Iteración JS + Extracción Selectores).
    Combina la velocidad de iteración de la V3 con la fiabilidad de extracción de la V1.
    """
    print("--- 🟢 EJECUTANDO SCRAPER MATCH RESULTS V4.0 (ROBUST HYBRID) ---")
    
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"
        
        all_leagues_matches = []
        NUM_SLOTS = 4

        # Función auxiliar para convertir iconos a tipos de evento
        def resolve_event_type(html_content):
            html = html_content.lower()
            if "icon-matchevent-goal" in html or "icon-matchevent-penaltygoal" in html or "goal" in html: return "goal"
            if "icon-player-yellowcard" in html or "yellowcard" in html: return "yellow_card"
            if "icon-player-redcard" in html or "redcard" in html: return "red_card"
            if "icon-player-injury" in html or "injury" in html: return "injury"
            if "icon-matchevent-sub" in html or "substitution" in html: return "substitution"
            if "icon-matchevent-penaltymiss" in html or "penaltymiss" in html: return "penalty_miss"
            return "other"

        # --- LOOP PRINCIPAL ---
        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Resultados - Slot #{i + 1} ---")
            
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            try:
                page.wait_for_selector(".career-teamslot", timeout=20000)
            except: 
                print("  ⚠️ Timeout en dashboard. Saltando.")
                continue
                
            handle_popups(page)

            slot = page.locator(".career-teamslot").nth(i)
            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando equipo: {team_name} en la liga {league_name}")

            slot.click()
            try:
                page.wait_for_selector("#timers", timeout=45000)
            except:
                print("  ⚠️ Timeout cargando equipo. Siguiente.")
                continue
                
            handle_popups(page)
            
            try:
                print(f"  - Navegando a Resultados...")
                if not safe_navigate(page, RESULTS_URL, verify_selector="table.table-sticky"):
                    print("  ❌ No se pudo cargar la tabla de resultados. Saltando.")
                    continue
                
                # --- JORNADA ---
                round_number = 0
                try:
                    header_span = page.locator("th.text-center span[data-bind*='weekNr']")
                    if header_span.count() > 0:
                        round_number = safe_int(header_span.inner_text())
                except: pass
                print(f"  - Jornada detectada: {round_number}")

                # --- EXTRACT ROWS VIA JS (Solo para saber cuántas son y su estado básico) ---
                # Esto es más rápido que iterar locators uno por uno solo para ver si se jugaron
                match_rows_data = page.evaluate("""() => {
                    const rows = Array.from(document.querySelectorAll("table.table-sticky tbody tr"));
                    return rows.map((r, i) => {
                        const isClickable = r.classList.contains('clickable') || r.getAttribute('onclick');
                        const home = r.querySelector('.td-home .font-sm');
                        const away = r.querySelector('.td-away .font-sm');
                        const scoreEl = r.querySelector('.match-score span') || r.querySelector('span[data-bind*="score"]');
                        
                        let isPlayed = false;
                        let hGoals = 0, aGoals = 0;
                        
                        if (scoreEl) {
                            const txt = scoreEl.innerText.trim();
                            if(txt.includes('-') && !txt.includes(':')) {
                                const parts = txt.split('-');
                                if(parts.length === 2) {
                                    isPlayed = true;
                                    hGoals = parseInt(parts[0]);
                                    aGoals = parseInt(parts[1]);
                                }
                            }
                        }

                        // Managers
                        const hMgrEl = r.querySelector('.td-home .text-secondary');
                        const aMgrEl = r.querySelector('.td-away .text-secondary');

                        return {
                            idx: i,
                            is_played: isPlayed,
                            home_team: home ? home.innerText.trim() : "Unknown",
                            away_team: away ? away.innerText.trim() : "Unknown",
                            home_manager: hMgrEl ? hMgrEl.innerText.trim() : "CPU",
                            away_manager: aMgrEl ? aMgrEl.innerText.trim() : "CPU",
                            home_goals: hGoals,
                            away_goals: aGoals
                        };
                    });
                }""")
                
                print(f"  - Encontrados {len(match_rows_data)} partidos en lista.")
                
                league_matches = []

                for m_idx, m_info in enumerate(match_rows_data):
                    # Filtrar futuros si no se piden
                    if not scrape_future_fixtures and not m_info['is_played']:
                        continue

                    # Objeto base
                    match_obj = {
                        "round": round_number,
                        "home_team": m_info['home_team'],
                        "home_manager": m_info['home_manager'],
                        "away_team": m_info['away_team'],
                        "away_manager": m_info['away_manager'],
                        "home_goals": m_info['home_goals'],
                        "away_goals": m_info['away_goals'],
                        "is_played": m_info['is_played'],
                        "referee": "",
                        "referee_strictness": "",
                        "events": [],
                        "statistics": {},
                        "ratings": {"home": [], "away": []}
                    }

                    if m_info['is_played']:
                        print(f"    🔍 Detalles para {m_info['home_team']} vs {m_info['away_team']}...")
                        
                        # --- CLICK (Usando Locator Estándar) ---
                        # Usamos .nth() sobre el selector original de la tabla para asegurar consistencia
                        row_locator = page.locator("table.table-sticky tbody tr").nth(m_info['idx'])
                        
                        try:
                            # Click con retry
                            row_locator.click(position={"x": 5, "y": 5}, force=True)
                            
                            # Esperar modal
                            try:
                                page.wait_for_selector(".modal-content table.table-match-events", state="visible", timeout=3500)
                            except:
                                # A veces no hay eventos (0-0), esperamos al menos el contenedor o stats
                                page.wait_for_selector(".modal-content", state="visible", timeout=3000)

                            # Pequeña espera de estabilización
                            time.sleep(0.3)

                            # --- EXTRACCIÓN DE DETALLES (ESTRATEGIA SELECTORES) ---
                            # Usamos evaluate para extraer todo el DOM del modal de una sola vez
                            # Esto es mucho más rápido que hacer 50 llamadas a locator().inner_text()
                            
                            details_data = page.evaluate("""() => {
                                const modal = document.querySelector('.modal-content');
                                if (!modal) return null;

                                // --- REFEREE ---
                                let refName = "";
                                let strictness = "Unknown";
                                const refDiv = modal.querySelector('#match-details-referee');
                                if (refDiv) {
                                    const spanName = refDiv.querySelector('span[data-bind*="text: name"]');
                                    if(spanName) refName = spanName.innerText.trim();
                                    
                                    // Strictness por clase de icono
                                    const icon = refDiv.querySelector('span.icon-referee');
                                    if(icon) {
                                        if(icon.classList.contains('very-lenient')) strictness = 'Very Lenient';
                                        else if(icon.classList.contains('lenient')) strictness = 'Lenient';
                                        else if(icon.classList.contains('average')) strictness = 'Average';
                                        else if(icon.classList.contains('strict')) strictness = 'Strict';
                                        else if(icon.classList.contains('very-strict')) strictness = 'Very Strict';
                                    }
                                }

                                // --- EVENTS ---
                                const events = [];
                                const rows = Array.from(modal.querySelectorAll('table.table-match-events tbody tr'));
                                rows.forEach(r => {
                                    const minEl = r.querySelector('.td-event-home-minute span, .td-event-away-minute span');
                                    if(!minEl) return;
                                    
                                    const minute = parseInt(minEl.innerText.trim()) || 0;
                                    
                                    // Detectar lado
                                    const homeNameDiv = r.querySelector('.td-event-home-names > div');
                                    const side = homeNameDiv ? 'home' : 'away';
                                    
                                    // Iconos HTML
                                    const hIcon = r.querySelector('.td-event-home-icon').innerHTML;
                                    const aIcon = r.querySelector('.td-event-away-icon').innerHTML;
                                    const rawHtml = (hIcon + aIcon).toLowerCase();
                                    
                                    // Nombre Jugador
                                    let player = "";
                                    const boldInfo = r.querySelector('.semi-bold');
                                    if(boldInfo) player = boldInfo.innerText.trim();
                                    
                                    // Detalle extra (si no es jugador)
                                    let detail = "";
                                    if(!player) {
                                        const detDiv = r.querySelector(`.td-event-${side}-names div div:not(.semi-bold)`);
                                        if(detDiv) detail = detDiv.innerText.trim();
                                    }

                                    events.push({
                                        minute: minute,
                                        side: side,
                                        raw_html: rawHtml,
                                        player: player,
                                        detail: detail
                                    });
                                });

                                // --- STATS ---
                                const stats = {};
                                const statRows = Array.from(modal.querySelectorAll('#table-match-statistics tbody tr'));
                                statRows.forEach(r => {
                                    const titleEl = r.querySelector('.td-match-stat-title');
                                    if(titleEl) {
                                        const key = titleEl.innerText.trim();
                                        const hVal = r.querySelector('.td-match-stat-home') ? r.querySelector('.td-match-stat-home').innerText.trim() : "0";
                                        const aVal = r.querySelector('.td-match-stat-away') ? r.querySelector('.td-match-stat-away').innerText.trim() : "0";
                                        stats[key] = { home: hVal, away: aVal };
                                    }
                                });

                                // --- RATINGS ---
                                const ratings = { home: [], away: [] };
                                const extractR = (selector, list) => {
                                    const t = modal.querySelector(selector);
                                    if(!t) return;
                                    t.querySelectorAll('tr').forEach(tr => {
                                        const nameEl = tr.querySelector('.td-playergrade-name .semi-bold');
                                        const gradeEl = tr.querySelector('.playergrade span');
                                        if(nameEl && gradeEl) {
                                            list.push({
                                                player: nameEl.innerText.trim(),
                                                grade: gradeEl.innerText.trim()
                                            });
                                        }
                                    });
                                };
                                extractR('table.table-match-events + table .table-playergrades-home table', ratings.home); // A veces el selector es complejo
                                // Fallback selector más simple
                                if(ratings.home.length === 0) extractR('.table-playergrades-home table', ratings.home);
                                if(ratings.home.length === 0) extractR('#table-playergrades .table-playergrades-home table', ratings.home);
                                
                                extractR('#table-playergrades .table-playergrades-away table', ratings.away);

                                return {
                                    referee: refName,
                                    strictness: strictness,
                                    events: events,
                                    stats: stats,
                                    ratings: ratings
                                };
                            }""")

                            if details_data:
                                match_obj['referee'] = details_data['referee']
                                match_obj['referee_strictness'] = details_data['strictness']
                                match_obj['statistics'] = details_data['stats']
                                match_obj['ratings'] = details_data['ratings']
                                
                                # Procesar eventos en Python
                                for raw_ev in details_data['events']:
                                    ev_type = resolve_event_type(raw_ev['raw_html'])
                                    match_obj['events'].append({
                                        "minute": raw_ev['minute'],
                                        "type": ev_type,
                                        "side": raw_ev['side'],
                                        "player": raw_ev['player'] or raw_ev['detail'] # Fallback
                                    })
                                
                                print(f"      ✓ Eventos: {len(match_obj['events'])}, Ref: {match_obj['referee']}")
                            else:
                                print("      ⚠️ No se pudieron extraer datos del DOM del modal.")

                        except Exception as e:
                            print(f"      ⚠️ Error procesando modal: {e}")

                        # --- CERRAR MODAL (Hardcore Mode) ---
                        # 1. Intentar click
                        try:
                            # La X de cierre
                            close_btn = page.locator("button.close, [data-dismiss='modal']").first
                            if close_btn.is_visible():
                                close_btn.click(timeout=1000)
                            else:
                                page.keyboard.press("Escape")
                        except:
                            page.keyboard.press("Escape")
                        
                        # 2. Esperar a que se vaya
                        try:
                            page.wait_for_selector(".modal-content", state="hidden", timeout=1500)
                        except:
                            # 3. Si sigue ahí, JS Nuke
                            page.evaluate("""() => {
                                const modals = document.querySelectorAll('.modal, .modal-backdrop');
                                modals.forEach(el => el.remove());
                                document.body.classList.remove('modal-open');
                                document.body.style.paddingRight = '';
                            }""")
                            time.sleep(0.2)

                    league_matches.append(match_obj)

                all_leagues_matches.append({
                    "league_name": league_name,
                    "matches": league_matches
                })

            except Exception as e:
                print(f"  ❌ Error en slot {i}: {e}")
                continue

        return all_leagues_matches

    except Exception as e:
        print(f"❌ Error crítico en scraper: {e}")
        return []