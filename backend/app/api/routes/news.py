from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import News, User
from app.core.deps import get_current_user, get_current_super_admin
from app.schemas.schemas import NewsIn
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/news", tags=["news"])

# The four news sections editors can post under.
SECTIONS = ["Announcements", "Product Updates", "Offers", "Alerts"]


def _ip(request: Request):
    return request.client.host if request and request.client else None


def _n(n: News) -> dict:
    return {
        "id": n.id,
        "section": n.section,
        "title": n.title,
        "body": n.body,
        "image": n.image,
        "author": n.author_name,
        "published": n.published,
        "createdAt": (n.created_at.isoformat() + "Z") if n.created_at else None,
        "updatedAt": (n.updated_at.isoformat() + "Z") if n.updated_at else None,
    }


@router.get("/sections")
async def list_sections(_: User = Depends(get_current_user)):
    return SECTIONS


@router.get("")
async def list_news(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All authenticated users see published news; the Super Admin also sees drafts."""
    q = select(News).order_by(News.id.desc())
    rows = (await db.execute(q)).scalars().all()
    if current_user.role.value != "SUPER_ADMIN":
        rows = [r for r in rows if r.published]
    return [_n(r) for r in rows]


@router.post("")
async def create_news(
    data: NewsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    if not data.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    n = News(
        section=data.section or "Announcements",
        title=data.title.strip(),
        body=data.body or "",
        image=data.image,
        author_name=sa.name,
        published=data.published,
    )
    db.add(n)
    await db.flush()
    await log_event(db, "NEWS_CREATED", f"News \"{n.title}\" posted by {sa.name}", actor=sa)
    await record_audit(db, "NEWS_CREATED", actor=sa, entity_type="news", entity_id=n.id, new=n.title, ip=_ip(request))
    await db.refresh(n)
    return _n(n)


@router.patch("/{news_id}")
async def update_news(
    news_id: int,
    data: NewsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    n = (await db.execute(select(News).where(News.id == news_id))).scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="News not found")
    n.section = data.section or n.section
    n.title = data.title.strip() or n.title
    n.body = data.body
    if data.image is not None:
        n.image = data.image or None
    n.published = data.published
    n.updated_at = datetime.utcnow()
    await db.flush()
    await log_event(db, "NEWS_UPDATED", f"News \"{n.title}\" updated by {sa.name}", actor=sa)
    await record_audit(db, "NEWS_UPDATED", actor=sa, entity_type="news", entity_id=n.id, new=n.title, ip=_ip(request))
    await db.refresh(n)
    return _n(n)


@router.delete("/{news_id}")
async def delete_news(
    news_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    n = (await db.execute(select(News).where(News.id == news_id))).scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="News not found")
    title = n.title
    await db.delete(n)
    await log_event(db, "NEWS_DELETED", f"News \"{title}\" deleted by {sa.name}", actor=sa)
    await record_audit(db, "NEWS_DELETED", actor=sa, entity_type="news", entity_id=news_id, old=title, ip=_ip(request))
    return {"message": "News deleted"}
