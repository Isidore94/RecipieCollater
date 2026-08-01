"""One-shot banner messages carried across a POST-redirect-GET, as query parameters.

Most mutations in this app are plain form posts that redirect back to the page they came from,
which leaves nowhere to put "that worked" or "that didn't". Several of them wrapped the service
call in ``contextlib.suppress`` and redirected regardless, so a rejected input was
indistinguishable from a successful one: the page simply reloaded unchanged and the user was
left to guess whether the button had done anything.

A query parameter is the right carrier here rather than a session flash: there is exactly one
persistent cookie by contract (CONVENTIONS section 5), the messages are short and non-secret,
and a refresh that re-shows the banner is harmless.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi.responses import RedirectResponse

# Long service errors are usually a symptom of an unexpected exception leaking through; cap the
# text so a banner can never become a wall of internals.
_MAX_MESSAGE = 200


def _query(path: str, key: str, message: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{key}={quote(message[:_MAX_MESSAGE])}"


def redirect(
    path: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str = "pantry",
) -> RedirectResponse:
    """303 back to ``path``, carrying at most one banner message.

    ``error`` wins when both are given: a partial failure is the thing the user needs to see.
    ``undo`` names something the banner can offer to reverse - a pantry adjustment, or a
    deleted recipe when ``undo_kind`` says so - so a wrong tap is recoverable from the
    confirmation itself rather than from a history screen.
    """
    if error:
        path = _query(path, "error", error)
    elif notice:
        path = _query(path, "notice", notice)
        if undo is not None:
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}undo={undo}&undo_kind={quote(undo_kind)}"
    return RedirectResponse(path, status_code=303)
