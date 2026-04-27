from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Definimos una excepción personalizada
class InvalidCredentialsError(Exception):
    pass

def handle_popups(page: Page):
    """
    Versión v4.2: Cierra modales agresivos, incluyendo el aviso de Password Login (que es un div, no un button).
    """
    try:
        understand_selectors = [
            "button:has-text('I understand')",
            "div.btn-new:has-text('I understand')",
            ".modal-content .btn-new",
            "button:has-text('Entiendo')",
            "div.btn-new:has-text('Entiendo')",
            "button:has-text('Continue')",
            "button:has-text('Continuar')",
            "button:has-text('Skip')",
            "button:has-text('Saltar')",
            "button:has-text('View later')",
            "button:has-text('Ver más tarde')"
        ]
        for sel in understand_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(force=True)
                    page.wait_for_timeout(500)
            except:
                pass
    except:
        pass

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
    
    try:
        page.evaluate("""
            document.querySelectorAll('.modal.in, .modal.show').forEach(modal => {
                const closeBtn = modal.querySelector('button.close, .btn-close, [data-dismiss='modal'], .close-button-container button');
                if (closeBtn) closeBtn.click();
            });
            document.querySelectorAll('#preloader-image, .modal-backdrop').forEach(el => el.remove());
            setTimeout(() => {
                document.querySelectorAll('.modal.in, .modal.show').forEach(modal => {
                    modal.classList.remove('in', 'show');
                    modal.style.display = 'none';
                });
                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            }, 100);
        """)
    except:
        pass
    
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
    print("🚀 Iniciando Login OSM...")
    LOGIN_URL = "https://en.onlinesoccermanager.com/Login"
    SUCCESS_URLS_REGEX = re.compile(".*(/Career|/ChooseLeague)")
    
    for attempt in range(max_retries):
        try:
            print(f"  🔑 Intento {attempt + 1}: Navegando a {LOGIN_URL}...")
            # networkidle es muy lento en OSM, usamos domcontentloaded y un timeout más alto
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            
            for check in range(30):
                handle_popups(page)
                current_url = page.url
                print(f"    Check {check+1}/30: {current_url}")
                
                if SUCCESS_URLS_REGEX.search(current_url):
                    print("    ✅ Redirección exitosa detectada!")
                    return True
                
                if "PrivacyNotice" in current_url:
                    print("    ⚖️ Aviso de privacidad detectado. Aceptando...")
                    accept_btn = page.get_by_role("button", name=re.compile("Accept|Agree|Aceptar|OK", re.IGNORECASE))
                    if accept_btn.is_visible():
                        accept_btn.click(force=True)
                        page.wait_for_timeout(2000)
                        page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    continue
                
                if "Register" in current_url:
                    print("    🔄 Redirigiendo desde Register a Login...")
                    page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    continue
                
                if "Login" in current_url:
                    username_input = page.locator("input#manager-name")
                    password_input = page.locator("input#password")
                    
                    if username_input.is_visible(timeout=5000):
                        print(f"    📝 Rellenando formulario para {osm_username}...")
                        username_input.fill(osm_username)
                        password_input.fill(osm_password)
                        page.locator("button#login").click() # Cambiado de Enter a Clic directo
                        time.sleep(8)
                        
                        try:
                            page.wait_for_function("() => window.location.href.includes('Career') || window.location.href.includes('ChooseLeague') || document.querySelector('.feedback-message') !== null", timeout=15000)
                            error_msg = page.locator(".feedbackcontainer .feedback-message")
                            if error_msg.is_visible(timeout=2000):
                                print(f"    ❌ Error de OSM: {error_msg.inner_text()}")
                                raise InvalidCredentialsError(f"OSM: {error_msg.inner_text()}")
                        except PlaywrightTimeoutError: 
                            print("    ⏳ Espera terminada, revisando URL de nuevo...")
                            pass
                    else:
                        print("    ⌛ Esperando a que el formulario sea visible...")
                
                time.sleep(2)
        except InvalidCredentialsError as e: raise e
        except Exception as e:
            print(f"  ⚠️ Error en intento {attempt + 1}: {e}")
            page.context.clear_cookies()
            page.wait_for_timeout(5000)
    return False


# ==========================================
# SESSION CACHE (Playwright Storage State)
# ==========================================

SESSION_CACHE_TTL_HOURS = 18  # Sesiones de OSM duran ~24h, renovamos a las 18h

def load_session_from_db(conn, user_id: str) -> dict | None:
    """
    Carga el estado de sesión del navegador (cookies + localStorage) desde la BD.
    Retorna el dict de storage_state o None si no existe / expiró.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_state, saved_at
                FROM public.user_browser_sessions
                WHERE user_id = %s
                LIMIT 1;
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            
            from datetime import datetime, timedelta
            age = datetime.now() - row['saved_at'].replace(tzinfo=None)
            if age > timedelta(hours=SESSION_CACHE_TTL_HOURS):
                print(f"  ⏳ Sesión cacheada expirada (hace {age}). Se hará login.")
                return None
            
            print(f"  ✅ Sesión cacheada encontrada (hace {age.seconds // 3600}h {(age.seconds % 3600) // 60}m)")
            import json
            return json.loads(row['session_state'])
    except Exception as e:
        print(f"  ⚠️ No se pudo leer la sesión de la BD: {e}")
        return None


def save_session_to_db(conn, user_id: str, storage_state: dict):
    """
    Guarda el estado de sesión del navegador en la BD para reutilizarlo.
    Auto-crea la tabla si no existe.
    """
    try:
        with conn.cursor() as cur:
            # Auto-migration: crear tabla si no existe
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.user_browser_sessions (
                    user_id UUID PRIMARY KEY,
                    session_state TEXT NOT NULL,
                    saved_at TIMESTAMP DEFAULT NOW()
                );
            """)
            import json
            cur.execute("""
                INSERT INTO public.user_browser_sessions (user_id, session_state, saved_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET session_state = EXCLUDED.session_state,
                        saved_at = NOW();
            """, (user_id, json.dumps(storage_state)))
        conn.commit()
        print("  💾 Sesión guardada en BD para próximas ejecuciones.")
    except Exception as e:
        print(f"  ⚠️ No se pudo guardar la sesión en la BD: {e}")
        try:
            conn.rollback()
        except:
            pass


def login_with_session_cache(browser, conn, user_id: str, osm_username: str, osm_password: str):
    """
    Crea un contexto de Playwright con sesión cacheada si existe.
    Si la sesión expiró o es inválida, hace login normal y guarda la nueva sesión.
    
    Retorna: (context, page) listos para usar.
    
    Uso en run_update_for_user.py:
        context, page = login_with_session_cache(browser, conn, user_id, username, password)
    """
    CAREER_URL = "https://en.onlinesoccermanager.com/Career"
    SUCCESS_REGEX = re.compile(r".*/(Career|ChooseLeague)")
    
    # --- 1. Intentar restaurar sesión cacheada ---
    cached_state = load_session_from_db(conn, user_id)
    
    if cached_state:
        print("  🔄 Restaurando sesión cacheada...")
        try:
            context = browser.new_context(
                storage_state=cached_state,
                viewport={'width': 1280, 'height': 720}
            )
            page = context.new_page()
            page.goto(CAREER_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            handle_popups(page)
            
            # Verificar si la sesión sigue activa
            if SUCCESS_REGEX.search(page.url):
                print("  ✅ Sesión restaurada correctamente. Login omitido.")
                return context, page
            else:
                print(f"  ⚠️ Sesión inválida (URL: {page.url}). Haciendo login...")
                page.close()
                context.close()
        except Exception as e:
            print(f"  ⚠️ Error restaurando sesión: {e}. Haciendo login...")
            try:
                page.close()
                context.close()
            except:
                pass

    # --- 2. Login normal ---
    context = browser.new_context(viewport={'width': 1280, 'height': 720})
    page = context.new_page()
    
    login_ok = login_to_osm(page, osm_username, osm_password)
    if not login_ok:
        raise Exception("Login fallido tras agotar reintentos")
    
    # --- 3. Guardar nueva sesión ---
    try:
        storage_state = context.storage_state()
        save_session_to_db(conn, user_id, storage_state)
    except Exception as e:
        print(f"  ⚠️ No se pudo capturar el storage_state: {e}")
    
    return context, page
