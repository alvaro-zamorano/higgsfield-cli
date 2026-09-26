---
name: higgsfield-cli
description: Generar imágenes y vídeos con la API de Higgsfield mediante `hf` (repo alvaro-zamorano/higgsfield-cli) o sus herramientas MCP hf_*: instalarlo en cualquier ordenador, escribir el prompt según la guía de cada modelo, generaciones, lotes, recuperar request_id, valoraciones, historial, Cloudinary. Usar cuando Álvaro pida generar con la API de Higgsfield o AgentOS necesite medios.
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
~/.higgsfield-cli/venv/bin/pip install -q "higgsfield-cli[cloudinary,mcp] @ git+https://github.com/alvaro-zamorano/higgsfield-cli.git"
~/.higgsfield-cli/venv/bin/hf init --open
~/.higgsfield-cli/venv/bin/hf doctor
~/.higgsfield-cli/venv/bin/hf mcp install      # y reiniciar la app de Claude
```

Para Windows y para actualizar a la última versión, mira el README del repo. El `pip install` necesita que git tenga acceso a la cuenta de GitHub de Álvaro, porque el repo es privado. La clave la escribe Álvaro en `.env.local` en ese ordenador.

## hf o el conector de Higgsfield

- **`hf` (API, saldo de open.higgsfield.ai):** los 52 endpoints del catálogo (Seedance, Kling, MiniMax, Wan, LTX, Grok, Happy Horse, Cinema Studio, Genjutsu, Soul, Ideogram, Recraft, Qwen, Z-Image, Marketing Studio). Úsalo para lotes, resultados reproducibles, descargas y copia en Cloudinary, y en AgentOS.
- **El conector de Higgsfield en Claude:** voces, doblaje, upscale, Shorts Studio, TikTok, webs y presets de la app. La API no tiene nada de eso.

## Reglas

1. **La clave nunca pasa por el chat.** No leas, imprimas, copies ni subas `.env.local`, y no pidas la clave en el chat. Si falta, Álvaro la pone en local. Para comprobar la configuración usa `hf doctor`: no gasta nada y no muestra la clave.
2. **Dirige antes de escribir.** Antes de redactar un prompt, lee la guía del modelo: `hf guide <alias>` o `hf_prompt_guide`. Sigue el flujo de la sección Dirección.
3. **Valida antes de gastar.** Si dudas de los parámetros, mira `hf models <alias>`. Lanza siempre primero `--dry-run` (gratis) y después la generación real.
4. **Cada generación cuesta dinero.** Confirma con Álvaro antes de lanzar algo que no haya pedido explícitamente, lotes de más de 5 trabajos, vídeo a 1080p o 4K, o duraciones largas. Dile siempre el coste estimado que da el dry run; si es desconocido, dilo. Para saber cuánto se lleva gastado: `hf spend` (o `hf_spend`); el importe real está en open.higgsfield.ai/usage.
5. **Usa `--json` y cree solo al resultado.** Hay éxito solo si `ok: true` y `outputs` trae `url` y `file`. No digas que funcionó sin haber visto esa salida.
6. **`status: "uncertain"`: no repitas.** Puede que la petición llegara. Pide a Álvaro que lo revise en open.higgsfield.ai/usage.
7. **`timeout`, `lost` o `interrupted`: recupera, no relances.** Usa `hf result <request_id>` y nunca `hf gen` de nuevo, que cobraría dos veces.

Códigos de salida:

| Código | Significado |
|---|---|
| 0 | OK |
| 1 | No generado (failed, nsfw, canceled, error, timeout, incierto) |
| 2 | Parámetros o configuración; no se gastó nada |
| 3 | Generado, pero falló la descarga o Cloudinary (la URL está en la salida) |

## Dirección: cómo conseguir lo que se pide

`hf guide <alias>` / `hf_prompt_guide` devuelve la guía del modelo y, antes, los prompts mejor y peor valorados de esa familia en este ordenador. Hay guías para Seedance 2.5/2.0 y Kling 3.0 (con Motion Control); el resto usa la general. Cada regla indica su fuente ([O] oficial, [H] Higgsfield, [C] comunidad, [I] criterio propio). Son documentación, no resultados probados.

1. **Brief.** Pregunta solo lo que falte y cambie el resultado: plataforma y aspect ratio, duración, material de referencia, diálogo y look. Lo demás, decídelo y decláralo como supuesto.
2. **Plan de planos.** Encuadre, acción, cámara, duración y audio de cada plano. Después, el prompt según la guía.
3. **Enseña a Álvaro** el prompt, los parámetros, los supuestos y el coste. Propón primero la prueba barata que indica la guía (Seedance: 480p; Kling: std y sin sonido).
4. **Revisa el resultado.** `hf_wait` devuelve miniaturas y, con ffmpeg, 4 fotogramas del vídeo con su segundo marcado. Di en concreto qué cumple y qué no.
5. **Pide una nota (1-5) y un comentario** y guárdalos con `hf rate` / `hf_rate`. Itera cambiando una sola cosa cada vez.

Cada iteración y la versión final son generaciones nuevas: dry run, coste y un OK explícito de Álvaro para cada una. Un OK anterior no vale para la siguiente.

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
$HF spend --days 7                           # gasto estimado por modelo
$HF guide seedance-2.5-i2v                   # guía de prompt del modelo + lo mejor/peor valorado aquí
$HF rate <request_id> 4 "buena luz, cámara demasiado rápida"
```

**Batch.** Una línea por trabajo: `{"model": "<alias>", "prompt": "...", "params": {...}, "name": "etiqueta"}`. Los archivos locales se indican como `"@/ruta"` dentro de `params`. Antes de lanzar nada se validan todos los trabajos y se suben los archivos.

**Pegar comandos en zsh.** Si Álvaro pega bloques con comentarios `#` en zsh sin `setopt interactivecomments`, falla con `parse error`. Pásale los comandos sin comentarios.

## Desde Claude: herramientas hf_* (MCP)

Si tienes las herramientas `hf_prompt_guide`, `hf_models`, `hf_model_info`, `hf_generate`, `hf_wait`, `hf_rate`, `hf_spend`, `hf_history`, `hf_cancel` y `hf_doctor` (en Cowork aparecen con prefijo, por ejemplo `mcp__remote-devices__higgsfield__hf_generate`), úsalas en vez de pasar comandos. Así es el flujo:

1. `hf_prompt_guide(model)` antes de escribir el prompt.
2. Si dudas de los parámetros, `hf_model_info`.
3. `hf_generate` con `dry_run=true`.
4. Enseña a Álvaro prompt, parámetros, supuestos y coste, y espera su OK. Empieza por la prueba barata.
5. `hf_generate` con `dry_run=false`.
6. `hf_wait` con el `request_id` hasta `done=true`.
7. Revisa las miniaturas o fotogramas. Luego `hf_rate` con la nota y el comentario de Álvaro.

El servidor rechaza peticiones idénticas en 15 minutos y puede tener un tope diario opcional. Si responde `refused`, no lo esquives con `allow_duplicate` salvo que Álvaro pida expresamente otra generación igual. Archivos locales: `"@/ruta/absoluta"` en los campos de URL de `params`.

Si no están esas herramientas, en ese ordenador falta `hf mcp install` y reiniciar Claude. Mientras tanto:

- La shell remota del Mac es una VM Linux y no puede usar el venv de macOS.
- No improvises otro cliente ni copies la clave a la nube. Pasa a Álvaro el comando exacto.
- Después, verifica leyendo `~/.higgsfield-cli/output/history.jsonl`, que no contiene credenciales. Busca el `request_id` y comprueba `status`, `outputs[].file` y `cloudinary_url`.

## Mantenimiento

- `python scripts/update_catalog.py --check` (desde el repo clonado) compara el catálogo con la documentación pública; sin `--check`, lo actualiza.
- Un endpoint nuevo se puede usar ya con su ID completo (`hf gen vendor/model/variant ...`), sin validación.
- Los cambios van por rama y PR, nunca directamente a main. En cada ordenador se actualiza con el `pip install` y `--upgrade --force-reinstall --no-deps`.
