# scraper_transfers.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, expect, Error as PlaywrightError
from utils import handle_popups

load_dotenv()

def get_transfers_data(page):
    """
    Extrae el historial de transferencias con una robusta lógica de reintentos y esperas inteligentes.
    """
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TRANSFERS_URL = "https://en.onlinesoccermanager.com/Transferlist"
    
    try:
        all_teams_transfers = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
            
            # Navegar al dashboard principal para empezar limpio en cada iteración
            if not page.url.endswith("/Career"):
                page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")

            # Esperar a que los slots sean visibles
            page.locator(".career-teamslot").first.wait_for(state="visible", timeout=60000)

            slot = page.locator(".career-teamslot").nth(i)

            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            
            MAX_RETRIES = 3
            success = False
            for attempt in range(MAX_RETRIES):
                try:
                    print(f"Procesando equipo: {team_name} (Intento {attempt + 1}/{MAX_RETRIES})")
                    
                    # --- INICIO DE LA CORRECCIÓN ---
                    # 1. Hacemos clic en el slot y esperamos a que la nueva página cargue,
                    #    sin importar cuál sea la URL de destino.
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                        page.locator(".career-teamslot").nth(i).click()
                    
                    print(f"  - Clic en slot realizado. Aterrizaje en: {page.url}")
                    handle_popups(page)

                    # 2. AHORA, en lugar de verificar la URL, vamos directamente
                    #    a la página que nos interesa (la de transferencias).
                    #    Como ya estamos "dentro" del club, esta navegación funcionará.
                    print(f"  - Navegando directamente a {TRANSFERS_URL}...")
                    page.goto(TRANSFERS_URL, wait_until="domcontentloaded", timeout=60000)
                    for _ in range(3):
                        handle_popups(page)
                        time.sleep(1)
                    
                    page.locator("a[href='#transfer-history']").click()
                    
                    # Esperar a que la tabla sea visible
                    history_table = page.locator("#transfer-history table.table")
                    expect(history_table).to_be_visible(timeout=30000)
                    print("  - Contenido inicial del historial visible.")

                    # --- INICIO DE LA LÓGICA DE CARGA MEJORADA ---
                    print("  - Cargando todos los registros...")
                    more_button = page.locator('button:has-text("More transfers")')
                    
                    while more_button.is_visible(timeout=5000):
                        old_row_count = history_table.locator("tbody tr").count()
                        more_button.click()
                        
                        # Bucle de espera manual
                        # Intentaremos hasta 10 veces (20 segundos en total) a que las filas aumenten
                        wait_success = False
                        for _ in range(10): # 10 reintentos
                            time.sleep(2) # Espera 2 segundos entre cada comprobación
                            new_row_count = history_table.locator("tbody tr").count()
                            if new_row_count > old_row_count:
                                print(f"    - Cargadas {new_row_count - old_row_count} filas más (Total: {new_row_count})")
                                wait_success = True
                                break # Salir del bucle de espera si las filas aumentaron
                        
                        if not wait_success:
                            print("    - El botón 'More transfers' fue presionado pero no cargó más filas después de 20 segundos. Asumiendo que se ha cargado todo.")
                            break # Salir del bucle principal 'while'

                    print("  - Todos los registros cargados.")
                    # --- FIN DE LA LÓGICA DE CARGA MEJORADA ---
                    
                    print("  - Extrayendo datos de la tabla (modo optimizado)...")
                    transfers_list = page.evaluate("""
                        () => {
                            const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                            return rows.map(row => {
                                const tds = row.querySelectorAll("td");
                                // La estructura de columnas real es diferente a la de la otra tabla
                                return {
                                    Name: tds[0]?.innerText.trim() || "N/A", From: tds[1]?.innerText.trim() || "N/A",
                                    To: tds[2]?.innerText.trim() || "N/A", Position: tds[3]?.innerText.trim() || "N/A",
                                    Gameweek: tds[4]?.innerText.trim() || "N/A", Value: tds[5]?.innerText.trim() || "N/A",
                                    Price: tds[6]?.innerText.trim() || "N/A", Date: tds[7]?.innerText.trim() || "N/A"
                                };
                            });
                        }
                    """)

                    all_teams_transfers.append({"team_name": team_name, "transfers": transfers_list})
                    print(f"  - ¡ÉXITO! Se extrajeron {len(transfers_list)} fichajes para {team_name}.")
                    success = True
                    break

                except (TimeoutError, PlaywrightError) as e:
                    print(f"  - ERROR en el intento {attempt + 1}: {e}")
                    if attempt < MAX_RETRIES - 1:
                        print("    -> Volviendo al dashboard para reintentar...")
                        page.goto(MAIN_DASHBOARD_URL)
                    else:
                        print(f"  - Todos los reintentos para '{team_name}' han fallado. Saltando este equipo.")
            
        return all_teams_transfers

    except Exception as e:
        error_message = f"Ocurrió un error inesperado CRÍTICO: {e}"
        print(error_message)
        try:
            page.screenshot(path="error_fichajes.png")
            print("Se ha guardado una captura de pantalla en 'error_fichajes.png'.")
        except Exception as screenshot_error:
            print(f"No se pudo tomar la captura de pantalla. Error: {screenshot_error}")
        return {"error": error_message}
    

if __name__ == "__main__":
    print("Ejecutando el scraper de fichajes en modo de prueba...")
    fichajes = get_transfers_data()
    
    if fichajes and "error" not in fichajes:
        with open("fichajes_test_output.json", "w", encoding="utf-8") as f:
            json.dump(fichajes, f, ensure_ascii=False, indent=4)
        print("\n--- DATOS DE PRUEBA GUARDADOS EN 'fichajes_test_output.json' ---")
    else:
        print("\n--- NO SE OBTUVIERON DATOS O HUBO UN ERROR ---")
