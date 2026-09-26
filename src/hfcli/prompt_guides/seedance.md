# Seedance 2.5 (ByteDance): guía de prompts

Cubre seedance-2.5 (texto → vídeo) y sus variantes -i2v, -ref, -edit y -extend.

**Seedance 2.0** usa la misma lógica, con estas diferencias:
- duración de 4 a 15 s;
- referencias: 9 imágenes, 3 vídeos y 3 audios;
- los timestamps se respetan peor: mejor "Shot 1 / Shot 2" [O];
- no se recomiendan hojas multivista.

Etiquetas: [O] ByteDance (Volcengine, BytePlus, Seed, Dreamina) · [H] Higgsfield · [C] comunidad · [I] criterio propio sin verificar. Consultado el 2026-09-26. Nada de esto está comprobado aún con generaciones nuestras.

## Parámetros en la API de Higgsfield

- **Generales:**
  - duration de 4 a 30 s;
  - resolution 480p, 720p o 1080p;
  - aspect_ratio (solo en t2v y ref);
  - generate_audio=true por defecto;
  - no hay negative_prompt ni seed.
- **i2v:** image_url (primer frame) y end_image_url opcional (último frame).
- **ref:** image_urls (hasta 30), video_urls (hasta 10) y audio_urls (hasta 10), con un máximo de 50 archivos [O]. Los vídeos de referencia duran entre 2 y 30 s y suman como mucho 30 s [O].
- **edit:** video_url (vídeo de 4 a 30 s) más referencias opcionales. **extend:** video_url.
- **Coste aproximado** (16:9, sin vídeo de entrada) [H]:

  | Resolución | Por segundo | Clip de 10 s |
  |---|---|---|
  | 480p | $0.21 | $2.06 |
  | 720p | $0.46 | $4.62 |
  | 1080p | $1.14 | $11.37 |

  En edit y extend se cobra la duración de entrada más la generada, a 0,6× la tarifa. La tarifa de 1080p sale del llms.txt del modelo; la página api-reference solo muestra 480p y 720p. Con la primera generación a 1080p, comprueba el cobro real en open.higgsfield.ai/usage.

## Estructura del prompt [O]

1. **Mapeo de materiales** (si hay referencias), al principio. Ejemplo: "Image1 is the woman in the red coat. Image2 is the café interior. Audio1 is her voice."
2. **Una frase de resumen:** sujeto + lugar + suceso + género o estilo + movimiento de cámara especial, si lo hay.
3. **Desarrollo por tramos de tiempo o por planos.** En cada uno: imagen, cámara, acción, diálogo y efectos de sonido.
4. **Cierre:** lo que se mantiene (posición de cámara, entorno, ambiente sonoro) y las restricciones, cortas y juntas.

Para un plano único, Dreamina propone este orden: formato → sujeto → acción → entorno → cámara → look → timing → audio y restricciones [O Dreamina].

Reglas de redacción:
- Escribe el prompt más corto que especifique toda la producción. No pegues un guion entero ni repitas información [O].
- Pide acciones observables, no adjetivos [O].
- Si una referencia ya define algo con precisión, basta con nombrarla; no la vuelvas a describir [O].

## Tiempo y planos [O]

- **Timestamps.** 2.5 los respeta en segundos enteros:
  - rangos: "0-3s: … 3-7s: …";
  - instantes: "at 5s, whip pan left";
  - relativos: "3s later…".

  No los uses para frecuencias ("three times per second"). La guía de Higgsfield usa decimales (0.0s–3.0s); redondea a segundos enteros [H frente a O].
- **Número de planos.** Declara "one continuous take" o el número exacto de planos, y pide los cortes de forma explícita: "hard cut to…", "match cut", "whip pan transition" [O Dreamina].
- **Un movimiento de cámara por plano** [O 2.0, Dreamina].
- **Clip de 30 s**, estructura típica en cuatro tramos [O Dreamina]:

  | Tramo | Segundos |
  |---|---|
  | Apertura | 0–6 |
  | Desarrollo | 6–14 |
  | Escalada | 14–24 |
  | Resolución | 24–30 |

- **Storyboard como imagen de referencia:** 15 viñetas como máximo (con más, el vídeo se congela o se desordena), dibujos limpios y con poco texto [O].

## Cámara [O]

- **Tamaños de plano:** extreme wide, wide, medium, close-up y extreme close-up (detalle).
- **Movimientos:** push-in, pull-out, pan, truck, follow/tracking, orbit, crane o dive, tilt-up, handheld shake.
- **Términos conocidos que entiende:** one-take, dolly zoom, aerial, FPV y bullet time.
- **Términos poco comunes:** descríbelos por su efecto. Por ejemplo, en vez de "rack focus": "the foreground blurs as focus shifts to the man in the background".
- **Ritmo:** slow, gradual y steady son lo más estable. Si hace falta velocidad, que sea un solo elemento [C].

## Audio nativo [O]

- **Diálogo:** `Character (emotion): "line"`. La emoción va antes de la frase. No repitas palabras de la frase ni añadas tono, gesto o acción después de ella: eso tiende a generar subtítulos.
- **Quién habla:** identifícalo con rasgos visibles justo antes de su línea, por ejemplo `The older man in the grey coat (tired): "…"`. Para la voz: género, edad, timbre, velocidad y emoción [O 1.5].
- **Idiomas:** 11 para el diálogo, español incluido. No mezcles idiomas dentro de una misma línea, salvo nombres propios [I].
- **Capas por separado:**
  - "no background music, only ambience and footsteps";
  - "no music";
  - "no subtitles".

  Si aun así se cuela música: "audio only character voices; no music track".
- **Efectos:** ancla cada uno a una acción o a un segundo. Para la música, di si sube, se mantiene o se resuelve [O Dreamina].
- **Agua, olas, eco:** estas palabras añaden burbujeo o reverberación; quítalas si no los quieres.
- **Música original:** pídela como "original, non-recognizable score" [H].
- **Voz desde un audio de referencia:** "Image1 is the lead and uses Audio1's voice".

## Image-to-video (seedance-2.5-i2v) [O]

- **Qué describir:** movimiento, cámara y audio, no lo que ya se ve en la imagen.
- **Primer y último frame:** image_url y end_image_url los fijan de forma estricta, igual que el aspect ratio. Si lo pides por texto en modo referencias, el resultado es solo aproximado.
- **Modos incompatibles:** primer frame, primer + último frame y referencias multimodales no se combinan.
- **Tamaño de la imagen:** que no supere la resolución de salida, porque aparecen texturas como de huella dactilar en hierba u hojas [O]. Para una prueba a 480p, reduce antes la imagen [I].
- **Clips largos (15–30 s):** da primer y último frame para que la composición no derive [O Dreamina].

## Referencias (seedance-2.5-ref) [O]

- **Numeración:** los archivos se numeran por tipo y por orden de subida (Image1, Image2…, Video1…, Audio1…). Súbelos en el orden en que aparece cada personaje.
- **Mapeo:** cada archivo se mapea en texto al principio del prompt. Con varios personajes: "Images 1-2 = character A (front and side views); Audio1 = A's voice".
- **Qué token usar:** las fuentes oficiales no coinciden (图片1, @image1, @Video 1), y fal usa @Image1. Usa "@Image1" junto con la frase de mapeo, y compruébalo en la primera prueba [I].
- **Personajes:** 2.5 acepta hojas multivista. Si la referencia ya define al personaje, nómbrala sin volver a describirlo [O]. Añadir un primer plano de la cara probablemente ayuda [I].
- **Caras reales:** la API oficial de 2.5 no acepta referencias con caras humanas reales [O]. En Higgsfield también podría moderarse [I].

## Edit y extend [O]

- **Edit:**
  - usa un verbo (edit, add, remove, replace, change to) y di qué y cuándo;
  - di expresamente qué no cambia;
  - ejemplo: "In Video1, 4-6s, replace the coffee cup with a phone. Keep characters, actions and visual style unchanged."
- **Extend:** "extend forward", "extend backward" o "continue", y describe lo nuevo.
- **Para empalmar clips:** quita los últimos 6 frames del primero y el primer frame del siguiente [O 2.0].

## Fallos conocidos y cómo arreglarlos [O]

- **Contradicciones** (trípode fijo y cámara en mano a la vez) o más sucesos de los que caben: simplifica.
- **Texto en pantalla que cambia letras:** deletréalo, dáselo como imagen de referencia o añádelo en postproducción.
- **Ojos que brillan en emociones extremas:** añade "normal human eyes, not glowing", evita emociones extremas ("frantic", "in shock") y no pongas luz de contorno sobre la cara.
- **Física compleja e interacciones entre varios sujetos:** todavía inestables. Reduce el número de sujetos que interactúan.

## Prueba barata [H precios] [I]

- **Si importa el timing o hay varios planos:** 480p con la misma duración. 12 s a 480p cuestan unos $2.47, frente a $5.55 a 720p.
- **Si solo pruebas look o acción:** 480p y 4–5 s.
- **Audio:** que generate_audio cambie el precio no consta. Déjalo activado si el audio forma parte de lo que pruebas.

## Plantillas [I, a partir de las reglas anteriores]

Plano único (t2v):
```
16:9, one continuous shot, 8 seconds. A [subject with 2-3 visible traits] [one clear action] in [place, time of day].
Medium shot, slow push-in. Light: [e.g. low warm sun from behind, soft haze]. Look: [e.g. 35mm film, muted greens].
Audio: [ambience]; [sound effect] as [action]. No music, no subtitles.
```

Varios planos con tiempos:
```
[Summary: who, where, what happens, genre.] Three shots, hard cuts.
0-4s: Wide shot. [setting and action]. Static camera. Audio: [ambience].
4-9s: Medium close-up of [character]. Slow push-in. [Character] (quiet, tense): "[short line]".
9-12s: Extreme close-up of [detail]. [final action]; [sound effect] at 11s.
Keep the same outfit and lighting across shots. No subtitles, no background music.
```

## Fuentes

- [O] Seedance 2.5 提示词指南 (Volcengine): https://docs.volcengine.com/docs/82379/2607689 (en inglés en BytePlus: https://docs.byteplus.com/en/docs/ModelArk/2607689)
- [O] Seedance 2.5 教程 (Volcengine): https://docs.volcengine.com/docs/82379/2607688
- [O] Seedance 2.0 提示词指南: https://ark.volcengine.com/docs/82379/2222480
- [O] Seedance 1.5 pro prompt guide: https://docs.byteplus.com/en/docs/ModelArk/2168087
- [O] Dreamina, Seedance 2.5 Prompt Guide: https://dreamina.capcut.com/seedance/seedance-2-5-prompt
- [O] Introducing Seedance 2.5 (ByteDance Seed): https://seed.bytedance.com/en/blog/one-take-creation-flexible-referencing-introducing-seedance-2-5
- [H] Precios y parámetros (llms.txt del modelo): https://dash.higgsfield.ai/models/bytedance/seedance-2.5/text-to-video/llms.txt
- [H] Seedance 2.5: Complete Prompting Guide: https://higgsfield.ai/blog/seedance-2-5-prompting-guide
- [H] Seedance 2.5 on Higgsfield: https://higgsfield.ai/blog/seedance-2-5-on-higgsfield-2026
- [C] fal, API Seedance 2.5 reference-to-video: https://fal.ai/models/bytedance/seedance-2.5/reference-to-video/api
