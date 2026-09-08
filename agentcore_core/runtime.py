"""Native-only OAuth lifecycle and model-facing read-only dispatch."""
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

from .connections import Connection, ConnectionRejected, SlackIdentity


@dataclass(frozen=True)
class Provider:
    name: str
    credential_provider: str
    endpoint: str
    scopes: tuple[str, ...]
    resources: tuple[str, ...] = ()


@dataclass(repr=False)
class Pending:
    identity: SlackIdentity
    name: str
    generation: int
    state: str
    session: str
    url: str
    expires: float
    digest: str | None = None
    attempts: int = 0


class Runtime:
    def __init__(self, store, backend, transport, providers, clock=time.monotonic):
        self.store, self.backend, self.transport = store, backend, transport
        self.providers = {p.name: p for p in providers}
        self.clock = clock
        self.pending = {}
        self.lock = threading.RLock()

    def _purge(self):
        for key, p in list(self.pending.items()):
            if p.expires <= self.clock():
                if self.store.status(p.identity, p.name) == Connection("connecting", p.generation):
                    self.store.disconnect(p.identity, p.name)
                del self.pending[key]

    def _current(self, p):
        if self.store.status(p.identity, p.name) != Connection("connecting", p.generation):
            raise ConnectionRejected("Invalid authorization session")

    def status(self, identity):
        with self.lock:
            self._purge()
            return {name: self.store.status(identity, name).state for name in self.providers}

    def connect(self, identity, name):
        with self.lock:
            self._purge()
            provider = self.providers[name]
            if len(self.pending) >= 256:
                raise ConnectionRejected("Too many pending connections")
            # Resume instead of creating duplicate OAuth sessions on repeated clicks.
            for p in self.pending.values():
                if p.identity == identity and p.name == name:
                    self._current(p)
                    return p.url
            generation = self.store.begin(identity, name).generation
            state = secrets.token_urlsafe(32)
            try:
                response = self.backend.token(identity, provider, state)
                if response.get("accessToken"):
                    self.store.complete_verified(identity, name, generation)
                    return None
                url, session = response["authorizationUrl"], response["sessionUri"]
                self.backend.validate_url(url)
                if not session.startswith("urn:ietf:params:oauth:request_uri:"):
                    raise ValueError("Invalid session URI")
                self.pending[session] = Pending(identity, name, generation, state,
                    session, url, self.clock() + 600)
                return url
            except Exception:
                self.store.disconnect(identity, name)
                raise ConnectionRejected("Connection could not be started") from None

    def callback(self, session, state):
        with self.lock:
            self._purge()
            p = self.pending.get(session)
            if p is None or not isinstance(state, str) or not hmac.compare_digest(p.state, state):
                raise ConnectionRejected("Invalid authorization session")
            self._current(p)
            if p.digest is not None:
                raise ConnectionRejected("Callback already processed")
            code = "HAC-" + secrets.token_urlsafe(24)
            p.digest = sha256(code.encode()).hexdigest()
            return code

    def confirm(self, identity, name, code):
        with self.lock:
            self._purge()
            p = next((p for p in self.pending.values()
                      if p.identity == identity and p.name == name), None)
            if p is None or p.digest is None:
                raise ConnectionRejected("Invalid confirmation")
            self._current(p)
            p.attempts += 1
            valid = isinstance(code, str) and len(code) < 256 and hmac.compare_digest(
                p.digest, sha256(code.encode()).hexdigest())
            if not valid:
                if p.attempts >= 5:
                    self.disconnect(identity, name)
                raise ConnectionRejected("Invalid confirmation")
            # Consume before calling AWS. Any ambiguous failure requires a new Connect.
            del self.pending[p.session]
            try:
                self.backend.complete(identity, p.session)
                self.store.complete_verified(identity, name, p.generation)
            except Exception:
                self.store.disconnect(identity, name)
                raise ConnectionRejected("Confirmation failed. Start a new connection") from None

    def disconnect(self, identity, name):
        with self.lock:
            self.store.disconnect(identity, name)
            self.pending = {k: p for k, p in self.pending.items()
                            if not (p.identity == identity and p.name == name)}
            return {"local": "disconnected", "remote_revocation": "not_supported"}

    def execute(self, identity, name, tool, arguments):
        # Explicit read-only allowlist. No caller-chosen host, subject or auth parameters.
        allowed = {
            "getAccessibleAtlassianResources": set(),
            "getJiraIssue": {"cloudId", "issueIdOrKey"},
        }
        if (tool not in allowed or not isinstance(arguments, dict)
                or set(arguments) != allowed[tool]
                or any(not isinstance(v, str) or not v or len(v) > 256 for v in arguments.values())):
            return {"error": "invalid_arguments"}
        with self.lock:
            try:
                provider = self.providers[name]
                generation = self.store.status(identity, name).generation
                def call():
                    response = self.backend.token(identity, provider)
                    token = response.get("accessToken")
                    if not token:
                        # Never expose SDK URLs or errors to the model.
                        raise ConnectionRejected("Connection required")
                    value = self.transport(provider, token, tool, arguments)
                    if value.get("isError"):
                        return {"error": "downstream_error"}
                    encoded = json.dumps(value)
                    if len(encoded) > 65536:
                        return {"error": "result_too_large"}
                    # Defense against an upstream unexpectedly echoing auth metadata.
                    if any(secret in encoded for secret in (token, "authorizationUrl",
                            "workloadAccessToken", "refreshToken", "confirmation_code",
                            "identities/oauth2/authorize", "HAC-")):
                        return {"error": "unsafe_result"}
                    return {"result": value}
                return self.store.dispatch(identity, name, generation, call)
            except ConnectionRejected:
                return {"error": "connection_required", "message": "Use the app Home Connections page"}
            except Exception:
                return {"error": "service_unavailable"}


class AgentCore:
    def __init__(self, client, region, workload, return_url):
        self.client, self.region, self.workload, self.return_url = client, region, workload, return_url

    def validate_url(self, value):
        u = urlsplit(value)
        if (u.scheme != "https" or u.hostname != f"bedrock-agentcore.{self.region}.amazonaws.com"
                or u.username or u.password or u.port not in (None, 443)
                or u.path != "/identities/oauth2/authorize"):
            raise ValueError("Unexpected OAuth endpoint")

    def token(self, identity, provider, state=None):
        workload = self.client.get_workload_access_token_for_user_id(
            workloadName=self.workload, userId=identity.subject)["workloadAccessToken"]
        args = dict(workloadIdentityToken=workload,
                    resourceCredentialProviderName=provider.credential_provider,
                    scopes=list(provider.scopes), oauth2Flow="USER_FEDERATION",
                    resourceOauth2ReturnUrl=self.return_url)
        if state:
            args["customState"] = state
        if provider.resources:
            args["resources"] = list(provider.resources)
        return self.client.get_resource_oauth2_token(**args)

    def complete(self, identity, session):
        self.client.complete_resource_token_auth(
            sessionUri=session, userIdentifier={"userId": identity.subject})
