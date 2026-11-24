"""Identity-managed Cognito tokens and Gateway tools for an existing Slack agent.

The platform hosts session binding. This plugin has no callback listener and no
permission to complete OAuth sessions. All authorization artifacts stay native.
"""
import atexit
import asyncio
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from .connections import LazyStore, Store, SlackIdentity, ConnectionRejected
from .runtime import AgentCore, Pending, Provider, Runtime

TOOLS = {"getAccessibleAtlassianResources": "JiraRovoTarget___getAccessibleAtlassianResources",
         "getJiraIssue": "JiraRovoTarget___getJiraIssue"}


def validate_settings(raw):
    s = dict(raw)
    required = ("region", "workload", "credential_provider", "agent_role_arn", "return_url",
                "binding_api_url", "gateway_url", "portal_url", "workspace_ids", "allowed_users", "data_dir", "scopes")
    if s.get("mode") != "vault_gateway" or any(not s.get(key) for key in required):
        raise ValueError("Missing Vault Gateway settings")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", s["region"]):
        raise ValueError("Invalid region")
    for key in ("workload", "credential_provider"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", s[key]):
            raise ValueError("Invalid Identity resource name")
    if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_/-]+", s["agent_role_arn"]):
        raise ValueError("Invalid platform access role")
    if not isinstance(s["workspace_ids"], list) or len(s["workspace_ids"]) != 1 or not isinstance(s["allowed_users"], list):
        raise ValueError("One approved Slack workspace and explicit members required")
    for user in s["allowed_users"]:
        SlackIdentity(s["workspace_ids"][0], user)
    urls = {key: urlsplit(s[key]) for key in ("return_url", "binding_api_url", "gateway_url", "portal_url")}
    for url in urls.values():
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.port not in (None, 443) or url.query or url.fragment):
            raise ValueError("Invalid HTTPS endpoint")
    api = urls["binding_api_url"]
    if (not re.fullmatch(r"[a-z0-9]+\.execute-api\." + re.escape(s["region"]) + r"\.amazonaws\.com", api.hostname)
            or api.path != "/bindings/requests" or urls["return_url"].netloc != api.netloc
            or urls["return_url"].path != "/oauth/agentcore/callback"):
        raise ValueError("Expected the platform's regional binding API and callback")
    gateway = urls["gateway_url"]
    if (gateway.path != "/mcp" or not re.fullmatch(r"[a-z0-9-]+\.gateway\.bedrock-agentcore\."
            + re.escape(s["region"]) + r"\.amazonaws\.com", gateway.hostname)):
        raise ValueError("Expected the regional Gateway endpoint")
    params = s.setdefault("custom_parameters", {})
    if not isinstance(params, dict) or set(params) - {"identity_provider"} or any(v != "Google" for v in params.values()):
        raise ValueError("Only a stable Google login hint is supported")
    scopes = s["scopes"]
    if (not isinstance(scopes, list) or not 1 <= len(scopes) <= 32 or "openid" not in scopes
            or any(not isinstance(scope, str) or not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]{1,256}", scope) for scope in scopes)
            or len(set(scopes)) != len(scopes)):
        raise ValueError("Configure explicit OAuth scopes supplied by the platform operator")
    if s.get("tools", TOOLS) != TOOLS:
        raise ValueError("Use the reviewed Jira read tools")
    if "binding_role_arn" in s:
        raise ValueError("Agents must not receive binding-role credentials")
    return s


class CognitoVault(AgentCore):
    def __init__(self, settings, session_factory, http):
        super().__init__(None, settings["region"], settings["workload"], settings["return_url"])
        self.settings, self.session_factory, self.http = settings, session_factory, http

    def token(self, identity, provider, state=None, session=None, *, force_authentication=False):
        if type(force_authentication) is not bool:
            raise ValueError("Force authentication must be a boolean")
        if force_authentication and (not state or session is not None):
            raise ValueError("Forced authentication must start a new session")
        client = self.session_factory().client("bedrock-agentcore", region_name=self.region)
        workload = client.get_workload_access_token_for_user_id(
            workloadName=self.workload, userId=identity.subject)["workloadAccessToken"]
        args = dict(workloadIdentityToken=workload, resourceCredentialProviderName=provider.credential_provider,
                    scopes=list(provider.scopes), oauth2Flow="USER_FEDERATION",
                    resourceOauth2ReturnUrl=self.return_url, customParameters=dict(self.settings["custom_parameters"]))
        if force_authentication:
            args["forceAuthentication"] = True
        if state:
            args["customState"] = state
        if session:
            args["sessionUri"] = session
        return client.get_resource_oauth2_token(**args)

    def register_binding(self, identity, session, state):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        data = json.dumps({"user_id": identity.subject, "session_uri": session, "state": state}).encode()
        credentials = self.session_factory().get_credentials().get_frozen_credentials()
        request = AWSRequest(method="POST", url=self.settings["binding_api_url"], data=data,
                             headers={"Content-Type": "application/json"})
        SigV4Auth(credentials, "execute-api", self.region).add_auth(request)
        response = self.http.post(self.settings["binding_api_url"], content=data, headers=dict(request.headers))
        response.raise_for_status()
        if response.status_code != 201:
            raise ConnectionRejected("Binding registration failed")

    def complete(self, identity, session):
        raise ConnectionRejected("Only the independent platform service can complete binding")


class GatewayRuntime(Runtime):
    def __init__(self, store, backend, transport, providers, clock=time.monotonic, *, portal_url=None):
        super().__init__(store, backend, transport, providers, clock)
        self.portal_url = portal_url

    def connect(self, identity, name):
        with self.lock:
            self._purge()
            for pending in self.pending.values():
                if pending.identity == identity and pending.name == name:
                    self._current(pending)
                    response = self.backend.token(identity, self.providers[name], session=pending.session)
                    if response.get("accessToken"):
                        self.store.complete_verified(identity, name, pending.generation)
                        del self.pending[pending.session]
                        return None
                    if response.get("sessionStatus") == "FAILED":
                        self.disconnect(identity, name)
                        raise ConnectionRejected("Sign-in failed. Start again")
                    return pending.url
            return self._start_sign_in(identity, name)

    def reauthenticate(self, identity, name):
        with self.lock:
            self._purge()
            # Replace only this user's local session before clearing the Vault grant.
            # Other users and their pending flows are untouched.
            self.disconnect(identity, name)
            return self._start_sign_in(identity, name, force_authentication=True)

    def _start_sign_in(self, identity, name, *, force_authentication=False):
        if len(self.pending) >= 256:
            raise ConnectionRejected("Too many pending sign-ins")
        generation = self.store.begin(identity, name).generation
        state = secrets.token_urlsafe(32)
        try:
            extra = {"force_authentication": True} if force_authentication else {}
            response = self.backend.token(identity, self.providers[name], state=state, **extra)
            if response.get("accessToken") and not force_authentication:
                self.store.complete_verified(identity, name, generation)
                return None
            # A forced sign-in must produce a browser flow, never silently reconnect.
            url, session = response["authorizationUrl"], response["sessionUri"]
            self.backend.validate_url(url)
            if not re.fullmatch(r"urn:ietf:params:oauth:request_uri:[A-Za-z0-9._~-]{1,900}", session):
                raise ValueError("Invalid authorization session")
            self.backend.register_binding(identity, session, state)
            self.pending[session] = Pending(identity, name, generation, state, session, url, self.clock() + 600)
            return url
        except Exception:
            self.store.disconnect(identity, name)
            raise ConnectionRejected("Sign-in could not be started") from None

    def callback(self, *args):
        raise ConnectionRejected("Binding is hosted by the platform")

    def confirm(self, *args):
        raise ConnectionRejected("Binding is hosted by the platform")

    def execute(self, identity, name, tool, arguments):
        with self.lock:
            previous = self.store.status(identity, name).state
            result = super().execute(identity, name, tool, arguments)
            if result.get("error") == "tool_authorization_required" and self.portal_url:
                # Only the fixed, validated portal entry URL is returned. Never
                # forward the RPC error's one-time authorization URL or state.
                result["authorization_portal_url"] = self.portal_url
            if previous == "connected" and result.get("error") == "connection_required":
                self.disconnect(identity, name)
            return result


def enable(ctx, raw):
    import boto3
    from botocore.config import Config
    import httpx
    from .host import task_identity
    from .slack_ui import NativeConnections
    from .transport import call_mcp
    from gateway import session_context as sc
    settings = validate_settings(raw)
    if not callable(getattr(ctx, "register_platform_handler", None)) or any(key not in getattr(sc, "_VAR_MAP", {}) for key in (
            "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID", "HERMES_CRON_SESSION")):
        raise RuntimeError("Hermes native platform handlers and task-local identity are required")
    config = Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1})
    sts = boto3.client("sts", region_name=settings["region"], config=config)
    def session_factory():
        credentials = sts.assume_role(RoleArn=settings["agent_role_arn"],
            RoleSessionName="hermes-vault-gateway", DurationSeconds=900)["Credentials"]
        # A fresh short-lived session per operation avoids persisting credentials
        # or retaining an expired role session in a long-running ECS task.
        return _session(credentials)

    def _session(credentials):
        session = boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"], region_name=settings["region"])
        # Configure all Identity API calls with bounded timeouts.
        class Session:
            def client(self, service, **kwargs):
                return session.client(service, config=config, **kwargs)
            def get_credentials(self):
                return session.get_credentials()
        return Session()

    http = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
    store = LazyStore(lambda: Store(Path(settings["data_dir"]).expanduser() / "connections.sqlite",
                  settings["region"] + ":" + settings["workload"], frozenset({"gateway"})))
    provider = Provider("gateway", settings["credential_provider"], settings["gateway_url"], tuple(settings["scopes"]))
    def transport(provider, token, tool, arguments):
        return call_mcp(provider, token, TOOLS[tool], arguments)
    runtime = GatewayRuntime(store, CognitoVault(settings, session_factory, http), transport, [provider],
                             portal_url=settings["portal_url"])

    class Connections(NativeConnections):
        connection_actions = NativeConnections.connection_actions | {"hac:reauthenticate"}

        def identity(self, body, context):
            identity = super().identity(body, context)
            if identity.member not in settings["allowed_users"]:
                raise ValueError("Unapproved Slack member")
            return identity

        async def home(self, client, identity, notice="", url=None, active=None):
            state = (await asyncio.to_thread(runtime.status, identity))["gateway"]
            blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Gateway Connections"}},
                      {"type": "section", "text": {"type": "plain_text", "text": "Gateway: " + state}}]
            if notice:
                blocks.append({"type": "section", "text": {"type": "plain_text", "text": notice}})
            actions = ["disconnect"] if state == "connected" else ["connect", "disconnect"] if state == "connecting" else ["connect"]
            buttons = [{"type": "button", "text": {"type": "plain_text", "text":
                "Disconnect" if action == "disconnect" else "Refresh sign-in" if state == "connecting" else "Sign in"},
                "action_id": "hac:" + action, "value": "gateway"} for action in actions]
            buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Sign in again"},
                "action_id": "hac:reauthenticate", "value": "gateway", "confirm": {
                    "title": {"type": "plain_text", "text": "Restart sign-in?"},
                    "text": {"type": "plain_text", "text": "Start a new platform sign-in for your Slack account. Complete the browser flow before using Gateway tools again."},
                    "confirm": {"type": "plain_text", "text": "Sign in again"},
                    "deny": {"type": "plain_text", "text": "Cancel"}}})
            if url and state == "connecting":
                buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Open sign-in"},
                    "action_id": "hac:open", "value": "gateway", "url": url})
            blocks += [{"type": "actions", "elements": buttons}, {"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "Tool permissions"},
                "action_id": "hac:open", "value": "gateway", "url": settings["portal_url"]}]}]
            await client.views_publish(user_id=identity.member, view={"type": "home", "blocks": blocks})

        async def action(self, client, identity, name, action, trigger):
            try:
                if action == "hac:connect":
                    url = await asyncio.to_thread(runtime.connect, identity, name)
                    await self.home(client, identity, "Complete browser sign-in and Slack identity verification, then refresh here." if url else "Connected", url, name)
                elif action == "hac:reauthenticate":
                    url = await asyncio.to_thread(runtime.reauthenticate, identity, name)
                    await self.home(client, identity, "New sign-in started. Open sign-in, complete browser verification, then select Refresh sign-in.", url, name)
                elif action == "hac:disconnect":
                    await asyncio.to_thread(runtime.disconnect, identity, name)
                    await self.home(client, identity, "Disconnected locally. Remote authorizations are unchanged.")
                else:
                    raise ValueError("Unsupported action")
            except Exception:
                await self.home(client, identity, "Sign-in failed or expired. Start again from this page.")

    wired = []
    def wire(app, adapter):
        if wired:
            raise RuntimeError("Only one Slack adapter is supported")
        wired.append(True)
        app.use(Connections(runtime, settings["workspace_ids"]))
    def shutdown():
        http.close()
        store.close()
    atexit.register(shutdown)
    ctx.register_platform_handler("slack", wire)
    def execute(args, **kwargs):
        try:
            if not isinstance(args, dict) or set(args) != {"tool", "arguments"}:
                return json.dumps({"error": "invalid_arguments"})
            identity = task_identity(settings["workspace_ids"])
            if identity.member not in settings["allowed_users"]:
                raise ValueError("Unapproved user")
            return json.dumps(runtime.execute(identity, "gateway", args["tool"], args["arguments"]))
        except Exception:
            return json.dumps({"error": "identity_unavailable"})
    ctx.register_tool(name="agentcore_jira_read", toolset="agentcore", handler=execute,
        schema={"name": "agentcore_jira_read", "description": "Read Jira through AgentCore Gateway as the current Slack user. If tool_authorization_required is returned, show the authorization_portal_url link and ask the user to connect Jira / Atlassian, then retry. Manage platform sign-in in app Home.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["tool", "arguments"],
                "properties": {"tool": {"type": "string", "enum": list(TOOLS)}, "arguments": {"type": "object"}}}})
    return runtime
