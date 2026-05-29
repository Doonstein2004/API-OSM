from __future__ import annotations
from playwright.sync_api import expect, Error as PlaywrightError, Page
import time
import os, re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Definimos una excepción personalizada
class InvalidCredentialsError(Exception):
    pass

def handle_popups(page: Page):
    """
    Versión v4.4: Cierra modales agresivos, incluyendo avisos de Cookies, Privacidad y Password Login.
    """
    try:
        # 1. Selectores de botones de "Entendido" / "Aceptar" / "Cookies"
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
            "button:has-text('Ver más tarde')",
            "button:has-text('Accept')",
            "button:has-text('Aceptar')",
            "button:has-text('Agree')",
            "button:has-text('Aceptar todo')",
            "button:has-text('Accept all')",
            "button#onetrust-accept-btn-handler", # Common cookie consent
            ".qc-cmp2-footer button:has-text('AGREE')", # Quantcast
            ".btn-primary:has-text('OK')",
            "button:has-text('Got it!')"
        ]
        for sel in understand_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=300):
                    loc.click(force=True)
                    page.wait_for_timeout(300)
            except:
                pass
    except:
        pass

    # 2. Inyección de CSS para ocultar elementos molestos y forzar visibilidad
    try:
        page.add_style_tag(content="""
            #preloader-image, .modal-backdrop, #genericModalContainer, 
            .social-login-modal, #social-login-container, .facebook-login-button, 
            iframe[src*="facebook"], #manager-social-login,
            #skillRatingUpdate-modal-content, .tier-up-title, .shield-animation-container,
            .modal-dialog .close-button-container, .loading-overlay, .vwo-overlay,
            #onetrust-banner-sdk { 
                display: none !important; 
                visibility: hidden !important; 
                pointer-events: none !important; 
            }
            .career-teamslot {
                visibility: visible !important;
                opacity: 1 !important;
            }
        """)
    except:
        pass
    
    # 3. Limpieza de modales vía JS y FORZADO DE VISIBILIDAD RADICAL
    try:
        page.evaluate("""
            // 1. Eliminar modales y backdrops
            document.querySelectorAll('.modal.in, .modal.show, .modal-backdrop, #preloader-image, .loading-overlay, #onetrust-banner-sdk').forEach(el => {
                el.style.display = 'none';
                el.remove();
            });
            
            // 2. Desbloquear el cuerpo
            document.body.classList.remove('modal-open');
            document.body.style.overflow = 'auto';
            document.body.style.pointerEvents = 'auto';

            // 3. DESBLOQUEO RADICAL DE SLOTS (Unhide ancestors)
            document.querySelectorAll('.career-teamslot').forEach(slot => {
                let curr = slot;
                while (curr && curr !== document.body) {
                    const style = getComputedStyle(curr);
                    if (style.display === 'none') {
                        curr.style.setProperty('display', 'block', 'important');
                    }
                    if (style.visibility === 'hidden') {
                        curr.style.setProperty('visibility', 'visible', 'important');
                    }
                    if (parseFloat(style.opacity) === 0) {
                        curr.style.setProperty('opacity', '1', 'important');
                    }
                    curr = curr.parentElement;
                }
            });
        """)
    except:
        pass
    
    # 4. Tecla Escape como último recurso
    try:
        page.keyboard.press("Escape")
    except:
        pass

def wait_for_visible_slots(page: Page, timeout=40000):
    """
    Espera robusta a que los slots de carrera sean visibles.
    Si están presentes pero ocultos, intenta limpiar popups repetidamente.
    """
    print(f"  🔍 Esperando slots (timeout {timeout/1000}s)...")
    start_time = time.time()
    while time.time() - start_time < (timeout / 1000):
        handle_popups(page)
        try:
            # Esperar a que al menos existan slots en el DOM
            slots = page.locator(".career-teamslot")
            count = slots.count()
            if count > 0:
                # Verificar si al menos uno es visible
                visible_found = False
                for i in range(count):
                    if slots.nth(i).is_visible():
                        visible_found = True
                        break
                
                if visible_found:
                    # Esperar a que al menos un slot tenga datos de equipo cargados
                    # (h2.clubslot-main-title solo aparece cuando KO.js pobló el slot)
                    try:
                        page.wait_for_selector(
                            "h2.clubslot-main-title",
                            timeout=8000,
                            state="visible",
                        )
                    except Exception:
                        # Si no aparece en 8s, continuar de todos modos
                        page.wait_for_timeout(1000)
                    return True
        except:
            pass
        
        # Si no son visibles, tal vez están presentes pero ocultos por un overlay
        try:
            if page.locator(".career-teamslot").count() > 0:
                # Intentar forzar visibilidad vía JS
                page.evaluate("document.querySelectorAll('.career-teamslot').forEach(el => el.style.display='block');")
        except:
            pass
            
        time.sleep(1.5)
    
    return False

def get_slot_info(slot_locator, max_retries=10):
    """
    Extrae información de un slot de carrera de forma segura.
    Maneja múltiples títulos (Searching, Unavailable) y evita Strict Mode Violations.
    Añade reintentos si el slot aparece como 'Unavailable' para esperar a que cargue.
    """
    for attempt in range(max_retries):
        try:
            # Si el slot está explícitamente vacío, no perder tiempo reintentando
            if slot_locator.locator(".career-teamslot-empty-label").count() > 0:
                print(f"    ℹ️ Slot sin utilizar detectado. Omitiendo.")
                return None, None, None
                
            # Buscar títulos posibles
            titles = slot_locator.locator("h2.clubslot-main-title")
            count = titles.count()
            
            if count == 0:
                time.sleep(1)
                continue
                
            team_name = ""
            is_searching = False
            is_unavailable = False
            
            for i in range(count):
                txt = titles.nth(i).inner_text().strip()
                if not txt: continue
                
                if "Searching" in txt:
                    is_searching = True
                elif "unavailable" in txt.lower():
                    is_unavailable = True
                else:
                    # Si no es un mensaje de estado, asumimos que es el nombre del equipo
                    db_attr = titles.nth(i).get_attribute("data-bind") or ""
                    if "teamPartial" in db_attr:
                        team_name = txt
                        break
                    elif not team_name:
                        team_name = txt
            
            if is_searching:
                print(f"    ⏳ Slot en estado 'Searching' (Intento {attempt+1})...")
            elif is_unavailable:
                print(f"    ⏳ Slot en estado 'Unavailable' (Intento {attempt+1})...")
            elif not team_name:
                print(f"    ⏳ Slot sin nombre de equipo (Intento {attempt+1})...")
            else:
                # Éxito: Tenemos nombre de equipo
                league_loc = slot_locator.locator("h4.display-name")
                league_name = league_loc.first.inner_text().strip() if league_loc.count() > 0 else "Unknown"

                # Leer jornada actual / total desde .career-teamslot-matchday
                matchday = None
                try:
                    md_loc = slot_locator.locator(".career-teamslot-matchday")
                    if md_loc.count() > 0:
                        spans = md_loc.first.locator("span")
                        if spans.count() >= 3:
                            current = int(spans.nth(0).inner_text().strip())
                            total   = int(spans.nth(2).inner_text().strip())
                            matchday = {
                                "current":  current,
                                "total":    total,
                                "finished": current >= total,
                            }
                except Exception:
                    pass

                return team_name, league_name, matchday

            # Si llegamos aquí es porque está en un estado no deseado, esperamos y reintentamos
            time.sleep(2)

        except Exception as e:
            print(f"    ⚠️ Error en get_slot_info (Intento {attempt+1}): {e}")
            time.sleep(1)

    return None, None, None

    
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
                    # Buscar botones de aceptar (pueden ser varios formatos)
                    accept_btn = page.locator("button:has-text('Accept'), button:has-text('Aceptar'), button:has-text('Agree'), button:has-text('OK')").first
                    if accept_btn.is_visible(timeout=5000):
                        accept_btn.click(force=True)
                        print("    ✅ Botón clickeado, esperando redirección...")
                        page.wait_for_timeout(3000)
                        if "PrivacyNotice" in page.url:
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
                        
                        login_btn = page.locator("button#login")
                        if login_btn.is_enabled():
                            login_btn.click(force=True)
                            page.keyboard.press("Enter")
                            print("    🚀 Formulario enviado. Esperando respuesta...")
                            try:
                                page.wait_for_function("() => window.location.href.includes('Career') || window.location.href.includes('ChooseLeague') || document.querySelector('.feedback-message') !== null", timeout=15000)
                                error_msg = page.locator(".feedbackcontainer .feedback-message")
                                if error_msg.is_visible(timeout=2000):
                                    print(f"    ❌ Error de OSM: {error_msg.inner_text()}")
                                    raise InvalidCredentialsError(f"OSM: {error_msg.inner_text()}")
                            except PlaywrightTimeoutError: 
                                pass
                        else:
                            print("    ⌛ Botón de login deshabilitado...")
                    else:
                        print("    ⌛ Esperando formulario...")
                
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
            # Auto-crear tabla si no existe (misma lógica que save)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.user_browser_sessions (
                    user_id UUID PRIMARY KEY,
                    session_state TEXT NOT NULL,
                    saved_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()

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
        try:
            conn.rollback()  # Limpiar transacción abortada para no bloquear próximas queries
        except:
            pass
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
            
            # Verificar si la sesión sigue activa comprobando si el perfil del manager cargó correctamente
            try:
                if SUCCESS_REGEX.search(page.url):
                    print("  🔍 Verificando autenticación de la sesión...")
                    # Esperar a que el nombre del manager esté visible en el DOM (indica que la API de perfil cargó)
                    page.wait_for_selector(".manager-name-text", timeout=12000, state="visible")
                    mgr_name = page.locator(".manager-name-text").first.inner_text().strip()
                    
                    if mgr_name.lower() == osm_username.lower():
                        print(f"  ✅ Sesión activa confirmada para el manager: {mgr_name}")
                        return context, page
                    else:
                        raise Exception(f"Nombre de manager no coincide ({mgr_name} vs {osm_username})")
            except Exception as check_err:
                print(f"  ⚠️ Sesión parece inactiva o no autenticada ({check_err}). Haciendo login...")
                try:
                    page.close()
                    context.close()
                except:
                    pass
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


def launch_playwright_browser(p, headless=None):
    """
    Inicia un navegador Playwright de forma robusta.
    Autodetecta Brave browser local si está disponible para evitar fallos de librerías en Chromium.
    Por defecto corre en headless=True para evitar errores de X11 en segundo plano, 
    pero se puede forzar con la variable de entorno HEADLESS=false o pasándole el parámetro.
    """
    import os
    is_gha = os.getenv("GITHUB_ACTIONS") == "true"
    
    # Determinar headless
    if headless is None:
        env_headless = os.getenv("HEADLESS")
        if env_headless is not None:
            headless = env_headless.lower() in ("true", "1")
        else:
            # En GitHub Actions siempre headless. En local, no-headless (visible) por defecto
            headless = True if is_gha else False
            
    # Autodetectar Brave browser local
    executable_path = None
    if not is_gha:
        for path in ["/usr/bin/brave-browser", "/usr/bin/brave"]:
            if os.path.exists(path):
                executable_path = path
                break
                
    launch_args = {
        "headless": headless,
        "args": ["--no-sandbox", "--disable-setuid-sandbox"]
    }
    if executable_path:
        launch_args["executable_path"] = executable_path
        print(f"🌐 Usando Brave Browser para scraping: {executable_path}")
    else:
        print("🌐 Usando Chromium por defecto de Playwright")
        
    return p.chromium.launch(**launch_args)


def click_slot_and_wait_for_dashboard(page: Page, slot_index: int, max_retries=3) -> bool:
    """
    Hace click en un slot de carrera e ingresa robustamente al dashboard del equipo.
    Si se detecta redirección a la animación de partido (MatchExperience), 
    la salta navegando directamente a /Dashboard.
    """
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    DASHBOARD_URL = "https://en.onlinesoccermanager.com/Dashboard"
    
    print(f"  - Activando Slot #{slot_index + 1}...")
    
    for attempt in range(max_retries):
        try:
            handle_popups(page)
            
            # Re-obtener el slot para evitar stale element
            slots = page.locator(".career-teamslot")
            if slots.count() <= slot_index:
                print(f"  ❌ El slot #{slot_index + 1} no existe en el DOM.")
                return False
                
            slot = slots.nth(slot_index)
            slot.click(timeout=15000, force=True)
            
            # Esperar a que ocurra una de las siguientes opciones:
            # 1. Cargue #timers (Dashboard normal)
            # 2. Navegue a MatchExperience (Animación de partido)
            try:
                page.wait_for_function("""
                    () => document.querySelector('#timers') !== null || window.location.href.includes('MatchExperience')
                """, timeout=30000)
            except Exception as wait_err:
                print(f"    ⚠️ Espera de redirección agotada en intento {attempt+1}: {wait_err}")
            
            # Saltar MatchExperience si ocurre
            if "MatchExperience" in page.url:
                print("    ⚠️ Redirigido a MatchExperience. Evitando la animación navegando a Dashboard...")
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            
            # Esperar confirmación de timers
            page.wait_for_selector("#timers", timeout=30000)
            handle_popups(page)
            return True
            
        except Exception as e:
            print(f"    ⚠️ Error al activar slot #{slot_index + 1} (intento {attempt+1}): {e}")
            if attempt < max_retries - 1:
                try:
                    # Recuperar navegando al Career
                    page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                    from utils import wait_for_visible_slots
                    wait_for_visible_slots(page, timeout=15000)
                    time.sleep(1)
                except:
                    pass
                    
    return False


