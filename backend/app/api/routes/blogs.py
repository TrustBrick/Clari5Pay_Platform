"""
Blog module — a simple company news/update center (News-style).
Readers see published posts; Admins + Super Admins create/edit/publish/delete.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import BlogPost, User
from app.core.deps import get_current_user, get_current_admin
from app.schemas.schemas import BlogIn, BlogStatusIn
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/blogs", tags=["blogs"])

# Fixed category list (no categories table) — the blog acts as a news/update center.
BLOG_CATEGORIES = [
    "Product Updates", "Deposit Management", "Withdrawal Management", "Settlement Management",
    "Security Updates", "Risk Analysis", "Release Notes", "Announcements",
]

STAFF_ROLES = {"ADMIN", "SUPER_ADMIN"}


def _ip(request: Request):
    return request.client.host if request and request.client else None


def _is_staff(user: User) -> bool:
    return (user.role.value if hasattr(user.role, "value") else str(user.role)) in STAFF_ROLES


def _b(p: BlogPost) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "category": p.category,
        "shortDescription": p.short_description,
        "coverImage": p.cover_image,
        "content": p.content,
        "status": p.status,
        "author": p.author_name,
        "publishDate": p.publish_date.isoformat() if p.publish_date else None,
        "createdAt": (p.created_at.isoformat() + "Z") if p.created_at else None,
        "updatedAt": (p.updated_at.isoformat() + "Z") if p.updated_at else None,
        "publishedAt": (p.published_at.isoformat() + "Z") if p.published_at else None,
    }


@router.get("/categories")
async def list_categories(_: User = Depends(get_current_user)):
    return BLOG_CATEGORIES


@router.get("")
async def list_blogs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    category: str | None = None,
    status: str | None = None,
):
    """Readers see PUBLISHED only; staff see everything (optionally filtered)."""
    q = select(BlogPost)
    if not _is_staff(current_user):
        q = q.where(BlogPost.status == "PUBLISHED")
    elif status in ("DRAFT", "PUBLISHED"):
        q = q.where(BlogPost.status == status)
    if category:
        q = q.where(BlogPost.category == category)
    rows = (await db.execute(q.order_by(BlogPost.id.desc()))).scalars().all()
    return [_b(p) for p in rows]


@router.get("/{blog_id}")
async def get_blog(
    blog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p or (not _is_staff(current_user) and p.status != "PUBLISHED"):
        raise HTTPException(status_code=404, detail="Blog not found")
    return _b(p)


@router.post("")
async def create_blog(
    data: BlogIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if not data.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    status = "PUBLISHED" if data.status == "PUBLISHED" else "DRAFT"
    p = BlogPost(
        title=data.title.strip(),
        category=data.category or "Announcements",
        short_description=data.short_description,
        content=data.content or "",
        cover_image=data.cover_image,
        status=status,
        author_id=admin.id,
        author_name=admin.name,
        publish_date=data.publish_date,
        published_at=datetime.utcnow() if status == "PUBLISHED" else None,
    )
    db.add(p)
    await db.flush()
    await log_event(db, "BLOG_CREATED", f'Blog "{p.title}" created by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_CREATED", actor=admin, entity_type="blog", entity_id=p.id, new=p.title, ip=_ip(request))
    await db.refresh(p)
    return _b(p)


@router.patch("/{blog_id}")
async def update_blog(
    blog_id: int,
    data: BlogIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Blog not found")
    if data.title.strip():
        p.title = data.title.strip()
    p.category = data.category or p.category
    p.short_description = data.short_description
    p.content = data.content or ""
    if data.cover_image is not None:
        p.cover_image = data.cover_image or None
    p.publish_date = data.publish_date
    new_status = "PUBLISHED" if data.status == "PUBLISHED" else "DRAFT"
    if new_status == "PUBLISHED" and p.status != "PUBLISHED":
        p.published_at = datetime.utcnow()
    p.status = new_status
    p.updated_at = datetime.utcnow()
    await db.flush()
    await log_event(db, "BLOG_UPDATED", f'Blog "{p.title}" updated by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_UPDATED", actor=admin, entity_type="blog", entity_id=p.id, new=p.title, ip=_ip(request))
    await db.refresh(p)
    return _b(p)


@router.patch("/{blog_id}/status")
async def set_blog_status(
    blog_id: int,
    data: BlogStatusIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Blog not found")
    new_status = "PUBLISHED" if data.status == "PUBLISHED" else "DRAFT"
    old = p.status
    p.status = new_status
    p.updated_at = datetime.utcnow()
    if new_status == "PUBLISHED" and not p.published_at:
        p.published_at = datetime.utcnow()
    action = "BLOG_PUBLISHED" if new_status == "PUBLISHED" else "BLOG_STATUS_CHANGED"
    await db.flush()
    await log_event(db, action, f'Blog "{p.title}" → {new_status} by {admin.name}', actor=admin)
    await record_audit(db, action, actor=admin, entity_type="blog", entity_id=p.id, old=old, new=new_status, ip=_ip(request))
    await db.refresh(p)
    return _b(p)


@router.delete("/{blog_id}")
async def delete_blog(
    blog_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Blog not found")
    title = p.title
    await db.delete(p)
    await log_event(db, "BLOG_DELETED", f'Blog "{title}" deleted by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_DELETED", actor=admin, entity_type="blog", entity_id=blog_id, old=title, ip=_ip(request))
    return {"message": "Blog deleted"}
