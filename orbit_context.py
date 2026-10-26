"""Conditional Orbit guidance; facts remain in the repository and local vault."""

import hashlib
import os
from pathlib import Path
import re
import tempfile
import json


REPOSITORY = 'ROKT/launch-engineering'
REFERENCE = Path('/Users/jklee/.codex/skills/avengers-falcon/references/orbit.md')
VAULT = Path('/Users/jklee/Documents/rokt-vault/repos/launch-engineering')
ROUTER = Path('/Users/jklee/Documents/ai_center/claude/rokt-repos.CLAUDE.md')
TOPICS = {f'wiki/{name}.md' for name in ('README', 'orbit-overview', 'orbit-architecture',
          'orbit-status-and-timeline', 'orbit-open-questions', 'orbit-agent-workflow', 'orbit-sources')}


def select(repo, changes, patches):
    if repo != REPOSITORY:
        return []
    affected = []
    for change in changes:
        paths = {change['filename'], change.get('previous_filename', change['filename'])}
        if any(path.startswith('Orbit/') for path in paths):
            affected.extend(sorted(paths))
            continue
        shared = any(path.startswith(('.buildkite/', '.github/workflows/', 'infra/'))
                     or path in {'Dockerfile', 'Makefile', 'justfile', 'pyproject.toml', 'docker-compose.yml'} for path in paths)
        # A changed executable/configuration line must explicitly name Orbit's directory.
        if shared and any(line[:1] in {'+', '-'} and not line.startswith(('+++', '---'))
                          and not line[1:].lstrip().startswith(('#', '//', ';')) and 'Orbit/' in line
                          for line in patches[change['filename']]):
            affected.extend(sorted(paths))
    return sorted(set(affected))


def architecture_sections(paths):
    sections = {'layout'}
    names = ' '.join(paths).lower()
    if any(word in names for word in ('sources/', 'worker', 'rounds', 'rules')):
        sections.update({'connectors', 'auth'})
    if any(word in names for word in ('evidence', 'migration', 'contract', 'blob', 'storage')):
        sections.update({'schema rules', 'auth'})
    if any(word in names for word in ('api/', 'commands', 'tickets', 'policy', 'web/')):
        sections.update({'the write path', 'auth'})
    if any(word in names for word in ('infra/', '.buildkite/', 'dockerfile')):
        sections.add('auth')
    return sections


def excerpt(text, sections):
    selected, include = [], True
    for line in text.splitlines():
        if line.startswith('## '):
            include = any(line[3:].lower().startswith(section) for section in sections)
        if include:
            selected.append(line)
    return '\n'.join(selected)


def read_local(evidence, path, kind, *, sections=None):
    key = str(path)
    if key not in evidence.local_documents:
        if path.is_symlink():
            raise ValueError('Context symlinks are not permitted.')
        raw = path.read_text()
        if len(raw) > 250_000:
            raise ValueError('Local context exceeds the review limit.')
        evidence.local_documents[key] = raw
    raw = evidence.local_documents[key]
    included = excerpt(raw, sections) if sections else raw
    match = re.search(r'^compiled:\s*([0-9a-f]{40})\s*$', raw, re.MULTILINE)
    if not match and kind == 'review_reference':
        match = re.search(r'Baseline inspected[^\n]*main `([0-9a-f]{40})`', raw)
    compiled = match[1] if match else None
    main = evidence.context_refs.get(evidence.repo + ':main')
    record = {'path': key, 'kind': kind, 'sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'included_sha256': hashlib.sha256(included.encode()).hexdigest(), 'compiled_sha': compiled,
              'freshness': 'matches_main' if compiled and compiled == main else 'different_from_main' if compiled else 'unversioned'}
    evidence.provenance = [row for row in evidence.provenance if row['path'] != key] + [record]
    if kind == 'review_reference':
        evidence.references['orbit'] = included
    elif kind == 'local_guidance':
        evidence.instructions[key] = included
    else:
        evidence.documents['local-context:' + key] = included
    evidence.check_budget()


def load(evidence, github, affected):
    evidence.pin_main(github, evidence.repo)
    evidence.scope = {'profile': 'orbit', 'affected_paths': affected,
                      'topic_requests': sorted(TOPICS)}
    read_local(evidence, REFERENCE, 'review_reference')
    read_local(evidence, ROUTER, 'local_guidance')
    read_local(evidence, VAULT / 'AGENTS.md', 'local_guidance')
    for path in ('wiki/README.md', 'wiki/orbit-status-and-timeline.md', 'wiki/orbit-open-questions.md'):
        read_local(evidence, VAULT / path, 'wiki_summary')
    read_local(evidence, VAULT / 'wiki/orbit-architecture.md', 'wiki_summary', sections=architecture_sections(affected))


def topic(evidence, path):
    if evidence.repo != REPOSITORY or not evidence.references.get('orbit') or path not in TOPICS:
        raise ValueError('Only listed Orbit topic pages may be requested for an Orbit review.')
    read_local(evidence, VAULT / path, 'wiki_summary')


def audit(directory, evidence):
    directory = Path(directory) / 'context'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = evidence.job_id or 'snapshot'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        raise ValueError('Invalid context audit job identifier.')
    data = {'head': evidence.head, 'base': evidence.base, 'merge_base': evidence.ancestor,
            'main': evidence.context_refs.get(evidence.repo + ':main'), 'scope': evidence.scope,
            'profiles': sorted(evidence.references), 'sources': evidence.provenance,
            'context_refs': evidence.context_refs, 'dependencies': evidence.dependencies,
            'documents': sorted(evidence.documents)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as output:
            temporary = output.name
            json.dump(data, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / (name + '-' + evidence.head + '.json'))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
