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
        

def safe_int(value, default=0):
    """
    Intenta convertir un valor a un entero. Si falla, devuelve un valor por defecto.
    Maneja strings con comas, puntos, etc.
    """
    try:
        # Eliminar caracteres no numéricos excepto el signo menos
        clean_value = ''.join(filter(lambda i: i.isdigit() or i == '-', str(value)))
        return int(clean_value)
    except (ValueError, TypeError):
        return default
    
    
def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    value_str = value_str.lower().strip().replace(',', '')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str)
    except (ValueError, TypeError): return 0


# --- NUEVA FUNCIÓN DE LOGIN CENTRALIZADA ---
def login_to_osm(page: Page, osm_username: str, osm_password: str, max_retries: int = 3):
    """
    Proceso de login ultra-robusto v3.1 que maneja pop-ups, redirecciones,
    carga dinámica y localizadores semánticos.
    """
    print("🚀 Iniciando proceso de login ultra-robusto v3.1...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        print(f"\n--- Intento Maestro {attempt + 1}/{max_retries} ---")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            
            for step in range(15):
                current_url = page.url
                
                if SUCCESS_URLS_REGEX.search(current_url):
                    print("✅ ¡LOGIN EXITOSO! Dashboard detectado.")
                    handle_popups(page)
                    return True

                print(f"  - [Paso {step+1}] URL actual: {current_url}")

                cookie_buttons = [
                    page.get_by_role("button", name=re.compile("Accept all|Agree|Consent|OK", re.IGNORECASE)),
                    page.get_by_text("Accept all cookies", exact=False)
                ]
                for button in cookie_buttons:
                    if button.is_visible(timeout=1500):
                        print("  - [ACCIÓN] Banner de cookies genérico detectado. Aceptando...")
                        button.click()
                        time.sleep(2)
                        break
                
                if "PrivacyNotice" in current_url:
                    print("  - [ACCIÓN] Página de Privacidad. Aceptando...")
                    page.get_by_role("button", name="Accept", exact=True).click()
                    page.wait_for_load_state("domcontentloaded", timeout=60000)
                    continue

                if "Register" in current_url:
                    print("  - [ACCIÓN] Página de Registro. Navegando a Login...")
                    page.get_by_role("button", name="Log in", exact=True).click()
                    page.wait_for_url("**/Login", timeout=60000)
                    continue

                # --- ESTADO 3: PÁGINA DE LOGIN (LÓGICA MEJORADA Y AGRESIVA) ---
                if "Login" in current_url:
                    print("  - [ACCIÓN] Página de Login. Asegurando visibilidad del formulario...")
                    
                    username_input = page.locator("#manager-name")
                    password_input = page.locator("#password")
                    login_button = page.locator("button#login")

                    # 1. Espera explícita y forzada a que los elementos sean visibles
                    try:
                        username_input.wait_for(state="visible", timeout=30000)
                        password_input.wait_for(state="visible", timeout=5000)
                        login_button.wait_for(state="visible", timeout=5000)
                    except PlaywrightTimeoutError:
                        print("  - [ERROR] El formulario de login no se hizo visible a tiempo.")
                        raise # Esto forzará un reintento maestro
                    
                    print("  - Formulario confirmado. Rellenando con `fill`...")
                    # 2. Usamos `fill` que es más rápido y limpia el campo antes de escribir.
                    username_input.fill(osm_username)
                    password_input.fill(osm_password)

                    # 3. Pausa "humana" antes del clic
                    time.sleep(1)

                    print("  - Haciendo clic en el botón de login...")
                    # 4. Usamos `with page.expect_navigation()` que es la forma más
                    #    fiable de manejar acciones que causan un cambio de página.
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                        login_button.click()
                    
                    print("  - Clic realizado y navegación detectada. Re-evaluando estado...")
                    # El 'continue' nos llevará al inicio del bucle donde se comprobará la nueva URL.
                    continue
                # --- FIN DE LA SECCIÓN CORREGIDA ---
                
                print(f"  - [INFO] Estado no reconocido. Esperando...")
                time.sleep(3)

            raise PlaywrightTimeoutError("El bucle de estado interno no logró el login.")

        except Exception as e:
            print(f"  - ❌ El intento maestro {attempt + 1} falló: {str(e).splitlines()[0]}")
            try:
                page.screenshot(path=f"login_error_master_attempt_{attempt + 1}.png")
                print(f"    - Captura guardada: login_error_master_attempt_{attempt + 1}.png")
            except: pass
            
            if attempt < max_retries - 1:
                print("    - Esperando 20 segundos antes del próximo intento maestro...")
                time.sleep(20)
            else:
                print("❌ ERROR DEFINITIVO: Se alcanzó el número máximo de reintentos de login.")
                return False
                
    return False


        



