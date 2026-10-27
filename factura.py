"""Lectura de las facturas de transporte (Trans J. Romero → Delaviuda) para la exportación
a Excel. Cada línea con fecha es un VIAJE (su «Nº albarán» es el número de viaje del
transportista) con su ruta, las referencias de albaranes Delaviuda que cita, los palés
(cantidad) y el importe; las líneas sin fecha que la siguen son extras de ese viaje
(descarga de palés vacíos, paralización, reparto…). Pedido de Mario 27-10-2026."""
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

import procesador

PROMPT = """Es una página de una factura de transporte. Extrae TODAS las líneas de la tabla
(FECHA · Nº ALBARAN · DESCRIPCIÓN · CANTIDAD · PRECIO · %DTO · IMPORTE), en orden y sin saltarte
ninguna, en JSON estricto, sin nada más:
{"numero_factura": "p.ej. 2026/8269", "fecha_factura": "AAAA-MM-DD", "pagina": 1, "paginas": 3,
 "imp_bruto": número o null, "total_factura": número o null,
 "lineas": [{"fecha": "AAAA-MM-DD o null si la línea no tiene fecha", "viaje": "el Nº ALBARAN tal cual o null",
             "descripcion": "texto completo tal cual", "cantidad": número, "precio": número, "importe": número}]}
Los importes usan punto decimal y pueden llevar el símbolo €: devuélvelos como número (1037.00, 6438.47).
Ojo: el IMPORTE es la última columna; no lo confundas con el PRECIO."""


def _leer_pagina(pdf: Path, pagina: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['pdftoppm', '-r', '200', '-png', '-singlefile', '-f', str(pagina + 1),
                        '-l', str(pagina + 1), str(pdf), f'{tmp}/p'], check=True)
        datos = base64.standard_b64encode(Path(f'{tmp}/p.png').read_bytes()).decode()
    texto = procesador._pedir(datos, PROMPT, 8000, procesador.MODELO)
    m = re.search(r'\{.*\}', texto, re.S)
    return json.loads(m.group(0)) if m else {'lineas': []}


def ruta_de(descripcion: str) -> tuple[str, list[str]]:
    """«SONSECA -> MUELA, LA Ref: 80466559» → ('LA MUELA', ['80466559'])."""
    d = descripcion or ''
    refs = re.findall(r'\b\d{8}\b', d.split('Ref:', 1)[1]) if 'Ref:' in d else []
    destino = d.split('->', 1)[1] if '->' in d else d
    destino = destino.split('Ref:', 1)[0]
    destino = re.sub(r'\(MUNICIPIO\)', '', destino, flags=re.I).strip(' .')
    if ', ' in destino and len(destino.split(', ')[-1]) <= 3:  # «MUELA, LA» → «LA MUELA»
        nombre, art = destino.rsplit(', ', 1)
        destino = f'{art} {nombre}'
    return destino.upper(), refs


def leer_factura(pdf: Path) -> dict:
    """Factura completa: cabecera + viajes con sus extras, y comprobación de la suma."""
    from pypdf import PdfReader
    n = len(PdfReader(str(pdf)).pages)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        paginas = list(ex.map(lambda i: _leer_pagina(pdf, i), range(n)))
    cab = next((p for p in paginas if p.get('numero_factura')), paginas[0] if paginas else {})
    viajes, sueltas = [], []
    for p in paginas:
        for l in p.get('lineas', []):
            importe = float(l.get('importe') or 0)
            if l.get('viaje'):
                destino, refs = ruta_de(l.get('descripcion', ''))
                viajes.append({'viaje': str(l['viaje']).strip(), 'fecha': l.get('fecha'),
                               'descripcion': l.get('descripcion', ''), 'destino': destino,
                               'refs': refs, 'cantidad': l.get('cantidad'), 'importe': importe,
                               'extras': []})
            elif viajes:
                viajes[-1]['extras'].append({'concepto': (l.get('descripcion') or '').strip(),
                                             'importe': importe, 'cantidad': l.get('cantidad')})
            else:
                sueltas.append(l)
    suma = round(sum(v['importe'] + sum(e['importe'] for e in v['extras']) for v in viajes), 2)
    imp_bruto = next((p.get('imp_bruto') for p in paginas if p.get('imp_bruto')), None)
    return {'numero': cab.get('numero_factura'), 'fecha': cab.get('fecha_factura'),
            'imp_bruto': imp_bruto, 'total': cab.get('total_factura'), 'viajes': viajes,
            'suma_lineas': suma, 'cuadra': imp_bruto is not None and abs(suma - float(imp_bruto)) < 0.01,
            'sueltas': sueltas}
