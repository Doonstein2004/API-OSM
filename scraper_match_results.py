import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int, safe_navigate

load_dotenv()

def get_match_results(page, scrape_future_fixtures=False):
    """
    Extrae los resultados.
    Si scrape_future_fixtures=True, recorre todas las jornadas disponibles.
    Si False, solo la actual por defecto.
    """

    # --- INTERNAL HELPER TO SCRAPE A ROUND ---
    def scrape_current_round(page, round_element_idx=None):
        matches_list = []
        
        # 1. Detect Round Number or Cup
        # Determine if it's a cup round by checking the active item or passed index
        # NOTE: When scraping future, the active item is what we clicked.
        
        try:
            # We look at the "header" of the round list (the slidee items are just numbers/icons)
            # The text "Matchday X" is usually in the summary or we imply it.
            # But simpler: check the th header.
            header_span = page.locator("th.text-center span[data-bind*='weekNr']")
            if header_span.count() > 0:
                week_text = header_span.inner_text()
                round_number = safe_int(week_text)
            else:
                # Might be Cup
                round_number = -1 # ID for Cup
        except:
            round_number = 0

        # Check for "Draw wasn't conducted" (Empty Cup Round)
        if page.locator("td:has-text(\"This Cup round's draw wasn't conducted yet\")").count() > 0:
             print(f"  - Jornada {round_number} (Copa): Sorteo no realizado. Saltando.")
             return []

        # Check for "No fixtures" message (Pre-season or unknown)
        if page.locator("td.text-center:has-text('There are no Fixtures')").count() > 0:
             print(f"  - Jornada {round_number}: Sin partidos (Pre-season/Empty).")
             return []

        # Contar filas
        # Sometimes rows are .clickable, sometimes not (unplayed)
        row_count = page.locator("table.table-sticky tbody tr").count()
        print(f"  - Encontrados {row_count} partidos en la jornada {round_number}.")

        for j in range(row_count):
            row = page.locator("table.table-sticky tbody tr").nth(j) 
            # Note: Removed .clickable constraint to ensure we catch all
            # Start checks to see if it's a real match row
            if row.locator("td.td-home").count() == 0: continue

            row.scroll_into_view_if_needed()
            
            # --- 1. EXTRACCIÓN SEGURA DE DATOS BÁSICOS ---
            home_team = row.locator("td.td-home .font-sm").inner_text().strip()
            away_team = row.locator("td.td-away .font-sm").inner_text().strip()
            
            h_mgr_loc = row.locator("td.td-home .text-secondary")
            home_manager = h_mgr_loc.first.inner_text().strip() if h_mgr_loc.count() > 0 else "CPU"
            
            a_mgr_loc = row.locator("td.td-away .text-secondary")
            away_manager = a_mgr_loc.first.inner_text().strip() if a_mgr_loc.count() > 0 else "CPU"

            # Referee
            ref_loc = row.locator("td.td-round .referee-name")
            referee_name = ref_loc.inner_text().strip() if ref_loc.count() > 0 else ""
            
            ref_strictness = "Unknown"
            strict_map = {
                "verylenient": "Very Lenient",
                "lenient": "Lenient",
                "average": "Average", 
                "strict": "Strict",
                "verystrict": "Very Strict"
            }
            # Check class of icon
            for cls, name in strict_map.items():
                if row.locator(f".icon-referee.{cls}").count() > 0:
                    ref_strictness = name
                    break

            # Init vars
            home_goals = 0
            away_goals = 0
            events = []
            stats = {}
            ratings = {"home": [], "away": []}
            is_played = False

            # Check if played
            # Una partida jugada muestra el resultado en lugar de fecha/hora
            is_played_visually = False
            try:
                # Verificar primero si existe la columna de score
                score_el = row.locator("td.td-score")
                if score_el.count() > 0:
                    score_col = score_el.inner_text(timeout=1000).strip()
                    # Heurística: Si tiene guión y no tiene dos puntos (hora), es un resultado
                    is_played_visually = "-" in score_col and ":" not in score_col
                else:
                    # Fallback: Usar la lógica antigua del referee-container
                    # Si NO hay referee container visible en la celda round, asumimos que se jugó (o el diseño cambió)
                    is_played_visually = row.locator("td.td-round .referee-container").count() == 0
            except Exception:
                # Si falla algo, fallback seguro
                is_played_visually = row.locator("td.td-round .referee-container").count() == 0

            # If it is played, we try to get more details
            if is_played_visually: 
                print(f"    🔍 Extrayendo detalles para {home_team} vs {away_team}...")
                try:
                    # Asegurar que no hay modales bloqueando
                    handle_popups(page)
                    
                    row.click(force=True)
                    try:
                        page.wait_for_selector(".modal-content", state="visible", timeout=3000)
                    except TimeoutError:
                        print("      ⚠️ Timeout esperando modal de detalles. Intentando click de nuevo...")
                        row.click(force=True)
                        page.wait_for_selector(".modal-content", state="visible", timeout=3000)
                    
                    # --- A. EXTRAER EVENTOS ---
                    try:
                        event_rows = page.locator(".modal-content table.table-match-events tbody tr")
                        # Esperar un poco a que carguen las filas si es necesario
                        if event_rows.count() == 0:
                            time.sleep(0.5)
                        
                        item_count = event_rows.count()
                        print(f"      - Eventos encontrados: {item_count}")
                        
                        for e_idx in range(item_count):
                            e_row = event_rows.nth(e_idx)
                            min_loc = e_row.locator(".td-event-home-minute, .td-event-away-minute").first
                            if min_loc.count() > 0:
                                minute = safe_int(min_loc.inner_text())
                                h_icon = e_row.locator(".td-event-home-icon").inner_html() if e_row.locator(".td-event-home-icon").count() > 0 else ""
                                a_icon = e_row.locator(".td-event-away-icon").inner_html() if e_row.locator(".td-event-away-icon").count() > 0 else ""
                                full_icon = (h_icon + a_icon).lower()
                                
                                event_type = "other"
                                if "goal" in full_icon: event_type = "goal"
                                elif "yellowcard" in full_icon: event_type = "yellow_card"
                                elif "redcard" in full_icon: event_type = "red_card"
                                
                                team_side = "home" if e_row.locator("td.td-event-home-names div").count() > 0 else "away"
                                p_name = e_row.locator(".semi-bold").first.inner_text().strip() if e_row.locator(".semi-bold").count() > 0 else ""
                                
                                events.append({"minute": minute, "type": event_type, "side": team_side, "player": p_name})

                        # Stats
                        stat_rows = page.locator("#table-match-statistics > tbody > tr")
                        print(f"      - Estadísticas encontradas: {stat_rows.count()}")
                        for s_idx in range(stat_rows.count()):
                            s_row = stat_rows.nth(s_idx)
                            if s_row.locator(".td-match-stat-title").count() > 0:
                                title = s_row.locator(".td-match-stat-title").inner_text().strip()
                                stats[title] = {
                                    "home": s_row.locator(".td-match-stat-home").inner_text().strip(),
                                    "away": s_row.locator(".td-match-stat-away").inner_text().strip()
                                }
                        
                        # Score
                        score_text_el = page.locator(".modal-content .match-score")
                        if score_text_el.count() > 0:
                            score_text = score_text_el.inner_text().strip()
                            if "-" in score_text:
                                p = score_text.split("-")
                                home_goals = safe_int(p[0])
                                away_goals = safe_int(p[1])

                        # Close modal - Robust way
                        close_btn = page.locator(".close-button-container button.close, .modal.in button.close, [data-dismiss='modal']")
                        if close_btn.count() > 0 and close_btn.first.is_visible():
                            close_btn.first.click()
                        else: 
                            page.keyboard.press("Escape")
                        
                        page.wait_for_selector(".modal-content", state="hidden", timeout=3000)

                    except Exception as inner_e:
                        print(f"      ⚠️ Error procesando contenido del modal: {inner_e}")
                        # Force close if failed inside
                        page.keyboard.press("Escape")

                except Exception as e:
                    print(f"    ⚠️ Error detail grab ({home_team} vs {away_team}): {e}")
                    # Ensure modal is gone
                    page.keyboard.press("Escape")
            else:
                # FUTURE MATCH
                # We already have referee, teams, managers.
                pass

            matches_list.append({
                "round": round_number,
                "home_team": home_team,
                "home_manager": home_manager,
                "away_team": away_team,
                "away_manager": away_manager,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "is_played": is_played_visually,
                "referee": referee_name,
                "referee_strictness": ref_strictness,
                "events": events,
                "statistics": stats,
                "ratings": ratings
            })
            
        return matches_list


    # --- MAIN LOOP ---
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"
        
        all_leagues_matches = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Resultados - Slot #{i + 1} ---")
            
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            page.wait_for_selector(".career-teamslot", timeout=35000)
            handle_popups(page)

            slot = page.locator(".career-teamslot").nth(i)
            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando equipo: {team_name} en la liga {league_name}")

            slot.click()
            page.wait_for_selector("#timers", timeout=45000)
            handle_popups(page)
            
            try:
                print(f"  - Navegando a Resultados...")
                if not safe_navigate(page, RESULTS_URL, verify_selector="table.table-sticky"):
                    print("  ❌ No se pudo cargar la tabla de resultados. Saltando.")
                    continue
                
                league_matches = []

                if scrape_future_fixtures:
                    # Iterate through ALL rounds
                    # Locate the slider items
                    rounds_locator = page.locator("#round-sly-container ul.slidee li.round-item")
                    count_rounds = rounds_locator.count()
                    print(f"  - Calendario detectado: {count_rounds} jornadas totales.")
                    
                    for r_idx in range(count_rounds):
                        r_item = rounds_locator.nth(r_idx)
                        
                        # --- SCROLL FIX: Use JS to force scroll ---
                        try:
                            r_item.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                            # Short wait for animation
                            time.sleep(0.2)
                            if not r_item.is_visible():
                                print(f"    ⚠️ Item {r_idx} no visible tras scroll. Intentando forzar...")
                            
                            r_item.click(force=True)
                            time.sleep(1) # Safety wait for transition
                            
                            round_data = scrape_current_round(page, round_element_idx=r_idx)
                            league_matches.extend(round_data)
                        except Exception as e:
                            print(f"    ❌ Error clicando jornada {r_idx}: {e}")
                            
                else:
                    # Original behavior: just scrape what is there (Current/Last Played)
                    round_data = scrape_current_round(page)
                    league_matches.extend(round_data)

                # Deduplicate locally (Safety net)
                unique_lm = []
                seen_local = set()
                for m in league_matches:
                    # Key: Round + Home + Away
                    # Sometimes round is 0 or -1, so teams are the best key
                    k = (m['round'], m['home_team'], m['away_team'])
                    if k not in seen_local:
                        seen_local.add(k)
                        unique_lm.append(m)
                
                print(f"  - Total escaneado (unicos): {len(unique_lm)} partidos.")
                all_leagues_matches.append({
                    "league_name": league_name,
                    "matches": unique_lm
                })

            except Exception as e:
                print(f"  ❌ Error en slot {i}: {e}")
                continue

        return all_leagues_matches
    except Exception as e:
        print(f"❌ Error crítico: {e}")
        return []

if __name__ == "__main__":
    # Bloque para testear individualmente (requiere sesión activa)
    print("Este módulo está diseñado para ser importado y recibir el objeto 'page'.")