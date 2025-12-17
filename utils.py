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
    print("🚀 Iniciando proceso de login ultra-robusto v3.3...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        try:
            # Navegación inicial
            print(f"--- Intento {attempt + 1}/{max_retries} ---")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            
            for step in range(20):
                # Limpieza preventiva
                handle_popups(page)
                
                current_url = page.url
                # print(f"  - [Paso {step}] URL: {current_url}") # Descomentar para debug

                if SUCCESS_URLS_REGEX.search(current_url):
                    print("✅ ¡LOGIN EXITOSO!")
                    return True

                # --- ACCIÓN: PRIVACIDAD ---
                if "PrivacyNotice" in current_url:
                    print("  - [ACCIÓN] Aceptando privacidad...")
                    accept_btn = page.get_by_role("button", name="Accept", exact=True)
                    if accept_btn.is_visible():
                        accept_btn.click(force=True)
                        
                        # --- CRÍTICO: EL BYPASS ---
                        print("  - [ESTRATEGIA] Saltando trampa social. Navegando directo a Login...")
                        time.sleep(1) # Esperamos 1s para que la cookie de privacidad se asiente
                        page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    continue

                # --- ACCIÓN: TRAP DE REGISTRO / SOCIAL ---
                # Si estamos en Register, el modal social nos ha atrapado
                if "Register" in current_url:
                    print("  - [ACCIÓN] Atrapado en Register. Forzando escape a Login...")
                    
                    # Opción A: Intentar clic rápido si el botón es visible
                    try:
                        btn_login = page.get_by_role("button", name="Log in", exact=True)
                        if btn_login.is_visible(timeout=1000):
                            btn_login.click(force=True)
                        else:
                            # Opción B: Si el modal tapa el botón, recargamos la URL de Login
                            raise Exception("Botón tapado")
                    except:
                        page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    continue

                # --- ACCIÓN: FORMULARIO DE LOGIN REAL ---
                if "Login" in current_url:
                    # Usamos localizadores específicos con ID para ser precisos
                    username_input = page.locator("input#manager-name")
                    password_input = page.locator("input#password")
                    login_button = page.locator("button#login")

                    # Si por algún motivo el modal de Facebook está tapando el formulario:
                    handle_popups(page)

                    try:
                        username_input.wait_for(state="visible", timeout=5000)
                        
                        # Usamos fill que es más rápido
                        username_input.fill(osm_username)
                        password_input.fill(osm_password)
                        
                        # Clic forzado
                        login_button.click(force=True)
                        
                        # Esperamos a ver si hay error de credenciales
                        error_msg = page.locator(".feedbackcontainer .feedback-message")
                        # Damos un tiempo corto para ver si sale el error
                        if error_msg.is_visible(timeout=3000):
                            text = error_msg.inner_text().lower()
                            if "incorrect" in text:
                                raise InvalidCredentialsError("Credenciales incorrectas.")
                        
                        # Si no hay error, esperamos la navegación
                        page.wait_for_url(SUCCESS_URLS_REGEX, timeout=10000)
                        return True
                        
                    except PlaywrightTimeoutError:
                        # Si falla el wait, es posible que estemos cargando, seguimos el loop
                        pass
                        
                    continue

                time.sleep(1)

        except InvalidCredentialsError as e:
            # Error fatal, no reintentamos
            print(f"❌ Error fatal de credenciales: {e}")
            raise e 
        except Exception as e:
            print(f"  - ❌ Intento {attempt + 1} falló: {e}")
            # Si falla, limpiamos cookies para asegurar un intento limpio
            try: page.context.clear_cookies()
            except: pass
            time.sleep(2)
                
    return False


        



