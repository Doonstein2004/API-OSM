import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int

load_dotenv()

def get_match_results(page):
    """
    Extrae los resultados de la jornada actual (la que muestra la pantalla por defecto).
    Retorna una lista de objetos con la info de la liga y sus partidos.
    """
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        RESULTS_URL = "https://en.onlinesoccermanager.com/League/Results"
        
        all_leagues_matches = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Resultados - Slot #{i + 1} ---")
            
            # Navegar de vuelta al dashboard si es necesario
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            page.wait_for_selector(".career-teamslot", timeout=35000)
            handle_popups(page)

            slot = page.locator(".career-teamslot").nth(i)

            # Verificar si el slot está vacío
            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando equipo: {team_name} en la liga {league_name}")

            # Hacer clic en el slot
            slot.click()
            page.wait_for_selector("#timers", timeout=45000)
            handle_popups(page)
            
            # --- EXTRAER RESULTADOS ---
            try:
                page.goto(RESULTS_URL)
                page.wait_for_selector("table.table-sticky", timeout=40000)
                handle_popups(page)
                
                # Detectar Jornada
                try:
                    round_number = safe_int(page.locator("th.text-center span[data-bind*='weekNr']").inner_text())
                except:
                    round_number = 0
                
                matches_list = []
                
                # Contar filas
                row_count = page.locator("table.table-sticky tbody tr.clickable").count()
                print(f"  - Encontrados {row_count} partidos en la jornada {round_number}.")

                for j in range(row_count):
                    row = page.locator("table.table-sticky tbody tr.clickable").nth(j)
                    row.scroll_into_view_if_needed()
                    
                    # --- 1. EXTRACCIÓN SEGURA DE DATOS BÁSICOS (ANTES DEL CLICK) ---
                    home_team = row.locator("td.td-home .font-sm").inner_text().strip()
                    away_team = row.locator("td.td-away .font-sm").inner_text().strip()
                    
                    # Mánagers: Buscamos .text-secondary. Si no existe, es "CPU"
                    # Usamos .first para evitar ambigüedades si hubiera duplicados ocultos
                    h_mgr_loc = row.locator("td.td-home .text-secondary")
                    home_manager = h_mgr_loc.first.inner_text().strip() if h_mgr_loc.count() > 0 else "CPU"
                    
                    a_mgr_loc = row.locator("td.td-away .text-secondary")
                    away_manager = a_mgr_loc.first.inner_text().strip() if a_mgr_loc.count() > 0 else "CPU"

                    print(f"    Processing: {home_team} ({home_manager}) vs {away_team} ({away_manager})")

                    # Variables para rellenar desde el modal (valores por defecto)
                    home_goals = 0
                    away_goals = 0
                    events = []
                    stats = {}
                    ratings = {"home": [], "away": []}

                    # --- 2. ABRIR MODAL ---
                    row.click(force=True)
                    
                    try:
                        # Esperar carga del modal
                        page.wait_for_selector(".modal-content", state="visible", timeout=8000)
                        
                        # Esperar datos (Statistics o Ratings) para asegurar que no leemos vacío
                        try:
                            page.wait_for_selector("#table-match-statistics", timeout=4000)
                        except:
                            pass # Si falla, intentamos leer lo que haya

                        # --- A. EXTRAER EVENTOS ---
                        event_rows = page.locator(".modal-content table.table-match-events tbody tr")
                        
                        for e_idx in range(event_rows.count()):
                            e_row = event_rows.nth(e_idx)
                            
                            # Minuto
                            min_loc = e_row.locator(".td-event-home-minute, .td-event-away-minute").first
                            if min_loc.count() == 0: continue
                            minute = safe_int(min_loc.inner_text())

                            # Iconos (Concatenado para evitar Strict Mode error)
                            h_icon = e_row.locator(".td-event-home-icon").inner_html() if e_row.locator(".td-event-home-icon").count() > 0 else ""
                            a_icon = e_row.locator(".td-event-away-icon").inner_html() if e_row.locator(".td-event-away-icon").count() > 0 else ""
                            full_icon = (h_icon + a_icon).lower()

                            event_type = "other"
                            if "icon-matchevent-goal" in full_icon or "icon-matchevent-penaltygoal" in full_icon: event_type = "goal"
                            elif "icon-player-yellowcard" in full_icon: event_type = "yellow_card"
                            elif "icon-player-redcard" in full_icon: event_type = "red_card"
                            elif "icon-player-injury" in full_icon: event_type = "injury"
                            elif "icon-matchevent-sub" in full_icon: event_type = "substitution"
                            elif "icon-matchevent-penaltymiss" in full_icon: event_type = "penalty_miss"

                            # Lado
                            team_side = "home" if e_row.locator("td.td-event-home-names div").count() > 0 else "away"

                            # Jugador
                            player_name = ""
                            if e_row.locator(".semi-bold").count() > 0:
                                player_name = e_row.locator(".semi-bold").first.inner_text().strip()

                            # Detalle
                            detail = ""
                            cause_loc = e_row.locator(f"td.td-event-{team_side}-names div div:not(.semi-bold)")
                            if cause_loc.count() > 0:
                                detail = cause_loc.first.inner_text().strip()

                            events.append({
                                "minute": minute,
                                "type": event_type,
                                "side": team_side,
                                "player": player_name,
                                "detail": detail
                            })

                        # --- B. ESTADÍSTICAS ---
                        stat_rows = page.locator("#table-match-statistics > tbody > tr")
                        for s_idx in range(stat_rows.count()):
                            s_row = stat_rows.nth(s_idx)
                            if s_row.locator(".td-match-stat-title").count() > 0:
                                title = s_row.locator(".td-match-stat-title").inner_text().strip()
                                stats[title] = {
                                    "home": s_row.locator(".td-match-stat-home").inner_text().strip(),
                                    "away": s_row.locator(".td-match-stat-away").inner_text().strip()
                                }

                        # --- C. RATINGS ---
                        def extract_ratings(cls, key):
                            rows = page.locator(f".{cls} tr")
                            for r in range(rows.count()):
                                row_r = rows.nth(r)
                                if row_r.locator(".semi-bold").count() > 0:
                                    p_name = row_r.locator(".semi-bold").inner_text().strip()
                                    p_grade = row_r.locator(".playergrade span").inner_text().strip() if row_r.locator(".playergrade span").count() > 0 else "-"
                                    ratings[key].append({"player": p_name, "grade": p_grade})

                        extract_ratings("table-playergrades-home", "home")
                        extract_ratings("table-playergrades-away", "away")

                        # Marcador final del modal
                        score_text = page.locator(".modal-content .match-score").inner_text().strip()
                        if "-" in score_text:
                            parts = score_text.split("-")
                            home_goals = safe_int(parts[0])
                            away_goals = safe_int(parts[1])

                    except Exception as e:
                        print(f"    ⚠️ Error leyendo modal: {e}")

                    finally:
                        # 3. CONSTRUIR OBJETO FINAL (Usando las variables de arriba)
                        matches_list.append({
                            "round": round_number,
                            "home_team": home_team,
                            "home_manager": home_manager, # Aquí usamos la variable extraída al principio
                            "away_team": away_team,
                            "away_manager": away_manager, # Aquí usamos la variable extraída al principio
                            "home_goals": home_goals,
                            "away_goals": away_goals,
                            "events": events,
                            "statistics": stats,
                            "ratings": ratings
                        })

                        # 4. CERRAR MODAL
                        close_btn = page.locator(".close-button-container button.close")
                        if close_btn.is_visible():
                            close_btn.click()
                        else:
                            page.keyboard.press("Escape")
                        
                        try:
                            page.wait_for_selector(".modal-content", state="hidden", timeout=3000)
                        except:
                            # Fallback click fuera
                            page.mouse.click(0, 0)
                            time.sleep(0.5)

                all_leagues_matches.append({
                    "league_name": league_name,
                    "matches": matches_list
                })

            except Exception as e:
                print(f"  ❌ Error en slot {i}: {e}")
                continue

        return all_leagues_matches
        

    except Exception as e:
        error_message = f"❌ Error crítico en scraper de resultados: {e}"
        print(error_message)
        return []

if __name__ == "__main__":
    # Bloque para testear individualmente (requiere sesión activa)
    print("Este módulo está diseñado para ser importado y recibir el objeto 'page'.")