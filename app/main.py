from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.database import Base, engine
import app.models

Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.APP_NAME)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

from app.routers import auth, pos, import_data, dashboard, products, customers, suppliers, inventory, hr, accounting, production, home, b2b
app.include_router(auth.router)
app.include_router(home.router)
app.include_router(pos.router)
app.include_router(import_data.router)
app.include_router(dashboard.router)
app.include_router(products.router)
app.include_router(customers.router)
app.include_router(suppliers.router)
app.include_router(inventory.router)
app.include_router(hr.router)
app.include_router(accounting.router)
app.include_router(production.router)
app.include_router(b2b.router)

@app.get("/health")
def health():
    return {"status": "ok", "app": settings.APP_NAME}
from app.routers import auth, pos, import_data, dashboard, products, customers, suppliers, inventory, hr, accounting, production, home, b2b, farm

app.include_router(farm.router)
from app.routers import reports
app.include_router(reports.router)
from app.routers import users
app.include_router(users.router)
