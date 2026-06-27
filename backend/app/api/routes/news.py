from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import News, User
from app.core.deps import get_current_user, get_current_super_admin
from app.core.uploads import validate_upload, IMAGE_TYPES
from app.schemas.schemas import NewsIn
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/news", tags=["news"])

# Sections editors can post under (Admins + Super Admins).
SECTIONS = [
    "Announcements", "Product Updates", "Offers", "Alerts",
    "Release Notes", "Security Alerts", "Maintenance Notices",
]


def _ip(request: Request):
    return request.client.host if request and request.client else None


def _n(n: News) -> dict:
    return {
        "id": n.id,
        "section": n.section,
        "category": getattr(n, "category", None) or n.section,
        "title": n.title,
        "body": n.body,
        "image": n.image,
        "author": n.author_name,
        "published": n.published,
        "featured": bool(getattr(n, "featured", False)),
        "views": int(getattr(n, "views", 0) or 0),
        "priority": n.priority,
        "publishDate": n.publish_date.isoformat() if n.publish_date else None,
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


@router.post("/{news_id}/view")
async def increment_views(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Count a read (drives 'Most Viewed'). Any authenticated user; published items only."""
    n = (await db.execute(select(News).where(News.id == news_id))).scalar_one_or_none()
    if not n or not n.published:
        raise HTTPException(status_code=404, detail="News not found")
    n.views = int(n.views or 0) + 1
    await db.flush()
    return {"id": n.id, "views": n.views}


@router.post("")
async def create_news(
    data: NewsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_super_admin),
):
    if not data.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    n = News(
        section=data.section or "Announcements",
        category=data.category or data.section or "Announcements",
        title=data.title.strip(),
        body=data.body or "",
        image=validate_upload(data.image, allowed=IMAGE_TYPES, label="news image"),
        author_name=actor.name,
        published=data.published,
        featured=data.featured,
        priority=data.priority or "Normal",
        publish_date=data.publish_date,
    )
    db.add(n)
    await db.flush()
    await log_event(db, "NEWS_CREATED", f"News \"{n.title}\" posted by {actor.name}", actor=actor)
    await record_audit(db, "NEWS_CREATED", actor=actor, entity_type="news", entity_id=n.id, new=n.title, ip=_ip(request))
    await db.refresh(n)
    return _n(n)


@router.patch("/{news_id}")
async def update_news(
    news_id: int,
    data: NewsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_super_admin),
):
    n = (await db.execute(select(News).where(News.id == news_id))).scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="News not found")
    n.section = data.section or n.section
    n.category = data.category or n.category
    n.title = data.title.strip() or n.title
    n.body = data.body
    if data.image is not None:
        n.image = validate_upload(data.image, allowed=IMAGE_TYPES, label="news image") or None
    n.published = data.published
    n.featured = data.featured
    n.priority = data.priority or "Normal"
    n.publish_date = data.publish_date
    n.updated_at = datetime.utcnow()
    await db.flush()
    await log_event(db, "NEWS_UPDATED", f"News \"{n.title}\" updated by {actor.name}", actor=actor)
    await record_audit(db, "NEWS_UPDATED", actor=actor, entity_type="news", entity_id=n.id, new=n.title, ip=_ip(request))
    await db.refresh(n)
    return _n(n)


@router.delete("/{news_id}")
async def delete_news(
    news_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_super_admin),
):
    n = (await db.execute(select(News).where(News.id == news_id))).scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="News not found")
    title = n.title
    await db.delete(n)
    await log_event(db, "NEWS_DELETED", f"News \"{title}\" deleted by {actor.name}", actor=actor)
    await record_audit(db, "NEWS_DELETED", actor=actor, entity_type="news", entity_id=news_id, old=title, ip=_ip(request))
    return {"message": "News deleted"}
