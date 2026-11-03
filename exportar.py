"""Excel de liquidación para Delaviuda: la relación de albaranes de cada factura, con el mismo
formato que el que preparaba a mano Trans J. Romero (EXCEL_DELAVIUDA_SEPTIEMBRE_2026.xls):
una hoja por factura, un bloque por viaje (sus albaranes con peso y palés, el precio del viaje
en la primera fila y sus extras), una fila en blanco entre viajes y, al final, la relación."""
import io
import re
import unicodedata

import xlwt

CABECERA = ['VIAJE', 'ROUTA', 'N°ALBARAN-DLV', 'PESO (KG)', 'Nº DE PALES', 'EUROS',
            'OBSERVACIONES CMR', 'CODIGO-TRANSPORTISTA', 'NOMBRE-TRANSPORTISTA']
ANCHOS = [10, 65, 38, 11, 11, 11, 20, 23, 24]


def sin_tildes(t: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', t or '') if not unicodedata.combining(c))


def origen_de(descripcion: str) -> str:
    """«SANTA PERPETUA DE LA MOGODA -> SONSECA Ref: …» → 'SANTA PERPETUA DE LA MOGODA'."""
    d = descripcion or ''
    return sin_tildes(d.split('->', 1)[0]).strip(' .').upper() if '->' in d else ''


def es_recogida(v: dict) -> bool:
    """Viaje de vuelta: se recoge mercancía en otro sitio y se lleva a Sonseca (la fábrica del
    cliente), p. ej. el 1088 SANTA PERPETUA DE LA MOGODA → SONSECA (Fuji Oil, factura 8268)."""
    origen = origen_de(v.get('descripcion', ''))
    return sin_tildes(v.get('destino', '')).upper() == 'SONSECA' and origen not in ('', 'SONSECA')


def es_linea_suelta(v: dict) -> bool:
    """Líneas de factura que no son un viaje con mercancía: combustible, gestión documental…
    (SONSECA -> SONSECA Ref: INCREMENTO INDEXACION…). Una recogida que viene a Sonseca, no."""
    return v['destino'] in ('SONSECA', '') and not v.get('albaranes') and 'Ref:' in v['descripcion'] \
        and not re.search(r'Ref:\s*\d{8}', v['descripcion']) and not es_recogida(v)


def nombre_hoja(numero: str, fecha: str) -> str:
    n = (numero or '').split('/')[-1].strip()
    f = '-'.join(reversed((fecha or '').split('-')))
    return f'FACTURA Nº {n} ({f})'[:31]


def excel(factura, datos_albaran: dict, fijos: dict) -> bytes:
    """`factura`: una o varias (lista): una hoja por factura, como su fichero del mes, que trae
    la 8269 y la 8268 de septiembre. `datos_albaran`: número → {'peso', 'pales'};
    `fijos`: cmr, codigo, nombre."""
    wb = xlwt.Workbook(encoding='utf-8')
    for f in (factura if isinstance(factura, list) else [factura]):
        _hoja(wb, f, datos_albaran, fijos)
    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()


def _hoja(wb, factura: dict, datos_albaran: dict, fijos: dict) -> None:
    hoja = wb.add_sheet(nombre_hoja(factura.get('numero'), factura.get('fecha')))
    fuente = 'font: name Calibri, height 220;'
    centro = 'align: horiz center, vert center;'
    st_cab = xlwt.easyxf(fuente + centro + 'pattern: pattern solid, fore_colour orange;')
    st = xlwt.easyxf(fuente + centro)
    st_kg = xlwt.easyxf(fuente + centro, num_format_str='0.00')
    st_eur = xlwt.easyxf(fuente + centro,
                         num_format_str='_-* #,##0.00\\ "€"_-;\\-* #,##0.00\\ "€"_-;_-* "-"??\\ "€"_-;_-@_-')
    st_neg = xlwt.easyxf('font: name Calibri, height 220, bold on;')
    for c, (t, w) in enumerate(zip(CABECERA, ANCHOS)):
        hoja.write(0, c, t, st_cab)
        hoja.col(c).width = 256 * w
    hoja.row(0).height_mismatch = True
    hoja.row(0).height = 300
    fila = 1
    fijos_fila = [fijos.get('cmr', 'NO'), fijos.get('codigo', 20110), fijos.get('nombre', 'TRANS J. ROMERO S.L.')]

    def escribe(valores, eur_col=True):
        nonlocal fila
        for c, v in enumerate(valores):
            if v in (None, ''):
                continue
            estilo = st_kg if c == 3 else st_eur if (c == 5 and eur_col) else st
            hoja.write(fila, c, v, estilo)
        fila += 1

    for v in factura['viajes']:
        viaje = int(v['viaje']) if str(v['viaje']).isdigit() else v['viaje']
        destino = sin_tildes(v['destino']).upper()
        if es_linea_suelta(v):
            concepto = v['descripcion'].split('Ref:', 1)[1].strip()
            escribe([viaje, concepto, None, None, v.get('cantidad') or 1, v['importe']])
        elif es_recogida(v):
            # como en su Excel: «ORIGEN-SONSECA», sin número de albarán DLV (el de la recogida
            # es del proveedor) ni palés
            escribe([viaje, f"{origen_de(v['descripcion'])}-{destino}", None, None, None,
                     v['importe'], *fijos_fila])
            for e in v['extras']:
                escribe([viaje, e['concepto'], None, None, None, e['importe'], *fijos_fila])
        else:
            albaranes = v.get('albaranes') or []
            if not albaranes:  # viaje sin albaranes identificados: una fila con los palés de la factura
                escribe([viaje, destino, None, None, v.get('cantidad'), v['importe'], *fijos_fila])
            for i, n in enumerate(albaranes):
                d = datos_albaran.get(n, {})
                escribe([viaje, destino, int(n) if n.isdigit() else n, d.get('peso'), d.get('pales'),
                         v['importe'] if i == 0 else None, *fijos_fila])
            for e in v['extras']:
                escribe([viaje, e['concepto'], None, None, None, e['importe'], *fijos_fila])
        fila += 1  # una fila en blanco entre viajes
    fila += 1
    hoja.write(fila, 0, f"RELACIÓN DE ALBARANES CORRESPONDIENTES A "
                        f"{nombre_hoja(factura.get('numero'), factura.get('fecha'))}", st_neg)
