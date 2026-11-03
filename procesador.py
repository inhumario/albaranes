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
albarán de entrega (delivery note), pero puede ser otro documento, como una ORDEN DE CARGA o un
justificante de otra empresa. La página puede estar escaneada al revés o de lado.
Extrae en JSON estricto, sin nada más:
{
  "giro": "grados que hay que girar la página EN EL SENTIDO DE LAS AGUJAS DEL RELOJ para que el texto principal se lea derecho: 0 si ya está derecha, 180 si está boca abajo, 90 o 270 si está de lado",
  "tipo": "albaran si es un albarán / delivery note emitido por el proveedor que envía la mercancía; orden_carga si es una ORDEN DE CARGA (u otro documento de transporte que no es un albarán pero cita uno en un campo ALBARÁN); albaran_agrupado si es un albarán que en su casilla de número cita VARIOS albaranes (p.ej. '80467050/52-58'); justificante si es un documento de otra empresa que acompaña a la entrega pero no es el albarán del proveedor (confirmación de entrada o de entrega del destinatario, vale o recibo de palés, reserva de muelle, CMR, ticket de báscula...); otro si no es nada de eso",
  "albaranes_ref": "solo si tipo es orden_carga o albaran_agrupado: el texto del campo ALBARÁN / Nº ALBARÁN tal cual (p.ej. '80465556-5568'); si no, null",
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
Si tipo no es albaran, numero: null (el número que cite va en albaranes_ref)."""


def giro_de(v) -> int:
    """Grados (0/90/180/270) que hay que girar la página en el sentido del reloj."""
    try:
        g = int(round(float(str(v).strip().rstrip('°º')) / 90)) * 90 % 360
    except (TypeError, ValueError):
        return 0
    return g


def tipo_de(tipo, numero) -> str:
    """El modelo escribe el tipo a su manera («albarán», «orden de carga»…): se normaliza.
    Un «albarán» cuyo número cita varios (80467050/52-58) es un albarán agrupado."""
    t = normaliza(tipo or '')
    if 'orden' in t:
        return 'orden_carga'
    if 'agrupado' in t:
        return 'albaran_agrupado'
    if 'justif' in t or t == 'otro':
        return 'justificante'
    if numero and len(re.findall(r'\d+', str(numero))) > 1:
        return 'albaran_agrupado'
    return 'albaran'


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

def _falta_una_cifra(corto: str, largo: str) -> bool:
    """«8048014» es «80468014» con una cifra de menos (escrito a mano)."""
    return len(largo) == len(corto) + 1 and any(largo[:i] + largo[i + 1:] == corto
                                                for i in range(len(largo)))


def albaranes_citados(texto, largo: int = 8, prefijo: str = '', conocidos=()) -> list[str]:
    """TODOS los albaranes que cita una orden de carga, uno a uno. El guion es un rango y la
    barra o la coma separan: «80466258-6261, 6269, 6719-21» → 80466258…80466261, 80466269,
    80466719…80466721. Lo abreviado repite el principio del número anterior; si la orden solo
    trae finales («6633/6634»), el principio sale de `prefijo` (un albarán de la misma pila)."""
    if not texto:
        return []
    piezas = re.findall(r'\d+|-', str(texto).replace(' ', ''))
    base = ''
    salida, rango_desde = [], None
    for x in piezas:
        if x == '-':
            rango_desde = salida[-1] if salida else None
            continue
        parecido = next((c for c in conocidos if len(x) == largo - 1 and _falta_una_cifra(x, c)), None)
        if parecido:  # número completo con una cifra de menos: es el de la pila
            x = parecido
        elif base and len(x) < len(base):
            x = base[:len(base) - len(x)] + x
        elif len(x) < largo and prefijo and len(prefijo) >= largo:
            x = prefijo[:largo - len(x)] + x
        base = x if len(x) >= len(base) else base
        if rango_desde and rango_desde.isdigit() and int(rango_desde) < int(x) \
                and int(x) - int(rango_desde) <= 200:
            salida += [str(n) for n in range(int(rango_desde) + 1, int(x) + 1)]
        elif x not in salida:
            salida.append(x)
        rango_desde = None
    return list(dict.fromkeys(salida))


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
    m = None
    for _ in range(3):  # a veces se le va el margen razonando y no devuelve el JSON: otra vez
        m = re.search(r'\{.*\}', _pedir(datos, PROMPT, 6000), re.S)
        if m:
            break
    try:
        info = json.loads(m.group(0)) if m else {'numero': None, 'confianza': 'baja'}
    except json.JSONDecodeError:
        info = {'numero': None, 'confianza': 'baja'}
    if info.get('numero'):  # «8046354 0» → 80463540
        info['numero'] = re.sub(r'\s+', '', str(info['numero'])) or None
    info['giro'] = giro_de(info.get('giro'))
    if info['giro']:
        return info  # la página está torcida: se endereza y se vuelve a leer (procesar_pdf)
    info['tipo'] = tipo_de(info.get('tipo'), info.get('numero'))
    if info['tipo'] == 'albaran_agrupado' and not info.get('albaranes_ref'):
        info['albaranes_ref'] = info.get('numero')
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


def _enderezar(pdf: Path, pagina: int, giro: int, carpeta: Path):
    """Copia de una página girada `giro` grados (sentido del reloj): PDF de una hoja + su PNG."""
    w = PdfWriter()
    w.add_page(PdfReader(str(pdf)).pages[pagina])
    w.pages[0].rotate(giro)
    sub = carpeta / f'girada-{pagina:04d}-{giro}'
    destino = sub.with_suffix('.pdf')
    with destino.open('wb') as f:
        w.write(f)
    subprocess.run(['pdftoppm', '-r', str(DPI), '-png', '-singlefile', str(destino), str(sub)],
                   check=True)
    return destino, sub.with_suffix('.png'), w.pages[0]


def _leer_derecha(png: Path, pdf: Path, i: int, tmp: Path):
    """Lee la página; si está boca abajo o de lado, la endereza y la vuelve a leer. Devuelve
    (info, página para el PDF de salida o None si no se ha girado)."""
    info = _analizar_pagina(png, pdf, i)
    giro_total, pagina = 0, None
    for _ in range(2):  # 90/270 se confunden a veces: si sigue torcida, se corrige otra vez
        if not info.get('giro'):
            break
        giro_total = (giro_total + info['giro']) % 360
        pdf_g, png_g, pagina = _enderezar(pdf, i, giro_total, tmp)
        info = _analizar_pagina(png_g, pdf_g, 0)
    if info.get('giro'):  # no hay manera: se lee tal cual y va a revisar
        info['giro'] = 0
        info['tipo'] = tipo_de(info.get('tipo'), info.get('numero'))
        info['confianza'] = 'baja'
    info['girada'] = giro_total
    return info, pagina


def _difiere(a, b) -> bool:
    """Dos lecturas del mismo número con palés o peso distintos: son versiones, no copias."""
    for k in ('pales', 'peso'):
        if a.get(k) is not None and b.get(k) is not None and abs(a[k] - b[k]) > 0.5:
            return True
    return False


def completar_refs(grupos: list[dict]) -> None:
    """La orden de carga a veces cita solo el final del número («6633/6634», «7050/52-58»):
    se completa con el principio de los albaranes de la misma pila (80466633…)."""
    numeros = [g['numero'] for g in grupos if g['tipo'] == 'albaran' and g.get('numero')
               and str(g['numero']).isdigit()]
    if not numeros:
        return
    largo = max(len(n) for n in numeros)
    for g in grupos:
        if g['tipo'] == 'albaran' or not g.get('rango'):
            continue
        cortos = [n for n in g['rango'] if len(n) < largo]
        if not cortos:
            continue
        base = next((n for n in numeros if len(n) == largo and (n.endswith(cortos[0])
                                                                or _falta_una_cifra(cortos[0], n))), numeros[0])
        g['rango'] = [base[:largo - len(n)] + n if len(n) < largo else n for n in g['rango']]
        g['refs'] = g['rango'][:1]
        g['citados'] = albaranes_citados(g['ref_texto'], largo, base, numeros)
        if g['citados']:  # el primero de la orden manda (la orden va con él)
            g['rango'], g['refs'] = g['citados'], g['citados'][:1]


def _resumen(d) -> str:
    return f"{d.get('pales') or 0:g} palés / {d.get('peso') or 0:g} kg"


def procesar_pdf(pdf: Path, avance=None) -> list[dict]:
    """Devuelve un dict por albarán: numero, emisor, confianza, revisar,
    paginas (índices) y writer con el PDF montado (llamar a .write()).
    `avance(hechas, total)` se llama tras cada página (para la barra de progreso).

    Tipos de grupo: albaran; orden_carga y albaran_agrupado (anexos que se pegan al primer
    albarán que citan). Los justificantes de terceros (vales de palés, confirmaciones de
    entrada del destinatario, reservas de muelle…) no son grupos: se pegan al documento
    anterior de la pila (o al siguiente si van al principio), que es el de su viaje."""
    grupos, actual = [], None
    lector = PdfReader(str(pdf))
    paginas_pdf = list(lector.pages)
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
            r = _leer_derecha(i_png[1], pdf, i_png[0], Path(tmp))
            with cerrojo:
                hechas[0] += 1
                if avance:
                    avance(hechas[0], len(pngs))
            return r
        with ThreadPoolExecutor(max_workers=LECTURAS_A_LA_VEZ) as ex:
            leidas = list(ex.map(leer, enumerate(pngs)))
        for i, (_, girada) in enumerate(leidas):
            if girada is not None:
                paginas_pdf[i] = girada  # lo que se guarda va ya derecho
        infos = [info for info, _ in leidas]
        # En una pila (un viaje) el proveedor es el que más albaranes trae; un «albarán» de
        # otra empresa (el vale de PoolBack lleva impreso ALBARÁN) es un justificante del viaje
        from collections import Counter
        emisores = Counter(normaliza(x.get('emisor') or '') for x in infos if x['tipo'] == 'albaran')
        emisores.pop('', None)
        if emisores:
            principal, veces = emisores.most_common(1)[0]
            for x in infos:
                e = normaliza(x.get('emisor') or '')
                if x['tipo'] == 'albaran' and e and e != principal and emisores[e] < veces:
                    x['tipo'], x['numero'] = 'justificante', None
        justif_sueltos = []  # justificantes antes del primer documento: van con el siguiente

        def cerrar():
            nonlocal actual
            if actual:
                grupos.append(actual)
                actual = None

        def nuevo(g):
            if justif_sueltos:
                g['paginas'].extend(justif_sueltos)
                g.setdefault('paginas_justif', []).extend(justif_sueltos)
                g['justificantes'] = g.get('justificantes', 0) + len(justif_sueltos)
                justif_sueltos.clear()
            return g

        for i, info in enumerate(infos):
            if info['tipo'] in ('justificante', 'otro'):
                ultimo = actual or (grupos[-1] if grupos else None)
                if ultimo:
                    ultimo['paginas'].append(i)
                    ultimo.setdefault('paginas_justif', []).append(i)
                    ultimo['justificantes'] = ultimo.get('justificantes', 0) + 1
                else:
                    justif_sueltos.append(i)
                continue
            if info['tipo'] in ('orden_carga', 'albaran_agrupado'):
                # documento anexo: se une luego al PDF del albarán que cita
                cerrar()
                ref = info.get('albaranes_ref') or info.get('numero') or ''
                grupos.append(nuevo({'tipo': info['tipo'], 'numero': None,
                                     'emisor': info.get('emisor') or '', 'fecha': info.get('fecha'),
                                     'ref_texto': str(ref),
                                     'rango': numeros_citados(ref),
                                     'citados': albaranes_citados(ref),
                                     # rango → la orden va con el PRIMER albarán (regla de Mario)
                                     'refs': numeros_citados(ref)[:1],
                                     'confianza': info.get('confianza', 'media'), 'paginas': [i]}))
                continue
            numero = info.get('numero')
            hoja = str(info.get('hoja') or '').strip() or None
            cont = str(info.get('es_continuacion')).lower() == 'true'
            datos = {'sellado': str(info.get('sellado')).lower() == 'true',
                     'peso': _num(info.get('peso_bruto')), 'pales': _num(info.get('pales')),
                     'destino': (info.get('destino') or '').strip().upper() or None,
                     'fecha': info.get('fecha')}
            otra_version = actual and numero and numero == actual['numero'] and _difiere(datos, actual)
            if actual and numero and numero == actual['numero'] and hoja in actual['hojas'] \
                    and not otra_version:
                # la misma hoja del mismo albarán escaneada otra vez: copia, se descarta
                actual['duplicadas'] += 1
                continue
            if actual and (numero == actual['numero'] or (cont and not numero)):
                # otra hoja del mismo albarán, u otra VERSIÓN (mismo número, palés o peso
                # distintos, p. ej. el 80467368 reexpedido a Quito): se guardan todas las hojas
                # y mandan los datos de la última, que es donde van los totales
                if otra_version:
                    actual['versiones'].append(_resumen(actual))
                actual['paginas'].append(i)
                actual['hojas'].append(hoja)
                for k, v in datos.items():
                    if v not in (None, False):
                        actual[k] = v
                continue
            cerrar()
            actual = nuevo({'tipo': 'albaran', 'numero': numero, 'emisor': info.get('emisor') or '',
                            'hojas': [hoja], 'duplicadas': 0, 'versiones': [],
                            'girada': bool(info.get('girada')),
                            'confianza': info.get('confianza', 'baja'), 'paginas': [i], **datos})
        cerrar()
        if justif_sueltos:  # una pila que solo trae justificantes: a revisar, sin perderlos
            grupos.append({'tipo': 'albaran', 'numero': None, 'emisor': '', 'hojas': [None],
                           'duplicadas': 0, 'versiones': [], 'confianza': 'baja',
                           'paginas': justif_sueltos, 'justificantes': len(justif_sueltos),
                           'sellado': False, 'peso': None, 'pales': None, 'destino': None,
                           'fecha': None})

    completar_refs(grupos)
    for g in grupos:
        g['girada'] = g.get('girada') or any(infos[i].get('girada') for i in g['paginas'])
        g['revisar'] = g['tipo'] == 'albaran' and (not g['numero'] or g['confianza'] == 'baja')
        w = PdfWriter()
        for idx in g['paginas']:
            w.add_page(paginas_pdf[idx])
        g['writer'] = w
        if g.get('paginas_justif'):  # solo los justificantes, por si el albarán ya estaba
            wj = PdfWriter()
            for idx in g['paginas_justif']:
                wj.add_page(paginas_pdf[idx])
            g['writer_justif'] = wj
    return grupos
