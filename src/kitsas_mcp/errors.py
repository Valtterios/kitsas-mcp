"""Errors whose messages are shown to the user by the MCP client."""


class KitsasError(Exception):
    """Base class. The message must name the cause and the fix."""


class NotAKitsasBookError(KitsasError):
    pass


class BookLockedError(KitsasError):
    pass


class UnsupportedSchemaError(KitsasError):
    pass


class BackupError(KitsasError):
    pass


class CorruptBookError(KitsasError):
    pass


class NoFiscalYearError(KitsasError):
    pass


class ClosedFiscalYearError(KitsasError):
    pass


class UnbalancedVoucherError(KitsasError):
    pass


class AccountNotFoundError(KitsasError):
    pass


class NotABankAccountError(KitsasError):
    pass


class LineFormatError(KitsasError):
    pass


class LedgerVoucherError(KitsasError):
    pass


class AmountError(KitsasError):
    pass


class AmbiguousSupplierError(KitsasError):
    pass


class SimilarPartnerError(KitsasError):
    """A name matching no partner, beside one that differs from it only in accents.

    Its own error rather than an AmbiguousSupplierError: nothing matched the
    name here, so the caller is not choosing between matches but deciding
    whether a dropped umlaut was a typo or a different supplier.
    """


class PartnerNotFoundError(KitsasError):
    """An explicit partner_id that names no partner, or is not an id at all."""


class DateFormatError(KitsasError):
    pass
