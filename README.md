# Aplicativo de albaranes — v0.3 (2026-09-19)

App web (Flask + SQLite) que implementa el flujo que describió Mario el 19-09:
**web con contraseña → subir el archivo sin más → archivar para ellos → entregar a cada
cliente según su protocolo configurado** («subir y estar»).

## Producción

- **URL**: https://albaranes.inhumario.com (contraseña inicial en Infisical `albaranes/ACCESO_CLAVE`;
  se cambia desde Ajustes).
- **Deploy**: EasyPanel `travelia/albaranes`, build Dockerfile desde el repo público
  `github.com/inhumario/albaranes` (main), volumen `albaranes-data` → `/data` (SQLite + archivo).
  Push a main NO redespliega: `python3 scripts/deploy_easypanel.py deploy`.
- **Secretos**: Infisical carpeta `albaranes` (SECRET_KEY, ACCESO_CLAVE, ANTHROPIC_API_KEY,
  DATA_DIR, DB_PATH, BASE_URL). DNS: A `albaranes.inhumario.com` → 46.202.168.58 (Cloudflare, DNS only).
- **Acceso**: contraseña única (PBKDF2 en BD, sesión 30 días). La primera contraseña sale de la
  variable `ACCESO_CLAVE`; luego manda la de la BD (Ajustes → Acceso).
- **Ajustes** (en la propia web): Comunicaciones (SMTP saliente con prueba de envío; la clave SMTP
  vive en la BD del volumen) y cambio de contraseña. Los protocolos por cliente, en Clientes.

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

## Pendiente (cuando José Miguel conteste las preguntas de la ficha)

- SMTP real de su empresa (ahora mismo, para la demo, está el Gmail de Mario en la BD local
  de desarrollo; en producción se configura desde Ajustes).
- Conectores SFTP/portal por cliente según se vean sus portales.
- Aviso resumen por WhatsApp/email al terminar cada lote.
- Decisión Mario: entrada extra por buzón scan-to-email (la bizhub escanea directo) y/o espejo
  del archivo en un Drive compartido — de momento la entrada única es la subida en la web.
