"""MCP-only onboarding: provision locally, verify the hub, export secret-free config."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import click
import httpx

from agent_bus.config import get_bus_url, get_config_dir, load_config
from agent_bus.project import init_project, load_project_config, project_identity, resolve_project_root
from agent_bus.security import AuthenticationError, load_session, validate_id


def parse_agents(value: str) -> list[tuple[str, str]]:
    pairs = []
    for item in value.split(','):
        name, sep, provider = item.strip().partition(':')
        provider = provider.strip() if sep else name
        try:
            validate_id(name)
            validate_id(provider)
        except AuthenticationError:
            raise click.ClickException('Usa identidades válidas: backend:claude,qa:codex') from None
        if name == 'free' or any(name == previous for previous, _ in pairs):
            raise click.ClickException('Las identidades deben ser únicas y distintas de free')
        pairs.append((name, provider))
    return pairs


def probe_hub(url: str, project_id: str) -> bool:
    """Never send credentials before identifying the local hub."""
    try:
        response = httpx.get(f'{url}/health', timeout=2, trust_env=False)
    except httpx.ConnectError:
        return False
    except httpx.HTTPError:
        raise click.ClickException('El puerto no responde correctamente; comprueba el hub o elige otro puerto') from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not isinstance(data, dict) or data.get('project_id') != project_id:
        raise click.ClickException('El puerto está ocupado por otro servicio o proyecto; usa --port con otro puerto')
    return True


def client_config(agent: str, url: str) -> dict:
    config = load_config()
    return {
        'command': sys.executable,
        'args': ['-m', 'agent_bus.cli.main', 'mcp-server'],
        'env': {
            'AGENT_BUS_PROJECT_ROOT': config.project_root,
            'AGENT_BUS_CONFIG_DIR': str(get_config_dir()),
            'AGENT_BUS_DATABASE_PATH': config.database_path,
            'AGENT_BUS_PROJECT_ID': config.bus.project_id,
            'AGENT_BUS_AGENT_ID': agent,
            'AGENT_BUS_SESSION_FILE': str(get_config_dir() / 'credentials' / f'{agent}.json'),
            'AGENT_BUS_URL': url,
            'AGENT_BUS_ALLOW_UNSIGNED': '0',
        },
    }


def onboard_mcp(agents: str | None, admin: str, port: int | None, yes: bool, run) -> None:
    # Explicit overrides belong to advanced/manual setup. Avoid provisioning into
    # an inherited project's database while displaying the current directory.
    overrides = ['AGENT_BUS_CONFIG_DIR', 'AGENT_BUS_DATABASE_PATH', 'AGENT_BUS_PROJECT_ID',
                 'AGENT_BUS_URL', 'AGENT_BUS_SESSION_FILE']
    if any(key in os.environ for key in overrides) or os.environ.get('AGENT_BUS_ALLOW_UNSIGNED') == '1':
        raise click.ClickException('Ejecuta onboard --mcp-only sin overrides AGENT_BUS de otro entorno; usa --project para seleccionar el proyecto')
    pairs = parse_agents(agents or 'backend:claude,qa:codex')
    try:
        validate_id(admin)
    except AuthenticationError:
        raise click.ClickException('Identidad admin inválida') from None
    if admin == 'free' or admin in [name for name, _ in pairs]:
        raise click.ClickException('La identidad admin debe ser distinta de los agentes')
    root = resolve_project_root() or Path.cwd()
    existing = load_project_config()
    url = existing.get('bus_url', 'http://127.0.0.1:8421')
    if port is not None:
        requested = f'http://127.0.0.1:{port}'
        if existing.get('bus_url') and existing['bus_url'] != requested:
            raise click.ClickException('El proyecto ya tiene otro hub configurado; no se cambiará durante onboarding')
        url = requested
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise click.ClickException('El asistente MCP requiere un hub local http://127.0.0.1:puerto')
    click.echo(f'Proyecto: {root}\nHub: {url}')
    click.echo('Agentes: ' + ', '.join(name for name, _ in pairs) + f'; admin: {admin}')
    click.echo('Se crearán credenciales locales y configuraciones MCP; no se inician workers ni merges.')
    if not yes and not click.confirm('¿Preparar este proyecto?', default=True):
        raise click.Abort()
    ready = probe_hub(url, existing.get('project_id') or project_identity(root))
    # init_project preserves existing instructions and configuration. Unlike the
    # general init command, it does not regenerate agent protocol documents.
    marker = init_project()
    import yaml
    settings = yaml.safe_load((marker / 'config.yaml').read_text())
    settings['bus_url'] = url
    (marker / 'config.yaml').write_text(yaml.safe_dump(settings, sort_keys=False))
    config = load_config()
    sessions = []
    for name, provider, role in [*( (n, p, 'agent') for n, p in pairs), (admin, 'human', 'admin')]:
        path = get_config_dir() / 'credentials' / f'{name}.json'
        if not path.exists():
            run('auth', 'create', '--agent', name, '--provider', provider, '--role', role)
        try:
            session = load_session(name, session_file=path)
        except AuthenticationError:
            raise click.ClickException(f'Credencial inválida o vencida: {path}. Revísala con la guía de autenticación; no se sobrescribe.') from None
        if session['role'] != role:
            raise click.ClickException(f'La credencial de {name} no tiene el rol requerido: {role}')
        sessions.append(session)
    if not ready:
        run('serve', '--daemon')
    deadline = time.monotonic() + 10
    while not probe_hub(url, config.bus.project_id):
        if time.monotonic() >= deadline:
            raise click.ClickException(f'El hub no arrancó. Revisa {get_config_dir() / "bus.log"}')
        time.sleep(.2)
    for session in sessions:
        try:
            response = httpx.get(f'{url}/auth/me', headers={
                'Authorization': f'Bearer {session["token"]}',
                'X-Agent-Bus-Project': config.bus.project_id,
            }, timeout=5, trust_env=False)
            me = response.json()
            valid = response.status_code == 200 and me.get('agent_id') == session['agent_id'] and me.get('role') == session['role']
        except (httpx.HTTPError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise click.ClickException(f'El hub rechazó la sesión de {session["agent_id"]}. Comprueba revocación y proyecto; no se regeneran credenciales automáticamente.')
    output = get_config_dir() / 'mcp'
    output.mkdir(parents=True, exist_ok=True)
    for name, _ in pairs:
        target = output / f'{name}.json'
        content = json.dumps({'mcpServers': {'agent-bus': client_config(name, get_bus_url())}}, indent=2) + '\n'
        if target.exists() and target.read_text() != content:
            raise click.ClickException(f'Ya existe una configuración diferente: {target}; consérvala o muévela antes de repetir')
        if not target.exists():
            with target.open('x') as stream:
                stream.write(content)
        click.echo(f'MCP de {name}: {target}')
    click.echo(f'Hub y {len(sessions)} sesiones verificados. Consola: {url}/console')
    click.echo('Copia la configuración del agente a tu cliente MCP y reconecta. Primera llamada: bootstrap_agent({}).')
    click.echo('Cada aplicación necesita su propia identidad. No compartas los archivos credentials/.')
    click.echo('La configuración usa este Python instalado: conserva su entorno; para uso permanente instala con uv tool install.')
