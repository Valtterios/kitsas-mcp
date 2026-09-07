"""Errors whose messages are shown to the user by the MCP client."""


class KitsasError(Exception):
    """Base class. The message must name the cause and the fix."""


class NotAKitsasBookError(KitsasError):
    pass


class BookLockedError(KitsasError):
    pass


class UnsupportedSchemaError(KitsasError):
    pass


class NoFiscalYearError(KitsasError):
    pass


class ClosedFiscalYearError(KitsasError):
    pass


class UnbalancedVoucherError(KitsasError):
    pass


class AccountNotFoundError(KitsasError):
    pass


class LedgerVoucherError(KitsasError):
    pass


class AmountError(KitsasError):
    pass
