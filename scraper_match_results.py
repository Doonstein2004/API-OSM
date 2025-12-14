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
                # Ir a resultados
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
                
                    # --- 1. EXTRACCIÓN DE DATOS BÁSICOS (Incluido Mánagers) ---
                    # Equipos
                    home_team = row.locator("td.td-home .font-sm").inner_text().strip()
                    away_team = row.locator("td.td-away .font-sm").inner_text().strip()
                
                    # Mánagers (Lógica robusta: Si no hay texto secundario, es CPU)
                    # Local
                    home_mgr_loc = row.locator("td.td-home .text-secondary")
                    home_manager = home_mgr_loc.inner_text().strip() if home_mgr_loc.count() > 0 else "CPU"
                
                    # Visita
                    away_mgr_loc = row.locator("td.td-away .text-secondary")
                    away_manager = away_mgr_loc.inner_text().strip() if away_mgr_loc.count() > 0 else "CPU"

                    print(f"    Processing: {home_team} ({home_manager}) vs {away_team} ({away_manager})")

                    # 2. ABRIR MODAL
                    row.click(force=True)
                    
                    try:
                        # ESPERA CRÍTICA: Esperar a que el contenedor de detalles exista dentro del modal
                        # Esto garantiza que el AJAX ha terminado y los datos están ahí
                        modal = page.locator(".modal-content")
                        modal.wait_for(state="visible", timeout=10000)
                        
                        # Esperar explícitamente a que aparezca la tabla de estadísticas o eventos
                        # Si no esperamos esto, leemos el HTML vacío
                        try:
                            page.wait_for_selector("#table-match-statistics", timeout=5000)
                            # Pequeña pausa extra por seguridad en scraping masivo
                            time.sleep(1) 
                        except:
                            print("      (Nota: Parece que no cargaron las estadísticas detalladas)")

                        # --- A. EXTRAER EVENTOS ---
                        events = []
                        # Buscamos todas las filas dentro de tablas de eventos (puede haber 1er tiempo y 2do tiempo)
                        event_rows = modal.locator("table.table-match-events tbody tr")
                        
                        for e_idx in range(event_rows.count()):
                            e_row = event_rows.nth(e_idx)
                            
                            # Extraer minuto (puede estar vacío en algunos casos raros)
                            minute_loc = e_row.locator(".td-event-home-minute, .td-event-away-minute").first
                            if minute_loc.count() == 0: continue # Fila de cabecera o separador
                            minute = safe_int(minute_loc.inner_text())

                            # Iconos (Concatenamos HTML para evitar error Strict Mode)
                            home_icon = e_row.locator(".td-event-home-icon").inner_html() if e_row.locator(".td-event-home-icon").count() > 0 else ""
                            away_icon = e_row.locator(".td-event-away-icon").inner_html() if e_row.locator(".td-event-away-icon").count() > 0 else ""
                            full_icon = (home_icon + away_icon).lower()

                            event_type = "other"
                            if "icon-matchevent-goal" in full_icon or "icon-matchevent-penaltygoal" in full_icon: event_type = "goal"
                            elif "icon-player-yellowcard" in full_icon: event_type = "yellow_card"
                            elif "icon-player-redcard" in full_icon: event_type = "red_card"
                            elif "icon-player-injury" in full_icon: event_type = "injury"
                            elif "icon-matchevent-sub" in full_icon: event_type = "substitution"
                            elif "icon-matchevent-penaltymiss" in full_icon: event_type = "penalty_miss"

                            # Lado
                            has_home_name = e_row.locator("td.td-event-home-names div").count() > 0
                            team_side = "home" if has_home_name else "away"

                            # Jugador
                            player_name = ""
                            if e_row.locator(".semi-bold").count() > 0:
                                player_name = e_row.locator(".semi-bold").first.inner_text().strip()

                            # Detalle (Asistencia, causa, etc)
                            detail = ""
                            # Buscamos el div que NO es semi-bold en la celda de nombres
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
                        stats = {}
                        stat_rows = modal.locator("#table-match-statistics > tbody > tr")
                        for s_idx in range(stat_rows.count()):
                            s_row = stat_rows.nth(s_idx)
                            if s_row.locator(".td-match-stat-title").count() > 0:
                                title = s_row.locator(".td-match-stat-title").inner_text().strip()
                                home_val = s_row.locator(".td-match-stat-home").inner_text().strip()
                                away_val = s_row.locator(".td-match-stat-away").inner_text().strip()
                                stats[title] = {"home": home_val, "away": away_val}

                        # --- C. RATINGS ---
                        ratings = {"home": [], "away": []}
                        
                        # Función auxiliar para extraer ratings de una tabla
                        def extract_ratings(selector, side_key):
                            r_rows = modal.locator(f"{selector} tr")
                            for r_idx in range(r_rows.count()):
                                r_row = r_rows.nth(r_idx)
                                if r_row.locator(".semi-bold").count() > 0:
                                    p_name = r_row.locator(".semi-bold").inner_text().strip()
                                    # Buscar el grado (número) dentro del span
                                    p_grade = "0"
                                    if r_row.locator(".playergrade span").count() > 0:
                                        p_grade = r_row.locator(".playergrade span").inner_text().strip()
                                    ratings[side_key].append({"player": p_name, "grade": p_grade})

                        extract_ratings(".table-playergrades-home", "home")
                        extract_ratings(".table-playergrades-away", "away")

                        # Marcador y Managers (Recuperados del modal para mayor precisión)
                        score_text = modal.locator(".match-score").inner_text().strip()
                        home_g, away_g = 0, 0
                        if "-" in score_text:
                            parts = score_text.split("-")
                            home_g = safe_int(parts[0])
                            away_g = safe_int(parts[1])
                        
                        # Managers dentro del modal (a veces el de la lista es CPU pero aquí sale nombre)
                        home_man = "CPU"
                        away_man = "CPU"
                        # Intentar buscar managers en modal si existen
                        # (Opcional, si no se encuentran se usan los de la lista externa, aquí simplificado)

                        matches_list.append({
                            "round": round_number,
                            "home_team": home_team,
                            "home_manager": home_manager, # Se puede refinar si se quiere extraer del modal
                            "away_team": away_team,
                            "away_manager": away_manager,
                            "home_goals": home_g,
                            "away_goals": away_g,
                            "events": events,
                            "statistics": stats,
                            "ratings": ratings
                        })

                    except Exception as e:
                        print(f"    ⚠️ Error extrayendo datos del modal: {e}")

                    finally:
                        # 2. CERRAR MODAL CORRECTAMENTE
                        # Buscamos el botón específico que nos diste
                        close_btn = page.locator(".close-button-container button.close")
                        
                        if close_btn.is_visible():
                            close_btn.click()
                        else:
                            print("    ⚠️ No se encontró botón cerrar, intentando Escape...")
                            page.keyboard.press("Escape")
                        
                        # 3. Esperar a que se cierre
                        try:
                            page.wait_for_selector(".modal-content", state="hidden", timeout=5000)
                        except:
                            print("    ⚠️ El modal no se cerró, intentando click en backdrop...")
                            page.mouse.click(0, 0)
                            time.sleep(1)

                # Fin del loop de partidos
                # Ahora tenemos que "rellenar" los managers que pusimos TBD con los de la tabla original si queremos
                # O simplemente confiar en la extracción inicial. 
                # Para este ejemplo, devolvemos el objeto completo.
                
                # NOTA: En la lógica anterior, extraíamos manager de la fila principal. 
                # Como aquí hemos reconstruido el objeto matches_list, asegúrate de pasar los managers correctos
                # desde la variable 'row' al inicio del loop si los necesitas.
                
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