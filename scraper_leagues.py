# scraper_leagues.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
from utils import handle_popups, login_to_osm

load_dotenv()

def get_data_from_website(page):
    """
    Función principal de scraping con lógica de espera corregida para extraer todas las ligas y sus equipos.
    """
    try:
            
        # --- FASE 2: NAVEGACIÓN A LA LISTA DE LIGAS ---
        print("Navegando a la página de la lista de ligas...")
        page.goto("https://en.onlinesoccermanager.com/LeagueTypes")
        
        # --- FASE 3: EXTRACCIÓN DE DATOS ---
        print("Esperando a que la tabla de ligas cargue...")
        page.wait_for_selector("table#leaguetypes-table tbody tr.clickable", timeout=40000)
        
        league_rows = page.locator("table#leaguetypes-table tbody tr.clickable")
        num_leagues = league_rows.count()
        print(f"Se encontraron {num_leagues} ligas. Empezando a procesar una por una...")

        all_leagues_data = []

        for i in range(num_leagues):
            # Es crucial volver a localizar las filas en cada iteración para evitar elementos "stale"
            current_row = page.locator("table#leaguetypes-table tbody tr.clickable").nth(i)
            league_name = current_row.locator("td span.semi-bold").inner_text()
            print(f"\nProcesando Liga #{i+1}: {league_name}")

            MAX_RETRIES = 3
            detail_page_loaded = False
            for attempt in range(MAX_RETRIES):
                try:
                    page.locator("table#leaguetypes-table tbody tr.clickable").nth(i).click()
                    page.wait_for_selector("table#leaguetypes-table thead th:has-text('Club')", timeout=45000)
                    print(f"  - Página de detalle cargada con éxito en el intento {attempt + 1}.")
                    detail_page_loaded = True
                    break
                except TimeoutError:
                    print(f"  - ADVERTENCIA: Intento {attempt + 1}/{MAX_RETRIES} falló para '{league_name}'.")
                    if attempt < MAX_RETRIES - 1:
                        print("    Volviendo a la página de lista para reintentar...")
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=40000)
                            page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=40000)
                        except (PlaywrightError, TimeoutError):
                            print("    ERROR: No se pudo volver a la página de lista. Saltando esta liga.")
                            break
            
            if not detail_page_loaded:
                print(f"  - ERROR CRÍTICO: Imposible cargar la página de detalle para '{league_name}'. Saltando esta liga.")
                try:
                    page.goto("https://en.onlinesoccermanager.com/LeagueTypes", timeout=30000)
                    page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=40000)
                except (PlaywrightError, TimeoutError):
                    raise Exception("Fallo catastrófico: No se pudo volver a la página de lista de ligas.")
                continue
            
            # --- LÓGICA DE EXTRACCIÓN CORREGIDA Y ROBUSTA ---
            league_details = {"league_name": league_name, "clubs": []}
            
            # --- NUEVA ESPERA INTELIGENTE ---
            # Solo esperamos los datos de valor si la liga NO es una de las especiales sin valor.
            is_special_league = "Fantasy 150" in league_name or "Fantasy Tournament" in league_name
            
            if not is_special_league:
                try:
                    print("  - Es una liga estándar. Esperando a que todos los valores de equipo se rendericen...")
                    # Esta función espera hasta que TODAS las filas de la tabla tengan sus datos de valor cargados.
                    page.wait_for_function("""
                        () => {
                            const rows = document.querySelectorAll("table#leaguetypes-table tbody tr.clickable");
                            if (rows.length === 0) return false; // Aún no hay filas, seguir esperando.

                            let populatedRows = 0;
                            for (const row of rows) {
                                // Buscamos el span en la 3ª celda (Valor de Plantilla)
                                const squadValueSpan = row.querySelector('td:nth-child(3) span.club-funds-amount');
                                // La fila está poblada si el span existe y tiene contenido de texto.
                                if (squadValueSpan && squadValueSpan.innerText.trim() !== '') {
                                    populatedRows++;
                                }
                            }
                            // La condición es verdadera solo si todas las filas encontradas están pobladas y hay al menos una.
                            return populatedRows > 0 && populatedRows === rows.length;
                        }
                    """, timeout=25000) # Timeout de 25 segundos
                    print("  - Todos los valores de equipo se han cargado.")
                except TimeoutError:
                    print(f"  - ADVERTENCIA: Timeout esperando los valores para '{league_name}'. Se procederá con los datos disponibles (pueden ser incompletos).")
            else:
                print("  - Es una liga Fantasy. No se esperarán valores de equipo.")


            club_rows = page.locator("table#leaguetypes-table tbody tr.clickable")
            
            for club_row in club_rows.all():
                try:
                    club_name = club_row.locator("td").nth(0).locator("span[data-bind*='text: name']").inner_text()
                    objective = club_row.locator("td").nth(1).inner_text()
                    
                    squad_value_elements = club_row.locator("td").nth(2).locator("span.club-funds-amount").all()
                    fixed_income_elements = club_row.locator("td").nth(3).locator("span.club-funds-amount").all()
                    
                    squad_value = squad_value_elements[0].inner_text() if squad_value_elements else "N/A"
                    fixed_income = fixed_income_elements[0].inner_text() if fixed_income_elements else "N/A"
                    
                    league_details["clubs"].append({
                        "club": club_name, "objective": objective,
                        "squad_value": squad_value, "fixed_income": fixed_income
                    })
                except Exception as e:
                    print(f"  - ADVERTENCIA: Saltando una fila de club en '{league_name}'. Error: {e}")
                    continue
            
            print(f"Se extrajeron datos de {len(league_details['clubs'])} clubes.")
            all_leagues_data.append(league_details)
            
            print("Volviendo a la lista de ligas...")
            try:
                page.go_back(timeout=30000)
                page.wait_for_selector("table#leaguetypes-table tbody tr.clickable", timeout=40000)
            except (PlaywrightError, TimeoutError) as e:
                    raise Exception(f"Fallo catastrófico al volver a la lista de ligas: {e}")

        return all_leagues_data

    except Exception as e: # Captura general para cualquier otro error
        error_message = f"Ocurrió un error inesperado CRÍTICO: {e}"
        print(error_message)
        try:
            page.screenshot(path="error_screenshot.png")
            print("Se ha guardado una captura de pantalla en 'error_screenshot.png'.")
        except Exception as screenshot_error:
            print(f"No se pudo tomar la captura de pantalla. Error: {screenshot_error}")
        return {"error": error_message}
    

# Bloque para probar este script de forma independiente
if __name__ == "__main__":
    print("Ejecutando el scraper en modo de prueba...")
    data = get_data_from_website()
    if data and "error" not in data:
         with open("data.json", "w", encoding="utf-8") as f:
            # Envolvemos los datos en la estructura que espera la API
            json.dump({"data": data, "last_updated": time.time()}, f, ensure_ascii=False, indent=4)
         print("\n--- DATOS DE PRUEBA GUARDADOS EN 'data.json' ---")
    else:
        print("\n--- NO SE OBTUVIERON DATOS O HUBO UN ERROR ---")
    print(data)
    print("-------------------------")
