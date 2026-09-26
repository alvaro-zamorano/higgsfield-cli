# Dirección: flujo y principios generales

Sirve para cualquier modelo del catálogo. Si el modelo tiene guía propia, prevalece esa.
Etiquetas: [O] documentación oficial del fabricante · [H] Higgsfield · [C] comunidad · [I] criterio propio sin verificar.
Consultado el 2026-09-26. Son reglas de documentación: nada está comprobado aún con generaciones nuestras. Lo que sí funciona aquí aparece al final, en las valoraciones (hf_rate).

## Flujo de trabajo (cada generación cuesta dinero)

1. **Brief.** Pregunta solo lo que falte y cambie el resultado:
   - para qué es: plataforma y aspect ratio;
   - duración;
   - material de referencia (imagen, vídeo, audio);
   - si lleva diálogo o audio;
   - el look.

   Lo demás lo decides con criterio y lo declaras como supuesto.
2. **Modo y modelo.**

   | Situación | Modo |
   |---|---|
   | No hay material | texto → vídeo |
   | La composición, el personaje o el producto deben salir exactos | imagen → vídeo |
   | Hay varios personajes u objetos que mantener | referencias |
   | Hay que cambiar un clip existente | edit / extend |

3. **Plan de planos** antes del prompt: para cada plano, encuadre, acción, cámara, duración y audio.
4. **Prompt según la guía del modelo.** Enseña a Álvaro el prompt, los parámetros, el coste del dry run y los supuestos. Espera su OK.
5. **Prueba barata primero:** la configuración más barata que siga probando lo que importa (ver la guía del modelo). Si importa el timing, conserva la duración y baja la resolución.
6. **Revisa con los fotogramas que devuelve hf_wait.** ¿Cumple el brief? ¿Qué falla, en concreto?
7. **Itera cambiando una sola variable cada vez** (cámara, luz, acción, duración…). Si cambias tres, no sabrás cuál arregló o estropeó el plano. [C]
8. **Versión final** a la resolución buena, solo cuando Álvaro da la prueba por buena.
9. **Pide a Álvaro nota (1-5) y un comentario** y guárdalos con hf_rate. Así esta guía mostrará lo que funciona y lo que no en este ordenador.

Cada iteración y la versión final son generaciones nuevas y se cobran: repite el dry run, enseña el coste y espera un OK explícito para esa generación. Un OK anterior no vale para la siguiente.

## Principios para casi cualquier modelo de vídeo

- **Prosa observable, no listas de etiquetas.** Nada de estilo Midjourney separado por comas. [O Seedance/Dreamina] [O Kling]
- **Sin palabras vagas.** "Cinematic", "epic" o "beautiful" no dirigen nada: cámbialas por encuadre, luz y acción concretos. [O Dreamina] [O Kling]
- **Una acción principal y un movimiento de cámara principal por plano.** [O Seedance 2.0/Dreamina] [O Kling]
- **Separa sujeto y cámara.** Qué hace el sujeto y qué hace la cámara van en frases distintas: "She turns slowly. The camera holds a locked-off medium shot." [C]
- **Velocidad.** Slow, smooth, gentle y steady son lo más estable. Si hace falta velocidad, que sea rápido un solo elemento, no cámara, acción y fondo a la vez. [O Kling] [C]
- **Luz.** Es lo que más cambia el resultado por palabra: golden hour backlight, overcast soft light, neon practicals, hard noon sun… [C]
- **Duración.** No pidas más eventos de los que caben en ella. [O Dreamina]
- **Sin campo negative_prompt.** Las restricciones van concretas y juntas al final: "no subtitles, no on-screen text, consistent outfit". [O Dreamina] [I]
- **Image-to-video.** Describe el movimiento y el cambio, no lo que ya se ve en la imagen. Contradecir la imagen provoca cortes o deriva. [O Seedance] [O Kling]
- **Diálogo.** Frases cortas, atribuidas a un personaje identificable, con el tono antes de la frase. El formato exacto está en la guía del modelo.
- **Personas reales, famosos, marcas, logos y canciones reconocibles.** Evítalos: pueden acabar moderados y plantean problemas de derechos. [O] [H]
- **Texto en pantalla.** Es donde más fallan. Si hace falta, corto, exacto y entre comillas; si tiene que salir perfecto, mejor en postproducción. [O] [I]

## Imágenes (Soul, Z-Image, Ideogram, Recraft, Qwen, Grok, Marketing Studio)

Todavía no hay guía investigada para estos modelos. Criterio general [I]:

- **Estructura:** sujeto con 2–3 rasgos visibles + composición (tamaño de plano, ángulo, óptica) + luz + entorno + estilo o material. Una idea por frase.
- **Texto en la imagen:** entre comillas, corto y diciendo dónde va. Pruébalo primero en uno o dos modelos baratos.
- **Frame para luego animar (image-to-video):** compón pensando en el movimiento. Deja aire hacia donde irá el sujeto, que el sujeto no quede cortado y que la luz sea coherente con el plano siguiente.
- **Marketing Studio (presets):** el preset ya fija el estilo. El prompt describe el producto y la escena.

## Si el modelo no tiene guía específica

Aplica estos principios y mira sus parámetros con hf_model_info. Haz la prueba con la resolución y duración mínimas del esquema. Valora el resultado con hf_rate para ir construyendo su guía.
