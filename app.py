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

VERSION = '0.5.1'
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


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        with db() as con:
            guardado = cfg_get(con, 'clave_hash')
        if guardado and verifica_clave(request.form.get('clave', ''), guardado):
            session.permanent = True
            session['acceso'] = True
            return redirect(request.args.get('siguiente') or url_for('index'))
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
            for g in grupos:
                cliente = casar_cliente(g['emisor'], con)
                carpeta = carpeta_de(cliente, g['emisor'], ahora)
                if g['revisar']:
                    carpeta = ARCHIVO / 'REVISAR'
                carpeta.mkdir(parents=True, exist_ok=True)
                nombre = f"{g['numero'] or 'sin-numero-lote%d-p%d' % (lote_id, g['paginas'][0]+1)}.pdf"
                destino = carpeta / nombre
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
                    ' confianza, ruta, estado, creado) VALUES (?,?,?,?,?,?,?,?,?)',
                    (lote_id, cliente['id'] if cliente else None, g['numero'], g['emisor'],
                     len(g['paginas']), g['confianza'], str(destino.relative_to(DATA)),
                     estado, ahora.isoformat(timespec='seconds')))
                if estado == 'pendiente_entrega' and cliente['auto_entrega']:
                    resultado = entregar(cur.lastrowid, con)
                    if resultado != 'ok':
                        con.execute('UPDATE albaranes SET entrega_nota=? WHERE id=?',
                                    (resultado, cur.lastrowid))
            con.execute('UPDATE lotes SET estado=?, paginas=? WHERE id=?',
                        ('procesado', sum(len(g['paginas']) for g in grupos), lote_id))
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
        resultado = f"subir a mano al portal: {a['portal_url'] or '(URL sin configurar)'}"
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
                       subtype='pdf', filename=f"{a['numero']}.pdf")
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
        filas = con.execute(
            "SELECT a.*, c.nombre AS cliente, c.modo_entrega, c.email_para, c.portal_url, "
            "c.sftp_datos FROM albaranes a LEFT JOIN clientes c ON c.id=a.cliente_id "
            "WHERE a.estado IN ('colocado','revisar') ORDER BY c.nombre, a.numero").fetchall()
        clientes = con.execute('SELECT id, nombre FROM clientes ORDER BY nombre').fetchall()
        revisar_antes = cfg_get(con, 'revisar_antes') == '1'
    grupos = {}  # carpeta → {cliente, destino, albaranes}
    for a in filas:
        if a['estado'] != 'colocado':
            continue
        carpeta = str(Path(a['ruta']).parent.relative_to('archivo')) + '/'
        g = grupos.setdefault(carpeta, {'cliente': a['cliente'], 'cliente_id': a['cliente_id'],
                                        'destino': destino_texto(a), 'albaranes': []})
        g['albaranes'].append(a)
    dudosos = [a for a in filas if a['estado'] == 'revisar']
    return render_template('revision.html', en_curso=en_curso, grupos=grupos,
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
    otros = len(resultados) - ok - archivados
    partes = [f'{ok} enviado(s) a su cliente'] if ok else []
    if archivados:
        partes.append(f'{archivados} solo archivado(s)')
    if otros:
        partes.append(f'{otros} pendiente(s) de entrega (ver Panel)')
    flash('Hecho: ' + ', '.join(partes) + '.' if partes else 'No había nada colocado.')
    return redirect(url_for('revision'))


@app.route('/albaran/<int:aid>/corregir', methods=['POST'])
@requiere_acceso
def albaran_corregir(aid):
    """Corrige número y/o cliente de un albarán y lo vuelve a colocar en su carpeta."""
    numero = ''.join(request.form.get('numero', '').split())
    cid = request.form.get('cliente_id') or None
    if not numero:
        flash('Pon el número del albarán.')
        return redirect(url_for('revision'))
    with db() as con:
        a = con.execute('SELECT * FROM albaranes WHERE id=?', (aid,)).fetchone()
        cliente = con.execute('SELECT * FROM clientes WHERE id=?', (cid,)).fetchone() if cid else None
        carpeta = carpeta_de(cliente, a['emisor'], datetime.fromisoformat(a['creado']))
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f'{numero}.pdf'
        origen = DATA / a['ruta']
        if destino.exists() and destino != origen:
            flash(f'Ya hay un albarán {numero} en {destino.parent.relative_to(ARCHIVO)}.')
            return redirect(url_for('revision'))
        origen.rename(destino)
        con.execute("UPDATE albaranes SET numero=?, cliente_id=?, ruta=?, confianza='corregido', "
                    "estado='colocado', entrega_nota='' WHERE id=?",
                    (numero, cliente['id'] if cliente else None,
                     str(destino.relative_to(DATA)), aid))
    flash(f'Albarán {numero} colocado.')
    return redirect(url_for('revision'))


@app.route('/albaran/<int:aid>/miniatura')
@requiere_acceso
def albaran_miniatura(aid):
    with db() as con:
        a = con.execute('SELECT ruta, creado FROM albaranes WHERE id=?', (aid,)).fetchone()
    pdf = DATA / a['ruta']
    # clave por ruta + fecha: SQLite reutiliza ids cuando se vacía la tabla
    clave = hashlib.sha1(f"{a['ruta']}|{a['creado']}|{pdf.stat().st_mtime}".encode()).hexdigest()
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
            'SELECT a.*, c.nombre AS cliente FROM albaranes a '
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
    return send_file(DATA / a['ruta'], download_name=f"{a['numero']}.pdf")


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
        for a in con.execute('SELECT ruta FROM albaranes WHERE lote_id=?', (lid,)):
            (DATA / a['ruta']).unlink(missing_ok=True)
        con.execute('DELETE FROM albaranes WHERE lote_id=?', (lid,))
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
                       'sftp_datos', 'portal_url', 'notas')}
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
    return render_template('ajustes.html', smtp=smtp, hay_clave=hay_clave, version=VERSION,
                           revisar_antes=revisar_antes)


@app.route('/salud')
def salud():
    return {'ok': True, 'version': VERSION}


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False)
