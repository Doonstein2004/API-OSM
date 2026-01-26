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
    Extrae los resultados. V2.1 Debug Mode.
    """
    print("--- 🟢 EJECUTANDO SCRAPER MATCH RESULTS V2.1 (DEBUG) ---")
    
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"
        
        all_leagues_matches = []
        NUM_SLOTS = 4

        # Función auxiliar
        def scrape_current_round(page, round_element_idx=None):
            matches_list = []
            
            # Detectar Jornada
            try:
                header_span = page.locator("th.text-center span[data-bind*='weekNr']")
                if header_span.count() > 0:
                    round_number = safe_int(header_span.inner_text())
                else:
                    round_number = 0
            except:
                round_number = 0
            
            # Contar filas
            rows = page.locator("table.table-sticky tbody tr")
            row_count = rows.count()
            print(f"  - Encontrados {row_count} filas (Jornada {round_number}).")

            for j in range(row_count):
                row = rows.nth(j)
                
                # Verificar si es una fila de partido válida
                if row.locator("td.td-home").count() == 0: continue

                row.scroll_into_view_if_needed()
                
                # --- DATOS BÁSICOS ---
                home_team = row.locator("td.td-home .font-sm").inner_text().strip()
                away_team = row.locator("td.td-away .font-sm").inner_text().strip()
                
                # Mánagers
                h_mgr_loc = row.locator("td.td-home .text-secondary")
                home_manager = h_mgr_loc.first.inner_text().strip() if h_mgr_loc.count() > 0 else "CPU"
                
                a_mgr_loc = row.locator("td.td-away .text-secondary")
                away_manager = a_mgr_loc.first.inner_text().strip() if a_mgr_loc.count() > 0 else "CPU"

                # Detección de partido jugado
                is_played = False
                home_goals = 0
                away_goals = 0
                score_text = ""
                
                # --- DEBUG ROW CONTENT ---
                # row_text = row.inner_text()
                # print(f"    [ROW RAW] {row_text.replace(chr(10), ' | ')}")

                try:
                    # Intento 1: Clase estándar
                    score_el = row.locator("td.td-score")
                    
                    # Intento 2: Clase alternativa (a veces cambia)
                    if score_el.count() == 0:
                        score_el = row.locator("td.match-score")
                    
                    # Intento 3: Por posición (normalmente es la 3ra td: Home, Score/Time, Away)
                    # Ojo: nth-child es 1-based. Home=1, Score=2 (si no hay round visual) o 3.
                    if score_el.count() == 0:
                        # Buscamos cualquier TD que tenga un guion o dos puntos
                        tds = row.locator("td")
                        for k in range(tds.count()):
                            txt = tds.nth(k).inner_text().strip()
                            if ("-" in txt or ":" in txt) and len(txt) < 10: # Score or Time
                                score_el = tds.nth(k)
                                break
                    
                    if score_el.count() > 0:
                        score_text = score_el.first.inner_text().strip()
                        print(f"    [DEBUG] Fila {j}: {home_team} vs {away_team} | ScoreDetected='{score_text}'")
                        
                        # Cualquier cosa que parezca numérico + separador + numérico
                        nums = re.findall(r'\d+', score_text)
                        
                        if len(nums) >= 2 and ":" not in score_text:
                            is_played = True
                            home_goals = int(nums[0])
                            away_goals = int(nums[1])
                        elif "-" in score_text and ":" not in score_text:
                             is_played = True
                             parts = score_text.split("-")
                             if len(parts) == 2:
                                home_goals = safe_int(parts[0])
                                away_goals = safe_int(parts[1])
                    else:
                        print(f"    ⚠️ [DEBUG] No se encontró celda de score para {home_team} vs {away_team}")

                except Exception as e:
                    print(f"    ⚠️ Error leyendo score: {e}")

                # Referee
                referee_name = ""
                try:
                    ref_loc = row.locator("td.td-round .referee-name")
                    if ref_loc.count() > 0:
                        referee_name = ref_loc.inner_text().strip()
                except: pass

                events = []
                stats = {}
                ratings = {"home": [], "away": []}

                # --- EXTRAER DETALLES ---
                if is_played:
                    try:
                        print(f"    🔍 Extrayendo detalles para {home_team} vs {away_team}...")
                        handle_popups(page)
                        
                        # INTENTO DE CLICK MEJORADO
                        # 1. Clicar específicamente en el score, suele ser más efectivo
                        target_click = row.locator("td.td-score, td.match-score").first
                        if target_click.count() == 0: target_click = row # Fallback a fila completa
                        
                        try:
                            target_click.click(timeout=1000) # Clic normal primero (dispara eventos JS mejor)
                        except:
                            target_click.click(force=True) # Fallback force
                        
                        # Esperar carga del modal
                        try:
                            # Esperar selector específico de contenido para confirmar carga
                            page.wait_for_selector(".modal-content table.table-match-events", state="visible", timeout=4000)
                        except:
                            # Si falla, verificar si abrió al menos el contenedor del modal
                            if page.locator(".modal-content").is_visible():
                                pass # Abrió pero quizas no hay eventos (0-0 sin tarjetas)
                            else:
                                # Reintentar click si no abrió nada
                                print("      ⚠️ Modal no abrió. Reintentando click...")
                                target_click.click(force=True)
                                page.wait_for_selector(".modal-content", state="visible", timeout=3000)
                            
                        # --- EVENTOS (Lógica Iconos Clasica) ---
                        event_rows = page.locator(".modal-content table.table-match-events tbody tr")
                        item_count = event_rows.count()
                        print(f"      - Eventos encontrados: {item_count}")

                        for e_idx in range(item_count):
                            e_row = event_rows.nth(e_idx)
                            
                            min_loc = e_row.locator(".td-event-home-minute, .td-event-away-minute").first
                            if min_loc.count() == 0: continue
                            minute = safe_int(min_loc.inner_text())

                            h_icon = e_row.locator(".td-event-home-icon").inner_html() if e_row.locator(".td-event-home-icon").count() > 0 else ""
                            a_icon = e_row.locator(".td-event-away-icon").inner_html() if e_row.locator(".td-event-away-icon").count() > 0 else ""
                            full_icon = (h_icon + a_icon).lower()

                            event_type = "other"
                            # Mapeo exhaustivo
                            if "goal" in full_icon: event_type = "goal"
                            elif "card-yellow" in full_icon or "yellowcard" in full_icon: event_type = "yellow_card"
                            elif "card-red" in full_icon or "redcard" in full_icon: event_type = "red_card"
                            elif "injury" in full_icon: event_type = "injury"
                            elif "substitution" in full_icon or "sub" in full_icon: event_type = "substitution"
                            elif "missed" in full_icon or "penalty" in full_icon: event_type = "missed_penalty"

                            team_side = "home" if e_row.locator("td.td-event-home-names div").count() > 0 else "away"

                            player_name = ""
                            if e_row.locator(".semi-bold").count() > 0:
                                player_name = e_row.locator(".semi-bold").first.inner_text().strip()
                            else:
                                detail_loc = e_row.locator(f"td.td-event-{team_side}-names div div:not(.semi-bold)")
                                if detail_loc.count() > 0:
                                    player_name = detail_loc.first.inner_text().strip()

                            events.append({
                                "minute": minute,
                                "type": event_type,
                                "side": team_side,
                                "player": player_name
                            })

                        # --- ESTADÍSTICAS ---
                        stat_rows = page.locator("#table-match-statistics > tbody > tr")
                        for s_idx in range(stat_rows.count()):
                            s_row = stat_rows.nth(s_idx)
                            if s_row.locator(".td-match-stat-title").count() > 0:
                                title = s_row.locator(".td-match-stat-title").inner_text().strip()
                                stats[title] = {
                                    "home": s_row.locator(".td-match-stat-home").inner_text().strip(),
                                    "away": s_row.locator(".td-match-stat-away").inner_text().strip()
                                }
                        
                        # --- CERRAR MODAL ---
                        closed = False
                        close_btn = page.locator(".close-button-container button.close, .modal.in button.close, [data-dismiss='modal']")
                        if close_btn.count() > 0 and close_btn.first.is_visible():
                            close_btn.first.click(timeout=1000, force=True)
                            closed = True
                        
                        if not closed: page.keyboard.press("Escape")
                        
                        try:
                            page.wait_for_selector(".modal-content", state="hidden", timeout=2000)
                        except:
                            page.keyboard.press("Escape")

                    except Exception as e:
                        print(f"    ⚠️ Error detalles: {e}")
                        page.keyboard.press("Escape")

                matches_list.append({
                    "round": round_number,
                    "home_team": home_team,
                    "home_manager": home_manager,
                    "away_team": away_team,
                    "away_manager": away_manager,
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "is_played": is_played,
                    "referee": referee_name,
                    "referee_strictness": "Unknown",
                    "events": events,
                    "statistics": stats,
                    "ratings": ratings
                })

            return matches_list

        # --- LOOP PRINCIPAL ---
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
                # Siempre scrapeamos la actual (que debería ser el último resultado)
                round_data = scrape_current_round(page)
                league_matches.extend(round_data)

                # Deduplicate
                unique_lm = []
                seen_local = set()
                for m in league_matches:
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
        print(f"❌ Error crítico en scraper: {e}")
        return []

if __name__ == "__main__":
    print("Este módulo está diseñado para ser importado.")