import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from comparador.adapters.storage.sqlite.repository import SqliteProductRepository
from comparador.adapters.web.auth import (
    check_credentials,
    is_admin,
    require_admin_or_redirect,
)

BASE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE / "templates"))

DB_PATH = Path(os.environ.get("COMPARADOR_DB", "data/comparador.db"))
repo = SqliteProductRepository(DB_PATH)

SESSION_SECRET = os.environ.get("COMPARADOR_SECRET", secrets.token_urlsafe(32))

app = FastAPI(title="Comparador de Preços")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


_MONTHS_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def _edition_date() -> str:
    now = datetime.now()
    return f"{now.day:02d} de {_MONTHS_PT[now.month - 1]}, {now.year}"


def _ctx(request: Request, **extra) -> dict:
    """Default template context — always includes is_admin and masthead date."""
    return {
        "is_admin": is_admin(request),
        "edition_date": _edition_date(),
        **extra,
    }


def _lowest_per_day(history_by_listing: dict[str, list[dict]]) -> list[dict]:
    """Merge N listing series into a single 'lowest per day' series.

    Input: { "ml:abc": [{"x": iso, "y": price}, ...], ... }
    Output: [{"label": "dd/mm", "iso": iso, "price": float}, ...] sorted by day.
    """
    if not history_by_listing:
        return []
    best_by_day: dict[str, tuple[float, str]] = {}
    for points in history_by_listing.values():
        for p in points:
            try:
                day = p["x"][:10]
            except (KeyError, TypeError):
                continue
            price = float(p["y"])
            prev = best_by_day.get(day)
            if prev is None or price < prev[0]:
                best_by_day[day] = (price, p["x"])
    out: list[dict] = []
    for day in sorted(best_by_day.keys()):
        price, iso = best_by_day[day]
        try:
            dd = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(
            {
                "label": dd.strftime("%d/%m"),
                "iso": iso,
                "price": round(price, 2),
            }
        )
    return out


def _demo_lowest_history(product_id: UUID, best_price: float) -> list[dict]:
    """Deterministic demo series used only when no real history exists yet."""
    seed = sum(product_id.bytes)
    start = datetime.utcnow() - timedelta(days=70)
    points: list[dict] = []
    for i, day in enumerate(range(0, 71, 10)):
        drift = (len(points) - 3) * 0.018
        wave = ((seed + i * 7) % 11 - 5) / 100
        value = best_price * (1.08 - drift + wave)
        if i == 5:
            value = best_price * 1.02
        if i == 7:
            value = best_price
        ts = start + timedelta(days=day)
        points.append(
            {
                "label": ts.strftime("%d/%m"),
                "iso": ts.isoformat(),
                "price": round(value, 2),
                "demo": True,
            }
        )
    return points


def _public_history_for(product_id: UUID, listings: list[dict]) -> list[dict]:
    """Prefer real 'lowest per day' history; fall back to a demo series so that
    freshly populated databases still render a readable chart."""
    real = _lowest_per_day(repo.get_price_history(product_id))
    if len(real) >= 4:
        return real
    priced = [l for l in listings if l.get("current_price")]
    if not priced:
        return []
    best_price = min(float(l["current_price"]) for l in priced)
    return _demo_lowest_history(product_id, best_price)


def _catalog_trend_badge(
    product_id: UUID, best_price: Optional[float]
) -> dict[str, bool]:
    """Flags for small badges on catalog cards (no sparkline drawing)."""
    real = _lowest_per_day(repo.get_price_history(product_id))
    if len(real) < 2:
        return {"is_at_min": False, "show_alta": False}
    window = real[-30:]
    prices = [p["price"] for p in window]
    min_p = min(prices)
    return {
        "is_at_min": best_price is not None
        and abs((best_price or 0) - min_p) < 0.01,
        "show_alta": prices[-1] > prices[0],
    }


# ---------------- root ----------------
@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/public/", status_code=303)


# ---------------- public ----------------
@app.get("/public/")
def public_index(request: Request):
    products = repo.list_products_public_view()
    for p in products:
        pid = UUID(p["id"]) if isinstance(p["id"], str) else p["id"]
        p["trend"] = _catalog_trend_badge(pid, p.get("best_price"))
    return TEMPLATES.TemplateResponse(
        request, "public/index.html", _ctx(request, products=products)
    )


@app.get("/public/product/{product_id}")
def public_product(request: Request, product_id: str):
    try:
        pid = UUID(product_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid product id")
    product = repo.get_product(pid)
    if not product:
        raise HTTPException(status_code=404, detail="product not found")
    listings = repo.get_listings_for_comparison(pid)
    history = _public_history_for(pid, listings)
    return TEMPLATES.TemplateResponse(
        request,
        "public/product.html",
        _ctx(
            request,
            product=product,
            listings=listings,
            history_json=json.dumps(history).replace("</", "<\\/"),
        ),
    )


# ---------------- admin auth ----------------
@app.get("/admin/login")
def login_form(request: Request):
    if is_admin(request):
        return RedirectResponse(url="/admin/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "admin/login.html", _ctx(request, error=None)
    )


@app.post("/admin/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    if check_credentials(email, password):
        request.session["user"] = email
        return RedirectResponse(url="/admin/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/login.html",
        _ctx(request, error="Credenciais inválidas"),
        status_code=401,
    )


@app.get("/admin/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/admin/login", status_code=303)


# ---------------- admin ----------------
@app.get("/admin/")
def admin_index(request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect:
        return redirect
    products = repo.list_products_summary()
    return TEMPLATES.TemplateResponse(
        request, "admin/index.html", _ctx(request, products=products)
    )


@app.get("/admin/product/{product_id}")
def admin_product(request: Request, product_id: str):
    redirect = require_admin_or_redirect(request)
    if redirect:
        return redirect
    try:
        pid = UUID(product_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid product id")
    product = repo.get_product(pid)
    if not product:
        raise HTTPException(status_code=404, detail="product not found")
    listings = repo.get_listings_with_current_price(pid)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/product.html",
        _ctx(
            request,
            product=product,
            listings=listings,
        ),
    )


def _update_listing_status(request: Request, listing_id: str, status: str):
    redirect = require_admin_or_redirect(request)
    if redirect:
        return redirect
    try:
        lid = UUID(listing_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid listing id")
    repo.set_listing_status(lid, status)
    return RedirectResponse(
        url=request.headers.get("referer", "/admin/"), status_code=303
    )


@app.post("/admin/listing/{listing_id}/confirm")
def listing_confirm(request: Request, listing_id: str):
    return _update_listing_status(request, listing_id, "confirmed")


@app.post("/admin/listing/{listing_id}/reject")
def listing_reject(request: Request, listing_id: str):
    return _update_listing_status(request, listing_id, "rejected")


@app.post("/admin/listing/{listing_id}/unobserve")
def listing_unobserve(request: Request, listing_id: str):
    # Same state as reject but semantically different: user is un-doing a
    # previous accept. History snapshots are kept, just not shown anywhere.
    return _update_listing_status(request, listing_id, "rejected")


@app.post("/admin/listing/{listing_id}/reactivate")
def listing_reactivate(request: Request, listing_id: str):
    # Bring back a rejected/unobserved listing to an explicitly-user-observed
    # state, regardless of its original auto-match score.
    return _update_listing_status(request, listing_id, "confirmed")
