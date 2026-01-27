from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Definimos una excepción personalizada
class InvalidCredentialsError(Exception):
    pass

def handle_popups(page: Page):
    """
    Versión v4.0: Cierra modales de forma más agresiva.
    Incluye reintentos y manejo de modales que bloquean interacción.
    """
    # 1. Inyectamos CSS agresivo para ocultar elementos sociales y bloqueadores
    try:
        page.add_style_tag(content="""
            #preloader-image, .modal-backdrop, #genericModalContainer, 
            .social-login-modal, #social-login-container, .facebook-login-button, 
            iframe[src*="facebook"], #manager-social-login,
            #skillRatingUpdate-modal-content, .tier-up-title, .shield-animation-container { 
                display: none !important; 
                visibility: hidden !important; 
                pointer-events: none !important; 
            }
        """)
    except:
        pass
    
    # 2. JS para cerrar activamente cualquier modal visible
    try:
        page.evaluate("""
            // Cerrar modales con click en close button
            document.querySelectorAll('.modal.in, .modal.show').forEach(modal => {
                const closeBtn = modal.querySelector('button.close, .btn-close, [data-dismiss="modal"], .close-button-container button');
                if (closeBtn) {
                    closeBtn.click();
                }
            });
            
            // Eliminar backdrops y preloaders
            document.querySelectorAll('#preloader-image, .modal-backdrop').forEach(el => el.remove());
            
            // Forzar cierre de modales que no respondieron al click
            setTimeout(() => {
                document.querySelectorAll('.modal.in, .modal.show').forEach(modal => {
                    modal.classList.remove('in', 'show');
                    modal.style.display = 'none';
                });
                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
                document.body.style.overflow = '';
                document.body.style.paddingRight = '';
            }, 100);
        """)
    except:
        pass
    
    # 3. Fallback: presionar Escape para cerrar modales
    try:
        modal_visible = page.locator(".modal.in, .modal.show")
        if modal_visible.count() > 0:
            page.keyboard.press("Escape")
            time.sleep(0.2)
    except:
        pass
    
def safe_navigate(page: Page, url: str, verify_selector: str = None, max_retries=3):
    """
    Intenta navegar a una URL. Si falla (timeout, abortado), reintenta.
    Si se pasa 'verify_selector', espera a que ese elemento exista para confirmar éxito.
    """
    for attempt in range(max_retries):
        try:
            # Usamos 'load' por defecto para ser conservadores, pero con timeout controlado
            # Si falla, el except lo atrapará y reintentaremos.
            page.goto(url, wait_until='load', timeout=30000)
            
            # Si nos piden verificar un elemento específico (ej: la tabla)
            if verify_selector:
                try:
                    page.wait_for_selector(verify_selector, timeout=10000)
                except TimeoutError:
                    print(f"  ⚠️ Carga incompleta (falta '{verify_selector}'). Reintentando (F5)...")
                    raise Exception("Selector de validación no encontrado")

            # Si llegamos aquí, todo cargó bien
            return True

        except Exception as e:
            print(f"  ⚠️ Error de navegación (Intento {attempt + 1}/{max_retries}): {e}")
            
            # Estrategia de "Enfriamiento" antes de reintentar
            time.sleep(2)
            
            # Si no es el último intento, intentamos un Reload explícito si la URL ya está puesta
            if attempt < max_retries - 1:
                try:
                    if page.url == url:
                        print("  🔄 Aplicando Reload (F5)...")
                        page.reload(wait_until='domcontentloaded')
                except:
                    pass

    print(f"  ❌ Fallo definitivo navegando a {url}")
    return False
        

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
    print("🚀 Iniciando Login v3.5 (Optimizado para GitHub Actions)...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        print(f"\n--- Intento Maestro {attempt + 1}/{max_retries} ---")
        try:
            # 1. Navegación con tiempo de espera generoso para GHA
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
            
            for step in range(30): # Aumentamos pasos
                handle_popups(page)
                current_url = page.url

                if SUCCESS_URLS_REGEX.search(current_url):
                    print("✅ ¡LOGIN EXITOSO!")
                    return True

                # --- CASO: PRIVACIDAD ---
                if "PrivacyNotice" in current_url:
                    print("  - [ACCIÓN] Aceptando privacidad...")
                    # Buscamos el botón de forma más flexible
                    accept_btn = page.get_by_role("button", name=re.compile("Accept|Agree|Aceptar|OK", re.IGNORECASE))
                    if accept_btn.is_visible():
                        accept_btn.click(force=True)
                        page.wait_for_timeout(2000) # Espera crucial para GHA
                        page.goto(LOGIN_URL, wait_until="networkidle")
                    continue

                # --- CASO: REDIRECCIÓN A REGISTRO (EL ERROR QUE TIENES) ---
                if "Register" in current_url:
                    print("  - [ALERTA] Caímos en Register. Forzando regreso a Login...")
                    page.goto(LOGIN_URL, wait_until="networkidle")
                    page.wait_for_timeout(2000)
                    continue

                # --- CASO: FORMULARIO DE LOGIN ---
                if "Login" in current_url:
                    username_input = page.locator("input#manager-name")
                    password_input = page.locator("input#password")
                    
                    if username_input.is_visible(timeout=10000):
                        print(f"  - [INFO] Rellenando credenciales para: {osm_username}")
                        
                        # Simulamos escritura humana con delay entre teclas
                        username_input.fill("") # Limpiar
                        username_input.type(osm_username, delay=100)
                        
                        password_input.fill("") # Limpiar
                        password_input.type(osm_password, delay=100)
                        
                        page.wait_for_timeout(1000) # Pausa humana
                        
                        # ESTRATEGIA GHA: En lugar de click, usamos Enter en el campo password
                        print("  - [ACCIÓN] Enviando formulario con tecla ENTER...")
                        password_input.press("Enter")
                        
                        time.sleep(8)
                        
                        # Esperamos a ver qué pasa (Navegación o Error)
                        try:
                            # Esperamos a que la URL cambie O aparezca un mensaje de error
                            page.wait_for_function("""
                                () => window.location.href.includes('Career') || 
                                      window.location.href.includes('ChooseLeague') ||
                                      document.querySelector('.feedback-message') !== null
                            """, timeout=15000)
                            
                            # Si hay error de credenciales, lanzamos excepción
                            error_msg = page.locator(".feedbackcontainer .feedback-message")
                            if error_msg.is_visible(timeout=2000):
                                raise InvalidCredentialsError(f"OSM dice: {error_msg.inner_text()}")
                            
                        except PlaywrightTimeoutError:
                            print("  - [!] Timeout tras Enter. Re-evaluando URL...")
                    continue

                time.sleep(2) # Pausa entre pasos de bucle

        except InvalidCredentialsError as e:
            print(f"❌ Error crítico: {e}")
            raise e
        except Exception as e:
            print(f"  - ⚠️ Error en intento {attempt + 1}: {e}")
            page.context.clear_cookies() # Limpiar rastro para el siguiente intento
            page.wait_for_timeout(5000)
                
    return False


        



