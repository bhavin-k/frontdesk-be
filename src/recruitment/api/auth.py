from fastapi import APIRouter, Response

from recruitment.api.dependencies import Actor, Services
from recruitment.api.schemas import Credentials, MeRead, SessionRead, UserRead

router = APIRouter(prefix="/api/v1", tags=["Authentication"])


@router.post("/auth/users", response_model=UserRead, status_code=201)
def register(payload: Credentials, services: Services) -> UserRead:
    return UserRead(
        user_id=services.auth.register(str(payload.email), payload.password.get_secret_value())
    )


@router.post("/auth/sessions", response_model=SessionRead)
def login(payload: Credentials, services: Services) -> SessionRead:
    token, expires = services.auth.login(str(payload.email), payload.password.get_secret_value())
    return SessionRead(access_token=token, expires_in=expires)


@router.delete("/auth/sessions/current", status_code=204)
def logout(actor: Actor, services: Services) -> Response:
    services.auth.logout(actor)
    return Response(status_code=204)


@router.get("/me", response_model=MeRead)
def me(actor: Actor, services: Services) -> MeRead:
    return MeRead.model_validate(
        {"user_id": actor.user_id, "memberships": services.tenants.memberships(actor)}
    )
