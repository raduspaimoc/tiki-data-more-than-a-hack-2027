TIKI-DATA MORE THAN A HACK

1. Smart tagging analysis
    1. 2 this bite you if used as ground truth
    2. 638 phases of play and set pieces
    3. Team no siempre es un equipo. Esas 36 filas N/A incluyen todos los saques de banda. Si al importar dejas que un N/A caiga en “local” por defecto, o lo tiras sin mirarlo, te inventas el equipo justo en el reinicio donde más te importaba saber quién saca
    4. El intervalo es una ventana, no el instante. Un set piece no está marcado en el frame en que se golpea el balón. Está marcado unos segundos alrededor. Los dos Kick Off (Start) lo enseñan:
    5. Sirve como pista, no como respuesta.
        1. Casi toda fase trae un equipo. Eso es un prior de quién ataca en ese tramo: H o A.
        2. Todo set piece es un reinicio, y todo reinicio cierra un tramo de balón muerto. Eso es un prior de D cerca de ese momento.
        3. El tracking dice el frame exacto (jugador cerca del balón, balón que reaparece, parón largo). El tag solo dice más o menos cuándo y qué tipo de cosa era.
        4. La combinación útil es: el tag te dice “por aquí hay un córner / un build-up del Barça”; el tracking te dice en qué frame el juego pasa de D a posesión viva, y si esa posesión se parte antes de que acabe la fase.

Resumen puede contener varios intervalos continuos para mismo equipo 
Progression es sinónimo de posesión? No. PROGRESSION implica que ese equipo tiene el balón, pero no es la posesió, puede tener varios frames donde la posee, la pierde, la vuelve a tener



El vídeo táctico empieza a contar en el frame 0. Ese frame es el segundo 0 del archivo y la fila 0 del CSV. El último es el 199318.
El partido, lo que sí se puntúa, empieza en el frame 11185 (el saque inicial de la primera parte, el segundo 447,4). Del 0 al 11184 es antes del partido y no se puntúa.



# El baseline y el 50
El baseline hace dos cosas. Si ve el balón, se lo da al equipo del jugador más cercano. Si no lo ve, escribe D. Luego suaviza con un voto de mayoría.

- Como calcula distancia entre balonazos y jugador mas cerca?
25, 50 y 125 son el medio ancho de esa votación, en frames. A 25 fps: 25 frames es 1 segundo, 50 son 2 segundos, 125 son 5 segundos. Para el frame 1000 con ventana 50, mira las etiquetas de 950 a 1050 y se queda con la que más sale. Si en esa ventana nadie vio el balón, deja D.


# Por dónde implementar
Un archivo nuevo al lado de starter/baseline.py. El baseline se deja como está, para seguir teniendo el suelo.
1. Medir los huecos del balón en el ATD: cuántos duran menos de 0,5 s, menos de 1 s, más de 3 s. Es una cuenta sobre el archivo, permitida. Decide el puente del paso 3. No se elige mirando el ground truth.
2. Con balón visible, la zona de Vidal-Codina: 1,5 m, y solo cambia de equipo si el balón entra en la zona y además cambia de velocidad o de dirección. Dos rivales en la zona es un duelo: se queda el último equipo. Opción más simple, y peor: el jugador más cercano, que es el baseline. Opción de Link: el más cercano por debajo de un umbral, y se acaba la acción si con esa velocidad no lo alcanza en 1 s.
3. Con balón en NaN, se mantiene el último equipo. En un hueco corto se puede alargar la última posición con la última velocidad, para que el paso 2 siga pudiendo decidir. En un hueco largo no se escribe D.
4. D aparte, con las ventanas SET PIECES como zona de búsqueda y la geometría de los jugadores para el frame y el equipo. En los saques de banda el equipo sale de quién está en la línea, porque el tag es N/A. Se sale de D cuando alguien vuelve a controlar el balón.
5. Escribir el CSV, validar el formato, y mirar solo si el número de posesiones es de partido (cientos, de varios segundos) y no el parpadeo del baseline sin suavizar. Una pasada por los 5 minutos públicos, para ver que spellF1 no se cae por un bug. Los radios se quedan en 1,5 m, 5 m/s y 10°.
La homografía no entra en esta versión.