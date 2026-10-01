from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import checkout, generate, tenants, usage, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation and evolution are owned by Alembic. Run
    # `alembic upgrade head` before starting the application.
    yield


app = FastAPI(title="Billing Engine", lifespan=lifespan)
app.include_router(tenants.router)
app.include_router(generate.router)
app.include_router(usage.router)
app.include_router(checkout.router)
app.include_router(webhooks.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    """Minimal Checkout success target and service status response."""
    return {"status": "ok", "service": "billing-engine"}
