from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Definimos una excepción personalizada
class InvalidCredentialsError(Exception):
    pass

def handle_popups(page: Page):
    """
    Versión optimizada: Elimina bloqueadores agresivamente mediante CSS y JS.
    """
    # 1. Inyectamos CSS para matar los bloqueadores conocidos de inmediato
    page.add_style_tag(content="""
        #preloader-image, .modal-backdrop, #genericModalContainer { 
            display: none !important; 
            visibility: hidden !important; 
            pointer-events: none !important; 
        }
    """)
    
    # 2. Eliminación física del DOM (doble seguridad)
    page.evaluate("""
        document.querySelectorAll('#preloader-image, .modal-backdrop, #genericModalContainer').forEach(el => el.remove());
    """)

    popups_to_close = [
        {"name": "Recompensa", "selector": "#consumable-reward-modal-content span.bold:has-text('View later')", "force": False},
        {"name": "Anuncio", "selector": "#modal-dialog-centerpopup button.close", "force": True},
        {"name": "Custom", "selector": "#customModalContainer .close, #customModalContainer button:has-text('Close')", "force": True}
    ]

    for _ in range(3): # Reducido a 3 pasadas para mayor velocidad
        closed = False
        for popup in popups_to_close:
            try:
                closer = page.locator(popup["selector"])
                if closer.is_visible(timeout=300): # Timeout muy bajo
                    closer.click(force=popup["force"], timeout=1000)
                    closed = True
            except: continue
        if not closed: break
        

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
    print("🚀 Iniciando proceso de login ultra-robusto v3.2...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            
            for step in range(15):
                handle_popups(page) # Limpiamos antes de cada acción
                current_url = page.url

                if SUCCESS_URLS_REGEX.search(current_url):
                    print("✅ ¡LOGIN EXITOSO!")
                    return True

                # --- ACCIÓN: BANNER COOKIES ---
                try:
                    cookie_btn = page.get_by_role("button", name=re.compile("Accept all|Agree|Consent|OK", re.IGNORECASE))
                    if cookie_btn.is_visible(timeout=1000):
                        cookie_btn.click(force=True)
                except: pass

                # --- ACCIÓN: PRIVACIDAD ---
                if "PrivacyNotice" in current_url:
                    page.get_by_role("button", name="Accept", exact=True).click(force=True)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    continue

                # --- ACCIÓN: CORREGIR REDIRECCIÓN A REGISTER ---
                if "Register" in current_url:
                    print("  - [ACCIÓN] En Register. Forzando navegación a Login...")
                    # Aquí es donde fallaba: aplicamos force=True
                    page.get_by_role("button", name="Log in", exact=True).click(force=True)
                    time.sleep(1)
                    continue

                # --- ACCIÓN: LOGIN FORM ---
                if "Login" in current_url:
                    username_input = page.locator("#manager-name")
                    password_input = page.locator("#password")
                    login_button = page.locator("button#login")

                    username_input.wait_for(state="visible", timeout=10000)
                    username_input.fill(osm_username)
                    password_input.fill(osm_password)
                    
                    # Clic forzado para evitar el 'preloader-image' que detectó el log
                    login_button.click(force=True) 
                    
                    # Verificación rápida de error de credenciales
                    try:
                        error_selector = ".feedbackcontainer .feedback-message"
                        if page.locator(error_selector).is_visible(timeout=3000):
                            error_text = page.locator(error_selector).inner_text()
                            if "incorrect" in error_text.lower():
                                raise InvalidCredentialsError("Credenciales incorrectas.")
                    except PlaywrightTimeoutError: pass
                    
                    page.wait_for_url(SUCCESS_URLS_REGEX, timeout=10000)
                    continue
                
                time.sleep(1)

        except InvalidCredentialsError as e:
            raise e 
        except Exception as e:
            print(f"  - ❌ Intento {attempt + 1} falló: {e}")
            time.sleep(2)
                
    return False


        



