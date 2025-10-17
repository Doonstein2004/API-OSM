from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError, Page
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError, Page
import time
import os

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
def login_to_osm(page: Page, max_retries: int = 5):
    """
    Gestiona el proceso completo de login en OSM, incluyendo la aceptación de cookies
    y una lógica de reintentos si el login inicial falla.
    
    Args:
        page (Page): La instancia de la página de Playwright.
        max_retries (int): El número máximo de intentos de login.

    Returns:
        bool: True si el login fue exitoso, False en caso contrario.
    """
    print("Iniciando proceso de login centralizado...")
    
    for attempt in range(max_retries):
        try:
            print(f"Intento de login {attempt + 1}/{max_retries}...")
            
            # Navegar a la página de inicio (que redirige a la de privacidad/cookies si es necesario)
            page.goto("https://en.onlinesoccermanager.com/Login", timeout=60000)

            # 1. Aceptar cookies si el botón aparece
            accept_button = page.locator('button:has-text("Accept")')
            if accept_button.count() > 0:
                print("  - Botón 'Accept' detectado. Aceptando cookies...")
                accept_button.click()
                # Esperar a que la página se estabilice después del clic
                page.wait_for_load_state('domcontentloaded', timeout=40000)
                
            try:        
                # 2 Pasar al login
                login_link_button = page.locator('button:has-text("Log in")')
                login_link_button.wait_for(state="visible", timeout=40000)
                login_link_button.click()
                
                
            except Exception as e:
                print("No se encontro boton para ir al login")

            # 3. Rellenar credenciales
            print("  - Esperando el formulario de login...")
            manager_name_input = page.locator("#manager-name")
            manager_name_input.wait_for(state="visible", timeout=40000)
            
            print("  - Rellenando credenciales...")
            manager_name_input.fill(os.getenv("MI_USUARIO"))
            page.locator("#password").fill(os.getenv("MI_CONTRASENA"))
            
            # 4. Hacer clic en el botón de login
            print("  - Haciendo clic en el botón de login...")
            page.locator("#login").click()
            
            print("  - Verificando el éxito del login...")
            page.wait_for_selector(
                '#crew, a[href="/Career"]', # Espera por #crew O un link al Career dashboard
                state='visible',
                timeout=45000
            )

            # Verificación final: asegurarnos de que no estamos en la página de registro
            if "/Register" in page.url:
                raise Exception("Redirigido a la página de registro, el login falló.")

            print("  - ¡Login exitoso verificado!")
            handle_popups(page)
            return True

        except Exception as e:
            print(f"  - ADVERTENCIA: El intento de login {attempt + 1} falló. Razón: {e}")

            if attempt < max_retries - 1:
                print("    Reintentando...")
                time.sleep(3) # Pausa antes de reintentar
            else:
                print("  - ERROR: Se alcanzó el número máximo de reintentos de login.")
                return False
        except Exception as e:
            print(f"  - ERROR INESPERADO durante el intento de login {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                print("    Reintentando...")
                time.sleep(3)
            else:
                print("  - ERROR: Se alcanzó el número máximo de reintentos debido a un error inesperado.")
                return False
    
    return False