# scraper_league_details.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int

load_dotenv()

def get_league_data(page):
    """
    Extrae TANTO la clasificación general COMO los valores de equipo 
    para cada liga gestionada en un solo pase.
    
    CORREGIDO: Detecta equipos no 'clickable' (campeones/propios) y asegura Managers.
    """
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        LEAGUE_TABLE_URL = "https://en.onlinesoccermanager.com/League/Standings"
        
        all_leagues_standings = []
        all_leagues_squad_values = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
            
            # Navegar de vuelta al dashboard si es necesario
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            
            try:
                page.wait_for_selector(".career-teamslot", timeout=35000)
            except:
                print("No se encontraron slots de carrera. Posible error de carga.")
                break

            handle_popups(page)

            slot = page.locator(".career-teamslot").nth(i)

            # Verificar si el slot está vacío
            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando equipo: {team_name} en la liga {league_name}")

            # Hacer clic en el slot para activar ese equipo
            slot.click()
            page.wait_for_selector("#timers", timeout=45000)
            handle_popups(page)
            
            # --- EXTRAER DATOS DE LA LIGA ---
            try:
                print(f"  - Navegando a la página de clasificación...")
                page.goto(LEAGUE_TABLE_URL)
                
                # === PARTE 1: EXTRAER CLASIFICACIÓN GENERAL ===
                print(f"  - Extrayendo clasificación general...")
                
                # Asegurar pestaña General
                try:
                    page.locator("a[href='#standings-total']").click()
                    time.sleep(1)
                except: pass

                standings_table_selector = "table.table-sticky:has(th:has-text('Pts'))"
                page.wait_for_selector(standings_table_selector, timeout=40000)
                
                standings_list = []
                
                # --- CORRECCIÓN CLAVE ---
                # Usamos 'tbody tr' en lugar de 'tbody tr.clickable' para no perder tu equipo
                rows = page.locator(f"{standings_table_selector} tbody tr")
                
                for row in rows.all():
                    if not row.is_visible(): continue
                    try:
                        # Verificamos que sea una fila válida buscando la celda de ranking
                        if row.locator("td.td-ranking").count() == 0: continue

                        position = row.locator("td.td-ranking").inner_text()
                        club_name = row.locator("span.ellipsis").inner_text()
                        
                        manager_locator = row.locator("span.text-italic")
                        manager_name = manager_locator.inner_text() if manager_locator.count() > 0 else "N/A"
                        
                        # Usamos índices fijos para las columnas estadísticas
                        cols = row.locator("td")
                        
                        standings_list.append({
                            "Position": safe_int(position),
                            "Club": club_name,
                            "Manager": manager_name,
                            "Played": safe_int(cols.nth(4).inner_text()),
                            "Won": safe_int(cols.nth(6).inner_text()),
                            "Drew": safe_int(cols.nth(7).inner_text()),
                            "Lost": safe_int(cols.nth(8).inner_text()),
                            "Points": safe_int(cols.nth(9).inner_text()),
                            "GoalsFor": safe_int(cols.nth(10).inner_text()),
                            "GoalsAgainst": safe_int(cols.nth(12).inner_text()),
                            "GoalDifference": safe_int(row.locator("td.td-goaldifference").inner_text())
                        })
                    except Exception as e:
                        # print(f"  - Saltando fila irrelevante o error menor: {e}")
                        continue
                
                # Ordenamos por si el DOM no estaba en orden
                standings_list.sort(key=lambda x: x["Position"])
                print(f"  ✓ Clasificación extraída: {len(standings_list)} equipos")
                
                # === PARTE 2: EXTRAER VALORES DE EQUIPO ===
                print(f"  - Cambiando a la pestaña 'Squad Value'...")
                page.locator("a[href='#standings-squad']").click()
                
                squad_value_panel = page.locator("#standings-squad")
                squad_value_panel.wait_for(state="visible", timeout=40000)
                
                squad_values_list = []
                
                # --- CORRECCIÓN CLAVE ---
                # Igual aquí, quitamos .clickable
                rows = squad_value_panel.locator("tbody tr")
                
                for row in rows.all():
                    if not row.is_visible(): continue
                    try:
                        if row.locator("td.td-ranking").count() == 0: continue

                        position = safe_int(row.locator("td.td-ranking").inner_text())
                        club_name = row.locator("span.ellipsis").inner_text()
                        
                        # MANTENEMOS EL CAMPO MANAGER QUE FALTABA EN LA VERSIÓN NUEVA
                        manager_locator = row.locator("span.text-italic")
                        manager_name = manager_locator.inner_text() if manager_locator.count() > 0 else "N/A"
                        
                        cols = row.locator("td")
                        
                        squad_value = cols.nth(2).locator("span.club-funds-amount").inner_text()
                        player_count = safe_int(cols.nth(3).inner_text())
                        avg_value = cols.nth(4).locator("span.club-funds-amount").inner_text()
                        
                        squad_values_list.append({
                            "Position": position,
                            "Club": club_name,
                            "Manager": manager_name,  # Restaurado
                            "Value": squad_value,
                            "Players": player_count,
                            "AverageValue": avg_value
                        })
                    except Exception as e:
                        continue
                
                squad_values_list.sort(key=lambda x: x["Position"])
                print(f"  ✓ Valores de equipo extraídos: {len(squad_values_list)} equipos")
                
                # === PARTE 3: AGREGAR A LAS LISTAS SEPARADAS (FORMATO ORIGINAL) ===
                all_leagues_standings.append({
                    "team_name": team_name,
                    "league_name": league_name,
                    "standings": standings_list
                })
                
                all_leagues_squad_values.append({
                    "team_name": team_name,
                    "league_name": league_name,
                    "squad_values_ranking": squad_values_list
                })

            except (TimeoutError, PlaywrightError) as e:
                print(f"  ❌ ERROR al procesar datos para '{team_name}' en '{league_name}'. Saltando. Error: {e}")
                # Intentar tomar captura para debugging (MANTENIDO)
                try:
                    page.screenshot(path=f"error_slot_{i+1}_{league_name.replace(' ', '_')}.png")
                    print(f"  📸 Captura guardada para debugging del slot {i+1}")
                except:
                    pass
                continue

        print(f"\n✅ Proceso completado. Se extrajeron datos de {len(all_leagues_standings)} ligas.")
        return all_leagues_standings, all_leagues_squad_values

    except Exception as e:
        error_message = f"❌ Error crítico inesperado: {e}"
        print(error_message)
        try:
            page.screenshot(path="error_league_data_critical.png")
            print("📸 Captura de pantalla guardada en 'error_league_data_critical.png'.")
        except Exception as screenshot_error:
            print(f"No se pudo tomar la captura de pantalla. Error: {screenshot_error}")
        
        # MANTENIDO EL RETORNO DE ERRORES ORIGINAL
        return {"error": error_message}, {"error": error_message}

if __name__ == "__main__":
    print("Este módulo está diseñado para ser importado.")