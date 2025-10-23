# scraper_league_data.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups

load_dotenv()


def get_league_data(page):
    """
    Extrae TANTO la clasificación general COMO los valores de equipo 
    para cada liga gestionada en un solo pase.
    
    Retorna una tupla con dos listas separadas, manteniendo el formato
    original que esperan las bases de datos:
    - all_leagues_standings: igual que get_standings_data()
    - all_leagues_squad_values: igual que get_squad_values_data()
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
                standings_table_selector = "table.table-sticky:has(th:has-text('Pts'))"
                page.wait_for_selector(standings_table_selector, timeout=40000)
                
                standings_list = []
                rows = page.locator(f"{standings_table_selector} tbody tr.clickable")
                
                for row in rows.all():
                    position = row.locator("td.td-ranking").inner_text()
                    club_name = row.locator("span.ellipsis").inner_text()
                    manager_locator = row.locator("span.text-italic")
                    manager_name = manager_locator.inner_text() if manager_locator.count() > 0 else "N/A"
                    played = row.locator("td").nth(4).inner_text()
                    won = row.locator("td").nth(6).inner_text()
                    drew = row.locator("td").nth(7).inner_text()
                    lost = row.locator("td").nth(8).inner_text()
                    points = row.locator("td").nth(9).inner_text()
                    goals_for = row.locator("td").nth(10).inner_text()
                    goals_against = row.locator("td").nth(12).inner_text()
                    goal_difference = row.locator("td.td-goaldifference").inner_text()
                    
                    standings_list.append({
                        "Position": int(position),
                        "Club": club_name,
                        "Manager": manager_name,
                        "Played": int(played),
                        "Won": int(won),
                        "Drew": int(drew),
                        "Lost": int(lost),
                        "Points": int(points),
                        "GoalsFor": int(goals_for),
                        "GoalsAgainst": int(goals_against),
                        "GoalDifference": int(goal_difference)
                    })
                
                print(f"  ✓ Clasificación extraída: {len(standings_list)} equipos")
                
                # === PARTE 2: EXTRAER VALORES DE EQUIPO ===
                print(f"  - Cambiando a la pestaña 'Squad Value'...")
                page.locator("a[href='#standings-squad']").click()
                
                squad_value_panel = page.locator("#standings-squad")
                squad_value_panel.wait_for(state="visible", timeout=40000)
                
                squad_values_list = []
                rows = squad_value_panel.locator("tbody tr.clickable")
                
                for row in rows.all():
                    position = row.locator("td.td-ranking").inner_text()
                    club_name = row.locator("span.ellipsis").inner_text()
                    manager_locator = row.locator("span.text-italic")
                    manager_name = manager_locator.inner_text() if manager_locator.count() > 0 else "N/A"
                    squad_value = row.locator("td").nth(2).locator("span.club-funds-amount").inner_text()
                    player_count = row.locator("td").nth(3).inner_text()
                    avg_value = row.locator("td").nth(4).locator("span.club-funds-amount").inner_text()
                    
                    squad_values_list.append({
                        "Position": int(position),
                        "Club": club_name,
                        "Manager": manager_name,
                        "Value": squad_value,
                        "Players": int(player_count),
                        "AverageValue": avg_value
                    })
                
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
                # Intentar tomar captura para debugging
                try:
                    page.screenshot(path=f"error_slot_{i+1}_{league_name.replace(' ', '_')}.png")
                    print(f"  📸 Captura guardada para debugging del slot {i+1}")
                except:
                    pass
                # Continuar con el siguiente slot en caso de error
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
        # Retornar tupla con errores para mantener la estructura
        return {"error": error_message}, {"error": error_message}


if __name__ == "__main__":
    print("=" * 70)
    print("🧪 SCRAPER UNIFICADO DE DATOS DE LIGAS - MODO DE PRUEBA")
    print("=" * 70)
    print("\n⚠️  NOTA: Este es el modo de prueba independiente.")
    print("    Para ejecutar realmente, necesitas:")
    print("    1. Inicializar Playwright")
    print("    2. Hacer login en OSM")
    print("    3. Pasar la página logueada a get_league_data(page)")
    print("\n📊 USO EN TU SCRIPT:")
    print("    from scraper_league_data import get_league_data")
    print("    standings_data, squad_values_data = get_league_data(page)")
    print("\n✅ FORMATO 100% COMPATIBLE con las bases de datos existentes")
    print("=" * 70)