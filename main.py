# main.py
import datetime
import json
import os
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from scraper_leagues import get_data_from_website
from scraper_transfers import get_transfers_data
from scraper_values import get_squad_values_data
from scraper_table import get_standings_data

# --- CONFIGURACIÓN (sin cambios) ---
app = FastAPI(
    title="Mi API Privada",
    description="Una API para obtener datos de un sitio web mediante scraping.",
    version="1.0.0"
)
API_KEY = "$#N!7!T8sGkRmz8vD9Uhr9s&mq&xpc3NBKC2BpN*GX98bKMNDsf2!"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
DATA_FILE = "data.json" # <--- 3. Definir el nombre del archivo de datos

# --- CACHÉ EN MEMORIA (ahora se inicializa desde el archivo) ---
cache = {
    "data": None,
    "last_updated": "Nunca"
}

# --- 4. NUEVAS FUNCIONES AUXILIARES PARA MANEJAR EL ARCHIVO ---
def save_data_to_json(data_to_save):
    """Guarda el diccionario de datos en el archivo JSON."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=4)
    print(f"Datos guardados exitosamente en {DATA_FILE}")

def load_data_from_json():
    """Carga los datos desde el archivo JSON si existe."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            print(f"Cargando datos desde {DATA_FILE}...")
            return json.load(f)
    return {"data": None, "last_updated": "Nunca"} # Devuelve caché vacía si el archivo no existe

# --- 5. MODIFICACIÓN: Cargar datos al iniciar la aplicación ---
@app.on_event("startup")
def startup_event():
    """Al iniciar la API, carga la caché desde el archivo JSON."""
    global cache
    cache = load_data_from_json()

# --- LÓGICA DE SEGURIDAD (sin cambios) ---
async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == API_KEY:
        return api_key_header
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clave de API no válida o ausente",
        )

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

        with open("squad_values_data.json", "w", encoding="utf-8") as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        print("Datos de valores de equipo actualizados y guardados.")
        return {"status": "éxito", "message": "Los datos de valores de equipo han sido actualizados."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar el scraper de valores: {str(e)}")
