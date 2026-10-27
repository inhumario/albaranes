"""Pipeline de procesado de escaneos: separa, lee, agrupa y archiva albaranes.

Adaptado del prototipo validado el 19-09-2026 (7/7 con las muestras reales de
Delaviuda). Cada página de la pila se analiza con Claude Haiku (visión): emisor
del membrete + número de albarán. Hojas de continuación se agrupan con su
primera hoja. No hace falta decir de qué cliente es el escaneo: se detecta del
membrete y se casa contra los alias configurados de cada cliente.
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import anthropic
from pypdf import PdfReader, PdfWriter

MODELO = 'claude-sonnet-5'  # Haiku confundía el 3 con el 5 en estos escaneos (27-10)
DPI = 200  # a 150 Haiku confundía el 3 con el 5 (27-10)
LECTURAS_A_LA_VEZ = 6

PROMPT = """Esta imagen es una página escaneada de un documento de transporte: normalmente un
albarán de entrega (delivery note), pero puede ser otro documento, como una ORDEN DE CARGA.
Extrae en JSON estricto, sin nada más:
{
  "tipo": "albaran si es un albarán / delivery note; orden_carga si es una ORDEN DE CARGA (u otro documento de transporte que no es un albarán pero cita uno en un campo ALBARÁN); otro si no es ninguno de los dos",
  "albaranes_ref": "solo si tipo es orden_carga: el texto del campo ALBARÁN tal cual (p.ej. '80465556-5568'); si no, null",
  "emisor": "marca principal del membrete, en minúsculas, UNA palabra si es posible (p.ej. 'delaviuda', no 'delaviuda alimentación s.a.u.')",
  "numero": "el número identificador del albarán tal cual aparece (junto a etiquetas como DELIVERY NOTE, ALBARÁN Nº, Nº DOCUMENTO...). Solo el número, sin espacios",
  "hoja": "valor del campo SHEET/HOJA si existe, si no null",
  "fecha": "SOLO la fecha de CARGA en formato AAAA-MM-DD: en un albarán, la de LOADING / Expedición / Fecha de carga / Fecha de salida; en una orden de carga, su campo FECHA. NUNCA la de entrega (DELIVERY, Entrega) ni la del pedido (Nº PEDIDO/FECHA) ni sellos de recepción. Si la casilla de carga o expedición está vacía, null. Las fechas van día/mes/año (29.06.2026 = 2026-06-29)",
  "peso_bruto": "solo en albaranes: PESO BRUTO total del albarán en kg, como número con punto decimal (1.138,200 → 1138.2); null si no aparece",
  "pales": "solo en albaranes: número total de palés (PALLETS / PALES / Nº PALETS) como número; null si no aparece",
  "destino": "solo en albaranes: población del domicilio de envío de la mercancía, en mayúsculas y sin código postal (p.ej. BARCELONA); null si no aparece",
  "sellado": "true si la hoja lleva sello o firma de recepción del cliente (p.ej. 'CONFORME', fecha de llegada estampada), si no false",
  "es_continuacion": "true solo si la página claramente NO es la primera hoja de un albarán (no tiene cabecera con número propio)",
  "confianza": "alta|media|baja según lo legible que sea el número"
}
Si no encuentras ningún número de albarán, pon numero: null y confianza: "baja".
Si tipo es orden_carga u otro, numero: null (el número que cite va en albaranes_ref)."""


def _num(v):
    """Número del modelo a float: acepta 1138.2, "1138,2" o "1.138,200"."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(' ', '')
    if ',' in t:
        t = t.replace('.', '').replace(',', '.')
    try:
        return float(t)
    except ValueError:
        return None


def numeros_citados(texto) -> list[str]:
    """Números de albarán que cita una orden de carga. Entiende la forma abreviada
    «80465556-5568» (el segundo repite los primeros dígitos del primero: 80465568).
    Un guion es un RANGO (del 80465556 al 80465568) y la orden va con el primero
    (regla de Mario 27-10): quien la une usa solo el primer número."""
    if not texto:
        return []
    trozos = re.findall(r'\d+', str(texto))
    numeros, base = [], ''
    for t in trozos:
        if base and len(t) < len(base):
            t = base[:len(base) - len(t)] + t
        else:
            base = t
        if t not in numeros:
            numeros.append(t)
    return numeros

_cliente_api = None


def _api() -> anthropic.Anthropic:
    global _cliente_api
    if _cliente_api is None:
        clave = os.environ.get('ANTHROPIC_API_KEY')
        if not clave:  # desarrollo local: se coge de Infisical
            import sys
            sys.path.insert(0, str(Path.home() / '.config/aromas'))
            from infisical_get import get_secrets
            clave = get_secrets('anthropic')['ANTHROPIC_API_KEY']
        _cliente_api = anthropic.Anthropic(api_key=clave)
    return _cliente_api


def normaliza(texto: str) -> str:
    plano = unicodedata.normalize('NFKD', (texto or '').lower())
    plano = plano.encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', plano).strip('-')


PROMPT_VERIFICACION = ('Devuelve SOLO el número identificador del albarán de esta página '
                       '(el que acompaña a DELIVERY NOTE / ALBARÁN Nº / Nº DOCUMENTO), '
                       'dígito a dígito y sin nada más. Si no hay número, devuelve NO.')


MODELO_CONTRASTE = 'claude-sonnet-5'
DPI_CONTRASTE = 220
DPI_AMPLIADA = 300
MODELO_AMPLIADA = 'claude-opus-5-5'  # tercera lectura con otra familia de modelo

PROMPT_AMPLIADA = ('Esto es un albarán escaneado a alta resolución. Lee el número del albarán (el que acompaña a '
                   'DELIVERY NOTE / ALBARÁN Nº) DÍGITO A DÍGITO, separando cada carácter con un espacio, '
                   'y nada más. Ejemplo de respuesta: 1 2 3 4 5 6 7 8. Si no lo ves, responde NO.')


def _pedir(datos: str, prompt: str, max_tokens: int = 300, modelo: str = MODELO) -> str:
    # sin temperature: desde oct-2026 la API la rechaza también en Haiku («Extra inputs
    # are not permitted»); la fiabilidad la da la lectura de contraste descorrelacionada
    max_tokens = max(max_tokens, 2000)  # margen para el razonamiento previo del modelo
    resp = _api().messages.create(
        model=modelo, max_tokens=max_tokens,
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': datos}},
            {'type': 'text', 'text': prompt},
        ]}])
    # Sonnet puede anteponer un bloque de razonamiento: se junta solo el texto
    return ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')


def _analizar_pagina(png: Path, pdf: Path, pagina: int) -> dict:
    """Triple lectura DESCORRELACIONADA (la tercera, ampliada, desde el 27-10): la extracción completa (Haiku, render 150 dpi)
    se contrasta con una segunda lectura solo del número hecha por otro modelo sobre
    otro renderizado (Sonnet, 220 dpi). Si no coinciden, el albarán va a revisión —
    nunca se archiva un número dudoso. Con la misma imagen y el mismo modelo no basta:
    en una pasada real las dos lecturas coincidieron en un número con dos dígitos
    traspuestos (80643536 por 80463536)."""
    datos = base64.standard_b64encode(png.read_bytes()).decode()
    m = re.search(r'\{.*\}', _pedir(datos, PROMPT), re.S)
    info = json.loads(m.group(0)) if m else {'numero': None, 'confianza': 'baja'}
    if info.get('numero'):  # «8046354 0» → 80463540
        info['numero'] = re.sub(r'\s+', '', str(info['numero'])) or None
    # el modelo escribe el tipo a su manera («albarán», «orden de carga»…): se normaliza
    info['tipo'] = 'orden_carga' if 'orden' in normaliza(info.get('tipo') or '') else 'albaran'
    if info['tipo'] != 'albaran':
        info['numero'] = None  # una orden de carga no es un albarán: no se verifica su número
        return info
    if info.get('numero'):
        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / 'v'
            subprocess.run(['pdftoppm', '-r', str(DPI_CONTRASTE), '-png',
                            '-f', str(pagina + 1), '-l', str(pagina + 1), str(pdf), str(pref)],
                           check=True)
            png2 = next(Path(tmp).glob('v-*.png'))
            datos2 = base64.standard_b64encode(png2.read_bytes()).decode()
        contraste = re.sub(r'[^0-9A-Za-z/-]', '',
                           _pedir(datos2, PROMPT_VERIFICACION, 50, MODELO_CONTRASTE))
        if contraste != info['numero']:
            info['confianza'] = 'baja'
            info['numero_contraste'] = contraste
            return info
        # Tercera lectura (27-10): las dos primeras llegaron a coincidir en el MISMO error
        # (80463538 → 80465538). Se amplía solo la casilla del número a 300 dpi y se lee
        # dígito a dígito: tiene que dar lo mismo que las otras dos.
        ampliada = _lectura_ampliada(png, pdf, pagina)
        info['numero_ampliada'] = ampliada
        if ampliada != info['numero']:
            info['confianza'] = 'baja'
    return info


def _lectura_ampliada(png: Path, pdf: Path, pagina: int) -> str:
    """Página entera a 300 dpi en gris, leída dígito a dígito por Sonnet. (Recortar la casilla
    con una caja pedida a Haiku se probó el 27-10 y falla: la caja sale desplazada.)"""
    with tempfile.TemporaryDirectory() as tmp:
        pref = Path(tmp) / 'a'
        subprocess.run(['pdftoppm', '-r', str(DPI_AMPLIADA), '-png', '-gray',
                        '-f', str(pagina + 1), '-l', str(pagina + 1), str(pdf), str(pref)], check=True)
        datos3 = base64.standard_b64encode(next(Path(tmp).glob('a-*.png')).read_bytes()).decode()
    texto = _pedir(datos3, PROMPT_AMPLIADA, 80, MODELO_AMPLIADA)
    return re.sub(r'[^0-9A-Za-z/-]', '', texto)


def procesar_pdf(pdf: Path, avance=None) -> list[dict]:
    """Devuelve un dict por albarán: numero, emisor, confianza, revisar,
    paginas (índices) y writer con el PDF montado (llamar a .write()).
    `avance(hechas, total)` se llama tras cada página (para la barra de progreso)."""
    grupos, actual = [], None
    lector = PdfReader(str(pdf))
    with tempfile.TemporaryDirectory() as tmp:
        prefijo = Path(tmp) / 'p'
        subprocess.run(['pdftoppm', '-r', str(DPI), '-png', str(pdf), str(prefijo)], check=True)
        pngs = sorted(Path(tmp).glob('p-*.png'))
        if avance:
            avance(0, len(pngs))
        # cada página tarda ~16 s (tres lecturas): se leen varias a la vez y luego se
        # agrupan en orden
        import threading
        from concurrent.futures import ThreadPoolExecutor
        hechas, cerrojo = [0], threading.Lock()

        def leer(i_png):
            r = _analizar_pagina(i_png[1], pdf, i_png[0])
            with cerrojo:
                hechas[0] += 1
                if avance:
                    avance(hechas[0], len(pngs))
            return r
        with ThreadPoolExecutor(max_workers=LECTURAS_A_LA_VEZ) as ex:
            infos = list(ex.map(leer, enumerate(pngs)))
        for i, info in enumerate(infos):
            if info['tipo'] == 'orden_carga':
                # documento anexo: se une luego al PDF del albarán que cita
                if actual:
                    grupos.append(actual)
                    actual = None
                grupos.append({'tipo': 'orden_carga', 'numero': None,
                               'emisor': info.get('emisor') or '', 'fecha': info.get('fecha'),
                               'ref_texto': info.get('albaranes_ref') or '',
                               'rango': numeros_citados(info.get('albaranes_ref')),
                               # rango → la orden va con el PRIMER albarán (regla de Mario)
                               'refs': numeros_citados(info.get('albaranes_ref'))[:1],
                               'confianza': info.get('confianza', 'media'), 'paginas': [i]})
                continue
            numero = info.get('numero')
            hoja = str(info.get('hoja') or '').strip() or None
            cont = str(info.get('es_continuacion')).lower() == 'true'
            if actual and numero and numero == actual['numero'] and hoja in actual['hojas']:
                # la misma hoja del mismo albarán escaneada otra vez: copia, se descarta
                actual['duplicadas'] += 1
                continue
            if actual and (numero == actual['numero'] or (cont and not numero)):
                actual['paginas'].append(i)
                actual['hojas'].append(hoja)
                continue
            if actual:
                grupos.append(actual)
            actual = {'tipo': 'albaran', 'numero': numero, 'emisor': info.get('emisor') or '',
                      'hojas': [hoja], 'duplicadas': 0,
                      'sellado': str(info.get('sellado')).lower() == 'true',
                      'peso': _num(info.get('peso_bruto')), 'pales': _num(info.get('pales')),
                      'destino': (info.get('destino') or '').strip().upper() or None,
                      'fecha': info.get('fecha'),
                      'confianza': info.get('confianza', 'baja'), 'paginas': [i]}
        if actual:
            grupos.append(actual)

    for g in grupos:
        g['revisar'] = g['tipo'] == 'albaran' and (not g['numero'] or g['confianza'] == 'baja')
        w = PdfWriter()
        for idx in g['paginas']:
            w.add_page(lector.pages[idx])
        g['writer'] = w
    return grupos
