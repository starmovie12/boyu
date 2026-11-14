"""Exit codes and exception types shared by every module."""

EXIT_OK = 0
EXIT_SOME_NOT_OK = 10
EXIT_TOKEN_EXPIRED = 20
EXIT_PROBE_MISSING = 30
EXIT_RIGHTS_NOT_ALLOWED = 40
EXIT_CORRUPT_FILE = 50
EXIT_OTHER_ERROR = 60
EXIT_HOLDOUT_OPENED = 70


class UpdlError(Exception):
    """Base error. Carries an exit code and a (Hinglish) owner message."""

    exit_code = EXIT_OTHER_ERROR

    def __init__(self, message="", exit_code=None):
        super().__init__(message)
        self.message = message
        if exit_code is not None:
            self.exit_code = exit_code


class TokenExpired(UpdlError):
    exit_code = EXIT_TOKEN_EXPIRED


class ProbeMissing(UpdlError):
    exit_code = EXIT_PROBE_MISSING


class ProbeTokenBlocked(ProbeMissing):
    """probe_report.json exists but its plan is BLOCKED_TOKEN (token missing or rejected): fix the token first."""


class RightsNotAllowed(UpdlError):
    exit_code = EXIT_RIGHTS_NOT_ALLOWED


class CorruptFile(UpdlError):
    exit_code = EXIT_CORRUPT_FILE


class HoldoutAlreadyOpened(UpdlError):
    exit_code = EXIT_HOLDOUT_OPENED


class HoldoutRefused(UpdlError):
    exit_code = EXIT_OTHER_ERROR


class ApiError(UpdlError):
    """The server answered with an error. Never holds headers or tokens."""

    def __init__(self, status, code="", text=""):
        super().__init__(f"HTTP {status} {code} {text}"[:300])
        self.status = status
        self.code = code
        self.text = text[:300]


class PlusRequired(ApiError):
    """UDAPI1149: the expired-instruments APIs need an Upstox Plus subscription. Users have seen this code
    arrive with HTTP 401 as well as 4xx, so the code is read before a 401 is treated as an expired token."""


class TokenNotPermitted(ApiError):
    """UDAPI100067: this token type (for example an analytics / extended token) is not allowed on the endpoint."""


class RoutingSuspected(UpdlError):
    """Several UDAPI1021 (key format) answers in a row: keys are going to the wrong endpoint. Stop, do not burn calls."""


class AlreadyRunning(UpdlError):
    """Another updl process holds state/run.lock for this workdir."""


class RetryExhausted(UpdlError):
    """All retries used up: the chunk becomes FAILED_RETRYABLE."""


class BudgetExceeded(UpdlError):
    """Call budget (probe: 40, or --max-calls) is used up."""


class TransportError(Exception):
    """Network-level failure. Message holds only an error class name, never a URL or header."""


class NotAllowedPath(UpdlError):
    """A request path is not in the read-only allowlist."""


class AmbiguousKey(UpdlError):
    exit_code = EXIT_OTHER_ERROR


class FillingForbidden(UpdlError):
    """Rule 4: nothing may be forward-filled or interpolated."""
