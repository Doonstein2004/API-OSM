# scraper_table.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups


load_dotenv()



def get_standings_data(page):
    """
    Extrae la tabla de clasificación general para cada liga gestionada.
    """
    try:
        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        LEAGUE_TABLE_URL = "https://en.onlinesoccermanager.com/League/Standings"
        # --- FASE 1: BUCLE POR CADA SLOT ACTIVO ---
        all_leagues_standings = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
            
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            page.wait_for_selector(".career-teamslot", timeout=35000)
            handle_popups(page)

            slot = page.locator(".career-teamslot").nth(i)

            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name_on_dashboard = slot.locator("h4.display-name").inner_text()
            print(f"Procesando equipo: {team_name} en la liga {league_name_on_dashboard}")

            slot.click()
            page.wait_for_selector("#timers", timeout=45000)
            handle_popups(page)
            
            # --- FASE 3: NAVEGAR Y EXTRAER CLASIFICACIÓN ---
            try:
                print(f"  - Navegando a la clasificación de la liga...")
                page.goto(LEAGUE_TABLE_URL)
                
                # --- CORRECCIÓN FINAL ---
                # Usamos un selector que busca la tabla que CONTIENE el encabezado 'Pts'
                standings_table_selector = "table.table-sticky:has(th:has-text('Pts'))"
                
                page.wait_for_selector(standings_table_selector, timeout=40000)
                print("  - Tabla de clasificación visible.")

                standings_list = []
                rows = page.locator(f"{standings_table_selector} tbody tr.clickable")
                # --- FIN DE LA CORRECCIÓN ---
                
                for row in rows.all():
                    position = row.locator("td.td-ranking").inner_text()
                    club_name = row.locator("span.ellipsis").inner_text()
                    manager_name_locator = row.locator("span.text-italic")
                    manager_name = manager_name_locator.inner_text() if manager_name_locator.count() > 0 else "N/A"
                    played = row.locator("td").nth(4).inner_text()
                    won = row.locator("td").nth(6).inner_text()
                    drew = row.locator("td").nth(7).inner_text()
                    lost = row.locator("td").nth(8).inner_text()
                    points = row.locator("td").nth(9).inner_text()
                    goals_for = row.locator("td").nth(10).inner_text()
                    goals_against = row.locator("td").nth(12).inner_text()
                    goal_difference = row.locator("td.td-goaldifference").inner_text()
                    
                    standings_list.append({
                        "Position": int(position), "Club": club_name, "Manager": manager_name,
                        "Played": int(played), "Won": int(won), "Drew": int(drew),
                        "Lost": int(lost), "Points": int(points), "GoalsFor": int(goals_for),
                        "GoalsAgainst": int(goals_against), "GoalDifference": int(goal_difference)
                    })
                
                all_leagues_standings.append({
                    "league_name": league_name_on_dashboard,
                    "standings": standings_list
                })
                print(f"  - Se extrajo la clasificación de {len(standings_list)} equipos.")

            except (TimeoutError, PlaywrightError) as e:
                print(f"  - ERROR al procesar la clasificación para '{team_name}'. Saltando. Error: {e}")

        return all_leagues_standings

    except Exception as e:
        error_message = f"Ocurrió un error inesperado CRÍTICO: {e}"
        print(error_message)
        try:
            page.screenshot(path="error_clasificacion.png")
            print("Se ha guardado una captura de pantalla en 'error_clasificacion.png'.")
        except Exception as screenshot_error:
            print(f"No se pudo tomar la captura de pantalla. Error: {screenshot_error}")
        return {"error": error_message}


if __name__ == "__main__":
    print("Ejecutando el scraper de clasificación en modo de prueba...")
    standings = get_standings_data()
    
    if standings:
        with open("standings_output.json", "w", encoding="utf-8") as f:
            json.dump(standings, f, ensure_ascii=False, indent=4)
        print("\n--- DATOS DE PRUEBA GUARDADOS EN 'standings_output.json' ---")
    else:
        print("\n--- NO SE OBTUVIERON DATOS O HUBO UN ERROR ---")
