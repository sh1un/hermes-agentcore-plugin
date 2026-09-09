import asyncio
from contextvars import ContextVar
import copy
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from agentcore_core.connections import SlackIdentity
from agentcore_core.host import task_channel, task_identity, enable_portal
from agentcore_core.portal import Login, PortalRuntime, validate_settings
from agentcore_core.portal_ui import PortalConnections
from test_portal import settings


def configured():
    s = settings()
    s['workspace_ids'] = ['TEXAMPLE', 'TOTHER']
    s['service_accounts'] = {'alerts': {
        'workspace_id': 'TEXAMPLE', 'expected_sub': 'service-sub',
        'admin_members': ['UADMIN', 'UADMIN2'],
        'allowed_members': ['U1', 'U2'], 'channel_ids': ['CALERT']}}
    return validate_settings(s)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = configured()
        self.now = 1000
        self.backend = Mock()
        self.backend.authorize.side_effect = lambda a: 'https://login.example/?state=' + a.state
        self.backend.exchange.return_value = Login('service-sub', 'service@example.com', 'svc-secret', 5000)
        self.transport = Mock(return_value={'content': [{'type': 'text', 'text': 'ok'}]})
        self.r = PortalRuntime(self.s, self.backend, self.transport, clock=lambda: self.now)
        self.admin = SlackIdentity('TEXAMPLE', 'UADMIN')
        self.user = SlackIdentity('TEXAMPLE', 'U1')
        self.owner = self.r.service_owner(self.admin, 'alerts')
        self.r.logins[self.user.subject] = Login('person', 'person@example.com', 'personal-secret', 5000)

    def pending(self, owner=None):
        owner = owner or self.owner
        url = self.r.login_url(owner)
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.assertEqual(self.r.callback(state, 'fake-code'), owner)
        return self.r.status(owner)['attempt']

    def connect(self):
        self.r.confirm(self.owner, self.pending())

    def read(self, who=None, channel='CALERT', **kwargs):
        return self.r.execute(who or self.user, 'getAccessibleAtlassianResources', {}, channel_id=channel, **kwargs)

    def test_channel_uses_service_and_other_channels_use_personal(self):
        self.connect()
        self.assertIn('result', self.read())
        self.assertEqual(self.transport.call_args.args[1], 'svc-secret')
        self.assertIn('result', self.read(channel='DPRIVATE'))
        self.assertEqual(self.transport.call_args.args[1], 'personal-secret')
        self.assertIn('result', self.read(channel='COTHER'))
        self.assertEqual(self.transport.call_args.args[1], 'personal-secret')

    def test_unconnected_service_never_falls_back(self):
        self.assertEqual(self.read()['error'], 'service_sign_in_required')
        self.transport.assert_not_called()

    def test_wrong_account_callback_is_consumed_and_rejected(self):
        self.backend.exchange.return_value = Login('admin-sub', 'admin@example.com', 'wrong', 5000)
        with self.assertRaises(ValueError):
            self.pending()
        self.assertEqual(self.r.status(self.owner)['state'], 'signed_out')
        self.assertNotIn(self.owner.subject, self.r.logins)

    def test_non_admin_and_other_workspace_cannot_manage(self):
        for actor in (self.user, SlackIdentity('TOTHER', 'UADMIN')):
            with self.assertRaises(ValueError):
                self.r.service_owner(actor, 'alerts')
            self.assertEqual(self.r.managed_services(actor), [])

    def test_confirmation_bound_to_initiating_admin(self):
        nonce = self.pending()
        other = self.r.service_owner(SlackIdentity('TEXAMPLE', 'UADMIN2'), 'alerts')
        with self.assertRaises(ValueError):
            self.r.confirm(other, nonce)
        self.r.confirm(self.owner, nonce)
        with self.assertRaises(ValueError):
            self.r.confirm(self.owner, nonce)

    def test_unauthorized_member_cannot_use_service(self):
        self.connect()
        self.assertEqual(self.read(SlackIdentity('TEXAMPLE', 'USTRANGER'))['error'], 'service_access_denied')
        self.transport.assert_not_called()

    def test_service_owner_cannot_be_used_as_tool_requester(self):
        self.connect()
        self.assertIn('error', self.read(self.owner))
        self.transport.assert_not_called()

    def test_workspace_scoped_route(self):
        self.connect()
        self.assertEqual(self.read(SlackIdentity('TOTHER', 'U1'))['error'], 'sign_in_required')
        self.transport.assert_not_called()

    def test_missing_channel_fails_closed(self):
        self.connect()
        for channel in (None, '', 'CALERT:thread', 'arbitrary', 1):
            self.assertEqual(self.read(channel=channel)['error'], 'identity_unavailable')
        self.transport.assert_not_called()

    def test_personal_signout_does_not_disconnect_service(self):
        self.connect()
        self.r.signout(self.user)
        self.assertIn('result', self.read())

    def test_service_signout_does_not_disconnect_personal(self):
        self.connect()
        self.r.signout(self.owner)
        self.assertEqual(self.read()['error'], 'service_sign_in_required')
        self.assertIn('result', self.read(channel='DPRIVATE'))

    def test_signout_cancels_pending_other_admin_attempt(self):
        other = self.r.service_owner(SlackIdentity('TEXAMPLE', 'UADMIN2'), 'alerts')
        key = self.pending(other)
        self.r.signout(self.owner)
        with self.assertRaises(ValueError):
            self.r.confirm(other, key)

    def test_shared_service_concurrency_limit(self):
        self.connect()
        self.r.active_requests[self.owner.subject] = 2
        self.assertEqual(self.read(SlackIdentity('TEXAMPLE', 'U2'))['error'], 'busy')
        self.transport.assert_not_called()

    def test_service_signout_rejects_inflight_result(self):
        self.connect()
        def call(*args):
            self.r.signout(self.owner)
            return {'content': []}
        self.transport.side_effect = call
        self.assertEqual(self.read()['error'], 'service_sign_in_required')
        self.assertEqual(self.r.active_requests, {})

    def test_expiration_and_restart_require_service_login(self):
        self.connect()
        self.now = 4990
        self.assertEqual(self.read()['error'], 'service_sign_in_required')
        self.r.close()
        fresh = PortalRuntime(self.s, self.backend, self.transport)
        self.assertEqual(fresh.execute(self.user, 'getAccessibleAtlassianResources', {}, channel_id='CALERT')['error'], 'service_sign_in_required')

    def test_no_auth_artifacts_in_model_result(self):
        self.connect()
        self.transport.return_value = {'content': [{'text': 'svc-secret'}]}
        result = self.read()
        self.assertNotIn('svc-secret', json.dumps(result))
        self.assertIn('error', result)

    def test_operator_audit_does_not_contain_credentials(self):
        self.connect()
        with self.assertLogs('agentcore.identity', level='INFO') as logs:
            self.read()
        text = '\n'.join(logs.output)
        self.assertIn('requester=U1', text)
        self.assertIn('principal=service:TEXAMPLE:alerts', text)
        self.assertNotIn('svc-secret', text)
        self.assertNotIn('personal-secret', text)

    def test_host_tool_wires_channel_without_model_arguments(self):
        self.connect()
        values = {'HERMES_SESSION_CHAT_ID': 'CALERT', 'HERMES_SESSION_PLATFORM': 'slack',
                  'HERMES_SESSION_SCOPE_ID': 'TEXAMPLE', 'HERMES_SESSION_USER_ID': 'U1',
                  'HERMES_CRON_SESSION': ''}
        variables = {name: ContextVar(name) for name in values}
        tokens = {name: variables[name].set(value) for name, value in values.items()}
        ctx = Mock()
        try:
            with patch.dict(sys.modules, {'gateway': types.SimpleNamespace(session_context=types.SimpleNamespace(_VAR_MAP=variables))}), patch('agentcore_core.portal.PortalRuntime', return_value=self.r), patch('atexit.register'):
                enable_portal(ctx, self.s)
                handler = ctx.register_tool.call_args.kwargs['handler']
                args = {'tool': 'getAccessibleAtlassianResources', 'arguments': {}}
                self.assertIn('result', json.loads(handler(args)))
                self.assertEqual(self.transport.call_args.args[1], 'svc-secret')
                self.assertEqual(json.loads(handler(dict(args, channel_id='DPRIVATE')))['error'], 'invalid_arguments')
                schema = ctx.register_tool.call_args.kwargs['schema']['parameters']['properties']
                self.assertEqual(set(schema), {'tool', 'arguments'})
        finally:
            for name, token in tokens.items():
                variables[name].reset(token)

    def test_argument_identity_injection_rejected(self):
        self.connect()
        result = self.r.execute(self.user, 'getAccessibleAtlassianResources', {'subject': 'service-sub'}, channel_id='CALERT')
        self.assertEqual(result['error'], 'invalid_arguments')
        self.transport.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_invalid_routes(self):
        for key, value in [('workspace_id', 'TUNKNOWN'), ('expected_sub', ''),
                           ('admin_members', []), ('allowed_members', []),
                           ('channel_ids', ['DPRIVATE']), ('channel_ids', ['CALERT', 'CALERT'])]:
            s = configured()
            s['service_accounts']['alerts'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_settings(s)

    def test_duplicate_route_rejected(self):
        s = configured()
        s['service_accounts']['second'] = copy.deepcopy(s['service_accounts']['alerts'])
        with self.assertRaises(ValueError):
            validate_settings(s)

    def test_context_ignores_environment_and_thread(self):
        names = ['HERMES_SESSION_CHAT_ID', 'HERMES_SESSION_PLATFORM', 'HERMES_SESSION_SCOPE_ID',
                 'HERMES_SESSION_USER_ID', 'HERMES_CRON_SESSION', 'HERMES_SESSION_THREAD_ID']
        variables = {name: ContextVar(name) for name in names}
        sc = types.SimpleNamespace(_VAR_MAP=variables)
        with patch.dict(sys.modules, {'gateway': types.SimpleNamespace(session_context=sc)}), patch.dict(os.environ, {'HERMES_SESSION_CHAT_ID': 'CALERT'}):
            with self.assertRaises(ValueError):
                task_channel()
            tokens = [variables[n].set(v) for n, v in zip(names, ['CALERT', 'slack', 'TEXAMPLE', 'U1', '', '123.456'])]
            try:
                self.assertEqual(task_channel(), 'CALERT')
                self.assertEqual(task_identity(['TEXAMPLE']), SlackIdentity('TEXAMPLE', 'U1'))
                token = variables['HERMES_CRON_SESSION'].set('1')
                with self.assertRaises(ValueError):
                    task_identity(['TEXAMPLE'])
                variables['HERMES_CRON_SESSION'].reset(token)
            finally:
                for name, token in zip(names, tokens):
                    variables[name].reset(token)


class ServiceUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = ServiceTests()
        self.fixture.setUp()
        self.r = self.fixture.r
        self.ui = PortalConnections(self.r, self.r.s['workspace_ids'])
        self.client = Mock(views_publish=AsyncMock())

    async def test_only_admin_sees_service_section(self):
        await self.ui.home(self.client, self.fixture.user)
        self.assertNotIn('Service account:', json.dumps(self.client.views_publish.call_args.kwargs))
        await self.ui.home(self.client, self.fixture.admin)
        self.assertIn('Service account:', json.dumps(self.client.views_publish.call_args.kwargs))

    async def test_service_login_is_separate_from_personal(self):
        await self.ui.perform(self.client, self.fixture.admin, 'hacp:service_login', '{"service":"alerts"}')
        view = self.client.views_publish.call_args.kwargs['view']
        buttons = [b for block in view['blocks'] if block['type'] == 'actions' for b in block['elements']]
        service_link = next(b['url'] for b in buttons if b['text']['text'] == '連接服務帳號')
        personal_link = next(b['url'] for b in buttons if b['text']['text'] == '連接 Google 帳號')
        self.assertNotEqual(service_link, personal_link)

    async def test_forged_non_admin_action_does_not_create_service_attempt(self):
        await self.ui.perform(self.client, self.fixture.user, 'hacp:service_login', '{"service":"alerts"}')
        self.assertFalse(any(a.identity.subject.startswith('service:') for a in self.r.pending.values()))
