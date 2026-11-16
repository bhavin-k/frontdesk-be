import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse

from recruitment.api.dependencies import Services
from recruitment.core.errors import AppError

router = APIRouter(prefix="/api/v1/mailbox-webhooks", tags=["Verified mailbox notifications"])


async def envelope(request: Request) -> dict[str, Any]:
    result = bytearray()
    async for part in request.stream():
        result.extend(part)
        if len(result) > 65536:
            raise AppError("NOTIFICATION_SIZE", "Notification body exceeds its limit.", 413)
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError) as exc:
        raise AppError("INVALID_NOTIFICATION", "Invalid notification envelope.", 422) from exc


@router.post("/gmail", status_code=202)
async def gmail(
    request: Request, services: Services, authorization: Annotated[str, Header()] = ""
) -> Response:
    from starlette.concurrency import run_in_threadpool

    payload = await envelope(request)
    await run_in_threadpool(services.notifications.gmail, payload, authorization)
    return Response(status_code=202)


@router.post("/microsoft/{connection_id}", status_code=202)
async def microsoft(
    connection_id: UUID,
    request: Request,
    services: Services,
    validation_token: Annotated[str | None, Query(alias="validationToken", max_length=512)] = None,
) -> Response:
    if validation_token is not None:
        return PlainTextResponse(validation_token)
    from starlette.concurrency import run_in_threadpool

    payload = await envelope(request)
    await run_in_threadpool(services.notifications.microsoft, connection_id, payload)
    return Response(status_code=202)
