# TextureRelay

Bot de Discord que publica **texturas de inventarios en un canal** y **packs de BoxPvP en otro**. Cada sección mantiene su propia cola, pausa y cooldown.

Cada publicación muestra el nombre del pack, creador, versiones disponibles, créditos, imagen cuando la fuente la proporciona y un botón para abrir la página de descarga. **Publica tarjetas y enlaces; no adjunta los ZIP de terceros.**

El pie de todos los embeds, incluidas las vistas previas, dice exactamente: `made by: ake, if you want this DM -Free-`.

## Empezar en Windows

1. Instala [Python 3.11 o superior](https://www.python.org/downloads/).
2. Extrae el ZIP completo y ejecuta `instalar.bat`.
3. Abre `.env` con un editor y completa:

   ```dotenv
   DISCORD_BOT_TOKEN=TOKEN_DE_TU_BOT
   INVENTORY_CHANNEL_ID=ID_DEL_CANAL_DE_INVENTARIOS
   BOXPVP_CHANNEL_ID=ID_DEL_CANAL_DE_BOXPVP
   DISCORD_GUILD_ID=ID_DE_TU_SERVIDOR
   PACKSMC_API_KEY=TU_CLAVE_DE_PACKSMC
   CURSEFORGE_API_KEY=TU_CLAVE_DE_CURSEFORGE
   ```

4. Invita el bot a tu servidor con los scopes **bot** y **applications.commands**. Dale en ambos canales: **Ver canal**, **Enviar mensajes**, **Insertar enlaces** y **Leer historial de mensajes**.
5. Ejecuta `iniciar.bat`. Los dos canales deben ser distintos y estar en el mismo servidor. Los comandos se registran solamente en ese servidor.

Puedes crear el bot en el [Discord Developer Portal](https://discord.com/developers/applications). En Discord, activa Ajustes → Avanzado → Modo desarrollador para copiar los IDs. No necesitas activar Message Content Intent ni Server Members Intent. El token se configura en tu equipo, en `.env`.

## Intervalos

El valor inicial es **120 minutos por canal**: hasta 12 packs diarios en inventarios y 12 en BoxPvP, 24 en total. Consulta las fuentes cada 30 minutos y conserva pendientes del catálogo existente. Si no hay packs adecuados, omite el turno; nunca rellena BoxPvP con PvP genérico.

| Minutos por canal | Máximo por canal/día | Máximo entre los dos canales/día |
| --- | ---: | ---: |
| 3 | 480 | 960 |
| 30 | 48 | 96 |
| 60 | 24 | 48 |
| 120 | 12 | 24 |
| 240 | 6 | 12 |

Las cifras suponen funcionamiento continuo y suficientes packs pendientes. Al volver tras una desconexión no publica en ráfaga los turnos perdidos.

## Comandos de Discord

Solo pueden usarlos miembros con **Administrar servidor** en la comunidad configurada. Las respuestas son privadas para quien ejecuta el comando.

| Comando | Función |
| --- | --- |
| `/packs estado` | Colas, publicados, pausas, cooldowns y errores de las fuentes. |
| `/packs preview seccion` | Ver el siguiente pack sin publicarlo en el canal. |
| `/packs pausar seccion` | Pausar una sección. |
| `/packs reanudar seccion` | Reanudar sin saltarse su cooldown. |
| `/packs cooldown seccion minutos` | Cambiar el intervalo entre 3 minutos y 7 días. |
| `/packs buscar` | Consultar las fuentes y actualizar las colas. |
| `/packs agregar seccion enlace titulo autor imagen` | Añadir un pack que revisaste personalmente; imagen es opcional. |

Los cambios de pausa y cooldown se guardan en `data/texturebot.sqlite3` y sobreviven a reinicios. `config.toml` establece el cooldown inicial; para cambiar una instalación existente usa `/packs cooldown`.

## Fuentes

| Fuente | Integración incluida | Requisito |
| --- | --- | --- |
| [Modrinth](https://docs.modrinth.com/api/) | API de búsqueda de resource packs y detalles. | Sin clave para lectura pública. |
| [PacksMC](https://packsmc.com/docs) | API de catálogo, paginación, imágenes y créditos. | `PACKSMC_API_KEY`: crea una clave en API Access de tu cuenta. |
| [CurseForge](https://docs.curseforge.com/rest-api/) | API de búsqueda restringida a Resource Packs. | `CURSEFORGE_API_KEY`: solicita acceso para servicios de terceros. |
| [TextureHub](https://texturehub.pl/en/api-docs) | API pública de búsqueda y detalles. | Sin token para los endpoints documentados. |
| [Planet Minecraft](https://www.planetminecraft.com/texture-packs/pvp/) y selección propia | Importación mediante `curated.json` o `/packs agregar`. | Revisar y añadir enlaces; no se incluye extracción automática de su web. |

Hay **cuatro conectores automáticos** y una lista complementaria revisada. PacksMC y CurseForge se omiten si no configuraste sus claves; el bot informa cuáles faltan. Con las claves vacías solo se consultan automáticamente Modrinth y TextureHub.

PacksMC devuelve enlaces a la página del pack, y exige conservarlos junto con los créditos. El bot mantiene esos enlaces y evita los packs marcados como exclusivos. Guarda el historial de la selección y consultas, no una copia de todo el catálogo.

La disponibilidad real y el número de packs específicos de cada modalidad dependen de las fuentes. Varios sitios pueden listar el mismo pack: se compara ID, URL, nombre + autor y SHA-256 cuando está disponible. Un cambio importante de título o de autor entre plataformas puede requerir revisión manual.

## Filtros y versiones

Edita `config.toml` y reinicia el bot para cambiar las búsquedas y filtros:

- **Inventarios:** `inventory`, `inventario` y `gui`. Incluye packs GUI, que pueden cambiar varias interfaces además del inventario.
- **BoxPvP:** exige `BoxPvP`, `box pvp`, `box-pvp` o `box_pvp` en el título, descripción o etiquetas. `PvP` solo no basta.
- `exclude` descarta referencias a Bedrock y MCPE por defecto.
- `versions = []` acepta cualquier versión declarada. Puedes usar, por ejemplo, `versions = ["1.21.4"]` para exigir esa versión exacta. Si la fuente no declara compatibilidad, se omite cuando hay un filtro de versiones.

Un pack añadido manualmente queda aprobado para la sección elegida; no depende de palabras clave. Si limitas versiones, usa `curated.json` para indicarlas. Los packs pueden coincidir con más de un filtro, pero la deduplicación es global: se publica una sola vez entre ambos canales.

## Añadir una selección revisada

`curated.json` viene vacío. Añade packs de Planet Minecraft u otra página con este formato, sustituyendo los valores de ejemplo por datos reales:

```json
[
  {
    "section": "inventarios",
    "title": "Nombre real del pack",
    "author": "Creador real",
    "url": "https://example.org/pagina-del-pack",
    "image_url": "https://example.org/captura.png",
    "description": "Breve descripción",
    "versions": ["1.21.4"],
    "credits": [],
    "resolution": "16x"
  }
]
```

`section` admite `inventarios` o `boxpvp`. El bot vuelve a leer el archivo en cada búsqueda. El ejemplo no se importa automáticamente.

## Linux, terminal o VPS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Completa .env antes de conectar el bot.
.venv/bin/python -m texturebot check
.venv/bin/python -m texturebot preview --refresh
.venv/bin/python -m texturebot run
```

`check` valida la configuración local y no prueba las credenciales. `preview --refresh` consulta las fuentes y prepara las colas **sin enviar mensajes a Discord**. `status` muestra el estado guardado.

Con Docker, después de completar `.env`:

```bash
docker compose up -d --build
docker compose logs -f
```

Para seguir publicando 24/7, el PC o VPS debe permanecer encendido con el proceso en ejecución. Conserva `data/` al actualizar o cambiar de host: ahí está el historial que evita repetir packs. Solo una instancia puede publicar con la misma base de datos en un host.

## Errores y recuperación

- Consulta `/packs estado` y `logs/bot.log`. Los logs rotan para limitar el espacio.
- Una fuente caída no detiene las demás. Hay timeouts, reintentos acotados, espera tras HTTP 429 y caché de detalles durante 24 horas.
- PacksMC tiene un presupuesto conservador de **900 consultas en una ventana móvil de 24 horas**, persistente entre reinicios. Se reservan menos consultas que su cuota gratuita documentada de 1,000.
- Si Discord recibe el mensaje pero la conexión se corta antes de confirmar, se usa el mismo nonce y se comprueba el historial antes de intentar reenviar. Una entrega incierta bloquea su sección mientras se revisa; no genera nuevos envíos por esa sección durante la duda.
- Si no se puede comprobar el historial, o la búsqueda alcanza 500 mensajes sin resolverlo, el envío permanece bloqueado. Restaura los permisos o revisa ese caso antes de modificar el estado. Las publicaciones normales no necesitan leer mensajes fuera de los dos canales configurados.

## Verificación incluida

```bash
python -m unittest discover -s tests -v
```

Las pruebas cubren separación de secciones, paginación, créditos, formatos de las cuatro APIs, deduplicación, reinicios, cooldowns, entregas inciertas, errores aislados y respuestas HTTP por fragmentos.

La entrega se validó con pruebas locales y los formatos publicados en las documentaciones oficiales. En el entorno de construcción, las conexiones directas a Modrinth y TextureHub fallaron por resolución DNS; no se pudo validar la búsqueda en vivo. Tampoco se conectó a tu Discord: no se proporcionaron token, IDs de canales ni claves de PacksMC/CurseForge. Ejecuta `preview --refresh` en tu host para comprobar las fuentes disponibles antes de iniciar.
