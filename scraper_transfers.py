# scraper_transfers.py
import os
import time
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
from utils import handle_popups, login_to_osm

load_dotenv()

        

def get_transfers_data():
    """
    Extrae el historial de transferencias, manejando pop-ups y la visibilidad de elementos.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
        TRANSFERS_URL = "https://en.onlinesoccermanager.com/Transferlist"

        try:
            # --- FASE 1: LOGIN (sin cambios) ---
            if not login_to_osm(page):
                raise Exception("El proceso de login falló. Abortando el scraper.")
            
            # Limpiamos cualquier pop-up que pueda aparecer justo después de iniciar sesión
            handle_popups(page)

            # --- FASE 2: BUCLE PRINCIPAL POR CADA SLOT ---
            all_teams_transfers = []
            NUM_SLOTS = 4

            for i in range(NUM_SLOTS):
                print(f"\n--- Analizando Slot de Equipo #{i + 1} ---")
                
                if page.url != MAIN_DASHBOARD_URL:
                    page.goto(MAIN_DASHBOARD_URL)
                page.wait_for_selector(".career-teamslot", timeout=40000)

                slot = page.locator(".career-teamslot").nth(i)

                if slot.locator("h2.clubslot-main-title").count() == 0:
                    print(f"Slot #{i + 1} está vacío. Saltando.")
                    continue

                team_name = slot.locator("h2.clubslot-main-title").inner_text()
                print(f"Procesando equipo: {team_name}")

                # 1. Hacemos clic para establecer el contexto
                slot.click()
                
                # 2. Esperamos a que la página del equipo cargue
                page.wait_for_selector("#timers", timeout=15000)
                
                # 3. Limpiamos pop-ups que puedan aparecer al entrar al club
                handle_popups(page)
                
                # 4. Navegamos DIRECTAMENTE a la página de transferencias
                page.goto(TRANSFERS_URL)
                
                page.wait_for_selector("a[href='#transfer-history']", timeout=40000)
                
                handle_popups(page)

                try:
                    # --- LÓGICA CORREGIDA ---
                    # 1. PRIMERO, hacemos clic en la pestaña "Transfers".
                    print("  - Navegando al historial de transferencias...")
                    page.locator("a[href='#transfer-history']").click()
                    time.sleep(5)  # Esperamos un poco para que el panel tenga tiempo de activarse
                    
                    # 2. AHORA, esperamos a que el panel del historial sea visible.
                    page.wait_for_selector("#transfer-history.active", timeout=40000)
                    print("  - Panel de historial visible.")
                    # --- FIN DE LA CORRECCIÓN ---

                    print("  - Cargando todos los registros...")
                    while page.locator('button:has-text("More transfers")').is_visible(timeout=8000):
                        old_count = page.locator("#transfer-history table.table tbody tr").count()
                        page.locator('button:has-text("More transfers")').click()
                        page.wait_for_function(
                            f"document.querySelectorAll('#transfer-history table.table tbody tr').length > {old_count}",
                            timeout=10000
                        )
                    print("  - Todos los registros cargados.")

                    print("  - Extrayendo datos de la tabla (modo optimizado)...")

                    # Espera que la tabla esté totalmente cargada
                    page.wait_for_selector("#transfer-history table.table tbody tr", timeout=50000)

                    # Ejecutamos un script JS dentro del navegador que recorre todas las filas
                    transfers_list = page.evaluate("""
                    () => {
                        const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                        return rows.map(row => {
                            const tds = row.querySelectorAll("td");
                            return {
                                Name: tds[0]?.innerText.trim() || "N/A",
                                From: tds[1]?.innerText.trim() || "N/A",
                                To: tds[2]?.innerText.trim() || "N/A",
                                Position: tds[3]?.innerText.trim() || "N/A",
                                Gameweek: tds[4]?.innerText.trim() || "N/A",
                                Value: tds[5]?.innerText.trim() || "N/A",
                                Price: tds[6]?.innerText.trim() || "N/A",
                                Date: tds[7]?.innerText.trim() || "N/A"
                            };
                        });
                    }
                    """)

                    print(f"  - Se extrajeron {len(transfers_list)} fichajes para {team_name}.")
                    
                    # ✅ Agregamos la lista al conjunto total
                    all_teams_transfers.append({
                        "team_name": team_name,
                        "transfers": transfers_list
                    })

                except (TimeoutError, PlaywrightError) as e:
                    print(f"  - ERROR al procesar los detalles de '{team_name}'. Saltando. Error: {e}")
            
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
        finally:
            print("\nProceso de fichajes completado. Cerrando el navegador.")
            browser.close()
            
            

# Bloque para probar este script de forma independiente
if __name__ == "__main__":
    print("Ejecutando el scraper de fichajes en modo de prueba...")
    fichajes = get_transfers_data()
    
    if fichajes and "error" not in fichajes:
        with open("fichajes_test_output.json", "w", encoding="utf-8") as f:
            json.dump(fichajes, f, ensure_ascii=False, indent=4)
        print("\n--- DATOS DE PRUEBA GUARDADOS EN 'fichajes_test_output.json' ---")
    else:
        print("\n--- NO SE OBTUVIERON DATOS O HUBO UN ERROR ---")
