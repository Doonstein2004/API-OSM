# update_leagues_in_db.py
# Este script solo ejecuta el scraper de ligas y actualiza la tabla 'leagues'.
# Se ejecuta manualmente desde GitHub Actions una o dos veces al año.

import json
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from scraper_leagues import get_data_from_website

# --- Cargar configuración ---
load_dotenv()
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"), "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}

# --- Funciones auxiliares reutilizadas ---
def get_db_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.cursor_factory = psycopg2.extras.DictCursor
        print("✅ Conexión con PostgreSQL establecida.")
        return conn
    except psycopg2.OperationalError as e:
        print(f"❌ ERROR: No se pudo conectar a PostgreSQL: {e}")
        return None

def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    value_str = value_str.lower().strip().replace(',', '')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str) / 1_000_000
    except (ValueError, TypeError): return 0

def sync_all_leagues(conn, all_leagues_data):
    print("\n🔄 Sincronizando TODAS las ligas de OSM con la base de datos...")
    with conn.cursor() as cur:
        for league_info in all_leagues_data:
            league_name = league_info.get("league_name")
            if not league_name: continue

            teams_for_db = [
                {
                    "name": c["club"], "alias": c["club"], 
                    "initialValue": parse_value_string(c["squad_value"]),
                    "fixedIncomePerRound": parse_value_string(c["fixed_income"]),
                    "initialCash": 0, "currentValue": 0
                } for c in league_info.get("clubs", [])
            ]
            teams_json = json.dumps(teams_for_db)
            
            # Sentencia UPSERT: Inserta una nueva liga o actualiza la existente si el nombre coincide.
            sql = """
                INSERT INTO leagues (name, teams) VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    teams = EXCLUDED.teams,
                    updated_at = NOW();
            """
            cur.execute(sql, (league_name, teams_json))
        print(f"  - {cur.rowcount} ligas procesadas (insertadas o actualizadas).")
    conn.commit()

# --- Función Principal ---
def main():
    print("🚀 Iniciando actualización de la lista maestra de ligas...")
    conn = get_db_connection()
    if not conn:
        exit(1)
    
    try:
        all_leagues_data = get_data_from_website()
        if "error" in all_leagues_data:
            print("❌ ERROR: El scraper de ligas falló.")
            return
        
        sync_all_leagues(conn, all_leagues_data)
        print("\n✨ Lista maestra de ligas actualizada en la base de datos.")
    
    finally:
        if conn:
            conn.close()
            print("\n🔌 Conexión con PostgreSQL cerrada.")

if __name__ == "__main__":
    main()