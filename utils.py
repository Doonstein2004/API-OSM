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
    Proceso de login basado en un bucle de estado, que reacciona a la página actual.
    """
    print("Iniciando proceso de login basado en estado...")
    ACTION_TIMEOUT = 120 * 1000  # 120 segundos

    for attempt in range(max_retries):
        try:
            print(f"--- Intento de Login {attempt + 1}/{max_retries} ---")
            
            # Navegamos a la página de entrada una sola vez al principio del intento.
            page.goto("https://en.onlinesoccermanager.com/Login", timeout=ACTION_TIMEOUT, wait_until="domcontentloaded")

            # --- INICIO DEL BUCLE DE ESTADO ---
            # Este bucle se ejecutará hasta 10 veces para resolver el estado del login.
            for _ in range(10): # Límite de 10 pasos para evitar bucles infinitos
                print("Debug")
                # Espera un poco para que la página se estabilice
                page.wait_for_timeout(10000) 
                print("Leão")
                current_url = page.url
                print(f"  - Verificando estado. URL actual: {current_url}")

                # ESTADO 1: Login Exitoso (Hemos llegado al dashboard)
                if "Career" in current_url:
                    print("  - ¡ESTADO: LOGIN EXITOSO! Estamos en el dashboard.")
                    handle_popups(page)
                    return True # Salimos de la función con éxito

                # ESTADO 2: En la Página de Aviso de Privacidad
                elif "PrivacyNotice" in current_url:
                    print("  - ESTADO: Aviso de Privacidad. Aceptando...")
                    accept_button = page.get_by_role("button", name="Accept", exact=True)
                    accept_button.click(timeout=ACTION_TIMEOUT)
                    # No esperamos navegación aquí, dejamos que el bucle vuelva a comprobar el estado.
                    continue # Vuelve al inicio del bucle para re-evaluar la nueva página
                
                elif "ChooseLeague" in current_url:
                    print("  - ESTADO: LOGIN EXITOSO! Estamos en la seleccion de ligas... Redirigiendo a Career.")
                    page.goto("https://en.onlinesoccermanager.com/Career", timeout=ACTION_TIMEOUT, wait_until="domcontentloaded")
                    # No esperamos navegación aquí, dejamos que el bucle vuelva a comprobar el estado.
                    return True # Salimos de la función con éxito

                # ESTADO 3: En la Página de Registro
                elif "Register" in current_url:
                    print("  - ESTADO: Página de Registro. Navegando a Login...")
                    go_to_login_button = page.get_by_role("button", name="Log in", exact=True)
                    go_to_login_button.click(timeout=ACTION_TIMEOUT)
                    continue # Vuelve al inicio del bucle para re-evaluar la nueva página

                # ESTADO 4: En la Página de Login (Nuestro objetivo intermedio)
                elif "/Login" in current_url:
                    print("  - ESTADO: Página de Login. Rellenando formulario...")
                    username_input = page.locator("#manager-name")
                    
                    # Verificamos si el formulario ya está visible
                    if not username_input.is_visible():
                        print("    - Formulario no visible, esperando...")
                        page.wait_for_selector("#manager-name", state="visible", timeout=ACTION_TIMEOUT)

                    #usuario = os.getenv("MI_USUARIO")
                    #contrasena = os.getenv("MI_CONTRASENA")
                    if not osm_username or not osm_password:
                        raise Exception("Credenciales de OSM no proporcionadas a la función de login.")

                    username_input.fill(osm_username)
                    page.locator("#password").fill(osm_password)
                    
                    login_button = page.locator("#login")
                    print("  - Enviando formulario...")
                    login_button.click(timeout=ACTION_TIMEOUT)
                    print("Que pasa")
                    continue # Vuelve al inicio del bucle para re-evaluar la nueva página

                # ESTADO DESCONOCIDO: Si no estamos en ninguna de las páginas esperadas
                else:
                    print(f"  - ESTADO DESCONOCIDO. Refrescando la página...")
                    page.reload(wait_until="domcontentloaded")
                    continue

            # Si salimos del bucle de 10 pasos sin éxito, el intento falla.
            raise Exception("El flujo de login no se pudo resolver después de 10 pasos.")

        except Exception as e:
            # ... (la lógica de error y reintento no cambia)
            print(f"  - ADVERTENCIA: El intento {attempt + 1} falló.")
            error_type = type(e).__name__
            error_message = str(e).split('\n')[0]
            print(f"    - Razón: {error_type} - {error_message}")
            try:
                page.screenshot(path=f"login_error_attempt_{attempt + 1}.png")
                print(f"    - Captura de pantalla guardada.")
            except Exception as se:
                print(f"    - No se pudo tomar la captura de pantalla: {se}")

            if attempt < max_retries - 1:
                print("    Reintentando en 10 segundos...")
                time.sleep(10)
            else:
                print("  - ERROR: Se alcanzó el número máximo de reintentos de login.")
                return False
    
    return False


