#!/usr/bin/env python3
"""Provisión / redeploy del servicio EasyPanel `travelia/albaranes`.

Uso:
  python3 scripts/deploy_easypanel.py          # asegura source+build+env+mount+dominio y despliega
  python3 scripts/deploy_easypanel.py deploy   # solo redeploy (tras un push a main)

Patrón calcado de tareas/scripts/deploy_easypanel.py. El push a main NO
redespliega: hay que ejecutar esto. Secretos en Infisical carpeta `albaranes`.
"""
import json
import os
import secrets
import string
import sys
import urllib.request

sys.path.insert(0, os.path.expanduser('~/.config/aromas'))
from infisical_get import get_secrets  # noqa: E402

PROJECT, SERVICE = 'travelia', 'albaranes'
HOST = 'albaranes.inhumario.com'
REPO = 'https://github.com/inhumario/albaranes.git'


def cargar_env(ruta):
    valores = {}
    with open(os.path.expanduser(ruta)) as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith('#') and '=' in linea:
                k, v = linea.split('=', 1)
                valores[k] = v.strip('"')
    return valores


EP = cargar_env('~/.config/aromas/easypanel.env')


def trpc(procedure, payload):
    req = urllib.request.Request(
        f"{EP['EASYPANEL_API_BASE']}/{procedure}",
        data=json.dumps({'json': payload}).encode(),
        headers={'Authorization': f"Bearer {EP['EASYPANEL_TOKEN']}",
                 'Content-Type': 'application/json'},
        method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b'{}')


def cuid():
    return 'c' + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))


def main():
    solo_deploy = len(sys.argv) > 1 and sys.argv[1] == 'deploy'
    if not solo_deploy:
        try:
            trpc('services.app.createService', {'projectName': PROJECT, 'serviceName': SERVICE})
            print('servicio: creado')
        except urllib.error.HTTPError as e:
            print(f'servicio: ya existía ({e.code})')

        trpc('services.app.updateSourceGit',
             {'projectName': PROJECT, 'serviceName': SERVICE,
              'repo': REPO, 'ref': 'main', 'path': '/'})
        print('source: git main OK')

        trpc('services.app.updateBuild',
             {'projectName': PROJECT, 'serviceName': SERVICE,
              'build': {'type': 'dockerfile', 'file': 'Dockerfile'}})
        print('build: dockerfile OK')

        cfg = get_secrets('albaranes')
        env = '\n'.join(f'{k}={v}' for k, v in sorted(cfg.items())) + '\nPORT=8000'
        trpc('services.app.updateEnv',
             {'projectName': PROJECT, 'serviceName': SERVICE, 'env': env})
        print(f'env: {len(cfg) + 1} variables OK')

        actual = trpc('services.app.inspectService',
                      {'projectName': PROJECT, 'serviceName': SERVICE})['json']
        if not any(m.get('name') == 'albaranes-data' for m in actual.get('mounts', [])):
            trpc('mounts.createMount',
                 {'projectName': PROJECT, 'serviceName': SERVICE,
                  'values': {'type': 'volume', 'name': 'albaranes-data', 'mountPath': '/data'}})
            print('mount: volumen albaranes-data → /data OK')

        dominios = trpc('domains.listDomains',
                        {'projectName': PROJECT, 'serviceName': SERVICE})['json']
        if not any(d.get('host') == HOST for d in dominios):
            trpc('domains.createDomain',
                 {'id': cuid(), 'destinationType': 'service', 'host': HOST, 'https': True,
                  'path': '/', 'middlewares': [], 'certificateResolver': 'letsencrypt',
                  'wildcard': False,
                  'serviceDestination': {'protocol': 'http', 'port': 8000,
                                         'projectName': PROJECT, 'serviceName': SERVICE}})
            print(f'dominio: {HOST} OK')

    trpc('services.app.deployService', {'projectName': PROJECT, 'serviceName': SERVICE})
    print('deploy: lanzado')


if __name__ == '__main__':
    main()
