# JARVIS para Windows 10 y 11

## Instalación

Ejecuta `dist-installer/Jarvis-Setup.exe`. El instalador incluye la aplicación y
sus dependencias, instala para el usuario actual en
`%LOCALAPPDATA%\Programs\Jarvis` y crea accesos directos en el Escritorio y el
menú Inicio. No necesita privilegios de administrador.

## Primer inicio

1. Abre **JARVIS**.
2. Pulsa **OBTENER CLAVE**, crea o copia una clave de Google Gemini y pégala en
   JARVIS. La aplicación no incluye ni comparte una clave común.
3. Pulsa **GUARDAR** y permite el micrófono si Windows lo solicita.
4. Actívalo diciendo **Hey Jarvis**, pulsando **ACTIVAR** o escribiendo una orden.

La clave, la memoria, el historial y la configuración se guardan únicamente en
`%LOCALAPPDATA%\Jarvis`, fuera de la carpeta de instalación. OpenRouter no es
necesario para conversar ni para controlar el ordenador.

## Funciones de la edición Windows 0.5.2

- Conversación por Gemini Live, wake word local y órdenes escritas.
- Orbe nativo transparente y animado, con renderizador simplificado automático
  si OpenGL o el shader principal no están disponibles.
- Ajustes de movimiento, sensibilidad visual al audio, visibilidad, tamaño,
  calidad y movimiento reducido. Los nodos orbitales permanecen activados.
- Selección de los dispositivos reales de entrada y salida detectados por
  Windows, persistencia por nombre y actualización automática al conectar o
  desconectar hardware.
- Historial local de preguntas, respuestas, avisos y errores.
- Memoria SQLite administrable: búsqueda, alta, edición, borrado y limpieza con
  confirmación. Las credenciales y secretos no se guardan como recuerdos.
- Pensamiento de profundidad media y planes persistentes y verificables para
  peticiones realmente complejas.
- Modo desarrollador con contraseña local enmascarada, sesión de 30 minutos,
  análisis de errores y de `Hey Jarvis`, auditoría encadenada por hash y ajustes
  protegidos de personalidad y voz.
- Control seguro de aplicaciones ordinarias y del navegador predeterminado. La
  automatización observa e inspecciona la interfaz entre pasos; no puede abrir
  terminales, Registro, Configuración de Windows, instaladores ni rutas
  arbitrarias. Enviar, publicar, comprar, borrar o modificar cuentas requiere
  confirmación explícita y un diálogo local.
- No utiliza la cámara ni observa permanentemente el ordenador. Solo captura la
  pantalla cuando necesita ejecutar una orden de control y la opción **Control
  del PC** está activada.

## Diagnóstico

En **AJUSTES → HISTORIAL** se pueden revisar conversaciones y errores. Para
comprobar una instalación desde PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\Jarvis\Jarvis.exe" --self-test
```

El resultado se guarda en `%LOCALAPPDATA%\Jarvis\self-test.json`. El código de
salida es `0` si todas las comprobaciones terminan correctamente y `2` si alguna
falla.

El indicador `Hey Jarvis: puntuación/umbral · micro RMS` de **AJUSTES** muestra
si el micrófono entrega audio y qué confianza obtiene el detector local. La
animación puede reaccionar al sonido aunque la puntuación no alcance el umbral;
solo una detección válida abre la conversación y cambia el estado desde reposo.

## Modo desarrollador

Di **Hey Jarvis, modo desarrollador** o abre **AJUSTES → DESARROLLADOR**. Jarvis
pedirá la contraseña en una ventana local enmascarada; no se envía a Gemini ni
se acepta por voz. La sesión se bloquea automáticamente a los 30 minutos y tres
intentos incorrectos provocan un bloqueo temporal de 60 segundos.

Desde esa ventana se puede cambiar la personalidad y una voz compatible. Los
cambios se aplican en el siguiente inicio. El registro de acciones está en
**AJUSTES → HISTORIAL → DESARROLLADOR**. Para cambiar la contraseña, sustituye
`DEVELOPER_PASSWORD_SHA256` en `%LOCALAPPDATA%\Jarvis\.env` por el SHA-256 de
una contraseña privada.

## Volver a compilar

Ejecuta `build_windows.ps1` desde PowerShell. El script usa
`.python-build\python.exe`, ejecuta todas las pruebas, reconstruye con
PyInstaller, ejecuta el autodiagnóstico empaquetado sin depender del hardware de
audio, crea el instalador con Inno Setup 6 y escribe
`dist-installer\SHA256SUMS.txt`.
