"""
Exporta a S3 el inventario de bases de datos SQL Server.

Lee la tabla de auditoría SCHAUDBD.STAGE_AUDIT_DB_SIZES (Oracle RDS),
filtra los registros cuyo motor es SQL Server, genera un CSV y lo
carga en un bucket S3.

Toda la configuración sensible (secreto, bucket, ruta del cliente
Oracle) se toma de variables de entorno o de un archivo .env; nada
queda escrito en el código. Ver .env.example y README.md.
"""

import argparse
import base64
import csv
import io
import json
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import oracledb
from botocore.exceptions import BotoCoreError, ClientError

try:
    # Opcional: si python-dotenv está instalado, carga el .env
    # que esté junto a este script.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURACIÓN (desde variables de entorno)
# ============================================================

def _env(nombre: str, default: str | None = None) -> str | None:
    valor = os.getenv(nombre, default)
    if valor is None:
        return None
    valor = valor.strip()
    return valor or default


def _env_bool(nombre: str, default: bool) -> bool:
    valor = _env(nombre)
    if valor is None:
        return default
    return valor.lower() in ("1", "true", "si", "sí", "yes", "y")


AWS_REGION = _env("AWS_REGION", "us-east-1")
SECRET_NAME = _env("SECRET_NAME")

S3_BUCKET = _env("S3_BUCKET")
S3_PREFIX = _env("S3_PREFIX", "Reports_CYR")

# Thick Mode es necesario cuando la base exige funcionalidades que
# el modo Thin de python-oracledb no soporta (p. ej. Native Network
# Encryption o verificadores de contraseña antiguos).
ORACLE_THICK_MODE = _env_bool("ORACLE_THICK_MODE", True)

# Carpeta que contiene directamente oci.dll (Windows) o
# libclntsh.so (Linux). En Linux puede dejarse vacía si el cliente
# está en LD_LIBRARY_PATH.
ORACLE_CLIENT_PATH = _env("ORACLE_CLIENT_PATH")

# Opciones válidas: "SID" o "SERVICE_NAME".
ORACLE_CONNECT_MODE = (_env("ORACLE_CONNECT_MODE", "SID") or "SID").upper()


def validar_configuracion() -> None:
    faltantes = [
        nombre
        for nombre, valor in (
            ("SECRET_NAME", SECRET_NAME),
            ("S3_BUCKET", S3_BUCKET),
        )
        if not valor
    ]

    if faltantes:
        raise RuntimeError(
            "Faltan variables de entorno obligatorias: "
            f"{', '.join(faltantes)}. "
            "Copia .env.example como .env y complétalo."
        )

    if ORACLE_CONNECT_MODE not in ("SID", "SERVICE_NAME"):
        raise ValueError(
            "ORACLE_CONNECT_MODE debe ser SID o SERVICE_NAME."
        )


# ============================================================
# CONSULTA DE SOLO LECTURA
# ============================================================

SQL_QUERY = """
SELECT
    FECHA,
    CUENTA_AWS,
    HOST,
    ENGINE,
    DBNAME,
    DBINSTANCEIDENTIFIER,
    SIZE_GB,
    AMBIENTE
FROM SCHAUDBD.STAGE_AUDIT_DB_SIZES
WHERE LOWER(TRIM(ENGINE)) LIKE '%sqlserver%'
ORDER BY
    CUENTA_AWS,
    DBINSTANCEIDENTIFIER,
    DBNAME
"""


# ============================================================
# ORACLE THICK MODE
# ============================================================

def activar_oracle_thick() -> None:
    if not ORACLE_THICK_MODE:
        logger.info("ORACLE_THICK_MODE=false: se usará modo Thin.")
        return

    if ORACLE_CLIENT_PATH:
        logger.info("Cargando Oracle Client desde: %s", ORACLE_CLIENT_PATH)

        if not os.path.isdir(ORACLE_CLIENT_PATH):
            raise FileNotFoundError(
                f"No existe la carpeta de Oracle Client: {ORACLE_CLIENT_PATH}"
            )

        if sys.platform.startswith("win"):
            oci_path = os.path.join(ORACLE_CLIENT_PATH, "oci.dll")
            if not os.path.isfile(oci_path):
                raise FileNotFoundError(
                    f"No se encontró oci.dll en: {ORACLE_CLIENT_PATH}"
                )

        oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)
    else:
        logger.info(
            "ORACLE_CLIENT_PATH vacío: se buscará Oracle Client "
            "en las rutas del sistema."
        )
        oracledb.init_oracle_client()

    if oracledb.is_thin_mode():
        raise RuntimeError("Oracle continúa funcionando en modo Thin.")

    logger.info("Oracle Thick Mode activado correctamente.")


# ============================================================
# CLIENTES AWS
# ============================================================

def crear_clientes_aws():
    session = boto3.Session(region_name=AWS_REGION)

    if session.get_credentials() is None:
        raise RuntimeError(
            "No se encontraron credenciales AWS. Configura las "
            "credenciales temporales (o AWS_PROFILE) en la misma "
            "terminal antes de ejecutar."
        )

    logger.info("Credenciales AWS detectadas correctamente.")

    return session.client("secretsmanager"), session.client("s3")


# ============================================================
# OBTENER SECRETO
# ============================================================

def obtener_secreto(secrets_client) -> dict[str, Any]:
    logger.info("Consultando secreto: %s", SECRET_NAME)

    response = secrets_client.get_secret_value(SecretId=SECRET_NAME)

    if "SecretString" in response:
        contenido = response["SecretString"]
    else:
        contenido = base64.b64decode(response["SecretBinary"]).decode("utf-8")

    secreto = json.loads(contenido)

    logger.info("Secreto obtenido correctamente.")

    return secreto


def obtener_campo(
    secreto: dict[str, Any],
    nombres: list[str],
    obligatorio: bool = True,
    valor_default: Any = None,
) -> Any:
    secreto_normalizado = {
        str(clave).strip().lower(): valor for clave, valor in secreto.items()
    }

    for nombre in nombres:
        valor = secreto_normalizado.get(nombre.strip().lower())
        if valor is not None and str(valor).strip():
            return valor

    if obligatorio:
        raise KeyError(
            f"No se encontró ninguno de estos campos en el secreto: {nombres}"
        )

    return valor_default


# ============================================================
# CONEXIÓN ORACLE
# ============================================================

def construir_dsn(host: str, puerto: int, dbname: str) -> str:
    logger.info("Tipo de conexión Oracle: %s", ORACLE_CONNECT_MODE)

    if ORACLE_CONNECT_MODE == "SID":
        return oracledb.makedsn(host=host, port=puerto, sid=dbname)

    return oracledb.makedsn(host=host, port=puerto, service_name=dbname)


def conectar_oracle(secreto: dict[str, Any]) -> oracledb.Connection:
    usuario = obtener_campo(secreto, ["username", "user", "usuario"])
    password = obtener_campo(
        secreto, ["password", "passwd", "pass", "contraseña"]
    )
    host = obtener_campo(secreto, ["host", "hostname", "endpoint", "address"])
    puerto = int(
        obtener_campo(
            secreto, ["port", "puerto"], obligatorio=False, valor_default=1521
        )
    )
    dbname = obtener_campo(
        secreto,
        ["dbname", "database", "service_name", "servicename", "service", "sid"],
    )

    logger.info("Conectando a Oracle: %s:%s/%s", host, puerto, dbname)

    conexion = oracledb.connect(
        user=usuario,
        password=password,
        dsn=construir_dsn(host=host, puerto=puerto, dbname=dbname),
    )

    logger.info("Conexión Oracle establecida correctamente.")

    return conexion


# ============================================================
# CONSULTAR SOLO SQL SERVER
# ============================================================

def consultar_sql_server(
    conexion: oracledb.Connection,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    cursor = conexion.cursor()

    try:
        cursor.execute("SET TRANSACTION READ ONLY")

        logger.info("Ejecutando consulta de registros SQL Server.")
        cursor.execute(SQL_QUERY)

        columnas = [descripcion[0] for descripcion in cursor.description]
        filas = cursor.fetchall()

        logger.info("Registros SQL Server encontrados: %s", len(filas))

        return columnas, filas

    finally:
        cursor.close()


# ============================================================
# GENERAR CSV
# ============================================================

def formatear_valor(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, Decimal):
        return format(valor, "f")
    return str(valor)


def generar_csv(columnas: list[str], filas: list[tuple[Any, ...]]) -> bytes:
    with io.StringIO() as buffer_csv:
        writer = csv.writer(
            buffer_csv,
            delimiter=";",
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writerow(columnas)
        for fila in filas:
            writer.writerow([formatear_valor(valor) for valor in fila])

        # utf-8-sig para que Excel reconozca los acentos.
        return buffer_csv.getvalue().encode("utf-8-sig")


# ============================================================
# CARGAR CSV A S3
# ============================================================

def nombre_archivo(ahora: datetime) -> str:
    return f"Informe_SQLServer_{ahora.strftime('%Y%m%d_%H%M%S')}.csv"


def cargar_csv_a_s3(
    s3_client,
    contenido_csv: bytes,
    cantidad_registros: int,
    ahora: datetime,
) -> str:
    s3_key = f"{S3_PREFIX.rstrip('/')}/{nombre_archivo(ahora)}"

    logger.info("Cargando archivo en s3://%s/%s", S3_BUCKET, s3_key)

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=contenido_csv,
        ContentType="text/csv; charset=utf-8",
        Metadata={
            "motor": "sqlserver",
            "cantidad-registros": str(cantidad_registros),
            "tabla-origen": "STAGE_AUDIT_DB_SIZES",
            "fecha-generacion": ahora.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )

    logger.info("Archivo cargado correctamente.")

    return f"s3://{S3_BUCKET}/{s3_key}"


def guardar_csv_local(contenido_csv: bytes, carpeta: str, ahora: datetime) -> str:
    destino = Path(carpeta)
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / nombre_archivo(ahora)
    ruta.write_bytes(contenido_csv)
    logger.info("Copia local guardada en: %s", ruta)
    return str(ruta)


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

def parsear_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta a S3 el inventario de bases SQL Server."
    )
    parser.add_argument(
        "--sin-s3",
        action="store_true",
        help="No sube a S3; útil para probar la consulta (requiere --local).",
    )
    parser.add_argument(
        "--local",
        metavar="CARPETA",
        help="Guarda además una copia del CSV en esta carpeta.",
    )
    args = parser.parse_args(argv)

    if args.sin_s3 and not args.local:
        parser.error("--sin-s3 requiere --local CARPETA")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parsear_argumentos(argv)
    conexion = None

    try:
        print()
        print("=" * 70)
        print("INICIANDO EXPORTACIÓN DE SQL SERVER")
        print("=" * 70)

        validar_configuracion()
        activar_oracle_thick()

        secrets_client, s3_client = crear_clientes_aws()
        secreto = obtener_secreto(secrets_client)

        conexion = conectar_oracle(secreto)
        columnas, filas = consultar_sql_server(conexion)

        contenido_csv = generar_csv(columnas, filas)
        ahora = datetime.now()

        ubicaciones = []
        if args.local:
            ubicaciones.append(guardar_csv_local(contenido_csv, args.local, ahora))
        if not args.sin_s3:
            ubicaciones.append(
                cargar_csv_a_s3(
                    s3_client=s3_client,
                    contenido_csv=contenido_csv,
                    cantidad_registros=len(filas),
                    ahora=ahora,
                )
            )

        print()
        print("=" * 70)
        print("PROCESO FINALIZADO CORRECTAMENTE")
        print("=" * 70)
        print(f"Registros encontrados : {len(filas)}")
        for ubicacion in ubicaciones:
            print(f"Archivo guardado en   : {ubicacion}")
        print("=" * 70)

        return 0

    except KeyError as error:
        logger.exception("Falta un campo requerido en el secreto: %s", error)

    except oracledb.Error as error:
        logger.exception("Error de Oracle: %s", error)

    except (ClientError, BotoCoreError) as error:
        logger.exception("Error consumiendo servicios AWS: %s", error)

    except (RuntimeError, FileNotFoundError, ValueError) as error:
        # Errores de configuración: mensaje claro, sin traza.
        logger.error("%s", error)

    except Exception as error:
        logger.exception("Error inesperado: %s", error)

    finally:
        if conexion is not None:
            try:
                conexion.rollback()
                conexion.close()
                logger.info("Conexión Oracle cerrada correctamente.")
            except oracledb.Error as error:
                logger.warning("No fue posible cerrar la conexión: %s", error)

    return 1


if __name__ == "__main__":
    sys.exit(main())
