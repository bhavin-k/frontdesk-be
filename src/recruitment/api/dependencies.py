from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from recruitment.adapters.db.session import Database
from recruitment.core.errors import AppError
from recruitment.services.auth import AuthService, Principal
from recruitment.services.candidates import CandidateService
from recruitment.services.mail_notifications import MailNotifications
from recruitment.services.mailboxes import MailboxService
from recruitment.services.openings import OpeningService
from recruitment.services.overview import OverviewService
from recruitment.services.recruitment import RecruitmentService
from recruitment.services.screening_dashboard import ScreeningDashboard
from recruitment.services.tenancy import TenancyService


@dataclass
class Container:
    db: Database
    auth: AuthService
    tenants: TenancyService
    recruitment: RecruitmentService
    mailboxes: MailboxService
    candidates: CandidateService
    notifications: MailNotifications
    overview: OverviewService
    openings: OpeningService
    screening: ScreeningDashboard


def container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


Services = Annotated[Container, Depends(container)]
bearer = HTTPBearer(auto_error=False)


def session_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if not credentials:
        raise AppError("UNAUTHENTICATED", "Sign in to continue.", 401)
    return credentials.credentials


SessionToken = Annotated[str, Depends(session_token)]


def principal(services: Services, token: SessionToken) -> Principal:
    return services.auth.authenticate(token)


Actor = Annotated[Principal, Depends(principal)]
