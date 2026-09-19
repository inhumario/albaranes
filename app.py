#!/usr/bin/env python3
"""Aplicativo de albaranes — web con contraseña (v0.3, 2026-09-19).

Flujo (regla de Mario: «subir y estar»): se sube el PDF de la pila escaneada →
el procesador separa y lee cada albarán → se archiva en
archivo/<cliente>/<año>/<mes>/<numero>.pdf → se entrega solo según el protocolo
configurado de cada cliente (email automático, SFTP, portal). Todos los
parámetros (clientes y comunicaciones) se ajustan desde la propia web.
"""
import hashlib
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

VERSION = '0.3.0'
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
            estado TEXT NOT NULL DEFAULT 'archivado', -- archivado | pendiente_entrega | entregado | revisar
            entrega_nota TEXT DEFAULT '',             -- por qué quedó pendiente (o instrucción de portal)
            entregado_el TEXT,
            creado TEXT NOT NULL
        );''')
        # migración v0.1 → v0.2
        try:
            con.execute("ALTER TABLE albaranes ADD COLUMN entrega_nota TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # contraseña inicial: de la variable ACCESO_CLAVE la primera vez
        if not cfg_get(con, 'clave_hash') and os.environ.get('ACCESO_CLAVE'):
            cfg_set(con, 'clave_hash', hash_clave(os.environ['ACCESO_CLAVE']))
        # migración desde config/smtp.json si existe y aún no hay SMTP en BD
        legado = BASE / 'config' / 'smtp.json'
        if legado.exists() and not cfg_get(con, 'smtp_host'):
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


def procesar_lote(lote_id: int, pdf: Path) -> None:
    try:
        grupos = procesador.procesar_pdf(pdf)
        ahora = datetime.now()
        with db() as con:
            for g in grupos:
                cliente = casar_cliente(g['emisor'], con)
                carpeta_cliente = cliente['nombre'] if cliente else (g['emisor'] or 'sin-clasificar')
                carpeta = (ARCHIVO / procesador.normaliza(carpeta_cliente)
                           / f'{ahora:%Y}' / f'{ahora:%m}')
                if g['revisar']:
                    carpeta = ARCHIVO / 'REVISAR'
                carpeta.mkdir(parents=True, exist_ok=True)
                nombre = f"{g['numero'] or 'sin-numero-lote%d-p%d' % (lote_id, g['paginas'][0]+1)}.pdf"
                destino = carpeta / nombre
                with destino.open('wb') as f:
                    g['writer'].write(f)

                if g['revisar']:
                    estado = 'revisar'
                elif cliente and cliente['modo_entrega'] != 'ninguna':
                    estado = 'pendiente_entrega'
                else:
                    estado = 'archivado'
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
    f = request.files.get('escaneo')
    if not f or not f.filename.lower().endswith('.pdf'):
        flash('Sube un PDF (la pila escaneada entera).')
        return redirect(url_for('index'))
    ENTRADA.mkdir(parents=True, exist_ok=True)
    destino = ENTRADA / f'{datetime.now():%Y%m%d-%H%M%S}-{Path(f.filename).name}'
    f.save(destino)
    with db() as con:
        cur = con.execute('INSERT INTO lotes (fichero, recibido) VALUES (?,?)',
                          (destino.name, datetime.now().isoformat(timespec='seconds')))
        lote_id = cur.lastrowid
    threading.Thread(target=procesar_lote, args=(lote_id, destino), daemon=True).start()
    flash('Escaneo recibido: procesando en segundo plano. Recarga en unos segundos.')
    return redirect(url_for('index'))


@app.route('/albaranes')
@requiere_acceso
def albaranes():
    q = request.args.get('q', '').strip()
    sql = ('SELECT a.*, c.nombre AS cliente FROM albaranes a '
           'LEFT JOIN clientes c ON c.id=a.cliente_id')
    args = []
    if q:
        sql += ' WHERE a.numero LIKE ? OR c.nombre LIKE ? OR a.emisor LIKE ?'
        args = [f'%{q}%'] * 3
    sql += ' ORDER BY a.id DESC LIMIT 500'
    with db() as con:
        filas = con.execute(sql, args).fetchall()
    return render_template('albaranes.html', filas=filas, q=q)


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
    return render_template('ajustes.html', smtp=smtp, hay_clave=hay_clave, version=VERSION)


@app.route('/salud')
def salud():
    return {'ok': True, 'version': VERSION}


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False)
