
"""Rawaj Pay transfer API."""

import json
import math
import threading
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Account, IdempotencyRecord, Transfer
from app.settings import Settings


class TransferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_account_id: str
    to_account_id: str
    amount_piastres: StrictInt = Field(gt=0)
    memo: str | None = Field(default=None, max_length=140)

    @model_validator(mode="after")
    def distinct_accounts(self):
        if self.from_account_id == self.to_account_id:
            raise ValueError("Source and destination must differ")
        return self


def problem(status, title, detail=None, headers=None):
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
    }

    if detail:
        body["detail"] = detail

    return JSONResponse(
        content=body,
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def transfer_dict(transfer):
    return {
        "id": transfer.id,
        "from_account_id": transfer.from_account_id,
        "to_account_id": transfer.to_account_id,
        "amount_piastres": transfer.amount_piastres,
        "memo": transfer.memo,
        "status": transfer.status,
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()

    engine = make_engine(settings.database_url)

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.settings = settings

    buckets = {}
    bucket_lock = threading.Lock()
    transfer_lock = threading.Lock()

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        titles = {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            422: "Unprocessable Content",
            429: "Too Many Requests",
        }

        detail = exc.detail if isinstance(exc.detail, str) else None

        return problem(
            exc.status_code,
            titles.get(exc.status_code, "Error"),
            detail,
            exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ):
        return problem(
            422,
            "Unprocessable Content",
            "Invalid request",
        )

    def customer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        parts = authorization.split(" ")

        if (
            len(parts) != 2
            or not parts[1]
            or parts[1] not in settings.api_tokens
        ):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return settings.api_tokens[parts[1]]

    def consume_token(customer_id: str):
        now = settings.clock()

        with bucket_lock:
            tokens, last = buckets.get(
                customer_id,
                (float(settings.rate_limit_capacity), now),
            )

            tokens = min(
                float(settings.rate_limit_capacity),
                tokens
                + max(0.0, now - last)
                * settings.rate_limit_refill_per_second,
            )

            if tokens < 1:
                if settings.rate_limit_refill_per_second > 0:
                    wait = max(
                        1,
                        math.ceil(
                            (1 - tokens)
                            / settings.rate_limit_refill_per_second
                        ),
                    )
                else:
                    wait = 1

                buckets[customer_id] = (tokens, now)

                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded",
                    headers={"Retry-After": str(wait)},
                )

            buckets[customer_id] = (tokens - 1, now)

    @app.get("/v1/accounts/{account_id}")
    def get_account(
        account_id: str,
        customer_id: str = Depends(customer),
    ):
        with app.state.session_factory() as session:
            account = session.get(Account, account_id)

            if account is None or account.owner_id != customer_id:
                raise HTTPException(
                    status_code=404,
                    detail="Account not found",
                )

            return {
                "id": account.id,
                "balance_piastres": account.balance_piastres,
                "currency": account.currency,
            }

    @app.get("/v1/transfers/{transfer_id}")
    def get_transfer(
        transfer_id: str,
        customer_id: str = Depends(customer),
    ):
        with app.state.session_factory() as session:
            transfer = session.get(Transfer, transfer_id)

            if transfer is None:
                raise HTTPException(
                    status_code=404,
                    detail="Transfer not found",
                )

            source = session.get(Account, transfer.from_account_id)
            destination = session.get(Account, transfer.to_account_id)

            allowed = (
                (source is not None and source.owner_id == customer_id)
                or (
                    destination is not None
                    and destination.owner_id == customer_id
                )
            )

            if not allowed:
                raise HTTPException(
                    status_code=404,
                    detail="Transfer not found",
                )

            return transfer_dict(transfer)

    @app.post("/v1/transfers")
    async def post_transfer(
        request: Request,
        customer_id: str = Depends(customer),
    ):
        consume_token(customer_id)

        key = request.headers.get("Idempotency-Key")

        if key is None or not key.strip():
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key is required",
            )

        try:
            payload = await request.json()
            data = TransferInput.model_validate(payload)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Invalid transfer request",
            )

        if data.amount_piastres > settings.max_transfer_piastres:
            raise HTTPException(
                status_code=422,
                detail="Transfer amount exceeds limit",
            )

        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )

        with transfer_lock:
            with app.state.session_factory() as session:
                with session.begin():
                    record = session.execute(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.customer_id == customer_id,
                            IdempotencyRecord.key == key,
                        )
                    ).scalar_one_or_none()

                    if record is not None:
                        if record.request_body != canonical:
                            raise HTTPException(
                                status_code=422,
                                detail=(
                                    "Idempotency-Key used "
                                    "with different request"
                                ),
                            )

                        media_type = (
                            "application/problem+json"
                            if record.response_status >= 400
                            else "application/json"
                        )

                        return JSONResponse(
                            content=json.loads(record.response_body),
                            status_code=record.response_status,
                            headers={"Idempotent-Replayed": "true"},
                            media_type=media_type,
                        )

                    source = session.get(
                        Account,
                        data.from_account_id,
                    )

                    if source is None or source.owner_id != customer_id:
                        raise HTTPException(
                            status_code=404,
                            detail="Account not found",
                        )

                    destination = session.get(
                        Account,
                        data.to_account_id,
                    )

                    if destination is None:
                        raise HTTPException(
                            status_code=422,
                            detail="Destination account not found",
                        )

                    if source.balance_piastres < data.amount_piastres:
                        response_body = {
                            "type": "about:blank",
                            "title": "Conflict",
                            "status": 409,
                            "detail": "Insufficient funds",
                        }
                        response_status = 409

                    else:
                        source.balance_piastres -= data.amount_piastres
                        destination.balance_piastres += data.amount_piastres

                        transfer = Transfer(
                            id="trf_" + uuid.uuid4().hex[:28],
                            from_account_id=data.from_account_id,
                            to_account_id=data.to_account_id,
                            amount_piastres=data.amount_piastres,
                            memo=data.memo,
                            status="completed",
                        )

                        session.add(transfer)

                        response_body = transfer_dict(transfer)
                        response_status = 201

                    session.add(
                        IdempotencyRecord(
                            customer_id=customer_id,
                            key=key,
                            request_body=canonical,
                            response_status=response_status,
                            response_body=json.dumps(
                                response_body,
                                separators=(",", ":"),
                            ),
                        )
                    )

                media_type = (
                    "application/problem+json"
                    if response_status >= 400
                    else "application/json"
                )

                return JSONResponse(
                    content=response_body,
                    status_code=response_status,
                    media_type=media_type,
                )

    return app



