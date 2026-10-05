# Herramientas de automatización

Repositorio de scripts y utilidades de automatización para operación de
bases de datos y servicios en AWS.

## Herramientas

| Herramienta | Descripción | Tecnologías |
|---|---|---|
| [s3-sqlserver](herramientas/s3-sqlserver/) | Exporta a S3 el inventario de bases SQL Server desde la tabla de auditoría en Oracle | Python, Oracle, Secrets Manager, S3 |

## Estructura

```
herramientas/
  <nombre-herramienta>/
    README.md          qué hace y cómo se usa
    requirements.txt   dependencias (si es Python)
    .env.example       variables de configuración (sin valores reales)
    ...                código
```

## Reglas para publicar

- **Nunca** subir contraseñas, llaves, tokens ni archivos `.env`
  (ya están en `.gitignore`). Las credenciales se leen de Secrets Manager
  o de variables de entorno.
- No subir binarios de terceros (Oracle Instant Client, `.dll`, `.so`):
  se documenta cómo instalarlos en el README de cada herramienta.
- No subir salidas generadas (`.csv`, logs).
- Cada herramienta nueva va en su propia carpeta bajo `herramientas/`
  con su `README.md` y se agrega a la tabla de arriba.
