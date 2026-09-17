"""HTTP layer: a thin shell over :class:`~hub.services.Hub`.

Every route does the same three things -- read arguments, call one service
method, return its result. No logic lives here, which is what keeps the API and
the assistant's future tool layer honestly in step.

On safety: the server binds to 127.0.0.1, so nothing outside your machine can
reach it. That alone does not stop a web page you have open in another tab from
posting to it, so state-changing requests must carry a same-origin ``Origin``
header. Browsers set that header and cannot be told to forge it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import paths as resolve_paths
from .services import Hub, HubError

STATIC_DIR = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The app is only ever served from loopback, so an Origin whose host is loopback
# is this app. Anything else is another site talking to us.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


def create_app(home: str | None = None, hub: Hub | None = None) -> FastAPI:
    app = FastAPI(
        title="YM Desk",
        description="A local hub for trading statements and their analysis.",
        version="0.1.0",
        docs_url="/api/docs",
    )
    app.state.hub = hub or Hub(resolve_paths(home))

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Refuse cross-site writes. See the module docstring."""
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                parsed = urlparse(origin)
                same_host = parsed.netloc == request.headers.get("host")
                if not same_host and parsed.hostname not in LOOPBACK_HOSTS:
                    return JSONResponse(
                        {"detail": f"cross-origin request from {origin} refused"},
                        status_code=403,
                    )
        return await call_next(request)

    @app.exception_handler(HubError)
    async def hub_error(request: Request, exc: HubError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def hub_of(request: Request) -> Hub:
        return request.app.state.hub

    # -- the page ----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- state -------------------------------------------------------------

    @app.get("/api/overview")
    async def overview(request: Request):
        return hub_of(request).overview()

    @app.get("/api/settings")
    async def get_settings(request: Request):
        return hub_of(request).settings()

    @app.put("/api/settings")
    async def put_settings(request: Request, changes: dict = Body(...)):
        return hub_of(request).update_settings(changes)

    @app.get("/api/instruments")
    async def instruments(request: Request):
        return {"instruments": hub_of(request).instruments()}

    # -- statements --------------------------------------------------------

    @app.get("/api/statements")
    async def list_statements(request: Request):
        return {"statements": hub_of(request).list_statements()}

    @app.post("/api/statements")
    async def upload_statement(request: Request, file: UploadFile = File(...)):
        content = await file.read()
        return hub_of(request).upload_statement(file.filename or "statement", content)

    @app.post("/api/statements/{statement_id}/import")
    async def import_statement(
        request: Request, statement_id: str, options: dict = Body(default={})
    ):
        allowed = {"default_stop_points", "symbol", "tz", "excursion_unit"}
        unknown = set(options) - allowed
        if unknown:
            raise HubError(f"unknown import option(s): {sorted(unknown)}")
        return hub_of(request).import_statement(statement_id, **options)

    @app.delete("/api/statements/{statement_id}")
    async def delete_statement(
        request: Request, statement_id: str, remove_trades: bool = Query(True)
    ):
        return hub_of(request).delete_statement(statement_id, remove_trades)

    # -- analysis ----------------------------------------------------------

    @app.get("/api/performance")
    async def performance(
        request: Request,
        start: str | None = None, end: str | None = None,
        symbol: str | None = None, setup: str | None = None,
    ):
        return hub_of(request).performance(start, end, symbol, setup)

    @app.get("/api/equity")
    async def equity(
        request: Request, start: str | None = None, end: str | None = None,
        symbol: str | None = None,
    ):
        return hub_of(request).equity_curve(start=start, end=end, symbol=symbol)

    @app.get("/api/breakdown")
    async def breakdown(
        request: Request, by: str = Query("hour"),
        start: str | None = None, end: str | None = None, symbol: str | None = None,
    ):
        return hub_of(request).breakdown(by, start=start, end=end, symbol=symbol)

    @app.get("/api/trades")
    async def trades(
        request: Request, limit: int = Query(100, ge=1, le=1000),
        start: str | None = None, end: str | None = None,
        symbol: str | None = None, setup: str | None = None,
    ):
        return hub_of(request).trades(
            limit=limit, start=start, end=end, symbol=symbol, setup=setup
        )

    @app.get("/api/behavior")
    async def behavior(
        request: Request, min_trades: int = Query(20, ge=1),
        start: str | None = None, end: str | None = None, symbol: str | None = None,
    ):
        return hub_of(request).behavior_review(
            min_trades=min_trades, start=start, end=end, symbol=symbol
        )

    @app.post("/api/size")
    async def size(request: Request, payload: dict = Body(...)):
        try:
            entry = float(payload["entry"])
            stop = float(payload["stop"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HubError("entry and stop are required numbers") from exc
        target = payload.get("target")
        return hub_of(request).position_size(
            entry=entry,
            stop=stop,
            target=float(target) if target not in (None, "") else None,
            symbol=payload.get("symbol"),
            equity=(
                float(payload["equity"]) if payload.get("equity") not in (None, "")
                else None
            ),
        )

    @app.get("/api/health")
    async def health(request: Request):
        return {
            "ok": True,
            "version": app.version,
            "data_dir": str(hub_of(request).paths.home),
        }

    return app
