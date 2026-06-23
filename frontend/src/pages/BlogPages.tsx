import React, { useState, useEffect, useRef, useCallback } from 'react';
import { T } from '../utils/theme';
import { fileToDataUrl, formatDateTime } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, StatusChart, LoadingScreen } from '../components/UI';
import { blogAPI, blogCategoryAPI } from '../services/api';
import type { BlogListParams } from '../services/api';
import type { User, BlogPost, BlogCategory, BlogStats, BlogAnalytics } from '../types';
import { useToast } from '../context/ToastContext';

const PAGE_SIZE = 10;

const isStaff = (u: User) => u.role === 'ADMIN' || u.role === 'SUPER_ADMIN';

// Strip anything dangerous before rendering authored HTML.
const sanitize = (html: string) =>
  (html || '')
    .replace(/<\s*script[^>]*>[\s\S]*?<\s*\/\s*script>/gi, '')
    .replace(/<\s*style[^>]*>[\s\S]*?<\s*\/\s*style>/gi, '')
    .replace(/ on\w+\s*=\s*"[^"]*"/gi, '')
    .replace(/ on\w+\s*=\s*'[^']*'/gi, '')
    .replace(/javascript:/gi, '');

const StatusPill: React.FC<{ status: string }> = ({ status }) => {
  const pub = status === 'PUBLISHED';
  return (
    <span style={{ padding:'2px 10px',borderRadius:12,fontSize:11,fontWeight:700,whiteSpace:'nowrap',
      background:pub?T.successBg:T.warningBg, color:pub?T.success:T.warning }}>
      {pub ? 'Published' : 'Draft'}
    </span>
  );
};

// ─── Lightweight rich-text editor (contentEditable + toolbar) ─────────────────
const RichTextEditor: React.FC<{ value: string; onChange: (html: string) => void }> = ({ value, onChange }) => {
  const ref = useRef<HTMLDivElement>(null);
  // Seed the initial HTML once (avoid clobbering the caret on every keystroke).
  useEffect(() => { if (ref.current && ref.current.innerHTML !== value) ref.current.innerHTML = value || ''; /* eslint-disable-next-line */ }, []);
  const exec = (cmd: string, arg?: string) => {
    document.execCommand(cmd, false, arg);
    ref.current?.focus();
    onChange(ref.current?.innerHTML || '');
  };
  const tools: Array<{ label: string; title: string; run: () => void; style?: React.CSSProperties }> = [
    { label: 'B', title: 'Bold', run: () => exec('bold'), style: { fontWeight: 800 } },
    { label: 'I', title: 'Italic', run: () => exec('italic'), style: { fontStyle: 'italic' } },
    { label: 'U', title: 'Underline', run: () => exec('underline'), style: { textDecoration: 'underline' } },
    { label: 'H2', title: 'Heading', run: () => exec('formatBlock', 'H2') },
    { label: 'H3', title: 'Subheading', run: () => exec('formatBlock', 'H3') },
    { label: '“ ”', title: 'Quote', run: () => exec('formatBlock', 'BLOCKQUOTE') },
    { label: '• List', title: 'Bullet list', run: () => exec('insertUnorderedList') },
    { label: '1. List', title: 'Numbered list', run: () => exec('insertOrderedList') },
    { label: '🔗', title: 'Link', run: () => { const u = window.prompt('Link URL'); if (u) exec('createLink', u); } },
    { label: '⨯', title: 'Clear formatting', run: () => exec('removeFormat') },
  ];
  return (
    <div style={{ border:`1.5px solid ${T.border}`, borderRadius:10, overflow:'hidden' }}>
      <div style={{ display:'flex',flexWrap:'wrap',gap:4,padding:'7px 8px',background:T.canvas,borderBottom:`1px solid ${T.border}` }}>
        {tools.map(t => (
          <button key={t.title} type="button" title={t.title} onMouseDown={e=>e.preventDefault()} onClick={t.run}
            style={{ minWidth:30,height:28,padding:'0 8px',border:`1px solid ${T.border}`,borderRadius:7,background:T.surface,
              color:T.textMain,fontSize:12,cursor:'pointer',fontFamily:'inherit',...t.style }}>
            {t.label}
          </button>
        ))}
      </div>
      <div ref={ref} contentEditable suppressContentEditableWarning
        onInput={() => onChange(ref.current?.innerHTML || '')}
        style={{ minHeight:220,padding:'12px 14px',fontSize:14,lineHeight:1.7,color:T.textMain,outline:'none',background:T.surface }} />
    </div>
  );
};

// ─── Dashboard summary cards ──────────────────────────────────────────────────
const BlogSummaryCards: React.FC = () => {
  const [stats, setStats] = useState<BlogStats | null>(null);
  useEffect(() => { blogAPI.stats().then(setStats).catch(() => setStats(null)); }, []);
  if (!stats) return null;
  const cards = [
    { icon:'📚', label:'Total Blogs', value:stats.total, color:T.blue },
    { icon:'✅', label:'Published', value:stats.published, color:T.success },
    { icon:'🗒', label:'Drafts', value:stats.draft, color:T.warning },
    { icon:'👁', label:'Total Views', value:stats.totalViews.toLocaleString(), color:T.info },
    { icon:'🏷', label:'Categories', value:stats.totalCategories, color:T.blue },
    { icon:'🔥', label:'Most Viewed', value:stats.mostViewed ? stats.mostViewed.title : '—',
      sub:stats.mostViewed ? `${stats.mostViewed.views.toLocaleString()} views` : undefined, color:T.danger },
  ];
  return (
    <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:18 }}>
      {cards.map(c => <StatCard key={c.label} icon={c.icon} label={c.label} value={c.value} sub={(c as any).sub} color={c.color} />)}
    </div>
  );
};

// ─── Blog details (inline view) ───────────────────────────────────────────────
const BlogDetailsView: React.FC<{ id: number; onBack: () => void }> = ({ id, onBack }) => {
  const [post, setPost] = useState<BlogPost | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { blogAPI.get(id).then(setPost).catch(() => setPost(null)).finally(() => setLoading(false)); }, [id]);
  if (loading) return <LoadingScreen label="Loading blog…" />;
  if (!post) return <Card style={{ padding:24 }}><p>Blog not found.</p><Btn variant="secondary" onClick={onBack} style={{ marginTop:12 }}>← Back</Btn></Card>;
  const info: Array<[string, React.ReactNode]> = [
    ['Title', post.title],
    ['Category', post.category || '—'],
    ['Author', post.author],
    ['Created Date', formatDateTime(post.createdAt || '')],
    ['Last Updated', post.updatedAt ? formatDateTime(post.updatedAt) : '—'],
    ['Status', <StatusPill status={post.status} />],
  ];
  const related = [
    { label:'Views', value:post.views, icon:'👁' },
    { label:'Likes', value:post.likes, icon:'❤' },
    { label:'Shares', value:post.shares, icon:'↗' },
    { label:'Comments', value:post.commentsCount, icon:'💬' },
  ];
  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:16 }}>
        <Btn variant="secondary" onClick={onBack}>← Back to list</Btn>
      </div>
      {post.coverImage && (
        <Card style={{ marginBottom:16,padding:0,overflow:'hidden' }}>
          <img src={post.coverImage} alt="" style={{ width:'100%',maxHeight:280,objectFit:'cover',display:'block' }} />
        </Card>
      )}
      <Card style={{ padding:20,marginBottom:16 }}>
        <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800,color:T.textMain }}>Blog Information</h3>
        <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:14 }}>
          {info.map(([k, v]) => (
            <div key={String(k)}>
              <p style={{ margin:0,fontSize:10,fontWeight:700,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em' }}>{k}</p>
              <p style={{ margin:'3px 0 0',fontSize:14,fontWeight:600,color:T.textMain }}>{v}</p>
            </div>
          ))}
        </div>
        {post.tags && post.tags.length > 0 && (
          <div style={{ display:'flex',gap:6,flexWrap:'wrap',marginTop:14 }}>
            {post.tags.map(t => <span key={t} style={{ background:T.infoBg,color:T.blue,padding:'2px 10px',borderRadius:12,fontSize:11,fontWeight:700 }}>#{t}</span>)}
          </div>
        )}
      </Card>
      <Card style={{ padding:20,marginBottom:16 }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800,color:T.textMain }}>Blog Content</h3>
        {post.shortDescription && <p style={{ fontSize:14,color:T.textMuted,fontStyle:'italic',marginBottom:14 }}>{post.shortDescription}</p>}
        <div style={{ fontSize:14,lineHeight:1.8,color:T.textMain }} dangerouslySetInnerHTML={{ __html: sanitize(post.content || '') }} />
      </Card>
      <Card style={{ padding:20 }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800,color:T.textMain }}>Related Information</h3>
        <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(120px,1fr))',gap:12 }}>
          {related.map(r => (
            <div key={r.label} style={{ background:T.canvas,borderRadius:12,padding:'14px',textAlign:'center' }}>
              <div style={{ fontSize:18 }}>{r.icon}</div>
              <div style={{ fontSize:20,fontWeight:800,color:T.textMain,marginTop:4 }}>{r.value.toLocaleString()}</div>
              <div style={{ fontSize:11,color:T.textMuted }}>{r.label}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

// ─── Editor (create / edit) ───────────────────────────────────────────────────
const BlogEditor: React.FC<{
  heading: string; blogId?: number | null; onCancel?: () => void; onSaved?: () => void;
}> = ({ heading, blogId, onCancel, onSaved }) => {
  const { showToast } = useToast();
  const [cats, setCats] = useState<BlogCategory[]>([]);
  const [title, setTitle] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [shortDesc, setShortDesc] = useState('');
  const [content, setContent] = useState('');
  const [cover, setCover] = useState<string | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [tagsText, setTagsText] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(!!blogId);

  useEffect(() => { blogCategoryAPI.list().then(setCats).catch(() => setCats([])); }, []);
  useEffect(() => {
    if (!blogId) return;
    setLoading(true);
    blogAPI.get(blogId).then(b => {
      setTitle(b.title); setCategoryId(b.categoryId ? String(b.categoryId) : '');
      setShortDesc(b.shortDescription || ''); setContent(b.content || '');
      setCover(b.coverImage || null); setImages(b.images || []); setTagsText((b.tags || []).join(', '));
    }).catch(() => showToast('Could not load blog', 'error')).finally(() => setLoading(false));
  }, [blogId]); // eslint-disable-line

  const onCover = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    if (f.size > 2 * 1024 * 1024) { showToast('Image must be under 2 MB', 'error'); return; }
    setCover(await fileToDataUrl(f));
  };
  const onImages = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    for (const f of files) {
      if (f.size > 2 * 1024 * 1024) { showToast(`${f.name} is over 2 MB`, 'error'); continue; }
      const url = await fileToDataUrl(f);
      setImages(prev => [...prev, url]);
    }
  };

  const save = async (status: 'DRAFT' | 'PUBLISHED') => {
    if (!title.trim()) { showToast('Enter a title', 'error'); return; }
    setBusy(true);
    try {
      const payload = {
        title: title.trim(),
        category_id: categoryId ? Number(categoryId) : null,
        short_description: shortDesc,
        content,
        cover_image: cover,
        images,
        tags: tagsText.split(',').map(t => t.trim()).filter(Boolean),
        status,
      };
      if (blogId) await blogAPI.update(blogId, payload); else await blogAPI.create(payload);
      showToast(blogId ? 'Blog updated' : (status === 'PUBLISHED' ? 'Blog published' : 'Draft saved'));
      onSaved?.();
      if (!blogId && !onSaved) {
        setTitle(''); setCategoryId(''); setShortDesc(''); setContent(''); setCover(null); setImages([]); setTagsText('');
      }
    } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to save blog', 'error'); }
    finally { setBusy(false); }
  };

  if (loading) return <LoadingScreen label="Loading blog…" />;
  const catOptions = [{ value: '', label: '— Uncategorized —' }, ...cats.map(c => ({ value: String(c.id), label: c.name }))];

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>{heading}</h2>
        {onCancel && <Btn variant="secondary" onClick={onCancel}>← Cancel</Btn>}
      </div>
      <Card style={{ padding:20 }}>
        <Input label="Blog Title" value={title} onChange={e => setTitle(e.target.value)} placeholder="A clear, specific headline" required />
        <Sel label="Blog Category" value={categoryId} onChange={e => setCategoryId(e.target.value)} options={catOptions} />
        <div style={{ marginBottom:16 }}>
          <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Short Description</label>
          <textarea value={shortDesc} onChange={e => setShortDesc(e.target.value)} placeholder="One or two sentences shown in listings…"
            style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:64 }} />
        </div>
        <div style={{ marginBottom:16 }}>
          <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Blog Content</label>
          <RichTextEditor value={content} onChange={setContent} />
        </div>
        <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:16 }}>
          <div>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Cover Image</label>
            <input type="file" accept="image/*" onChange={onCover} style={{ fontSize:12 }} />
            {cover && <div style={{ marginTop:8 }}><img src={cover} alt="" style={{ maxHeight:120,maxWidth:'100%',objectFit:'contain',borderRadius:8,border:`1px solid ${T.border}` }} /><br /><span onClick={() => setCover(null)} style={{ fontSize:11,color:T.danger,cursor:'pointer',fontWeight:700 }}>Remove</span></div>}
          </div>
          <div>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Additional Images</label>
            <input type="file" accept="image/*" multiple onChange={onImages} style={{ fontSize:12 }} />
            {images.length > 0 && (
              <div style={{ display:'flex',gap:6,flexWrap:'wrap',marginTop:8 }}>
                {images.map((im, i) => (
                  <div key={i} style={{ position:'relative' }}>
                    <img src={im} alt="" style={{ height:56,width:56,objectFit:'cover',borderRadius:8,border:`1px solid ${T.border}` }} />
                    <span onClick={() => setImages(prev => prev.filter((_, j) => j !== i))}
                      style={{ position:'absolute',top:-6,right:-6,background:T.danger,color:'#fff',borderRadius:'50%',width:18,height:18,display:'flex',alignItems:'center',justifyContent:'center',fontSize:11,cursor:'pointer' }}>✕</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        <div style={{ marginTop:16 }}>
          <Input label="Tags (comma separated)" value={tagsText} onChange={e => setTagsText(e.target.value)} placeholder="fraud, upi, security" />
        </div>
        <div style={{ display:'flex',gap:10,marginTop:8 }}>
          <Btn variant="secondary" onClick={() => save('DRAFT')} disabled={busy || !title.trim()}>{busy ? 'Saving…' : 'Save Draft'}</Btn>
          <Btn onClick={() => save('PUBLISHED')} disabled={busy || !title.trim()}>{busy ? 'Saving…' : 'Publish Blog'}</Btn>
        </div>
      </Card>
    </div>
  );
};

// ─── Blog table (list + filters + pagination), used by All / Published / Draft ──
const BlogTable: React.FC<{ user: User; title: string; subtitle?: string; fixedStatus?: string; showSummary?: boolean }> =
({ user, title, subtitle, fixedStatus, showSummary }) => {
  const { showToast } = useToast();
  const staff = isStaff(user);
  const canDelete = user.role === 'SUPER_ADMIN';
  const [cats, setCats] = useState<BlogCategory[]>([]);
  const [items, setItems] = useState<BlogPost[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  // filters
  const [statusF, setStatusF] = useState(fixedStatus || '');
  const [catF, setCatF] = useState('');
  const [author, setAuthor] = useState('');
  const [q, setQ] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  // inline views
  const [viewId, setViewId] = useState<number | null>(null);
  const [editId, setEditId] = useState<number | null>(null);

  useEffect(() => { blogCategoryAPI.list().then(setCats).catch(() => setCats([])); }, []);

  const load = useCallback(() => {
    const params: BlogListParams = { limit: PAGE_SIZE, offset: page * PAGE_SIZE };
    if (fixedStatus) params.status = fixedStatus; else if (statusF) params.status = statusF;
    if (catF) params.category_id = Number(catF);
    if (author.trim()) params.author = author.trim();
    if (q.trim()) params.q = q.trim();
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    setLoading(true);
    blogAPI.list(params).then(r => { setItems(r.items); setTotal(r.total); })
      .catch(() => { setItems([]); setTotal(0); }).finally(() => setLoading(false));
  }, [page, statusF, catF, author, q, dateFrom, dateTo, fixedStatus]);

  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [load]);
  // Reset to page 0 whenever a filter changes.
  useEffect(() => { setPage(0); }, [statusF, catF, author, q, dateFrom, dateTo]);

  const refresh = () => load();

  const doStatus = async (b: BlogPost, status: string) => {
    try { await blogAPI.setStatus(b.id, status); showToast(status === 'PUBLISHED' ? 'Blog published' : 'Blog unpublished'); refresh(); }
    catch { showToast('Failed to update status', 'error'); }
  };
  const doDelete = async (b: BlogPost) => {
    if (!window.confirm(`Delete "${b.title}"? This cannot be undone.`)) return;
    try { await blogAPI.remove(b.id); showToast('Blog deleted'); refresh(); }
    catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to delete', 'error'); }
  };

  if (viewId) return <BlogDetailsView id={viewId} onBack={() => { setViewId(null); refresh(); }} />;
  if (editId) return <BlogEditor heading="Edit Blog" blogId={editId} onCancel={() => setEditId(null)} onSaved={() => { setEditId(null); refresh(); }} />;

  const catOptions = [{ value:'', label:'All Categories' }, ...cats.map(c => ({ value:String(c.id), label:c.name }))];
  const headers = ['Blog ID','Title','Category','Author','Status','Views','Created','Updated','Action'];
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min(total, (page + 1) * PAGE_SIZE);

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18,flexWrap:'wrap',gap:10 }}>
        <div>
          <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>{title}</h2>
          {subtitle && <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{subtitle}</p>}
        </div>
      </div>

      {showSummary && <BlogSummaryCards />}

      <Card style={{ padding:14,marginBottom:16 }}>
        <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(150px,1fr))',gap:10,alignItems:'end' }}>
          <Input label="Search" value={q} onChange={e => setQ(e.target.value)} placeholder="Title / ID / author" icon="🔍" style={{ marginBottom:0 }} />
          <div style={{ marginBottom:0 }}>
            <Sel label="Category" value={catF} onChange={e => setCatF(e.target.value)} options={catOptions} />
          </div>
          {!fixedStatus && staff && (
            <div style={{ marginBottom:0 }}>
              <Sel label="Status" value={statusF} onChange={e => setStatusF(e.target.value)}
                options={[{ value:'', label:'All Statuses' }, { value:'PUBLISHED', label:'Published' }, { value:'DRAFT', label:'Draft' }]} />
            </div>
          )}
          <Input label="Author" value={author} onChange={e => setAuthor(e.target.value)} placeholder="Author name" style={{ marginBottom:0 }} />
          <Input label="From" type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} style={{ marginBottom:0 }} />
          <Input label="To" type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} style={{ marginBottom:0 }} />
        </div>
      </Card>

      <Card>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {headers.map(h => (
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}`,whiteSpace:'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && <tr><td colSpan={headers.length} style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading…</td></tr>}
              {!loading && items.length === 0 && <tr><td colSpan={headers.length} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No blogs found.</td></tr>}
              {!loading && items.map((b, i) => (
                <tr key={b.id} className="c5-row-in" style={{ background:i%2===0?T.surface:'#f8faff', ['--c5-i' as any]: Math.min(i, 12) }}>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontWeight:700 }}>#{b.id}</td>
                  <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain,maxWidth:260 }}>{b.title}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{b.category || '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{b.author}</td>
                  <td style={{ padding:'11px 14px' }}><StatusPill status={b.status} /></td>
                  <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{b.views.toLocaleString()}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{b.createdAt ? formatDateTime(b.createdAt) : '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{b.updatedAt ? formatDateTime(b.updatedAt) : '—'}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',gap:6,flexWrap:'wrap' }}>
                      <Btn size="sm" variant="ghost" onClick={() => setViewId(b.id)}>👁 View</Btn>
                      {staff && <Btn size="sm" variant="secondary" onClick={() => setEditId(b.id)}>✎ Edit</Btn>}
                      {staff && (b.status === 'PUBLISHED'
                        ? <Btn size="sm" variant="secondary" onClick={() => doStatus(b, 'DRAFT')}>Unpublish</Btn>
                        : <Btn size="sm" variant="primary" onClick={() => doStatus(b, 'PUBLISHED')}>Publish</Btn>)}
                      {canDelete && <Btn size="sm" variant="danger" onClick={() => doDelete(b)}>Delete</Btn>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',padding:'12px 14px',borderTop:`1px solid ${T.borderLight}` }}>
          <span style={{ fontSize:12,color:T.textMuted }}>{total === 0 ? 'No results' : `Showing ${start}–${end} of ${total}`}</span>
          <div style={{ display:'flex',gap:8 }}>
            <Btn size="sm" variant="secondary" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>← Prev</Btn>
            <Btn size="sm" variant="secondary" onClick={() => setPage(p => p + 1)} disabled={(page + 1) * PAGE_SIZE >= total}>Next →</Btn>
          </div>
        </div>
      </Card>
    </div>
  );
};

// ─── Exported pages (wired in App.tsx) ────────────────────────────────────────
export const AllBlogsPage: React.FC<{ user: User }> = ({ user }) => (
  <BlogTable user={user} title="All Blogs" subtitle="Every article across the platform" showSummary />
);

export const PublishedBlogsPage: React.FC<{ user: User }> = ({ user }) => (
  <BlogTable user={user} title="Published Blogs" subtitle="Live articles visible to readers" fixedStatus="PUBLISHED" />
);

export const DraftBlogsPage: React.FC<{ user: User }> = ({ user }) => (
  <BlogTable user={user} title="Draft Blogs" subtitle="Unpublished work in progress" fixedStatus="DRAFT" />
);

export const CreateBlogPage: React.FC<{ user: User }> = () => (
  <BlogEditor heading="Create Blog" />
);

// ─── Categories management ────────────────────────────────────────────────────
export const BlogCategoriesPage: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const staff = isStaff(user);
  const canDelete = user.role === 'SUPER_ADMIN';
  const [cats, setCats] = useState<BlogCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<BlogCategory | null>(null);
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = () => blogCategoryAPI.list().then(setCats).catch(() => setCats([]));
  useEffect(() => { reload().finally(() => setLoading(false)); }, []);

  const startNew = () => { setEditing(null); setName(''); setDesc(''); };
  const startEdit = (c: BlogCategory) => { setEditing(c); setName(c.name); setDesc(c.description || ''); };

  const save = async () => {
    if (!name.trim()) { showToast('Enter a category name', 'error'); return; }
    setBusy(true);
    try {
      if (editing) await blogCategoryAPI.update(editing.id, { name: name.trim(), description: desc });
      else await blogCategoryAPI.create({ name: name.trim(), description: desc });
      showToast(editing ? 'Category updated' : 'Category created');
      startNew(); await reload();
    } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to save category', 'error'); }
    finally { setBusy(false); }
  };
  const remove = async (c: BlogCategory) => {
    if (!window.confirm(`Delete category "${c.name}"? Posts will become uncategorized.`)) return;
    try { await blogCategoryAPI.remove(c.id); showToast('Category deleted'); await reload(); }
    catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to delete', 'error'); }
  };

  if (loading) return <LoadingScreen label="Loading categories…" />;
  return (
    <div>
      <h2 style={{ margin:'0 0 18px',fontSize:16,fontWeight:800 }}>Blog Categories</h2>
      <div style={{ display:'grid',gridTemplateColumns: staff ? '1fr 320px' : '1fr',gap:16,alignItems:'start' }}>
        <Card>
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['Category','Description','Posts', ...(staff ? ['Action'] : [])].map(h => (
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cats.length === 0 && <tr><td colSpan={staff ? 4 : 3} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No categories yet.</td></tr>}
                {cats.map((c, i) => (
                  <tr key={c.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{c.name}</td>
                    <td style={{ padding:'11px 14px',color:T.textMuted }}>{c.description || '—'}</td>
                    <td style={{ padding:'11px 14px',color:T.textMuted }}>{c.postCount ?? 0}</td>
                    {staff && (
                      <td style={{ padding:'11px 14px' }}>
                        <div style={{ display:'flex',gap:6 }}>
                          <Btn size="sm" variant="ghost" onClick={() => startEdit(c)}>Edit</Btn>
                          {canDelete && <Btn size="sm" variant="danger" onClick={() => remove(c)}>Delete</Btn>}
                        </div>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        {staff && (
          <Card style={{ padding:18 }}>
            <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>{editing ? 'Edit Category' : 'New Category'}</h3>
            <Input label="Name" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Payment Security" required />
            <div style={{ marginBottom:16 }}>
              <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Description</label>
              <textarea value={desc} onChange={e => setDesc(e.target.value)} placeholder="Short description…"
                style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:70 }} />
            </div>
            <div style={{ display:'flex',gap:10 }}>
              <Btn onClick={save} disabled={busy || !name.trim()}>{busy ? 'Saving…' : (editing ? 'Save' : 'Create')}</Btn>
              {editing && <Btn variant="secondary" onClick={startNew}>Cancel</Btn>}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
};

// ─── Analytics ────────────────────────────────────────────────────────────────
export const BlogAnalyticsPage: React.FC<{ user: User }> = () => {
  const [data, setData] = useState<BlogAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { blogAPI.analytics().then(setData).catch(() => setData(null)).finally(() => setLoading(false)); }, []);
  if (loading) return <LoadingScreen label="Loading analytics…" />;
  if (!data) return <Card style={{ padding:24 }}><p>Analytics unavailable.</p></Card>;

  const palette = [T.blue, T.green, T.info, T.warning, T.danger, '#8B5CF6', '#00C2A8'];
  const catChart = data.categoryPerformance.map((c, i) => ({ label: c.category, value: c.views, color: palette[i % palette.length] }));
  const monthlyViews = data.monthly.map(m => ({ label: m.month, value: m.views, color: T.blue }));
  const monthlyPublished = data.monthly.map(m => ({ label: m.month, value: m.published, color: T.green }));

  return (
    <div>
      <h2 style={{ margin:'0 0 18px',fontSize:16,fontWeight:800 }}>Blog Analytics</h2>

      <Card style={{ padding:20,marginBottom:16 }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800 }}>Top Viewed Blogs</h3>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Blog Name','Views','Reads','Avg Read Time'].map(h => (
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.topViewed.length === 0 && <tr><td colSpan={4} style={{ padding:24,textAlign:'center',color:T.textMuted }}>No data yet.</td></tr>}
              {data.topViewed.map((t, i) => (
                <tr key={t.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{t.title}</td>
                  <td style={{ padding:'11px 14px',color:T.textMain }}>{t.views.toLocaleString()}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{t.reads.toLocaleString()}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{t.avgReadTime} min</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(280px,1fr))',gap:16,marginBottom:16 }}>
        <Card style={{ padding:20 }}>
          <h3 style={{ margin:'0 0 6px',fontSize:14,fontWeight:800 }}>Category Performance</h3>
          <p style={{ margin:'0 0 14px',fontSize:12,color:T.textMuted }}>Most popular: <b style={{ color:T.textMain }}>{data.mostPopularCategory || '—'}</b></p>
          {catChart.length > 0 ? <StatusChart data={catChart} /> : <p style={{ fontSize:12,color:T.textMuted }}>No data.</p>}
        </Card>
        <Card style={{ padding:20 }}>
          <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>Monthly Views</h3>
          {monthlyViews.length > 0 ? <StatusChart data={monthlyViews} /> : <p style={{ fontSize:12,color:T.textMuted }}>No data.</p>}
        </Card>
        <Card style={{ padding:20 }}>
          <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>Blogs Published / Month</h3>
          {monthlyPublished.length > 0 ? <StatusChart data={monthlyPublished} /> : <p style={{ fontSize:12,color:T.textMuted }}>No data.</p>}
        </Card>
      </div>
    </div>
  );
};
