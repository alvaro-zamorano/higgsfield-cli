# Kling 3.0 (Kuaishou): guía de prompts

Cubre kling-3 (std), -pro, -4k, -turbo (texto → vídeo e imagen → vídeo) y kling-3-motion (Motion Control).

Etiquetas: [O] Kling AI · [H] Higgsfield · [C] comunidad o proveedores · [I] criterio propio sin verificar. Consultado el 2026-09-26. Nada de esto está comprobado aún con generaciones nuestras.

## Parámetros en la API de Higgsfield

- **duration:** de 3 a 15 s.
- **aspect_ratio:** 16:9, 9:16 o 1:1. Solo en t2v.
- **i2v:** image_url (primer frame) y last_image_url (último frame).
- **sound:** "on" u "off". Por defecto "on".
- **cfg_scale:** de 0 a 1, por defecto 0,5. Más alto = más fiel al prompt [C]. No hay valor oficial recomendado: empieza en 0,5.
- **Multiplano:**
  - multi_shots=true con multi_prompt, una lista de hasta 6 planos {prompt de hasta 512 caracteres, duration de 1 a 15}.
  - Las duraciones deben sumar la duración total [C].
  - Evita planos de 1–2 s: otros proveedores de la misma API exigen al menos 3 s por plano [C].
  - Se pasa en params (MCP), en un batch JSON o en la CLI como `multi_prompt='[{"prompt": "...", "duration": 3}, ...]'`.
- **elements:** lista de strings sin documentar en Higgsfield. En Kling son personajes u objetos "vinculados". No lo uses hasta probar qué acepta [I].
- **Motion Control:**
  - image_url: el personaje;
  - video_url: el movimiento;
  - character_orientation: "video" (por defecto) o "image";
  - keep_original_sound;
  - prompt opcional.
- **Variantes:**
  - tabla oficial de créditos por segundo: 720p 6 (9 con audio), 1080p 8 (12 con audio), 4K 30 [O]. El audio encarece en torno a un 50 % en Kling; en Higgsfield no está publicado [I];
  - que std sea 720p y pro 1080p lo dicen terceros [C];
  - turbo: resolution 720p o 1080p (720p por defecto), sin sound, cfg_scale ni multiplano (esquema de la API).

  No hay precio público por API para ninguna variante: el dry run dirá "desconocido".
- **No hay negative_prompt.**

## Estructura del prompt [O]

- **Orden:** sujeto (con rasgos) + acción + escena + cámara + luz y ambiente. Higgsfield lo resume en sujeto + acción + cámara + estilo [H].
- **Prompts largos (10 s o más):** primero el escenario, luego el personaje y después las acciones en orden cronológico. Nada de palabras clave sueltas.
- **Lenguaje:** concreto y visible. "Make it cinematic" no sirve.
- **Frases sencillas.** Evita números y trayectorias complicadas [O].
- **Idioma:** no está documentado. Todos los ejemplos oficiales están en inglés. Escribe la descripción en inglés y el diálogo en el idioma que deba hablarse [I].
- **Longitud:** no hay cifra oficial. Límites técnicos: unos 2500 caracteres por prompt y 512 por plano en multi_prompt [C].

## Cámara [O]

- **Movimientos:**
  - push in / pull back;
  - pan left/right, tilt up/down;
  - track forward, dolly-in;
  - orbit slowly;
  - static camera.

  Higgsfield añade handheld, whip pan y reveal [H].
- **Planos:** wide, medium, close-up, low angle, over-the-shoulder.
- **Un solo movimiento principal por plano.** Mezclar push, pan, tilt y zoom resta estabilidad [O]. "Un plano = una acción + un movimiento de cámara" [H].
- **Velocidad:** slow, smooth, gentle, stable; o "constant speed" [H].
- **Para encadenar movimientos,** usa planos distintos (multi_prompt) [I].

## Multiplano (multi_shots + multi_prompt) [O]

- **Cada plano lleva:** encuadre, acción, diálogo atribuido y cámara.
- **Cada plano debe hacer avanzar la escena.** No repitas lo mismo desde otro ángulo.
- **Reparto del tiempo:** los ejemplos oficiales reparten 15 s como 4/6/5 y 10 s como 3/4/3. Con cinco planos en un clip corto, cada uno dura tan poco que la acción no se entiende [H].
- **Continuidad del personaje:**
  - descripción idéntica en todos los planos; solo cambian cámara y acción [O];
  - etiquetas fijas ("Character A") en vez de pronombres [C];
  - para encadenar clips, el último frame de uno puede ser el primero del siguiente [H].
- **Formato de ejemplo por plano:** "Shot 1, 4s: wide shot, …" [O].

## Audio nativo (sound="on") [O]

- **Diálogo:** `Name (tone): line`, por ejemplo `Mom (softly, surprised): Wow, I didn't expect that.`
- **Frases cortas.** Nombre, frase y forma de decirla juntos. Añade el tono solo cuando importa.
- **Idiomas del diálogo:** chino, inglés, japonés, coreano y español. Admite acentos (americano, británico, indio) y mezclar idiomas dentro de una escena.
- **Ambiente y efectos:** en lenguaje llano ("the soft hum of an air conditioner"). Higgsfield añade música de fondo [H].
- **Borradores:** sound="off", salvo que pruebes el diálogo [H].
- **Sin documentar:** cuánto diálogo cabe por segundo y cuántos hablantes como máximo.

## Image-to-video y primer/último frame [O]

- **Qué describir:** el movimiento del sujeto y del fondo, no lo que ya se ve. Si el texto se aleja mucho de la imagen, el modelo mete un corte.
- **Primer y último frame:**
  - mismo tema, composición e iluminación, o aparecerá un corte;
  - el prompt describe la transición, por ejemplo "camera slowly pulls back revealing the valley";
  - el movimiento tiene que poder llegar al último frame. Si el resultado deriva, simplifica el prompt y deja que manden los frames [H].

## Motion Control (kling-3-motion, -pro) [O]

- **Imagen del personaje:**
  - una sola persona (si hay varias, elige la más grande);
  - cuerpo y cabeza completos y sin tapar;
  - mismo encuadre que el vídeo (medio cuerpo con medio cuerpo);
  - brazos y manos visibles y fondo limpio [H].
- **Vídeo de movimiento:**
  - de 3 a 30 s; el resultado dura lo mismo;
  - sin cortes ni movimientos de cámara;
  - movimientos moderados.
- **character_orientation:**
  - "video": copia movimiento, expresión, cámara y orientación, hasta 30 s [C];
  - "image": respeta la orientación de la imagen y permite indicar la cámara en el prompt, hasta 10 s [C]. La guía oficial no da estos límites.
- **Prompt:** describe fondo y contexto, no el movimiento, que ya viene del vídeo [O] [C].

## Fallos conocidos [O] [H]

- **Más de una acción o más de un movimiento de cámara por plano,** o instrucciones que se contradicen.
- **Volver a describir el aspecto de un personaje ya vinculado** (element).
- **Física compleja** [O].
- **Texto en pantalla:** 3.0 lo genera. Indica la palabra exacta, dónde va, sobre qué superficie y a qué distancia de cámara [O].
- **Sin negative_prompt:** escribe la restricción en positivo ("consistent outfit, stable face, same hairstyle") [I].
- **Contenido bloqueado:** menores, contenido sexual o violento, y lo que atente contra la seguridad nacional o la estabilidad social [O].

## Prueba barata [I]

- Variante std (≈720p) y sound="off", salvo que pruebes el diálogo.
- En multiplano, mantén la duración total para probar el reparto de tiempo.
- El coste real por API no está publicado: compáralo en open.higgsfield.ai/usage tras la primera prueba.

## Plantillas [I, a partir de las reglas anteriores]

Plano único (t2v):
```
A [subject with 2-3 visible traits] [one clear action] in [place, time of day].
Medium shot, slow push-in. [Light: e.g. soft overcast light, cool tones]. [Atmosphere].
[Character] (calm): [short line in the target language].
```

Multiplano (multi_shots=true, duration=10):
```
multi_prompt = [
  {"prompt": "Shot 1: wide shot. [Setting]. Character A, [fixed traits], walks in from the left. Static camera.", "duration": 3},
  {"prompt": "Shot 2: medium shot. Character A, [same traits], stops and looks up. Slow push-in.", "duration": 4},
  {"prompt": "Shot 3: close-up of Character A's face, [same traits]. Character A (whispering): [line]. Camera holds.", "duration": 3}
]
```

## Fuentes

- [O] Kling VIDEO 3.0 Model Guide: https://kling.ai/quickstart/klingai-video-3-model-user-guide
- [O] Kling VIDEO 3.0 Omni Guide: https://kling.ai/quickstart/klingai-video-3-omni-model-user-guide
- [O] Kling AI Prompt Guide: https://kling.ai/blog/kling-ai-prompt-guide
- [O] Multi-Shot guide: https://kling.ai/blog/kling-video-3-multi-shot-guide
- [O] Narrative and duration (15 s): https://kling.ai/blog/kling-3-15s-video-narrative-control-duration
- [O] Subject binding: https://kling.ai/blog/kling-3-subject-binding-character-consistency
- [O] Camera control guide: https://kling.ai/blog/kling-ai-camera-control-video-guide
- [O] Native audio / lip sync: https://kling.ai/blog/kling-video-3-omni-native-lip-sync-audio-guide
- [O] Long video prompts: https://kling.ai/blog/kling-ai-long-video-prompts-10s-script-guide
- [O] Image to video guide: https://kling.ai/quickstart/image-to-video-guide
- [O] Start and end frames: https://kling.ai/quickstart/ai-video-start-end-frames
- [O] Motion Control user guide: https://kling.ai/quickstart/motion-control-user-guide
- [O] Credit cost guide: https://kling.ai/blog/kling-video-3-0-credit-cost-guide
- [O] Community guidelines: https://kling.ai/docs/community-policy
- [H] Kling 3.0 on Higgsfield: https://higgsfield.ai/blog/Kling-3.0-is-on-Higgsfield-User-Guide-AI-Video-Generation
- [H] How to use Kling on Higgsfield: https://higgsfield.ai/creator-hub/help-center/ai-models/how-do-i-use-kling
- [H] Kling Motion Control 3.0: https://higgsfield.ai/blog/kling-motion-control-3
- [C] AI SDK, Kling provider: https://ai-sdk.dev/providers/ai-sdk-providers/klingai
- [C] fal, Kling 3.0 prompting guide: https://blog.fal.ai/kling-3-0-prompting-guide/
- [C] Magnific, Kling 3 API: https://docs.magnific.com/api-reference/video/kling-v3/overview
