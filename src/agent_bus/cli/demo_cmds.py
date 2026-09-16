"""Packaged, isolated coordination demo. Actors are scripted; transports are real."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path

import click
import httpx
from mcp import Client, StdioServerParameters, stdio_client


SMOKE = '''import unittest
from cart import total
class Smoke(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(total(2, 10), 20)
'''
REVIEW = '''import unittest
from cart import total
class Review(unittest.TestCase):
    def test_negative(self):
        with self.assertRaises(ValueError):
            total(-1, 10)
'''
INITIAL = 'def total(quantity, unit_price):\n    return quantity * unit_price\n'
FIXED = ('def total(quantity, unit_price):\n'
         '    if quantity < 0:\n        raise ValueError("quantity must be nonnegative")\n'
         '    return quantity * unit_price\n')


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise click.ClickException(message)


def _environment(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('AGENT_BUS_') and k != 'AGENT_ID'}
    env.update(AGENT_BUS_PROJECT_ROOT=str(root), AGENT_BUS_ALLOW_UNSIGNED='0',
               PYTHONDONTWRITEBYTECODE='1')
    return env


def _cli(root: Path, env: dict, *args: str) -> None:
    result = subprocess.run([sys.executable, '-m', 'agent_bus.cli.main', *args],
                            cwd=root, env=env, capture_output=True, timeout=20)
    _check(result.returncode == 0, 'No se pudo preparar la demo: ' + ' '.join(args[:2]))


async def _scenario(root: Path, url: str, env: dict, sessions: dict, report: dict, yes: bool):
    started = time.monotonic()

    def step(name, text):
        report['steps'].append({'name': name, 'seconds': round(time.monotonic() - started, 3)})
        click.echo(f'  ✓ {text}')

    def run_tests(*modules):
        result = subprocess.run([sys.executable, '-m', 'unittest', *modules], cwd=root,
                                env=env, text=True, capture_output=True, timeout=15)
        evidence = {'command': 'python -m unittest ' + ' '.join(modules),
                    'returncode': result.returncode,
                    'output': (result.stdout + result.stderr).replace(str(root), '<demo>')}
        report['test_runs'].append(evidence)
        return evidence

    async def tool(client, name, arguments):
        report['mcp_calls'] += 1
        result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=15)
        _check(not result.is_error, f'Falló la herramienta {name}; demo detenida')
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(next(block.text for block in result.content if block.type == 'text'))

    async with httpx.AsyncClient(base_url=url, headers={
        'Authorization': 'Bearer ' + sessions['human']['token'],
        'X-Agent-Bus-Project': sessions['human']['project_id'],
    }, timeout=10, trust_env=False) as admin, AsyncExitStack() as stack:
        async def http(method, path, body=None):
            response = await admin.request(method, path, json=body)
            _check(response.is_success, f'Falló {method} {path}: HTTP {response.status_code}')
            return response.json()

        clients = {}
        for name in ('backend', 'qa'):
            child_env = {**env, 'AGENT_BUS_AGENT_ID': name,
                         'AGENT_BUS_SESSION_FILE': str(root / '.agent-bus/runtime/credentials' / f'{name}.json')}
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'agent_bus.cli.main', 'mcp-server'], env=child_env, cwd=str(root))
            log = stack.enter_context((root / f'mcp-{name}.log').open('w'))
            clients[name] = await stack.enter_async_context(Client(stdio_client(params, errlog=log)))
            state = await tool(clients[name], 'bootstrap_agent', {'display_name': f'Demo {name} (guion)'})
            _check(state['agent_id'] == name, 'Identidad MCP inesperada')
        backend, qa = clients['backend'], clients['qa']
        step('connected', 'Backend y QA conectados por MCP con identidades independientes')
        for task_id, title in [('DEMO-BACKEND', 'Implementar total'), ('DEMO-QA', 'Revisar cantidades negativas')]:
            await http('POST', '/room/api/tasks', {'task_id': task_id, 'title': title,
                'acceptance_criteria': ['Rechazar cantidades negativas con ValueError']})
        await tool(backend, 'claim_task', {'task_id': 'DEMO-BACKEND'})
        await tool(qa, 'claim_task', {'task_id': 'DEMO-QA'})
        edit = await tool(backend, 'prepare_edit', {'paths': ['cart.py'], 'operation_key': 'backend-edit'})
        _check(edit['authorized'], 'Edición no autorizada')
        report['mcp_calls'] += 1
        conflict = await asyncio.wait_for(qa.call_tool('prepare_edit', {
            'paths': ['qa_notes.md', 'cart.py'], 'operation_key': 'qa-conflict'}), timeout=15)
        _check(conflict.is_error and conflict.structured_content.get('http_status') == 409,
               'No se detectó el conflicto esperado')
        locks = await http('GET', '/locks')
        _check(not any(lock['locked_by'] == 'qa' for lock in locks), 'Reserva parcial inesperada')
        report['conflict_http_status'] = 409
        report['partial_locks_after_conflict'] = 0
        step('conflict', 'QA recibe conflicto 409; no obtiene una reserva parcial ni edita el archivo')
        (root / 'cart.py').write_text(INITIAL)
        smoke = run_tests('test_smoke')
        _check(smoke['returncode'] == 0, 'Falló la prueba básica de la demo')
        lock = edit['locks'][0]
        delivery_args = {'task_id': 'DEMO-BACKEND', 'to_agent': 'qa', 'summary': 'Implementación lista para revisión',
            'operation_key': 'first-delivery', 'files_touched': ['cart.py'],
            'validation_commands': [smoke['command']], 'validation_summary': smoke['output'],
            'release_locks': [{key: lock[key] for key in ('file_path', 'scope', 'acquisition_id')}]}
        delivery = await tool(backend, 'complete_handoff', delivery_args)
        replay = await tool(backend, 'complete_handoff', delivery_args)
        _check(replay['replayed'] and replay['message_id'] == delivery['message_id'], 'La entrega se duplicó')
        report['handoff_replay_same_message'] = True
        step('delivered', 'Backend entrega evidencia y libera su reserva; repetir no duplica la entrega')
        pending = await tool(qa, 'my_pending_items', {})
        _check(any(m['message_id'] == delivery['message_id'] for m in pending['messages']), 'QA no recibió la entrega')
        failed = run_tests('test_review')
        _check(failed['returncode'] != 0 and 'ValueError not raised' in failed['output'], 'No se reprodujo el fallo esperado de QA')
        await tool(qa, 'reply_message', {'message_id': delivery['message_id'],
            'text': 'La revisión falla: una cantidad negativa debe producir ValueError.',
            'idempotency_key': 'qa-review-failed', 'acknowledge': True})
        step('review_failed', 'QA ejecuta el caso límite: falla de verdad y devuelve la revisión al backend')
        await tool(qa, 'post_message', {'to_agent': 'human', 'text': '¿Autorizar la corrección para rechazar cantidades negativas?',
            'related_task': 'DEMO-BACKEND', 'reply_needed': True, 'idempotency_key': 'human-decision'})
        approvals = await http('GET', '/room/api/pending-approvals')
        _check(len(approvals) == 1, 'Solicitud humana ausente o duplicada')
        waiting = time.monotonic()
        if yes:
            approved = True
            click.echo('  → Aprobación simulada por --yes; no se cuenta como intervención humana.')
        else:
            approved = await asyncio.to_thread(click.confirm,
                '  ¿Autorizar la corrección que rechaza cantidades negativas?', default=True)
        report['human_wait_seconds'] = round(time.monotonic() - waiting, 3)
        report['human_interventions'] = 0 if yes else 1
        report['decision'] = 'approve' if approved else 'reject'
        await http('POST', '/room/api/approve', {'message_id': approvals[0]['message_id'],
            'decision': report['decision'], 'note': 'Decisión simulada (--yes)' if yes else 'Decisión del operador en la terminal'})
        decision_inbox = await tool(qa, 'my_pending_items', {})
        decision = next((m for m in decision_inbox['messages'] if m['body'].get('type') == 'approval_decision'), None)
        _check(decision is not None and decision['body']['decision'] == report['decision'], 'La decisión no llegó a QA')
        await tool(qa, 'ack_messages', {'message_ids': [decision['message_id']]})
        step('human_decision', 'La decisión llega al inbox de QA con correlación al mensaje original')
        if not approved:
            report['status'] = 'declined'
            step('declined', 'Se respeta el rechazo: no se aplica la corrección ni se declara la tarea completada')
            return
        await tool(qa, 'post_message', {'to_agent': 'backend',
            'text': 'El operador autorizó corregir el caso negativo; procede con una reserva nueva.',
            'related_task': 'DEMO-BACKEND', 'idempotency_key': 'approved-correction'})
        pending_backend = await tool(backend, 'my_pending_items', {})
        edit = await tool(backend, 'prepare_edit', {'paths': ['cart.py'], 'operation_key': 'backend-correction'})
        _check(edit['authorized'], 'Corrección sin reserva')
        (root / 'cart.py').write_text(FIXED)
        passed = run_tests('test_smoke', 'test_review')
        _check(passed['returncode'] == 0, 'La corrección no superó las pruebas')
        lock = edit['locks'][0]
        second = await tool(backend, 'complete_handoff', {'task_id': 'DEMO-BACKEND', 'to_agent': 'qa',
            'summary': 'Corregido: pruebas básica y de borde aprobadas', 'operation_key': 'corrected-delivery',
            'files_touched': ['cart.py'], 'validation_commands': [passed['command']], 'validation_summary': passed['output'],
            'acknowledge_message_ids': [m['message_id'] for m in pending_backend['messages']],
            'release_locks': [{key: lock[key] for key in ('file_path', 'scope', 'acquisition_id')}]})
        reviewed = run_tests('test_smoke', 'test_review')
        _check(reviewed['returncode'] == 0, 'QA no pudo verificar la corrección')
        await tool(qa, 'reply_message', {'message_id': second['message_id'], 'text': 'QA verificó ambas pruebas: aprobado.',
            'idempotency_key': 'qa-review-passed', 'acknowledge': True})
        final_inbox = await tool(backend, 'my_pending_items', {})
        await tool(backend, 'ack_messages', {'message_ids': [m['message_id'] for m in final_inbox['messages']]})
        await tool(backend, 'complete_task', {'task_id': 'DEMO-BACKEND'})
        await tool(qa, 'complete_task', {'task_id': 'DEMO-QA'})
        overview = await http('GET', '/room/api/overview')
        _check(all(t['status'] == 'done' for t in overview['tasks']) and not overview['locks'], 'Cierre incompleto')
        report['final_tasks'] = {t['task_id']: t['status'] for t in overview['tasks']}
        report['remaining_locks'] = len(overview['locks'])
        report['final_source'] = (root / 'cart.py').read_text()
        report['status'] = 'completed'
        step('completed', 'Corrección revisada: dos tareas completadas, sin reservas activas')


def run_demo(yes: bool) -> dict:
    started = time.monotonic()
    report = {'schema_version': 1, 'mode': 'scripted_actors_real_mcp', 'status': 'running',
              'decision_source': 'simulated' if yes else 'terminal_operator',
              'model_calls': 0, 'model_cost_usd': None, 'steps': [], 'test_runs': [], 'mcp_calls': 0}
    with tempfile.TemporaryDirectory(prefix='agent-bus-demo-') as directory:
        root = Path(directory)
        env = _environment(root)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        _cli(root, env, 'init', '--bus-url', url)
        for name, role in [('backend', 'agent'), ('qa', 'agent'), ('human', 'admin')]:
            _cli(root, env, 'auth', 'create', '--agent', name, '--role', role)
        sessions = {name: json.loads((root / '.agent-bus/runtime/credentials' / f'{name}.json').read_text())
                    for name in ('backend', 'qa', 'human')}
        (root / 'test_smoke.py').write_text(SMOKE)
        (root / 'test_review.py').write_text(REVIEW)
        with (root / 'hub.log').open('w') as log:
            hub = subprocess.Popen([sys.executable, '-m', 'agent_bus.cli.main', 'serve'],
                                   cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    _check(hub.poll() is None, 'El hub temporal no pudo arrancar')
                    try:
                        response = httpx.get(url + '/health', timeout=.5, trust_env=False)
                        if response.status_code == 200:
                            _check(response.json().get('project_id') == sessions['human']['project_id'], 'Puerto ocupado por otro proyecto')
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.1)
                else:
                    raise click.ClickException('El hub temporal no respondió a tiempo')
                asyncio.run(_scenario(root, url, env, sessions, report, yes))
            finally:
                hub.terminate()
                try:
                    hub.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    hub.kill()
                    hub.wait(timeout=5)
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report['temporary_project_removed'] = True
    return report


@click.command('demo')
@click.option('--yes', is_flag=True, help='Simular la aprobación para automatización; no cuenta como decisión humana.')
@click.option('--report', type=click.Path(path_type=Path, dir_okay=False), help='Guardar evidencia JSON sin secretos; no sobrescribe archivos.')
def demo(yes: bool, report: Path | None):
    """Probar coordinación con dos actores programados, MCP real y un proyecto temporal."""
    if report and report.exists():
        raise click.ClickException('El informe ya existe; elige otro nombre')
    if report and not report.parent.is_dir():
        raise click.ClickException('El directorio del informe no existe')
    click.echo('Demo guiada: backend y QA programados; MCP, HTTP, SQLite y pruebas reales. No usa modelos IA.')
    try:
        result = run_demo(yes)
    except (OSError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        raise click.ClickException('No se pudo completar la demo: ' + type(exc).__name__) from None
    if report:
        try:
            with report.open('x') as stream:
                json.dump(result, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
        except OSError:
            raise click.ClickException('No se pudo guardar el informe sin sobrescribir archivos') from None
        click.echo(f'Informe: {report}')
    click.echo(f'Demo {result["status"]} en {result["elapsed_seconds"]:.1f} s. Proyecto temporal eliminado.')
