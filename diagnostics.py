"""Small, content-free diagnostics shared by the review worker's threads."""

from contextvars import ContextVar
import json
import logging
import re
import time


CONTEXT = ContextVar('review_diagnostics', default=None)
LOG = logging.getLogger('review-agent')


def emit(event, *, warning=False, **fields):
    context = CONTEXT.get() or {}
    job = context.get('job') or {}
    record = {key: value for key, value in {
        'job_id': job.get('id'), 'repo': job.get('repo'), 'pr': job.get('number'),
        'head': job.get('head'), 'base': job.get('base'), 'worker_id': job.get('owner'),
        'stage': context.get('stage'), 'run_id': context.get('run_id'),
    }.items() if value is not None}
    record.update(event=event, **fields)
    LOG.log(logging.WARNING if warning else logging.INFO, '%s', json.dumps(record, sort_keys=True))


def elapsed(start):
    return round((time.monotonic() - start) * 1000)


def endpoint_name(endpoint):
    # Only known API routes; never retain arbitrary paths, query values or payload filenames.
    path = endpoint.partition('?')[0]
    sha = r'[0-9a-f]{40}'
    route = (r'user|repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/(?:'
             r'pulls/[0-9]+(?:/(?:reviews|comments|files))?|'
             rf'commits/{sha}/(?:status|check-runs)|compare/{sha}\.\.\.{sha}|'
             rf'git/(?:trees|blobs)/{sha}|git/ref/heads/main))?')
    return '/' + path if re.fullmatch(route, path) else '[unrecognized endpoint]'


def error_category(raw, http_status=None):
    """Classify locally and discard the raw provider/CLI text."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    text = raw.lower()
    if http_status == 429 or any(value in text for value in (
            'rate limit', 'too many requests', 'usage limit', 'quota exceeded', 'insufficient_quota')):
        return 'rate_limited'
    if http_status == 401 or any(value in text for value in (
            'unauthorized', 'authentication failed', 'token expired', 'refresh token', 'not logged in')):
        return 'authentication'
    if http_status == 403:
        return 'forbidden'
    if http_status == 404:
        return 'not_found'
    if http_status and http_status >= 500:
        return 'server_error'
    if any(value in text for value in ('error connecting', 'connection reset', 'connection refused',
                                      'no such host', 'network is unreachable', 'tls handshake')):
        return 'network'
    return 'unknown'
