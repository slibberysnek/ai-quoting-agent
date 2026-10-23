"""FastAPI entry point for Azure App Service and Microsoft Graph webhooks."""

from __future__ import annotations

import hmac
import logging
import os
from functools import lru_cache
from typing import Protocol

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .azure_sql import AzureSqlQuoteTools
from .classifier import RequestClassifier
from .graph import GraphClient
from .handler import QuoteHandler
from .models import RunRecord
from .openrouter import OpenRouterClient, load_api_key
from .pipeline import QuotePipeline
from .teams import TeamsWebhookClient


logger = logging.getLogger("quote_agent.web")


class MessageProcessor(Protocol):
    """The one operation the HTTP layer needs from the quote application."""

    def process_message(self, message_id: str) -> RunRecord | None: ...


class ReviewPublisher(Protocol):
    """The delivery operation used after the quote pipeline finishes."""

    def post_run(self, run: RunRecord) -> None: ...


class QuoteNotificationProcessor:
    """Bridge a Graph message ID into the existing deterministic pipeline."""

    def __init__(
        self,
        graph: GraphClient,
        pipeline: QuotePipeline,
        reviews: ReviewPublisher,
    ) -> None:
        self.graph = graph
        self.pipeline = pipeline
        self.reviews = reviews

    def process_message(self, message_id: str) -> RunRecord:
        email = self.graph.get_inbound_email(message_id)
        run = self.pipeline.process(email)
        self.reviews.post_run(run)

        # Log only identifiers and outcomes so App Service diagnostics do not
        # accidentally become a second copy of customer email content.
        logger.info(
            "processed Graph message message_id=%s run_id=%s outcome=%s",
            message_id,
            run.run_id,
            run.result.outcome.value,
        )
        return run


@lru_cache(maxsize=1)
def _default_processor() -> QuoteNotificationProcessor:
    """Lazily assemble production adapters only when a real message arrives."""

    # Lazy construction keeps /health and Graph's validation handshake working
    # before secrets and SQL access are configured during deployment.
    tools = AzureSqlQuoteTools.from_environment()
    openrouter = OpenRouterClient(load_api_key())
    classifier = RequestClassifier(openrouter)
    handler = QuoteHandler(openrouter, tools)
    pipeline = QuotePipeline(classifier, handler, tools)
    return QuoteNotificationProcessor(
        GraphClient.from_environment(),
        pipeline,
        TeamsWebhookClient.from_environment(),
    )


def _run_background_processor(
    configured_processor: MessageProcessor | None,
    message_id: str,
) -> None:
    """Contain failures after the webhook has already acknowledged Graph."""

    try:
        processor = configured_processor or _default_processor()
        processor.process_message(message_id)
    except Exception:
        # Graph requires a fast HTTP acknowledgement. Background failures are
        # logged for now and will become durable retry records in step 4.
        logger.exception("failed to process Graph message message_id=%s", message_id)


def create_app(
    *,
    processor: MessageProcessor | None = None,
    graph_client_state: str | None = None,
) -> FastAPI:
    """Create the web app, allowing small test doubles at external boundaries."""

    application = FastAPI(title="Obelyr Quote Agent", version="0.1.0")
    application.state.message_processor = processor
    application.state.graph_client_state = graph_client_state

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Prove the Python process is reachable without touching dependencies."""

        return {"status": "ok", "service": "obelyr-quote-agent"}

    @application.post("/webhooks/graph")
    async def graph_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        validation_token: str | None = Query(default=None, alias="validationToken"),
    ) -> Response:
        """Validate the endpoint or accept basic Outlook change notifications."""

        if validation_token is not None:
            # Graph expects the URL-decoded token as raw text, not JSON, during
            # subscription creation. No credentials or database access belong
            # in this time-sensitive handshake.
            return PlainTextResponse(validation_token, media_type="text/plain")

        expected_state = application.state.graph_client_state or os.environ.get(
            "GRAPH_CLIENT_STATE", ""
        )
        if not expected_state:
            raise HTTPException(
                status_code=503,
                detail="GRAPH_CLIENT_STATE is not configured",
            )
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc

        notifications = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(notifications, list) or not notifications:
            raise HTTPException(status_code=400, detail="notification value must be a list")

        message_ids: list[str] = []
        seen_ids: set[str] = set()
        for notification in notifications:
            if not isinstance(notification, dict):
                raise HTTPException(status_code=400, detail="invalid notification entry")
            supplied_state = notification.get("clientState")
            if not isinstance(supplied_state, str) or not hmac.compare_digest(
                supplied_state,
                expected_state,
            ):
                raise HTTPException(status_code=401, detail="invalid notification clientState")

            resource_data = notification.get("resourceData")
            message_id = resource_data.get("id") if isinstance(resource_data, dict) else None
            if isinstance(message_id, str) and message_id and message_id not in seen_ids:
                seen_ids.add(message_id)
                message_ids.append(message_id)

        for message_id in message_ids:
            background_tasks.add_task(
                _run_background_processor,
                application.state.message_processor,
                message_id,
            )

        # Acknowledge before calling Graph, OpenRouter, or SQL. Microsoft can
        # otherwise retry the webhook while the quote is still being prepared.
        return Response(status_code=202)

    @application.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        """Keep configuration and validation errors compact and predictable."""

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return application


# Azure App Service imports this module-level ASGI object at process startup.
app = create_app()


def run() -> None:
    """Run the same ASGI app locally without requiring Azure tooling."""

    import uvicorn

    uvicorn.run(
        "quote_agent.web:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    run()
