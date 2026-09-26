# higgsfield-cli · comando `hf`

Herramienta de terminal para la API de Higgsfield (open.higgsfield.ai): imágenes, vídeo y workflows. Usa el SDK oficial `higgsfield-client`.

Qué hace en cada generación:

- valida los parámetros contra el esquema oficial del modelo **antes de gastar**;
- sube tus archivos locales (`@ruta`);
- espera con tiempo máximo y sobrevive a cortes de red;
- descarga el resultado;
- opcionalmente, lo copia a Cloudinary;
- deja todo registrado en un historial JSONL.

## Instalación en cualquier ordenador

Requisitos: Python 3.9 o superior y git con acceso a tu GitHub (el repo es privado). El código se instala en `~/.higgsfield-cli/venv`, y la configuración y las salidas quedan en `~/.higgsfield-cli/`, igual en todos los ordenadores.

**macOS / Linux**

```bash
python3 -m venv ~/.higgsfield-cli/venv
~/.higgsfield-cli/venv/bin/pip install -q -U pip
~/.higgsfield-cli/venv/bin/pip install -q "higgsfield-cli[cloudinary,mcp] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
~/.higgsfield-cli/venv/bin/hf init --open      # crea ~/.higgsfield-cli/.env.local: pon HF_KEY (y CLOUDINARY_URL)
~/.higgsfield-cli/venv/bin/hf doctor           # comprueba todo sin gastar créditos
~/.higgsfield-cli/venv/bin/hf mcp install      # opcional: que Claude pueda usarlo (ver abajo)
```

**Windows (PowerShell)**

```powershell
py -m venv $HOME\.higgsfield-cli\venv
& $HOME\.higgsfield-cli\venv\Scripts\pip install -q "higgsfield-cli[cloudinary,mcp] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
& $HOME\.higgsfield-cli\venv\Scripts\hf init --open
& $HOME\.higgsfield-cli\venv\Scripts\hf doctor
```

**Actualizar** a la última versión de `main`: repite el `pip install` añadiendo `--upgrade --force-reinstall --no-deps`.

**Desarrollo** (con el repo clonado): `~/.higgsfield-cli/venv/bin/pip install -e '.[cloudinary,mcp,dev]'` desde la carpeta del repo. Así los cambios en el código se aplican sin reinstalar.

**Nombre del comando.** `hf` coincide con la CLI de Hugging Face, así que no lo pongo en el `PATH`. Llámalo por su ruta (`~/.higgsfield-cli/venv/bin/hf`), que es lo que deben usar scripts y agentes (AgentOS, launchd). Si quieres un atajo en tu terminal, usa un alias con otro nombre, por ejemplo `alias higgs="$HOME/.higgsfield-cli/venv/bin/hf"`.

## Uso

```bash
hf models                         # todos los modelos (alias → endpoint)
hf models seedance-2.5-i2v        # parámetros, valores permitidos y precio publicado

hf gen z-image "a red apple on white" --ratio 1:1
hf gen seedance-2.5 "A cinematic scene at sunset" --duration 5 --res 720p --ratio 16:9
hf gen seedance-2.5-i2v "slow push-in" --image @frames/intro.png --duration 8
hf gen kling-3-motion --image @actor.png --video @moves.mp4
hf gen product-shots "cream tube on green tiles" image_urls=@product.jpg
hf gen soul-2 "editorial portrait" batch_size=4 --dry-run   # valida y muestra la petición, no envía nada

hf batch jobs.jsonl --concurrency 2
hf history
hf spend --days 7                 # lanzadas, cobradas y USD estimado por modelo
hf result <request_id>            # recuperar una petición (timeout, corte, Ctrl+C)
hf status <request_id> · hf cancel <request_id> · hf upload archivo.png · hf presets
```

**Parámetros.** Cualquier parámetro del modelo va como `clave=valor`. Los tipos se convierten según el esquema del modelo. En los parámetros que son listas, repetir la clave añade elementos: `image_urls=@a.png image_urls=@b.png`. Los atajos `--ratio`, `--res`, `--duration`, `--seed`, `--image`, `--video` y `--audio` se traducen al nombre que use cada modelo.

**Archivos locales.** `@ruta` sirve en cualquier valor. El archivo se sube a Higgsfield justo antes de enviar y se sustituye por su URL pública.

**Batch.** Un `.jsonl` con un trabajo por línea, o un `.json` con una lista:

```json
{"model": "seedance-2.5", "prompt": "sunset over Madrid", "params": {"duration": 6, "aspect_ratio": "9:16"}, "name": "madrid-sunset"}
```

Antes de lanzar nada se validan todos los trabajos y se suben los archivos. Si uno está mal, no se gasta nada.

**Salida.** Los archivos se guardan en `~/.higgsfield-cli/output/AAAA-MM-DD/<alias>_<request_id>.<ext>` y el historial en `~/.higgsfield-cli/output/history.jsonl`. `HFCLI_HOME` o `HFCLI_OUTPUT` cambian esas rutas. Con `CLOUDINARY_URL` configurada, cada archivo se copia además a `higgsfield/AAAA-MM-DD/` en Cloudinary. Higgsfield solo garantiza sus URLs durante 7 días.

**`--json`** imprime un único objeto con `ok`, `status`, `request_id`, `outputs[].url|file|cloudinary_url`, `error` y `warnings`. El progreso va por stderr, así que stdout queda limpio para scripts.

### Control del gasto

- **Coste antes de lanzar.** `hf gen` muestra el coste estimado antes de enviar, y `--dry-run` lo incluye sin gastar nada.
- **Qué modelos tienen estimación.** Seedance 2.5 (texto, imagen y referencias sin vídeo de entrada), Wan 3.0 y MiniMax H3 referencias. Salen de los precios públicos, antes de descuentos.
- **Qué modelos no.** Los que Higgsfield no publica (Z-Image, Soul, Kling…) aparecen como «sin precio».
- **Gasto acumulado.** `hf spend` suma por modelo lo lanzado, lo completado, lo que no se cobra (fallos y NSFW, según la FAQ de Higgsfield) y lo pendiente.
- **Importe real.** Está en open.higgsfield.ai/usage.
- **Freno duro.** El saldo de la API es de prepago: solo se gasta lo que recargas.

### Códigos de salida

| Código | Significado |
|---|---|
| 0 | Generado y guardado |
| 1 | No generado: `failed`, `nsfw`, `canceled`, error de API o de red, timeout |
| 2 | Uso o configuración: parámetros inválidos, falta clave, archivo inexistente (no se ha gastado nada) |
| 3 | Generado, pero falló la descarga o Cloudinary (la URL está en la salida y en el historial) |

## Usar hf desde Claude (servidor MCP)

`hf mcp install` registra el servidor en la app de Claude del ordenador. Añade `higgsfield` a `claude_desktop_config.json`, guarda una copia de seguridad y no toca el resto de servidores. Después, cierra Claude del todo (Cmd+Q) y ábrelo. Claude (chat, Cowork o Code) podrá generar directamente en ese ordenador, con la clave siempre en local. Necesita Python 3.10 o superior y el extra `mcp`.

| Herramienta | Qué hace | Coste |
|---|---|---|
| `hf_models`, `hf_model_info` | catálogo, parámetros y precio publicado | gratis |
| `hf_generate` | valida por defecto (`dry_run=true`); con `dry_run=false` lanza y devuelve un `request_id` | de pago |
| `hf_wait` | espera 35-45 s como máximo; si `done=false`, se vuelve a llamar. Al terminar descarga, copia a Cloudinary, anota en el historial y devuelve miniaturas | gratis |
| `hf_spend` | lanzadas, cobradas y USD estimado por modelo en los últimos N días | gratis |
| `hf_history`, `hf_cancel`, `hf_doctor` | historial, cancelar en cola, comprobar configuración | gratis |

**Protecciones de gasto** (las aplica el servidor, no dependen del modelo):

- Antes de subir o enviar nada, cada generación queda reservada en `output/mcp_ledger.jsonl`.
- Una petición idéntica en los últimos 15 minutos se rechaza, esté enviándose, aceptada o en estado incierto. Así un reintento por timeout no cobra dos veces.
- Si ese registro no se puede escribir, no se envía nada.
- Cada validación y cada envío llevan el coste estimado cuando el precio público lo permite. Si no se puede calcular, se dice.
- Tope opcional de generaciones vía MCP en 24 h móviles: `HFCLI_MCP_MAX_JOBS_PER_DAY` en `.env.local`. Sin esa variable no hay tope.

Los logs del servidor están en `~/Library/Logs/Claude/mcp-server-higgsfield.log` (macOS).

## Catálogo

`src/hfcli/catalog.json` contiene 52 endpoints, con 52 alias y 3 workflows. Cada uno lleva su esquema de entrada oficial y el precio publicado cuando existe. Los esquemas se contrastaron con dos fuentes públicas distintas de Higgsfield y coinciden en los 52.

- **Actualizar:** `python scripts/update_catalog.py` (o `--check` para solo ver cambios).
- **Endpoint nuevo:** `hf gen <endpoint/completo> ...` funciona aunque no esté en el catálogo, sin validación.
- **Validación desactualizada:** `--no-validate`.

## Límites conocidos

- La API pública no incluye todo Higgsfield: no hay voces, doblaje, upscale, Shorts Studio ni publicación en TikTok. Eso sigue en el conector de Higgsfield de Claude.
- `hf presets` y la comprobación de clave de `hf doctor` están probadas solo con una clave inválida. Con una válida, confirma la primera vez que salen bien.
- 37 de los 52 endpoints no publican precio. Revisa el consumo en open.higgsfield.ai/usage.

## Desarrollo

```bash
~/.higgsfield-cli/venv/bin/pip install -e '.[cloudinary,dev]'
~/.higgsfield-cli/venv/bin/pytest -q      # tests sin red: SDK y Cloudinary simulados, descargas contra un servidor local
```

Los cambios van por rama y PR. `examples/seedance_subscribe.py` es el ejemplo mínimo original con `subscribe`.
