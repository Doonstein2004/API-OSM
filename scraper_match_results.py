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
    Extrae los resultados. V4.1 FIXED - Corregida extracción de eventos, stats y ratings.
    """
    print("--- 🟢 EJECUTANDO SCRAPER MATCH RESULTS V4.1 (FIXED) ---")
    
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"
        
        all_leagues_matches = []
        NUM_SLOTS = 4

        # Función auxiliar para convertir iconos a tipos de evento
        # Función auxiliar para convertir iconos a tipos de evento
        def resolve_event_type(html_content):
            html = html_content.lower()
            
            # Prioridad a eventos específicos (Tarjetas, Lesiones, Cambios, Penales fallados)
            if "yellowcard" in html or "icon-player-yellowcard" in html: return "yellow_card"
            if "redcard" in html or "icon-player-redcard" in html: return "red_card"
            if "injury" in html or "icon-player-injury" in html: return "injury"
            if "substitution" in html or "icon-matchevent-sub" in html: return "substitution"
            if "penaltymiss" in html or "icon-matchevent-penaltymiss" in html: return "penalty_miss"
            
            # Goles (Al final para evitar falsos positivos con substrings)
            if "icon-matchevent-goal" in html or "icon-matchevent-penaltygoal" in html or "own-goal" in html: return "goal"
            
            # Fallback seguro para gol solo si no es ninguno de los anteriores
            if "goal" in html and "kick" not in html: return "goal"
            
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
                        
                        # --- CLICK ---
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
                            time.sleep(0.5)

                            # --- EXTRACCIÓN DE DETALLES MEJORADA ---
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
                                    
                                    // Strictness por clase de icono - Devuelve Texto en lugar de número
                                    const icon = refDiv.querySelector('span.icon-referee');
                                    if(icon) {
                                        if(icon.classList.contains('verylenient')) strictness = 'Very Lenient';
                                        else if(icon.classList.contains('lenient')) strictness = 'Lenient';
                                        else if(icon.classList.contains('average')) strictness = 'Average';
                                        else if(icon.classList.contains('strict')) strictness = 'Strict';
                                        else if(icon.classList.contains('verystrict')) strictness = 'Very Strict';
                                    }
                                }

                                // --- EVENTS --- (Ya actualizado anteriormente)
                                const events = [];
                                const rows = Array.from(modal.querySelectorAll('table.table-match-events tbody tr'));
                                rows.forEach(r => {
                                    const minHomeEl = r.querySelector('.td-event-home-minute span');
                                    const minAwayEl = r.querySelector('.td-event-away-minute span');
                                    const minEl = minHomeEl || minAwayEl;
                                    if(!minEl) return;
                                    
                                    const minute = parseInt(minEl.innerText.trim()) || 0;
                                    
                                    const homeNameDiv = r.querySelector('.td-event-home-names > div');
                                    const side = (homeNameDiv && homeNameDiv.children.length > 0) ? 'home' : 'away';
                                    
                                    let eventType = "other";
                                    const iconSpan = r.querySelector('.td-event-home-icon span, .td-event-away-icon span');
                                    
                                    if (iconSpan) {
                                        const cls = iconSpan.className.toLowerCase();
                                        if (cls.includes('yellowcard')) eventType = 'yellow_card';
                                        else if (cls.includes('redcard')) eventType = 'red_card';
                                        else if (cls.includes('injury')) eventType = 'injury';
                                        else if (cls.includes('sub')) eventType = 'substitution';
                                        else if (cls.includes('penaltymiss')) eventType = 'penalty_miss';
                                        else if (cls.includes('goal')) eventType = 'goal';
                                    }
                                    
                                    let player = "";
                                    let detail = "";
                                    const namesCell = r.querySelector(`.td-event-${side}-names`);
                                    if(namesCell) {
                                        const boldEl = namesCell.querySelector('.semi-bold');
                                        if(boldEl) player = boldEl.innerText.trim();
                                        
                                        const allDivs = namesCell.querySelectorAll('div');
                                        allDivs.forEach(d => {
                                            if(!d.classList.contains('semi-bold') && d.innerText.trim()) {
                                                detail = d.innerText.trim();
                                            }
                                        });
                                    }

                                    events.push({
                                        minute: minute,
                                        side: side,
                                        type: eventType,
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
                                        // Usamos TitleCase o snake_case según preferencia? 
                                        // El usuario mostró "Cards" (TitleCase) en el JSON "Correcto". Ajustamos.
                                        let key = titleEl.innerText.trim(); 
                                        // key = key.toLowerCase().replace(/\s+/g, '_'); // Anterior
                                        
                                        const hEl = r.querySelector('.td-match-stat-home');
                                        const aEl = r.querySelector('.td-match-stat-away');
                                        
                                        let hVal = hEl ? hEl.innerText.trim() : "0";
                                        let aVal = aEl ? aEl.innerText.trim() : "0";
                                        
                                        hVal = hVal.replace('%', '').replace(/[^\d]/g, '');
                                        aVal = aVal.replace('%', '').replace(/[^\d]/g, '');
                                        
                                        if(key === 'Cards') {
                                            const hYellow = hEl.querySelector('.icon-player-yellowcard');
                                            const hRed = hEl.querySelector('.icon-player-redcard');
                                            const aYellow = aEl.querySelector('.icon-player-yellowcard');
                                            const aRed = aEl.querySelector('.icon-player-redcard');
                                            
                                            // Formato "Yellow Red" string para coincidir con "2 0" del JSON correcto?
                                            // En el JSON correcto: "Cards": { "away": "0 1", "home": "2 0" } -> Parece "Yellow Red"
                                            const hY = hYellow ? (parseInt(hYellow.innerText.trim()) || 0) : 0;
                                            const hR = hRed ? (parseInt(hRed.innerText.trim()) || 0) : 0;
                                            const aY = aYellow ? (parseInt(aYellow.innerText.trim()) || 0) : 0;
                                            const aR = aRed ? (parseInt(aRed.innerText.trim()) || 0) : 0;
                                            
                                            // Sobrescribimos para devolver strings "Y R" si queremos match exacto
                                            hVal = `${hY} ${hR}`;
                                            aVal = `${aY} ${aR}`;
                                            
                                            // OJO: El código anterior devolvía objeto {white:.., red:..}.
                                            // El JSON correcto muestra "Cards": {"away": "0 1", "home": "2 0"}
                                            // El JSON incorrecto muestra "cards": {"away": {red:0, yellow:2}...}
                                            // Si el Front usa el JSON Correcto, debo cambiar esto.
                                            // Cambiamos a objeto simple string style "Y R"
                                        }
                                        
                                        stats[key] = { home: hVal, away: aVal };
                                    }
                                });

                                // --- RATINGS ---
                                const ratings = { home: [], away: [] };
                                
                                const extractRatings = (table, targetArray) => {
                                    if(!table) return;
                                    const rows = table.querySelectorAll('tbody tr');
                                    rows.forEach(tr => {
                                        const nameEl = tr.querySelector('.td-playergrade-name .semi-bold');
                                        const gradeEl = tr.querySelector('.playergrade span');
                                        if(nameEl && gradeEl) {
                                            const gradeText = gradeEl.innerText.trim();
                                            targetArray.push({
                                                player: nameEl.innerText.trim(), // CAMBIO: 'name' -> 'player'
                                                grade: gradeText === '-' ? "0" : gradeText // CAMBIO: String grade
                                            });
                                        }
                                    });
                                };

                                extractRatings(modal.querySelector('.table-playergrades-home table'), ratings.home);
                                extractRatings(modal.querySelector('.table-playergrades-away table'), ratings.away);

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
                                    match_obj['events'].append({
                                        "minute": raw_ev['minute'],
                                        "type": raw_ev['type'],
                                        "side": raw_ev['side'],
                                        "player": raw_ev['player'] or raw_ev['detail'], # Fallback
                                        "detail": raw_ev['detail']
                                    })
                                
                                print(f"      ✓ Eventos: {len(match_obj['events'])}, Stats: {len(match_obj['statistics'])}, Ref: {match_obj['referee']}")
                                print(f"      ✓ Ratings - Home: {len(match_obj['ratings']['home'])}, Away: {len(match_obj['ratings']['away'])}")
                            else:
                                print("      ⚠️ No se pudieron extraer datos del DOM del modal.")

                        except Exception as e:
                            print(f"      ⚠️ Error procesando modal: {e}")

                        # --- CERRAR MODAL (Hardcore Mode) ---
                        try:
                            # Intentar click en botón de cierre
                            close_btn = page.locator("button.close, [data-dismiss='modal']").first
                            if close_btn.is_visible(timeout=500):
                                close_btn.click(timeout=1000)
                            else:
                                page.keyboard.press("Escape")
                        except:
                            page.keyboard.press("Escape")
                        
                        # Esperar a que se vaya
                        try:
                            page.wait_for_selector(".modal-content", state="hidden", timeout=1500)
                        except:
                            # Si sigue ahí, JS Nuke
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