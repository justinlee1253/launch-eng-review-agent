"""Service review profiles: a bundled reference plus the service's own docs, chosen by changed path."""

from fnmatch import fnmatch
import hashlib
from pathlib import Path
import re


REPOSITORY = 'ROKT/launch-engineering'
REFERENCES = Path(__file__).resolve().parent / 'references'
# Topic docs stop loading while this much of the context budget is still free for evidence rounds.
HEADROOM = 400_000
# A doc holding a credential-shaped URL parameter (a Chat webhook key or token) never loads by profile.
CREDENTIAL = re.compile(r'[?&](?:key|token|sig|signature)=[A-Za-z0-9_%.-]{16,}')
SHARED = ('.buildkite/', '.github/workflows/', 'infra/')
SHARED_FILES = {'Dockerfile', 'Makefile', 'justfile', 'pyproject.toml', 'docker-compose.yml'}

_A = 'ad-sentinel/ad_sentinel/'
_K = 'ad-sentinel/infra/kube/ad-sentinel/'
_Q = 'prism-qa/QAProxyMVP/QAProxyMVP/'
_T = 'prism-qa/QAProxyMVP/QAProxyMVPTests/'
PROFILES = {
    'prism-qa': {
        'reference': 'prism-qa.md',
        'baseline': ('prism-qa/.claude/prism_overview.md',),
        # Ordered by review risk; each doc loads when a changed path matches one of its patterns.
        'topics': (
            ('prism-qa/RELEASING.md', (
                'prism-qa/.buildkite/*', 'prism-qa/distribution/*', 'prism-qa/scripts/release.sh',
                'prism-qa/scripts/check-release-source.py', 'prism-qa/release-requirements.json', 'prism-qa/Makefile',
                'prism-qa/QAProxyMVP/project.yml', _Q + 'Services/Update*', _Q + 'Views/UpdateBannerView.swift')),
            ('prism-qa/.claude/prism_telemetry.md', (
                _Q + 'Services/Telemetry*', _Q + 'Services/TicketAnalyticsService.swift',
                _Q + 'Services/TicketContextPersistence.swift', _Q + 'Models/TicketAnalyticsSnapshot.swift',
                _Q + 'Resources/tests/test_telemetry_contracts.py', _T + 'Telemetry*', _T + 'TicketAnalyticsTests.swift',
                'prism-qa/scripts/stamp-telemetry.py', 'prism-qa/scripts/tests/test_stamp_telemetry.py',
                'prism-qa/QAProxyMVP/project.yml')),
            ('prism-qa/.claude/prism_har_capture.md', (
                _Q + 'Resources/har_capture.py', _Q + 'Resources/tests/test_har_capture.py',
                _Q + 'Services/NetworkHARCapture.swift', _T + 'NetworkHARCaptureTests.swift')),
            ('prism-qa/.claude/prism_capture.md', (
                _Q + 'Resources/rokt_capture.py', _Q + 'Resources/header_inject.py', _Q + 'Resources/tests/*',
                _Q + 'Services/ProxyManager.swift', _Q + 'Services/SessionManager.swift',
                _Q + 'Services/MitmdumpLocator.swift', _Q + 'Services/ValidationDefaultsService.swift',
                _Q + 'Services/CustomHeadersStore.swift', _Q + 'Services/EcommerceQAEngine.swift',
                _Q + 'Services/MockDataService.swift', _Q + 'Models/QASession.swift', _Q + 'Models/EcommerceQA.swift',
                _Q + 'Models/ChecklistItem.swift', _Q + 'Views/PageTabsView.swift', _Q + 'Views/MP*Popover.swift',
                _Q + 'Views/DataSummaryView.swift', _Q + 'Views/SHA256ValidationModal.swift',
                _Q + 'Views/ChecklistView.swift', _Q + 'Views/HUD/*', _T + 'Ecom*',
                _T + 'ExperiencesWireShapeTests.swift', _T + 'V2SelectionCaptureTests.swift')),
            ('prism-qa/.claude/merlin_context.md', (
                _Q + 'Resources/rokt_capture.py', _Q + 'Resources/tests/test_rokt_capture.py',
                _T + 'ExperiencesWireShapeTests.swift')),
            ('prism-qa/.claude/prism_export.md', (
                _Q + 'Services/GoogleDriveService.swift', _Q + 'Services/WebhookService.swift',
                _Q + 'Services/AppConstants.swift', _Q + 'Services/AccountLookupService.swift',
                _Q + 'Services/AdvertiserListService.swift', _Q + 'Services/PartnerAuditSheetExport.swift',
                _Q + 'Services/ContainedFileURL.swift', _Q + 'Services/JiraService.swift',
                _Q + 'Services/JiraShare*', _Q + 'Services/ChatShare*', _Q + 'Services/ChatSpaceDirectory.swift',
                _Q + 'Services/SharePageGatherer.swift', _Q + 'Services/ShareScope.swift',
                _Q + 'Services/ZshrcCredentials.swift', _Q + 'Models/UploadOutcome.swift',
                _Q + 'Views/EndRunReviewView.swift', _Q + 'Views/JiraShareSheet.swift', _Q + 'Views/ChatShareSheet.swift',
                _T + '*Share*', _T + 'ChatSpaceDirectoryTests.swift', _T + 'PartnerAuditSheetExportTests.swift',
                _T + 'ContainedFileURLTests.swift', _Q + 'Resources/fluent_capture.py', _Q + 'Services/Competitor.swift')),
            ('prism-qa/.claude/prism_mobile_web.md', (
                _Q + 'Services/Adb*', _Q + 'Services/AndroidDeviceWatcher.swift', _Q + 'Services/Scrcpy*',
                _Q + 'Services/MacIPWatcher.swift', _Q + 'Services/MobileReachabilityWatcher.swift',
                _Q + 'Services/MobileScreenshotUploadServer.swift', _Q + 'Services/NativeImport*',
                _Q + 'Services/NativeSessionParser.swift', _Q + 'Services/Transactions*',
                _Q + 'Services/ScreenshotService.swift', _Q + 'Services/ScreenRecordingService.swift',
                _Q + 'Services/BrowserLauncher.swift', _Q + 'Views/AndroidUSBSetupSheet.swift',
                _Q + 'Views/MobileProxySetupSheet.swift', _Q + 'Views/CaptureMethodSheet.swift',
                _Q + 'Views/NativeAppPasteSheet.swift', _Q + 'Views/SessionSearchSheet.swift',
                _T + 'MobileScreenshotUploadServerTests.swift', _T + 'NativeSessionParserTests.swift',
                _T + 'TransactionsAttributeDumpTests.swift')),
            ('prism-qa/.claude/prism_competitor_audits.md', (
                _Q + 'Resources/fluent_capture.py', _Q + 'Services/Competitor.swift',
                _Q + 'Models/CompetitorAuditForm.swift', _Q + 'Models/FluentCapture.swift',
                _Q + 'Views/CompetitorAuditEndRunReviewView.swift')),
            ('prism-qa/.claude/prism_redesign.md', (
                _Q + 'Design/*', _Q + 'Models/TicketContext.swift', _Q + 'Models/RunMetadata.swift',
                _Q + 'Models/ShareDraft.swift', _Q + 'Services/Jira*Loader.swift', _Q + 'Services/JiraFieldResolver.swift',
                _Q + 'Services/JiraTicketStatusService.swift', _Q + 'Services/ADFTextExtractor.swift',
                _Q + 'Services/TicketExpectationParser.swift', _Q + 'Services/RunMetadataStore.swift',
                _Q + 'Services/RecentSessionStore.swift', _Q + 'Services/PrismNavigation.swift',
                _Q + 'Services/ShareComposer*', _Q + 'Services/ShareDeliveryCoordinator.swift',
                _Q + 'Services/ShareAnnotation.swift', _Q + 'Services/SharePreviewFormatter.swift',
                _Q + 'Services/DocShare*', _Q + 'Services/PartnerBrandMarkService.swift',
                _Q + 'Services/RecordingFinalizationGate.swift', _Q + 'Services/ScreenshotCaptureGate.swift',
                _Q + 'Views/Landing*', _Q + 'Views/Ticket*', _Q + 'Views/PostRunReviewView.swift',
                _Q + 'Views/ShareComposerView.swift', _Q + 'Views/ShareNotesEditor.swift',
                _Q + 'Views/ShareAnnotationEditor.swift', _Q + 'Views/PrismEvidenceTile.swift',
                _Q + 'Views/PrismImageViewer.swift', _Q + 'Views/ContentView.swift', _Q + 'Views/SettingsView.swift',
                _Q + 'Views/ToolbarView.swift', _Q + 'Views/SessionHomeView.swift', _T + 'Ticket*',
                _T + 'RunMetadataTests.swift', _T + 'PrismThemeTests.swift', _T + 'RecentSessionStoreTests.swift')),
        ),
    },
    'ad-sentinel': {
        'reference': 'ad-sentinel.md',
        'baseline': ('ad-sentinel/README.md', 'ad-sentinel/docs/testing.md'),
        # Files that hold webhook credentials are not mapped; CREDENTIAL withholds one that a later change adds.
        'topics': (
            (_A + 'controllers/suppression.py', (
                _A + 'controllers/suppression.py', _A + 'controllers/process_alerts.py', _A + 'constants.py',
                _K + 'values.yaml', _K + 'config/*')),
            (_A + 'controllers/process_alerts.py', (
                _A + 'controllers/*', _A + 'detector/*', _A + 'group_monitors/*', _A + 'app.py',
                _A + 'models/killswitch.py', _A + 'constants.py')),
            # Suppression and auto-close behaviour is switched by these values, so their code loads them too.
            (_K + 'values.yaml', (_K + '*', _A + 'constants.py', _A + 'config.py', _A + 'controllers/suppression.py',
                                  _A + 'account_alert_service.py')),
            (_K + 'config/prod/us-west-2.yaml', (_K + '*', _A + 'constants.py', _A + 'config.py',
                                                 _A + 'controllers/suppression.py', _A + 'account_alert_service.py')),
            (_K + 'config/stage/us-west-2.yaml', (_K + '*', _A + 'constants.py', _A + 'config.py',
                                                  _A + 'controllers/suppression.py', _A + 'account_alert_service.py')),
            (_K + 'templates/rollout.yaml', (_K + '*', _A + 'constants.py', _A + 'config.py')),
            (_A + 'controllers/notification.py', (
                _A + 'channels/*', _A + 'controllers/notification.py', _A + 'controllers/jira_operations.py',
                _A + 'controllers/templates/*', _A + 'group_monitors/*', _A + 'account_alert_service.py',
                _A + 'notification.py', _A + 'detector/*')),
            (_A + 'controllers/jira_operations.py', (
                _A + 'channels/jira*', _A + 'controllers/jira*', _A + 'account_alert_service.py')),
            (_A + 'controllers/db.py', (
                'ad-sentinel/sql/*', _A + 'controllers/db.py', _A + 'models/*', _A + 'clickhouse.py',
                _A + 'starrocks.py', _A + 'trino_db.py', _A + 'alert_baseline.py')),
            (_A + 'alert_baseline.py', ('ad-sentinel/sql/*', _A + 'controllers/db.py', _A + 'models/*')),
            ('ad-sentinel/docs/runbooks/runbook-high-partner-anomaly-count.md', (
                _A + 'controllers/suppression.py', _A + 'controllers/notification.py',
                _A + 'account_alert_service.py', 'ad-sentinel/sql/*')),
            (_A + 'channels/channel.py', (_A + 'channels/*', _A + 'detector/*')),
            (_A + 'channels/channel_factory.py', (_A + 'channels/*', _A + 'detector/*')),
            (_A + 'detector/detector_event_checker.py', (_A + 'detector/*', _A + 'channels/*')),
            (_A + 'detector/detector_event_controller.py', (_A + 'detector/*', _A + 'channels/*')),
            (_A + 'detector/detector_event_suppressor.py', (_A + 'detector/*',)),
            (_A + 'controllers/templates/template.py', (_A + 'controllers/templates/*', _A + 'channels/*template.py')),
            (_A + 'group_monitors/datadog_webhook.py', (_A + 'group_monitors/*',)),
            ('ad-sentinel/infra/cfn/amazon-resources.yaml', (
                'ad-sentinel/infra/cfn/*', 'ad-sentinel/infra/ecr/*', 'ad-sentinel/infra/deploy.sh')),
            ('ad-sentinel/infra/deploy.sh', ('ad-sentinel/infra/*', 'ad-sentinel/.buildkite/*')),
            ('ad-sentinel/bin/run.sh', (
                'ad-sentinel/.buildkite/*', 'ad-sentinel/bin/*', 'ad-sentinel/Dockerfile', 'ad-sentinel/Makefile',
                'ad-sentinel/tests/*')),
            ('ad-sentinel/trunk-security.md', ('ad-sentinel/.buildkite/*', 'ad-sentinel/.trunk/*')),
            ('ad-sentinel/pytest.ini', ('ad-sentinel/tests/*', 'ad-sentinel/test_scripts/*')),
            ('ad-sentinel/tests/fixtures/__init__.py', ('ad-sentinel/tests/*',)),
            ('ad-sentinel/.cortex/catalog/ad-sentinel.yaml', ('ad-sentinel/.cortex/*',)),
        ),
    },
}


def select(repo, changes, patches):
    """Return {service: sorted affected paths} for each profiled service the PR touches."""
    if repo != REPOSITORY:
        return {}
    affected = {}
    for change in changes:
        paths = {change['filename'], change.get('previous_filename', change['filename'])}
        shared = any(path.startswith(SHARED) or path in SHARED_FILES for path in paths)
        lines = [line[1:] for line in patches.get(change['filename'], ())
                 if line[:1] in {'+', '-'} and not line.startswith(('+++', '---'))
                 and not line[1:].lstrip().startswith(('#', '//', ';'))]
        for service in PROFILES:
            # A shared pipeline file counts only when a changed executable line names the service's directory.
            if any(path.startswith(service + '/') for path in paths) or (
                    shared and any(service + '/' in line for line in lines)):
                affected.setdefault(service, set()).update(paths)
    return {service: sorted(paths) for service, paths in affected.items()}


def topics(service, paths):
    profile = PROFILES[service]
    return [doc for doc, patterns in profile['topics']
            if any(fnmatch(path, pattern) for path in paths for pattern in patterns)]


def load(evidence, github, service, affected, limit):
    """Attach the service reference and its docs; docs past the headroom are left for the model to request."""
    profile = PROFILES[service]
    path = REFERENCES / profile['reference']
    if path.is_symlink():
        raise ValueError('Reference symlinks are not permitted.')
    text = path.read_text()
    evidence.provenance = [row for row in evidence.provenance if row['path'] != str(path)] + [
        {'path': str(path), 'kind': 'review_reference', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
         'compiled_sha': None, 'freshness': 'unversioned'}]
    evidence.references[service] = text
    loaded, deferred, unavailable, withheld = [], [], [], []
    evidence.services[service] = {'affected_paths': affected, 'loaded_docs': loaded, 'deferred_docs': deferred,
                                  'unavailable_docs': unavailable, 'withheld_docs': withheld}
    baseline = tuple(profile['baseline'])
    for doc in baseline + tuple(doc for doc in topics(service, affected) if doc not in baseline):
        # Baseline docs fall back to the merge base; topic docs describe the code at head only.
        for sha in (evidence.head, evidence.ancestor) if doc in baseline else (evidence.head,):
            key = evidence.repo + '@' + sha + ':' + doc
            known = key in evidence.documents
            text = evidence.documents[key] if known else github.context_file(evidence.repo, sha, doc)
            if text is not None:
                break
        if text is False:
            unavailable.append(doc)
            continue
        if not known and text is not None and CREDENTIAL.search(text):
            withheld.append(doc)
            continue
        loaded.append(doc)
        if known:
            continue
        evidence.documents[key] = text
        # Admission is measured as serialized evidence, so escapes and non-ASCII text count at full cost.
        if text is not None and evidence.size() > limit - HEADROOM:
            del evidence.documents[key]
            loaded.pop()
            deferred.append(doc)
    evidence.check_budget()
