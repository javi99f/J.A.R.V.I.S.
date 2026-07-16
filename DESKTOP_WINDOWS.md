# Jarvis para Windows

## Instalacion

El instalador generado esta en `dist-installer/Jarvis-Setup.exe`. Instala Jarvis
para el usuario actual en `%LOCALAPPDATA%\Programs\Jarvis` y crea accesos directos
en el Escritorio y en el menu Inicio.

## Primer inicio

1. Abre **Jarvis**.
2. Pulsa **OBTENER CLAVE**. Inicia sesion en Google AI Studio, crea o
   copia una clave y pegala en Jarvis. No es necesario activar la facturacion
   para utilizar el nivel gratuito disponible.
3. Pulsa **GUARDAR**.
4. Permite el microfono si Windows lo solicita.
5. Activa el asistente diciendo **Hey Jarvis**, pulsando **ACTIVAR** o usando el
   boton **MIC**.

Las claves y los datos de ejecucion se guardan en `%LOCALAPPDATA%\Jarvis`, no en
la carpeta donde esta instalado el programa.

OpenRouter no es necesario para conversar por voz y se ha retirado del primer
inicio para evitar confusiones.

## Alcance de esta version

- Usa el microfono y ofrece entrada escrita.
- No usa la camara.
- No controla archivos, teclado, raton ni aplicaciones del ordenador.
- La interfaz principal es una ventana nativa transparente, sin marco y
  arrastrable. Un clic sobre el nucleo muestra u oculta los controles.
- El nucleo mecanico rojo se genera en tiempo real con OpenGL/GLSL; no usa imagenes,
  GIF, video ni recursos de Internet.
- Su lenguaje visual es original, inspirado en interfaces de inteligencia
  artificial mecanicas: placas simetricas, ojos rojos y anillos segmentados.
- La geometria reacciona al PCM real del microfono al escuchar y al PCM exacto
  que se envia a los altavoces cuando habla Jarvis.
- **AJUSTES** permite cambiar movimiento, sensibilidad, tamano, calidad,
  nodos orbitales y movimiento reducido. La configuracion se guarda en
  `%LOCALAPPDATA%\Jarvis\config\liquid_visual.json`.
- Si OpenGL o el shader no estan disponibles, Jarvis activa automaticamente
  un renderizador procedural simplificado y conserva todos los controles.

## Volver a compilar

Ejecuta `build_windows.ps1` desde PowerShell. Hace falta Python 3.11 y, para crear
el instalador, Inno Setup 6.
