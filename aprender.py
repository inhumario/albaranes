"""Corregir hablando: la persona cuenta qué pasa con un albarán y la app lo entiende, lo aplica
y, si se marca «siempre», lo guarda como regla para los que vengan (petición de Mario 05-11-2026:
«en corregir poner un campo que nos permita conversacionalmente decir lo que ocurre y que haya un
check de hacer siempre esto o solo ahora»).

Una regla = cuándo (emisor / a quién se entrega / origen / destino del albarán) + qué hacer
(cliente, recogida sí o no). Lo propio de un solo albarán (número, fecha, palés) nunca se
convierte en regla."""
import json
import re
import sqlite3
from datetime import datetime

import procesador

MODELO = 'claude-sonnet-5'

PROMPT = """Eres el asistente de una app que archiva albaranes escaneados de un transportista.
Cada albarán va a la carpeta de un CLIENTE (la empresa a la que se le presentan) y con su fecha de carga.
Una RECOGIDA es un albarán de otra empresa (un proveedor del cliente) cuya mercancía el camión
recoge en el ORIGEN y lleva al cliente: va con los albaranes del cliente.

Lo que la app YA HACE SOLA, sin regla ni nada que hacer a mano:
- Nombre del PDF: cada albarán del cliente se llama por su número (formato de la ficha del cliente).
  Una RECOGIDA (albarán de otra empresa que se entrega al cliente, como Fuji Oil → Delaviuda) se
  llama sola «DD-MM-AAAA-ORIGEN.DESTINO.pdf» (fecha de recogida, población de recogida y de entrega),
  y se vuelve a nombrar sola en cuanto cambian su fecha, origen o destino o se marca como recogida.
  Un albarán del cliente con el número tapado o ilegible se llama «sin-numero-DD-MM-AAAA-DESTINO.pdf».
- Las hojas al revés se enderezan; cada albarán va a la carpeta cliente/año/mes de su fecha de carga;
  los de Soprema van separados en serie-100 y serie-200.
Así que si la persona pide un nombre, formato o colocación que la app ya hace sola, NO es una duda:
en "entendido" díselo en llano («Eso ya lo hace sola: …»), pon en "este" solo los datos que haya que
cambiar para que salga bien (p. ej. "recogida": true, "origen", "destino", "fecha") y, si el alcance
es «siempre», crea la regla que marque los parecidos (mismo emisor) como recogida de ese cliente.
NUNCA le pidas a la persona que renombre, mueva o haga nada a mano: lo hace la app.

Albarán que está mirando la persona:
{albaran}

Por qué la app se lo está enseñando (lo que la persona tiene delante en la pantalla):
{contexto}

Clientes configurados en la app: {clientes}

La persona escribe esto sobre el albarán:
«{texto}»

Quiere aplicarlo: {alcance}

Devuelve SOLO un JSON, sin nada más:
{{
  "entendido": "una o dos frases en castellano, de tú, con lo que vas a hacer (p. ej. «Lo pongo como recogida en Santa Perpetua para Delaviuda»)",
  "este": {{solo los campos de ESTE albarán que cambian, de entre: "numero" (texto), "fecha" (AAAA-MM-DD, fecha de carga), "cliente" (nombre EXACTO de la lista o null para sin cliente), "recogida" (true/false), "origen" (población en mayúsculas), "destino" (población en mayúsculas), "pales" (número)}},
  "regla": null o {{"cuando": {{solo las condiciones que identifican los albaranes parecidos, de entre "emisor", "entrega_a", "origen", "destino", con el valor tal cual o abreviado}}, "hacer": {{de entre "cliente" (nombre exacto de la lista) y "recogida" (true/false)}}}},
  "duda": null o "la pregunta, si no se entiende qué quiere o pide algo que la app no puede hacer"
}}
Si la persona da por bueno lo que hay («vale», «ok», «está bien», «la buena es…», «la segunda», «la última»…),
"este" es {{}} y "duda" es null: no hay nada que cambiar. Si escribe solo un número, es el número del albarán.
Pregunta ("duda") SOLO si de verdad no se puede saber qué quiere; ante la duda razonable, entiende lo más probable
según lo que tiene delante en la pantalla.
Si el alcance es «solo este albarán», "regla" es null. Si es «siempre», la regla tiene que valer
para los albaranes PARECIDOS (lo normal: el mismo emisor), nunca números, fechas ni palés concretos."""


def tabla(con: sqlite3.Connection) -> None:
    con.execute('''CREATE TABLE IF NOT EXISTS correcciones (
        id INTEGER PRIMARY KEY,
        albaran_id INTEGER,
        texto TEXT NOT NULL,          -- lo que escribió la persona en «Corregir»
        alcance TEXT NOT NULL,        -- solo / siempre
        respuesta TEXT NOT NULL,      -- lo que entendió o preguntó la app (JSON)
        creado TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS reglas (
        id INTEGER PRIMARY KEY,
        texto TEXT NOT NULL,          -- lo que escribió la persona
        entendido TEXT NOT NULL,      -- lo que entendió la app
        cuando TEXT NOT NULL,         -- JSON: emisor / entrega_a / origen / destino
        hacer TEXT NOT NULL,          -- JSON: cliente_id / recogida
        veces INTEGER NOT NULL DEFAULT 0,
        creado TEXT NOT NULL)''')
    for col in ('entrega_a TEXT', 'visto INTEGER NOT NULL DEFAULT 0'):
        try:
            con.execute(f'ALTER TABLE albaranes ADD COLUMN {col}')
        except sqlite3.OperationalError:
            pass


def _json(texto: str) -> dict:
    m = re.search(r'\{.*\}', texto, re.S)
    return json.loads(m.group(0)) if m else {}


def interpretar(a: dict, clientes: list[str], texto: str, siempre: bool, contexto: str = '') -> dict:
    if re.fullmatch(r'\s*(n[ºo°.]?\s*)?[A-Za-z]?\d[\d\s.-]{3,}\s*', texto or '', re.I) and not siempre:
        # un número suelto: es el número del albarán (no hace falta preguntar al modelo)
        n = re.sub(r'^\s*n[ºo°.]?\s*', '', texto, flags=re.I)
        n = re.sub(r'[\s.-]', '', n)
        return {'entendido': f'Le pongo el número {n}.', 'este': {'numero': n}, 'regla': None, 'duda': None}
    datos = {k: a.get(k) for k in ('numero', 'emisor', 'entrega_a', 'origen', 'destino', 'fecha_doc',
                                    'pales', 'peso_kg', 'recogida', 'cliente')}
    prompt = PROMPT.format(albaran=json.dumps(datos, ensure_ascii=False), contexto=contexto or '(nada especial)', clientes=', '.join(clientes) or '(ninguno)',
                           texto=texto.strip(), alcance='SIEMPRE que pase esto' if siempre else 'solo este albarán')
    with procesador._LECTURAS:
        resp = procesador._api().messages.create(model=MODELO, max_tokens=2000,
                                                  messages=[{'role': 'user', 'content': prompt}])
    salida = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
    try:
        r = _json(salida)
    except json.JSONDecodeError:
        r = {'duda': 'No he entendido bien lo que hay que hacer. ¿Puedes contarlo de otra forma?'}
    if r.get('duda') and a.get('recogida') and re.search(r'nombr|llam|renombr|fecha|ruta|origen|destino|recogid',
                                                         texto, re.I):
        # pide cómo nombrar una recogida: eso ya lo hace la app sola (no se le devuelve el trabajo)
        r = {'entendido': 'Eso ya lo hace la app sola: las recogidas se nombran «fecha-origen.destino» '
                          'y se renombran solas si cambia algún dato.',
             'este': {'recogida': True}, 'duda': None,
             'regla': {'cuando': {'emisor': a.get('emisor')},
                       'hacer': {'cliente': a.get('cliente'), 'recogida': True}} if siempre and a.get('emisor') else None}
    return r


def _casa(valor, patron) -> bool:
    v, p = procesador.normaliza(valor or ''), procesador.normaliza(patron or '')
    return bool(v and p and (p in v or v in p))


def regla_para(con: sqlite3.Connection, datos: dict):
    """La regla aprendida que casa con un albarán (todas sus condiciones), o None."""
    for r in con.execute('SELECT * FROM reglas ORDER BY id DESC'):
        cuando = json.loads(r['cuando'] or '{}')
        if cuando and all(_casa(datos.get(k), v) for k, v in cuando.items()):
            return r
    return None


def aplicar_regla(con: sqlite3.Connection, datos: dict, cliente, recogida: bool):
    """Al procesar: si hay regla aprendida para este albarán, manda ella. Devuelve
    (cliente, recogida, regla o None)."""
    r = regla_para(con, datos)
    if not r:
        return cliente, recogida, None
    hacer = json.loads(r['hacer'] or '{}')
    if 'cliente_id' in hacer:
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (hacer['cliente_id'],)).fetchone() \
            if hacer['cliente_id'] else None
    if 'recogida' in hacer:
        recogida = bool(hacer['recogida'])
    con.execute('UPDATE reglas SET veces = veces + 1 WHERE id=?', (r['id'],))
    return cliente, recogida, r


def guardar_regla(con: sqlite3.Connection, texto: str, entendido: str, regla: dict, clientes: dict) -> int | None:
    cuando = {k: v for k, v in (regla.get('cuando') or {}).items()
              if k in ('emisor', 'entrega_a', 'origen', 'destino') and v}
    hacer = {}
    h = regla.get('hacer') or {}
    if 'cliente' in h:
        hacer['cliente_id'] = clientes.get(h['cliente']) if h['cliente'] else None
    if 'recogida' in h:
        hacer['recogida'] = bool(h['recogida'])
    if not cuando or not hacer:
        return None
    cur = con.execute('INSERT INTO reglas (texto, entendido, cuando, hacer, creado) VALUES (?,?,?,?,?)',
                      (texto.strip(), entendido, json.dumps(cuando, ensure_ascii=False), json.dumps(hacer),
                       datetime.now().isoformat(timespec='seconds')))
    return cur.lastrowid
