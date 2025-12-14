# run_update_from_user.py
import json
import sys
import os
import psycopg2
import psycopg2.extras
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from utils import login_to_osm

# --- Importar las funciones de los scrapers ---
from scraper_league_details import get_league_data
from scraper_market_data import get_market_data
from scraper_match_results import get_match_results

# --- CONFIGURACIÓN ---
load_dotenv()
LEAGUES_TO_IGNORE = ["Africa 2024", "All Stars Battle League", "Americas Cup 2019", "Americas Cup 2024", "Asia 2024", "Boss Tournament", "Club History A", "Club History B", "Club Stars", "Community League M", "Community League S", "Europe 2024", "Knockout Royale", "World 2002"]

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}

# --- FUNCIONES AUXILIARES ---

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.cursor_factory = psycopg2.extras.DictCursor
        print("? Conexión con PostgreSQL establecida.")
        return conn
    except psycopg2.OperationalError as e:
        print(f"? ERROR: No se pudo conectar a PostgreSQL. Revisa tus credenciales en .env y que la BD esté en ejecución. Error: {e}")
        return None

def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    value_str = value_str.lower().strip().replace(',', '')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str) / 1_000_000
    except (ValueError, TypeError): return 0

def normalize_team_name(name):
    if not isinstance(name, str): return ""
    prefixes_to_remove = ["fk ", "ca ", "fc ", "cd "]
    normalized = name.lower().strip()
    for prefix in prefixes_to_remove:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return normalized

def create_league_maps(all_leagues_data):
    team_to_leagues = defaultdict(list)
    league_to_teams = defaultdict(set)
    for league in all_leagues_data:
        league_name = league.get("league_name")
        for club in league.get("clubs", []):
            normalized_club = normalize_team_name(club["name"])
            team_to_leagues[normalized_club].append(league_name)
            league_to_teams[league_name].add(normalized_club)
    return dict(team_to_leagues), dict(league_to_teams)

def resolve_active_leagues(my_fichajes, all_leagues_data, league_details_data, leagues_to_ignore):
    print("\n[3.1] 🧠 Resolviendo ligas activas por composición de equipos...")
    
    # 1. Mapeo: Nombre Equipo -> Nombre Liga Dashboard (Desde los detalles scrapeados)
    team_to_dashboard_map = {}
    dashboard_to_official_map = {}
    
    # Pre-cargamos la info del scraper de detalles
    current_competitors_map = {}
    
    for league_info in league_details_data:
        managed_team = league_info.get("team_name")
        dashboard_name = league_info.get("league_name")
        
        # Guardamos la relación Equipo -> Dashboard
        team_to_dashboard_map[managed_team] = dashboard_name
        
        # Preparamos los competidores para el análisis
        current_competitors = {normalize_team_name(team['Club']) for team in league_info.get("standings", [])}
        current_competitors_map[managed_team] = current_competitors

    # 2. Mapas maestros de la Base de Datos
    team_to_leagues_master, league_to_teams_master = create_league_maps(all_leagues_data)
    
    # 3. Resolución: Dashboard Name -> Official Name
    for league_info in league_details_data:
        dashboard_name = league_info.get("league_name")
        managed_team = league_info.get("team_name")
        
        # Lógica de votación para encontrar el nombre oficial
        candidate_official_names = []
        
        # A. Intentamos adivinar por el equipo del usuario
        normalized_my_team = normalize_team_name(managed_team)
        candidates_by_team = team_to_leagues_master.get(normalized_my_team, [])
        
        # Filtramos ignoradas
        candidates_by_team = [c for c in candidates_by_team if c not in leagues_to_ignore]
        
        if len(candidates_by_team) == 1:
            dashboard_to_official_map[dashboard_name] = candidates_by_team[0]
            print(f"  - '{dashboard_name}' mapeada a '{candidates_by_team[0]}' (por equipo único).")
            continue
            
        # B. Si hay ambigüedad, usamos los competidores (el resto de equipos de la liga)
        competitors = current_competitors_map.get(managed_team, set())
        scores = {}
        
        # Si no hay candidatos por equipo, probamos todas las ligas (caso raro)
        search_space = candidates_by_team if candidates_by_team else league_to_teams_master.keys()
        
        for official_name in search_space:
            if official_name in leagues_to_ignore: continue
            official_teams = league_to_teams_master.get(official_name, set())
            # Contamos cuántos equipos coinciden
            match_count = len(competitors.intersection(official_teams))
            scores[official_name] = match_count
            
        if scores:
            best_match = max(scores, key=scores.get)
            best_score = scores[best_match]
            
            # Umbral de confianza (por ejemplo, al menos el 30% de los equipos coinciden)
            if best_score > len(competitors) * 0.3:
                dashboard_to_official_map[dashboard_name] = best_match
                print(f"  - '{dashboard_name}' mapeada a '{best_match}' (Coincidencia: {best_score} equipos).")
            else:
                print(f"  ⚠️ No se pudo determinar la liga oficial para '{dashboard_name}'. Mejor intento: {best_match} ({best_score})")
        else:
             print(f"  ⚠️ Sin datos para resolver '{dashboard_name}'.")

    return team_to_dashboard_map, dashboard_to_official_map


# --- FUNCIONES DE GESTIÓN DE TEMPORADAS ---

def check_league_continuity(conn, user_id, league_name, current_managers_set):
    """
    Verifica si existe una liga activa para este usuario con este nombre.
    Retorna (league_id, action):
      - action 'USE_EXISTING': Es la misma temporada.
      - action 'NEW_SEASON': Es una temporada nueva (o no existía ninguna).
    """
    with conn.cursor() as cur:
        # Buscar si el usuario ya tiene una liga activa con ese nombre
        sql = """
            SELECT l.id, ul.managers_by_team 
            FROM user_leagues ul
            JOIN leagues l ON ul.league_id = l.id
            WHERE ul.user_id = %s AND l.name = %s AND ul.is_active = TRUE
            LIMIT 1;
        """
        cur.execute(sql, (user_id, league_name))
        row = cur.fetchone()

        if not row:
            return None, 'NEW_SEASON' # No tiene liga activa con ese nombre

        league_id = row['id']
        saved_managers_json = row['managers_by_team']
        
        # Si no hay managers guardados, asumimos que es la misma (está empezando)
        if not saved_managers_json:
            return league_id, 'USE_EXISTING'

        # Comparar managers actuales vs guardados
        saved_managers = set(saved_managers_json.values())
        
        # Intersección: cuántos managers se mantienen
        common_managers = current_managers_set.intersection(saved_managers)
        
        # Criterio: Si menos del 40% de los managers coinciden, es una liga nueva
        if len(saved_managers) > 0:
            match_ratio = len(common_managers) / len(saved_managers)
        else:
            match_ratio = 1.0

        print(f"    ?? Análisis continuidad '{league_name}': Coincidencia {match_ratio:.2%} ({len(common_managers)}/{len(saved_managers)})")

        if match_ratio < 0.40 and len(current_managers_set) > 0:
            return league_id, 'NEW_SEASON'
        else:
            return league_id, 'USE_EXISTING'
        
        
def find_matching_active_league(conn, user_id, official_name, current_managers_set):
    """
    Busca entre las ligas activas del usuario con el mismo nombre oficial.
    Retorna el ID de la liga si encuentra una donde los mánagers coincidan (>40%).
    """
    with conn.cursor() as cur:
        # Traemos TODAS las ligas activas de ese tipo para este usuario
        sql = """
            SELECT l.id, ul.managers_by_team 
            FROM user_leagues ul
            JOIN leagues l ON ul.league_id = l.id
            WHERE ul.user_id = %s AND l.name = %s AND ul.is_active = TRUE;
        """
        cur.execute(sql, (user_id, official_name))
        candidates = cur.fetchall()
        
        best_match_id = None
        best_match_ratio = 0.0
        
        for row in candidates:
            league_id = row['id']
            saved_managers_json = row['managers_by_team'] or {}
            saved_managers = set(saved_managers_json.values())
            
            if not saved_managers: continue # Liga vacía, no podemos comparar
            
            common = current_managers_set.intersection(saved_managers)
            ratio = len(common) / len(saved_managers)
            
            if ratio > best_match_ratio:
                best_match_ratio = ratio
                best_match_id = league_id
        
        # Umbral de coincidencia del 40%
        if best_match_id and best_match_ratio > 0.40:
            return best_match_id
            
        return None
    

def sync_leagues_smart(conn, dashboard_to_official_map, all_leagues_data, user_id, standings_data):
    print("\n🔄 Sincronizando ligas (Soporte Multi-Instancia)...")
    
    # Mapa final: Nombre Dashboard -> ID Base de Datos
    dashboard_to_id_map = {}
    
    # Lista de IDs que hemos confirmado/creado en esta ejecución
    confirmed_league_ids = set()

    for dashboard_name, official_name in dashboard_to_official_map.items():
        print(f"  Analizando '{dashboard_name}' ({official_name})...")
        
        # 1. Obtener datos actuales
        league_info_db = next((l for l in all_leagues_data if l.get('league_name') == official_name), None)
        if not league_info_db: continue
        
        # 2. Obtener mánagers actuales del scraper
        current_managers_set = set()
        ls_data = next((ls for ls in standings_data if ls.get("league_name") == dashboard_name), None)
        if ls_data:
            for team in ls_data.get("standings", []):
                mgr = team.get("Manager", "N/A")
                if mgr and mgr != "N/A": current_managers_set.add(mgr)
        
        # 3. Buscar si ya existe una liga activa compatible en la BD
        matched_id = find_matching_active_league(conn, user_id, official_name, current_managers_set)
        
        # Evitar asignar el mismo ID a dos dashboards diferentes en la misma ejecución
        if matched_id in confirmed_league_ids:
            matched_id = None # Forzar creación de nueva porque este ID ya se usó para otra liga hoy
        
        final_id = None
        
        if matched_id:
            print(f"    ✅ Encontrada liga existente ID {matched_id} (Managers coinciden).")
            final_id = matched_id
            # Actualizamos timestamp
            with conn.cursor() as cur:
                cur.execute("UPDATE user_leagues SET last_scraped_at = NOW() WHERE user_id = %s AND league_id = %s", (user_id, final_id))
            conn.commit()
        else:
            print(f"    ✨ Creando NUEVA instancia para '{dashboard_name}'...")
            
            # Preparar equipos para insertar
            raw_clubs = league_info_db.get("clubs", [])
            if raw_clubs and isinstance(raw_clubs, list) and len(raw_clubs) > 0 and "club" in raw_clubs[0]:
                 teams_for_db = [{"name": c["club"], "alias": c["club"], "initialValue": parse_value_string(c["squad_value"]), "fixedIncomePerRound": parse_value_string(c["fixed_income"]), "initialCash": 0, "currentValue": 0} for c in raw_clubs]
            else:
                 teams_for_db = raw_clubs

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO leagues (name, teams) VALUES (%s, %s) RETURNING id;",
                    (official_name, json.dumps(teams_for_db))
                )
                final_id = cur.fetchone()['id']
                
                cur.execute(
                    "INSERT INTO user_leagues (user_id, league_id, is_active, last_scraped_at) VALUES (%s, %s, TRUE, NOW())",
                    (user_id, final_id)
                )
            conn.commit()
            print(f"    ✅ Nueva liga creada con ID: {final_id}")

        dashboard_to_id_map[dashboard_name] = final_id
        confirmed_league_ids.add(final_id)

    # 4. Limpieza: Desactivar ligas que el usuario tenía activas pero que NO están en los dashboards de hoy
    # Esto maneja el caso de que una liga haya terminado.
    print("  🧹 Verificando ligas terminadas...")
    with conn.cursor() as cur:
        # Obtener todas las ligas activas del usuario
        cur.execute("SELECT league_id FROM user_leagues WHERE user_id = %s AND is_active = TRUE", (user_id,))
        active_db_ids = {row['league_id'] for row in cur.fetchall()}
        
        # Identificar las que ya no están en la lista confirmada
        ids_to_deactivate = active_db_ids - confirmed_league_ids
        
        if ids_to_deactivate:
            print(f"    ❄️ Archivando {len(ids_to_deactivate)} ligas que ya no están en el dashboard: {ids_to_deactivate}")
            cur.execute(
                "UPDATE user_leagues SET is_active = FALSE WHERE user_id = %s AND league_id IN %s",
                (user_id, tuple(ids_to_deactivate))
            )
            conn.commit()

    return dashboard_to_id_map



def translate_and_group_transfers(fichajes_data, team_to_dashboard_map):
    grouped_transfers = defaultdict(list)
    processed_transfers_keys = set()

    for team_block in fichajes_data:
        my_team_name = team_block.get("team_name")
        
        # CAMBIO CLAVE: Usamos el nombre del dashboard, no el oficial
        dashboard_league_name = team_to_dashboard_map.get(my_team_name)
        
        if not dashboard_league_name: 
            print(f"  ⚠️ Saltando transfers de '{my_team_name}': No tiene liga dashboard asignada.")
            continue

        for transfer in team_block.get("transfers", []):
            try:
                # Deduplicación (igual qFalsetes)
                transfer_key = (transfer.get("Name"), transfer.get("From"), transfer.get("To"), transfer.get("Gameweek"), transfer.get("Price"))
                if transfer_key in processed_transfers_keys: continue
                processed_transfers_keys.add(transfer_key)

                # Parsing (igual que antes)
                from_parts = transfer.get("From", "").split('\n')
                to_parts = transfer.get("To", "").split('\n')
                from_manager = from_parts[1].strip() if len(from_parts) > 1 else None
                to_manager = to_parts[1].strip() if len(to_parts) > 1 else None

                if to_manager:
                    main_manager = to_manager
                    transaction_type = 'purchase'
                elif from_manager:
                    main_manager = from_manager
                    transaction_type = 'sale'
                else:
                    continue

                # Guardamos bajo la clave del DASHBOARD
                grouped_transfers[dashboard_league_name].append({
                    "playerName": transfer.get("Name"),
                    "managerName": main_manager,
                    "seller_manager": from_manager,
                    "buyer_manager": to_manager,
                    "from_text": transfer.get("From", ""), 
                    "to_text": transfer.get("To", ""),
                    "transactionType": transaction_type,
                    "position": transfer.get("Position"),
                    "round": int(transfer.get("Gameweek", 0)),
                    "baseValue": parse_value_string(transfer.get("Value")),
                    "finalPrice": parse_value_string(transfer.get("Price")),
                    "createdAt": datetime.now()
                })
            except Exception as e:
                print(f"  - Error procesando transfer: {e}")
                continue
                
    return dict(grouped_transfers)


def upload_data_to_postgres(conn, grouped_transfers, league_id_map, user_id):
    print("\n📦 Sincronizando fichajes...")
    with conn.cursor() as cur:
        for league_name, transfers in grouped_transfers.items():
            if league_name in LEAGUES_TO_IGNORE or not transfers: continue
            
            league_id = league_id_map.get(league_name)
            if not league_id: continue
            
            # --- CORRECCIÓN: Deduplicación estricta pre-SQL ---
            # Filtramos duplicados dentro del lote actual basándonos EXACTAMENTE
            # en las columnas de la 'unique_transfer_constraint' de la BD.
            unique_batch = {}
            for t in transfers:
                # Clave única basada en la restricción de la BD
                # (user_id, league_id, round, player_name, manager_name, final_price)
                constraint_key = (
                    user_id,
                    league_id,
                    t['round'],
                    t['playerName'],
                    t['managerName'],
                    t['finalPrice']
                )
                # Si hay duplicados, nos quedamos con el último procesado
                unique_batch[constraint_key] = t
            
            # Generamos las tuplas solo con los datos únicos
            data_tuples = [
                (
                    user_id, league_id, t['playerName'], t['managerName'], t['transactionType'],
                    t['position'], t['round'], t['baseValue'], t['finalPrice'], t['createdAt'],
                    t['seller_manager'], t['buyer_manager'], t['from_text'], t['to_text']
                ) for t in unique_batch.values()
            ]
            # --------------------------------------------------
            
            sql = """
                INSERT INTO transfers (
                    user_id, league_id, player_name, manager_name, transaction_type, 
                    position, round, base_value, final_price, created_at,
                    seller_manager, buyer_manager, from_text, to_text
                ) VALUES %s
                ON CONFLICT (user_id, league_id, round, player_name, manager_name, final_price)
                DO UPDATE SET
                    seller_manager = EXCLUDED.seller_manager,
                    buyer_manager = EXCLUDED.buyer_manager,
                    from_text = EXCLUDED.from_text,
                    to_text = EXCLUDED.to_text;
            """
            
            if data_tuples:
                psycopg2.extras.execute_values(cur, sql, data_tuples, page_size=200)
                print(f"  - Liga '{league_name}': {len(data_tuples)} fichajes procesados (Deduplicados de {len(transfers)} originales).")
            
    conn.commit()

def get_leagues_for_mapping(conn):
    """Obtiene datos de ligas para mapeo, normalizando el campo 'teams'."""
    with conn.cursor() as cur:
        # Nota: SELECT DISTINCT name para evitar duplicados en el mapeo inicial
        # ya que la estructura de equipos es igual para el mismo tipo de liga.
        cur.execute("SELECT DISTINCT ON (name) name AS league_name, teams FROM leagues;")
        
        results = []
        for row in cur.fetchall():
            league_dict = dict(row)
            teams_data = league_dict.get('teams')
            if isinstance(teams_data, str):
                try: league_dict['clubs'] = json.loads(teams_data)
                except json.JSONDecodeError: league_dict['clubs'] = []
            else:
                league_dict['clubs'] = teams_data or []
            if 'teams' in league_dict: del league_dict['teams']
            results.append(league_dict)
        return results

def sync_transfer_list(conn, transfer_list_data, league_id, user_id, scrape_timestamp):
    print(f"  - Sincronizando lista de venta (ID {league_id})...")
    
    with conn.cursor() as cur:
        try:
            # 1. Archivar listados antiguos (Esto se queda igual)
            cur.execute(
                "UPDATE public.transfer_list_players SET is_active = FALSE WHERE user_id = %s AND league_id = %s AND is_active = TRUE;",
                (user_id, league_id)
            )

            if not transfer_list_data:
                conn.commit()
                return

            # --- CORRECCIÓN: DEDUPLICACIÓN PREVIA ---
            # Creamos un diccionario para asegurar que cada par (nombre, vendedor) sea único.
            # Si el scraper trajo el mismo jugador dos veces, nos quedamos con la última versión.
            unique_players = {}
            for p in transfer_list_data:
                # La clave única debe coincidir con la restricción UNIQUE de tu base de datos
                # Constraint: (user_id, league_id, name, seller_manager)
                key = (p['name'], p['seller_manager'])
                unique_players[key] = p
            
            # Ahora generamos las tuplas usando solo los datos únicos
            data_tuples = [
                (
                    user_id, league_id, p['name'], p['seller_manager'], p.get('nationality', 'N/A'),
                    p['position'], p['age'], p['seller_team'], 
                    p['attack'], p['defense'], p['overall'], 
                    p['price'], p.get('value', 0), # Base value
                    scrape_timestamp, scrape_timestamp, True
                ) for p in unique_players.values()
            ]
            # ----------------------------------------

            sql = """
                INSERT INTO public.transfer_list_players (
                    user_id, league_id, name, seller_manager, nationality, position, age, 
                    seller_team, attack, defense, overall, price, base_value, 
                    scrape_id, scraped_at, is_active
                ) VALUES %s
                ON CONFLICT (user_id, league_id, name, seller_manager)
                DO UPDATE SET
                    price = EXCLUDED.price,
                    base_value = EXCLUDED.base_value,
                    nationality = EXCLUDED.nationality,
                    position = EXCLUDED.position,
                    age = EXCLUDED.age,
                    seller_team = EXCLUDED.seller_team,
                    attack = EXCLUDED.attack,
                    defense = EXCLUDED.defense,
                    overall = EXCLUDED.overall,
                    scrape_id = EXCLUDED.scrape_id,
                    scraped_at = EXCLUDED.scraped_at,
                    is_active = TRUE;
            """
            
            if data_tuples:
                psycopg2.extras.execute_values(cur, sql, data_tuples)
                print(f"    - {len(data_tuples)} jugadores insertados/actualizados (Deduplicados de {len(transfer_list_data)} originales).")
            
            conn.commit()

        except Exception as e:
            print(f"    - ❌ ERROR en sync_transfer_list: {e}")
            conn.rollback()



def sync_league_details(conn, standings_data, squad_values_data, league_id_map, dashboard_to_official_map, user_id):
    print("\n?? Sincronizando detalles (Standings/Valores) en 'user_leagues'...")
    with conn.cursor() as cur:
        for official_name, league_id in league_id_map.items():
            if official_name in LEAGUES_TO_IGNORE: continue

            managersByTeam, standings, squad_values = {}, [], []
            dashboard_name = next((dn for dn, on in dashboard_to_official_map.items() if on == official_name), None)
            
            if dashboard_name:
                league_standings_data = next((ls for ls in standings_data if ls.get("league_name") == dashboard_name), None)
                if league_standings_data:
                    standings = league_standings_data.get("standings", [])
                    for team in standings:
                        if team.get("Manager") and team.get("Manager") != "N/A": 
                            managersByTeam[team["Club"]] = team["Manager"]
                            
                league_values_data = next((lv for lv in squad_values_data if lv.get("league_name") == dashboard_name), None)
                if league_values_data:
                    squad_values = league_values_data.get("squad_values_ranking", [])

            sql = """
                INSERT INTO public.user_leagues (user_id, league_id, standings, squad_values, managers_by_team)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, league_id) DO UPDATE SET
                    standings = EXCLUDED.standings,
                    squad_values = EXCLUDED.squad_values,
                    managers_by_team = EXCLUDED.managers_by_team;
            """
            try:
                cur.execute(sql, (user_id, league_id, json.dumps(standings), json.dumps(squad_values), json.dumps(managersByTeam)))
            except Exception as e:
                print(f"  - ? ERROR al sincronizar detalles para '{official_name}': {e}")
    conn.commit()
    

def sync_matches(conn, matches_data, dashboard_to_id_map, user_id):
    print("\n⚽ Sincronizando resultados de partidos...")
    
    with conn.cursor() as cur:
        for league_info in matches_data:
            # Obtenemos el ID real de la liga en la BD
            league_id = dashboard_to_id_map.get(league_info["league_name"])
            if not league_id: continue

            for m in league_info["matches"]:
                sql = """
                    INSERT INTO public.matches (
                        user_id, league_id, round, 
                        home_team, home_manager,    -- FALTABA ESTO
                        away_team, away_manager,    -- FALTABA ESTO
                        home_goals, away_goals, 
                        events, statistics, ratings
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (league_id, round, home_team, away_team) 
                    DO UPDATE SET
                        home_manager = EXCLUDED.home_manager, -- Actualizar si cambia (ej. renuncia)
                        away_manager = EXCLUDED.away_manager, -- Actualizar si cambia
                        home_goals = EXCLUDED.home_goals,
                        away_goals = EXCLUDED.away_goals,
                        events = EXCLUDED.events,
                        statistics = EXCLUDED.statistics,
                        ratings = EXCLUDED.ratings;
                """
                
                # Ejecutamos la consulta pasando los 12 valores
                cur.execute(sql, (
                    user_id, 
                    league_id, 
                    m['round'], 
                    m['home_team'], 
                    m['home_manager'],  # <-- Pasamos el dato del scraper
                    m['away_team'], 
                    m['away_manager'],  # <-- Pasamos el dato del scraper
                    m['home_goals'], 
                    m['away_goals'], 
                    json.dumps(m['events']), 
                    json.dumps(m['statistics']), 
                    json.dumps(m['ratings'])
                ))
    
    conn.commit()
    print("✅ Partidos sincronizados correctamente.")



def get_osm_credentials(conn, user_id):
    print(f"  - Obteniendo credenciales para el usuario ID: {user_id}...")
    with conn.cursor() as cur:
        cur.execute("SELECT osm_username, osm_password FROM public.get_credentials_for_user(%s);", (user_id,))
        creds = cur.fetchone()
        if not creds or not creds['osm_username'] or not creds['osm_password']:
            raise Exception("No se encontraron credenciales para este usuario.")
        return creds['osm_username'], creds['osm_password']

# --- ORQUESTADOR PRINCIPAL ---

def run_update_for_user(user_id):
    print(f"?? Iniciando actualización para el usuario: {user_id}")
    
    # 1. Obtener credenciales
    conn = get_db_connection()
    if not conn: return
    try:
        osm_username, osm_password = get_osm_credentials(conn, user_id)
    except Exception as e:
        print(f"? Error: {e}")
        return
    finally:
        conn.close()

    # 2. Scraping
    try:
        scrape_timestamp = datetime.now() 
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            if not login_to_osm(page, osm_username, osm_password):
                raise Exception("Login fallido.")

            print("\n[1/3] ?? Scraping en curso...")
            transfer_list_data, fichajes_data = get_market_data(page)
            standings_data, squad_values_data = get_league_data(page)
            matches_data = get_match_results(page)
            
            
            if any(d and isinstance(d, list) and d[0].get("error") for d in [transfer_list_data, fichajes_data, standings_data, squad_values_data, matches_data]):
                raise Exception("Error en uno de los scrapers.")
            
            if isinstance(standings_data, dict):
                 raise Exception(f"Error crítico en datos de clasificación: {standings_data.get('error', 'Unknown')}")
             
            print("✅ Scraping completado.")
    except Exception as e:
        print(f"❌ Error crítico en scraping: {e}")
        return

    # 3. Sincronización
    print("\n[2/3] ?? Sincronizando BD...")
    conn = get_db_connection()
    if not conn: return
    
    try:
        # A. Mapeo de ligas
        all_leagues_data_from_db = get_leagues_for_mapping(conn)
        
        # AHORA OBTENEMOS TAMBIÉN EL MAPA EQUIPO->DASHBOARD
        team_to_dashboard_map, dashboard_to_official_map = resolve_active_leagues(
            fichajes_data, all_leagues_data_from_db, standings_data, LEAGUES_TO_IGNORE
        )
        
        if not dashboard_to_official_map:
            print("ℹ️ No hay ligas activas con datos para procesar.")
            return

        print(f"  - Ligas detectadas en dashboard: {list(dashboard_to_official_map.keys())}")

        # B. Obtener IDs (Usando el mapa de Dashboards)
        # IMPORTANTE: Pasamos dashboard_to_official_map
        dashboard_to_id_map = sync_leagues_smart(
            conn, 
            dashboard_to_official_map,  # <-- CAMBIO
            all_leagues_data_from_db, 
            user_id, 
            standings_data
        )
        
        # C. Sincronizar Detalles (Usando el ID correcto para cada Dashboard Name)
        print("\n🔄 Sincronizando detalles...")
        with conn.cursor() as cur:
            for dash_name, league_id in dashboard_to_id_map.items():
                if not league_id: continue
                
                # Buscar datos scrapeados específicos para ESTE dashboard name
                ls_data = next((ls for ls in standings_data if ls.get("league_name") == dash_name), None)
                lv_data = next((lv for lv in squad_values_data if lv.get("league_name") == dash_name), None)
                
                managers, standings, squad_vals = {}, [], []
                
                if ls_data:
                    standings = ls_data.get("standings", [])
                    for team in standings:
                        if team.get("Manager") and team.get("Manager") != "N/A": 
                            managers[team["Club"]] = team["Manager"]
                if lv_data:
                    squad_vals = lv_data.get("squad_values_ranking", [])

                sql = """
                    UPDATE user_leagues 
                    SET standings = %s, squad_values = %s, managers_by_team = %s
                    WHERE user_id = %s AND league_id = %s
                """
                cur.execute(sql, (json.dumps(standings), json.dumps(squad_vals), json.dumps(managers), user_id, league_id))
        conn.commit()

        # D. Sincronizar Fichajes (Agrupados por Dashboard Name)
        grouped_transfers = translate_and_group_transfers(fichajes_data, team_to_dashboard_map)
        upload_data_to_postgres(conn, grouped_transfers, dashboard_to_id_map, user_id)
        
        # E. Sincronizar Mercado
        print("\n📦 Sincronizando Mercado...")
        for dash_name, league_id in dashboard_to_id_map.items():
            list_data = next((i for i in transfer_list_data if i.get("league_name") == dash_name), None)
            if list_data:
                sync_transfer_list(conn, list_data.get("players_on_sale"), league_id, user_id, scrape_timestamp)
                
        # F. ### NUEVO: Sincronizar Partidos (Matches)
        print("\n📦 Sincronizando Resultados...")
        sync_matches(conn, matches_data, dashboard_to_id_map, user_id)

        print("\n✨ Proceso finalizado correctamente.")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_update_for_user(sys.argv[1])
    else:
        print("ERROR: Falta user_id.")
