# run_update.py
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

# --- AÑADIDO: Importar las funciones de los scrapers ---
from scraper_transfers import get_transfers_data
from scraper_league_details import get_league_data

# --- CONFIGURACIÓN ---
load_dotenv()
LEAGUES_TO_IGNORE = ["Champions Cup 25/26", "Greece"]

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
        # Usamos DictCursor para que sea más fácil trabajar con los resultados
        conn.cursor_factory = psycopg2.extras.DictCursor
        print("✅ Conexión con PostgreSQL establecida.")
        return conn
    except psycopg2.OperationalError as e:
        print(f"❌ ERROR: No se pudo conectar a PostgreSQL. Revisa tus credenciales en .env y que la BD esté en ejecución. Error: {e}")
        return None # Devuelve None en lugar de salir para un manejo más limpio

def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    # CORREGIDO: Eliminada línea duplicada
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
        # --- CORRECCIÓN: Iterar sobre 'clubs' que es una lista de diccionarios de equipo ---
        for club in league.get("clubs", []):
            # --- CAMBIO CLAVE AQUÍ ---
            # El diccionario del equipo usa la clave 'name', no 'club'.
            normalized_club = normalize_team_name(club["name"])
            team_to_leagues[normalized_club].append(league_name)
            league_to_teams[league_name].add(normalized_club)
    return dict(team_to_leagues), dict(league_to_teams)


def resolve_active_leagues(my_fichajes, all_leagues_data):
    my_managed_teams = {team['team_name']: team for team in my_fichajes}
    team_to_leagues, league_to_teams = create_league_maps(all_leagues_data)
    resolved_map = {}
    for original_name, team_data in my_managed_teams.items():
        normalized_name = normalize_team_name(original_name)
        candidate_leagues = team_to_leagues.get(normalized_name, [])
        if len(candidate_leagues) == 1:
            resolved_map[original_name] = candidate_leagues[0]
            print(f"  - Equipo '{original_name}' asignado unívocamente a la liga '{candidate_leagues[0]}'.")
        elif len(candidate_leagues) > 1:
            print(f"  - Ambigüedad para '{original_name}'. Candidatas: {candidate_leagues}. Analizando...")
            league_scores = {league: 0 for league in candidate_leagues}
            witness_teams = set()
            for transfer in team_data.get("transfers", []):
                witness_teams.add(normalize_team_name(transfer.get("From", "").split('\n')[0]))
                witness_teams.add(normalize_team_name(transfer.get("To", "").split('\n')[0]))
            for witness in witness_teams:
                for league_name in candidate_leagues:
                    if witness in league_to_teams.get(league_name, set()):
                        league_scores[league_name] += 1
            if not any(league_scores.values()):
                 print(f"  - ADVERTENCIA: No se pudo resolver la ambigüedad para '{original_name}'.")
                 continue
            winner_league = max(league_scores, key=league_scores.get)
            resolved_map[original_name] = winner_league
            print(f"  - Análisis completado. '{original_name}' asignado a '{winner_league}'.")
        else:
            print(f"  - ADVERTENCIA: El equipo '{original_name}' no se encontró en ninguna liga.")
    return resolved_map

def create_dashboard_to_official_league_map(standings_data, team_to_resolved_league):
    print("\n🗺️ Creando mapa de nombres de liga (Dashboard -> Oficial)...")
    dashboard_map = {}
    my_managed_teams = set(team_to_resolved_league.keys())
    for league_standings in standings_data:
        dashboard_name = league_standings.get("league_name")
        found_match = False
        for team in league_standings.get("standings", []):
            team_name_in_standings = team.get("Club")
            for my_team in my_managed_teams:
                if normalize_team_name(team_name_in_standings) == normalize_team_name(my_team):
                    official_name = team_to_resolved_league[my_team]
                    dashboard_map[dashboard_name] = official_name
                    print(f"  - Mapeado '{dashboard_name}' -> '{official_name}'")
                    found_match = True
                    break
            if found_match:
                break
    return dashboard_map

# --- FUNCIONES DE SINCRONIZACIÓN CON POSTGRESQL ---

def sync_leagues_with_postgres(conn, active_league_names, all_leagues_data):
    print("\n🔄 Sincronizando ligas con PostgreSQL...")
    league_id_map = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM leagues;")
        existing_leagues = {name: league_id for league_id, name in cur.fetchall()}
        print(f"  - Encontradas {len(existing_leagues)} ligas en PostgreSQL.")
        for league_name in active_league_names:
            league_info = next((l for l in all_leagues_data if l.get('league_name') == league_name), None)
            teams_for_db = []
            if league_info:
                teams_for_db = [{"name": c["club"], "alias": c["club"], "initialValue": parse_value_string(c["squad_value"]), "fixedIncomePerRound": parse_value_string(c["fixed_income"]), "initialCash": 0, "currentValue": 0} for c in league_info.get("clubs", [])]
            teams_json = json.dumps(teams_for_db)
            sql = """
                INSERT INTO leagues (name, teams, managers_by_team, standings) VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET teams = EXCLUDED.teams, updated_at = NOW()
                RETURNING id;
            """
            cur.execute(sql, (league_name, teams_json, '{}', '[]'))
            league_id = cur.fetchone()['id']
            league_id_map[league_name] = league_id
            print(f"  - Liga '{league_name}' asegurada en la BD con ID: {league_id}")
    conn.commit()
    return league_id_map

def translate_and_group_transfers(fichajes_data, team_to_resolved_league):
    grouped_transfers = defaultdict(list)
    for team_block in fichajes_data:
        my_team_name = team_block.get("team_name")
        league_name = team_to_resolved_league.get(my_team_name)
        if not league_name: continue
        for transfer in team_block.get("transfers", []):
            from_parts = transfer.get("From", "").split('\n')
            to_parts = transfer.get("To", "").split('\n')
            from_manager, to_manager = (from_parts[1] if len(from_parts) > 1 else None, to_parts[1] if len(to_parts) > 1 else None)
            managerName, transaction_type = (to_manager, 'purchase') if to_manager else (from_manager, 'sale')
            if managerName and transaction_type:
                grouped_transfers[league_name].append({"playerName": transfer.get("Name"), "managerName": managerName, "transactionType": transaction_type, "position": transfer.get("Position"), "round": int(transfer.get("Gameweek", 0)), "baseValue": parse_value_string(transfer.get("Value")), "finalPrice": parse_value_string(transfer.get("Price")), "createdAt": datetime.now()})
    return dict(grouped_transfers)

def upload_data_to_postgres(conn, grouped_transfers, league_id_map, user_id):
    # Ya no borramos todo. En su lugar, insertamos de forma incremental.
    print("\n🔄 Sincronizando fichajes de forma inteligente...")
    
    with conn.cursor() as cur:
        total_new_transfers = 0
        for league_name, transfers in grouped_transfers.items():
            if league_name in LEAGUES_TO_IGNORE:
                continue
            
            league_id = league_id_map.get(league_name)
            if not league_id: continue
            
            new_transfers_in_league = 0
            
            # --- INICIO DE LA LÓGICA INTELIGENTE ---
            # Preparamos la sentencia SQL con la cláusula ON CONFLICT
            sql = """
                INSERT INTO transfers (
                    user_id, league_id, player_name, manager_name, transaction_type, 
                    position, round, base_value, final_price, created_at
                ) VALUES (
                    %(user_id)s, %(league_id)s, %(playerName)s, %(managerName)s, %(transactionType)s, 
                    %(position)s, %(round)s, %(baseValue)s, %(finalPrice)s, %(createdAt)s
                )
                ON CONFLICT (user_id, league_id, round, player_name, manager_name, final_price)
                DO NOTHING;
            """
            
            # Iteramos sobre cada fichaje scrapeado
            for t in transfers:
                # Añadimos el league_id a cada diccionario de fichaje
                t_with_ids = {**t, "league_id": league_id, "user_id": user_id}
                
                # Ejecutamos la inserción para este fichaje
                cur.execute(sql, t_with_ids)
                
                # 'cur.rowcount' será 1 si la inserción fue exitosa (el fichaje era nuevo)
                # y 0 si hubo un conflicto (el fichaje ya existía y no se hizo nada).
                if cur.rowcount > 0:
                    new_transfers_in_league += 1
            # --- FIN DE LA LÓGICA INTELIGENTE ---
            
            if new_transfers_in_league > 0:
                print(f"  - Liga '{league_name}': {new_transfers_in_league} nuevos fichajes insertados.")
                total_new_transfers += new_transfers_in_league

    if total_new_transfers == 0:
        print("  - No se encontraron nuevos fichajes en ninguna liga.")
    else:
        print(f"\n✅ Total de {total_new_transfers} nuevos fichajes insertados en la base de datos.")

    conn.commit()

# --- NUEVA FUNCIÓN ---
def get_leagues_for_mapping(conn, user_id):
    """Obtiene los datos de las ligas desde la BD en el formato que create_league_maps necesita."""
    print("  - Obteniendo lista de ligas desde la base de datos para el mapeo...")
    with conn.cursor() as cur:
        cur.execute("SELECT name AS league_name, teams FROM leagues WHERE user_id = %s;", (user_id,))
        
        results = []
        for row in cur.fetchall():
            # Convertimos la fila a un diccionario de Python normal
            league_dict = dict(row)
            
            # --- INICIO DE LA CORRECCIÓN CLAVE ---
            # La columna 'teams' viene de la BD. Puede ser un string JSON o ya un objeto Python (lista).
            teams_data = league_dict.get('teams')
            
            # Si es un string, lo parseamos a un objeto Python. Si no, lo usamos como está.
            if isinstance(teams_data, str):
                try:
                    league_dict['clubs'] = json.loads(teams_data)
                except json.JSONDecodeError:
                    league_dict['clubs'] = [] # Si el JSON está corrupto, usa una lista vacía
            else:
                league_dict['clubs'] = teams_data or [] # Si es None o ya es una lista
            
            # Eliminamos la clave original 'teams' para evitar confusión
            if 'teams' in league_dict:
                del league_dict['teams']
            
            results.append(league_dict)
            # --- FIN DE LA CORRECCIÓN ---
            
        return results


    
    
def get_all_leagues_from_db(cursor):
    cursor.execute("SELECT id, name, type, teams, managers_by_team, standings FROM leagues;")
    return {row['id']: dict(row) for row in cursor.fetchall()}

def sync_league_details(conn, standings_data, squad_values_data, league_id_map, dashboard_to_official_map, user_id):
    print("\n🔄 Sincronizando Mánagers, Valores y Clasificaciones...")
    with conn.cursor() as cur:

        # 1. Iteramos solo sobre las ligas activas que nos interesan. ¡Esto es correcto!
        for official_name, league_id in league_id_map.items():
            if official_name in LEAGUES_TO_IGNORE: 
                continue # Saltamos las ligas ignoradas

            managersByTeam, current_values, standings = {}, {}, []
            dashboard_name = next((dn for dn, on in dashboard_to_official_map.items() if on == official_name), None)
            
            # 2. La lógica para obtener los nuevos datos es correcta.
            if dashboard_name:
                league_standings_data = next((ls for ls in standings_data if ls.get("league_name") == dashboard_name), None)
                if league_standings_data:
                    standings = league_standings_data.get("standings", [])
                    for team in standings:
                        if team.get("Manager") and team.get("Manager") != "N/A": 
                            managersByTeam[team["Club"]] = team["Manager"]
                            
                league_values_data = next((lv for lv in squad_values_data if lv.get("league_name") == dashboard_name), None)
                if league_values_data:
                    for team in league_values_data.get("squad_values_ranking", []): 
                        current_values[team["Club"]] = parse_value_string(team["Value"])

            # --- INICIO DE LA CORRECCIÓN ---
            # 3. Necesitamos obtener la lista de equipos ACTUAL de la BD para esta liga específica.
            cur.execute("SELECT teams FROM leagues WHERE id = %s;", (league_id,))
            result = cur.fetchone()
            
            # Nos aseguramos de que el resultado no sea nulo y que la columna 'teams' tampoco lo sea.
            updated_teams = result['teams'] if result and 'teams' in result else []
            if updated_teams is None:
                updated_teams = []
            # --- FIN DE LA CORRECCIÓN ---

            # 4. Actualizamos la lista de equipos con los nuevos valores. ¡Esto es correcto!
            for team_obj in updated_teams:
                if team_obj.get("name") in current_values: 
                    team_obj["currentValue"] = current_values[team_obj.get("name")]

            # 5. Ejecutamos el UPDATE final para esta liga. ¡Esto es correcto!
            sql = "UPDATE leagues SET managers_by_team = %s, teams = %s, standings = %s WHERE id = %s AND user_id = %s;"
            cur.execute(sql, (json.dumps(managersByTeam), json.dumps(updated_teams), json.dumps(standings), league_id, user_id))
            print(f"  - Detalles actualizados para '{official_name}'.")
            
    conn.commit()


def get_osm_credentials(conn, user_id):
    """Obtiene y desencripta las credenciales de OSM para un usuario específico."""
    print(f"  - Obteniendo credenciales para el usuario ID: {user_id}")
    with conn.cursor() as cur:
        # Esta consulta usa la función de desencriptación de pgsodium
        sql = """
            SELECT 
                osm_username,
                convert_from(
                    pgsodium.crypto_aead_det_decrypt(
                        osm_password_encrypted,
                        convert_to(id::text, 'utf8'),
                        'bf2a7b1b1c31114e9f783104c4b22055' -- La misma clave de contexto que usaste al encriptar
                    ),
                    'utf8'
                ) AS osm_password
            FROM public.users
            WHERE id = %s;
        """
        cur.execute(sql, (user_id,))
        creds = cur.fetchone()
        if not creds or not creds['osm_username'] or not creds['osm_password']:
            raise Exception("No se encontraron o no se pudieron desencriptar las credenciales de OSM para este usuario.")
        
        print("  - Credenciales obtenidas y desencriptadas con éxito.")
        return creds['osm_username'], creds['osm_password']


# --- ORQUESTADOR PRINCIPAL ---

def run_update_for_user(user_id):
    print(f"🚀 Iniciando actualización a demanda para el usuario: {user_id}")
    
    conn = get_db_connection()
    if not conn: return

    try:
        osm_username, osm_password = get_osm_credentials(conn, user_id)
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            if not login_to_osm(page, osm_username, osm_password):
                raise Exception("El proceso de login falló.")

            print("\n[1/3] 🌐 Login exitoso. Ejecutando scrapers...")
            fichajes_data = get_transfers_data(page)
            standings_data, squad_values_data = get_league_data(page) 
            print("✅ Datos dinámicos obtenidos con éxito.")

        # --- Lógica de procesamiento y sincronización ---
        print("\n[2/3] 🧠 Procesando y sincronizando datos...")
        
        # --- CORRECCIÓN 1: RESETEAR 'is_active' PRIMERO ---
        with conn.cursor() as cur:
            print("  - Reseteando estado de ligas activas para este usuario...")
            cur.execute("UPDATE leagues SET is_active = FALSE WHERE user_id = %s;", (user_id,))
        conn.commit()
        
        # Pasamos el user_id para filtrar correctamente
        all_leagues_data_from_db = get_leagues_for_mapping(conn, user_id)
        team_to_resolved_league = resolve_active_leagues(fichajes_data, all_leagues_data_from_db)
        dashboard_to_official_map = create_dashboard_to_official_league_map(standings_data, team_to_resolved_league)
        official_leagues_from_transfers = set(team_to_resolved_league.values())
        filtered_leagues_to_process = {name for name in official_leagues_from_transfers if name not in LEAGUES_TO_IGNORE}
        
        if not filtered_leagues_to_process:
            print("ℹ️ No se encontraron ligas con fichajes para procesar. Finalizando.")
            return
        print(f"  - Ligas activas encontradas: {list(filtered_leagues_to_process)}")

        print("\n[3/3] 🔄 Sincronizando datos con PostgreSQL...")
        
        # --- CORRECCIÓN 2: OBTENER EL MAPA DE IDs FILTRADO POR USUARIO ---
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM leagues WHERE user_id = %s;", (user_id,))
            full_league_id_map = {row['name']: row['id'] for row in cur.fetchall()}
        
        active_league_id_map = {
            name: full_league_id_map[name] 
            for name in filtered_leagues_to_process 
            if name in full_league_id_map
        }
        
        if active_league_id_map:
            with conn.cursor() as cur:
                active_ids = tuple(active_league_id_map.values())
                # Usar ANY() es más seguro que formatear el string
                query = "UPDATE leagues SET is_active = TRUE, last_scraped_at = %s WHERE id = ANY(%s) AND user_id = %s;"
                cur.execute(query, (datetime.now(), list(active_ids), user_id))
                print(f"  - {cur.rowcount} ligas marcadas como activas.")
            conn.commit()
            
        sync_league_details(conn, standings_data, squad_values_data, active_league_id_map, dashboard_to_official_map, user_id)
        grouped_transfers = translate_and_group_transfers(fichajes_data, team_to_resolved_league)
        upload_data_to_postgres(conn, grouped_transfers, active_league_id_map, user_id)

        print(f"\n✨ Proceso de sincronización completado para el usuario {user_id}.")
    finally:
        if conn:
            conn.close()
            print("\n🔌 Conexión con PostgreSQL cerrada.")
            

if __name__ == "__main__":
    load_dotenv() # Cargar variables de entorno para pruebas locales
    if len(sys.argv) > 1:
        user_id_from_cli = sys.argv[1]
        run_update_for_user(user_id_from_cli)
    else:
        print("ERROR: Se requiere un user_id. Uso: python run_update_for_user.py <user_id>")