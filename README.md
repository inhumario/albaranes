# Aplicativo de albaranes — v0.1 (2026-09-19)

App web (Flask + SQLite) que implementa el flujo que describió Mario el 19-09:
**escanear → subir → archivar para ellos → entregar a cada cliente según su configuración**.

## Flujo

1. **Subida**: en el panel se sube el PDF de la pila escaneada entera (más adelante,
   la entrada será también un buzón scan-to-email de la fotocopiadora — mismo pipeline).
2. **Procesado** (en segundo plano, `procesador.py`, validado con las muestras reales):
   separa páginas, lee emisor + número con Claude Haiku (visión), agrupa hojas de
   continuación (campo SHEET).
3. **Clasificación automática**: el emisor leído del membrete se casa contra los **alias**
   configurados de cada cliente. No hay que decir de quién es el escaneo, y una pila con
   albaranes de varios clientes mezclados se reparte sola. Lo que no casa → `sin-clasificar/`;
   lo ilegible → `REVISAR/` y estado «revisar».
4. **Archivo para ellos**: `archivo/<cliente>/<año>/<mes>/<numero>.pdf` (en producción,
   esa carpeta será una compartida tipo Drive para que la vean sin entrar en la app).
5. **Entrega al cliente** según su configuración:
   - `email` — envío automático con el PDF adjunto (asunto con plantilla `{fecha}`/`{numero}`);
     con `auto_entrega` sale solo al procesar, sin auto queda «pendiente» con botón.
   - `sftp` — configurado en la ficha; el conector se activa en producción.
   - `portal` — queda «pendiente» con la URL del portal y botón «ya entregado»
     (la automatización de portales se hace conector a conector con el navegador del VPS).
   - `ninguna` — solo archivar.

## Pantallas

- **Panel** (`/`): subir escaneo, contadores, pendientes de entrega/revisión, últimos lotes.
- **Archivo** (`/albaranes`): buscador por número/cliente/emisor, descarga de cada PDF.
- **Clientes** (`/clientes`): alta y edición de clientes con toda su configuración.

## Ejecutar

```bash
python3 app.py   # http://127.0.0.1:5055
```

SMTP para el envío real: `config/smtp.json` con `{"host","puerto","usuario","clave","de"}`
(sin ese fichero, las entregas por email quedan en «pendiente» y lo dicen).
En producción: clave en Infisical, no en fichero.

## Pendiente para producción (cuando Mario dé el OK y José Miguel conteste las preguntas)

- Buzón scan-to-email vigilado por cron (la bizhub C3320i escanea directo ahí).
- Carpeta de archivo en Drive compartido (o lo que usen ellos).
- Despliegue con usuario/contraseña (EasyPanel, subdominio tipo albaranes.inhumario.com).
- SMTP real de su empresa + conectores SFTP/portal por cliente según lleguen.
- Aviso resumen por WhatsApp/email al terminar cada lote.
