# scraper.py
import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError

# Cargar variables de entorno del archivo .env
load_dotenv()

def get_data_from_website():
    """
    Función principal de scraping.
    Acepta términos, navega al login, inicia sesión y extrae los datos.
    """
    with sync_playwright() as p:
        # Para depurar, usa headless=False para ver el navegador en acción.
        # Para producción, usa headless=True para que se ejecute en segundo plano.
        browser = p.chromium.launch(headless=True) # <--- Puesto en False para que veas qué pasa
        page = browser.new_page()

        try:
            # --- PASO 1A: ACEPTAR TÉRMINOS ---
            print("Navegando a la página inicial...")
            # Reemplaza con la URL inicial donde aparecen los términos
            page.goto(f"https://www.onlinesoccermanager.com/PrivacyNotice?nextUrl=%2F") 

            print("Buscando el botón 'Accept'...")
            # Usamos un selector de Playwright que busca un botón que contenga el texto "Accept".
            # Esto es muy robusto. Basado en tu HTML, este selector debería funcionar.
            accept_button = page.locator('button:has-text("Accept")')
            
            print("Haciendo clic en 'Accept'...")
            accept_button.click()

            # --- PASO 1B: IR A LA PÁGINA DE LOGIN ---
            print("Esperando a que cargue la página de registro/intermedia...")
            # Ahora, la página ha redirigido. Buscamos el botón que nos lleva al login.
            # Esperamos a que este botón sea visible antes de hacer clic.
            login_link_button = page.locator('button:has-text("Log in")')
            login_link_button.wait_for(state="visible", timeout=20000) # Espera hasta 10 seg
            
            print("Haciendo clic en el enlace 'Log in'...")
            login_link_button.click()

            # --- PASO 1C: RELLENAR CREDENCIALES ---
            print("Esperando a que cargue el formulario de login final...")
            # Esperamos a que el campo de usuario sea visible para asegurarnos de que la página cargó.
            # Usamos los 'id' que proporcionaste, ¡son los mejores selectores!
            manager_name_input = page.locator("#manager-name")
            manager_name_input.wait_for(state="visible", timeout=10000)

            print("Rellenando credenciales...")
            manager_name_input.fill(os.getenv("MI_USUARIO"))
            # El selector para la contraseña, usando su 'id'.
            page.locator("#password").fill(os.getenv("MI_CONTRASENA"))
            
            # Buscamos y hacemos clic en el botón final para iniciar sesión.
            # Este selector es un supuesto, ¡ajústalo si es necesario!
            print("Haciendo clic en el botón de login final...")
            page.locator("#login").click()

            # --- PASO 2: ESPERAR Y NAVEGAR ---
            print("Esperando confirmación de login...")
            # Esperamos a un elemento que solo existe DESPUÉS de iniciar sesión.
            # Reemplaza '#dashboard' con un selector real de tu página.
            page.wait_for_selector("#crew", timeout=30000)
            print("Login exitoso.")
            
            # ¡NUEVO! Navegamos a la página que contiene la primera tabla (la de las ligas)
            # Reemplaza esta URL con la correcta
            print("Navegando a la página de la lista de ligas...")
            page.goto("https://en.onlinesoccermanager.com/LeagueTypes")
            
            # --- PASO 3: EXTRAER DATOS DE LAS LIGAS (PATRÓN LISTA-DETALLE) ---
            print("Esperando a que la tabla de ligas cargue...")
            page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=25000)
            
            # Obtenemos el localizador de todas las filas de la tabla de ligas
            league_rows = page.locator("table#leaguetypes-table tbody tr.clickable")
            num_leagues = league_rows.count()
            print(f"Se encontraron {num_leagues} ligas. Empezando a procesar una por una...")

            all_leagues_data = []

            # Bucle principal para iterar sobre cada liga
            for i in range(num_leagues):
                # Es crucial volver a localizar las filas en cada iteración
                current_row = page.locator("table#leaguetypes-table tbody tr.clickable").nth(i)
                league_name = current_row.locator("td span.semi-bold").inner_text()
                print(f"\nProcesando Liga #{i+1}: {league_name}")

                # --- INICIO DE LA LÓGICA DE REINTENTOS ---
                MAX_RETRIES = 3
                detail_page_loaded = False
                for attempt in range(MAX_RETRIES):
                    try:
                        # Re-localizamos la fila por si la página recargó
                        page.locator("table#leaguetypes-table tbody tr.clickable").nth(i).click()
                        
                        # La espera crítica que a veces falla
                        page.wait_for_selector("table#leaguetypes-table thead th:has-text('Club')", timeout=15000)
                        
                        print(f"  - Página de detalle cargada con éxito en el intento {attempt + 1}.")
                        detail_page_loaded = True
                        break # Si tenemos éxito, salimos del bucle de reintentos
                    except TimeoutError:
                        print(f"  - ADVERTENCIA: Intento {attempt + 1}/{MAX_RETRIES} falló para '{league_name}'.")
                        if attempt < MAX_RETRIES - 1:
                            print("    Volviendo a la página de lista para reintentar...")
                            try:
                                page.go_back(wait_until="domcontentloaded", timeout=10000)
                                page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=15000)
                            except TimeoutError:
                                print("    ERROR: No se pudo volver a la página de lista. Saltando esta liga.")
                                break # Salimos del bucle de reintentos si no podemos recuperarnos
                
                # Si después de todos los reintentos no se pudo cargar, saltamos a la siguiente liga
                if not detail_page_loaded:
                    print(f"  - ERROR CRÍTICO: Imposible cargar la página de detalle para '{league_name}'. Saltando esta liga.")
                    # Intentamos volver a la página de lista una última vez para no romper el bucle principal
                    try:
                        page.goto("https://en.onlinesoccermanager.com/LeagueTypes")
                        page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=25000)
                    except TimeoutError:
                        raise Exception("Fallo catastrófico: No se pudo volver a la página de lista de ligas.")
                    continue # Pasa a la siguiente iteración del bucle `for i in range(num_leagues)`
                # --- FIN DE LA LÓGICA DE REINTENTOS ---
                
                # El resto del código solo se ejecuta si la página de detalle cargó con éxito
                league_details = {"league_name": league_name, "clubs": []}
                club_rows = page.locator("table#leaguetypes-table tbody tr.clickable")
                
                page.wait_for_selector("table#leaguetypes-table tbody tr.clickable", timeout=15000)
                
                # (El código de extracción de clubes no necesita cambios, ya es defensivo)
                for club_row in club_rows.all():
                    try:
                        club_name = club_row.locator("td").nth(0).locator("span[data-bind*='text: name']").inner_text()
                        objective = club_row.locator("td").nth(1).inner_text()
                        # Espera específica: solo si no es una liga sin valores
                        
                        if "Fantasy Tournament" not in league_name and "Fantasy 150" not in league_name:
                            # Espera dinámica: verifica que las celdas con montos tengan texto distinto de vacío
                            page.wait_for_function(
                                """(row) => {
                                    const spans = row.querySelectorAll('span.club-funds-amount');
                                    return Array.from(spans).every(span => span && span.innerText.trim() !== '');
                                }""",
                                arg=club_row,
                                timeout=8000
                            )
                            
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
                page.go_back()
                page.wait_for_selector("table#leaguetypes-table tbody tr", timeout=15000)
            
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
        finally:
            print("\nProceso completado. Cerrando el navegador.")
            browser.close()

# Bloque para probar este script de forma independiente
if __name__ == "__main__":
    print("Ejecutando el scraper en modo de prueba...")
    data = get_data_from_website()
    print("\n--- DATOS OBTENIDOS ---")
    print(data)
    print("-------------------------")