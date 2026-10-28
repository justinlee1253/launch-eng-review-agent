"""Private, atomic review drafts. A retry must not discard completed analysis."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


class DraftError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def snapshot(evidence):
    value = {key: data for key, data in vars(evidence).items() if key != 'local_documents'}
    value['inline'] = sorted(evidence.inline)
    return copy.deepcopy(value)


class Drafts:
    def __init__(self, runtime):
        self.directory = Path(runtime) / 'drafts'
        if self.directory.is_symlink():
            raise DraftError('Review draft directory cannot be a symlink.')
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
        except OSError:
            raise DraftError('Private review draft storage is unavailable; no review may be published.') from None

    def prefix(self, repo, number, head):
        if not re.fullmatch('[0-9a-f]{40}', head) or type(number) is not int or number < 1:
            raise DraftError('Review draft identity is invalid.')
        return hashlib.sha256(f'{repo}#{number}'.encode()).hexdigest()[:16] + '-' + head + '-'

    def create(self, evidence, output, *, progress=None, instructions_hash=None):
        record = {'id': self.prefix(evidence.repo, evidence.number, evidence.head) + uuid.uuid4().hex,
                  'version': 1, 'repo': evidence.repo, 'number': evidence.number,
                  'head': evidence.head, 'base': evidence.base, 'snapshot': snapshot(evidence),
                  'original': copy.deepcopy(output), 'current': copy.deepcopy(output),
                  'issues': [], 'history': [], 'repair_calls': 0, 'status': 'draft'}
        if progress is not None:
            record.update(progress=copy.deepcopy(progress), instructions_hash=instructions_hash)
        self.save(record)
        return record

    def save(self, record):
        if not re.fullmatch(r'[0-9a-f]{16}-[0-9a-f]{40}-[0-9a-f]{32}', record.get('id', '')):
            raise DraftError('Review draft filename is invalid.')
        value = {key: data for key, data in record.items() if key != 'checksum'}
        value['checksum'] = digest(value)
        path = self.directory / (record['id'] + '.json')
        if path.is_symlink():
            raise DraftError('Review draft cannot be a symlink.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as output:
                temporary = output.name
                json.dump(value, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except (OSError, ValueError, TypeError):
            raise DraftError('Review draft could not be saved; no review may be published.') from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def latest(self, repo, number, head, base, *, include_progress=False):
        paths = sorted(self.directory.glob(self.prefix(repo, number, head) + '*.json'),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths:
            try:
                if path.is_symlink():
                    raise ValueError()
                with path.open() as source:
                    value = json.load(source)
                if not isinstance(value, dict):
                    raise ValueError()
                checksum = value.pop('checksum')
                if (checksum != digest(value) or value['version'] != 1 or path.stem != value['id']
                        or (value['repo'], value['number'], value['head']) != (repo, number, head)):
                    raise ValueError()
                if not isinstance(value['original'], dict):
                    raise ValueError()
                status = value['original'].get('status')
                if status == 'in_progress' and not isinstance(value.get('progress'), dict):
                    raise ValueError()
                if value['base'] == base and (status == 'complete' or include_progress and status == 'in_progress'):
                    return value
            except (OSError, ValueError, TypeError, KeyError):
                raise DraftError('Saved review draft is unreadable or corrupt; preserved without restarting analysis.') from None
        return None


def apply_corrections(original, response, issues):
    """The model can correct citations, never findings, severity or the verdict."""
    if not isinstance(response, dict) or set(response) != {'corrections', 'unresolved'}:
        raise DraftError('Citation correction returned an invalid response.')
    if not isinstance(response['corrections'], list) or not isinstance(response['unresolved'], list):
        raise DraftError('Citation correction returned invalid lists.')
    allowed = {row['finding_index'] for row in issues}
    seen = set()
    output = copy.deepcopy(original)
    for row in response['corrections']:
        if not isinstance(row, dict) or set(row) != {'finding_index', 'path', 'side', 'line', 'evidence'}:
            raise DraftError('Citation correction tried to change fields outside the citation.')
        index = row['finding_index']
        if type(index) is not int or index not in allowed or index in seen:
            raise DraftError('Citation correction did not match the retained finding indexes.')
        seen.add(index)
        output['findings'][index].update({key: row[key] for key in ('path', 'side', 'line', 'evidence')})
    for row in response['unresolved']:
        if (not isinstance(row, dict) or set(row) != {'finding_index', 'reason'}
                or type(row['finding_index']) is not int or row['finding_index'] not in allowed
                or row['finding_index'] in seen or not isinstance(row['reason'], str) or not row['reason'].strip()):
            raise DraftError('Citation correction returned conflicting or invalid uncertainty.')
        seen.add(row['finding_index'])
    return output
