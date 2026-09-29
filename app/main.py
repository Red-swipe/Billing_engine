from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import Base, engine
from app.routes import checkout, generate, tenants, usage, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic is deliberately skipped for now; see DESIGN.md.
    Base.metadata.create_all(engine)
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
