class AuditError(Exception):
    """Base for every error this package raises."""

    ...


class FetchError(AuditError):
    """A request failed at the transport layer."""

    ...


class RobotsDeniedError(AuditError):
    """robots.txt disallows this URL."""

    ...


class BudgetExceededError(AuditError):
    """Depth or page budget reached."""

    ...
