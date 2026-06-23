"""
Blog Management module — native Clari5Pay feature (mirrors the News module's
CRUD + audit shape). Posts + categories with role-aware reads, admin writes,
super-admin deletes, dashboard stats and analytics.
"""
import json
import re
from collections import defaultdict
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.models import BlogPost, BlogCategory, User
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import BlogIn, BlogCategoryIn, BlogStatusIn
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/blogs", tags=["blogs"])

STAFF_ROLES = {"ADMIN", "SUPER_ADMIN"}


def _ip(request: Request):
    return request.client.host if request and request.client else None


def _is_staff(user: User) -> bool:
    return (user.role.value if hasattr(user.role, "value") else str(user.role)) in STAFF_ROLES


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:200] or "post"


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    return _TAG_RE.sub(" ", html or "")


def _read_minutes(content: str) -> int:
    words = len(_plain(content).split())
    return max(1, round(words / 200))


def _b(p: BlogPost, category_name: str | None = None, *, full: bool = False) -> dict:
    d = {
        "id": p.id,
        "title": p.title,
        "slug": p.slug,
        "categoryId": p.category_id,
        "category": category_name,
        "shortDescription": p.short_description,
        "coverImage": p.cover_image,
        "status": p.status,
        "author": p.author_name,
        "authorId": p.author_id,
        "views": p.views,
        "likes": p.likes,
        "shares": p.shares,
        "commentsCount": p.comments_count,
        "tags": _loads(p.tags, []),
        "createdAt": (p.created_at.isoformat() + "Z") if p.created_at else None,
        "updatedAt": (p.updated_at.isoformat() + "Z") if p.updated_at else None,
        "publishedAt": (p.published_at.isoformat() + "Z") if p.published_at else None,
    }
    if full:
        d["content"] = p.content
        d["images"] = _loads(p.images, [])
        d["readMinutes"] = _read_minutes(p.content)
    return d


def _cat(c: BlogCategory, post_count: int | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "description": c.description,
        "postCount": post_count,
        "createdAt": (c.created_at.isoformat() + "Z") if c.created_at else None,
    }


async def _category_names(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(BlogCategory))).scalars().all()
    return {c.id: c.name for c in rows}


# ─── Categories (declared before /{id} so the int path param can't capture them) ──
@router.get("/categories")
async def list_categories(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    cats = (await db.execute(select(BlogCategory).order_by(BlogCategory.name))).scalars().all()
    counts = dict(
        (await db.execute(
            select(BlogPost.category_id, func.count(BlogPost.id)).group_by(BlogPost.category_id)
        )).all()
    )
    return [_cat(c, int(counts.get(c.id, 0))) for c in cats]


@router.post("/categories")
async def create_category(
    data: BlogCategoryIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    exists = (await db.execute(select(BlogCategory).where(func.lower(BlogCategory.name) == name.lower()))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="A category with this name already exists")
    c = BlogCategory(name=name, slug=_slugify(name), description=data.description)
    db.add(c)
    await db.flush()
    await log_event(db, "BLOG_CATEGORY_CREATED", f'Category "{c.name}" created by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_CATEGORY_CREATED", actor=admin, entity_type="blog_category", entity_id=c.id, new=c.name, ip=_ip(request))
    await db.refresh(c)
    return _cat(c, 0)


@router.patch("/categories/{cat_id}")
async def update_category(
    cat_id: int,
    data: BlogCategoryIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    c = (await db.execute(select(BlogCategory).where(BlogCategory.id == cat_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Category not found")
    if data.name.strip():
        c.name = data.name.strip()
        c.slug = _slugify(c.name)
    c.description = data.description
    await db.flush()
    await log_event(db, "BLOG_CATEGORY_UPDATED", f'Category "{c.name}" updated by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_CATEGORY_UPDATED", actor=admin, entity_type="blog_category", entity_id=c.id, new=c.name, ip=_ip(request))
    await db.refresh(c)
    return _cat(c)


@router.delete("/categories/{cat_id}")
async def delete_category(
    cat_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    c = (await db.execute(select(BlogCategory).where(BlogCategory.id == cat_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Category not found")
    name = c.name
    # Detach posts from the deleted category rather than deleting them.
    posts = (await db.execute(select(BlogPost).where(BlogPost.category_id == cat_id))).scalars().all()
    for p in posts:
        p.category_id = None
    await db.delete(c)
    await log_event(db, "BLOG_CATEGORY_DELETED", f'Category "{name}" deleted by {sa.name}', actor=sa)
    await record_audit(db, "BLOG_CATEGORY_DELETED", actor=sa, entity_type="blog_category", entity_id=cat_id, old=name, ip=_ip(request))
    return {"message": "Category deleted"}


# ─── Dashboard stats ──────────────────────────────────────────────────────────
@router.get("/stats")
async def blog_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = _is_staff(current_user)
    base = select(BlogPost) if staff else select(BlogPost).where(BlogPost.status == "PUBLISHED")
    posts = (await db.execute(base)).scalars().all()
    total = len(posts)
    published = sum(1 for p in posts if p.status == "PUBLISHED")
    total_views = sum(p.views for p in posts)
    total_categories = (await db.execute(select(func.count(BlogCategory.id)))).scalar() or 0
    most = max(posts, key=lambda p: p.views, default=None)
    return {
        "total": total,
        "published": published,
        "draft": total - published,
        "totalViews": total_views,
        "totalCategories": int(total_categories),
        "mostViewed": ({"id": most.id, "title": most.title, "views": most.views} if most else None),
    }


# ─── Analytics (admin+) ───────────────────────────────────────────────────────
@router.get("/analytics")
async def blog_analytics(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    posts = (await db.execute(select(BlogPost))).scalars().all()
    names = await _category_names(db)

    top = sorted(posts, key=lambda p: p.views, reverse=True)[:8]
    top_viewed = [
        {"id": p.id, "title": p.title, "views": p.views, "reads": p.views,
         "avgReadTime": _read_minutes(p.content)}
        for p in top
    ]

    cat_views: dict[str, int] = defaultdict(int)
    cat_posts: dict[str, int] = defaultdict(int)
    for p in posts:
        label = names.get(p.category_id, "Uncategorized")
        cat_views[label] += p.views
        cat_posts[label] += 1
    category_performance = sorted(
        [{"category": k, "views": cat_views[k], "posts": cat_posts[k]} for k in cat_views],
        key=lambda x: x["views"], reverse=True,
    )
    most_popular = category_performance[0]["category"] if category_performance else None

    # Last 6 months of published count + views, oldest→newest.
    monthly: dict[str, dict] = {}
    for p in posts:
        when = p.published_at or p.created_at
        if not when:
            continue
        key = when.strftime("%Y-%m")
        m = monthly.setdefault(key, {"month": when.strftime("%b"), "key": key, "published": 0, "views": 0})
        if p.status == "PUBLISHED":
            m["published"] += 1
        m["views"] += p.views
    monthly_list = [monthly[k] for k in sorted(monthly)][-6:]

    return {
        "topViewed": top_viewed,
        "categoryPerformance": category_performance,
        "mostPopularCategory": most_popular,
        "monthly": monthly_list,
    }


# ─── List ─────────────────────────────────────────────────────────────────────
@router.get("")
async def list_blogs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: str | None = None,
    category_id: int | None = None,
    author: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Role-aware, filterable, paginated list. Non-staff see PUBLISHED only."""
    conds = []
    if not _is_staff(current_user):
        conds.append(BlogPost.status == "PUBLISHED")
    elif status in ("DRAFT", "PUBLISHED"):
        conds.append(BlogPost.status == status)
    if category_id:
        conds.append(BlogPost.category_id == category_id)
    if author:
        conds.append(BlogPost.author_name.ilike(f"%{author}%"))
    if date_from:
        conds.append(BlogPost.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        conds.append(BlogPost.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
    if q:
        term = q.strip()
        like = f"%{term}%"
        search = [BlogPost.title.ilike(like), BlogPost.author_name.ilike(like)]
        if term.isdigit():
            search.append(BlogPost.id == int(term))
        from sqlalchemy import or_
        conds.append(or_(*search))

    base = select(BlogPost)
    for c in conds:
        base = base.where(c)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.order_by(BlogPost.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    names = await _category_names(db)
    return {"items": [_b(p, names.get(p.category_id)) for p in rows], "total": int(total)}


# ─── Detail (increments views for non-author readers) ─────────────────────────
@router.get("/{blog_id}")
async def get_blog(
    blog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Blog not found")
    if not _is_staff(current_user) and p.status != "PUBLISHED":
        raise HTTPException(status_code=404, detail="Blog not found")
    # Count a view when the reader isn't the author.
    if p.author_id != current_user.id:
        p.views += 1
        await db.flush()
    names = await _category_names(db)
    return _b(p, names.get(p.category_id), full=True)


# ─── Create / update / status / delete ────────────────────────────────────────
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
        slug=_slugify(data.title),
        category_id=data.category_id,
        short_description=data.short_description,
        content=data.content or "",
        cover_image=data.cover_image,
        images=json.dumps(data.images or []),
        tags=json.dumps(data.tags or []),
        status=status,
        author_id=admin.id,
        author_name=admin.name,
        published_at=datetime.utcnow() if status == "PUBLISHED" else None,
    )
    db.add(p)
    await db.flush()
    await log_event(db, "BLOG_CREATED", f'Blog "{p.title}" created by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_CREATED", actor=admin, entity_type="blog", entity_id=p.id, new=p.title, ip=_ip(request))
    names = await _category_names(db)
    await db.refresh(p)
    return _b(p, names.get(p.category_id), full=True)


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
        p.slug = _slugify(p.title)
    p.category_id = data.category_id
    p.short_description = data.short_description
    p.content = data.content or ""
    if data.cover_image is not None:
        p.cover_image = data.cover_image or None
    p.images = json.dumps(data.images or [])
    p.tags = json.dumps(data.tags or [])
    new_status = "PUBLISHED" if data.status == "PUBLISHED" else "DRAFT"
    if new_status == "PUBLISHED" and p.status != "PUBLISHED":
        p.published_at = datetime.utcnow()
    p.status = new_status
    p.updated_at = datetime.utcnow()
    await db.flush()
    await log_event(db, "BLOG_UPDATED", f'Blog "{p.title}" updated by {admin.name}', actor=admin)
    await record_audit(db, "BLOG_UPDATED", actor=admin, entity_type="blog", entity_id=p.id, new=p.title, ip=_ip(request))
    names = await _category_names(db)
    await db.refresh(p)
    return _b(p, names.get(p.category_id), full=True)


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
    names = await _category_names(db)
    await db.refresh(p)
    return _b(p, names.get(p.category_id))


@router.delete("/{blog_id}")
async def delete_blog(
    blog_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    p = (await db.execute(select(BlogPost).where(BlogPost.id == blog_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Blog not found")
    title = p.title
    await db.delete(p)
    await log_event(db, "BLOG_DELETED", f'Blog "{title}" deleted by {sa.name}', actor=sa)
    await record_audit(db, "BLOG_DELETED", actor=sa, entity_type="blog", entity_id=blog_id, old=title, ip=_ip(request))
    return {"message": "Blog deleted"}
