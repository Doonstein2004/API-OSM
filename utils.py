from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Definimos una excepción personalizada
class InvalidCredentialsError(Exception):
    pass

def handle_popups(page: Page):
    """
    Versión v3.3: Especializada en matar modales sociales y bloqueos de transición.
    """
    # 1. Inyectamos CSS agresivo para ocultar elementos sociales y bloqueadores
    # Añadido: .social-login-modal, #social-login-container, .facebook-login-button
    page.add_style_tag(content="""
        #preloader-image, .modal-backdrop, #genericModalContainer, 
        .social-login-modal, #social-login-container, .facebook-login-button, 
        iframe[src*="facebook"], #manager-social-login { 
            display: none !important; 
            visibility: hidden !important; 
            pointer-events: none !important; 
        }
    """)
    
    # 2. JS para cerrar activamente cualquier modal que use la clase 'in' (visible en Bootstrap)
    # y eliminar el preloader si se quedó pegado.
    try:
        page.evaluate("""
            document.querySelectorAll('.modal.in, .modal.show').forEach(modal => {
                const closeBtn = modal.querySelector('button.close, .btn-close, [data-dismiss="modal"]');
                if (closeBtn) closeBtn.click();
                else modal.remove();
            });
            document.querySelectorAll('#preloader-image, .modal-backdrop').forEach(el => el.remove());
        """)
    except: pass
        

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
    print("🚀 Iniciando proceso de login ultra-robusto v3.4...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        print(f"\n--- Intento Maestro {attempt + 1}/{max_retries} ---")
        try:
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
            
            for step in range(25):
                handle_popups(page)
                current_url = page.url

                if SUCCESS_URLS_REGEX.search(current_url):
                    print("✅ ¡LOGIN EXITOSO!")
                    return True

                # --- ACCIÓN: PRIVACIDAD (CORREGIDO) ---
                if "PrivacyNotice" in current_url:
                    print("  - [ACCIÓN] Detectada Privacidad. Intentando Aceptar...")
                    accept_btn = page.get_by_role("button", name=re.compile("Accept|Agree|Aceptar", re.IGNORECASE))
                    
                    if accept_btn.is_visible(timeout=5000):
                        accept_btn.click(force=True)
                        print("  - [INFO] Click en Accept realizado. Esperando redirección...")
                        # Esperamos a que la URL cambie para salir del estado de Privacidad
                        try:
                            page.wait_for_url(lambda url: "PrivacyNotice" not in url, timeout=10000)
                        except:
                            page.goto(LOGIN_URL)
                    else:
                        # Si la URL dice privacidad pero no vemos el botón, forzamos login
                        page.goto(LOGIN_URL)
                    continue

                # --- ACCIÓN: REGISTRO / TRAP SOCIAL ---
                if "Register" in current_url:
                    print("  - [ACCIÓN] En página de Registro. Forzando salto a Login...")
                    page.goto(LOGIN_URL, wait_until="networkidle")
                    continue

                # --- ACCIÓN: FORMULARIO DE LOGIN ---
                if "Login" in current_url:
                    # Esperamos a que los inputs existan realmente
                    username_input = page.locator("input#manager-name")
                    if username_input.is_visible(timeout=5000):
                        print("  - [INFO] Formulario visible. Rellenando...")
                        username_input.fill(osm_username)
                        page.locator("input#password").fill(osm_password)
                        
                        # Click en el botón de login
                        page.locator("button#login").click(force=True)
                        
                        # Verificamos si hay error de credenciales
                        try:
                            error_container = page.locator(".feedbackcontainer .feedback-message")
                            if error_container.is_visible(timeout=3000):
                                msg = error_container.inner_text().lower()
                                if "incorrect" in msg or "can't log in" in msg:
                                    raise InvalidCredentialsError(f"Error de OSM: {msg}")
                        except PlaywrightTimeoutError:
                            pass
                        
                        # Esperamos a que la URL cambie a una de éxito
                        page.wait_for_url(SUCCESS_URLS_REGEX, timeout=15000)
                        continue
                
                # Pequeña pausa de seguridad en cada paso para no saturar
                time.sleep(1)

        except InvalidCredentialsError as e:
            print(f"❌ CREDENCIALES INVÁLIDAS: {e}")
            raise e
        except Exception as e:
            print(f"  - ⚠️ Error en paso: {e}")
            page.context.clear_cookies() # Limpiamos para el siguiente reintento
            time.sleep(3)
                
    return False


        



