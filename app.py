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
import os
import secrets
import smtplib
import sqlite3
import threading
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import (Flask, flash, redirect, render_template, request,
                   send_file, session, url_for)

import procesador

VERSION = '0.9.0'
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


PROHIBIDOS = str.maketrans({c: '-' for c in '/\\:*?"<>|'})


def nombre_fichero(cliente, numero: str, emisor: str, cuando: datetime) -> str:
    """Nombre del PDF según el formato que pide cada cliente (ficha del cliente).
    Variables: {numero} {cliente} {emisor} {fecha} (AAAAMMDD) {año} {mes} {dia}."""
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


def procesar_lote(lote_id: int, pdf: Path) -> None:
    def avance(hechas, total):
        with db() as con:
            con.execute('UPDATE lotes SET hechas=?, paginas=? WHERE id=?', (hechas, total, lote_id))
    try:
        grupos = procesador.procesar_pdf(pdf, avance)
        ahora = datetime.now()
        with db() as con:
            revisar_antes = cfg_get(con, 'revisar_antes') == '1'
            descartados = []  # copias repetidas: solo hace falta una
            for n, g in enumerate(grupos):
                if g.get('duplicadas'):
                    descartados.append(f"{g['numero']} (copia en el mismo escaneo)")
                if g.get('tipo') == 'orden_carga':
                    if g['ref_texto'] and con.execute('SELECT 1 FROM anexos WHERE ref_texto=?',
                                                      (g['ref_texto'],)).fetchone():
                        descartados.append(f"orden de carga {g['ref_texto']} (ya estaba)")
                        continue
                    carpeta = ARCHIVO / '_ordenes-de-carga'
                    carpeta.mkdir(parents=True, exist_ok=True)
                    destino = destino_libre(carpeta, f'orden-lote{lote_id}-{n + 1}.pdf')
                    with destino.open('wb') as f:
                        g['writer'].write(f)
                    con.execute('INSERT INTO anexos (lote_id, ref_texto, refs, ruta, paginas, creado) '
                                'VALUES (?,?,?,?,?,?)',
                                (lote_id, g['ref_texto'], json.dumps(g['refs']),
                                 str(destino.relative_to(DATA)), len(g['paginas']),
                                 ahora.isoformat(timespec='seconds')))
                    continue
                cliente = casar_cliente(g['emisor'], con)
                if g['numero'] and not g['revisar'] and con.execute(
                        'SELECT 1 FROM albaranes WHERE numero=? AND cliente_id IS ?',
                        (g['numero'], cliente['id'] if cliente else None)).fetchone():
                    descartados.append(f"{g['numero']} (ya estaba)")
                    continue
                fecha_doc = fecha_valida(g.get('fecha'))
                cuando = fecha_doc or ahora  # año/mes de la carpeta: los del albarán
                carpeta = carpeta_de(cliente, g['emisor'], cuando)
                if g['revisar']:
                    carpeta = ARCHIVO / 'REVISAR'
                carpeta.mkdir(parents=True, exist_ok=True)
                if g['numero'] and not g['revisar']:
                    nombre = nombre_fichero(cliente, g['numero'], g['emisor'], cuando)
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
                    ' confianza, ruta, estado, creado, fecha_doc) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (lote_id, cliente['id'] if cliente else None, g['numero'], g['emisor'],
                     len(g['paginas']), g['confianza'], str(destino.relative_to(DATA)),
                     estado, ahora.isoformat(timespec='seconds'),
                     f'{fecha_doc:%Y-%m-%d}' if fecha_doc else None))
                if estado == 'pendiente_entrega' and cliente['auto_entrega']:
                    resultado = entregar(cur.lastrowid, con)
                    if resultado != 'ok':
                        con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?',
                                    (resultado, cur.lastrowid))
            # las órdenes de carga se pegan a su albarán, venga en este escaneo o en otro
            unir_anexos(con)
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
    adjuntos = ', '.join(x for x in (albaran['adjuntos'], 'orden de carga') if x)
    con.execute('UPDATE albaranes SET paginas=paginas+?, adjuntos=? WHERE id=?',
                (anexo['paginas'], adjuntos, albaran['id']))
    con.execute('UPDATE anexos SET unido_a=? WHERE id=?', (albaran['id'], anexo['id']))


def unir_anexos(con: sqlite3.Connection) -> None:
    """Pega cada orden de carga suelta al albarán que cita (el primero del rango), si ya
    existe y no se ha entregado."""
    for anexo in con.execute('SELECT * FROM anexos WHERE unido_a IS NULL').fetchall():
        refs = json.loads(anexo['refs'] or '[]')
        if not refs:
            continue
        destinos = con.execute(
            f"SELECT * FROM albaranes WHERE numero IN ({','.join('?' * len(refs))}) "
            "AND estado != 'entregado' ORDER BY numero", refs).fetchall()
        for alb in destinos:
            pegar_anexo(anexo, alb, con)


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
@app.route('/')
@requiere_acceso
def inicio():
    """Pantalla de entrada tipo Odoo: un acceso por área, con su aviso si hay algo pendiente."""
    with db() as con:
        n = dict(con.execute('SELECT estado, COUNT(*) FROM albaranes GROUP BY estado').fetchall())
        leyendo = con.execute("SELECT COUNT(*) FROM lotes WHERE estado='procesando'").fetchone()[0]
        n_clientes = con.execute('SELECT COUNT(*) FROM clientes').fetchone()[0]
        n['revisar'] = n.get('revisar', 0) + con.execute(
            'SELECT COUNT(*) FROM anexos WHERE unido_a IS NULL').fetchone()[0]
    accesos = [
        {'url': url_for('index'), 'nombre': 'Subir escaneo', 'icono': 'subir', 'color': 'coral',
         'aviso': f'{leyendo} leyéndose' if leyendo else ''},
        {'url': url_for('revision'), 'nombre': 'Revisión', 'icono': 'revision', 'color': 'azul',
         'aviso': (f"{n.get('colocado', 0) + n.get('revisar', 0)} por revisar"
                   if n.get('colocado', 0) + n.get('revisar', 0) else '')},
        {'url': url_for('albaranes'), 'nombre': 'Archivo', 'icono': 'archivo', 'color': 'ambar',
         'aviso': ''},
        {'url': url_for('pendientes'), 'nombre': 'Por entregar', 'icono': 'entregar', 'color': 'violeta',
         'aviso': f"{n.get('pendiente_entrega', 0)} sin entregar" if n.get('pendiente_entrega') else ''},
        {'url': url_for('clientes'), 'nombre': 'Clientes', 'icono': 'clientes', 'color': 'verde',
         'aviso': ''},
        {'url': url_for('ajustes'), 'nombre': 'Ajustes', 'icono': 'ajustes', 'color': 'tinta',
         'aviso': ''},
    ]
    accesos = [a for a in accesos if a['nombre'] != 'Por entregar' or n.get('pendiente_entrega')]
    return render_template('inicio.html', accesos=accesos, n_clientes=n_clientes)


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
            ordenes.append({**dict(o), 'refs_l': refs, 'rango': rango,
                            'entregados': [r['numero'] for r in ya]})
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
    return render_template('revision.html', en_curso=en_curso, grupos=grupos, ordenes=ordenes,
                           con_repetidos=con_repetidos,
                           dudosos=dudosos, clientes=clientes, revisar_antes=revisar_antes,
                           n_colocados=sum(len(g['albaranes']) for g in grupos.values()))


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
        cuando = fecha_nueva or fecha_de(a)
        carpeta = carpeta_de(cliente, a['emisor'], cuando)
        carpeta.mkdir(parents=True, exist_ok=True)
        origen = DATA / a['ruta']
        nombre = nombre_fichero(cliente, numero, a['emisor'], cuando)
        if (carpeta / nombre).exists() and carpeta / nombre != origen:
            flash(f'Ya hay un albarán con el nombre {nombre} en {carpeta.relative_to(ARCHIVO)}.')
            return redirect(url_for('revision'))
        destino = carpeta / nombre
        origen.rename(destino)
        con.execute("UPDATE albaranes SET numero=?, cliente_id=?, ruta=?, confianza='corregido', "
                    "estado='colocado', entrega_nota='', fecha_doc=? WHERE id=?",
                    (numero, cliente['id'] if cliente else None, str(destino.relative_to(DATA)),
                     f'{fecha_nueva:%Y-%m-%d}' if fecha_nueva else a['fecha_doc'], aid))
    flash(f'Albarán {numero} colocado.')
    return redirect(url_for('revision'))


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
        return {'REVISAR': 'Para revisar'}.get(slug, nombres_cliente.get(slug, slug))
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
        hay_clave = bool(cfg_get(con, 'clave_hash'))
        revisar_antes = cfg_get(con, 'revisar_antes') == '1'
        repaso = json.loads(cfg_get(con, 'repaso') or '{}')
    return render_template('ajustes.html', repaso=repaso, smtp=smtp, hay_clave=hay_clave, version=VERSION,
                           revisar_antes=revisar_antes)


def repasar_todo() -> None:
    """Relee todos los albaranes ya subidos con el lector actual (número con triple lectura y
    fecha de carga): recoloca en su año/mes los que estén mal y manda a revisar los números
    que no se confirmen. Lo entregado no se mueve: solo se informa."""
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    with db() as con:
        filas = [dict(r) for r in con.execute(
            'SELECT a.*, c.nombre AS cliente FROM albaranes a LEFT JOIN clientes c '
            'ON c.id=a.cliente_id ORDER BY a.id')]
        cfg_set(con, 'repaso', json.dumps({'estado': 'leyendo', 'hechas': 0, 'total': len(filas),
                                           'inicio': datetime.now().isoformat(timespec='seconds')}))

    def leer(a):
        pdf = DATA / a['ruta']
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['pdftoppm', '-r', str(procesador.DPI), '-png', '-singlefile', '-f', '1',
                            '-l', '1', str(pdf), f'{tmp}/p'], check=True)
            return procesador._analizar_pagina(Path(f'{tmp}/p.png'), pdf, 0)
    with ThreadPoolExecutor(max_workers=procesador.LECTURAS_A_LA_VEZ) as ex:
        lecturas = list(ex.map(leer, filas))

    cambios = []
    with db() as con:
        for a, info in zip(filas, lecturas):
            nombre_actual = Path(a['ruta']).name
            seguro = info.get('numero') and info.get('confianza') != 'baja'
            fecha = fecha_valida(info.get('fecha'))
            if not seguro or info['numero'] != a['numero']:
                visto = info.get('numero') or 'ilegible'
                if a['estado'] == 'entregado':
                    cambios.append(f"⚠️ {nombre_actual}: ya ENTREGADO, la relectura no confirma el "
                                   f"número (lee {visto})")
                elif a['estado'] != 'revisar':
                    con.execute("UPDATE albaranes SET estado='revisar', entrega_nota=? WHERE id=?",
                                (f'el repaso lee {visto}', a['id']))
                    cambios.append(f"🔎 {nombre_actual}: a revisar (la relectura lee {visto})")
                continue
            if not fecha or a['estado'] == 'revisar':
                continue
            if a['fecha_doc'] == f'{fecha:%Y-%m-%d}' and f'/{fecha:%Y}/{fecha:%m}/' in a['ruta']:
                continue
            if a['estado'] == 'entregado':
                con.execute('UPDATE albaranes SET fecha_doc=? WHERE id=?', (f'{fecha:%Y-%m-%d}', a['id']))
                cambios.append(f"ℹ️ {nombre_actual}: ya entregado; su fecha de carga es "
                               f"{fecha:%d/%m/%Y} (no se mueve)")
                continue
            cliente = con.execute('SELECT * FROM clientes WHERE id=?', (a['cliente_id'],)).fetchone() \
                if a['cliente_id'] else None
            carpeta = carpeta_de(cliente, a['emisor'], fecha)
            carpeta.mkdir(parents=True, exist_ok=True)
            destino = destino_libre(carpeta, nombre_fichero(cliente, a['numero'], a['emisor'], fecha),
                                    DATA / a['ruta'])
            (DATA / a['ruta']).rename(destino)
            con.execute('UPDATE albaranes SET fecha_doc=?, ruta=? WHERE id=?',
                        (f'{fecha:%Y-%m-%d}', str(destino.relative_to(DATA)), a['id']))
            cambios.append(f"📁 {nombre_actual}: de {Path(a['ruta']).parent.relative_to('archivo')}/ "
                           f"a {destino.parent.relative_to(ARCHIVO)}/ (carga {fecha:%d/%m/%Y})")
        cfg_set(con, 'repaso', json.dumps({'estado': 'hecho', 'total': len(filas), 'cambios': cambios,
                                           'fin': datetime.now().isoformat(timespec='seconds')}))


@app.route('/ajustes/repasar', methods=['POST'])
@requiere_acceso
def ajustes_repasar():
    threading.Thread(target=repasar_todo, daemon=True).start()
    flash('Repaso en marcha: se están releyendo todos los albaranes. El resultado sale aquí.')
    return redirect(url_for('ajustes') + '#repaso')


@app.route('/salud')
def salud():
    return {'ok': True, 'version': VERSION}


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False)
