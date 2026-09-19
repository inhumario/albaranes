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

MODELO = 'claude-haiku-4-5-20251001'
DPI = 150

PROMPT = """Esta imagen es una página escaneada de un albarán de entrega (delivery note).
Extrae en JSON estricto, sin nada más:
{
  "emisor": "marca principal del membrete, en minúsculas, UNA palabra si es posible (p.ej. 'delaviuda', no 'delaviuda alimentación s.a.u.')",
  "numero": "el número identificador del albarán tal cual aparece (junto a etiquetas como DELIVERY NOTE, ALBARÁN Nº, Nº DOCUMENTO...). Solo el número, sin espacios",
  "hoja": "valor del campo SHEET/HOJA si existe, si no null",
  "es_continuacion": "true solo si la página claramente NO es la primera hoja de un albarán (no tiene cabecera con número propio)",
  "confianza": "alta|media|baja según lo legible que sea el número"
}
Si no encuentras ningún número de albarán, pon numero: null y confianza: "baja"."""

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


def _pedir(datos: str, prompt: str, max_tokens: int = 300, modelo: str = MODELO) -> str:
    # temperature=0 solo donde el modelo lo admite (Sonnet 5 lo rechaza como deprecado)
    extra = {'temperature': 0} if modelo == MODELO else {}
    resp = _api().messages.create(
        model=modelo, max_tokens=max_tokens, extra_body=extra,
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': datos}},
            {'type': 'text', 'text': prompt},
        ]}])
    return resp.content[0].text


def _analizar_pagina(png: Path, pdf: Path, pagina: int) -> dict:
    """Doble lectura DESCORRELACIONADA: la extracción completa (Haiku, render 150 dpi)
    se contrasta con una segunda lectura solo del número hecha por otro modelo sobre
    otro renderizado (Sonnet, 220 dpi). Si no coinciden, el albarán va a revisión —
    nunca se archiva un número dudoso. Con la misma imagen y el mismo modelo no basta:
    en una pasada real las dos lecturas coincidieron en un número con dos dígitos
    traspuestos (80643536 por 80463536)."""
    datos = base64.standard_b64encode(png.read_bytes()).decode()
    m = re.search(r'\{.*\}', _pedir(datos, PROMPT), re.S)
    info = json.loads(m.group(0)) if m else {'numero': None, 'confianza': 'baja'}
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


def procesar_pdf(pdf: Path) -> list[dict]:
    """Devuelve un dict por albarán: numero, emisor, confianza, revisar,
    paginas (índices) y writer con el PDF montado (llamar a .write())."""
    grupos, actual = [], None
    lector = PdfReader(str(pdf))
    with tempfile.TemporaryDirectory() as tmp:
        prefijo = Path(tmp) / 'p'
        subprocess.run(['pdftoppm', '-r', str(DPI), '-png', str(pdf), str(prefijo)], check=True)
        pngs = sorted(Path(tmp).glob('p-*.png'))
        for i, png in enumerate(pngs):
            info = _analizar_pagina(png, pdf, i)
            numero = info.get('numero')
            cont = str(info.get('es_continuacion')).lower() == 'true'
            if actual and (numero == actual['numero'] or (cont and not numero)):
                actual['paginas'].append(i)
                continue
            if actual:
                grupos.append(actual)
            actual = {'numero': numero, 'emisor': info.get('emisor') or '',
                      'confianza': info.get('confianza', 'baja'), 'paginas': [i]}
        if actual:
            grupos.append(actual)

    for g in grupos:
        g['revisar'] = not g['numero'] or g['confianza'] == 'baja'
        w = PdfWriter()
        for idx in g['paginas']:
            w.add_page(lector.pages[idx])
        g['writer'] = w
    return grupos
