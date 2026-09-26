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
~/.higgsfield-cli/venv/bin/pip install -q "higgsfield-cli[cloudinary] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
~/.higgsfield-cli/venv/bin/hf init --open      # crea ~/.higgsfield-cli/.env.local: pon HF_KEY (y CLOUDINARY_URL)
~/.higgsfield-cli/venv/bin/hf doctor           # comprueba todo sin gastar créditos
```

**Windows (PowerShell)**

```powershell
py -m venv $HOME\.higgsfield-cli\venv
& $HOME\.higgsfield-cli\venv\Scripts\pip install -q "higgsfield-cli[cloudinary] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
& $HOME\.higgsfield-cli\venv\Scripts\hf init --open
& $HOME\.higgsfield-cli\venv\Scripts\hf doctor
```

**Actualizar** a la última versión de `main`: repite el `pip install` añadiendo `--upgrade --force-reinstall --no-deps`.

**Desarrollo** (con el repo clonado): `~/.higgsfield-cli/venv/bin/pip install -e '.[cloudinary,dev]'` desde la carpeta del repo. Así los cambios en el código se aplican sin reinstalar.

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

### Códigos de salida

| Código | Significado |
|---|---|
| 0 | Generado y guardado |
| 1 | No generado: `failed`, `nsfw`, `canceled`, error de API o de red, timeout |
| 2 | Uso o configuración: parámetros inválidos, falta clave, archivo inexistente (no se ha gastado nada) |
| 3 | Generado, pero falló la descarga o Cloudinary (la URL está en la salida y en el historial) |

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
