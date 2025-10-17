# scraper_transfers.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils import handle_popups

load_dotenv()

def get_transfers_data(page):
    """
    Extrae el historial de transferencias con una robusta lógica de reintentos y esperas inteligentes.
    """
    try:
        all_teams_transfers = []
        NUM_SLOTS = 4

        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
            
            # Reseteamos al estado inicial en cada iteración del bucle principal
            if page.url != MAIN_DASHBOARD_URL:
                page.goto(MAIN_DASHBOARD_URL)
            page.wait_for_selector(".career-teamslot", timeout=40000)

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
                    
                    # Es crucial volver a localizar el slot en cada reintento
                    page.locator(".career-teamslot").nth(i).click()
                    page.wait_for_selector("#timers", timeout=40000)
                    handle_popups(page)
                    
                    page.goto(TRANSFERS_URL)
                    
                    time.sleep(10)
                    
                    # --- INICIO DE LA CORRECCIÓN LÓGICA ---
                    print("  - Navegando al historial de transferencias...")
                    page.locator("a[href='#transfer-history']").click()
                    
                    # ESPERA INTELIGENTE: Esperamos a que la primera fila de la tabla sea visible.
                    # Esto confirma que el JS ha renderizado el contenido inicial.
                    page.wait_for_selector("#transfer-history table.table tbody tr", timeout=15000)
                    print("  - Contenido inicial del historial visible.")
                    # --- FIN DE LA CORRECCIÓN ---

                    print("  - Cargando todos los registros...")
                    while page.locator('button:has-text("More transfers")').is_visible(timeout=5000): # Timeout más corto aquí es seguro
                        old_count = page.locator("#transfer-history table.table tbody tr").count()
                        page.locator('button:has-text("More transfers")').click()
                        # Esperamos a que el número de filas aumente, confirmando la carga
                        page.wait_for_function(
                            f"document.querySelectorAll('#transfer-history table.table tbody tr').length > {old_count}",
                            timeout=10000
                        )
                        time.sleep(2)
                    print("  - Todos los registros cargados.")

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
