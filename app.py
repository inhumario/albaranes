#!/usr/bin/env python3
"""Aplicativo de albaranes — web con contraseña (v0.4, 2026-10-26).

Flujo (regla de Mario: «subir y estar»): se sube el PDF de la pila escaneada →
el procesador separa y lee cada albarán → se archiva en
archivo/<cliente>/<año>/<mes>/<numero>.pdf → se entrega solo según el protocolo
configurado de cada cliente (email automático, SFTP, portal). Todos los
parámetros (clientes y comunicaciones) se ajustan desde la propia web.

v0.4 (petición de Mario 26-10, para la demo): con «Revisar antes de enviar»
(Ajustes, activado por defecto) lo procesado queda «colocado» y se ve en la
pestaña Revisión ordenado por cliente y carpeta; nada sale hacia los clientes
hasta pulsar «Enviar».
"""
import hashlib
import subprocess
import json
import re
import os
import secrets
import smtplib
import sqlite3
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import (Flask, flash, redirect, render_template, request,
                   send_file, session, url_for)

import aprender
import exportar
import listado
import factura as lector_factura
import procesador

VERSION = '1.7.1'
BASE = Path(__file__).parent
DATA = Path(os.environ.get('DATA_DIR', BASE))
ARCHIVO = DATA / 'archivo'
ENTRADA = DATA / 'entrada'
DB = Path(os.environ.get('DB_PATH', DATA / 'albaranes.db'))

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'albaranes-dev-local')
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 30
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

MODOS_ENTREGA = ['ninguna', 'email', 'sftp', 'portal']
CLAVES_SMTP = ['smtp_host', 'smtp_puerto', 'smtp_usuario', 'smtp_clave', 'smtp_de']
# Buzón de entrada: la Konica escanea a un correo y la app recoge los PDF sola (v1.2)
CLAVES_IMAP = ['imap_host', 'imap_usuario', 'imap_clave', 'imap_carpeta', 'imap_remitentes']
BUZON_CADA = 120  # segundos entre lecturas del buzón


# ---------------------------------------------------------------- base de datos
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY,
            nombre TEXT NOT NULL,
            alias TEXT NOT NULL DEFAULT '',          -- coincidencias de membrete, separadas por comas
            modo_entrega TEXT NOT NULL DEFAULT 'ninguna',
            auto_entrega INTEGER NOT NULL DEFAULT 1, -- 1 = entregar solo al procesar (subir y estar)
            email_para TEXT DEFAULT '',
            email_asunto TEXT DEFAULT 'Albaranes {fecha}',
            sftp_datos TEXT DEFAULT '',              -- host:usuario:ruta (la clave, en ajustes en producción)
            portal_url TEXT DEFAULT '',
            notas TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS lotes (
            id INTEGER PRIMARY KEY,
            fichero TEXT NOT NULL,
            recibido TEXT NOT NULL,
            paginas INTEGER NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'procesando'
        );
        CREATE TABLE IF NOT EXISTS albaranes (
            id INTEGER PRIMARY KEY,
            lote_id INTEGER NOT NULL REFERENCES lotes(id),
            cliente_id INTEGER REFERENCES clientes(id),
            numero TEXT,
            emisor TEXT DEFAULT '',
            paginas INTEGER NOT NULL DEFAULT 1,
            confianza TEXT DEFAULT '',
            ruta TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'archivado', -- colocado | archivado | pendiente_entrega | entregado | revisar
            entrega_nota TEXT DEFAULT '',             -- por qué quedó pendiente (o instrucción de portal)
            entregado_el TEXT,
            creado TEXT NOT NULL
        );''')
        # migración v0.1 → v0.2
        try:
            con.execute("ALTER TABLE albaranes ADD COLUMN entrega_nota TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # migración v0.3 → v0.4: progreso del lote (páginas leídas)
        try:
            con.execute('ALTER TABLE lotes ADD COLUMN hechas INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        # v0.8: órdenes de carga (y otros anexos) que van pegadas al PDF de su albarán
        con.execute('''CREATE TABLE IF NOT EXISTS anexos (
            id INTEGER PRIMARY KEY,
            lote_id INTEGER NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'orden_carga',
            ref_texto TEXT NOT NULL DEFAULT '',   -- lo que pone en su campo ALBARÁN
            refs TEXT NOT NULL DEFAULT '[]',      -- números de albarán que cita (JSON)
            ruta TEXT NOT NULL,
            paginas INTEGER NOT NULL DEFAULT 1,
            unido_a INTEGER,                      -- albarán al que se ha pegado
            creado TEXT NOT NULL)''')
        try:
            con.execute("ALTER TABLE albaranes ADD COLUMN adjuntos TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # v1.0: datos del albarán para el Excel de liquidación y facturas de transporte;
        # v1.4: recogidas (albarán de un proveedor del cliente que se le lleva a él)
        for col in ('peso_kg REAL', 'pales REAL', 'destino TEXT', 'origen TEXT',
                    'recogida INTEGER NOT NULL DEFAULT 0'):
            try:
                con.execute(f'ALTER TABLE albaranes ADD COLUMN {col}')
            except sqlite3.OperationalError:
                pass
        aprender.tabla(con)  # v1.6: lo aprendido al corregir hablando
        # v1.7: listado de viajes exportado del programa del transportista (Kaleido)
        con.execute('''CREATE TABLE IF NOT EXISTS listados (
            id INTEGER PRIMARY KEY, fichero TEXT NOT NULL, subido TEXT NOT NULL,
            datos TEXT NOT NULL DEFAULT '{}')''')
        con.execute('''CREATE TABLE IF NOT EXISTS facturas (
            id INTEGER PRIMARY KEY,
            fichero TEXT NOT NULL,
            ruta TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'leyendo',   -- leyendo | lista | error: …
            numero TEXT, fecha TEXT, imp_bruto REAL, cuadra INTEGER,
            datos TEXT NOT NULL DEFAULT '{}',         -- viajes, extras y albaranes de cada viaje
            creado TEXT NOT NULL)''')
        # v0.9.1: fecha y rango de la orden de carga (da la fecha de carga a los albaranes que
        # no la traen) y sello de recepción (entre copias repetidas se guarda la sellada)
        for tabla, col in (('anexos', "fecha TEXT"), ('anexos', "rango_ini TEXT"),
                           ('anexos', "citados TEXT"),
                           ('anexos', "rango_fin TEXT"),
                           ('albaranes', "sellado INTEGER NOT NULL DEFAULT 0")):
            try:
                con.execute(f'ALTER TABLE {tabla} ADD COLUMN {col}')
            except sqlite3.OperationalError:
                pass
        # v0.8.2: lo descartado por repetido en cada escaneo
        try:
            con.execute("ALTER TABLE lotes ADD COLUMN descartados TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # v0.8: fecha propia del albarán (la que decide año/mes de la carpeta)
        try:
            con.execute("ALTER TABLE albaranes ADD COLUMN fecha_doc TEXT")
        except sqlite3.OperationalError:
            pass
        # v0.6: cómo quiere cada cliente el nombre del fichero y sus instrucciones de entrega
        for col in ("nombre_formato TEXT NOT NULL DEFAULT '{numero}'",
                    "instrucciones TEXT NOT NULL DEFAULT ''"):
            try:
                con.execute(f'ALTER TABLE clientes ADD COLUMN {col}')
            except sqlite3.OperationalError:
                pass
        # v1.1.1: lista exacta de albaranes de cada orden (el intervalo del primero al último
        # se tragaba los de otros viajes cuando la orden cita sueltos: 6258-6261, 6269, 6762)
        for o in con.execute('SELECT id, lote_id, ref_texto, rango_ini FROM anexos WHERE citados IS NULL').fetchall():
            pila = [r[0] for r in con.execute('SELECT numero FROM albaranes WHERE lote_id=? AND numero IS NOT NULL '
                                              'ORDER BY numero', (o['lote_id'],))]
            parecido = next((n for n in pila if (o['rango_ini'] or '')[-4:] and n.endswith((o['rango_ini'] or '')[-4:])), '')
            citados = procesador.albaranes_citados(o['ref_texto'], 8, parecido or o['rango_ini'] or '', pila)
            con.execute('UPDATE anexos SET citados=? WHERE id=?', (json.dumps(citados), o['id']))
        if not cfg_get(con, 'revisar_antes'):
            cfg_set(con, 'revisar_antes', '1')
        # contraseña inicial: de la variable ACCESO_CLAVE la primera vez
        if not cfg_get(con, 'clave_hash') and os.environ.get('ACCESO_CLAVE'):
            cfg_set(con, 'clave_hash', hash_clave(os.environ['ACCESO_CLAVE']))
        # migración desde config/smtp.json si existe y aún no hay SMTP en BD
        # (solo con la BD de desarrollo por defecto: una BD de pruebas en otro DATA_DIR
        # no debe heredar un SMTP real y mandar correos de verdad)
        legado = BASE / 'config' / 'smtp.json'
        if legado.exists() and not cfg_get(con, 'smtp_host') and 'DATA_DIR' not in os.environ:
            v = json.loads(legado.read_text())
            for a, b in (('host', 'smtp_host'), ('puerto', 'smtp_puerto'),
                         ('usuario', 'smtp_usuario'), ('clave', 'smtp_clave'), ('de', 'smtp_de')):
                cfg_set(con, b, str(v.get(a, '')))
        # v1.7.1: las recogidas ya guardadas con su número pasan a «fecha-origen.destino»
        try:
            renombrar_recogidas(con)
        except Exception:
            pass


def cfg_get(con: sqlite3.Connection, clave: str, defecto: str = '') -> str:
    fila = con.execute('SELECT valor FROM config WHERE clave=?', (clave,)).fetchone()
    return fila['valor'] if fila else defecto


def cfg_set(con: sqlite3.Connection, clave: str, valor: str) -> None:
    con.execute('INSERT INTO config (clave, valor) VALUES (?,?) '
                'ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor', (clave, valor))


# ----------------------------------------------------------------------- acceso
def hash_clave(clave: str, sal: str | None = None) -> str:
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', clave.encode(), bytes.fromhex(sal), 200_000)
    return f'{sal}:{h.hex()}'


def verifica_clave(clave: str, guardado: str) -> bool:
    try:
        sal, _ = guardado.split(':', 1)
        return secrets.compare_digest(hash_clave(clave, sal), guardado)
    except ValueError:
        return False


def requiere_acceso(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        with db() as con:
            hay_clave = bool(cfg_get(con, 'clave_hash'))
        if hay_clave and not session.get('acceso'):
            return redirect(url_for('login', siguiente=request.path))
        return f(*args, **kwargs)
    return envoltura


@app.context_processor
def atascados():
    """Cuántos albaranes aprobados no han llegado a su cliente: «Por entregar» solo se
    enseña (baldosa y pestaña) cuando hay alguno (decisión de Mario 27-10)."""
    if not session.get('acceso'):
        return {'n_por_entregar': 0}
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM albaranes WHERE estado='pendiente_entrega'").fetchone()[0]
    return {'n_por_entregar': n}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        with db() as con:
            guardado = cfg_get(con, 'clave_hash')
        if guardado and verifica_clave(request.form.get('clave', ''), guardado):
            session.permanent = True
            session['acceso'] = True
            return redirect(request.args.get('siguiente') or url_for('inicio'))
        flash('Contraseña incorrecta.')
    return render_template('login.html')


@app.route('/salir')
def salir():
    session.clear()
    return redirect(url_for('login'))


# ------------------------------------------------------------------- procesado
def casar_cliente(emisor: str, con: sqlite3.Connection):
    """Devuelve la fila del cliente cuyo alias casa con el emisor leído."""
    slug = procesador.normaliza(emisor)
    if not slug:
        return None
    for c in con.execute('SELECT * FROM clientes'):
        candidatos = [procesador.normaliza(c['nombre'])] + [
            procesador.normaliza(a) for a in c['alias'].split(',') if a.strip()]
        for cand in candidatos:
            if cand and (slug.startswith(cand) or cand.startswith(slug)):
                return c
    return None


def cliente_del_albaran(emisor: str, entrega_a: str, con: sqlite3.Connection):
    """(cliente, es_recogida). Lo normal es que el albarán lo emita el cliente (Delaviuda).
    Si lo emite otra empresa pero la mercancía se entrega AL cliente, es una recogida que el
    transportista le lleva (Fuji Oil en Santa Perpetua → Delaviuda en Sonseca, viaje 1088):
    es del cliente y va con sus albaranes. Corrección de Mario 03-11-2026."""
    cliente = casar_cliente(emisor, con)
    if cliente or not entrega_a:
        return cliente, False
    cliente = casar_cliente(entrega_a, con)
    return cliente, bool(cliente)


PROHIBIDOS = str.maketrans({c: '-' for c in '/\\:*?"<>|'})


def nombre_recogida(cuando: datetime, origen, destino, emisor, cliente) -> str:
    """Albarán que no es una salida del cliente y no lleva su número de logística (recogida en
    un proveedor para llevársela al cliente: Fuji Oil → Delaviuda). Regla de Mario 05-11-2026:
    se sube igual, pero el fichero se llama «fecha-municipio de recogida.municipio de entrega»,
    p. ej. «01-10-2026-SANTA PERPETUA DE LA MOGODA.SONSECA.pdf»."""
    o = (origen or emisor or 'SIN ORIGEN').strip().upper()
    d = (destino or (cliente['nombre'] if cliente else '') or 'SIN DESTINO').strip().upper()
    return f'{cuando:%d-%m-%Y}-{o}.{d}'.translate(PROHIBIDOS) + '.pdf'


def nombre_fichero(cliente, numero: str, emisor: str, cuando: datetime, recogida=None) -> str:
    """Nombre del PDF según el formato que pide cada cliente (ficha del cliente).
    Variables: {numero} {cliente} {emisor} {fecha} (AAAAMMDD) {año} {mes} {dia}.
    `recogida` = (origen, destino) si el albarán es una recogida: manda nombre_recogida."""
    if recogida:
        return nombre_recogida(cuando, recogida[0], recogida[1], emisor, cliente)
    formato = (cliente['nombre_formato'] if cliente else '') or '{numero}'
    valores = {'numero': numero, 'cliente': cliente['nombre'] if cliente else '',
               'emisor': emisor or '', 'fecha': f'{cuando:%Y%m%d}', 'año': f'{cuando:%Y}',
               'anio': f'{cuando:%Y}', 'mes': f'{cuando:%m}', 'dia': f'{cuando:%d}'}
    try:
        nombre = formato.format(**valores)
    except (KeyError, IndexError, ValueError):
        nombre = numero  # formato mal escrito: nunca se queda sin nombre
    nombre = nombre.translate(PROHIBIDOS).strip(' .') or numero
    return nombre if nombre.lower().endswith('.pdf') else nombre + '.pdf'


def de_recogida(a):
    """(origen, destino) de un albarán guardado si es una recogida; None si no."""
    a = dict(a)
    return (a.get('origen'), a.get('destino')) if a.get('recogida') else None


def renombrar(con, aid: int) -> str | None:
    """Pone a un albarán ya guardado el nombre que le toca según sus datos de ahora (misma
    carpeta). Devuelve el nombre nuevo si ha cambiado."""
    a = con.execute('SELECT * FROM albaranes WHERE id=?', (aid,)).fetchone()
    if not a or not a['numero'] or a['estado'] in ('entregado', 'descartado', 'revisar'):
        return None
    cliente = con.execute('SELECT * FROM clientes WHERE id=?', (a['cliente_id'],)).fetchone() \
        if a['cliente_id'] else None
    origen = DATA / a['ruta']
    nombre = nombre_fichero(cliente, a['numero'], a['emisor'], fecha_de(a), de_recogida(a))
    if origen.name == nombre or not origen.exists():
        return None
    destino = destino_libre(origen.parent, nombre, origen)
    origen.rename(destino)
    con.execute('UPDATE albaranes SET ruta=? WHERE id=?', (str(destino.relative_to(DATA)), aid))
    return destino.name


def renombrar_recogidas(con) -> list[str]:
    """Aplica el nombre de recogida a las que ya estaban guardadas con su número."""
    return [f'{Path(a["ruta"]).name} → {n}' for a in con.execute(
        "SELECT id, ruta FROM albaranes WHERE recogida AND estado NOT IN ('entregado','descartado')").fetchall()
        if (n := renombrar(con, a['id']))]


def destino_libre(carpeta: Path, nombre: str, propio: Path | None = None) -> Path:
    """Ruta en la carpeta sin pisar otro albarán (si coincide el nombre, añade -2, -3…)."""
    destino, n = carpeta / nombre, 2
    while destino.exists() and destino != propio:
        destino = carpeta / f'{Path(nombre).stem}-{n}.pdf'
        n += 1
    return destino


def fecha_valida(texto) -> datetime | None:
    """La fecha leída del albarán, si es creíble (ni futura ni de hace más de 3 años)."""
    try:
        f = datetime.strptime(str(texto)[:10], '%Y-%m-%d')
    except (TypeError, ValueError):
        return None
    hoy = datetime.now()
    return f if (hoy.year - 3) <= f.year and f <= hoy.replace(hour=23, minute=59) else None


def fecha_de(a) -> datetime:
    """Fecha con la que se coloca un albarán: la suya; si no se leyó, la del escaneo."""
    return fecha_valida(a['fecha_doc']) or datetime.fromisoformat(a['creado'])


def carpeta_de(cliente, emisor: str, cuando: datetime) -> Path:
    nombre = cliente['nombre'] if cliente else (emisor or 'sin-clasificar')
    return ARCHIVO / procesador.normaliza(nombre) / f'{cuando:%Y}' / f'{cuando:%m}'


def estado_final(cliente) -> str:
    """Estado de un albarán ya aprobado: hacia el cliente o solo al archivo."""
    return 'pendiente_entrega' if cliente and cliente['modo_entrega'] != 'ninguna' else 'archivado'


_ESCRITURA = threading.Lock()


def lotes_con_error(con: sqlite3.Connection) -> list:
    """Escaneos de la última semana que no se han podido leer (hay que volver a subirlos)."""
    desde = (datetime.now() - timedelta(days=7)).isoformat(timespec='seconds')
    return con.execute("SELECT * FROM lotes l WHERE estado LIKE 'error%' AND recibido >= ? "
                       "AND NOT EXISTS (SELECT 1 FROM lotes l2 WHERE l2.fichero=l.fichero AND l2.id > l.id "
                       "AND l2.estado='procesado') ORDER BY id", (desde,)).fetchall()  # ya resubido: fuera


def lotes_colgados(con: sqlite3.Connection) -> None:
    """Un escaneo que lleva más de una hora «leyéndose» se cortó (reinicio de la app): se
    marca con error para que no se quede la pantalla esperando y se pueda volver a subir."""
    limite = (datetime.now() - timedelta(hours=1)).isoformat(timespec='seconds')
    con.execute("UPDATE lotes SET estado='error: se cortó la lectura (reinicio). Vuelve a subir este PDF' "
                "WHERE estado='procesando' AND recibido < ?", (limite,))


def procesar_lote(lote_id: int, pdf: Path) -> None:
    def avance(hechas, total):
        with db() as con:
            con.execute('UPDATE lotes SET hechas=?, paginas=? WHERE id=?', (hechas, total, lote_id))
    try:
        grupos = procesador.procesar_pdf(pdf, avance)
        import tempfile
        for g in grupos:  # palés sin leer: una lectura solo de esa casilla
            if g.get('tipo') == 'albaran' and g.get('numero') and g.get('pales') is None:
                with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
                    g['writer'].write(f)
                    f.flush()
                    try:
                        g['pales'] = procesador.leer_pales(Path(f.name))
                    except Exception:
                        pass
        ahora = datetime.now()
        # la lectura va en paralelo; el archivado, de uno en uno: dos escaneos con copias del
        # mismo albarán no pueden darlo de alta los dos a la vez
        with _ESCRITURA, db() as con:
            revisar_antes = cfg_get(con, 'revisar_antes') == '1'
            descartados = []  # copias repetidas: solo hace falta una
            for n, g in enumerate(grupos):
                if g.get('duplicadas'):
                    descartados.append(f"{g['numero']} (copia en el mismo escaneo)")
                if g.get('versiones'):
                    descartados.append(f"{g['numero']} (trae otra versión con {', '.join(g['versiones'])}: "
                                       f"mandan los datos de la última, {procesador._resumen(g)})")
                if g.get('tipo') in ('orden_carga', 'albaran_agrupado'):
                    if g['ref_texto'] and con.execute('SELECT 1 FROM anexos WHERE ref_texto=?',
                                                      (g['ref_texto'],)).fetchone():
                        descartados.append(f"{ETIQUETA_ANEXO[g['tipo']]} {g['ref_texto']} (ya estaba)")
                        continue
                    carpeta = ARCHIVO / '_ordenes-de-carga'
                    carpeta.mkdir(parents=True, exist_ok=True)
                    destino = destino_libre(carpeta, f'orden-lote{lote_id}-{n + 1}.pdf')
                    with destino.open('wb') as f:
                        g['writer'].write(f)
                    alta_anexo(con, lote_id, g, destino, ahora)
                    continue
                cliente, recogida = cliente_del_albaran(g['emisor'], g.get('entrega_a'), con)
                cliente, recogida, regla = aprender.aplicar_regla(
                    con, {'emisor': g['emisor'], 'entrega_a': g.get('entrega_a'), 'origen': g.get('origen'),
                          'destino': g.get('destino')}, cliente, recogida)
                previo = con.execute(
                    "SELECT * FROM albaranes WHERE numero=? AND cliente_id IS ? AND estado != 'descartado'",
                    (g['numero'], cliente['id'] if cliente else None)).fetchone() \
                    if g['numero'] and not g['revisar'] else None
                if previo:
                    if g.get('sellado') and not previo['sellado'] and previo['estado'] != 'entregado':
                        # la copia nueva trae el sello de recepción: sustituye a la que había
                        with (DATA / previo['ruta']).open('wb') as f:
                            g['writer'].write(f)
                        con.execute('UPDATE albaranes SET sellado=1, paginas=? WHERE id=?',
                                    (len(g['paginas']), previo['id']))
                        descartados.append(f"{g['numero']} (ya estaba: se guarda esta copia, que viene sellada)")
                    else:
                        descartados.append(f"{g['numero']} (ya estaba)")
                    if g.get('writer_justif') and previo['estado'] != 'entregado':
                        # la copia repetida trae justificantes (palés, confirmación…): se pegan
                        pegar_paginas(previo, g['writer_justif'], 'justificantes', con)
                    continue
                fecha_doc = fecha_valida(g.get('fecha'))
                cuando = fecha_doc or ahora  # año/mes de la carpeta: los del albarán
                carpeta = carpeta_de(cliente, g['emisor'], cuando)
                if g['revisar']:
                    carpeta = ARCHIVO / 'REVISAR'
                carpeta.mkdir(parents=True, exist_ok=True)
                if g['numero'] and not g['revisar']:
                    nombre = nombre_fichero(cliente, g['numero'], g['emisor'], cuando,
                                            (g.get('origen'), g.get('destino')) if recogida else None)
                else:
                    nombre = f"{g['numero'] or 'sin-numero-lote%d-p%d' % (lote_id, g['paginas'][0]+1)}.pdf"
                destino = destino_libre(carpeta, nombre)
                with destino.open('wb') as f:
                    g['writer'].write(f)

                if g['revisar']:
                    estado = 'revisar'
                elif revisar_antes:
                    estado = 'colocado'
                else:
                    estado = estado_final(cliente)
                cur = con.execute(
                    'INSERT INTO albaranes (lote_id, cliente_id, numero, emisor, paginas,'
                    ' confianza, ruta, estado, creado, fecha_doc, sellado, peso_kg, pales, destino,'
                    ' origen, recogida, entrega_a, visto) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (lote_id, cliente['id'] if cliente else None, g['numero'], g['emisor'],
                     len(g['paginas']), g['confianza'], str(destino.relative_to(DATA)),
                     estado, ahora.isoformat(timespec='seconds'),
                     f'{fecha_doc:%Y-%m-%d}' if fecha_doc else None, int(bool(g.get('sellado'))),
                     g.get('peso'), g.get('pales'), g.get('destino'),
                     g.get('origen') if recogida else None, int(recogida), g.get('entrega_a'),
                     1 if regla else 0))
                adjuntos = ', '.join(x for x in (
                    'justificantes' if g.get('justificantes') else '',
                    'enderezado' if g.get('girada') else '',
                    'otra versión' if g.get('versiones') else '') if x)
                if adjuntos:
                    con.execute('UPDATE albaranes SET adjuntos=? WHERE id=?', (adjuntos, cur.lastrowid))
                if estado == 'pendiente_entrega' and cliente['auto_entrega']:
                    resultado = entregar(cur.lastrowid, con)
                    if resultado != 'ok':
                        con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?',
                                    (resultado, cur.lastrowid))
            # las órdenes de carga se pegan a su albarán, venga en este escaneo o en otro, y
            # dan su fecha de carga a los albaranes de su rango que no la traen
            unir_anexos(con)
            unir_anexos_del_viaje(con, lote_id)
            fechas_desde_ordenes(con)
            con.execute('UPDATE lotes SET estado=?, descartados=? WHERE id=?',
                        ('procesado', ', '.join(descartados), lote_id))
    except Exception as e:  # el lote nunca se queda en 'procesando' en silencio
        with db() as con:
            con.execute('UPDATE lotes SET estado=? WHERE id=?', (f'error: {e}', lote_id))


# -------------------------------------------------------------------- entregas
def entregar(albaran_id: int, con: sqlite3.Connection) -> str:
    a = con.execute('SELECT a.*, c.*, a.id AS aid FROM albaranes a '
                    'JOIN clientes c ON c.id=a.cliente_id WHERE a.id=?',
                    (albaran_id,)).fetchone()
    if not a:
        return 'sin cliente'
    if a['modo_entrega'] == 'email':
        resultado = entregar_email(a, con)
    elif a['modo_entrega'] == 'sftp':
        resultado = 'SFTP configurado pero el conector se activa en producción'
    else:  # portal
        resultado = 'para subir al portal'
    if resultado == 'ok':
        con.execute('UPDATE albaranes SET estado=?, entrega_nota=?, entregado_el=? WHERE id=?',
                    ('entregado', '', datetime.now().isoformat(timespec='seconds'), albaran_id))
    return resultado


def entregar_email(a: sqlite3.Row, con: sqlite3.Connection) -> str:
    cfg = smtp_config(con)
    if not cfg:
        return 'SMTP sin configurar (Ajustes → Comunicaciones): queda pendiente'
    if not a['email_para']:
        return 'el cliente no tiene email de entrega'
    msg = EmailMessage()
    msg['From'] = cfg['de']
    msg['To'] = a['email_para']
    msg['Subject'] = (a['email_asunto'] or 'Albaranes {fecha}').replace(
        '{fecha}', datetime.now().strftime('%d/%m/%Y')).replace('{numero}', a['numero'] or '')
    msg.set_content(f"Adjuntamos el albarán {a['numero']}.\n\nUn saludo.")
    msg.add_attachment((DATA / a['ruta']).read_bytes(), maintype='application',
                       subtype='pdf', filename=Path(a['ruta']).name)
    return enviar_smtp(cfg, msg)


def enviar_smtp(cfg: dict, msg: EmailMessage) -> str:
    try:
        with smtplib.SMTP_SSL(cfg['host'], int(cfg.get('puerto') or 465)) as s:
            s.login(cfg['usuario'], cfg['clave'])
            s.send_message(msg)
        return 'ok'
    except Exception as e:
        return f'error SMTP: {e}'


def smtp_config(con: sqlite3.Connection) -> dict | None:
    cfg = {c.replace('smtp_', ''): cfg_get(con, c) for c in CLAVES_SMTP}
    return cfg if cfg['host'] and cfg['usuario'] else None


ETIQUETA_ANEXO = {'orden_carga': 'orden de carga', 'albaran_agrupado': 'albarán agrupado'}


def pegar_paginas(albaran, writer, etiqueta: str, con: sqlite3.Connection) -> None:
    """Añade hojas sueltas (justificantes) al final del PDF de un albarán ya archivado."""
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for pagina in PdfReader(str(DATA / albaran['ruta'])).pages:
        w.add_page(pagina)
    for pagina in writer.pages:
        w.add_page(pagina)
    tmp = (DATA / albaran['ruta']).with_suffix('.tmp')
    with tmp.open('wb') as f:
        w.write(f)
    tmp.replace(DATA / albaran['ruta'])
    actual = con.execute('SELECT adjuntos FROM albaranes WHERE id=?', (albaran['id'],)).fetchone()[0]
    adjuntos = ', '.join(dict.fromkeys(x for x in ((actual or '').split(', ') + [etiqueta]) if x))
    con.execute('UPDATE albaranes SET paginas=paginas+?, adjuntos=? WHERE id=?',
                (len(writer.pages), adjuntos, albaran['id']))


def pegar_anexo(anexo, albaran, con: sqlite3.Connection) -> None:
    """Añade las hojas de un anexo al final del PDF del albarán (lo que recibe el cliente)."""
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for ruta in (albaran['ruta'], anexo['ruta']):
        for pagina in PdfReader(str(DATA / ruta)).pages:
            w.add_page(pagina)
    tmp = (DATA / albaran['ruta']).with_suffix('.tmp')
    with tmp.open('wb') as f:
        w.write(f)
    tmp.replace(DATA / albaran['ruta'])
    etiqueta = ETIQUETA_ANEXO.get(anexo['tipo'], 'orden de carga')
    if anexo['paginas'] > 1:  # la orden viene con los justificantes de su viaje detrás
        etiqueta += ' y justificantes'
    adjuntos = ', '.join(dict.fromkeys(x for x in ((albaran['adjuntos'] or '').split(', ') + [etiqueta]) if x))
    con.execute('UPDATE albaranes SET paginas=paginas+?, adjuntos=? WHERE id=?',
                (anexo['paginas'], adjuntos, albaran['id']))
    con.execute('UPDATE anexos SET unido_a=? WHERE id=?', (albaran['id'], anexo['id']))


def alta_anexo(con, lote_id, g, destino: Path, ahora: datetime) -> None:
    rango = g.get('rango') or []
    fecha = fecha_valida(g.get('fecha'))
    con.execute('INSERT INTO anexos (lote_id, tipo, ref_texto, refs, ruta, paginas, creado, fecha, '
                'rango_ini, rango_fin, citados) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (lote_id, g['tipo'], g['ref_texto'], json.dumps(g['refs']), str(destino.relative_to(DATA)),
                 len(g['paginas']), ahora.isoformat(timespec='seconds'),
                 f'{fecha:%Y-%m-%d}' if fecha else None,
                 rango[0] if rango else None, rango[-1] if rango else None,
                json.dumps(g.get('citados') or procesador.albaranes_citados(
                    g['ref_texto'], 8, rango[0] if rango else ''))))


def recolocar(con, a, fecha: datetime) -> str:
    """Mueve un albarán a la carpeta de su fecha de carga (y aplica el nombre del cliente)."""
    cliente = con.execute('SELECT * FROM clientes WHERE id=?', (a['cliente_id'],)).fetchone() \
        if a['cliente_id'] else None
    carpeta = carpeta_de(cliente, a['emisor'], fecha)
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = destino_libre(carpeta, nombre_fichero(cliente, a['numero'], a['emisor'], fecha, de_recogida(a)),
                            DATA / a['ruta'])
    (DATA / a['ruta']).rename(destino)
    con.execute('UPDATE albaranes SET fecha_doc=?, ruta=? WHERE id=?',
                (f'{fecha:%Y-%m-%d}', str(destino.relative_to(DATA)), a['id']))
    return (f"{Path(a['ruta']).parent.relative_to('archivo')}/ → "
            f"{destino.parent.relative_to(ARCHIVO)}/{destino.name}")


def fechas_desde_ordenes(con: sqlite3.Connection) -> list[str]:
    """Albaranes sin fecha de carga: toman la FECHA de la orden de carga que los cita
    (uno a uno, no del primero al último) (la orden es la que lleva la fecha de carga; el albarán a veces no)."""
    cambios = []
    ordenes = [(o, set(json.loads(o['citados'] or '[]'))) for o in con.execute(
        'SELECT * FROM anexos WHERE fecha IS NOT NULL AND rango_ini IS NOT NULL').fetchall()]
    for a in con.execute("SELECT * FROM albaranes WHERE fecha_doc IS NULL AND numero IS NOT NULL "
                         "AND estado NOT IN ('entregado', 'descartado')").fetchall():
        for o, citados in ordenes:
            if a['numero'] in citados:
                fecha = fecha_valida(o['fecha'])
                if fecha:
                    antes = Path(a['ruta']).parent
                    movido = recolocar(con, a, fecha)
                    if f'/{fecha:%Y}/{fecha:%m}' not in str(antes) + '/':  # solo si cambia de carpeta
                        cambios.append(f"📅 {Path(a['ruta']).name}: toma la fecha de carga de su orden "
                                       f"({fecha:%d/%m/%Y}) · {movido}")
                break
    return cambios


def descartar_repetidos(con: sqlite3.Connection) -> list[str]:
    """Del mismo albarán (número y cliente) solo se queda uno: el entregado si lo hay; si no,
    el que trae sello de recepción; si no, el primero. Las copias se apartan a _descartados/
    (no se borran) y se marcan como descartadas."""
    cambios = []
    grupos = {}
    for a in con.execute("SELECT * FROM albaranes WHERE estado != 'descartado' AND numero IS NOT NULL "
                         "ORDER BY id").fetchall():
        grupos.setdefault((a['numero'], a['cliente_id']), []).append(a)
    for (numero, _), copias in grupos.items():
        if len(copias) < 2:
            continue
        # se queda: la entregada; si no, una con el número confirmado (y mejor sellada)
        confirmadas = [c for c in copias if c['estado'] != 'revisar']
        guardar = next((c for c in copias if c['estado'] == 'entregado'), None) \
            or next((c for c in confirmadas if c['sellado']), None) \
            or (confirmadas[0] if confirmadas else None) \
            or next((c for c in copias if c['sellado']), None) or copias[0]
        for c in copias:
            if c['id'] == guardar['id'] or c['estado'] == 'entregado':
                continue
            carpeta = ARCHIVO / '_descartados'
            carpeta.mkdir(parents=True, exist_ok=True)
            destino = destino_libre(carpeta, Path(c['ruta']).name)
            (DATA / c['ruta']).rename(destino)
            con.execute("UPDATE albaranes SET estado='descartado', ruta=?, "
                        "entrega_nota='copia repetida' WHERE id=?",
                        (str(destino.relative_to(DATA)), c['id']))
            # si la orden de carga estaba pegada a la copia, pasa a la que se queda
            con.execute('UPDATE anexos SET unido_a=NULL WHERE unido_a=?', (c['id'],))
        cambios.append(f"♻️ {numero}: {len(copias)} copias → se queda una"
                       f"{' (la sellada)' if guardar['sellado'] else ''}")
    return cambios


def unir_anexos(con: sqlite3.Connection) -> None:
    """Pega cada orden de carga suelta al albarán que cita (el primero del rango), si ya
    existe y no se ha entregado."""
    for anexo in con.execute('SELECT * FROM anexos WHERE unido_a IS NULL').fetchall():
        refs = json.loads(anexo['refs'] or '[]')
        if not refs:
            continue
        destinos = con.execute(
            f"SELECT * FROM albaranes WHERE numero IN ({','.join('?' * len(refs))}) "
            "AND estado NOT IN ('entregado', 'descartado') ORDER BY numero", refs).fetchall()
        for alb in destinos:
            pegar_anexo(anexo, alb, con)


def unir_anexos_del_viaje(con: sqlite3.Connection, lote_id: int) -> None:
    """Un escaneo es un viaje: la orden de carga (o el albarán agrupado) cuyo número no casa
    con ningún albarán —mal escrito a mano, mal leído— va con el primer albarán de su mismo
    escaneo, y les da su fecha de carga a los de ese escaneo que no la traen."""
    albs = con.execute("SELECT * FROM albaranes WHERE lote_id=? AND numero IS NOT NULL "
                       "AND estado NOT IN ('entregado', 'descartado', 'revisar') ORDER BY numero",
                       (lote_id,)).fetchall()
    if not albs:
        return
    for anexo in con.execute('SELECT * FROM anexos WHERE lote_id=? AND unido_a IS NULL',
                             (lote_id,)).fetchall():
        pegar_anexo(anexo, albs[0], con)
        fecha = fecha_valida(anexo['fecha'])
        if fecha:
            for a in albs:
                fila = con.execute('SELECT * FROM albaranes WHERE id=?', (a['id'],)).fetchone()
                if not fila['fecha_doc']:
                    recolocar(con, fila, fecha)


def aprobar(albaran_id: int, con: sqlite3.Connection) -> str:
    """Da por bueno un albarán colocado: lo entrega a su cliente o lo deja archivado."""
    a = con.execute('SELECT a.cliente_id, c.modo_entrega FROM albaranes a '
                    'LEFT JOIN clientes c ON c.id=a.cliente_id WHERE a.id=?',
                    (albaran_id,)).fetchone()
    estado = estado_final(a if a['cliente_id'] else None)
    con.execute('UPDATE albaranes SET estado=? WHERE id=?', (estado, albaran_id))
    if estado == 'archivado':
        return 'archivado'
    resultado = entregar(albaran_id, con)
    if resultado != 'ok':
        con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?', (resultado, albaran_id))
    return resultado


# ---------------------------------------------------------------------- rutas
def ordenes_por_revisar(con: sqlite3.Connection) -> int:
    """Órdenes de carga sueltas que necesitan a una persona. Las que esperan a que se escaneen
    TODOS sus albaranes no cuentan: eso es «faltan escaneos» y se pegan solas al llegar."""
    escaneados = {r[0] for r in con.execute("SELECT numero FROM albaranes WHERE estado != 'descartado' "
                                             "AND numero IS NOT NULL")}
    n = 0
    for o in con.execute('SELECT citados FROM anexos WHERE unido_a IS NULL'):
        citados = set(json.loads(o['citados'] or '[]'))
        if not citados or citados & escaneados:
            n += 1
    return n


@app.route('/')
@requiere_acceso
def inicio():
    """Pantalla de entrada tipo Odoo: un acceso por área, con su aviso si hay algo pendiente."""
    with db() as con:
        n = dict(con.execute('SELECT estado, COUNT(*) FROM albaranes GROUP BY estado').fetchall())
        lotes_colgados(con)
        leyendo = con.execute("SELECT COUNT(*) FROM lotes WHERE estado='procesando'").fetchone()[0]
        n_clientes = con.execute('SELECT COUNT(*) FROM clientes').fetchone()[0]
        n['revisar'] = n.get('revisar', 0) + ordenes_por_revisar(con)
        faltan = faltan_total(con)
        errores = len(lotes_con_error(con))
    accesos = [
        {'url': url_for('index'), 'nombre': 'Subir escaneo', 'icono': 'subir', 'color': 'coral',
         'aviso': f'{leyendo} leyéndose' if leyendo else ''},
        # solo lo que necesita una persona: números dudosos, órdenes sin su albarán y escaneos
        # que no se han podido leer (lo colocado y bien leído no es «por revisar»)
        {'url': url_for('revision'), 'nombre': 'Revisión', 'icono': 'revision', 'color': 'azul',
         'aviso': f"{n.get('revisar', 0) + errores} por revisar" if n.get('revisar', 0) + errores else ''},
        {'url': url_for('albaranes'), 'nombre': 'Archivo', 'icono': 'archivo', 'color': 'ambar',
         'aviso': ''},
        {'url': url_for('pendientes'), 'nombre': 'Por entregar', 'icono': 'entregar', 'color': 'violeta',
         'aviso': f"{n.get('pendiente_entrega', 0)} sin entregar" if n.get('pendiente_entrega') else ''},
        {'url': url_for('mes_en_curso'), 'nombre': 'Mes en curso', 'icono': 'mes', 'color': 'verde',
         'aviso': f'faltan {faltan}' if faltan else ''},
        {'url': url_for('facturas'), 'nombre': 'Facturas', 'icono': 'facturas', 'color': 'naranja',
         'aviso': ''},
        {'url': url_for('clientes'), 'nombre': 'Clientes', 'icono': 'clientes', 'color': 'verde',
         'aviso': ''},
        {'url': url_for('ajustes'), 'nombre': 'Ajustes', 'icono': 'ajustes', 'color': 'tinta',
         'aviso': ''},
    ]
    accesos = [a for a in accesos if a['nombre'] != 'Por entregar' or n.get('pendiente_entrega')]
    return render_template('inicio.html', accesos=accesos, n_clientes=n_clientes)


# --------------------------------------------------------------- viajes del mes
def viajes(con: sqlite3.Connection, mes: str | None = None) -> list[dict]:
    """Cada orden de carga es un viaje: qué albaranes cita, cuáles están escaneados y cuáles
    faltan, si vienen firmados y si ya se han enviado. `mes` = 'AAAA-MM' (por la fecha de carga)."""
    albs = {r['numero']: dict(r) for r in con.execute(
        "SELECT numero, estado, sellado, pales, destino, fecha_doc FROM albaranes "
        "WHERE estado != 'descartado' AND numero IS NOT NULL")}
    salida, vistos = [], set()
    for o in con.execute("SELECT * FROM anexos WHERE tipo='orden_carga' ORDER BY fecha, id").fetchall():
        fecha = o['fecha'] or o['creado'][:10]
        if mes and not fecha.startswith(mes):
            continue
        citados = json.loads(o['citados'] or '[]') or json.loads(o['refs'] or '[]')
        if not citados or tuple(citados) in vistos:
            continue
        vistos.add(tuple(citados))
        estan = [albs[n] for n in citados if n in albs]
        faltan = [n for n in citados if n not in albs]
        destinos = [a['destino'] for a in estan if a['destino']]
        sin_firma = sum(1 for a in estan if not a['sellado'])
        enviados = sum(1 for a in estan if a['estado'] == 'entregado')
        dudosos = sum(1 for a in estan if a['estado'] == 'revisar')
        if faltan:
            estado, texto = 'rojo', f'Faltan {len(faltan)}'
        elif dudosos:
            estado, texto = 'rojo', f'{dudosos} por revisar'
        elif enviados == len(estan):
            estado, texto = 'verde', 'Completo y enviado'
        else:
            estado, texto = 'ambar', 'Completo · sin enviar'
        salida.append({'fecha': fecha, 'ref': o['ref_texto'], 'citados': citados, 'faltan': faltan,
                       'escaneados': len(estan), 'destino': max(set(destinos), key=destinos.count) if destinos else '',
                       'pales': sum(a['pales'] or 0 for a in estan), 'sin_firma': sin_firma,
                       'enviados': enviados, 'estado': estado, 'texto': texto, 'unido_a': o['unido_a']})
    return salida


def albaranes_sin_orden(con: sqlite3.Connection, mes: str) -> list[dict]:
    """Albaranes del mes que no cita ninguna orden de carga (escaneados sin su orden)."""
    citados = set()
    for o in con.execute("SELECT citados FROM anexos WHERE citados IS NOT NULL"):
        citados.update(json.loads(o['citados']))
    return [dict(r) for r in con.execute(
        "SELECT numero, destino, pales, fecha_doc, estado FROM albaranes WHERE estado NOT IN ('descartado') "
        "AND numero IS NOT NULL AND NOT recogida AND fecha_doc LIKE ? ORDER BY numero", (mes + '%',))
        if r['numero'] not in citados]


def recogidas_del_mes(con: sqlite3.Connection, mes: str) -> list[dict]:
    """Albaranes de proveedores que se recogen para el cliente (no llevan orden de carga), con
    el viaje de factura en el que están, si ya se ha subido."""
    viaje_de, de_este_mes = {}, set()
    for f in con.execute("SELECT numero, fecha, datos FROM facturas WHERE estado='lista'"):
        for v in json.loads(f['datos'] or '{}').get('viajes', []):
            for n in v.get('albaranes', []):
                viaje_de[n] = f"{v['viaje']} (factura {f['numero']})"
                if (f['fecha'] or '').startswith(mes):
                    de_este_mes.add(n)
    return [{**dict(r), 'viaje': viaje_de.get(r['numero'])} for r in con.execute(
        "SELECT numero, emisor, origen, destino, fecha_doc FROM albaranes WHERE recogida "
        "AND estado != 'descartado' AND numero IS NOT NULL ORDER BY fecha_doc")
        if (r['fecha_doc'] or '').startswith(mes) or r['numero'] in de_este_mes]


def faltan_total(con: sqlite3.Connection) -> int:
    """Albaranes que citan las órdenes de carga y no se han escaneado (de los meses sin cerrar)."""
    return sum(len(v['faltan']) for v in viajes(con) if v['estado'] == 'rojo')


@app.route('/mes')
@requiere_acceso
def mes_en_curso():
    """Cada viaje del mes con su semáforo: lo que falta antes de cerrar el mes."""
    with db() as con:
        meses = sorted({(o['fecha'] or o['creado'])[:7] for o in con.execute(
            "SELECT fecha, creado FROM anexos WHERE tipo='orden_carga'")}, reverse=True)
        mes = request.args.get('m') or (meses[0] if meses else f'{datetime.now():%Y-%m}')
        lista = viajes(con, mes)
        sueltos = albaranes_sin_orden(con, mes)
        cierre = json.loads(cfg_get(con, f'cierre_{mes}') or '{}')
        checks = comprobar_cierre(con, mes, lista)
        facs = facturas_del_mes(con, mes)
        recogidas = recogidas_del_mes(con, mes)
        programa = listado_del_mes(con, mes)
    resumen = {k: sum(1 for v in lista if v['estado'] == k) for k in ('verde', 'ambar', 'rojo')}
    return render_template('mes.html', mes=mes, meses=meses, viajes=lista, sueltos=sueltos,
                           cierre=cierre, checks=checks, facs=facs, recogidas=recogidas, programa=programa,
                           resumen=resumen, faltan=sum(len(v['faltan']) for v in lista),
                           albaranes=sum(v['escaneados'] for v in lista) + len(sueltos) + len(recogidas),
                           pales=sum(v['pales'] for v in lista))


def listado_del_mes(con: sqlite3.Connection, mes: str) -> dict | None:
    """El último listado de viajes subido que trae viajes de este mes, cruzado con lo escaneado:
    qué viajes no tienen ningún papel y a cuáles les faltan albaranes."""
    for fila in con.execute('SELECT * FROM listados ORDER BY id DESC'):
        d = json.loads(fila['datos'] or '{}')
        viajes_l = d.get('viajes', [])
        # si el listado trae viajes de otros clientes, solo cuentan los de los configurados
        if any(v.get('cliente') for v in viajes_l):
            nuestros = [v for v in viajes_l if v.get('cliente') and casar_cliente(v['cliente'], con)]
            viajes_l = nuestros or viajes_l
        fac = listado.como_factura(viajes_l, mes)
        if not fac['viajes']:
            continue
        cruce = cruzar_factura(fac, con)
        filas = [v for v in cruce['viajes'] if v.get('estado') not in ('suelta',)]
        return {'fichero': fila['fichero'], 'subido': fila['subido'], 'columnas': d.get('columnas', {}),
                'total': len(filas), 'id': fila['id'],
                'sin_papeles': [v for v in filas if v['estado'] == 'sin albaranes'],
                'faltan': [v for v in filas if v['estado'] == 'faltan escaneos'],
                'ok': [v for v in filas if v['estado'] not in ('sin albaranes', 'faltan escaneos')]}
    return None


@app.route('/listado', methods=['POST'])
@requiere_acceso
def listado_subir():
    """Sube el Excel (o CSV) de viajes exportado del programa del transportista."""
    f = request.files.get('listado')
    if not f or not f.filename:
        flash('Elige el fichero exportado del programa.')
        return redirect(url_for('mes_en_curso'))
    datos = f.read()
    try:
        leido = listado.leer(f.filename, datos)
    except Exception as e:
        flash(f'No he podido leer «{f.filename}» ({e.__class__.__name__}). ¿Es un Excel o un CSV?')
        return redirect(url_for('mes_en_curso'))
    if not leido['viajes']:
        flash(f'«{f.filename}»: {leido["aviso"]}')
        return redirect(url_for('mes_en_curso'))
    carpeta = ARCHIVO / '_listados'
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f'{datetime.now():%Y%m%d-%H%M%S}-{Path(f.filename).name}').write_bytes(datos)
    with db() as con:
        con.execute('INSERT INTO listados (fichero, subido, datos) VALUES (?,?,?)',
                    (f.filename, datetime.now().isoformat(timespec='seconds'), json.dumps(leido)))
    meses_l = sorted({v['fecha'][:7] for v in leido['viajes']})
    flash(f'✓ Listado leído: {len(leido["viajes"])} viajes ({", ".join(m[5:7] + "/" + m[:4] for m in meses_l)}). '
          f'Columnas reconocidas: {", ".join(f"{k} = «{v}»" for k, v in leido["columnas"].items())}.')
    return redirect(url_for('mes_en_curso', m=meses_l[-1]) + '#programa')


def facturas_del_mes(con: sqlite3.Connection, mes: str) -> list:
    """Las facturas de un mes (puede haber varias: en septiembre, la 8269 y la 8268). Si se
    subió dos veces la misma, cuenta la última."""
    filas, vistas = [], set()
    for f in con.execute("SELECT * FROM facturas WHERE estado='lista' AND fecha LIKE ? ORDER BY id DESC",
                         (mes + '%',)):
        if f['numero'] not in vistas:
            vistas.add(f['numero'])
            filas.append(f)
    return sorted(filas, key=lambda f: f['numero'] or '', reverse=True)  # como su Excel: 8269, 8268


def comprobar_cierre(con: sqlite3.Connection, mes: str, lista: list[dict]) -> list[dict]:
    """Lo que hay que mirar antes de cerrar el mes. nivel: ok · aviso (se puede cerrar
    sabiéndolo) · falta (lo normal es arreglarlo antes)."""
    c = []
    faltan = [n for v in lista for n in v['faltan']]
    c.append({'nivel': 'falta' if faltan else 'ok',
              'texto': f'{len(faltan)} albarán(es) que cita una orden de carga y no están escaneados' if faltan
              else 'Todos los albaranes de las órdenes de carga están escaneados',
              'detalle': ', '.join(faltan[:30]) + (' …' if len(faltan) > 30 else '')})
    dudosos = con.execute("SELECT COUNT(*) FROM albaranes WHERE estado='revisar'").fetchone()[0] + \
        ordenes_por_revisar(con)
    errores = len(lotes_con_error(con))
    c.append({'nivel': 'falta' if dudosos or errores else 'ok',
              'texto': (f'{dudosos} cosa(s) por revisar' + (f' y {errores} escaneo(s) sin leer' if errores else '')
                        if dudosos or errores else 'Nada pendiente en Revisión'),
              'detalle': 'números dudosos u órdenes de carga sin su albarán' if dudosos else '',
              'enlace': url_for('revision') if dudosos or errores else ''})
    sin_firma = sum(v['sin_firma'] for v in lista)
    c.append({'nivel': 'aviso' if sin_firma else 'ok',
              'texto': f'{sin_firma} albarán(es) sin firma ni sello de recepción' if sin_firma
              else 'Todos los albaranes vienen firmados', 'detalle': ''})
    # recogidas que no están en ninguna factura (p. ej. falta subir la 8268, que va aparte)
    sueltas = [r for r in recogidas_del_mes(con, mes) if not r['viaje']]
    if sueltas:
        c.append({'nivel': 'aviso', 'texto': f'{len(sueltas)} recogida(s) sin su viaje en ninguna factura',
                  'detalle': ', '.join(f"{r['numero']} ({r['emisor']}, {r['origen'] or 'origen sin leer'})" for r in sueltas)
                  + ' · si su factura va aparte, súbela también', 'enlace': url_for('facturas')})
    facs = facturas_del_mes(con, mes)
    if not facs:
        c.append({'nivel': 'falta', 'texto': 'Falta subir la factura del mes', 'detalle': '',
                  'enlace': url_for('facturas')})
        return c
    sin_facturar = None
    for fac in facs:
        datos = cruzar_factura(json.loads(fac['datos']), con)
        con.execute('UPDATE facturas SET datos=? WHERE id=?', (json.dumps(datos), fac['id']))
        reales = [v for v in datos['viajes'] if v['estado'] != 'suelta']
        mal = [v for v in reales if v['estado'] != 'ok']
        c.append({'nivel': 'ok' if datos.get('cuadra') else 'falta',
                  'texto': f"Factura {fac['numero']} leída" + (' y sus líneas cuadran con el total' if datos.get('cuadra')
                                                              else ': las líneas NO suman el total'),
                  'detalle': '', 'enlace': url_for('factura_ver', fid=fac['id'])})
        c.append({'nivel': 'aviso' if mal else 'ok',
                  'texto': f"{len(mal)} de {len(reales)} viajes de la factura {fac['numero']} no cuadran (palés o albaranes)"
                  if mal else f"Los {len(reales)} viajes de la factura {fac['numero']} cuadran con sus albaranes",
                  'detalle': ', '.join(f"{v['viaje']} ({v['estado']})" for v in mal[:20]),
                  'enlace': url_for('factura_ver', fid=fac['id'])})
        # lo que no está en NINGUNA factura (cada una ya descuenta lo de las demás)
        nums = {a['numero'] for a in datos.get('sin_facturar') or []}
        sin_facturar = nums if sin_facturar is None else sin_facturar & nums
    sf = sorted(sin_facturar or [])
    c.append({'nivel': 'aviso' if sf else 'ok',
              'texto': f'{len(sf)} albarán(es) escaneado(s) que no están en ningún viaje facturado' if sf
              else 'Todo lo escaneado está facturado',
              'detalle': ', '.join(sf), 'enlace': url_for('factura_ver', fid=facs[0]['id'])})
    return c


def albaranes_del_mes(con: sqlite3.Connection, mes: str) -> list:
    """Los del mes: los de su fecha de carga y los que van en un viaje de sus facturas (la
    recogida de Fuji Oil se cargó el 01-10 y va en la factura 8268 de 30-09), menos los que ya
    van en la factura de otro mes."""
    de_sus_facturas, de_otras = set(), set()
    for f in con.execute("SELECT fecha, datos FROM facturas WHERE estado='lista'"):
        nums = {n for v in json.loads(f['datos'] or '{}').get('viajes', []) for n in v.get('albaranes', [])}
        (de_sus_facturas if (f['fecha'] or '').startswith(mes) else de_otras).update(nums)
    de_otras -= de_sus_facturas
    return [a for a in con.execute(
        "SELECT * FROM albaranes WHERE estado NOT IN ('descartado', 'revisar') AND numero IS NOT NULL "
        "ORDER BY ruta").fetchall()
        if a['numero'] in de_sus_facturas or ((a['fecha_doc'] or '').startswith(mes) and a['numero'] not in de_otras)]


@app.route('/mes/<mes>/paquete')
@requiere_acceso
def mes_paquete(mes):
    """Todo lo del mes en un ZIP: los albaranes ya nombrados, por carpetas, y el Excel."""
    import io
    import zipfile
    if not re.fullmatch(r'\d{4}-\d{2}', mes):
        return redirect(url_for('mes_en_curso'))
    buf = io.BytesIO()
    with db() as con, zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for a in albaranes_del_mes(con, mes):
            ruta = DATA / a['ruta']
            if ruta.exists():
                z.write(ruta, Path(a['ruta']).relative_to('archivo'))
        facs = facturas_del_mes(con, mes)
        if facs:
            contenido, nombre = excel_de_factura(con, facs, mes)
            z.writestr(nombre, contenido)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'albaranes-{mes}.zip')


@app.route('/mes/<mes>/cerrar', methods=['POST'])
@requiere_acceso
def mes_cerrar(mes):
    """Da el mes por entregado: lo de cada cliente queda «entregado» con la fecha del cierre y
    lo que no tiene cliente, archivado. Se usa después de mandar el paquete."""
    if not re.fullmatch(r'\d{4}-\d{2}', mes):
        return redirect(url_for('mes_en_curso'))
    with db() as con:
        if request.form.get('accion') == 'reabrir':
            con.execute("UPDATE albaranes SET estado='colocado', entregado_el=NULL, entrega_nota='' "
                        "WHERE entrega_nota=?", (f'Cierre del mes {mes[5:]}/{mes[:4]}',))
            cfg_set(con, f'cierre_{mes}', '')
            flash(f'Mes {mes[5:]}/{mes[:4]} reabierto.')
            return redirect(url_for('mes_en_curso', m=mes))
        avisos = [x for x in comprobar_cierre(con, mes, viajes(con, mes)) if x['nivel'] != 'ok']
        if avisos and not request.form.get('confirmo'):
            flash('Hay avisos sin resolver: marca la casilla para cerrar el mes igualmente.')
            return redirect(url_for('mes_en_curso', m=mes) + '#cierre')
        ahora = datetime.now().isoformat(timespec='seconds')
        nota = f'Cierre del mes {mes[5:]}/{mes[:4]}'
        n = 0
        for a in albaranes_del_mes(con, mes):
            if a['estado'] == 'entregado':
                continue
            con.execute('UPDATE albaranes SET estado=?, entregado_el=?, entrega_nota=? WHERE id=?',
                        ('entregado' if a['cliente_id'] else 'archivado', ahora, nota, a['id']))
            n += 1
        cfg_set(con, f'cierre_{mes}', json.dumps({'fecha': ahora, 'albaranes': n, 'avisos': len(avisos)}))
    flash(f'Mes {mes[5:]}/{mes[:4]} cerrado: {n} albaranes dados por entregados.')
    return redirect(url_for('mes_en_curso', m=mes))


@app.route('/por-entregar')
@requiere_acceso
def pendientes():
    """Lo aprobado que aún no ha llegado a su cliente, agrupado por cliente y con el motivo."""
    with db() as con:
        filas = con.execute(
            "SELECT a.*, c.nombre AS cliente, c.modo_entrega, c.email_para, c.portal_url "
            "FROM albaranes a LEFT JOIN clientes c ON c.id=a.cliente_id "
            "WHERE a.estado='pendiente_entrega' ORDER BY c.nombre, a.ruta").fetchall()
        sin_smtp = not smtp_config(con)
    grupos = {}
    for a in filas:
        g = grupos.setdefault(a['cliente_id'], {'cliente_id': a['cliente_id'], 'cliente': a['cliente'],
                                                'modo': a['modo_entrega'], 'destino': destino_texto(a),
                                                'albaranes': []})
        g['albaranes'].append(a)
    return render_template('por_entregar.html', grupos=list(grupos.values()), sin_smtp=sin_smtp)


@app.route('/por-entregar/<int:cid>/reintentar', methods=['POST'])
@requiere_acceso
def reintentar(cid):
    """Vuelve a intentar la entrega de todo lo pendiente de un cliente."""
    with db() as con:
        ids = [r['id'] for r in con.execute(
            "SELECT id FROM albaranes WHERE cliente_id=? AND estado='pendiente_entrega'", (cid,))]
        resultados = []
        for i in ids:
            r = entregar(i, con)
            if r != 'ok':
                con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?', (r, i))
            resultados.append(r)
    ok = sum(r == 'ok' for r in resultados)
    fallos = [r for r in resultados if r != 'ok']
    flash(f'{ok} entregado(s).' + (f' {len(fallos)} siguen pendientes: {fallos[0]}' if fallos else ''))
    return redirect(url_for('pendientes'))


@app.route('/panel')
@requiere_acceso
def index():
    with db() as con:
        lotes = con.execute('SELECT * FROM lotes ORDER BY id DESC LIMIT 10').fetchall()
        contadores = dict(con.execute(
            "SELECT estado, COUNT(*) FROM albaranes GROUP BY estado").fetchall())
        pendientes = con.execute(
            "SELECT a.*, c.nombre AS cliente, c.modo_entrega FROM albaranes a "
            "LEFT JOIN clientes c ON c.id=a.cliente_id "
            "WHERE a.estado IN ('pendiente_entrega','revisar') ORDER BY a.id DESC").fetchall()
        sin_smtp = any(c['modo_entrega'] == 'email' for c in
                       con.execute('SELECT modo_entrega FROM clientes')) and not smtp_config(con)
    return render_template('index.html', lotes=lotes, contadores=contadores,
                           pendientes=pendientes, sin_smtp=sin_smtp)


@app.route('/subir', methods=['POST'])
@requiere_acceso
def subir():
    ficheros = [f for f in request.files.getlist('escaneo')
                if f and f.filename.lower().endswith('.pdf')]
    if not ficheros:
        flash('Sube uno o varios PDF (la pila escaneada).')
        return redirect(url_for('index'))
    ENTRADA.mkdir(parents=True, exist_ok=True)
    for n, f in enumerate(ficheros):
        destino = ENTRADA / f'{datetime.now():%Y%m%d-%H%M%S}-{n}-{Path(f.filename).name}'
        f.save(destino)
        with db() as con:
            cur = con.execute('INSERT INTO lotes (fichero, recibido) VALUES (?,?)',
                              (Path(f.filename).name, datetime.now().isoformat(timespec='seconds')))
            lote_id = cur.lastrowid
        threading.Thread(target=procesar_lote, args=(lote_id, destino), daemon=True).start()
    flash(f'{len(ficheros)} fichero(s) recibido(s): se están leyendo. Aquí los verás colocar.')
    return redirect(url_for('revision'))


@app.route('/revision')
@requiere_acceso
def revision():
    with db() as con:
        lotes_colgados(con)
        en_curso = con.execute("SELECT * FROM lotes WHERE estado='procesando' ORDER BY id").fetchall()
        con_repetidos = con.execute(
            "SELECT fichero, descartados FROM lotes WHERE descartados != '' AND recibido >= ? "
            "ORDER BY id DESC LIMIT 5", (f'{datetime.now():%Y-%m-%d}',)).fetchall()
        filas = con.execute(
            "SELECT a.*, c.nombre AS cliente, c.modo_entrega, c.email_para, c.portal_url, "
            "c.sftp_datos FROM albaranes a LEFT JOIN clientes c ON c.id=a.cliente_id "
            "WHERE a.estado IN ('colocado','revisar') ORDER BY c.nombre, a.numero").fetchall()
        clientes = con.execute('SELECT id, nombre FROM clientes ORDER BY nombre').fetchall()
        revisar_antes = cfg_get(con, 'revisar_antes') == '1'
        ordenes = []
        for o in con.execute('SELECT * FROM anexos WHERE unido_a IS NULL ORDER BY id').fetchall():
            refs = json.loads(o['refs'] or '[]')
            ya = con.execute(f"SELECT numero FROM albaranes WHERE numero IN ({','.join('?' * len(refs))}) "
                             "AND estado='entregado'", refs).fetchall() if refs else []
            rango = procesador.numeros_citados(o['ref_texto'])
            citados = json.loads(o['citados'] or '[]')
            hay = con.execute(f"SELECT COUNT(*) FROM albaranes WHERE estado != 'descartado' AND numero IN "
                              f"({','.join('?' * len(citados))})", citados).fetchone()[0] if citados else 1
            ordenes.append({**dict(o), 'refs_l': refs, 'rango': rango, 'esperando': not hay,
                            'citados_l': citados, 'entregados': [r['numero'] for r in ya]})
    grupos = {}  # carpeta → {cliente, destino, albaranes}
    for a in filas:
        if a['estado'] != 'colocado':
            continue
        carpeta = str(Path(a['ruta']).parent.relative_to('archivo')) + '/'
        g = grupos.setdefault(carpeta, {'cliente': a['cliente'], 'cliente_id': a['cliente_id'],
                                        'destino': destino_texto(a), 'modo': a['modo_entrega'],
                                        'albaranes': []})
        g['albaranes'].append(a)
    dudosos = [a for a in filas if a['estado'] == 'revisar']
    # lo bien colocado va plegado aparte; arriba solo lo raro que la app ha resuelto sola y conviene
    # mirar una vez (recogidas, dos versiones, sin fecha). Lo corregido o aprendido ya no se pide.
    def para_mirar(a):
        return not a['visto'] and (a['recogida'] or 'otra versión' in (a['adjuntos'] or '')
                                   or not a['fecha_doc'])
    mirar = [{'a': a, 'porque': por_que_mirar(a)} for g in grupos.values() for a in g['albaranes'] if para_mirar(a)]
    for g in grupos.values():
        g['albaranes'] = [a for a in g['albaranes'] if not para_mirar(a)]
    grupos = {k: g for k, g in grupos.items() if g['albaranes']}
    with db() as con:  # viajes con albaranes que su orden de carga cita y no están
        incompletos = [v for v in viajes(con) if v['faltan']]
        errores = lotes_con_error(con)
    return render_template('revision.html', mirar=mirar, incompletos=incompletos, errores=errores, en_curso=en_curso, grupos=grupos, ordenes=ordenes,
                           con_repetidos=con_repetidos,
                           dudosos=dudosos, clientes=clientes, revisar_antes=revisar_antes,
                           n_colocados=sum(len(g['albaranes']) for g in grupos.values()) + len(mirar),
                           n_bien=sum(len(g['albaranes']) for g in grupos.values()))


def bonito(lugar: str) -> str:
    """SANTA PERPETUA DE LA MOGODA → Santa Perpetua de la Mogoda"""
    menores = {'de', 'del', 'la', 'las', 'los', 'el', 'y', 'i'}
    return ' '.join(w.lower() if i and w.lower() in menores else w.capitalize()
                    for i, w in enumerate((lugar or '').split()))


def por_que_mirar(a) -> list[dict]:
    """Explicación en cristiano de por qué un albarán bien colocado sale en «Para mirar»."""
    casos = []
    if 'otra versión' in (a['adjuntos'] or ''):
        with db() as con:
            l = con.execute('SELECT descartados FROM lotes WHERE id=?', (a['lote_id'],)).fetchone()
        m = re.search(re.escape(a['numero'] or '') + r' \(trae otra versión con ([^:]+): mandan los datos de la última, ([^)]+)\)',
                      (l['descartados'] if l else '') or '')
        def es(t):  # 3115.8 kg → 3.115,8 kg
            return re.sub(r'\d+(?:\.\d+)?', lambda x: f'{float(x.group()):,.2f}'.rstrip('0').rstrip('.')
                          .replace(',', '_').replace('.', ',').replace('_', '.'), t)
        vieja, buena = (es(m.group(1)), es(m.group(2))) if m else ('otros datos', 'los de la última')
        casos.append({'titulo': 'Escaneado dos veces, con datos distintos',
                      'texto': f'En el escaneo venía este albarán dos veces y las copias no dicen lo mismo: una pone '
                               f'{vieja} y la otra {buena}. Suele pasar cuando se rehace el albarán (p. ej. al final '
                               f'se carga un palé menos). La app usa la última, {buena}, para el Excel y la factura, '
                               f'y el PDF guarda las dos copias.',
                      'si_no': 'Si la buena es la otra, cuéntalo en «Corregir».'})
    if a['recogida']:
        emisor = (a['emisor'] or 'otra empresa').replace('-', ' ').title()
        cliente = a['cliente'] or 'el cliente'
        casos.append({'titulo': f'Recogida: el albarán es de {emisor}, no de {cliente}',
                      'texto': f'El camión recogió la mercancía en {emisor}'
                               f'{" (" + bonito(a["origen"]) + ")" if a["origen"] else ""} y la llevó a {cliente}. '
                               f'Por eso la app lo guarda con los albaranes de {cliente} y lo cruza con el viaje de '
                               f'recogida de la factura.',
                      'si_no': f'Si con {emisor} es siempre así, cuéntalo en «Corregir» con «Hacer siempre esto» '
                               f'y no volverá a preguntarse.'})
    if not a['fecha_doc']:
        casos.append({'titulo': 'Sin fecha de carga',
                      'texto': f'No se ha podido leer la fecha de carga y se ha guardado en el mes en que se escaneó '
                               f'({a["creado"][5:7]}/{a["creado"][:4]}).',
                      'si_no': 'Si el mes no es el bueno, pon la fecha en «Corregir».'})
    return casos


@app.route('/albaran/<int:aid>/visto', methods=['POST'])
@requiere_acceso
def albaran_visto(aid):
    """«Está bien así»: sale de «Para mirar» y pasa a los bien colocados."""
    with db() as con:
        con.execute('UPDATE albaranes SET visto=1 WHERE id=?', (aid,))
        a = con.execute('SELECT numero FROM albaranes WHERE id=?', (aid,)).fetchone()
    flash(f'✓ {a["numero"] if a else "Albarán"}: visto y bien. Pasa a los bien colocados.')
    return redirect(url_for('revision'))


def destino_texto(a) -> str:
    if not a['cliente']:
        return 'sin cliente configurado: solo se archiva'
    modo = a['modo_entrega']
    if modo == 'email':
        return f"email a {a['email_para'] or '(sin email configurado)'}"
    if modo == 'sftp':
        return f"SFTP {a['sftp_datos'] or '(sin configurar)'}"
    if modo == 'portal':
        return f"subir al portal {a['portal_url'] or '(sin URL)'}"
    return 'solo archivar (este cliente no recibe envío)'


@app.route('/revision/enviar', methods=['POST'])
@requiere_acceso
def revision_enviar():
    """Aprueba lo colocado (todo, o solo una carpeta) y lo manda a cada cliente."""
    carpeta = request.form.get('carpeta', '')
    with db() as con:
        ids = [r['id'] for r in con.execute(
            "SELECT id, ruta FROM albaranes WHERE estado='colocado'")
            if not carpeta or r['ruta'].startswith(f'archivo/{carpeta}')]
        resultados = [aprobar(i, con) for i in ids]
    ok = sum(r == 'ok' for r in resultados)
    archivados = sum(r == 'archivado' for r in resultados)
    a_portal = sum(r == 'para subir al portal' for r in resultados)
    otros = len(resultados) - ok - archivados - a_portal
    partes = [f'{ok} enviado(s) a su cliente'] if ok else []
    if archivados:
        partes.append(f'{archivados} solo archivado(s)')
    if a_portal:
        partes.append(f'{a_portal} preparado(s) para subir al portal')
    if otros:
        partes.append(f'{otros} pendiente(s) de entrega (ver «Por entregar»)')
    flash('Hecho: ' + ', '.join(partes) + '.' if partes else 'No había nada colocado.')
    if a_portal and not ok and not otros:
        with db() as con:
            cid = con.execute("SELECT cliente_id FROM albaranes WHERE id=?", (ids[0],)).fetchone()[0]
        if cid and all(r == 'para subir al portal' for r in resultados):
            return redirect(url_for('portal', cid=cid))
    return redirect(url_for('revision'))


@app.route('/portal/<int:cid>')
@requiere_acceso
def portal(cid):
    """Subida manual a un portal: abrir el portal, descargar lo pendiente ya con el
    nombre que pide el cliente, subirlo allí y marcarlo aquí como subido."""
    with db() as con:
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (cid,)).fetchone()
        if not cliente:
            return redirect(url_for('clientes'))
        pendientes = con.execute(
            "SELECT * FROM albaranes WHERE cliente_id=? AND estado='pendiente_entrega' "
            "ORDER BY ruta", (cid,)).fetchall()
        subidos = con.execute(
            "SELECT * FROM albaranes WHERE cliente_id=? AND estado='entregado' "
            "ORDER BY entregado_el DESC LIMIT 10", (cid,)).fetchall()
    return render_template('portal.html', c=cliente, pendientes=pendientes, subidos=subidos)


@app.route('/portal/<int:cid>/zip')
@requiere_acceso
def portal_zip(cid):
    """Los pendientes de ese cliente en un ZIP plano, con su nombre de entrega."""
    import io
    import zipfile
    with db() as con:
        cliente = con.execute('SELECT nombre FROM clientes WHERE id=?', (cid,)).fetchone()
        filas = con.execute("SELECT ruta FROM albaranes WHERE cliente_id=? AND "
                            "estado='pendiente_entrega' ORDER BY ruta", (cid,)).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for a in filas:
            z.write(DATA / a['ruta'], Path(a['ruta']).name)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"{procesador.normaliza(cliente['nombre'])}-para-subir-"
                                   f"{datetime.now():%Y%m%d}.zip")


@app.route('/portal/<int:cid>/subidos', methods=['POST'])
@requiere_acceso
def portal_subidos(cid):
    ids = [int(i) for i in request.form.getlist('ids') if i.isdigit()]
    ahora = datetime.now().isoformat(timespec='seconds')
    with db() as con:
        for i in ids:
            con.execute("UPDATE albaranes SET estado='entregado', entrega_nota='', entregado_el=? "
                        "WHERE id=? AND cliente_id=? AND estado='pendiente_entrega'", (ahora, i, cid))
    flash(f'{len(ids)} albarán(es) marcado(s) como subidos al portal.' if ids
          else 'No has marcado ninguno.')
    return redirect(url_for('portal', cid=cid))


def recolocar_corregido(con: sqlite3.Connection, a, numero: str, cliente, fecha_nueva) -> str | None:
    """Vuelve a colocar un albarán con otro número, cliente o fecha. Devuelve el error o None."""
    cuando = fecha_nueva or fecha_de(a)
    carpeta = carpeta_de(cliente, a['emisor'], cuando)
    carpeta.mkdir(parents=True, exist_ok=True)
    origen = DATA / a['ruta']
    rec = de_recogida(a)
    nombre = nombre_fichero(cliente, numero, a['emisor'], cuando, rec)
    if rec:  # dos recogidas del mismo día y ruta: -2, -3… (el número no va en el nombre)
        destino = destino_libre(carpeta, nombre, origen)
    elif (carpeta / nombre).exists() and carpeta / nombre != origen:
        return f'Ya hay un albarán con el nombre {nombre} en {carpeta.relative_to(ARCHIVO)}.'
    else:
        destino = carpeta / nombre
    origen.rename(destino)
    con.execute("UPDATE albaranes SET numero=?, cliente_id=?, ruta=?, confianza='corregido', visto=1, "
                "estado=CASE WHEN estado IN ('revisar','colocado') THEN 'colocado' ELSE estado END, "
                "entrega_nota='', fecha_doc=? WHERE id=?",
                (numero, cliente['id'] if cliente else None, str(destino.relative_to(DATA)),
                 f'{fecha_nueva:%Y-%m-%d}' if fecha_nueva else a['fecha_doc'], a['id']))
    return None


@app.route('/albaran/<int:aid>/corregir', methods=['POST'])
@requiere_acceso
def albaran_corregir(aid):
    """Corrige número y/o cliente de un albarán y lo vuelve a colocar en su carpeta."""
    numero = ''.join(request.form.get('numero', '').split())
    cid = request.form.get('cliente_id') or None
    fecha_nueva = fecha_valida(request.form.get('fecha', ''))
    if not numero:
        flash('Pon el número del albarán.')
        return redirect(url_for('revision'))
    with db() as con:
        a = con.execute('SELECT * FROM albaranes WHERE id=?', (aid,)).fetchone()
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (cid,)).fetchone() if cid else None
        error = recolocar_corregido(con, a, numero, cliente, fecha_nueva)
    flash(error or f'Albarán {numero} colocado.')
    return redirect(url_for('revision'))


@app.route('/albaran/<int:aid>/explicar', methods=['POST'])
@requiere_acceso
def albaran_explicar(aid):
    """Corregir hablando: se cuenta qué pasa, la app lo entiende y lo aplica a este albarán y,
    si se marca «siempre», a los parecidos que ya están y a los que vengan."""
    texto = request.form.get('texto', '').strip()
    siempre = request.form.get('alcance') == 'siempre'
    volver = redirect((request.referrer or url_for('revision')).split('#')[0] + f'#a{aid}')
    if not texto:
        flash('Cuenta qué pasa con el albarán.')
        return volver
    with db() as con:
        a = con.execute('SELECT a.*, c.nombre AS cliente FROM albaranes a LEFT JOIN clientes c '
                        'ON c.id=a.cliente_id WHERE a.id=?', (aid,)).fetchone()
        clientes = {r['nombre']: r['id'] for r in con.execute('SELECT id, nombre FROM clientes')}
    if not a:
        return volver
    try:
        r = aprender.interpretar(dict(a), list(clientes), texto, siempre)
    except Exception as e:  # sin conexión con el modelo: que se vea, sin tocar nada
        flash(f'No he podido consultarlo ahora mismo ({e.__class__.__name__}). Prueba otra vez en un momento.')
        return volver
    if r.get('duda'):
        flash(f'🤔 {r["duda"]}')
        return volver
    este = r.get('este') or {}
    with db() as con:
        a = con.execute('SELECT * FROM albaranes WHERE id=?', (aid,)).fetchone()
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (a['cliente_id'],)).fetchone() \
            if a['cliente_id'] else None
        if 'cliente' in este:
            cliente = con.execute('SELECT * FROM clientes WHERE id=?', (clientes.get(este['cliente']),)).fetchone() \
                if este['cliente'] else None
        numero = ''.join(str(este.get('numero') or a['numero'] or '').split())
        fecha_nueva = fecha_valida(este.get('fecha')) if este.get('fecha') else None
        error = recolocar_corregido(con, a, numero, cliente, fecha_nueva)
        if error:
            flash(error)
            return volver
        for campo, col in (('recogida', 'recogida'), ('origen', 'origen'), ('destino', 'destino'), ('pales', 'pales')):
            if campo in este:
                valor = int(bool(este[campo])) if campo == 'recogida' else este[campo]
                con.execute(f'UPDATE albaranes SET {col}=? WHERE id=?', (valor, aid))
        renombrar(con, aid)  # si ahora es (o deja de ser) recogida, cambia el nombre del fichero
        mensaje = f'✓ {r.get("entendido") or "Hecho."}'
        if siempre and r.get('regla'):
            rid = aprender.guardar_regla(con, texto, r.get('entendido') or '', r['regla'], clientes)
            if rid:
                otros = aplicar_regla_a_lo_que_hay(con, rid, aid)
                mensaje += ' A partir de ahora se hará siempre así' + (
                    f' (y ya lo he aplicado a {otros} albarán(es) parecidos que había).' if otros else '.')
        elif siempre:
            mensaje += ' (Esto es propio de este albarán: no hay nada que repetir en los demás.)'
        con.execute('UPDATE albaranes SET visto=1 WHERE id=?', (aid,))
    flash(mensaje)
    return volver


def aplicar_regla_a_lo_que_hay(con: sqlite3.Connection, rid: int, salvo: int) -> int:
    """Una regla nueva también vale para lo ya subido que no se ha entregado."""
    regla = con.execute('SELECT * FROM reglas WHERE id=?', (rid,)).fetchone()
    hacer = json.loads(regla['hacer'])
    n = 0
    for a in con.execute("SELECT * FROM albaranes WHERE id != ? AND estado IN ('colocado','revisar') "
                         "AND numero IS NOT NULL", (salvo,)).fetchall():
        if aprender.regla_para(con, dict(a)) is None or aprender.regla_para(con, dict(a))['id'] != rid:
            continue
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (hacer.get('cliente_id', a['cliente_id']),)).fetchone() \
            if hacer.get('cliente_id', a['cliente_id']) else None
        if not recolocar_corregido(con, a, a['numero'], cliente, None):
            if 'recogida' in hacer:
                con.execute('UPDATE albaranes SET recogida=? WHERE id=?', (int(hacer['recogida']), a['id']))
                renombrar(con, a['id'])
            n += 1
    if n:
        con.execute('UPDATE reglas SET veces = veces + ? WHERE id=?', (n, rid))
    return n


@app.route('/regla/<int:rid>/olvidar', methods=['POST'])
@requiere_acceso
def regla_olvidar(rid):
    with db() as con:
        con.execute('DELETE FROM reglas WHERE id=?', (rid,))
    flash('Regla olvidada: lo que venga a partir de ahora ya no la sigue (lo ya colocado no se mueve).')
    return redirect(url_for('ajustes') + '#aprendido')


@app.route('/anexo/<int:xid>/unir', methods=['POST'])
@requiere_acceso
def anexo_unir(xid):
    """Pega a mano una orden de carga al albarán que se indique por su número."""
    numero = ''.join(request.form.get('numero', '').split())
    with db() as con:
        anexo = con.execute('SELECT * FROM anexos WHERE id=?', (xid,)).fetchone()
        alb = con.execute('SELECT * FROM albaranes WHERE numero=? ORDER BY id DESC', (numero,)).fetchone()
        if not anexo or anexo['unido_a']:
            flash('Esa orden de carga ya está unida.')
        elif not alb:
            flash(f'No hay ningún albarán {numero} escaneado todavía. Cuando llegue, se unirá solo.')
            con.execute('UPDATE anexos SET refs=? WHERE id=?', (json.dumps([numero]), xid))
        elif alb['estado'] == 'entregado':
            flash(f'El albarán {numero} ya se entregó: no se toca lo que ya tiene el cliente.')
        else:
            pegar_anexo(anexo, alb, con)
            flash(f'Orden de carga unida al albarán {numero}.')
    return redirect(url_for('revision'))


@app.route('/anexo/<int:xid>/pdf')
@requiere_acceso
def anexo_pdf(xid):
    with db() as con:
        a = con.execute('SELECT ruta FROM anexos WHERE id=?', (xid,)).fetchone()
    return send_file(DATA / a['ruta'], download_name=Path(a['ruta']).name)


@app.route('/anexo/<int:xid>/miniatura')
@requiere_acceso
def anexo_miniatura(xid):
    with db() as con:
        a = con.execute('SELECT ruta, creado FROM anexos WHERE id=?', (xid,)).fetchone()
    return miniatura_de(DATA / a['ruta'], f"anexo|{a['ruta']}|{a['creado']}")


@app.route('/albaran/<int:aid>/miniatura')
@requiere_acceso
def albaran_miniatura(aid):
    with db() as con:
        a = con.execute('SELECT ruta, creado FROM albaranes WHERE id=?', (aid,)).fetchone()
    return miniatura_de(DATA / a['ruta'], f"{a['ruta']}|{a['creado']}")


def miniatura_de(pdf: Path, base: str):
    # clave por ruta + fecha + mtime: SQLite reutiliza ids cuando se vacía la tabla
    clave = hashlib.sha1(f"{base}|{pdf.stat().st_mtime}".encode()).hexdigest()
    png = DATA / 'miniaturas' / f'{clave}.png'
    if not png.exists():
        png.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['pdftoppm', '-f', '1', '-l', '1', '-scale-to', '420', '-png',
                        '-singlefile', str(pdf), str(png.with_suffix(''))], check=True)
    return send_file(png, max_age=3600)


MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
         'septiembre', 'octubre', 'noviembre', 'diciembre']


def nombre_carpeta(nivel: int, slug: str, nombres_cliente: dict) -> str:
    """Nombre legible de una carpeta del archivo (cliente / año / mes)."""
    if nivel == 0:
        return {'REVISAR': 'Para revisar', '_descartados': 'Copias repetidas descartadas',
                '_ordenes-de-carga': 'Órdenes de carga'}.get(slug, nombres_cliente.get(slug, slug))
    if nivel == 2 and slug.isdigit() and 1 <= int(slug) <= 12:
        return f'{slug} · {MESES[int(slug) - 1]}'
    return slug


SIN_ENVIAR = ('colocado', 'pendiente_entrega', 'revisar')
FILTROS = {'todos': None, 'enviados': ('entregado',), 'sin-enviar': SIN_ENVIAR}


@app.route('/albaranes')
@app.route('/albaranes/<path:carpeta>')
@requiere_acceso
def albaranes(carpeta=''):
    """Archivo navegable por carpetas, igual que queda en disco:
    cliente / año / mes / número.pdf. Con búsqueda, lista plana de resultados."""
    q = request.args.get('q', '').strip()
    ver = request.args.get('ver', 'todos')
    if ver not in FILTROS:
        ver = 'todos'
    carpeta = carpeta.strip('/')
    with db() as con:
        nombres_cliente = {procesador.normaliza(c['nombre']): c['nombre']
                           for c in con.execute('SELECT nombre FROM clientes')}
        if q:
            filas = con.execute(
                'SELECT a.*, c.nombre AS cliente FROM albaranes a '
                'LEFT JOIN clientes c ON c.id=a.cliente_id '
                'WHERE a.numero LIKE ? OR c.nombre LIKE ? OR a.emisor LIKE ? '
                'ORDER BY a.id DESC LIMIT 500', [f'%{q}%'] * 3).fetchall()
            return render_template('albaranes.html', q=q, resultados=filas, migas=[],
                                   carpetas=[], ficheros=[], carpeta='', ver='todos', cuenta={})
        prefijo = 'archivo/' + (carpeta + '/' if carpeta else '')
        filas = con.execute(
            'SELECT a.*, c.nombre AS cliente, c.modo_entrega FROM albaranes a '
            'LEFT JOIN clientes c ON c.id=a.cliente_id WHERE a.ruta LIKE ? '
            'ORDER BY a.ruta', (prefijo + '%',)).fetchall()
    nivel = len(carpeta.split('/')) if carpeta else 0
    cuenta = {'todos': len(filas),
              'enviados': sum(a['estado'] == 'entregado' for a in filas),
              'sin-enviar': sum(a['estado'] in SIN_ENVIAR for a in filas)}
    subcarpetas, ficheros = {}, []
    for a in filas:
        resto = a['ruta'][len(prefijo):]
        if '/' in resto:
            nombre = resto.split('/', 1)[0]
            c = subcarpetas.setdefault(nombre, {'slug': nombre, 'n': 0, 'sin_enviar': 0,
                                                'enviados': 0, 'ultimo': ''})
            c['n'] += 1
            c['sin_enviar'] += a['estado'] in SIN_ENVIAR
            c['enviados'] += a['estado'] == 'entregado'
            c['ultimo'] = max(c['ultimo'], a['creado'])
        elif not FILTROS[ver] or a['estado'] in FILTROS[ver]:
            ficheros.append(a)
    if FILTROS[ver]:  # con filtro, fuera las carpetas que no tienen nada de eso
        clave = 'enviados' if ver == 'enviados' else 'sin_enviar'
        subcarpetas = {k: c for k, c in subcarpetas.items() if c[clave]}
    carpetas = sorted(subcarpetas.values(), key=lambda c: c['slug'], reverse=nivel > 0)
    for c in carpetas:
        c['nombre'] = nombre_carpeta(nivel, c['slug'], nombres_cliente)
        c['ruta'] = (carpeta + '/' if carpeta else '') + c['slug']
    partes = carpeta.split('/') if carpeta else []
    migas = [{'nombre': nombre_carpeta(i, parte, nombres_cliente),
              'ruta': '/'.join(partes[:i + 1])} for i, parte in enumerate(partes)]
    return render_template('albaranes.html', q='', resultados=None, migas=migas,
                           carpetas=carpetas, ficheros=ficheros, carpeta=carpeta,
                           ver=ver, cuenta=cuenta)


@app.route('/zip/<path:carpeta>')
@requiere_acceso
def carpeta_zip(carpeta):
    """Descarga una carpeta del archivo entera en un ZIP, con la misma estructura."""
    import io
    import zipfile
    carpeta = carpeta.strip('/')
    base = (ARCHIVO / carpeta).resolve()
    if not base.is_dir() or ARCHIVO.resolve() not in base.parents:
        flash('Esa carpeta no existe.')
        return redirect(url_for('albaranes'))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(base.rglob('*.pdf')):
            z.write(f, f.relative_to(ARCHIVO.resolve()))
    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     download_name=carpeta.replace('/', '-') + '.zip', as_attachment=True)


@app.route('/albaran/<int:aid>/pdf')
@requiere_acceso
def albaran_pdf(aid):
    with db() as con:
        a = con.execute('SELECT ruta, numero FROM albaranes WHERE id=?', (aid,)).fetchone()
    return send_file(DATA / a['ruta'], download_name=Path(a['ruta']).name)


@app.route('/albaran/<int:aid>/entregar', methods=['POST'])
@requiere_acceso
def albaran_entregar(aid):
    with db() as con:
        resultado = entregar(aid, con)
        if resultado != 'ok':
            con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?', (resultado, aid))
    flash('Entregado.' if resultado == 'ok' else f'Entrega: {resultado}')
    return redirect(request.referrer or url_for('index'))


@app.route('/albaran/<int:aid>/marcar-entregado', methods=['POST'])
@requiere_acceso
def albaran_marcar(aid):
    with db() as con:
        con.execute('UPDATE albaranes SET estado=?, entrega_nota=?, entregado_el=? WHERE id=?',
                    ('entregado', '', datetime.now().isoformat(timespec='seconds'), aid))
    flash('Marcado como entregado.')
    return redirect(request.referrer or url_for('index'))


@app.route('/lote/<int:lid>/borrar', methods=['POST'])
@requiere_acceso
def lote_borrar(lid):
    """Borra un lote entero (registros y PDFs archivados) para poder rehacerlo."""
    with db() as con:
        for a in con.execute('SELECT ruta FROM albaranes WHERE lote_id=? UNION ALL '
                             'SELECT ruta FROM anexos WHERE lote_id=?', (lid, lid)):
            (DATA / a['ruta']).unlink(missing_ok=True)
        con.execute('DELETE FROM albaranes WHERE lote_id=?', (lid,))
        con.execute('DELETE FROM anexos WHERE lote_id=?', (lid,))
        con.execute('DELETE FROM lotes WHERE id=?', (lid,))
    flash(f'Lote {lid} borrado (registros y archivos). Puedes volver a subir el escaneo.')
    return redirect(url_for('index'))


@app.route('/clientes')
@requiere_acceso
def clientes():
    with db() as con:
        filas = con.execute('SELECT c.*, COUNT(a.id) AS n_albaranes FROM clientes c '
                            'LEFT JOIN albaranes a ON a.cliente_id=c.id '
                            'GROUP BY c.id ORDER BY c.nombre').fetchall()
    return render_template('clientes.html', filas=filas)


@app.route('/clientes/nuevo', methods=['GET', 'POST'])
@app.route('/clientes/<int:cid>', methods=['GET', 'POST'])
@requiere_acceso
def cliente_form(cid=None):
    with db() as con:
        if request.method == 'POST':
            campos = {k: request.form.get(k, '').strip() for k in
                      ('nombre', 'alias', 'modo_entrega', 'email_para', 'email_asunto',
                       'sftp_datos', 'portal_url', 'notas', 'nombre_formato', 'instrucciones')}
            campos['nombre_formato'] = campos['nombre_formato'] or '{numero}'
            campos['auto_entrega'] = 1 if request.form.get('auto_entrega') else 0
            if not campos['nombre']:
                flash('El nombre es obligatorio.')
                return redirect(request.url)
            if cid:
                con.execute('UPDATE clientes SET ' + ','.join(f'{k}=?' for k in campos)
                            + ' WHERE id=?', [*campos.values(), cid])
            else:
                con.execute('INSERT INTO clientes (' + ','.join(campos) + ') VALUES ('
                            + ','.join('?' * len(campos)) + ')', list(campos.values()))
            flash('Cliente guardado.')
            return redirect(url_for('clientes'))
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (cid,)).fetchone() if cid else None
    return render_template('cliente_form.html', c=cliente, modos=MODOS_ENTREGA)


@app.route('/ajustes', methods=['GET', 'POST'])
@requiere_acceso
def ajustes():
    with db() as con:
        if request.method == 'POST':
            accion = request.form.get('accion')
            if accion == 'smtp':
                for c in CLAVES_SMTP:
                    valor = request.form.get(c, '').strip()
                    if c == 'smtp_clave' and not valor:
                        continue  # clave vacía = conservar la guardada
                    cfg_set(con, c, valor)
                flash('Comunicaciones guardadas.')
            elif accion == 'buzon':
                for c in CLAVES_IMAP:
                    valor = request.form.get(c, '').strip()
                    if c == 'imap_clave' and not valor:
                        continue  # clave vacía = conservar la guardada
                    cfg_set(con, c, valor)
                cfg_set(con, 'imap_activo', '1' if request.form.get('imap_activo') else '0')
                flash('Buzón guardado.')
            elif accion == 'buzon_probar':
                r = leer_buzon(solo_probar=True)
                flash(r)
            elif accion == 'revision':
                cfg_set(con, 'revisar_antes', '1' if request.form.get('revisar_antes') else '0')
                flash('Guardado.')
            elif accion == 'probar':
                cfg = smtp_config(con)
                destino = request.form.get('destino', '').strip()
                if not cfg or not destino:
                    flash('Falta configurar el SMTP o el destinatario de la prueba.')
                else:
                    msg = EmailMessage()
                    msg['From'], msg['To'] = cfg['de'], destino
                    msg['Subject'] = 'Prueba de comunicaciones — aplicativo de albaranes'
                    msg.set_content('Si lees esto, el envío de albaranes funciona.')
                    r = enviar_smtp(cfg, msg)
                    flash('Email de prueba enviado.' if r == 'ok' else f'Fallo: {r}')
            elif accion == 'clave':
                actual, nueva = request.form.get('actual', ''), request.form.get('nueva', '')
                guardado = cfg_get(con, 'clave_hash')
                if guardado and not verifica_clave(actual, guardado):
                    flash('La contraseña actual no es correcta.')
                elif len(nueva) < 8:
                    flash('La contraseña nueva debe tener al menos 8 caracteres.')
                else:
                    cfg_set(con, 'clave_hash', hash_clave(nueva))
                    session['acceso'] = True
                    flash('Contraseña cambiada.')
            return redirect(url_for('ajustes'))
        smtp = {c: cfg_get(con, c) for c in CLAVES_SMTP}
        buzon = {c: cfg_get(con, c) for c in CLAVES_IMAP if c != 'imap_clave'}
        buzon['activo'] = cfg_get(con, 'imap_activo') == '1'
        buzon['hay_clave'] = bool(cfg_get(con, 'imap_clave'))
        buzon['estado'] = cfg_get(con, 'imap_estado')
        buzon['comprobado'] = cfg_get(con, 'imap_comprobado')
        hay_clave = bool(cfg_get(con, 'clave_hash'))
        revisar_antes = cfg_get(con, 'revisar_antes') == '1'
        repaso = json.loads(cfg_get(con, 'repaso') or '{}')
        nombres = {r['id']: r['nombre'] for r in con.execute('SELECT id, nombre FROM clientes')}
        reglas = []
        for r in con.execute('SELECT * FROM reglas ORDER BY id DESC'):
            cuando = json.loads(r['cuando'] or '{}')
            hacer = json.loads(r['hacer'] or '{}')
            reglas.append({**dict(r), 'cuando_l': cuando, 'cliente': nombres.get(hacer.get('cliente_id'), '')
                           if 'cliente_id' in hacer else None, 'recogida': hacer.get('recogida')})
    return render_template('ajustes.html', reglas=reglas, repaso=repaso, smtp=smtp, buzon=buzon, hay_clave=hay_clave, version=VERSION,
                           revisar_antes=revisar_antes)


def repasar_todo() -> None:
    """Relee todos los albaranes ya subidos con el lector actual y los deja como los dejaría
    un escaneo de hoy: hojas que son órdenes de carga pasan a serlo, el número se confirma
    con tres lecturas, cada uno va al año/mes de su FECHA DE CARGA (la suya o la de su
    orden de carga), las copias repetidas se apartan y las órdenes se pegan a su albarán.
    Lo entregado no se mueve: solo se informa."""
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    with db() as con:
        filas = [dict(r) for r in con.execute(
            "SELECT * FROM albaranes WHERE estado != 'descartado' ORDER BY id")]
        cfg_set(con, 'repaso', json.dumps({'estado': 'leyendo', 'total': len(filas),
                                           'inicio': datetime.now().isoformat(timespec='seconds')}))

    def leer(a):
        pdf = DATA / a['ruta']
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['pdftoppm', '-r', str(procesador.DPI), '-png', '-singlefile', '-f', '1',
                            '-l', '1', str(pdf), f'{tmp}/p'], check=True)
            return procesador._leer_derecha(Path(f'{tmp}/p.png'), pdf, 0, Path(tmp))[0]
    try:
        with ThreadPoolExecutor(max_workers=procesador.LECTURAS_A_LA_VEZ) as ex:
            lecturas = list(ex.map(leer, filas))
        cambios = []
        with db() as con:
            ahora = datetime.now()
            for a, info in zip(filas, lecturas):
                nombre = Path(a['ruta']).name
                if info.get('tipo') == 'orden_carga' and a['estado'] != 'entregado':
                    carpeta = ARCHIVO / '_ordenes-de-carga'
                    carpeta.mkdir(parents=True, exist_ok=True)
                    destino = destino_libre(carpeta, f"orden-{procesador.normaliza(info.get('albaranes_ref') or nombre)}.pdf")
                    (DATA / a['ruta']).rename(destino)
                    rango = procesador.numeros_citados(info.get('albaranes_ref'))
                    alta_anexo(con, a['lote_id'], {'tipo': 'orden_carga', 'ref_texto': info.get('albaranes_ref') or '',
                                                   'refs': rango[:1], 'rango': rango,
                                                   'fecha': info.get('fecha'),
                                                   'paginas': [0] * a['paginas']}, destino, ahora)
                    con.execute('DELETE FROM albaranes WHERE id=?', (a['id'],))
                    cambios.append(f"📎 {nombre}: era una ORDEN DE CARGA (albarán {info.get('albaranes_ref')}, "
                                   f"fecha {info.get('fecha')}) → pasa a orden de carga")
                    continue
                con.execute('UPDATE albaranes SET sellado=?, peso_kg=?, pales=?, destino=? WHERE id=?',
                            (int(str(info.get('sellado')).lower() == 'true'),
                             procesador._num(info.get('peso_bruto')), procesador._num(info.get('pales')),
                             (info.get('destino') or '').strip().upper() or None, a['id']))
                seguro = info.get('numero') and info.get('confianza') != 'baja'
                if not seguro or info['numero'] != a['numero']:
                    visto = info.get('numero') or 'ilegible'
                    if a['estado'] == 'entregado':
                        cambios.append(f"⚠️ {nombre}: ya ENTREGADO y la relectura no confirma el número "
                                       f"(lee {visto})")
                    elif a['estado'] != 'revisar':
                        motivo = (f'la relectura lee {visto}' if visto != a['numero']
                                  else 'las tres lecturas del número no coinciden')
                        con.execute("UPDATE albaranes SET estado='revisar', entrega_nota=? WHERE id=?",
                                    (motivo, a['id']))
                        cambios.append(f"🔎 {nombre}: a revisar ({motivo})")
                    continue
                if a['estado'] == 'revisar':  # estaba en duda y ahora las tres lecturas lo confirman
                    con.execute("UPDATE albaranes SET estado='colocado', entrega_nota='' WHERE id=?", (a['id'],))
                    cambios.append(f"✅ {nombre}: número confirmado, sale de Revisar")
                fecha = fecha_valida(info.get('fecha'))
                if a['estado'] == 'entregado':
                    if fecha and a['fecha_doc'] != f'{fecha:%Y-%m-%d}':
                        con.execute('UPDATE albaranes SET fecha_doc=? WHERE id=?',
                                    (f'{fecha:%Y-%m-%d}', a['id']))
                    continue
                if not fecha:
                    # sin fecha de carga propia: se quita la que se hubiera leído antes (p. ej. la
                    # del pedido) y la pondrá su orden de carga
                    con.execute('UPDATE albaranes SET fecha_doc=NULL WHERE id=?', (a['id'],))
                    continue
                if a['fecha_doc'] == f'{fecha:%Y-%m-%d}' and f'/{fecha:%Y}/{fecha:%m}/' in a['ruta']:
                    continue
                a2 = con.execute('SELECT * FROM albaranes WHERE id=?', (a['id'],)).fetchone()
                cambios.append(f"📁 {nombre}: carga {fecha:%d/%m/%Y} · {recolocar(con, a2, fecha)}")
            cambios += descartar_repetidos(con)
            unir_anexos(con)
            cambios += fechas_desde_ordenes(con)
            for a in con.execute("SELECT ruta FROM albaranes WHERE fecha_doc IS NULL "
                                 "AND estado NOT IN ('descartado', 'revisar')"):
                cambios.append(f"❓ {Path(a['ruta']).name}: sin fecha de carga (ni en el albarán ni en "
                               f"una orden de carga): sigue en {Path(a['ruta']).parent.relative_to('archivo')}/")
            for o in con.execute('SELECT * FROM anexos WHERE unido_a IS NOT NULL'):
                alb = con.execute('SELECT ruta FROM albaranes WHERE id=?', (o['unido_a'],)).fetchone()
                if alb:
                    cambios.append(f"📎 orden de carga {o['ref_texto']} → pegada a {Path(alb['ruta']).name}")
            cfg_set(con, 'repaso', json.dumps({'estado': 'hecho', 'total': len(filas), 'cambios': cambios,
                                               'fin': datetime.now().isoformat(timespec='seconds')}))
    except Exception as e:  # el repaso nunca se queda «leyendo» en silencio
        with db() as con:
            cfg_set(con, 'repaso', json.dumps({'estado': 'hecho', 'total': len(filas),
                                               'cambios': [f'❌ el repaso falló: {e}'],
                                               'fin': datetime.now().isoformat(timespec='seconds')}))


def repasar_sin_cliente() -> None:
    """Relee solo los albaranes que no son de ningún cliente: los que en realidad son una
    recogida PARA el cliente (Fuji Oil → Delaviuda) pasan a ser suyos, con su fecha de recogida."""
    import tempfile
    with db() as con:
        filas = [dict(r) for r in con.execute(
            "SELECT * FROM albaranes WHERE cliente_id IS NULL AND estado NOT IN ('descartado', 'entregado')")]
        cfg_set(con, 'repaso', json.dumps({'estado': 'leyendo', 'total': len(filas),
                                           'inicio': datetime.now().isoformat(timespec='seconds')}))
    cambios = []
    try:
        for a in filas:
            pdf = DATA / a['ruta']
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(['pdftoppm', '-r', str(procesador.DPI), '-png', '-singlefile', '-f', '1',
                                '-l', '1', str(pdf), f'{tmp}/p'], check=True)
                info = procesador._leer_derecha(Path(f'{tmp}/p.png'), pdf, 0, Path(tmp))[0]
            with _ESCRITURA, db() as con:
                cliente, recogida = cliente_del_albaran(a['emisor'], info.get('entrega_a'), con)
                if not recogida:
                    cambios.append(f"· {Path(a['ruta']).name}: sigue sin cliente (se entrega a {info.get('entrega_a') or '¿?'})")
                    continue
                origen = (info.get('origen') or '').strip().upper() or None
                con.execute('UPDATE albaranes SET cliente_id=?, recogida=1, origen=?, destino=COALESCE(?, destino) '
                            'WHERE id=?', (cliente['id'], origen,
                                           (info.get('destino') or '').strip().upper() or None, a['id']))
                fecha = fecha_valida(info.get('fecha')) or fecha_de(a)
                a2 = con.execute('SELECT * FROM albaranes WHERE id=?', (a['id'],)).fetchone()
                cambios.append(f"🚚 {Path(a['ruta']).name}: recogida en {origen or '¿?'} para {cliente['nombre']} "
                               f"({fecha:%d/%m/%Y}) · {recolocar(con, a2, fecha)}")
        with db() as con:
            cfg_set(con, 'repaso', json.dumps({'estado': 'hecho', 'total': len(filas), 'cambios': cambios,
                                               'fin': datetime.now().isoformat(timespec='seconds')}))
    except Exception as e:
        with db() as con:
            cfg_set(con, 'repaso', json.dumps({'estado': 'hecho', 'total': len(filas),
                                               'cambios': cambios + [f'❌ el repaso falló: {e}'],
                                               'fin': datetime.now().isoformat(timespec='seconds')}))


@app.route('/ajustes/repasar', methods=['POST'])
@requiere_acceso
def ajustes_repasar():
    if request.form.get('solo') == 'sin_cliente':
        threading.Thread(target=repasar_sin_cliente, daemon=True).start()
        flash('Releyendo los albaranes sin cliente (recogidas). El resultado sale aquí.')
        return redirect(url_for('ajustes') + '#repaso')
    threading.Thread(target=repasar_todo, daemon=True).start()
    flash('Repaso en marcha: se están releyendo todos los albaranes. El resultado sale aquí.')
    return redirect(url_for('ajustes') + '#repaso')


# ------------------------------------------------------------------ facturas
def cruzar_factura(datos: dict, con: sqlite3.Connection) -> dict:
    """Pone en cada viaje sus albaranes: los que cita la factura, los de su misma orden de
    carga y los de la misma fecha de carga (o la víspera) y destino que aún no están en otro
    viaje. Respeta lo añadido o quitado a mano. Compara los palés con la cantidad facturada."""
    from datetime import timedelta
    albs = [dict(r) for r in con.execute(
        "SELECT numero, fecha_doc, destino, pales, peso_kg, recogida, origen FROM albaranes "
        "WHERE estado != 'descartado' AND numero IS NOT NULL")]
    por_numero = {a['numero']: a for a in albs}
    filas_ordenes = con.execute('SELECT citados, fecha FROM anexos WHERE citados IS NOT NULL').fetchall()
    ordenes = [set(json.loads(o['citados'] or '[]')) for o in filas_ordenes]
    escaneados = {a['numero'] for a in albs}
    viajes = [v for v in datos['viajes'] if not exportar.es_linea_suelta({**v, 'albaranes': []})]
    for v in datos['viajes']:
        if v not in viajes:
            v['albaranes'], v['estado'] = [], 'suelta'
    usados = set()

    def tomar(v, lista):
        for n in lista:
            if n not in v['albaranes'] and n not in v.get('quitados', []) and (
                    n not in usados or n in v['refs'] or n in v.get('anadidos', [])):
                v['albaranes'].append(n)
                usados.add(n)
    # 1.ª pasada, lo seguro: lo que cita la factura, lo añadido a mano y los demás albaranes
    # que cita la misma orden de carga (uno a uno, nunca del primero al último)
    for v in viajes:
        v['albaranes'] = []
        tomar(v, v['refs'] + v.get('anadidos', []))
        for citados in ordenes:
            if citados & set(v['refs']):
                tomar(v, sorted(n for n in citados if n in escaneados))
    # recogidas (ORIGEN -> SONSECA): su albarán es el del proveedor recogido, del mismo origen
    # y de la fecha del viaje con tres días de margen (la 8268 factura el 1088 a 30-09 y la
    # recogida de Fuji Oil fue el 01-10)
    for v in viajes:
        v['recogida'] = exportar.es_recogida(v)
        v['origen'] = exportar.origen_de(v['descripcion']) if v['recogida'] else ''
        f = fecha_valida(v.get('fecha'))
        if not v['recogida'] or v['albaranes']:
            continue
        origen = procesador.normaliza(exportar.origen_de(v['descripcion']))
        candidatos = [a for a in albs if a['recogida'] and a['numero'] not in usados]
        mismo = [a for a in candidatos if a['origen'] and origen and (
            origen in procesador.normaliza(a['origen']) or procesador.normaliza(a['origen']) in origen)]
        if f:
            cerca = lambda a: a['fecha_doc'] and abs((fecha_valida(a['fecha_doc']) or f) - f).days <= 3
            mismo = [a for a in mismo if cerca(a)] or [a for a in candidatos if not a['origen'] and cerca(a)]
        tomar(v, [a['numero'] for a in mismo[:1]])
    # 2.ª pasada: lo que nadie ha cogido, por fecha de carga (o la víspera) y destino
    for v in viajes:
        if v.get('recogida'):
            continue
        f = fecha_valida(v.get('fecha'))
        destino = procesador.normaliza(exportar.sin_tildes(v['destino']))
        ya = sum((por_numero.get(n) or {}).get('pales') or 0 for n in v['albaranes'])
        cuadra_ya = v['albaranes'] and (v.get('cantidad') or 0) > 1 and abs(ya - v['cantidad']) < 0.01
        if f and destino and not cuadra_ya and not v['albaranes'][len(v['refs']):]:
            dias = {f'{f:%Y-%m-%d}', f'{f - timedelta(days=1):%Y-%m-%d}'}
            tomar(v, [a['numero'] for a in albs if a['numero'] not in usados and a['fecha_doc'] in dias
                      and a['destino'] and destino in procesador.normaliza(exportar.sin_tildes(a['destino']))])
    # 3.ª pasada: viaje que sigue vacío (la factura no cita albaranes y van a otra dirección,
    # p. ej. Sant Gregori → plataformas de Lidl Francia): si hay UNA sola orden de carga de su
    # fecha (o la víspera) con albaranes escaneados que nadie ha cogido, es la suya
    for v in viajes:
        f = fecha_valida(v.get('fecha'))
        if v['albaranes'] or not f or v.get('recogida'):
            continue
        dias = {f'{f:%Y-%m-%d}', f'{f - timedelta(days=1):%Y-%m-%d}'}
        libres = [sorted(n for n in json.loads(o['citados'] or '[]') if n in escaneados)
                  for o in filas_ordenes if o['fecha'] in dias]
        libres = [list(t) for t in dict.fromkeys(tuple(l) for l in libres if l and not usados & set(l))]
        if len(libres) == 1:  # (dos copias escaneadas de la misma orden cuentan como una)
            tomar(v, libres[0])
            continue
        # orden de carga de su fecha sin NINGÚN albarán escaneado (p. ej. el 955 de Sant
        # Gregori): el viaje es suyo y sus albaranes salen como «faltan», no como «sin albaranes»
        sin_escanear = [sorted(json.loads(o['citados'] or '[]')) for o in filas_ordenes if o['fecha'] in dias]
        sin_escanear = [list(t) for t in dict.fromkeys(
            tuple(l) for l in sin_escanear if l and not set(l) & escaneados and not usados & set(l))]
        if len(sin_escanear) == 1:
            tomar(v, sin_escanear[0])
    # 4.ª pasada: lo que sigue sin viaje. (a) Si un viaje del mismo destino tiene un hueco de
    # palés que es exactamente el del albarán, va ahí (80466430, 1 palé, en el 876 de 33).
    # (b) Si queda un viaje vacío y UN solo albarán suelto de su destino, es el suyo aunque la
    # fecha no cuadre (80466365 de Montcada en el 893).
    mes_f = (datos.get('fecha') or '')[:7]
    def sueltos():
        return [a for a in albs if a['numero'] not in usados and not a['recogida']
                and (a['fecha_doc'] or '').startswith(mes_f) and a['destino']]
    def mismo_destino(v, a):
        d = procesador.normaliza(exportar.sin_tildes(v['destino']))
        return d and d in procesador.normaliza(exportar.sin_tildes(a['destino']))
    for a in sueltos():
        huecos = []
        for v in viajes:
            if v.get('recogida') or not v['albaranes'] or not mismo_destino(v, a):
                continue
            ya = sum((por_numero.get(n) or {}).get('pales') or 0 for n in v['albaranes'])
            if (v.get('cantidad') or 0) > 1 and a['pales'] and abs(v['cantidad'] - ya - a['pales']) < 0.01:
                huecos.append(v)
        if len(huecos) == 1:
            tomar(huecos[0], [a['numero']])
    for v in viajes:
        if v['albaranes'] or v.get('recogida'):
            continue
        f = fecha_valida(v.get('fecha'))
        cand = [a for a in sueltos() if mismo_destino(v, a) and (
            not f or abs((fecha_valida(a['fecha_doc']) or f) - f).days <= 7)]
        if len(cand) == 1:  # (con una semana de margen: el 893 es del 03-09 y su albarán del 09-09)
            tomar(v, [cand[0]['numero']])
    for v in viajes:
        vistos = v['albaranes']
        v['faltan'] = [n for n in vistos if n not in por_numero]
        pales = sum((por_numero.get(n) or {}).get('pales') or 0 for n in vistos)
        v['pales_albaranes'] = pales
        cantidad = v.get('cantidad') or 0
        sin_pales = [n for n in vistos if n in por_numero and por_numero[n].get('pales') is None]
        v['sin_pales'] = sin_pales
        v['estado'] = ('sin albaranes' if not vistos else 'faltan escaneos' if v['faltan']
                       else 'ok' if v.get('recogida') or cantidad <= 1 or abs(pales - cantidad) < 0.01
                       else 'palés sin leer' if sin_pales else 'palés no cuadran')
    # para cerrar el círculo: lo escaneado del mes que no está en ningún viaje de la factura
    # (¿un viaje sin facturar?) y lo facturado que no se ha escaneado
    mes = (datos.get('fecha') or '')[:7]
    en_factura = {n for v in viajes for n in v['albaranes']} | en_otras_facturas(con, datos.get('numero'))
    datos['sin_facturar'] = [
        {'numero': r['numero'], 'destino': r['destino'], 'pales': r['pales'], 'fecha': r['fecha_doc']}
        for r in con.execute("SELECT numero, destino, pales, fecha_doc FROM albaranes WHERE cliente_id IS NOT NULL "
                             "AND estado NOT IN ('descartado', 'revisar') AND numero IS NOT NULL AND fecha_doc LIKE ? "
                             "ORDER BY numero", (mes + '%',))
        if r['numero'] not in en_factura] if mes else []
    datos['no_escaneados'] = sorted({n for v in viajes for n in v.get('faltan', [])})
    return datos


def en_otras_facturas(con: sqlite3.Connection, numero) -> set:
    """Albaranes que ya están en un viaje de otra factura (un mes puede traer dos: la 8269 y la
    8268 de septiembre, que solo lleva la recogida 1088)."""
    vistos = set()
    for f in con.execute("SELECT numero, datos FROM facturas WHERE estado='lista'"):
        if f['numero'] != numero:
            vistos.update(n for v in json.loads(f['datos'] or '{}').get('viajes', []) for n in v.get('albaranes', []))
    return vistos


def leer_factura_en_segundo_plano(fid: int, pdf: Path) -> None:
    try:
        datos = lector_factura.leer_factura(pdf)
        with db() as con:
            datos = cruzar_factura(datos, con)
            con.execute('UPDATE facturas SET estado=?, numero=?, fecha=?, imp_bruto=?, cuadra=?, datos=? '
                        'WHERE id=?', ('lista', datos['numero'], datos['fecha'], datos['imp_bruto'],
                                       int(bool(datos['cuadra'])), json.dumps(datos), fid))
    except Exception as e:
        with db() as con:
            con.execute('UPDATE facturas SET estado=? WHERE id=?', (f'error: {e}', fid))


@app.route('/facturas', methods=['GET', 'POST'])
@requiere_acceso
def facturas():
    if request.method == 'POST':
        f = request.files.get('factura')
        if not f or not f.filename.lower().endswith('.pdf'):
            flash('Sube la factura en PDF.')
            return redirect(url_for('facturas'))
        carpeta = ARCHIVO / '_facturas'
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = destino_libre(carpeta, Path(f.filename).name)
        f.save(destino)
        with db() as con:
            fid = con.execute('INSERT INTO facturas (fichero, ruta, creado) VALUES (?,?,?)',
                              (Path(f.filename).name, str(destino.relative_to(DATA)),
                               datetime.now().isoformat(timespec='seconds'))).lastrowid
        threading.Thread(target=leer_factura_en_segundo_plano, args=(fid, destino), daemon=True).start()
        return redirect(url_for('factura_ver', fid=fid))
    with db() as con:
        filas = con.execute('SELECT * FROM facturas ORDER BY id DESC').fetchall()
    return render_template('facturas.html', filas=filas)


@app.route('/factura/<int:fid>', methods=['GET', 'POST'])
@requiere_acceso
def factura_ver(fid):
    with db() as con:
        fac = con.execute('SELECT * FROM facturas WHERE id=?', (fid,)).fetchone()
        if not fac:
            return redirect(url_for('facturas'))
        datos = json.loads(fac['datos'] or '{}')
        if request.method == 'POST' and datos.get('viajes'):
            viaje = request.form.get('viaje')
            numero = ''.join(request.form.get('numero', '').split())
            accion = request.form.get('accion')
            for v in datos['viajes']:
                if v['viaje'] != viaje:
                    continue
                if accion == 'anadir' and numero:
                    v.setdefault('anadidos', []).append(numero)
                    v['quitados'] = [n for n in v.get('quitados', []) if n != numero]
                elif accion == 'quitar' and numero:
                    v.setdefault('quitados', []).append(numero)
                    v['anadidos'] = [n for n in v.get('anadidos', []) if n != numero]
            if accion == 'releer_pales':
                numeros = {n for v in datos['viajes'] for n in v.get('sin_pales', [])}
                filas = con.execute(f"SELECT id, ruta FROM albaranes WHERE pales IS NULL AND estado != 'descartado' "
                                    f"AND numero IN ({','.join('?' * len(numeros))})", list(numeros)).fetchall() if numeros else []
                leidos = 0
                for r in filas:
                    valor = procesador.leer_pales(DATA / r['ruta'])
                    if valor:
                        con.execute('UPDATE albaranes SET pales=? WHERE id=?', (valor, r['id']))
                        leidos += 1
                flash(f'Palés leídos en {leidos} de {len(filas)} albaranes.')
                accion = 'recruzar'
            if accion in ('anadir', 'quitar', 'recruzar'):
                datos = cruzar_factura(datos, con)
                con.execute('UPDATE facturas SET datos=? WHERE id=?', (json.dumps(datos), fid))
            return redirect(url_for('factura_ver', fid=fid) + (f'#v{viaje}' if viaje else ''))
        albs = {r['numero']: dict(r) for r in con.execute(
            "SELECT numero, peso_kg, pales, destino, fecha_doc, id FROM albaranes "
            "WHERE estado != 'descartado' AND numero IS NOT NULL")}
    return render_template('factura.html', fac=fac, datos=datos, albs=albs)


MESES = ['ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO', 'JUNIO', 'JULIO', 'AGOSTO', 'SEPTIEMBRE',
         'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']


def excel_de_factura(con: sqlite3.Connection, fac, mes: str | None = None) -> tuple[bytes, str]:
    """El Excel de liquidación de una factura, con su nombre de fichero de siempre. Con varias
    (el mes), una hoja por factura y el nombre de su fichero mensual:
    EXCEL_DELAVIUDA_SEPTIEMBRE_2026.xls."""
    if isinstance(fac, list):
        datos = [json.loads(f['datos']) for f in fac]
        _, nombre = excel_de_factura(con, fac[0])
        if mes and len(fac) > 1:
            nombre = f"EXCEL_DELAVIUDA_{MESES[int(mes[5:7]) - 1]}_{mes[:4]}.xls"
        albs, fijos = _datos_excel(con)
        return exportar.excel(datos, albs, fijos), nombre
    datos = json.loads(fac['datos'])
    albs, fijos = _datos_excel(con)
    nombre = f"EXCEL_DELAVIUDA_{exportar.nombre_hoja(datos.get('numero'), datos.get('fecha')).split('(')[0].strip().replace(' ', '_').replace('º', '')}.xls"
    return exportar.excel(datos, albs, fijos), nombre


def _datos_excel(con: sqlite3.Connection) -> tuple[dict, dict]:
    albs = {r['numero']: {'peso': r['peso_kg'], 'pales': r['pales']} for r in con.execute(
        "SELECT numero, peso_kg, pales FROM albaranes WHERE estado != 'descartado' AND numero IS NOT NULL")}
    fijos = {'cmr': cfg_get(con, 'excel_cmr', 'NO'),
             'codigo': int(cfg_get(con, 'excel_codigo', '20110') or 0) or cfg_get(con, 'excel_codigo'),
             'nombre': cfg_get(con, 'excel_nombre', 'TRANS J. ROMERO S.L.')}
    return albs, fijos


@app.route('/mes/<mes>/excel')
@requiere_acceso
def mes_excel(mes):
    import io
    with db() as con:
        facs = facturas_del_mes(con, mes)
        if not facs:
            return redirect(url_for('mes_en_curso', m=mes))
        contenido, nombre = excel_de_factura(con, facs, mes)
    return send_file(io.BytesIO(contenido), as_attachment=True,
                     download_name=nombre, mimetype='application/vnd.ms-excel')


@app.route('/factura/<int:fid>/excel')
@requiere_acceso
def factura_excel(fid):
    import io
    with db() as con:
        fac = con.execute('SELECT * FROM facturas WHERE id=?', (fid,)).fetchone()
        contenido, nombre = excel_de_factura(con, fac)
    return send_file(io.BytesIO(contenido), as_attachment=True,
                     download_name=nombre, mimetype='application/vnd.ms-excel')


# ------------------------------------------------------------ buzón de entrada
def leer_buzon(solo_probar: bool = False) -> str:
    """Recoge los PDF de los correos no leídos del buzón (los que manda la Konica al escanear)
    y los procesa como si se hubieran subido a mano. Solo PDF; si hay remitentes configurados,
    solo de ellos. Cada correo se marca como leído al recogerlo, así no se procesa dos veces."""
    import email
    import email.utils
    import imaplib
    from email.header import decode_header, make_header
    with db() as con:
        cfg = {c: cfg_get(con, c) for c in CLAVES_IMAP}
    if not (cfg['imap_host'] and cfg['imap_usuario'] and cfg['imap_clave']):
        return 'Falta configurar el buzón (servidor, usuario y contraseña).'
    permitidos = {x.strip().lower() for x in cfg['imap_remitentes'].split(',') if x.strip()}
    recogidos, saltados = [], 0
    try:
        with imaplib.IMAP4_SSL(cfg['imap_host'], 993, timeout=60) as m:
            m.login(cfg['imap_usuario'], cfg['imap_clave'])
            m.select(cfg['imap_carpeta'] or 'INBOX')
            _, datos = m.uid('search', None, 'UNSEEN')
            uids = datos[0].split()
            if solo_probar:
                return f'Buzón correcto: {len(uids)} correo(s) sin leer esperando.'
            for uid in uids:
                _, partes = m.uid('fetch', uid, '(BODY.PEEK[])')
                msg = email.message_from_bytes(partes[0][1])
                remitente = email.utils.parseaddr(msg.get('From', ''))[1].lower()
                if permitidos and remitente not in permitidos:
                    saltados += 1
                    m.uid('store', uid, '+FLAGS', '(\\Seen)')
                    continue
                for parte in msg.walk():
                    nombre = parte.get_filename()
                    if not nombre:
                        continue
                    nombre = str(make_header(decode_header(nombre)))
                    cuerpo = parte.get_payload(decode=True) or b''
                    if not (nombre.lower().endswith('.pdf') or cuerpo[:5] == b'%PDF-') \
                            or len(cuerpo) > 80 * 1024 * 1024:
                        continue
                    ENTRADA.mkdir(parents=True, exist_ok=True)
                    limpio = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(nombre).name) or 'escaneo.pdf'
                    destino = ENTRADA / f'{datetime.now():%Y%m%d-%H%M%S}-correo-{uid.decode()}-{limpio}'
                    destino.write_bytes(cuerpo)
                    recogidos.append((limpio, destino))
                m.uid('store', uid, '+FLAGS', '(\\Seen)')
    except Exception as e:
        texto = f'Error al leer el buzón: {e}'
        with db() as con:
            cfg_set(con, 'imap_estado', f'{datetime.now():%d/%m %H:%M} · {texto}')
        return texto
    for nombre, destino in recogidos:
        with db() as con:
            lote_id = con.execute('INSERT INTO lotes (fichero, recibido) VALUES (?,?)',
                                  (f'{nombre} (por correo)', datetime.now().isoformat(timespec='seconds'))).lastrowid
        threading.Thread(target=procesar_lote, args=(lote_id, destino), daemon=True).start()
    texto = f'{len(recogidos)} escaneo(s) recogido(s)' + (f', {saltados} correo(s) de otros remitentes ignorados' if saltados else '')
    with db() as con:
        cfg_set(con, 'imap_comprobado', f'{datetime.now():%d/%m %H:%M}')
        if recogidos or saltados or 'Error' in cfg_get(con, 'imap_estado'):
            cfg_set(con, 'imap_estado', f'{datetime.now():%d/%m %H:%M} · ' + (texto if recogidos or saltados else 'conexión recuperada'))
    return texto


def vigilar_buzon() -> None:
    """Hilo de fondo: cada BUZON_CADA segundos lee el buzón si está activado. Con varios
    procesos de gunicorn, un candado de fichero hace que solo uno lo lea cada vez."""
    import fcntl
    import time
    while True:
        time.sleep(BUZON_CADA)
        try:
            with db() as con:
                activo = cfg_get(con, 'imap_activo') == '1'
            if not activo:
                continue
            with open(DATA / '.buzon.lock', 'w') as candado:
                try:
                    fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue  # otro proceso lo está leyendo
                leer_buzon()
        except Exception:
            pass  # el error queda en imap_estado; el hilo no se para nunca


@app.route('/salud')
def salud():
    return {'ok': True, 'version': VERSION}


init_db()
if os.environ.get('BUZON_HILO', '1') == '1':
    threading.Thread(target=vigilar_buzon, daemon=True).start()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False)
