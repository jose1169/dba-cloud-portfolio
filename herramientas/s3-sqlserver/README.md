# Exportación de inventario SQL Server a S3

Consulta la tabla de auditoría `SCHAUDBD.STAGE_AUDIT_DB_SIZES` (Oracle RDS),
filtra las bases cuyo motor es **SQL Server**, genera un CSV (separador `;`,
UTF-8 con BOM para Excel) y lo carga en S3 como
`<S3_PREFIX>/Informe_SQLServer_AAAAMMDD_HHMMSS.csv`.

**Herramientas:** Python · AWS Secrets Manager · Amazon S3 · Oracle (python-oracledb, Thick Mode)

## Flujo

1. Lee las credenciales de Oracle desde AWS Secrets Manager.
2. Se conecta a Oracle en modo Thick y abre una transacción de **solo lectura**.
3. Ejecuta la consulta y arma el CSV en memoria.
4. Sube el archivo a S3 con metadatos (motor, cantidad de registros, fecha).

## Requisitos

- Python 3.10+
- Oracle Instant Client / Oracle Client instalado en el equipo
  (no se incluye en el repositorio por licencia; descárgalo de oracle.com)
- Credenciales AWS con permisos:
  - `secretsmanager:GetSecretValue` sobre el secreto
  - `s3:PutObject` sobre `arn:aws:s3:::<bucket>/<prefijo>/*`

## Instalación

```powershell
cd herramientas\s3-sqlserver
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # y edita los valores
```

## Configuración (`.env`)

| Variable | Obligatoria | Descripción |
|---|---|---|
| `SECRET_NAME` | Sí | Secreto de Secrets Manager con `username`, `password`, `host`, `port`, `dbname` |
| `S3_BUCKET` | Sí | Bucket destino |
| `S3_PREFIX` | No | Carpeta en el bucket (por defecto `Reports_CYR`) |
| `AWS_REGION` | No | Por defecto `us-east-1` |
| `ORACLE_THICK_MODE` | No | `true` (por defecto) o `false` para modo Thin |
| `ORACLE_CLIENT_PATH` | No | Carpeta con `oci.dll` / `libclntsh.so` |
| `ORACLE_CONNECT_MODE` | No | `SID` (por defecto) o `SERVICE_NAME` |

## Uso

```powershell
# Credenciales temporales en la misma terminal
$env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."; $env:AWS_SESSION_TOKEN="..."
# o bien: $env:AWS_PROFILE="mi-perfil"

python exportar_sqlserver_s3.py                       # sube a S3
python exportar_sqlserver_s3.py --local salida        # sube a S3 y guarda copia local
python exportar_sqlserver_s3.py --sin-s3 --local salida   # solo prueba local
```

Código de salida `0` si terminó bien, `1` si hubo error (útil para programadores de tareas).
