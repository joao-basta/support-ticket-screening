"""Exception hierarchy for the pipeline.

Every failure the operator can actually cause (missing file, unreadable
encoding, corrupt archive, malformed API payload) is raised as an `LdpError`
carrying a pt-BR message ready to show. The CLI turns it into a clean stderr
line plus an exit code; the API turns it into an RFC 7807 problem document.
Nothing else should surface as a raw traceback.
"""


class LdpError(Exception):
    """Base class for every expected, user-facing pipeline failure.

    Args:
        message: pt-BR text shown directly to the user.
        detail: optional technical context (path, offending value) for logs.
    """

    #: HTTP status the API should map this to.
    http_status: int = 400
    #: Short machine-readable slug, used as the RFC 7807 `type`.
    slug: str = 'erro-generico'

    def __init__(self, message: str, detail: str = '') -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.message} ({self.detail})" if self.detail else self.message


class TicketLoadError(LdpError):
    """The ticket text could not be loaded (missing file, unreadable, empty)."""

    http_status = 400
    slug = 'chamado-nao-carregado'


class AttachmentError(LdpError):
    """An attachment could not be resolved (corrupt archive, unsafe member)."""

    http_status = 400
    slug = 'anexo-invalido'


class ParseError(LdpError):
    """The ticket body could not be parsed into anything analysable."""

    http_status = 422
    slug = 'chamado-nao-interpretado'


class PayloadError(LdpError):
    """An API request was malformed (missing field, wrong content type)."""

    http_status = 400
    slug = 'requisicao-invalida'
