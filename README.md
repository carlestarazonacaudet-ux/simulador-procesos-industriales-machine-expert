# Simulador de procesos industriales para Machine Expert Basic

Aplicación de escritorio en español para leer y escribir palabras y bits por Modbus TCP y simular equipos. El ejecutable de Windows incluye la interfaz y los simuladores.

## Primeros pasos

1. Abre **Conexión Modbus…**, introduce la IP o el nombre del servidor, el puerto y el ID de unidad, y pulsa **Aplicar**.
2. En **Memorias**, añade una dirección conocida y revisa **Último valor**. El escaneo actualiza esa columna automáticamente.
3. En **Simulación**, elige el equipo y rellena sus direcciones. Los mandos se leen del PLC y los sensores simulados escriben al PLC cuando tienen asignada una salida.
4. Pulsa **Iniciar** y comprueba las señales en el gráfico. **Guardar como…** exporta la configuración a JSON para recuperarla o compartirla.

Abre esta guía con **Ayuda** o con **F1**. Selecciona un tema en **Contenido** o escribe una palabra en **Buscar en la guía** y pulsa **Siguiente**.

## Abrir la aplicación

En Windows de 64 bits, abre `MachineExpertPLC.exe` directamente. El ejecutable usa el archivo `memorias.json` de su misma carpeta. Si lo mueves a otra carpeta y quieres conservar la configuración, copia también ese archivo.

La chincheta **Fijar ventana** mantiene la aplicación por encima de las demás ventanas. Vuelve a pulsarla para desactivar esta opción.

Usa **Guardar como…** para exportar un perfil JSON con la conexión, el intervalo de escaneo, las direcciones y sus metadatos, y los campos de los simuladores. También incluye los campos de simulación rellenados antes de pulsar **Iniciar**. Usa **Cargar configuración…** para sustituir la configuración activa por un perfil. Los simuladores quedan en pausa y los valores en directo del PLC no forman parte del archivo.

La aplicación conserva la configuración del último uso al cerrarse. Los cambios en **Valor a escribir** se guardan automáticamente y se incluyen en los perfiles. Al abrir o cargar una configuración con conexión Modbus, se envían una vez los valores guardados válidos al PLC. Se escriben primero las palabras y luego los bits. Si un simulador no puede iniciar por un conflicto de configuración, el aviso indica el apartado, el campo y el valor introducido. Los conflictos de direcciones identifican las dos asignaciones y el bit o palabra afectados.

Al pulsar **Pausar** en un simulador, los bits guardados en **Valor a escribir** recuperan su estado seleccionado. Un pulsador temporal restaura ese mismo estado al soltarlo si su bit está guardado en Memorias. Si no lo está, recupera el estado leído antes del pulso.

Para ejecutar el código fuente, usa Python 3.10 o posterior con Tkinter. Desde PowerShell o la terminal de Visual Studio, en esta carpeta:

```powershell
python app.py
```

Abre **Conexión Modbus…**, al lado de **Guardar como…**, para editar el servidor, el puerto y el ID de unidad. La conexión inicial es `127.0.0.1:502`, con ID `255`. Para un PLC remoto, introduce la IP del PLC. La dirección `127.0.0.1` apunta al ordenador donde se ejecuta la aplicación.

## Leer y escribir

El signo `%` inicial es opcional. Por ejemplo, `%M1` y `M1` son equivalentes, igual que `%MW2` y `MW2` o `%MX200.0` y `MX200.0`.

| Dirección | Tipo | Valor de escritura |
| --- | --- | --- |
| `%MW100` | Palabra 100 | Entero decimal de -32768 a 65535 |
| `%MX201.0` o `%MW100.8` | Bit 8 de la palabra 100 | 0 o 1 |
| `%M10` | Bobina 10 de un servidor que publique ese mapa | 0 o 1 |

Introduce una dirección y pulsa **Añadir y guardar**. La lista se recupera al abrir el programa y se guarda en `memorias.json`, junto a `app.py`. **Quitar** elimina la dirección de la lista. Los valores se consultan en el PLC y no se guardan en el archivo.

Escribe el nombre de la variable en **Símbolo** y su explicación en **Descripción**, al extremo derecho de la tabla. Usa la barra horizontal para llegar a esa columna si la ventana es estrecha. Los campos se guardan junto a la dirección al pulsar **Enter** o salir del campo. Usa **↑** y **↓**, delante de la dirección, para cambiar el orden de las filas. El orden se guarda automáticamente.

El escaneo actualiza todas las direcciones guardadas cada **1000 ms** de forma predeterminada. Cambia **Escaneo (ms, 0 = pausa)** y pulsa **Aplicar** para guardar el intervalo. Si una ronda tarda más que el tiempo configurado, la siguiente espera a que termine. Las órdenes manuales tienen prioridad antes de iniciar otra lectura automática.

- **Bits.** Consulta el estado en **Último valor**. Selecciona **Activo** o **Inactivo** para escribirlo inmediatamente. El indicador se actualiza con el escaneo y al confirmarse una escritura.
- **Pulso temporal.** Elige **Activo** o **Inactivo** en la columna **Pulso temporal** y mantén presionado **Mantener** con el ratón o con la barra espaciadora. La aplicación lee primero el estado del bit y lo restaura al soltar. El escaneo se reanuda al finalizar el pulso. Si el PLC no confirma la restauración, la aplicación muestra el error.
- **Palabras.** Consulta el valor en **Último valor**. Para cambiarlo, escribe un entero y pulsa **Escribir**. Cuando el bit de signo está activo, se muestran las interpretaciones con signo (INT) y sin signo (WORD).

La confirmación indica que el servidor aceptó la orden. El escaneo comprueba el valor efectivo en la siguiente lectura.

## Simulación de depósito

Abre **Simulación**, elige **Depósito** y configura la capacidad en litros. Para las válvulas puedes introducir `%MW100` o solo `100`. Basta con asignar una de las dos direcciones. La válvula sin dirección se mantiene cerrada. Indica el rango de mando y el caudal máximo en L/min. La pantalla muestra la escala de 0, 50 y 100 %, el valor leído y la apertura resultante. Por ejemplo, con rango 0–1000 y caudal máximo de 40 L/min, un valor de 250 abre la válvula al 25 % y solicita 10 L/min.

Cada sensor tiene un umbral en litros. La etiqueta, el indicador y la línea del umbral aparecen a esa altura en el gráfico. El sensor se activa cuando el volumen alcanza o supera el umbral. Para publicar su estado por Modbus, asigna una dirección booleana como `%MX200.0`. Si queda vacía, el sensor funciona solo en el gráfico.

Pulsa **Iniciar** para guardar los parámetros y simular. El depósito comienza vacío al abrir la aplicación. Su volumen cambia según el balance entre los caudales de entrada y salida, con límites de cero y la capacidad configurada. **Pausar** conserva el volumen durante la sesión y deja de actualizar las salidas de sensores. Si falla la lectura de las válvulas, el volumen queda congelado hasta que se recupere la comunicación.

## Mezcla de tres depósitos

En **Simulación**, selecciona **Mezcla de tres depósitos**. Introduce el nombre, la capacidad y el volumen inicial de cada depósito. Configura el caudal máximo de cada origen. Si asignas una palabra `%MW`, el origen utiliza su rango mínimo y máximo para calcular la apertura. Si dejas la dirección vacía, ese origen descarga al caudal máximo mientras tenga líquido y quede espacio en la mezcla.

El tercer depósito recibe la suma de los caudales que salen de los dos orígenes. Si uno se vacía, solo aporta el otro. Si la mezcla se llena, la transferencia se detiene y el líquido restante se conserva en los orígenes. El gráfico muestra el nivel de cada depósito y dos tuberías que se unen antes de entrar en la mezcla. Cada tramo se colorea cuando transporta líquido y muestra su caudal.

Los nombres y parámetros se guardan al pulsar **Iniciar**. **Pausar** conserva los volúmenes durante la sesión y **Restablecer volúmenes** recupera los valores iniciales con la simulación en pausa.

Junto al depósito de mezcla se muestra el porcentaje del volumen actual aportado por cada origen. Los porcentajes se calculan con el volumen acumulado de cada líquido. Si el tercer depósito empieza con líquido, este aparece como **Contenido inicial**.

Para enviar los volúmenes al PLC, escribe una dirección en **Volumen actual → %MW** de cada depósito. El campo es opcional. Durante la simulación esa palabra recibe el volumen redondeado al litro más cercano, como entero sin signo de 0 a 65535. Usa direcciones distintas entre sí y distintas de las palabras de mando de las válvulas.

La descarga de la mezcla tiene su propio **Mando descarga %MW**, rango y caudal máximo. Sin dirección, descarga al caudal configurado. El caudal inicial es 0 L/min. En el gráfico, la tubería de descarga indica el caudal efectivo. El líquido que sale conserva la proporción de los componentes que había en la mezcla y deja espacio para que sigan entrando los dos orígenes.

Cada depósito tiene dos sensores independientes. Introduce el **Umbral (L)** y, si quieres publicar su estado en el PLC, un bit como `%MX200.0` o `%MW100.0`. El sensor se activa al alcanzar el umbral y se desactiva al bajar de él. Con un umbral y sin dirección, funciona solo en el gráfico. Con ambos campos vacíos, queda deshabilitado. Usa la barra vertical o la rueda del ratón para desplazarte por el panel.

## Cinta transportadora

En **Simulación**, selecciona **Cinta transportadora**, configura el mando y pulsa **Iniciar**.

- **Dos bits.** Configura **Marcha derecha** y **Marcha izquierda**. La cinta se mueve cuando solo uno de los bits está activo. Con ambos bits apagados o ambos encendidos, se detiene.
- **Variador %MW.** Configura una palabra de 16 bits con signo, un mínimo negativo y un máximo positivo. El mínimo da la máxima velocidad hacia la izquierda, el máximo la da hacia la derecha y cero detiene la cinta. Los valores intermedios ajustan la velocidad en proporción. Con rango de -100 a 200, una consigna de -50 da un 50 % hacia la izquierda y 100 da un 50 % hacia la derecha.

Las cajas nuevas entran por el extremo desde el que se mueve la cinta. Antes de iniciar, puedes ajustar la velocidad máxima y el **Tiempo entre cajas (s)**. Admite decimales a partir de 0,25 segundos. Si el extremo está ocupado, la siguiente caja espera a que quede espacio.

En **Sensores**, asigna hasta dos bits por estación (**Entrada**, **Centro** y **Final**). Una caja pequeña activa el sensor inferior. Una grande activa el inferior y el superior. En **Cajas** elige pequeñas, grandes o una secuencia aleatoria. En **Alimentación** elige **Sinfín** o **Cantidad fija**. Al llegar a la cantidad indicada, dejan de entrar cajas nuevas y las que estén en la cinta continúan su recorrido. **Retirar cajas** también reinicia el contador del lote.

En **Avisos**, asigna un bit de lectura a cada luz de la baliza, a la sirena y a la luz de emergencia. La baliza muestra azul, verde, amarillo y rojo de arriba abajo. Si el bit de sirena está activo, Windows reproduce el sonido de aviso del sistema.

En **Displays**, puedes asignar a cada uno de los seis recuadros un nombre y una palabra o bit de memoria. Los recuadros muestran el valor leído del PLC. Puedes compartir una dirección entre sensores, avisos y displays. Si varios sensores comparten un bit, este permanece activo mientras al menos uno detecte una caja.

**Pausar** conserva las cajas en su posición. **Retirar cajas** vacía la cinta en pausa. Los ajustes se guardan al iniciar.

En **Pulsadores de la cinta** puedes asignar un bit y un símbolo a **Marcha**, **Paro**, **Emergencia** y **Reset**. Elige si cada tecla escribe **Activo** o **Inactivo** mientras está presionada. Al soltarla, la aplicación restaura el estado previo del bit. Estos pulsadores envían señales al PLC para que las interprete la lógica del programa.

El **Selector** admite un bit y un símbolo por posición. Usa el desplegable o pulsa el dibujo para cambiar de posición. Al elegir una posición, primero desactiva el bit de la otra y después activa el de la seleccionada. La aguja indica la posición confirmada. Los dos bits deben ser distintos. Las direcciones y los símbolos se guardan con la configuración.

## Cinta de medición de altura

En **Simulación**, elige **Cinta de medición de altura**. Asigna un bit a **Marcha adelante** y otro a **Marcha atrás**. La cinta se mueve cuando hay un solo mando activo. Puedes alimentar piezas metálicas bajas, nominales, altas o aleatorias, en lote o en continuo.

En **Sensor difuso**, configura las tres alturas, la tolerancia alrededor de la nominal y la dirección de salida `%MW`. El haz vertical termina en la pieza que pasa por el centro. El gráfico muestra si la pieza es **Correcta**, **Más alta** o **Más baja**, junto con la altura, el voltaje y el valor de la palabra.

La salida de simulación aumenta linealmente de 0 a 10 V y se satura en el valor configurado como **Altura a 10 V**. Sin pieza se escribe 0. Con la escala predeterminada, 0–10 V se codifican como 0–10000. Puedes cambiar el valor de fondo de escala para adaptarlo al programa del PLC. Esta salida representa un sensor analógico genérico. El [Omron E3Z-LS](https://www.ia.omron.com/products/family/420/specification.html) tiene salida de conmutación NPN o PNP.

La baliza, la bocina, la luz de emergencia, los pulsadores y los seis displays se configuran igual que en la cinta transportadora.

En **Proceso**, elige cómo generar las alturas:

- **Alturas fijas** usa las alturas baja, nominal y alta.
- **Normal (sigma)** usa la media y la desviación típica que indiques. Una media vacía usa la altura nominal. Las alturas generadas se limitan a la altura baja y alta configuradas.
- **Normal (nivel sigma)** calcula la desviación dividiendo la distancia de la media al límite de tolerancia más cercano entre el nivel sigma. Por ejemplo, con media de 40 mm, límites 35–45 mm y nivel 6, la sigma configurada es 0,833 mm.
- **Buenas por cada 100** genera exactamente la cantidad de piezas dentro de tolerancia indicada en cada bloque completo de 100. El orden dentro de cada bloque es aleatorio.

**Calcular variabilidad** muestra la definición del proceso y, tras generar piezas, la media muestral, varianza, desviación típica y proporción dentro de tolerancia. En los modos normales muestra también Cp y Cpk cuando hay al menos dos piezas y la desviación muestral es positiva. **Retirar piezas** inicia una nueva muestra.

## Cinta de medición de ancho

En **Simulación**, elige **Cinta de medición de ancho** para ver desde arriba una cinta de azulejos cuadrados. Los dos sensores difusos enfrentados apuntan a los bordes del azulejo en el centro de la cinta. El gráfico muestra ambos haces, el ancho, la clasificación, el voltaje y la palabra resultante.

Configura los mismos parámetros que en la cinta de altura, usando anchos en milímetros. Se usan los dos bits de marcha, la misma escala configurable de 0–10 V a `%MW`, los métodos del proceso y **Calcular variabilidad**. El nuevo simulador guarda su configuración por separado e incluye avisos, pulsadores y seis displays.

En **Forma y posición**, elige azulejos cuadrados, trapezoidales, irregulares o una mezcla de trapezoidales e irregulares. Cada pieza toma un desplazamiento aleatorio entre el mínimo y el máximo. Los valores negativos desplazan hacia arriba y los positivos hacia abajo. Dos límites iguales fijan el desplazamiento.

Los sensores superior e inferior muestran por separado su distancia, voltaje y valor digital y pueden publicar en dos palabras `%MW` distintas. El ancho local se calcula restando ambas distancias a la separación fija entre los sensores. En formas no cuadradas, el ancho medido puede cambiar según el tramo de la pieza que esté cruzando los haces. Las estadísticas del proceso se calculan sobre los tamaños nominales generados de las piezas, una vez por pieza.

## Inspección por cuatro esquinas

En **Simulación**, elige **Inspección por cuatro esquinas**. Los sensores virtuales localizan las cuatro esquinas de la pieza al llegar al centro. En **Forma y posición**, puedes asignar una palabra `%MW` independiente a S1 (superior izquierda), S2 (superior derecha), S3 (inferior derecha) y S4 (inferior izquierda). Cada lectura muestra distancia en mm, voltaje y valor digital.

El simulador calcula los anchos izquierdo y derecho, los largos superior e inferior, el desplazamiento lateral del centro de las cuatro esquinas, la diferencia entre diagonales y el mayor error angular respecto a 90°. La salida analógica general representa el promedio de los dos anchos. Se pueden configurar las mismas formas, desplazamientos, métodos de variabilidad y avisos que en la cinta de ancho.

## Paletizador cartesiano

Los bits **Cinta adelante** y **Cinta atrás** controlan la cinta. Avanza hasta que la caja llegue al tope, invierte la marcha para alinearla con **Sensor de caja centrada** y para la cinta. Baja el brazo y activa **Mando de vacío** para recoger. Mantén el vacío durante el traslado y desactívalo con el brazo abajo y la carga apoyada. Si lo desactivas con carga en el aire o sin apoyo compatible, el elemento se rompe y aumenta el contador de roturas.

Cada estación admite un palet y las cajas ocupan todo su ancho. El flanco de **Colocar palet** añade un palet a la estación. El flanco de **Retirar palet y cajas** retira la carga completa. También puedes recoger un palet de la pila y soltarlo en una estación vacía. La ilustración muestra el número real de palets de la pila y el calderín encima de las ventosas.

Elige uno de estos modos en **Movimiento**:

- **Encoder**. Una palabra contiene el desplazamiento horizontal relativo en pulsos con signo y el flanco del bit **Ejecutar desplazamiento** aplica la orden. El eje vertical tiene su propia palabra de pulsos y su bit de ejecución. Los pulsos verticales positivos bajan el brazo y los negativos lo suben. El recorrido vertical completo usa la misma cantidad de pulsos que la distancia horizontal entre dos estaciones. Recogida es el cero horizontal y brazo arriba es el cero vertical.
- **Palabra de posición**. Escribe 0 para recogida, 1 a 3 para las estaciones o 4 para la pila.
- **Bits de posición**. Asigna un bit por destino. Cuando hay un solo destino activo, se acepta esa orden.

Los bits **Subir brazo** y **Bajar brazo** permiten mover el eje vertical en cualquier modo mientras están activos. Fija la velocidad directa o usa una palabra con límites de mando y velocidad. Los sensores de posición, los sensores de cinta y los pilotos de palet pueden publicar en bits Modbus. Cada estación tiene un display configurable. Si no le asignas una palabra, muestra las cajas sobre el palet. Usa la barra vertical para ver movimiento, sensores, estaciones y avisos en el mismo panel.

Los pilotos del gráfico muestran **Inicial**, **Estación 1**, **Estación 2**, **Estación 3**, **Pila**, **Brazo arriba** y **Brazo abajo**. Cada sensor admite su propio bit. En **Barómetro**, puedes habilitar un nivel de vacío simulado, indicar los valores inicial, final y de agarre en kPa y configurar un intervalo de tiempos. En cada activación se elige un tiempo de ese intervalo para pasar del vacío inicial al final. La ventosa espera a alcanzar el umbral de agarre. El **Bit presión alcanzada** se activa al alcanzarlo y vuelve a 0 cuando se apaga el vacío.

## Botonera

En **Simulación**, elige **Botonera**. Los ajustes aparecen debajo de cada elemento del frontal. Pulsa **Iniciar** para aplicar los estados iniciales y comunicar con el PLC.

- Los cuatro **pulsadores** tienen color, símbolo, dirección y estado de reposo. Admiten un bit de memoria o una palabra `%MW` completa para escribir 0 o 1. Mientras se pulsan, invierten el estado. Al soltarlos, vuelven al estado de reposo.
- Los **selectores** cambian al hacer clic sobre el mando. Los dos primeros tienen izquierda y derecha. Los otros dos también tienen centro, que pone ambos bits a cero. Configura dos bits y un símbolo por posición.
- Los seis **displays** leen una palabra `%MW` cada uno y admiten un símbolo.
- Los tres **potenciómetros** tienen rango entero configurable y escriben su valor en una palabra `%MW`. El deslizador está debajo del mando.

**Botonera lite** usa las direcciones, símbolos, rangos y estados de **Botonera** sin mostrar los campos de configuración. El frontal compacto agrupa los pulsadores cuadrados, los selectores y potenciómetros estrechos y los seis displays en dos columnas. Pulsa los iconos para usar los pulsadores y selectores, y gira los potenciómetros arrastrando su mando. Si estaba en pausa, al abrir Lite se activa con la configuración actual. Puedes volver a **Botonera** sin reiniciar los mandos.

En los M241, M251 y M262, usa `%MX` o `%MWn.bit` para acceder a bits de memoria del servidor integrado. Para escribirlos, la aplicación intenta usar la función Modbus 22 (escritura por máscara). Si el servidor la rechaza como función no soportada, puedes marcar **Permitir lectura y modificación** para hacer la escritura en dos operaciones. Usa esa opción solo en palabras que el PLC u otro cliente no modifiquen simultáneamente, porque una actualización entre la lectura y la escritura podría perderse. [Direccionamiento de bits de Schneider](https://www.se.com/my/en/faqs/FA248757/)

## Resolver problemas

- **Escaneo sin conexión.** Comprueba que el servidor Modbus TCP está iniciado y que la IP, el puerto y el ID de unidad coinciden con los del servidor.
- **La dirección no está disponible.** Comprueba que existe en el mapa Modbus del PLC. Usa `%MW` para palabras y el formato de bit que admita el controlador, por ejemplo `%MX200.0`.
- **La escritura de un bit de una palabra falla.** Revisa si el controlador admite la función por máscara o requiere la opción de lectura y modificación descrita en **Botonera**.
- **Un simulador no inicia.** Corrige el campo indicado en el aviso. Si hay un conflicto de direcciones, el mensaje muestra ambos campos y la palabra o bit compartidos.
- **Un bit cambia después de escribirlo.** **Último valor** indica lo que devuelve el PLC. El programa del PLC u otra salida del simulador pueden cambiar el bit después de la escritura.
- **Un display muestra «—».** Comprueba que el simulador está activo, que el display tiene asignada una dirección válida y que el PLC responde.

## Conexión local y simulación

Si usas el modo **Online → Simulación** integrado de EcoStruxure Machine Expert, Schneider indica que ese simulador usa el protocolo de Machine Expert y no ofrece Modbus TCP. Para usar esta aplicación en `127.0.0.1`, hace falta un servidor o puente Modbus TCP local que publique las variables. También puedes conectarte directamente a un PLC con el servidor Modbus TCP habilitado usando su dirección IP. [FAQ de Schneider sobre el simulador](https://www.se.com/es/es/faqs/FAQ000224762/)

## Autor y licencia

Autor: **Carles Tarazona**.

Puedes usar, modificar y redistribuir el simulador para fines no comerciales según los términos de la [PolyForm Noncommercial License 1.0.0](LICENSE). Conserva la atribución y el aviso de licencia al redistribuirlo, incluso si lo has modificado. Consulta [NOTICE](NOTICE) para copiar la atribución.

Los componentes de terceros integrados en el ejecutable conservan sus propias [licencias](licencias-terceros).
