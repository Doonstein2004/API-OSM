from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re

def handle_popups(page):
    """
    Busca y cierra una lista de pop-ups conocidos, usando clics forzados si es necesario.
    """
    # Lista de pop-ups a cerrar. Podemos añadir más aquí fácilmente en el futuro.
    # Cada diccionario contiene: el nombre (para logging), el selector del botón de cierre, y si requiere un clic forzado.
    popups_to_close = [
        {
            "name": "Pop-up de Recompensa",
            "selector": "#consumable-reward-modal-content span.bold:has-text('View later')",
            "force": False
        },
        {
            "name": "Pop-up de Anuncio/Modal Genérico",
            "selector": "#modal-dialog-centerpopup button.close",
            "force": True  # Usamos clic forzado por la posible capa <canvas>
        }
    ]

    # Hacemos varias pasadas para cerrar pop-ups que puedan aparecer en cascada
    for _ in range(5): 
        popup_closed_in_this_pass = False
        for popup in popups_to_close:
            try:
                closer = page.locator(popup["selector"])
                if closer.is_visible(timeout=500):
                    print(f"  - DETECTADO '{popup['name']}'. Cerrando...")
                    closer.click(force=popup["force"], timeout=2000)
                    popup_closed_in_this_pass = True
                    time.sleep(1) # Pausa para que la animación de cierre termine
                    break # Salimos del bucle interior para empezar el chequeo desde el principio
            except PlaywrightError:
                # Es normal que no encuentre nada, continuamos con el siguiente tipo de pop-up
                continue
        
        # Si en una pasada completa no cerramos nada, la página está limpia.
        if not popup_closed_in_this_pass:
            break
        

# --- NUEVA FUNCIÓN DE LOGIN CENTRALIZADA ---
def login_to_osm(page: Page, osm_username: str, osm_password: str, max_retries: int = 3):
    """
    Proceso de login final basado en un bucle de estado persistente que maneja todos los casos.
    """
    print("Iniciando proceso de login con bucle de estado persistente...")
    ACTION_TIMEOUT = 120 * 1000

    for attempt in range(max_retries):
        try:
            print(f"--- Intento de Login {attempt + 1}/{max_retries} ---")
            
            page.goto("https://en.onlinesoccermanager.com/Login", timeout=ACTION_TIMEOUT, wait_until="domcontentloaded")

            # Este bucle se ejecuta hasta que se resuelva el estado (éxito o fallo definitivo)
            for step in range(15): # Límite de 15 pasos para evitar bucles infinitos
                # Pausa generosa para que el JS de la página reaccione
                print(f"  - [Paso {step+1}/15] Esperando a que la página se estabilice...")
                page.wait_for_timeout(5000)
                
                current_url = page.url
                print(f"  - Verificando estado. URL actual: {current_url}")

                # --- CASOS DE ÉXITO (TERMINAN EL BUCLE) ---
                if "Career" in current_url or "ChooseLeague" in current_url:
                    print("  - ¡ESTADO: LOGIN EXITOSO! Estamos en el dashboard o selección de liga.")
                    handle_popups(page)
                    return True

                # --- CASO DE FALLO DEFINITIVO (TERMINA EL BUCLE) ---
                error_locator = page.locator('span.feedback-message:has-text("incorrect")')
                if error_locator.is_visible():
                    print("  - ¡ESTADO: FALLO DETECTADO! Credenciales incorrectas.")
                    raise Exception(f"Credenciales de OSM incorrectas para el usuario '{osm_username}'.")

                # --- CASOS DE ACCIÓN (CONTINÚAN EL BUCLE) ---

                # CASO 1: Página de Aviso de Privacidad
                privacy_button = page.get_by_role("button", name="Accept", exact=True)
                if privacy_button.is_visible():
                    print("  - ESTADO: Aviso de Privacidad. Aceptando...")
                    privacy_button.click()
                    continue # Vuelve al inicio del bucle para re-evaluar

                # CASO 2: Página de Registro
                register_login_button = page.get_by_role("button", name="Log in", exact=True)
                if "Register" in current_url and register_login_button.is_visible():
                    print("  - ESTADO: Página de Registro. Navegando a Login...")
                    register_login_button.click()
                    continue # Vuelve al inicio del bucle para re-evaluar

                # CASO 3: Página de Login (con el formulario listo)
                username_input = page.locator("#manager-name")
                if "/Login" in current_url and username_input.is_visible():
                    print("  - ESTADO: Página de Login. Rellenando y enviando formulario...")
                    
                    if not osm_username or not osm_password:
                        raise Exception("Credenciales de OSM no proporcionadas.")

                    username_input.fill(osm_username)
                    page.locator("#password").fill(osm_password)
                    page.locator("#login").click()
                    # Después del clic, no hacemos nada más. El bucle se encargará de
                    # esperar y re-evaluar la nueva página en la siguiente iteración.
                    continue

                # Si no se cumple ninguna de las condiciones anteriores, es que la página
                # todavía está cargando o en un estado intermedio. El bucle esperará y lo reintentará.
                print("  - Estado intermedio o cargando, esperando al siguiente ciclo...")

            # Si después de 15 pasos no hemos llegado a un estado final, el intento falla.
            raise Exception("El flujo de login no se pudo resolver (timeout de pasos).")

        except Exception as e:
            # Misma lógica de manejo de errores que antes
            if "Credenciales de OSM incorrectas" in str(e):
                print(f"❌ ERROR DEFINITIVO: {e}")
                return False
            
            print(f"  - ADVERTENCIA: El intento {attempt + 1} falló.")
            print(f"    - Razón: {str(e).splitlines()[0]}")
            try:
                page.screenshot(path=f"login_error_attempt_{attempt + 1}.png")
                print(f"    - Captura de pantalla guardada.")
            except: pass

            if attempt < max_retries - 1:
                print("    Reintentando en 10 segundos...")
                time.sleep(10)
            else:
                print("  - ERROR: Se alcanzó el número máximo de reintentos de login.")
                return False
    
    return False



