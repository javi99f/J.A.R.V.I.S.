# Historial de versiones

## 0.5.2

- Añadido en Windows el modo desarrollador protegido por contraseña local, con sesión de 30 minutos y bloqueo tras intentos fallidos.
- Añadida auditoría de desarrollador redactada, encadenada por hash y visible desde el historial.
- Añadidos controles protegidos para personalizar el estilo de respuesta y seleccionar una voz compatible.
- Calibrado `Hey Jarvis` para micrófonos silenciosos: umbral 0,40, VAD opcional, ganancia solo en el detector y confirmación de picos débiles.
- Añadida telemetría local del detector en Ajustes para distinguir audio entrante de una frase realmente reconocida.
- Ampliado el autodiagnóstico empaquetado y la suite a 82 pruebas para cubrir modo desarrollador y configuración persistente.

## 0.5.1

- Añadido pensamiento de profundidad media y planificación persistente para tareas complejas.
- Añadida memoria a largo plazo en SQLite, recuperación por relevancia y controles para buscar, editar y borrar recuerdos.
- Reforzado el filtro que impide guardar credenciales y secretos en la memoria.
- Añadido autodiagnóstico de la aplicación empaquetada para memoria, planificación, wake word, interfaz, herramientas y audio.
- Reparada la carga del wake word en la versión de Windows sin dependencias de entrenamiento innecesarias.
- Añadida reconexión automática y registro conciso ante interrupciones transitorias de Gemini Live.
- Mejorada la persistencia de audio para remapear por nombre los dispositivos aunque Windows cambie sus índices.
- Reforzada la compilación final con pruebas previas, autodiagnóstico empaquetado y SHA-256 del instalador.

## 0.5.0

- Añadido historial de preguntas, respuestas y errores desde Ajustes.
- Añadida selección de dispositivos reales de entrada y salida, actualización automática y reinicio de los flujos sin cerrar JARVIS.
- Ampliado el control seguro de aplicaciones de Windows con inspección semántica y navegación por varios pasos.
- Añadido control del PC desactivable y confirmación local para acciones sensibles.
- Ajustados tamaño mínimo visible, visibilidad máxima, nodos orbitales permanentes y menú contextual reducido a «Cerrar Jarvis».

## 0.4.0

- Añadido actualizador remoto para Raspberry Pi mediante GitHub Releases.
- Añadidas comprobaciones de tamaño y SHA-256, extracción segura, copia de seguridad y restauración automática.
- Conservación de claves, memoria, estado, configuración visual y dispositivos durante las actualizaciones.
