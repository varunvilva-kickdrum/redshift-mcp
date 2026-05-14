"""Auth0 JWT verification for FastMCP bearer auth."""

from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from redshift_mcp.tiers import TierName, normalize_tier


class Auth0JWTVerifier(TokenVerifier):
    """Validate Auth0-issued JWT access tokens and expose tier in scopes."""

    def __init__(
        self,
        *,
        domain: str,
        audience: str,
        tier_claim: str,
        cache_ttl_sec: int = 3600,
    ) -> None:
        domain = domain.removeprefix("https://").removesuffix("/")
        self._issuer = f"https://{domain}/"
        self._audience = audience
        self._tier_claim = tier_claim
        jwks_url = f"https://{domain}/.well-known/jwks.json"
        self._jwks = PyJWKClient(
            jwks_url,
            cache_keys=True,
            lifespan=cache_ttl_sec,
            max_cached_keys=16,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                return None
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp"]},
            )
        except Exception:
            return None

        sub = str(claims.get("sub") or "")
        if not sub:
            return None

        tier = _extract_tier(claims, self._tier_claim)
        scopes = _scopes_from_claims(claims)
        scopes = list(dict.fromkeys([*scopes, f"tier:{tier}"]))

        exp = claims.get("exp")
        expires_at = int(exp) if isinstance(exp, (int, float)) else None

        return AccessToken(
            token=token,
            client_id=sub,
            scopes=scopes,
            expires_at=expires_at,
        )


def _scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    scope = claims.get("scope")
    if isinstance(scope, str) and scope.strip():
        return [s for s in scope.split() if s]
    perms = claims.get("permissions")
    if isinstance(perms, list):
        return [str(p) for p in perms if p]
    return []


def _extract_tier(claims: dict[str, Any], tier_claim: str) -> TierName:
    raw = claims.get(tier_claim)
    if isinstance(raw, str):
        return normalize_tier(raw)
    if isinstance(raw, list) and raw:
        return normalize_tier(str(raw[0]))
    return "free"
