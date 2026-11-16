# Frames a mirar

Lista de tramos para ver en el vídeo y decidir cómo tratarlos. Los números son `frame_id`, el mismo frame del CSV y del vídeo.

## 1. Saque de banda

El balón desaparece antes del saque. El paso 6 conserva el último equipo y escribe `no_ball`. Eso vale para un hueco corto con el balón todavía en juego. No vale cuando el balón ha salido y el tramo es tiempo muerto: esos frames son `D`. El equipo que saca se decide cuando el balón vuelve a estar en juego.

En este caso el smart tagging no trae un `Throw-in`. El `Kick Off (After Goal)` acaba en el frame 163125, antes de que se pierda el balón.

- Vídeo: segundo saque de la segunda parte, después del gol.
- Detección del kickoff: frame 163083, equipo `A`.
- Balón visible: 163083–163197. Es el saque y la posesión se queda en `A`.
- Balón ausente: 163198–163486. 289 frames, 11,6 s. El paso 6 pone `A` y `no_ball`. En el vídeo es el balón fuera y el saque de banda del equipo `A`.
- El balón vuelve: frame 163487.

```bash
python play_clip.py 163083 163537 163198
```

Qué probar: marcar `D` desde que el balón se pierde junto a la banda hasta que vuelve y alguien lo controla. El archivo, si trae el saque de banda, solo indica dónde buscar.

## 2. Posesión por la posición en el campo

Ver si la zona del campo donde está un jugador sirve para predecir quién tiene el balón cuando el track de quien la toca falta o el más cercano está quieto lejos del juego.

Qué anotar en cada ejemplo:

- Frame de vídeo.
- Equipo y posición del jugador (mitad propia, banda, área).
- Si se mueve o está quieto.
- Distancia al balón, `nearest_tracked_m`.
- Etiqueta del paso 6 y etiqueta de la referencia, si el frame cae en los 5 minutos públicos.

## 3. Centro con el que toca sin track, 11351

Primer `missed` del paso 6: la referencia pasa de `A` a `H` en el 11351. El paso 6 sigue en `A` con `open` y no pasa a `H` hasta el 11479.

En el 11351 el balón está trackeado, baja de 5 m/s y la componente horizontal cambia de signo en el 11359. `touch` ya es verdadero. No cambia la etiqueta porque no hay nadie dentro de 1,5 m.

- El más cercano es el slot 45, equipo `A`: 1,50 m en el 11343, 2,22 m en el 11351, y se aleja hasta unos 5,8 m.
- El primer `H` es el slot 3: 3,3 m en el 11351, se va hasta 9,6 m y no entra en 1,5 m hasta el 11461.
- Del 11347 al 11355 no hay duelo. Los dos están fuera de la zona.
- El duelo de verdad es 11461–11476: el slot 3 y el slot 45 dentro de 1,5 m. Se conserva `A`.
- En el 11417 el slot 45 vuelve a cumplir la zona y el toque, y el paso 6 reafirma `A`.
- En el 11477 el `A` sale. En el 11479 el `H` completa el control.

Bajar los 3 frames no adelanta el 11351: el contador no llega a empezar. Ampliar el radio en ese frame mete antes al `A` que al `H`.

Qué probar, sin mover el 1,5 m: si el balón frena y cambia de sentido mientras el más cercano se aleja, ese toque no es suyo. No volver a darle el control a ese mismo jugador solo porque luego entra en la zona. La etiqueta nueva la da otro jugador, con la regla de siempre. En esta jugada eso sigue cayendo en el 11479, porque el `H` trackeado llega entonces.

El rojo que desaparece en el mapa es el slot 11. Se pierde en el 11288, a 1,8 m del azul slot 35, y vuelve en el 11403. Son 115 frames, 4,6 s. En el 11351 ese hueco no está junto al balón: el slot 35 está a 16 m y una línea entre la última posición y el regreso también cae a unos 19 m. El azul pegado al balón es el slot 45, a 2,2 m, y el rojo que sí sigue trackeado ahí es el slot 3, a 3,4 m. Rellenar el slot 11 no pone a nadie sobre el toque.




## Nuevos:
- El ultimo step incluyendo eventos se carga 'Kick Off (Start)', 'Kick Off (After Goal)', hay que volverlos a incluir estos estaban bien son faciles de detectar, falta revisar que el tiempo muerto mientras se poscionann en el campo lo contemos bien
- Luego: nan, 'throw_in', 'goal_kick', 'corner', 'kick_off_after_goal',
 'kick_off_start' veo que estan pero porque me clasifica entonces el  evento de estos frames:
event_id	Fuera de juego	Balón en juego	Metros a la banda
1 11033–11186 11187 0,04
2 17047–17278 17279–17280 1,22
como 'throw_in' y no kick_off_start


es por el orden que aplicamos las reglas? el saque de medio campo tiene mas prioridad
hay que ver como detectamos el tiempo muerto que el balon no esta en juego despues del gol hasta saque de porteria

luego revisamos los saques de banda uno a uno y intentamos ajustar, sobretodo los que sean dentro de estos 5 minutos de juego


pero falta implementar de aqui:
Type
NaN                      579
Throw-in                  26
Corner Kick               12
Goal Kick                 10
Free Kick                  5
Kick Off (After Goal)      4
Kick Off (Start)           2

el goal kick tiene 10, cuantos nos salen a nosotros lo mismo que en corners?


Añadido tiki_data_algorithm.py


