"""Listado de viajes exportado del programa del transportista (Kaleido) en Excel o CSV.

Sin orden de carga la app no sabe qué viajes ha habido; el listado sí. Al subirlo, cada viaje
se cruza con lo escaneado (igual que una factura) y sale qué viaje no tiene papeles o le faltan
albaranes, ANTES de facturar. Petición de Mario 05-11-2026: «prepara para que lo exporte por
Excel y lo podamos subir».

No sabemos aún cómo es el export de Kaleido, así que el lector no depende de un formato fijo:
busca la fila de cabecera y reconoce las columnas por su nombre; si no reconoce las esenciales
(viaje y fecha), se lo pregunta al modelo con las primeras filas. Una fila por viaje o una por
albarán (como su Excel de liquidación): las filas del mismo viaje se juntan."""
import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import procesador

# nombre de columna (normalizado) → campo; el primero que case gana
SINONIMOS = {
    'viaje': ['n-viaje', 'no-viaje', 'num-viaje', 'numero-viaje', 'viaje', 'n-servicio', 'servicio',
              'expedicion', 'n-expedicion', 'n-orden', 'orden'],
    'fecha': ['fecha-carga', 'f-carga', 'fecha-de-carga', 'fecha-salida', 'fecha-servicio', 'fecha'],
    'cliente': ['cliente', 'nombre-cliente', 'razon-social', 'cargador'],
    'origen': ['origen', 'poblacion-origen', 'lugar-carga', 'carga'],
    'destino': ['destino', 'poblacion-destino', 'lugar-descarga', 'descarga', 'routa', 'ruta'],
    'refs': ['n-albaran-dlv', 'n-albaran', 'albaran', 'albaranes', 'referencia', 'referencias', 'ref',
             'observaciones'],
    'pales': ['n-de-pales', 'pales', 'palets', 'n-pales', 'bultos', 'cantidad'],
    'importe': ['euros', 'importe', 'precio', 'total', 'base'],
    'matricula': ['matricula', 'vehiculo', 'tractora'],
}

PROMPT = """Estas son las primeras filas de un Excel exportado del programa de una empresa de
transporte con su lista de viajes (o servicios). Cada fila va numerada (fila: celdas separadas por |).
{filas}

Devuelve SOLO un JSON: {{"cabecera": número de la fila de cabecera (o null si no hay),
"columnas": {{"viaje": índice de columna (0 = primera) del número de viaje/servicio, "fecha": de la fecha de
carga o del servicio, "cliente": ..., "origen": ..., "destino": ..., "refs": columna con números de albarán o
referencias, "pales": ..., "importe": ..., "matricula": ...}}}} — pon null en lo que no exista."""


def _celdas_xlsx(datos: bytes) -> list[list]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(datos), read_only=True, data_only=True)
    hojas = [[list(f) for f in h.iter_rows(values_only=True)] for h in wb.worksheets]
    return max(hojas, key=len) if hojas else []


def _celdas_xls(datos: bytes) -> list[list]:
    import xlrd
    wb = xlrd.open_workbook(file_contents=datos)
    hojas = []
    for h in wb.sheets():
        filas = []
        for i in range(h.nrows):
            fila = []
            for j, c in enumerate(h.row(i)):
                if c.ctype == xlrd.XL_CELL_DATE:
                    fila.append(xlrd.xldate.xldate_as_datetime(c.value, wb.datemode))
                else:
                    fila.append(c.value)
            filas.append(fila)
        hojas.append(filas)
    return max(hojas, key=len) if hojas else []


def _celdas_csv(datos: bytes) -> list[list]:
    for cod in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texto = datos.decode(cod)
            break
        except UnicodeDecodeError:
            continue
    dialecto = csv.Sniffer().sniff(texto[:5000], delimiters=';,\t|') if texto.strip() else csv.excel
    return [list(f) for f in csv.reader(io.StringIO(texto), dialecto)]


def celdas(nombre: str, datos: bytes) -> list[list]:
    ext = Path(nombre).suffix.lower()
    if ext == '.xlsx' or datos[:2] == b'PK':
        return _celdas_xlsx(datos)
    if ext == '.xls' or datos[:4] == b'\xd0\xcf\x11\xe0':
        return _celdas_xls(datos)
    return _celdas_csv(datos)


def _texto(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _cabecera(filas: list[list]):
    """(índice de la fila de cabecera, {campo: columna}) por nombres; la fila que más casa."""
    mejor = (None, {})
    for i, fila in enumerate(filas[:30]):
        nombres = [procesador.normaliza(_texto(c).replace('º', '').replace('°', '')) for c in fila]
        nombres = [re.sub(r'^(n|no|num|numero)-?', lambda m: 'n-', n) if n.startswith(('n-', 'no-', 'num')) else n
                   for n in nombres]
        mapa = {}
        for campo, sinon in SINONIMOS.items():
            for s in sinon:
                cols = [j for j, n in enumerate(nombres) if n and (
                    n == s or n.startswith(s + '-') or n.endswith('-' + s) or (len(s) >= 5 and s in n))]
                cols = [j for j in cols if j not in mapa.values()]
                if cols:
                    mapa[campo] = cols[0]
                    break
        if len(mapa) > len(mejor[1]):
            mejor = (i, mapa)
    return mejor


def _preguntar(filas: list[list]):
    muestra = '\n'.join(f'{i}: ' + ' | '.join(_texto(c) for c in f) for i, f in enumerate(filas[:15]))
    with procesador._LECTURAS:
        resp = procesador._api().messages.create(
            model='claude-sonnet-5', max_tokens=2000,
            messages=[{'role': 'user', 'content': PROMPT.format(filas=muestra)}])
    salida = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
    m = re.search(r'\{.*\}', salida, re.S)
    d = json.loads(m.group(0)) if m else {}
    cols = {k: v for k, v in (d.get('columnas') or {}).items() if isinstance(v, int)}
    return d.get('cabecera'), cols


def fecha_de(v) -> str | None:
    if isinstance(v, datetime):
        return f'{v:%Y-%m-%d}'
    if isinstance(v, date):
        return f'{v:%Y-%m-%d}'
    if isinstance(v, (int, float)) and 30000 < v < 60000:  # número de serie de Excel
        return f'{datetime(1899, 12, 30) + timedelta(days=float(v)):%Y-%m-%d}'
    t = _texto(v)
    m = re.search(r'(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})', t)
    if m:
        d, mes, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = a + 2000 if a < 100 else a
        try:
            return f'{date(a, mes, d):%Y-%m-%d}'
        except ValueError:
            return None
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', t)
    return m.group(0) if m else None


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    t = _texto(v).replace('€', '').replace(' ', '')
    if not t:
        return None
    if ',' in t:
        t = t.replace('.', '').replace(',', '.')
    try:
        return float(t)
    except ValueError:
        return None


def leer(nombre: str, datos: bytes, preguntar: bool = True) -> dict:
    """→ {'viajes': [...], 'columnas': {campo: nombre de columna}, 'filas': n, 'aviso': texto}"""
    filas = [f for f in celdas(nombre, datos) if any(_texto(c) for c in f)]
    if not filas:
        return {'viajes': [], 'columnas': {}, 'filas': 0, 'aviso': 'El fichero está vacío.'}
    cab, cols = _cabecera(filas)
    if (('viaje' not in cols or 'fecha' not in cols) and preguntar):
        cab, cols = _preguntar(filas)
    if 'viaje' not in cols and 'fecha' not in cols:
        return {'viajes': [], 'columnas': {}, 'filas': len(filas),
                'aviso': 'No he encontrado en el fichero la columna del número de viaje ni la de la fecha.'}
    nombres = [_texto(c) for c in filas[cab]] if cab is not None else []
    cuerpo = filas[cab + 1:] if cab is not None else filas
    viajes, orden = {}, []
    ultimo = None
    for f in cuerpo:
        def c(campo):
            j = cols.get(campo)
            return f[j] if j is not None and j < len(f) else None
        num = _texto(c('viaje')) or ultimo  # filas de continuación del mismo viaje sin el número
        fecha = fecha_de(c('fecha'))
        if not num and not fecha:
            continue
        if not num:  # sin número de viaje: cada fila es un viaje (fecha + destino)
            num = f"{fecha}·{_texto(c('destino'))}"
        refs = re.findall(r'\b\d{8}\b', _texto(c('refs')))
        v = viajes.get(num)
        if v is None:
            v = viajes[num] = {'viaje': num, 'fecha': fecha, 'cliente': _texto(c('cliente')),
                               'origen': _texto(c('origen')).upper(), 'destino': _texto(c('destino')).upper(),
                               'refs': [], 'pales': None, 'importe': None, 'matricula': _texto(c('matricula'))}
            orden.append(num)
        v['fecha'] = v['fecha'] or fecha
        v['destino'] = v['destino'] or _texto(c('destino')).upper()
        v['refs'] += [r for r in refs if r not in v['refs']]
        p, imp = _num(c('pales')), _num(c('importe'))
        if p:
            v['pales'] = (v['pales'] or 0) + p
        if imp:
            v['importe'] = (v['importe'] or 0) + imp
        ultimo = num if _texto(c('viaje')) else ultimo
    lista = [viajes[n] for n in orden if viajes[n]['fecha']]
    return {'viajes': lista, 'filas': len(cuerpo),
            'columnas': {k: (nombres[j] if j < len(nombres) else f'columna {j + 1}') for k, j in cols.items()},
            'aviso': '' if lista else 'He leído el fichero pero ninguna fila trae una fecha que entienda.'}


def como_factura(viajes: list[dict], mes: str) -> dict:
    """El listado en la forma de una factura leída, para cruzarlo igual con lo escaneado."""
    salida = []
    for v in viajes:
        if not (v['fecha'] or '').startswith(mes):
            continue
        origen = v['origen'] or 'SONSECA'
        desc = f"{origen} -> {v['destino']}" + (f" Ref: {' / '.join(v['refs'])}" if v['refs'] else '')
        salida.append({'viaje': v['viaje'], 'fecha': v['fecha'], 'descripcion': desc,
                       'destino': v['destino'] or '?', 'refs': list(v['refs']),
                       'cantidad': v['pales'] or 0, 'importe': v['importe'] or 0, 'extras': [],
                       'cliente': v['cliente'], 'matricula': v['matricula']})
    return {'numero': 'listado', 'fecha': f'{mes}-28', 'viajes': salida}
