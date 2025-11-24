# run_update_for_user.py
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
    print("\n[3.1] ?? Resolviendo ligas activas por composición de equipos...")
    
    my_managed_teams = {team['team_name'] for team in my_fichajes if team.get("transfers")}
    team_to_leagues_master, league_to_teams_master = create_league_maps(all_leagues_data)
    resolved_map = {}

    current_competitors_map = defaultdict(set)
    dashboard_to_team_map = {}
    for league_info in league_details_data:
        managed_team_name = league_info.get("team_name")
        dashboard_name = league_info.get("league_name")
        if managed_team_name in my_managed_teams:
            dashboard_to_team_map[dashboard_name] = managed_team_name
            current_competitors = {normalize_team_name(team['Club']) for team in league_info.get("standings", [])}
            current_competitors_map[managed_team_name] = current_competitors

    for original_name in my_managed_teams:
        normalized_name = normalize_team_name(original_name)
        candidate_leagues = [name for name in team_to_leagues_master.get(normalized_name, []) if name not in leagues_to_ignore]

        if not candidate_leagues:
            print(f"  - No hay ligas candidatas válidas para '{original_name}'. Saltando.")
            continue

        if len(candidate_leagues) == 1:
            resolved_map[original_name] = candidate_leagues[0]
            print(f"  - Equipo '{original_name}' asignado unívocamente a la liga '{candidate_leagues[0]}'.")
            continue

        print(f"  - Ambigüedad para '{original_name}'. Candidatas válidas: {candidate_leagues}.")
        current_competitors = current_competitors_map.get(original_name)
        if not current_competitors:
            print(f"  - ADVERTENCIA: Sin datos de clasificación para resolver '{original_name}'.")
            continue

        scores = {name: len(current_competitors.intersection(league_to_teams_master.get(name, set()))) for name in candidate_leagues}
        best_score = max(scores.values()) if scores else 0
        
        if best_score > len(current_competitors) / 2:
            winners = [name for name, score in scores.items() if score == best_score]
            if len(winners) == 1:
                winner_league = winners[0]
                resolved_map[original_name] = winner_league
                print(f"  - Ganador: '{winner_league}' con {scores[winner_league]} coincidencias.")
            else:
                print(f"  - ADVERTENCIA: Múltiples ganadores con la misma puntuación. Ganadores: {winners}")
        else:
            print(f"  - ADVERTENCIA: Coincidencia insuficiente para resolver ambigüedad.")

    dashboard_map = {}
    for dash_name, team_name in dashboard_to_team_map.items():
        if team_name in resolved_map:
            dashboard_map[dash_name] = resolved_map[team_name]

    print(f"\n[3.2] ??? Mapa de Dashboard a Oficial creado: {dashboard_map}")
    return resolved_map, dashboard_map

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

def sync_leagues_smart(conn, active_league_names, all_leagues_data, user_id, standings_data, dashboard_to_official_map):
    print("\n?? Sincronizando ligas (Lógica de Temporadas)...")
    active_league_id_map = {}

    for league_name in active_league_names:
        # 1. Obtener datos actuales (managers) para comparar
        league_info = next((l for l in all_leagues_data if l.get('league_name') == league_name), None)
        if not league_info: continue
        
        # Buscar managers actuales desde los standings scrapeados
        current_managers_set = set()
        dashboard_name = next((dn for dn, on in dashboard_to_official_map.items() if on == league_name), None)
        if dashboard_name:
            ls_data = next((ls for ls in standings_data if ls.get("league_name") == dashboard_name), None)
            if ls_data:
                for team in ls_data.get("standings", []):
                    mgr = team.get("Manager", "N/A")
                    if mgr and mgr != "N/A": current_managers_set.add(mgr)

        # 2. Decidir si usamos la existente o creamos nueva
        old_league_id, action = check_league_continuity(conn, user_id, league_name, current_managers_set)

        league_id_to_use = None

        if action == 'NEW_SEASON':
            if old_league_id:
                print(f"  ? Detectada NUEVA TEMPORADA para '{league_name}'. Archivando ID {old_league_id}...")
                with conn.cursor() as cur:
                    cur.execute("UPDATE user_leagues SET is_active = FALSE WHERE user_id = %s AND league_id = %s", (user_id, old_league_id))
            
            print(f"  ?? Creando nueva instancia de liga para '{league_name}'...")
            raw_clubs = league_info.get("clubs", [])
            if raw_clubs and "club" in raw_clubs[0]:
                # Caso A: Datos vienen del Scraper (tienen clave 'club') -> Convertir
                teams_for_db = [{"name": c["club"], "alias": c["club"], "initialValue": parse_value_string(c["squad_value"]), "fixedIncomePerRound": parse_value_string(c["fixed_income"]), "initialCash": 0, "currentValue": 0} for c in raw_clubs]
            else:
                # Caso B: Datos vienen de la BD (tienen clave 'name') -> Usar tal cual
                # Como ya están en la BD, ya tienen el formato correcto jsonb
                teams_for_db = raw_clubs

            
            with conn.cursor() as cur:
                # INSERT simple, permitiendo nombres duplicados con diferentes IDs
                cur.execute(
                    "INSERT INTO leagues (name, teams) VALUES (%s, %s) RETURNING id;",
                    (league_name, json.dumps(teams_for_db))
                )
                league_id_to_use = cur.fetchone()['id']

                
                # Crear la relación activa en user_leagues
                cur.execute(
                    "INSERT INTO user_leagues (user_id, league_id, is_active, last_scraped_at) VALUES (%s, %s, TRUE, NOW())",
                    (user_id, league_id_to_use)
                )
            conn.commit()
            print(f"  ? Nueva temporada creada con ID: {league_id_to_use}")

        else: # USE_EXISTING
            league_id_to_use = old_league_id
            print(f"  ? Continuando temporada existente (ID {league_id_to_use}) para '{league_name}'.")

        active_league_id_map[league_name] = league_id_to_use

    return active_league_id_map


def translate_and_group_transfers(fichajes_data, team_to_resolved_league):
    grouped_transfers = defaultdict(list)
    processed_transfers_keys = set()

    for team_block in fichajes_data:
        my_team_name = team_block.get("team_name")
        league_name = team_to_resolved_league.get(my_team_name)
        if not league_name: continue

        for transfer in team_block.get("transfers", []):
            try:
                transfer_key = (
                    transfer.get("Name"), transfer.get("From"), transfer.get("To"),
                    transfer.get("Gameweek"), transfer.get("Price")
                )
                if transfer_key in processed_transfers_keys: continue
                processed_transfers_keys.add(transfer_key)

                from_raw = transfer.get("From", "")
                to_raw = transfer.get("To", "")
                from_parts = from_raw.split('\n')
                to_parts = to_raw.split('\n')
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

                grouped_transfers[league_name].append({
                    "playerName": transfer.get("Name"),
                    "managerName": main_manager,
                    "seller_manager": from_manager,
                    "buyer_manager": to_manager,
                    "from_text": from_raw, 
                    "to_text": to_raw,
                    "transactionType": transaction_type,
                    "position": transfer.get("Position"),
                    "round": int(transfer.get("Gameweek", 0)),
                    "baseValue": parse_value_string(transfer.get("Value")),
                    "finalPrice": parse_value_string(transfer.get("Price")),
                    "createdAt": datetime.now()
                })
            except Exception as e:
                print(f"  - ADVERTENCIA: Saltando un fichaje durante el procesamiento. Error: {e}")
                continue
                
    return dict(grouped_transfers)

def upload_data_to_postgres(conn, grouped_transfers, league_id_map, user_id):
    print("\n?? Sincronizando fichajes...")
    with conn.cursor() as cur:
        for league_name, transfers in grouped_transfers.items():
            if league_name in LEAGUES_TO_IGNORE or not transfers: continue
            
            league_id = league_id_map.get(league_name)
            if not league_id: continue
            
            data_tuples = [
                (
                    user_id, league_id, t['playerName'], t['managerName'], t['transactionType'],
                    t['position'], t['round'], t['baseValue'], t['finalPrice'], t['createdAt'],
                    t['seller_manager'], t['buyer_manager'], t['from_text'], t['to_text']
                ) for t in transfers
            ]
            
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
            psycopg2.extras.execute_values(cur, sql, data_tuples, page_size=200)
            print(f"  - Liga '{league_name}': {len(data_tuples)} fichajes procesados.")
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
    print(f"  - Sincronizando lista de transferencias (ID {league_id})...")
    with conn.cursor() as cur:
        try:
            # Archivar listados antiguos
            cur.execute(
                "UPDATE public.transfer_list_players SET is_active = FALSE WHERE user_id = %s AND league_id = %s AND is_active = TRUE;",
                (user_id, league_id)
            )

            if not transfer_list_data:
                conn.commit()
                return

            sql = """
                INSERT INTO public.transfer_list_players (
                    user_id, league_id, name, seller_manager, nationality, position, age, 
                    seller_team, attack, defense, overall, price, 
                    scrape_id, scraped_at, is_active
                ) VALUES %s
                ON CONFLICT (user_id, league_id, name, seller_manager)
                DO UPDATE SET
                    price = EXCLUDED.price,
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
            data_tuples = [
                (
                    user_id, league_id, p['name'], p['seller_manager'], p.get('nationality', 'N/A'),
                    p['position'], p['age'], p['seller_team'], 
                    p['attack'], p['defense'], p['overall'], p['price'],
                    scrape_timestamp, scrape_timestamp, True
                ) for p in transfer_list_data
            ]
            if data_tuples:
                psycopg2.extras.execute_values(cur, sql, data_tuples)
            conn.commit()
        except Exception as e:
            print(f"    - ? ERROR en sync_transfer_list: {e}")
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
            
            if any(d and isinstance(d, list) and d[0].get("error") for d in [transfer_list_data, fichajes_data, standings_data, squad_values_data]):
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
        team_to_resolved_league, dashboard_to_official_map = resolve_active_leagues(
            fichajes_data, all_leagues_data_from_db, standings_data, LEAGUES_TO_IGNORE
        )
        official_leagues_from_transfers = set(team_to_resolved_league.values())
        
        if not official_leagues_from_transfers:
            print("?? No hay ligas activas con datos para procesar.")
            return

        print(f"  - Ligas activas: {list(official_leagues_from_transfers)}")

        # B. Obtener IDs Correctos (Gestión de Temporadas)
        # ESTE ES EL CAMBIO CLAVE: Usamos sync_leagues_smart para obtener los IDs
        active_league_id_map = sync_leagues_smart(
            conn, 
            official_leagues_from_transfers, 
            all_leagues_data_from_db, 
            user_id, 
            standings_data, 
            dashboard_to_official_map
        )
        
        # C. Asegurar que las ligas activas estén marcadas en user_leagues
        if active_league_id_map:
            with conn.cursor() as cur:
                # Resetear activas para asegurar limpieza (opcional pero seguro)
                cur.execute("UPDATE public.user_leagues SET is_active = FALSE WHERE user_id = %s;", (user_id,))
                
                # Activar las actuales
                now = datetime.now()
                sql_activate = """
                    INSERT INTO public.user_leagues (user_id, league_id, is_active, last_scraped_at)
                    VALUES (%s, %s, TRUE, %s)
                    ON CONFLICT (user_id, league_id) DO UPDATE SET
                        is_active = TRUE,
                        last_scraped_at = EXCLUDED.last_scraped_at;
                """
                for league_id in active_league_id_map.values():
                    cur.execute(sql_activate, (user_id, league_id, now))
            conn.commit()
            
        # D. Sincronizar Detalles, Fichajes y Mercado usando los IDs correctos
        sync_league_details(conn, standings_data, squad_values_data, active_league_id_map, dashboard_to_official_map, user_id)
        grouped_transfers = translate_and_group_transfers(fichajes_data, team_to_resolved_league)
        upload_data_to_postgres(conn, grouped_transfers, active_league_id_map, user_id)
        
        print("\n?? Sincronizando Mercado...")
        for dash_name, official_name in dashboard_to_official_map.items():
            if official_name in active_league_id_map:
                league_id = active_league_id_map[official_name]
                list_data = next((i for i in transfer_list_data if i.get("league_name") == dash_name), None)
                if list_data:
                    sync_transfer_list(conn, list_data.get("players_on_sale"), league_id, user_id, scrape_timestamp)

        print("\n? Proceso finalizado correctamente.")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_update_for_user(sys.argv[1])
    else:
        print("ERROR: Falta user_id.")