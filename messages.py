"""Deterministic Chat wording; review decisions and external writes stay with the worker."""

from datetime import datetime
import re
from zoneinfo import ZoneInfo
import diagnostics


MAINTAINER = '<users/104386993234033491963>'


def plain(value):
    return value.replace('<', '‹').replace('>', '›')


def paragraphs(value):
    return '\n\n'.join(line.strip() for line in value.splitlines() if line.strip())


def chunks(text):
    # Leave room below Chat's 32 KB limit, including JSON escaping and thread metadata.
    remaining, parts = text.encode('utf-8'), []
    while remaining:
        part = remaining[:8000].decode('utf-8', errors='ignore')
        if len(remaining) > 8000 and '\n' in part:
            part = part[:part.rfind('\n')+1]
        parts.append(part)
        remaining = remaining[len(part.encode('utf-8')):]
    return parts or [text]


def validation(ci):
    state = (ci or {}).get('state', 'unverified')
    description = {'passed': 'passed at review time', 'none': 'no checks reported',
                   'failed': 'failed', 'pending': 'pending'}.get(state, 'unverified')
    return 'Tests were not run. CI: ' + description + '.'


def review_result(job, output, url, ci, *, stamp=False):
    findings = output.get('findings', [])
    blockers = [item for item in findings if item['blocking']]
    notes = [item for item in findings if not item['blocking']]
    verdict = 'Stamped' if stamp else 'Changes requested' if blockers else 'Approved'
    lines = [f"{job['repo']}#{job['number']} — {verdict}"]
    if stamp:
        lines += ['', 'Approved as requested without code review.']
    elif not findings:
        lines += ['', plain(paragraphs(output['summary']))]
    for label, items in [('Required', blockers), ('Also', notes)]:
        if items:
            lines += ['', f'*{label}*']
            for item in items:
                title = plain(' '.join(item['title'].split()))
                fix = plain(' '.join(item['fix'].split()))
                lines.append(f'• *{title}* — {fix}')
    footer = [] if stamp else [validation(ci).replace('Tests were not run.', 'Tests not run').removesuffix('.')]
    footer += [f"<{url}|Review>", "Commit: " + job['head'][:8]]
    lines += ['', ' · '.join(footer)]
    return '\n'.join(lines)


def original_request(first):
    date = datetime.fromisoformat(first['created_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('America/New_York'))
    clock = date.strftime('%b %d, %I:%M %p ET').replace(' 0', ' ')
    space, thread = first['thread'].split('/')[1::2]
    return f'Original request: <https://chat.google.com/room/{space}/{thread}|{clock}>.'


def duplicate(job, first):
    text = {
        'queued': 'Already queued.', 'reviewing': 'Already reviewing this PR.',
        'waiting_ci': 'Already queued — waiting for CI.',
        'submitting': 'Review complete — posting to GitHub.',
        'ambiguous': 'Checking the GitHub result before any further submission.',
    }[job['status']]
    if job['mode'] == 'stamp':
        text = 'STAMP request already active — approval without code review.'
    return text + (' ' + original_request(first) if first else '')


def warning(job, status, text, *, diagnostic=None, blocking_review=None):
    """Known host errors get an action; unknown errors never expose arbitrary exception text."""
    title, detail, action, operator = 'Review paused', '', 'inspect this run and retry after resolving the failure.', True
    manual_steps = None
    reason = re.split(r'Review held(?: —|:)\s*', text, maxsplit=1)[-1]
    lower = reason.lower()
    review_link = re.search(r'https://github\.com/' + re.escape(job['repo']) + r'/pull/' + str(job['number']) + r'#pullrequestreview-[0-9]+', text)
    posted = status == 'submitted' or bool(job.get('review_url')) or bool(review_link)
    uncertain = status in {'submitting', 'ambiguous'} or (bool(job.get('payload')) and not posted)
    if uncertain:
        title = 'GitHub result unconfirmed'
        detail = 'A review may already exist. No second submission will be attempted automatically.'
        action = 'verify the existing GitHub result before deciding whether to retry.'
    elif status == 'submitted':
        title, detail = 'Review posted', 'The PR is now closed, merged, or draft.'
        action, operator = 'No action needed for this reviewed commit.', False
    elif 'already handled at this head' in lower:
        title, detail = 'Already reviewed', 'This commit already has a review; no duplicate was posted.'
        action, operator = 'Follow the existing GitHub review.', False
    elif status == 'cancelled':
        title = 'Review cancelled'
        if 'usage' in lower:
            detail = 'Some model usage may already have occurred or may be missing from cost totals.'
        action, operator = 'Post the PR link again if you want a new review.', False
    elif 'stamp' in lower:
        title = 'STAMP paused'
        if 'new head' in lower or 'head/base moved' in lower:
            detail, action, operator = 'The PR changed.', 'Post a new STAMP request for the latest commit.', False
        else:
            detail, action = 'STAMP permissions need a check.', 'verify STAMP access before the requester posts another STAMP request.'
    elif status == 'queued' and 'head/base moved' in lower:
        title = 'New commit detected'
        detail = ('The earlier review applies only to the old commit. ' if posted else '') + 'A fresh review is queued for the updated PR.'
        action, operator = 'No action needed — the agent will review the new commit automatically.', False
    elif 'head keeps changing' in lower:
        detail = 'Commits keep changing during review.'
        action, operator = 'Wait until the PR is stable, then repost the PR link.', False
    elif 'referenced dependency changed' in lower:
        dependency = re.search(r'Referenced dependency changed during review: ROKT/launch-engineering#([1-9][0-9]*)', reason)
        related = '#' + dependency[1] if dependency else 'used by this review'
        detail = f'Related PR {related} changed during review.'
        action = 'refresh the related PR context and retry this review once that PR is stable.'
    elif status == 'waiting_ci':
        title, detail = 'Waiting for CI', 'The review has not started.'
        action, operator = 'No action needed — CI will be rechecked automatically.', False
    elif 'ci/buildkite failed' in lower:
        detail = 'CI failed, was cancelled, or needs action.'
        checks = reason.partition('. ')[2].removesuffix(' Fix it and post the link again.').strip()
        if checks:
            detail += '\n' + plain(re.sub(r'\s*—\s*https://\S+', '', checks))
        action, operator = 'Fix the affected CI checks, then repost the PR link.', False
    elif any(value in lower for value in ('ci state', 'ci status', 'ci checks', 'ci pagination')):
        detail = 'CI could not be verified for this commit.'
        action = 'check CI access and the commit being checked, then retry.'
    elif 'merge conflicts' in lower:
        detail = 'The PR has merge conflicts.'
        action, operator = 'Resolve the conflicts, then repost the PR link.', False
    elif 'draft pr' in lower or 'became draft' in lower:
        title, detail = 'Skipped', 'The PR is a draft.'
        action, operator = 'Mark the PR ready, then repost the PR link.', False
    elif 'pr is merged' in lower or 'pr is closed' in lower:
        title, detail = 'Skipped', 'The PR is merged.' if 'pr is merged' in lower else 'The PR is closed.'
        action, operator = 'No action needed — closed and merged PRs are not reviewed.', False
    elif 'self-approval' in lower or 'authored by justin' in lower:
        title, detail = 'Skipped', 'The connected GitHub account cannot review its own PR.'
        action, operator = 'Ask a teammate to review this PR from a different GitHub account.', False
    elif 'no changed files' in lower:
        title, detail = 'Skipped', 'There are no changed files.'
        action, operator = 'No action needed until the PR contains changes.', False
    elif 'context budget' in lower or ('review input has' in lower and 'cli limit' in lower):
        detail = 'The review input exceeds the supported text-review limits.'
        action = 'reduce the review input or arrange a manual review before retrying the agent.'
    elif any(value in lower for value in (
            'justin has a manual blocking review', 'justin has an unfinished manual review',
            'intervening justin review/comment', 'effective review is missing or dismissed')):
        detail = 'An existing manual review or changed review decision needs attention.'
        action = 'resolve the manual review decision before retrying the agent.'
        if blocking_review:
            actor = blocking_review.get('user', {}).get('login', '')
            state = blocking_review.get('state')
            review_id = blocking_review.get('id')
            if (isinstance(actor, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', actor)
                    and state in {'CHANGES_REQUESTED', 'COMMENTED', 'PENDING'}
                    and type(review_id) is int and review_id > 0):
                url = f"https://github.com/{job['repo']}/pull/{job['number']}#pullrequestreview-{review_id}"
                detail = f'Blocking reviewer: @{actor}\nReview state: {state}\n<{url}|Read the blocking review>'
                if state == 'CHANGES_REQUESTED':
                    title = 'Review paused — an earlier review still requests changes'
                    detail += '\n\nNew commits and retries do not automatically clear this block.'
                    manual_steps = [
                        'PR author: Address the blockers in the linked review, then ask the reviewer to reassess.',
                        f'Justin: {MAINTAINER}\nOnce satisfied, resolve or dismiss the blocking review, then retry the agent.',
                    ]
                else:
                    title = 'Review paused — an earlier manual review needs attention'
                    manual_steps = [f'Justin: {MAINTAINER}, finish or resolve the linked manual review before retrying the agent.']
                detail += '\nThe agent stopped before code analysis and posted no new review.'
    elif 'account' in lower or 'github cli' in lower:
        detail = 'The GitHub account or access needs attention.'
        action = 'verify the connected GitHub account and repository access, then retry.'
    elif '20-minute' in lower or 'time limit' in lower or 'time budget' in lower:
        detail = 'The review reached its 20-minute limit.' if '20-minute' in lower else 'The review reached its time limit.'
        action = 'inspect the partial run and retry when appropriate.'
    elif 'processing stopped' in lower or 'worker stopped' in lower:
        detail = 'The worker stopped before this review finished. Usage totals may be incomplete.'
        action = 'inspect the interrupted run and explicitly retry it.'
    elif any(value in lower for value in ('citation', 'quotation', 'finding file', 'finding location', 'quoted code', 'line is outside', 'source was not captured')):
        detail = 'A finding could not be verified against the code.'
        citation = re.search(r'Finding ([1-9][0-9]*) at ([^:\n]+):(RIGHT|LEFT):([0-9]+): ([^.\n]+)', text)
        if citation:
            detail += f"\nFinding {citation[1]}: {plain(citation[2])}:{citation[4]} — {plain(citation[5])}."
        action = 'inspect the citation mismatch and repair the review before retrying.'
    elif 'review draft' in lower or 'saved review' in lower or 'storage' in lower:
        detail = 'The saved review or private draft storage needs attention.'
        action = 'check draft storage and the retained review before deciding whether to retry.'
    elif 'limit exceeded' in lower or 'context budget' in lower or 'too large' in lower or 'symlink' in lower or 'submodule' in lower or 'binary' in lower:
        detail = 'The change exceeds the supported text-review limits.'
        action = 'arrange a manual review or help the author split the change into supported parts.'
    elif ' skill ' in lower or 'review reference' in lower:
        detail = 'Required review guidance is unavailable.'
        action = 'restore the review configuration and required guidance, then retry.'
    elif 'github' in lower or 'file is inaccessible' in lower or 'source' in lower or 'context' in lower:
        detail = 'Required GitHub source or review context could not be loaded.'
        action = 'check the failed GitHub read or required context, then retry.'
    elif 'structured' in lower or 'codex' in lower or 'execution' in lower:
        detail = 'The reviewer did not return a complete result.'
        action = 'inspect the model run diagnostics and retry after resolving the failure.'
    else:
        detail = 'The agent could not validate or complete the review.'

    if diagnostic:
        detail = diagnostics.failure_summary(diagnostic)
        if uncertain:
            detail = 'A review may already exist. ' + detail
        action = 'inspect the matching diagnostic in .runtime/activity.log and resolve the reported failure before retrying.'

    ref = f"{job['repo']}#{job['number']}"
    lines = [f'{ref} — {title}', detail]
    if not posted and not uncertain:
        lines.append('No review posted.')
    link = review_link
    if not link and job.get('review_url'):
        link = re.fullmatch(r'https://github\.com/' + re.escape(job['repo']) + r'/pull/' + str(job['number']) + r'#pullrequestreview-[0-9]+', job['review_url'])
    if link:
        lines.append('GitHub review: ' + link[0])
    if re.search(r'\bci\b', lower):
        check = re.search(r'https://buildkite\.com/[^\s<>?#]+', reason) or re.search(r'https://github\.com/[^\s<>?#]+/(?:runs|actions/runs)/[0-9]+', reason)
        url = check[0] if check else f"https://github.com/{job['repo']}/pull/{job['number']}/checks"
        lines.append(f'<{url}|View CI checks>')
    if manual_steps and not diagnostic:
        lines += ['', 'Next:', '\n\n'.join(manual_steps), '', 'Maintainer details:']
        if job.get('mode') == 'stamp':
            lines.append('After resolving the issue, an authorized sender must post a fresh STAMP request.')
        else:
            lines.append('After resolving the issue, run agent.py retry ' + job['id'] + '. Existing evidence and publishing checks still apply.')
    else:
        lines += ['', 'Next: ' + (MAINTAINER + ', ' if operator else '') + action]
    if operator:
        lines.append('Ref: ' + job['id'][:8])
    return '\n'.join(line for line in lines if line is not None)
