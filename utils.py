from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
import time

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