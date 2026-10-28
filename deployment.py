"""Offline ownership handoff and fail-closed health for one persistent runtime."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path


class DeploymentError(Exception):
    pass


def runtime_path(root):
    value = os.environ.get('REVIEW_AGENT_RUNTIME')
    if value and not Path(value).expanduser().is_absolute():
        raise DeploymentError(
            'REVIEW_AGENT_RUNTIME must be an absolute persistent directory.'
        )
    return Path(value).expanduser().resolve() if value else Path(root) / '.runtime'


def host_id():
    try:
        machine = Path('/etc/machine-id').read_text().strip()
    except OSError:
        machine = ''
    return hashlib.sha256((socket.gethostname() + ':' + machine).encode()).hexdigest()[
        :24
    ]


def read_json(path):
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise TypeError()
        return result
    except (OSError, ValueError, TypeError):
        raise DeploymentError(
            'Required runtime state is missing or invalid; restore the persistent runtime. Never reconfigure it to recover.'
        ) from None


def state(directory):
    directory = Path(directory)
    config = read_json(directory / 'config.json')
    database = directory / 'state.sqlite3'
    if not database.is_file():
        raise DeploymentError(
            'Runtime database is missing; restore it before startup. No empty queue was created.'
        )
    try:
        with contextlib.closing(
            sqlite3.connect(
                database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1
            )
        ) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        if not {
            'metadata',
            'jobs',
            'sources',
            'outbox',
            'ledger',
            'events',
            'model_runs',
        }.issubset(tables):
            raise ValueError()
    except (sqlite3.Error, ValueError):
        raise DeploymentError(
            'Runtime database schema is missing or invalid; restore it. No empty queue was created.'
        ) from None
    return config


def check_identity(record, config):
    if (
        record.get('version') != 1
        or record.get('persistent') is not True
        or record.get('space') != config.get('space')
        or record.get('actor') != config.get('actor')
    ):
        raise DeploymentError(
            'Deployment identity or persistent-storage declaration does not match this runtime.'
        )


def verify(directory):
    directory = Path(directory)
    config = state(directory)
    record = read_json(directory / 'deployment.json')
    check_identity(record, config)
    if record.get('retired') is not False or record.get('host') != host_id():
        raise DeploymentError(
            'This runtime belongs to another host or is retired. Complete the offline handoff; do not initialize a second listener.'
        )
    return config


def guard(directory, required=False):
    if required or (Path(directory) / 'deployment.json').exists():
        verify(directory)


@contextlib.contextmanager
def offline(directory):
    with (Path(directory) / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError(
                'Worker is still running. Drain and stop its supervisor before handoff; no process was interrupted.'
            ) from None
        yield


def save(directory, record):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=directory, prefix='.deployment-')
        with os.fdopen(fd, 'w') as output:
            json.dump(record, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, Path(directory) / 'deployment.json')
        temporary = None
        # Persist retirement before any copy can enable another host.
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary:
            os.unlink(temporary)


def initialize(directory, *, persistent=False, single_host=False):
    directory = Path(directory)
    if not persistent or not single_host:
        raise DeploymentError(
            'Explicit persistent-storage and single-host confirmation are required.'
        )
    config = state(directory)
    with offline(directory):
        if (directory / 'deployment.json').exists():
            raise DeploymentError(
                'Deployment ownership already exists; use retire/activate for a handoff.'
            )
        save(
            directory,
            {
                'version': 1,
                'persistent': True,
                'host': host_id(),
                'retired': False,
                'space': config['space'],
                'actor': config['actor'],
            },
        )


def retire(directory, target):
    if not target or target == host_id():
        raise DeploymentError('Choose a different target host ID.')
    with offline(directory):
        verify(directory)
        record = read_json(Path(directory) / 'deployment.json')
        save(directory, dict(record, retired=True, target=target))


def activate(directory, *, source_fenced=False):
    config = state(directory)
    with offline(directory):
        record = read_json(Path(directory) / 'deployment.json')
        check_identity(record, config)
        planned = record.get('retired') is True and record.get('target') == host_id()
        recovered = source_fenced and record.get('host') != host_id()
        if not planned and not recovered:
            raise DeploymentError(
                'No retired-source handoff targets this host; no listener was enabled.'
            )
        save(directory, dict(record, host=host_id(), retired=False, target=None))
        verify(directory)


def health(directory, actor, now=None):
    config = verify(directory)
    if config.get('actor') != actor:
        raise DeploymentError(
            'Configured review actor differs from the authenticated GitHub account.'
        )
    database = (Path(directory) / 'state.sqlite3').resolve()
    try:
        with contextlib.closing(
            sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
        ) as db:
            values = {
                key: json.loads(value)
                for key, value in db.execute(
                    "select key,value from metadata where key in ('heartbeat','chat_blocked','worker_count')"
                )
            }
        age = (time.time() if now is None else now) - datetime.fromisoformat(
            values['heartbeat'].replace('Z', '+00:00')
        ).timestamp()
        if not -5 <= age <= 30 or values.get('chat_blocked'):
            raise ValueError()
        auth = read_json(Path(directory) / 'auth-state.json')
        if auth.get('status') != 'ready' or auth.get('blocked'):
            raise ValueError()
        capacity = values['worker_count']
        if type(capacity) is not int or capacity != config.get('worker_count', 1):
            raise ValueError()
    except (sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        raise DeploymentError(
            'Worker is unhealthy: heartbeat missing/stale, Chat paused, authentication unready, or capacity not loaded.'
        ) from None
    return {'heartbeat': values['heartbeat'], 'worker_count': capacity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'command',
        choices=['host', 'initialize', 'verify', 'retire', 'activate', 'health'],
    )
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--actor')
    parser.add_argument('--target')
    parser.add_argument('--confirm-persistent-storage', action='store_true')
    parser.add_argument('--confirm-single-host', action='store_true')
    parser.add_argument('--confirm-source-fenced', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'host':
            print(host_id())
            return 0
        directory = args.runtime or runtime_path(Path(__file__).parent)
        if args.command == 'initialize':
            initialize(
                directory,
                persistent=args.confirm_persistent_storage,
                single_host=args.confirm_single_host,
            )
        elif args.command == 'retire':
            retire(directory, args.target)
        elif args.command == 'activate':
            activate(directory, source_fenced=args.confirm_source_fenced)
        elif args.command == 'health':
            result = health(directory, args.actor)
            print(
                'healthy: capacity='
                + str(result['worker_count'])
                + ' heartbeat='
                + result['heartbeat']
            )
            return 0
        else:
            verify(directory)
        print('deployment: ' + args.command + ' complete')
        return 0
    except (DeploymentError, OSError) as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
