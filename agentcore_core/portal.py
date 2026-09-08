"""Cognito login and Gateway dispatch. All authorization state is memory-only."""
import base64
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from .connections import SlackIdentity
from .runtime import Provider


def https_url(value, path=None):
    u = urlsplit(value)
    if (u.scheme != "https" or not u.hostname or u.username or u.password
            or u.port not in (None, 443) or u.query or u.fragment
            or (path is not None and u.path != path)):
        raise ValueError("Invalid HTTPS endpoint")
    return value.rstrip("/")


def validate_settings(s):
    s = dict(s)
    for key in ("issuer", "cognito_domain", "client_id", "return_url", "portal_url", "gateway_url", "workspace_ids", "tools"):
        if not s.get(key):
            raise ValueError(f"Missing portal setting: {key}")
    for key in ("issuer", "cognito_domain", "portal_url", "gateway_url"):
        s[key] = https_url(s[key])
    if not re.fullmatch(r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+", s["issuer"]):
        raise ValueError("A Cognito user pool issuer is required")
    if urlsplit(s["cognito_domain"]).path or urlsplit(s["portal_url"]).path:
        raise ValueError("Domain and portal must be HTTPS origins")
    https_url(s["return_url"], "/oauth/cognito/callback")
    if not isinstance(s["workspace_ids"], list):
        raise ValueError("Workspace IDs must be a list")
    for team in s["workspace_ids"]:
        SlackIdentity(team, "U0")
    if not re.fullmatch(r"[a-z0-9]{1,128}", s["client_id"]):
        raise ValueError("Invalid client ID")
    required = {"getAccessibleAtlassianResources", "getJiraIssue"}
    if set(s["tools"]) != required or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", v) for v in s["tools"].values()):
        raise ValueError("Two explicit Gateway read tool names are required")
    if not 1 <= s.get("callback_port", 8849) <= 65535:
        raise ValueError("Invalid callback port")
    s["scope"] = "openid email profile agentcore/gateway.invoke"
    return s


@dataclass(repr=False)
class Login:
    sub: str
    label: str
    token: str
    expires: float


@dataclass(repr=False)
class Attempt:
    identity: SlackIdentity
    state: str
    nonce: str
    verifier: str
    expires: float
    login: Login | None = None


class Cognito:
    def __init__(self, settings):
        self.s = settings

    def authorize(self, attempt):
        challenge = base64.urlsafe_b64encode(sha256(attempt.verifier.encode()).digest()).decode().rstrip("=")
        return self.s["cognito_domain"] + "/oauth2/authorize?" + urlencode({
            "client_id": self.s["client_id"], "response_type": "code",
            "redirect_uri": self.s["return_url"], "scope": self.s["scope"],
            "state": attempt.state, "nonce": attempt.nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def exchange(self, attempt, code):
        import httpx
        # Fixed admin-configured destinations, no redirects or diagnostic bodies.
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(self.s["cognito_domain"] + "/oauth2/token", data={
                "grant_type": "authorization_code", "client_id": self.s["client_id"],
                "redirect_uri": self.s["return_url"], "code": code,
                "code_verifier": attempt.verifier})
            response.raise_for_status()
            tokens = response.json()
            jwks_response = client.get(self.s["issuer"] + "/.well-known/jwks.json")
            jwks_response.raise_for_status()
            return self.verify(tokens, jwks_response.json(), attempt.nonce)

    def verify(self, tokens, jwks, nonce):
        import jwt
        def decode(token, audience=None):
            if not isinstance(token, str) or len(token) > 32768:
                raise ValueError("Invalid token")
            header = jwt.get_unverified_header(token)
            keys = [k for k in jwks["keys"] if k.get("kid") == header.get("kid")]
            if header.get("alg") != "RS256" or len(keys) != 1:
                raise ValueError("Invalid signing key")
            key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key
            return jwt.decode(token, key, algorithms=["RS256"], issuer=self.s["issuer"],
                audience=audience, options={"verify_aud": audience is not None,
                    "require": ["exp", "iat", "iss", "sub", "token_use"]})
        access = decode(tokens["access_token"])
        identity = decode(tokens["id_token"], self.s["client_id"])
        if (identity["token_use"] != "id" or access["token_use"] != "access"
                or access.get("client_id") != self.s["client_id"]
                or access["sub"] != identity["sub"] or not access["sub"]
                or not isinstance(identity.get("nonce"), str)
                or not hmac.compare_digest(identity["nonce"], nonce)
                or not set(self.s["scope"].split()).issubset(set(access.get("scope", "").split()))):
            raise ValueError("Invalid login claims")
        # Display only, never use email as an identity mapping key.
        label = identity.get("email") if identity.get("email_verified") is True else None
        return Login(access["sub"], str(label or access["sub"])[:200], tokens["access_token"],
                     min(access["exp"], identity["exp"]))


class PortalRuntime:
    def __init__(self, settings, backend, transport, clock=time.time):
        self.s, self.backend, self.transport, self.clock = settings, backend, transport, clock
        self.lock = threading.RLock()
        self.pending, self.logins = {}, {}

    def _identity(self, identity):
        if not isinstance(identity, SlackIdentity) or identity.workspace not in self.s["workspace_ids"]:
            raise ValueError("Untrusted identity")

    def _purge(self):
        now = self.clock()
        self.pending = {k: a for k, a in self.pending.items()
                        if a.expires > now and (a.login is None or a.login.expires > now + 30)}
        self.logins = {k: a for k, a in self.logins.items() if a.expires > now + 30}

    def start(self, identity):
        self._identity(identity)
        with self.lock:
            self._purge()
            self.pending = {k: a for k, a in self.pending.items() if a.identity != identity}
            if len(self.pending) >= 256 or len(self.logins) >= 1024:
                raise ValueError("Login capacity reached")
            attempt = Attempt(identity, secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                              secrets.token_urlsafe(48), self.clock() + 600)
            self.pending[attempt.state] = attempt
            return self.backend.authorize(attempt)

    def callback(self, state, code):
        with self.lock:
            self._purge()
            attempt = self.pending.pop(state, None)
            if attempt is None or attempt.login is not None or not code or len(code) > 4096:
                raise ValueError("Invalid login")
            # Consume the callback state before network I/O. No callback replay.
            login = self.backend.exchange(attempt, code)
            attempt.login = login
            # Independent nonce for Slack confirmation, not the OAuth state.
            self.pending[secrets.token_urlsafe(32)] = attempt

    def status(self, identity):
        self._identity(identity)
        with self.lock:
            self._purge()
            for key, a in self.pending.items():
                if a.identity == identity and a.login is not None:
                    return {"state": "confirm_identity", "label": a.login.label, "attempt": key}
            login = self.logins.get(identity.subject)
            return {"state": "signed_in", "label": login.label} if login else {"state": "signed_out"}

    def confirm(self, identity, key):
        self._identity(identity)
        with self.lock:
            self._purge()
            a = self.pending.get(key)
            if a is None or a.identity != identity or a.login is None:
                raise ValueError("Invalid confirmation")
            self.logins[identity.subject] = a.login
            del self.pending[key]

    def signout(self, identity):
        self._identity(identity)
        with self.lock:
            self.logins.pop(identity.subject, None)
            self.pending = {k: a for k, a in self.pending.items() if a.identity != identity}

    def close(self):
        with self.lock:
            self.pending.clear()
            self.logins.clear()

    def execute(self, identity, tool, arguments):
        allowed = {"getAccessibleAtlassianResources": set(), "getJiraIssue": {"cloudId", "issueIdOrKey"}}
        if (not isinstance(tool, str) or tool not in allowed or not isinstance(arguments, dict)
                or set(arguments) != allowed[tool]
                or any(not isinstance(v, str) or not v or len(v) > 256 for v in arguments.values())):
            return {"error": "invalid_arguments"}
        try:
            self._identity(identity)
            with self.lock:
                self._purge()
                login = self.logins.get(identity.subject)
                if login is None:
                    return {"error": "sign_in_required", "message": "Use the Slack app Home"}
                provider = Provider("gateway", "", self.s["gateway_url"], ())
                value = self.transport(provider, login.token, self.s["tools"][tool], arguments)
                encoded = json.dumps(value)
                if len(encoded) > 65536:
                    return {"error": "result_too_large"}
                # Only successful tool results cross the model boundary.
                if (not isinstance(value, dict) or value.get("isError")
                        or any(x.lower() in encoded.lower() for x in (
                            login.token, "authorizationurl", "authorization_url", "access_token", "refresh_token",
                            "accesstoken", "refreshtoken", "idtoken", "state=", "?code=", "&code=",
                            "id_token", "code_verifier", "elicitation", "oauth2/authorize", "oauth/authorize",
                            "consent-portal", "confirmation_code", "HAC-"))):
                    return {"error": "gateway_request_failed", "message": "Check sign-in and Connections in the app Home"}
                return {"result": value}
        except Exception:
            return {"error": "gateway_request_failed", "message": "Check sign-in and Connections in the app Home"}
