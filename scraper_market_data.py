# scraper_market_data.py
import time
from playwright.sync_api import Page, expect, TimeoutError
from utils import handle_popups, safe_int

def parse_price(price_text):
    if not isinstance(price_text, str): return 0
    
    value_str = price_text.lower().strip().replace(',', '.')
    
    # Buscamos el multiplicador
    if 'm' in value_str:
        multiplier = 1.0  # Ya está en millones
        value_str = value_str.replace('m', '')
    elif 'k' in value_str:
        multiplier = 0.001  # Mil a Millones (1/1000)
        value_str = value_str.replace('k', '')
    else:
        # Si no tiene letra, asumimos que es el valor entero y lo pasamos a millones
        multiplier = 0.000001 
    
    # Limpiar caracteres no numéricos
    value_str = ''.join(filter(lambda x: x.isdigit() or x == '.', value_str))
    
    try:
        # Devolvemos el valor en unidades de "Millón" con 2 decimales
        return round(float(value_str) * multiplier, 2)
    except:
        return 0

def get_market_data(page: Page):
    print("\n--- Scraper de Mercado V8 (Data Context Extraction) ---")
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TRANSFERS_URL = "https://en.onlinesoccermanager.com/Transferlist"
    
    all_teams_transfer_list = []
    all_teams_transfer_history = []

    try:
        NUM_SLOTS = 4
        for i in range(NUM_SLOTS):
            print(f"\n--- Slot #{i + 1} ---")
            if not page.url.endswith("/Career"):
                page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")
            
            try:
                page.wait_for_selector(".career-teamslot", timeout=10000)
            except: break

            slot = page.locator(".career-teamslot").nth(i)
            if slot.locator("h2.clubslot-main-title").count() == 0: continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando: {team_name}")
            
            slot.click()
            page.wait_for_selector("#timers", timeout=60000)
            handle_popups(page)

            page.goto(TRANSFERS_URL, wait_until="load")
            handle_popups(page)

            # --- EXTRACCIÓN MAGISTRAL ---
            # En lugar de hacer clic, le pedimos a la página que nos de los datos de cada fila
            print("  - Extrayendo datos de jugadores (sin abrir modales)...")
            try:
                table_selector = "#transfer-list table.table-sticky tbody tr.clickable"
                page.wait_for_selector(table_selector, timeout=20000)
                
                # Esta función de JS se ejecuta en el navegador y extrae TODO de golpe
                players_on_sale = page.evaluate("""
                    () => {
                        const rows = Array.from(document.querySelectorAll("#transfer-list table.table-sticky tbody tr.clickable"));
                        return rows.map(row => {
                            // Extraemos el contexto de Knockout vinculado a esta fila
                            const data = ko.dataFor(row);
                            if (!data || !data.playerPartial()) return null;
                            
                            const player = data.playerPartial();
                            const team = data.teamPartial() ? data.teamPartial().name : "N/A";
                            const manager = (data.teamPartial() && data.teamPartial().managerPartial()) 
                                            ? data.teamPartial().managerPartial().name 
                                            : "CPU";

                            return {
                                name: player.name,
                                nationality: player.nationality ? player.nationality.name : "N/A",
                                position: row.querySelectorAll("td")[2].innerText.trim(),
                                age: player.age,
                                seller_team: team,
                                seller_manager: manager,
                                attack: player.statAtt,
                                defense: player.statDef,
                                overall: player.statOvr,
                                price: data.price, // Valor de venta
                                value: player.value // ¡VALOR BASE DIRECTO SIN MODAL!
                            };
                        }).filter(p => p !== null);
                    }
                """)
                
                # Limpiamos los precios que vienen de JS (por si acaso)
                for p in players_on_sale:
                    p['price'] = round(float(p['price_raw']) / 1_000_000, 2)
                    p['value'] = round(float(p['value_raw']) / 1_000_000, 2)
                    
                    
                    del p['price_raw']
                    del p['value_raw']

                all_teams_transfer_list.append({
                    "team_name": team_name, 
                    "league_name": league_name, 
                    "players_on_sale": players_on_sale
                })
                print(f"  ✓ {len(players_on_sale)} jugadores extraídos instantáneamente.")

            except Exception as e:
                print(f"  ⚠️ Error extrayendo mercado: {e}")

            # --- HISTORIAL (Misma lógica de extracción rápida) ---
            print("  - Historial...")
            history_tab = page.locator("a[href='#transfer-history']")
            if history_tab.is_visible():
                history_tab.click()
                try:
                    page.wait_for_selector("#transfer-history table.table", timeout=10000)
                    # Cargar más un par de veces
                    for _ in range(3):
                        btn = page.locator('button:has-text("More transfers")')
                        if btn.is_visible(timeout=500): 
                            btn.click()
                            time.sleep(0.5)

                    hist_list = page.evaluate("""() => {
                        const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                        return rows.map(r => {
                            const c = r.querySelectorAll("td");
                            if(c.length < 8) return null;
                            return {
                                Name: c[0].innerText.trim(), From: c[1].innerText.trim(),
                                To: c[2].innerText.trim(), Position: c[3].innerText.trim(),
                                Gameweek: c[4].innerText.trim(), Value: c[5].innerText.trim(),
                                Price: c[6].innerText.trim(), Date: c[7].innerText.trim()
                            };
                        }).filter(x => x);
                    }""")
                    all_teams_transfer_history.append({
                        "team_name": team_name, "league_name": league_name, "transfers": hist_list
                    })
                except: pass

    except Exception as e:
        print(f"❌ Error crítico mercado: {e}")

    return all_teams_transfer_list, all_teams_transfer_history