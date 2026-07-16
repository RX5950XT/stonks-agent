"""Target-scoped service authentication shared by isolated runtimes."""

from .authorization import (
    ServiceAccessTarget,
    ServiceAuthenticator,
    ServiceIdentity,
    ServicePermission,
    ServicePrincipal,
    ServiceReceiver,
    ServiceResourceKind,
    authorize_service_dispatch,
    authorize_service_target,
    canonical_request_hash,
    service_nonce_hash,
)
from .environment import validate_isolated_runtime_environment
from .headers import (
    exactly_one_authorization_header,
    invalid_or_oversized_content_length,
)
from .oidc import (
    ServiceOIDCSettings,
    StaticOIDCServiceAuthenticator,
    load_static_oidc_service_authenticator,
)
from .source_identity import service_auth_source_hash

__all__ = [
    "ServiceAccessTarget",
    "ServiceAuthenticator",
    "ServiceIdentity",
    "ServiceOIDCSettings",
    "ServicePermission",
    "ServicePrincipal",
    "ServiceReceiver",
    "ServiceResourceKind",
    "StaticOIDCServiceAuthenticator",
    "authorize_service_dispatch",
    "authorize_service_target",
    "canonical_request_hash",
    "exactly_one_authorization_header",
    "invalid_or_oversized_content_length",
    "load_static_oidc_service_authenticator",
    "service_auth_source_hash",
    "service_nonce_hash",
    "validate_isolated_runtime_environment",
]
