"""Install the single local LaunchAgent after a successful live pilot review."""

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

from agent import Config, ConfigError, ROOT, RUNTIME, Store
from auth import CredentialManager


LABEL = 'com.jklee.launch-eng-review-agent'


def definition(root=ROOT):
    root = Path(root).resolve()
    return {'Label': LABEL, 'ProgramArguments': [str(root / '.venv/bin/python'), str(root / 'agent.py'), 'run'],
            'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30, 'ExitTimeOut': 50, 'ProcessType': 'Background', 'Nice': 10,
            'LowPriorityIO': True, 'Umask': 0o077,
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                                     'HOME': str(Path.home())},
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['render', 'install', 'stop', 'status'])
    args = parser.parse_args()
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    target = f'gui/{os.getuid()}/{LABEL}'
    try:
        if args.command == 'render':
            print(plistlib.dumps(definition()).decode(), end='')
            return 0
        if args.command == 'stop':
            return subprocess.run(['/bin/launchctl', 'bootout', target], capture_output=True).returncode
        if args.command == 'status':
            result = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, text=True)
            if result.returncode:
                print('LaunchAgent is not loaded.')
            else:
                for line in result.stdout.splitlines():
                    if line.strip().startswith(('state =', 'pid =', 'last exit code =')):
                        print(line.strip())
            return result.returncode
        config = Config.load()
        if not all(shutil.which(tool) for tool in ('gh', 'codex')):
            raise ConfigError('Existing gh and Codex CLI must be on the LaunchAgent PATH.')
        if CredentialManager().status().get('status') != 'ready':
            raise ConfigError('Verify silent Google renewal before background installation.')
        store = Store()
        try:
            if store.meta('chat_blocked') or not store.db.execute(
                "select 1 from jobs join ledger using(repo,number,head) where jobs.status='submitted' and ledger.actor=? limit 1",
                    (config.actor,)).fetchone():
                raise ConfigError('Complete one real coworker pilot request before enabling automatic startup.')
        finally:
            store.db.close()
        content = plistlib.dumps(definition())
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != content:
            raise ConfigError('An existing different LaunchAgent was preserved; inspect it before replacement.')
        path.write_bytes(content)
        path.chmod(0o600)
        result = subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], capture_output=True)
        if result.returncode:
            raise ConfigError('LaunchAgent did not bootstrap; stop the foreground pilot and inspect launchctl status.')
        print('Installed one local LaunchAgent. Activity logs rotate under .runtime; the laptop is not kept awake.')
        return 0
    except (ConfigError, OSError) as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
