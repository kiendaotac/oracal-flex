"""Classification of OCI ServiceError responses into retry decisions.

Classification is driven by the structured fields of
`oci.exceptions.ServiceError` (`status` and `code`), never by parsing the human
readable message — with one deliberate, narrow exception documented in
`_is_legacy_capacity_error` below.
"""

from __future__ import annotations

from enum import Enum

from oci.exceptions import ServiceError

# Codes OCI returns when a shape cannot be placed right now.
CAPACITY_CODES: frozenset[str] = frozenset({"OutOfCapacity", "OutOfHostCapacity"})

# Codes meaning the tenancy has no room left for this shape at all.
QUOTA_CODES: frozenset[str] = frozenset({"LimitExceeded", "QuotaExceeded"})

# Codes meaning we are calling the API too often.
RATE_LIMIT_CODES: frozenset[str] = frozenset({"TooManyRequests"})

# Codes meaning credentials or OCIDs are wrong. Retrying will not fix these.
AUTH_CODES: frozenset[str] = frozenset(
    {"NotAuthenticated", "NotAuthorized", "NotAuthorizedOrNotFound", "InvalidAuthorization"}
)


class ErrorKind(Enum):
    """What the retry loop should do about a given failure."""

    OUT_OF_CAPACITY = "out_of_capacity"   # expected; try the next AD
    QUOTA_EXCEEDED = "quota_exceeded"     # fatal; the Always Free budget is used up
    RATE_LIMITED = "rate_limited"         # back off hard
    AUTH = "auth"                         # config/permission problem
    OTHER = "other"                       # anything else; log and keep going

    @property
    def is_fatal(self) -> bool:
        return self is ErrorKind.QUOTA_EXCEEDED


def _is_legacy_capacity_error(error: ServiceError) -> bool:
    """Detect the older `500 InternalError / "Out of host capacity."` response.

    Some regions still answer a capacity shortage with a generic 500 whose only
    distinguishing feature is the message text. Treating that as a hard failure
    would stop the retry loop on the single error it exists to survive, so this
    one substring check is kept — scoped strictly to 500/InternalError so it can
    never shadow a properly coded error.
    """
    if error.status != 500 or (error.code or "") != "InternalError":
        return False
    return "out of host capacity" in (error.message or "").lower()


def classify(error: ServiceError) -> ErrorKind:
    """Map a ServiceError onto the action the retry loop should take."""
    code = error.code or ""

    if code in CAPACITY_CODES or _is_legacy_capacity_error(error):
        return ErrorKind.OUT_OF_CAPACITY
    if code in QUOTA_CODES:
        return ErrorKind.QUOTA_EXCEEDED
    if error.status == 429 or code in RATE_LIMIT_CODES:
        return ErrorKind.RATE_LIMITED
    if error.status in (401, 403) or code in AUTH_CODES:
        return ErrorKind.AUTH
    return ErrorKind.OTHER


def describe(error: ServiceError) -> str:
    """One-line, log-friendly rendering of a ServiceError."""
    request_id = getattr(error, "request_id", None) or "-"
    return (
        f"status={error.status} code={error.code!r} "
        f"request_id={request_id} message={error.message!r}"
    )
