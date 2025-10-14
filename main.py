# main.py
import datetime
import json
import os
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware 

from scraper_leagues import get_data_from_website
from scraper_transfers import get_transfers_data
from scraper_values import get_squad_values_data
from scraper_table import get_standings_data
from dotenv import load_dotenv

# --- NUEVO: Importar Pydantic ---
from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel
from typing import List, Optional

# --- CONFIGURACIÓN ---
load_dotenv()
app = FastAPI(
    title="OSM Analysis API",
    description="API para servir datos de OSM y ejecutar scrapers.",
    version="3.0.0"
)
API_KEY = os.getenv("API_KEY")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# --- NUEVO: Configuración de CORS ---
# Permite que tu frontend (que corre en otro dominio/puerto) pueda hacerle peticiones a esta API
origins = [
    "http://localhost",
    "http://localhost:8080",
    "http://127.0.0.1",
    "http://127.0.0.1:5500", # Típico puerto de Live Server en VSCode para archivos HTML
    "https://api-osm.fly.dev",
    "https://osmtransfers.netlify.app", # Para permitir abrir el index.html directamente desde el sistema de archivos
]

app.add_middleware(
    CORSMiddleware,
    # 2. Usa la lista 'origins' que acabas de definir, en lugar del comodín '*'.
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- NUEVO: Conexión a la base de datos ---
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"), "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}


class CamelModel(BaseModel):
    """Un modelo base que convierte snake_case a camelCase automáticamente."""
    class Config:
        from_attributes = True
        alias_generator = to_camel # <-- La magia está aquí
        populate_by_name = True # Permite usar tanto el nombre original como el alias


class League(CamelModel):
    id: int
    name: str
    type: str

class LeagueDetails(League):
    teams: Optional[list] = []
    managers_by_team: Optional[dict] = {} # El nombre del atributo coincide con la BD
    standings: Optional[list] = []

class Transfer(CamelModel):
    id: int
    player_name: str # El nombre del atributo coincide con la BD
    manager_name: str # El nombre del atributo coincide con la BD
    transaction_type: str # El nombre del atributo coincide con la BD
    position: str
    round: int
    base_value: float # El nombre del atributo coincide con la BD
    final_price: float # El nombre del atributo coincide con la BD
    created_at: datetime.datetime # El nombre del atributo coincide con la BD

    class Config:
        from_attributes = True

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        # Usar un cursor de diccionario para obtener resultados como objetos {columna: valor}
        conn.cursor_factory = psycopg2.extras.DictCursor
        return conn
    except psycopg2.OperationalError as e:
        raise HTTPException(status_code=500, detail=f"Error de conexión con la base de datos: {e}")
    

# --- LÓGICA DE SEGURIDAD (sin cambios) ---
async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == API_KEY:
        return api_key_header
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clave de API no válida o ausente",
        )
        

# --- ENDPOINTS DE LECTURA (MODIFICADOS) --- 
@app.get("/api/leagues", response_model=List[League], response_model_by_alias=True)
def get_all_leagues():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, type FROM leagues ORDER BY name ASC;")
            leagues_data = cur.fetchall()
            # CORRECCIÓN: Reintroducir la conversión explícita a dict
            return [dict(row) for row in leagues_data]
    finally:
        conn.close()

@app.get("/api/test-cors")
def test_cors_endpoint():
    return {"message": "CORS está funcionando!"}

@app.get("/api/leagues/{league_id}", response_model=LeagueDetails, response_model_by_alias=True)
def get_league_data(league_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, type, teams, managers_by_team, standings FROM leagues WHERE id = %s;", (league_id,))
            league_data = cur.fetchone()
            if not league_data:
                raise HTTPException(status_code=404, detail="Liga no encontrada")
            # CORRECCIÓN: Reintroducir la conversión explícita a dict
            return dict(league_data)
    finally:
        conn.close()

@app.get("/api/leagues/{league_id}/transfers", response_model=List[Transfer], response_model_by_alias=True)
def get_league_transfers(league_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, player_name, manager_name, transaction_type, position, round, base_value, final_price, created_at 
                FROM transfers 
                WHERE league_id = %s 
                ORDER BY created_at ASC;
            """, (league_id,))
            transfers_data = cur.fetchall()
            # CORRECCIÓN: Reintroducir la conversión explícita a dict
            return [dict(row) for row in transfers_data]
    finally:
        conn.close()




# --- ENDPOINTS DE LA API (con una pequeña modificación) ---
@app.get("/")
def read_root():
    return {"mensaje": "Bienvenido a tu API privada. Usa /data o /refresh-data."}

@app.get("/data", dependencies=[Security(get_api_key)])
def get_data():
    if not cache or cache["data"] is None:
        raise HTTPException(
            status_code=404,
            detail="La caché está vacía. Ejecuta /refresh-data para obtener los datos."
        )
    return cache

@app.post("/refresh-data", dependencies=[Security(get_api_key)])
def refresh_data():
    print("Solicitud recibida en /refresh-data. Iniciando scraper...")
    try:
        scraped_data = get_data_from_website()
        
        if "error" in scraped_data:
             raise HTTPException(status_code=500, detail=scraped_data["error"])

        global cache
        cache["data"] = scraped_data
        cache["last_updated"] = datetime.datetime.now().isoformat()
        
        # --- 6. MODIFICACIÓN: Guardar datos después de actualizar ---
        save_data_to_json(cache)
        
        print("Caché actualizada y datos guardados.")
        return {"status": "éxito", "message": "Los datos han sido actualizados."}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar el scraper: {str(e)}")
    
    
@app.post("/refresh-transfers", dependencies=[Security(get_api_key)])
def refresh_transfers_data():
    """
    Ejecuta el scraper de fichajes y guarda los resultados en un archivo JSON.
    """
    print("Solicitud recibida en /refresh-fichajes. Iniciando scraper...")
    try:
        scraped_data = get_transfers_data()

        if "error" in scraped_data:
             raise HTTPException(status_code=500, detail=scraped_data["error"])

        # Guardamos los datos en un archivo separado
        with open("fichajes_data.json", "w", encoding="utf-8") as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        print("Datos de fichajes actualizados y guardados.")
        return {"status": "éxito", "message": "Los datos de fichajes han sido actualizados."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar el scraper de fichajes: {str(e)}")
    
    
# Añade este nuevo endpoint al final de main.py
@app.post("/refresh-squad-values", dependencies=[Security(get_api_key)])
def refresh_squad_values_data():
    """
    Ejecuta el scraper de valores de equipo y guarda los resultados en un archivo JSON.
    """
    print("Solicitud recibida en /refresh-squad-values. Iniciando scraper...")
    try:
        scraped_data = get_squad_values_data()
        if "error" in scraped_data:
             raise HTTPException(status_code=500, detail=scraped_data["error"])

        with open("squad_values_data.json", "w", encoding="utf-8") as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        print("Datos de valores de equipo actualizados y guardados.")
        return {"status": "éxito", "message": "Los datos de valores de equipo han sido actualizados."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar el scraper de valores: {str(e)}")
    
    
# Añade este nuevo endpoint al final de main.py
@app.post("/refresh-league-table", dependencies=[Security(get_api_key)])
def refresh_standings_league():
    """
    Ejecuta el scraper de valores de equipo y guarda los resultados en un archivo JSON.
    """
    print("Solicitud recibida en /refresh-squad-values. Iniciando scraper...")
    try:
        scraped_data = get_standings_data()
        if "error" in scraped_data:
             raise HTTPException(status_code=500, detail=scraped_data["error"])

        with open("standings_output.json", "w", encoding="utf-8") as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        print("Datos de valores de equipo actualizados y guardados.")
        return {"status": "éxito", "message": "Los datos de valores de equipo han sido actualizados."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar el scraper de valores: {str(e)}")




