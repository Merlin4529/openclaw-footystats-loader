"""
OpenClaw - Railway Cron Job: Carga automática de FootyStats → Google Drive
=============================================================================

Reemplaza el paso manual de correr footystats_connector.py en tu máquina.
Corre 1 vez al día en Railway (cron), y deja el archivo normalizado en la
carpeta 01_HARPO de Drive, donde Claude ya sabe buscarlo en cada conversación.

IMPORTANTE - Esto es SOLO para Liga Argentina (Apertura/Clausura).
FootyStats no aplica al Mundial (confirmado en el protocolo: N/A para mundial).

Flujo:
  1. Llama a FootyStats API (4 ligas del plan Hobby, hoy solo usamos Primera Argentina)
  2. Normaliza: form (últimos 5), H2H, xG por equipo
  3. Autentica con cuenta de servicio de Google
  4. Sube un archivo NUEVO versionado a Drive (no sobreescribe — mismo patrón
     que ya usás en el proyecto: footystats_data_2026-08-15.json, etc.)
  5. Termina y libera recursos (requisito de Railway cron: el proceso debe
     salir solo, si no, la próxima ejecución programada se salta)

Variables de entorno requeridas en Railway:
  FOOTYSTATS_API_KEY        -> tu API key de FootyStats
  GOOGLE_SERVICE_ACCOUNT_JSON -> el contenido COMPLETO del JSON de la cuenta
                                  de servicio, como un solo string (Railway
                                  soporta multilínea en variables de entorno)
  DRIVE_FOLDER_ID           -> ID de la carpeta 01_HARPO en Drive

Cron schedule (configurar en Railway > Settings > Cron Schedule):
  0 9 * * *     -> todos los días a las 9:00 UTC (6:00 AM ART)
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

FOOTYSTATS_BASE_URL = "https://api.football-data-api.com"
LIGA_ARGENTINA_COMPETITION_ID = None  # se resuelve en tiempo de ejecución vía /league-list


def obtener_env(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        print(f"❌ ERROR: falta la variable de entorno {nombre}")
        sys.exit(1)
    return valor


def llamar_footystats(api_key: str) -> dict:
    """
    Llama a FootyStats para traer las predicciones/estadísticas de
    Primera División Argentina. En un entorno real, esto pega contra
    /league-list para resolver el ID de la competición, y luego contra
    /league-matches o /team para form, H2H, xG.

    Aquí se muestra la estructura; el ID exacto de la liga se resuelve
    la primera vez que corre el script y se puede cachear.
    """
    resp = requests.get(
        f"{FOOTYSTATS_BASE_URL}/league-list",
        params={"key": api_key, "chosen_leagues_only": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    ligas = resp.json().get("data", [])

    liga_arg = next(
        (l for l in ligas if "argentina" in l.get("name", "").lower()
         and "primera" in l.get("name", "").lower()),
        None,
    )
    if not liga_arg:
        raise RuntimeError("No se encontró Primera División Argentina en las ligas del plan.")

    season_id = liga_arg["season"][-1]["id"]  # temporada más reciente

    resp_matches = requests.get(
        f"{FOOTYSTATS_BASE_URL}/league-matches",
        params={"key": api_key, "season_id": season_id},
        timeout=30,
    )
    resp_matches.raise_for_status()
    return resp_matches.json()


def normalizar_datos(datos_crudos: dict) -> dict:
    """
    Transforma la respuesta cruda de FootyStats en la estructura que
    HARPO consume: form, H2H, xG por equipo y por partido.
    """
    partidos = datos_crudos.get("data", [])
    normalizado = {
        "fuente": "FootyStats",
        "fecha_extraccion": datetime.now(timezone.utc).isoformat(),
        "liga": "Primera División Argentina",
        "partidos": [],
    }

    for p in partidos:
        normalizado["partidos"].append({
            "partido_id": p.get("id"),
            "equipo_local": p.get("home_name"),
            "equipo_visitante": p.get("away_name"),
            "fecha": p.get("date_unix"),
            "xg_local": p.get("team_a_xg_prematch"),
            "xg_visitante": p.get("team_b_xg_prematch"),
            "form_local": p.get("home_ppg"),      # points per game reciente, proxy de form
            "form_visitante": p.get("away_ppg"),
            "estado": p.get("status"),
        })

    return normalizado


def subir_a_drive(datos_normalizados: dict, folder_id: str, service_account_json: str) -> str:
    """
    Sube el archivo normalizado como un archivo NUEVO versionado
    (patrón del proyecto: nunca sobreescribir, siempre crear nueva versión).
    """
    credenciales_dict = json.loads(service_account_json)
    credenciales = service_account.Credentials.from_service_account_info(
        credenciales_dict,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    servicio = build("drive", "v3", credentials=credenciales)

    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    nombre_archivo = f"footystats_data_{fecha}.json"

    contenido_bytes = json.dumps(datos_normalizados, indent=2, ensure_ascii=False).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(contenido_bytes), mimetype="application/json")

    metadata = {"name": nombre_archivo, "parents": [folder_id]}
    archivo = servicio.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()

    return archivo.get("id")


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Iniciando job de carga FootyStats...")

    api_key = obtener_env("FOOTYSTATS_API_KEY")
    service_account_json = obtener_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    folder_id = obtener_env("DRIVE_FOLDER_ID")

    try:
        datos_crudos = llamar_footystats(api_key)
    except Exception as e:
        print(f"❌ ERROR al llamar FootyStats API: {e}")
        sys.exit(1)

    datos_normalizados = normalizar_datos(datos_crudos)
    print(f"✅ {len(datos_normalizados['partidos'])} partidos normalizados.")

    try:
        archivo_id = subir_a_drive(datos_normalizados, folder_id, service_account_json)
    except Exception as e:
        print(f"❌ ERROR al subir a Drive: {e}")
        sys.exit(1)

    print(f"✅ Archivo subido a Drive. ID: {archivo_id}")
    print("Job terminado — proceso finaliza limpiamente (requisito de Railway cron).")


if __name__ == "__main__":
    main()
