# scraper_values.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
from utils import handle_popups, login_to_osm

load_dotenv()



def get_squad_values_data():
    """
    Extrae la clasificación por valor de equipo para cada liga gestionada.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        LEAGUE_TABLE_URL = "https://en.onlinesoccermanager.com/League/Standings"

        try:
            # --- FASE 1: LOGIN ---
            if not login_to_osm(page):
                raise Exception("El proceso de login falló. Abortando el scraper.")

            # --- FASE 2: BUCLE POR CADA SLOT ACTIVO ---
            all_leagues_squad_values = []
            NUM_SLOTS = 4

            for i in range(NUM_SLOTS):
                print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
                
                if page.url != MAIN_DASHBOARD_URL:
                    page.goto(MAIN_DASHBOARD_URL)
                page.wait_for_selector(".career-teamslot", timeout=45000)
                handle_popups(page)

                slot = page.locator(".career-teamslot").nth(i)

                if slot.locator("h2.clubslot-main-title").count() == 0:
                    print(f"Slot #{i + 1} está vacío. Saltando.")
                    continue

                team_name = slot.locator("h2.clubslot-main-title").inner_text()
                # Extraemos el nombre de la liga del dashboard para usarlo como identificador
                league_name_on_dashboard = slot.locator("h4.display-name").inner_text()
                print(f"Procesando equipo: {team_name} en la liga {league_name_on_dashboard}")

                slot.click()
                page.wait_for_selector("#timers", timeout=40000)
                handle_popups(page)
                
                # --- FASE 3: NAVEGAR Y EXTRAER VALORES DE EQUIPO ---
                try:
                    print(f"  - Navegando a la clasificación de la liga...")
                    page.goto(LEAGUE_TABLE_URL)
                    
                    print("  - Cambiando a la pestaña 'Squad Value'...")
                    page.locator("a[href='#standings-squad']").click()
                    
                    squad_value_panel = page.locator("#standings-squad")
                    squad_value_panel.wait_for(state="visible", timeout=40000)
                    print("  - Tabla de valores visible.")

                    squad_values_list = []
                    rows = squad_value_panel.locator("tbody tr.clickable")
                    
                    for row in rows.all():
                        # Usamos los selectores correctos basados en el nuevo HTML
                        position = row.locator("td.td-ranking").inner_text()
                        club_name = row.locator("span.ellipsis").inner_text()
                        manager_locator = row.locator("span.text-italic")
                        if manager_locator.count() > 0:
                            manager_name = manager_locator.inner_text()
                        else:
                            manager_name = "N/A" # Asignar un valor por defecto si no hay mánager
                        # --- FIN DE LA CORRECCIÓN ---
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
                    
                    all_leagues_squad_values.append({
                        "league_name": league_name_on_dashboard,
                        "squad_values_ranking": squad_values_list
                    })
                    print(f"  - Se extrajeron los datos de valor de {len(squad_values_list)} equipos.")

                except (TimeoutError, PlaywrightError) as e:
                    print(f"  - ERROR al procesar los valores para '{team_name}'. Saltando. Error: {e}")

            return all_leagues_squad_values

        except Exception as e:
            error_message = f"Ocurrió un error inesperado CRÍTICO: {e}"
            print(error_message)
            try:
                page.screenshot(path="error_valores.png")
                print("Se ha guardado una captura de pantalla en 'error_valores.png'.")
            except Exception as screenshot_error:
                print(f"No se pudo tomar la captura de pantalla. Error: {screenshot_error}")
            return {"error": error_message}
        finally:
            print("\nProceso de valores de equipo completado. Cerrando el navegador.")
            if browser.is_connected():
                browser.close()

if __name__ == "__main__":
    print("Ejecutando el scraper de valores de equipo en modo de prueba...")
    squad_values = get_squad_values_data()
    
    if squad_values:
        with open("squad_values_output.json", "w", encoding="utf-8") as f:
            json.dump(squad_values, f, ensure_ascii=False, indent=4)
        print("\n--- DATOS DE PRUEBA GUARDADOS EN 'squad_values_output.json' ---")
    else:
        print("\n--- NO SE OBTUVIERON DATOS O HUBO UN ERROR ---")
