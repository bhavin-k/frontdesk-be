from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recruitment.adapters.db.session import Database
from recruitment.adapters.file_storage import PostgresFileStorage
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.providers import Providers
from recruitment.api import auth, intake, mail_webhooks, opening_routes, recruitment, tenants
from recruitment.api.dependencies import Container
from recruitment.api.schemas import ErrorResponse
from recruitment.core.config import Settings
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.logging import configure_provider_logging
from recruitment.services.auth import AuthService
from recruitment.services.candidates import CandidateService
from recruitment.services.mail_notifications import MailNotifications
from recruitment.services.mailboxes import MailboxService
from recruitment.services.openings import OpeningService
from recruitment.services.overview import OverviewService
from recruitment.services.recruitment import RecruitmentService
from recruitment.services.tenancy import TenancyService


class RequestLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = 0

        async def limited_receive() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            path = scope.get("path", "")
            headers = dict(scope.get("headers", []))
            document_request = path.endswith("/openings") or path.endswith("/documents")
            maximum = (
                51 * 1024 * 1024
                if document_request and b"multipart/form-data" in headers.get(b"content-type", b"")
                else 11 * 1024 * 1024
            )
            if size > maximum:
                raise HTTPException(413, "Request body is too large.")
            return message

        await self.app(scope, limited_receive, send)


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
                "request_id": getattr(request.state, "request_id", str(uuid4())),
            }
        },
    )


def create_app(settings: Settings | None = None, providers: Providers | None = None) -> FastAPI:
    configure_provider_logging()
    settings = settings or Settings()  # type: ignore[call-arg]
    db = Database(settings)
    providers = providers or Providers.from_settings(settings)
    tenancy = TenancyService(db)
    providers.mailboxes = providers.mailboxes or MailboxRuntime(settings)
    services = Container(
        db,
        AuthService(db, settings),
        tenancy,
        RecruitmentService(tenancy, providers, PostgresFileStorage, settings),
        MailboxService(tenancy, providers.mailboxes),
        CandidateService(tenancy, settings),
        MailNotifications(db, providers.mailboxes),
        OverviewService(tenancy),
        OpeningService(tenancy, providers, settings),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                providers.close()
            finally:
                db.dispose()

    app = FastAPI(
        title="Recruitment API",
        version="0.1.0",
        lifespan=lifespan,
        description="CV intake from uploads and new email, evidence matching and recruiter review.",
    )
    app.state.container = services
    app.add_middleware(RequestLimit)

    @app.middleware("http")
    async def request_headers(request: Request, call_next: Any) -> Any:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw errors include submitted passwords and document content; omit inputs.
        details = [
            {"field": ".".join(str(x) for x in err["loc"]), "type": err["type"]}
            for err in exc.errors()
        ]
        return error_response(request, 422, "INVALID_REQUEST", "Check the request fields.", details)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        return error_response(
            request, 409, "STATE_CONFLICT", "The request conflicts with current data."
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error_response(request, 503, "DATABASE_UNAVAILABLE", "The service is unavailable.")

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return error_response(request, 503, exc.code, "The provider is unavailable.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(
            request, exc.status_code, "HTTP_ERROR", "The request could not complete."
        )

    @app.get("/health/live", tags=["Health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    def ready() -> dict[str, str]:
        for engine in (db.api, db.auth, db.worker, db.dispatcher):
            with db.transaction(engine) as session:
                session.execute(text("SELECT 1"))
        with db.transaction(db.api) as session:
            session.execute(text("SELECT id FROM job_runs LIMIT 0"))
        return {"status": "ok"}

    errors: dict[int | str, dict[str, Any]] = {
        status: {"model": ErrorResponse}
        for status in (401, 403, 404, 409, 410, 413, 415, 422, 429, 503)
    }
    for router in (
        auth.router,
        tenants.router,
        recruitment.router,
        opening_routes.router,
        intake.router,
        intake.oauth,
        mail_webhooks.router,
    ):
        app.include_router(router, responses=errors)
    return app
