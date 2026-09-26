---
name: higgsfield-cli
description: Generar imágenes y vídeos con la API de Higgsfield mediante el comando `hf` (repo alvaro-zamorano/higgsfield-cli): instalarlo en cualquier ordenador, generaciones, lotes, recuperar request_id, historial, Cloudinary. Usar cuando Álvaro pida generar con la API de Higgsfield o AgentOS necesite medios.
---

# hf — API de Higgsfield desde la terminal

Código en GitHub (privado): `alvaro-zamorano/higgsfield-cli`. En cualquier ordenador la instalación queda igual:

- **Binario:** `~/.higgsfield-cli/venv/bin/hf` (Windows: `~\.higgsfield-cli\venv\Scripts\hf.exe`).
- **Configuración:** `~/.higgsfield-cli/.env.local`, con `HF_KEY` y, opcionalmente, `CLOUDINARY_URL`.
- **Salidas:** `~/.higgsfield-cli/output/AAAA-MM-DD/`. Historial: `~/.higgsfield-cli/output/history.jsonl`. Con `CLOUDINARY_URL` configurada, además hay una copia en Cloudinary en `higgsfield/AAAA-MM-DD/`.

Llama siempre a `hf` por su ruta. Ese nombre coincide con la CLI de Hugging Face, así que un `hf` a secas en el `PATH` puede ser otra cosa.

## ¿Está instalado en este ordenador?

Compruébalo con `test -x ~/.higgsfield-cli/venv/bin/hf && ~/.higgsfield-cli/venv/bin/hf doctor`. Si no está, la instalación (macOS/Linux) es esta:

```bash
python3 -m venv ~/.higgsfield-cli/venv
~/.higgsfield-cli/venv/bin/pip install -q -U pip
~/.higgsfield-cli/venv/bin/pip install -q "higgsfield-cli[cloudinary] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
~/.higgsfield-cli/venv/bin/hf init --open
~/.higgsfield-cli/venv/bin/hf doctor
```

Para Windows y para actualizar a la última versión, mira el README del repo. El `pip install` necesita que git tenga acceso a la cuenta de GitHub de Álvaro, porque el repo es privado. La clave la escribe Álvaro en `.env.local` en ese ordenador.

## hf o el conector de Higgsfield

- **`hf` (API, saldo de open.higgsfield.ai):** los 52 endpoints del catálogo (Seedance, Kling, MiniMax, Wan, LTX, Grok, Happy Horse, Cinema Studio, Genjutsu, Soul, Ideogram, Recraft, Qwen, Z-Image, Marketing Studio). Úsalo para lotes, resultados reproducibles, descargas y copia en Cloudinary, y en AgentOS.
- **El conector de Higgsfield en Claude:** voces, doblaje, upscale, Shorts Studio, TikTok, webs y presets de la app. La API no tiene nada de eso.

## Reglas

1. **La clave nunca pasa por el chat.** No leas, imprimas, copies ni subas `.env.local`, y no pidas la clave en el chat. Si falta, Álvaro la pone en local. Para comprobar la configuración usa `hf doctor`: no gasta nada y no muestra la clave.
2. **Valida antes de gastar.** Si dudas de los parámetros, mira `hf models <alias>`. Lanza siempre primero `--dry-run` (gratis) y después la generación real.
3. **Cada generación cuesta dinero.** Confirma con Álvaro antes de lanzar algo que no haya pedido explícitamente, lotes de más de 5 trabajos, vídeo a 1080p o 4K, o duraciones largas. `hf models <alias>` muestra el precio publicado cuando existe.
4. **Usa `--json` y cree solo al resultado.** Hay éxito solo si `ok: true` y `outputs` trae `url` y `file`. No digas que funcionó sin haber visto esa salida.
5. **`status: "uncertain"`: no repitas.** Puede que la petición llegara. Pide a Álvaro que lo revise en open.higgsfield.ai/usage.
6. **`timeout`, `lost` o `interrupted`: recupera, no relances.** Usa `hf result <request_id>` y nunca `hf gen` de nuevo, que cobraría dos veces.

Códigos de salida:

| Código | Significado |
|---|---|
| 0 | OK |
| 1 | No generado (failed, nsfw, canceled, error, timeout, incierto) |
| 2 | Parámetros o configuración; no se gastó nada |
| 3 | Generado, pero falló la descarga o Cloudinary (la URL está en la salida) |

## Recetas

```bash
HF="$HOME/.higgsfield-cli/venv/bin/hf"
$HF models --kind video                       # alias disponibles
$HF models kling-3-i2v                        # parámetros, límites y precio
$HF gen z-image "a red apple on white" --ratio 1:1 --dry-run
$HF gen z-image "a red apple on white" --ratio 1:1 --json
$HF gen seedance-2.5-i2v "slow push-in" --image @/ruta/frame.png --duration 8 --json
$HF gen seedance-2.5-ref "same character walking" image_urls=@a.png image_urls=@b.png --json
$HF gen product-shots "cream tube on green tiles" image_urls=@product.jpg --json   # $HF presets para cambiar preset_id
$HF batch trabajos.jsonl --dry-run && $HF batch trabajos.jsonl --concurrency 2 --json
$HF result <request_id> --json               # recuperar tras timeout, corte o Ctrl+C
$HF history -n 10
```

**Batch.** Una línea por trabajo: `{"model": "<alias>", "prompt": "...", "params": {...}, "name": "etiqueta"}`. Los archivos locales se indican como `"@/ruta"` dentro de `params`. Antes de lanzar nada se validan todos los trabajos y se suben los archivos.

**Pegar comandos en zsh.** Si Álvaro pega bloques con comentarios `#` en zsh sin `setopt interactivecomments`, falla con `parse error`. Pásale los comandos sin comentarios.

## Desde una sesión de Claude (Cowork)

- La shell remota del Mac es una VM Linux y no puede usar el venv de macOS.
- Si hace falta ejecutar `hf` y no hay forma de hacerlo en el ordenador de Álvaro, no improvises otro cliente ni copies la clave a la nube. Pásale el comando exacto.
- Después, verifica leyendo `~/.higgsfield-cli/output/history.jsonl`, que no contiene credenciales. Busca el `request_id` y comprueba `status`, `outputs[].file` y `cloudinary_url`.

## Mantenimiento

- `python scripts/update_catalog.py --check` (desde el repo clonado) compara el catálogo con la documentación pública; sin `--check`, lo actualiza.
- Un endpoint nuevo se puede usar ya con su ID completo (`hf gen vendor/model/variant ...`), sin validación.
- Los cambios van por rama y PR, nunca directamente a main. En cada ordenador se actualiza con el `pip install` y `--upgrade --force-reinstall --no-deps`.
