# SPDX-License-Identifier: MIT
from __future__ import annotations


class CodexError(Exception):
    def __init__(self, message: str, code: str = "GENERIC", details: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class CodexAuthError(CodexError):
    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message, "AUTH", details)


class CodexHttpError(CodexError):
    def __init__(self, message: str, status: int | None = None, details: object | None = None) -> None:
        super().__init__(message, "HTTP", details)
        self.status = status


# Compatibility superset for the alternative client surface
class CodexCloudError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        body: object | None = None,
        attempt: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.body = body
        self.attempt = attempt

    def as_dict(self):
        return {
            "message": str(self),
            "status": self.status,
            "code": self.code,
            "request_id": self.request_id,
            "attempt": self.attempt,
        }
