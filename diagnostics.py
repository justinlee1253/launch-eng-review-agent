"""Small, content-free diagnostics shared by the review worker's threads."""

from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
import logging
import re
import subprocess
import time


CONTEXT = ContextVar('review_diagnostics', default=None)
LOG = logging.getLogger('review-agent')

# Keep provider diagnostics useful without retaining arbitrary response text.
PROVIDER_CODES = ('context_length_exceeded', 'insufficient_quota', 'rate_limit_exceeded',
                  'invalid_api_key', 'model_not_found', 'invalid_json_schema',
                  'invalid_request_error', 'server_error', 'usage_limit_exceeded',
                  'refresh_token_reused', 'refresh_token_expired', 'unsupported_model',
                  'request_too_large', 'input_too_large', 'invalid_model', 'insufficient_permissions')


@lru_cache(maxsize=4)
def cli_version(executable):
    try:
        response = subprocess.run([executable, '--version'], capture_output=True, timeout=5)
        match = re.fullmatch(rb'codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:[-.][a-zA-Z0-9.]+)?)\s*', response.stdout)
        return match[1].decode('ascii') if response.returncode == 0 and match else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def provider_failure(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    code = next((code for code in PROVIDER_CODES if re.search(r'\b' + code + r'\b', raw)), None)
    status = re.search(r'\bHTTP(?:/\d(?:\.\d)?)?\s+([1-5][0-9]{2})\b', raw)
    if not status:
        status = re.search(r'"(?:http_status|status_code|status)"\s*:\s*([1-5][0-9]{2})\b', raw)
    status = int(status[1]) if status else None
    request = re.search(r'\breq_[a-zA-Z0-9]{16,64}\b', raw)
    return {'error_category': error_category(raw, status), 'provider_code': code, 'http_status': status,
            'provider_request_id': request[0] if request else None,
            'error_fingerprint': hashlib.sha256(raw.encode()).hexdigest()}


def failure_summary(detail):
    """Only host-built diagnostic metadata reaches Chat; never provider error text."""
    if detail['service'] == 'github':
        operation = 'GitHub ' + detail['method'] + ' ' + detail['endpoint']
    else:
        operation = 'Model call'
        if detail.get('review_round'):
            operation += ' (evidence round ' + str(detail['review_round']) + ')'
    facts = [detail.get('failure_reason'), detail.get('error_category'), detail.get('provider_code')]
    if detail.get('http_status') is not None:
        facts.append('HTTP ' + str(detail['http_status']))
    if detail.get('exit_code') is not None:
        facts.append('exit ' + str(detail['exit_code']))
    if 'elapsed_ms' in detail:
        facts.append(str(detail['elapsed_ms']) + 'ms')
    if 'prompt_bytes' in detail:
        facts.append(str(detail['prompt_bytes']) + ' prompt bytes')
    if 'structured_output' in detail:
        facts.append('structured output ' + detail['structured_output'])
    return (operation + ' failed: ' + '; '.join(str(value) for value in facts if value)
            + '. Diagnostic: ' + detail['diagnostic_id'])


def emit(event, *, warning=False, **fields):
    context = CONTEXT.get() or {}
    job = context.get('job') or {}
    record = {key: value for key, value in {
        'job_id': job.get('id'), 'repo': job.get('repo'), 'pr': job.get('number'),
        'head': job.get('head'), 'base': job.get('base'), 'worker_id': job.get('owner'),
        'stage': context.get('stage'), 'run_id': context.get('run_id'),
        'source_path': context.get('source_path'), 'source_ref': context.get('source_ref'),
        'source_repository': context.get('source_repository'),
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
    if any(value in text for value in ('context_length_exceeded', 'context window', 'maximum context length',
                                       'request_too_large', 'input_too_large', 'input exceeds the maximum length')):
        return 'context_limit'
    if any(value in text for value in ('invalid_json_schema', 'invalid schema', 'invalid_json')):
        return 'invalid_schema'
    if any(value in text for value in ('model_not_found', 'unsupported_model', 'invalid_model')):
        return 'model_unavailable'
    if any(value in text for value in ('unexpected argument', 'unknown variant', 'error parsing configuration',
                                      'failed to load configuration', 'invalid value')):
        return 'configuration'
    if http_status == 429 or any(value in text for value in (
            'rate limit', 'rate_limit_exceeded', 'too many requests', 'usage limit',
            'usage_limit_exceeded', 'quota exceeded', 'insufficient_quota')):
        return 'rate_limited'
    if http_status == 401 or any(value in text for value in (
            'unauthorized', 'invalid_api_key', 'authentication failed', 'token expired', 'refresh token',
            'refresh_token_reused', 'refresh_token_expired', 'not logged in')):
        return 'authentication'
    if http_status == 403 or 'insufficient_permissions' in text:
        return 'forbidden'
    if http_status == 404:
        return 'not_found'
    if http_status and http_status >= 500:
        return 'server_error'
    if any(value in text for value in ('error connecting', 'connection reset', 'connection refused',
                                      'no such host', 'network is unreachable', 'tls handshake',
                                      'error sending request', 'dns error', 'stream disconnected')):
        return 'network'
    return 'unknown'
