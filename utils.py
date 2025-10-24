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


# --- NUEVA FUNCIÓN DE LOGIN CENTRALIZADA ---
def login_to_osm(page: Page, osm_username: str, osm_password: str, max_retries: int = 3):
    """
    Proceso de login lineal y explícito, usando expect_navigation para cada acción.
    """
    print("Iniciando proceso de login explícito...")
    ACTION_TIMEOUT = 120 * 1000

    for attempt in range(max_retries):
        try:
            print(f"--- Intento de Login {attempt + 1}/{max_retries} ---")
            
            # --- PASO 1: AVISO DE PRIVACIDAD ---
            print("  - [Paso 1/4] Navegando a la página de Login...")
            page.goto("https://en.onlinesoccermanager.com/Login", timeout=ACTION_TIMEOUT, wait_until="load")

            # La página puede redirigir. Esperamos a que la URL se estabilice.
            page.wait_for_url(re.compile(".*(Login|PrivacyNotice)"), timeout=ACTION_TIMEOUT)
            
            if "PrivacyNotice" in page.url:
                print("  - Página de Privacidad detectada. Aceptando...")
                # Usamos page.expect_navigation() para esperar a que el clic cause una recarga.
                with page.expect_navigation(timeout=ACTION_TIMEOUT, wait_until="domcontentloaded"):
                    page.get_by_role("button", name="Accept", exact=True).click()
            
            # --- PASO 2: PÁGINA DE REGISTRO (SI APARECE) ---
            if "Register" in page.url:
                print("  - Página de Registro detectada. Navegando a Login...")
                with page.expect_navigation(timeout=ACTION_TIMEOUT, wait_until="domcontentloaded"):
                    page.get_by_role("button", name="Log in", exact=True).click()

            # --- PASO 3: FORMULARIO DE LOGIN ---
            print("  - [Paso 3/4] Esperando el formulario de login...")
            expect(page).to_have_url(re.compile(".*Login"), timeout=ACTION_TIMEOUT)
            
            username_input = page.locator("#manager-name")
            expect(username_input).to_be_visible(timeout=ACTION_TIMEOUT)
            
            if not osm_username or not osm_password:
                raise Exception("Credenciales de OSM no proporcionadas.")
            
            print("  - Rellenando credenciales...")
            username_input.fill(osm_username)
            page.locator("#password").fill(osm_password)
            
            # --- PASO 4: VERIFICACIÓN FINAL ---
            print("  - [Paso 4/4] Enviando formulario y esperando redirección al dashboard...")
            with page.expect_navigation(timeout=ACTION_TIMEOUT, wait_until="domcontentloaded"):
                page.locator("#login").click()

            # Después de la navegación, la URL final DEBE ser la del dashboard.
            if "Career" not in page.url and "ChooseLeague" not in page.url:
                # Si nos redirige a Register OTRA VEZ, es el fallo definitivo por anti-bot.
                if "Register" in page.url:
                    raise Exception("Login fallido: Redirigido a /Register después de enviar credenciales (medida anti-bot).")
                else:
                    raise Exception(f"Login fallido: URL inesperada después del login: {page.url}")

            print("  - ¡LOGIN EXITOSO Y VERIFICADO!")
            handle_popups(page)
            return True

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



