import logging

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from web.err import install_exception_handlers
from web.mid.log import LoggingMiddleware
from web.mid.rid import RequestIdMiddleware


def _test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    install_exception_handlers(app)

    @app.get("/unauthorized")
    async def unauthorized():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    @app.get("/validated")
    async def validated(item_id: int):
        return {"item_id": item_id}

    @app.get("/crash")
    async def crash():
        raise RuntimeError("database unavailable")

    @app.get("/unavailable")
    async def unavailable():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        )

    return app


def test_http_exception_reason_is_in_access_log(caplog):
    caplog.set_level(logging.INFO, logger="cloudrave.http")

    with TestClient(_test_app()) as client:
        response = client.get("/unauthorized", headers={"X-Request-ID": "test-401"})

    assert response.status_code == 401
    assert any(
        record.request_id == "test-401"
        and record.levelno == logging.WARNING
        and 'reason="Refresh token missing"' in record.getMessage()
        for record in caplog.records
    )


def test_validation_reason_omits_submitted_value(caplog):
    caplog.set_level(logging.INFO, logger="cloudrave.http")

    with TestClient(_test_app()) as client:
        response = client.get("/validated", params={"item_id": "not-a-number"})

    assert response.status_code == 422
    access_log = next(
        record
        for record in caplog.records
        if "/validated -> 422" in record.getMessage()
    )
    assert access_log.levelno == logging.INFO
    assert "query.item_id" in access_log.getMessage()
    assert "not-a-number" not in access_log.getMessage()


def test_handled_server_error_is_logged_as_error(caplog):
    caplog.set_level(logging.INFO, logger="cloudrave.http")

    with TestClient(_test_app()) as client:
        response = client.get("/unavailable", headers={"X-Request-ID": "test-503"})

    assert response.status_code == 503
    assert any(
        record.request_id == "test-503"
        and record.levelno == logging.ERROR
        and 'reason="Storage service unavailable"' in record.getMessage()
        for record in caplog.records
    )


def test_unhandled_exception_logs_type_message_and_request_id(caplog):
    caplog.set_level(logging.INFO, logger="cloudrave.http")

    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/crash", headers={"X-Request-ID": "test-500"})

    assert response.status_code == 500
    assert any(
        record.request_id == "test-500"
        and record.levelno == logging.ERROR
        and 'reason="RuntimeError: database unavailable"' in record.getMessage()
        and record.exc_info is not None
        for record in caplog.records
    )
