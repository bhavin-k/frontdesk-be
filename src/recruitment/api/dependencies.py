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


def container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


Services = Annotated[Container, Depends(container)]
bearer = HTTPBearer(auto_error=False)


def principal(
    services: Services,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if not credentials:
        raise AppError("UNAUTHENTICATED", "Sign in to continue.", 401)
    return services.auth.authenticate(credentials.credentials)


Actor = Annotated[Principal, Depends(principal)]
