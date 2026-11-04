import os
import re
import json
import hashlib
import hmac
import secrets
import shutil
import difflib
import xml.etree.ElementTree as etree
from datetime import date, datetime
from html import escape as html_escape
from pathlib import Path
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, abort, session, send_from_directory,
)
from markupsafe import Markup

load_dotenv()

import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.tables import TableExtension
from markdown.extensions.toc import TocExtension
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

# Simple password auth - set NOTEWALL_PASSWORD env variable
ADMIN_PASSWORD = os.environ.get('NOTEWALL_PASSWORD', 'admin')

# Notes directory (notes/ folder inside Notewall, or parent folder if notes/ doesn't exist)
NOTEWALL_DIR = Path(__file__).parent
NOTES_DIR = NOTEWALL_DIR / 'notes'
if not NOTES_DIR.exists():
    NOTES_DIR = NOTEWALL_DIR.parent  # Fallback to parent for existing setups

# Uploads live outside static/ so they can only be fetched through
# uploaded_file(), which checks that the visitor may read a note using them.
UPLOAD_DIR = NOTEWALL_DIR / 'uploads'
LEGACY_UPLOAD_DIR = NOTEWALL_DIR / 'static' / 'uploads'
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR


def migrate_uploads():
    """Move images from the old public static/uploads folder."""
    if not LEGACY_UPLOAD_DIR.exists():
        return
    for path in sorted(LEGACY_UPLOAD_DIR.rglob('*')):
        if path.is_file():
            target = UPLOAD_DIR / path.relative_to(LEGACY_UPLOAD_DIR)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
    shutil.rmtree(LEGACY_UPLOAD_DIR, ignore_errors=True)


migrate_uploads()

# Old notes link images as /static/uploads/..., rewritten when rendering.
LEGACY_UPLOAD_URL = '/static/uploads/'

SETTINGS_FILE = NOTEWALL_DIR / 'settings.json'
# Per-note access rules, custom code and the list of viewers.
META_FILE = NOTES_DIR / '.notewall.json'
# Older versions only stored a list of private slugs here.
LEGACY_PRIVATE_FILE = NOTES_DIR / '.private_notes.json'

VISIBILITIES = ('public', 'shared', 'private')

# Earlier versions of every note, newest last: .history/<slug>/<timestamp>.md
HISTORY_DIR = NOTES_DIR / '.history'
HISTORY_LIMIT = 50

# Default settings
DEFAULT_SETTINGS = {
    'site_title': 'Notewall',
    'theme': 'dark',
    'accent_color': '#6366f1',
    'editor_height': 500,
    'font_size': 16,
    'editor_font_size': 15,
    'show_line_numbers': False,
    'show_subtopics': True,
    'auto_save': False,
}


def load_settings():
    """Load settings from JSON file."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                # Merge with defaults
                return {**DEFAULT_SETTINGS, **saved}
        except:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to JSON file."""
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)


# Make settings available to all templates
@app.context_processor
def inject_settings():
    return {
        'settings': load_settings(),
        'logged_in': session.get('logged_in', False),
        'viewer': current_viewer(),
    }

# Markdown processor is created dynamically in render_markdown


def load_meta():
    """Load viewers and per-note metadata (access rules, custom code)."""
    meta = {'viewers': {}, 'notes': {}}
    if META_FILE.exists():
        try:
            data = json.loads(META_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                meta['viewers'] = data.get('viewers') or {}
                meta['notes'] = data.get('notes') or {}
        except (OSError, ValueError):
            pass
    elif LEGACY_PRIVATE_FILE.exists():
        # One-time migration from the old private-notes list.
        try:
            slugs = json.loads(LEGACY_PRIVATE_FILE.read_text(encoding='utf-8'))
            for slug in slugs if isinstance(slugs, list) else []:
                meta['notes'][str(slug)] = {'visibility': 'private'}
            save_meta(meta)
        except (OSError, ValueError):
            pass
    return meta


def save_meta(meta):
    """Persist viewers and per-note metadata."""
    try:
        META_FILE.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8', newline='\n'
        )
    except OSError:
        pass


def note_meta(slug, meta=None):
    """Return a note's metadata with defaults filled in."""
    entry = (meta or load_meta())['notes'].get(slug, {})
    visibility = entry.get('visibility', 'public')
    return {
        'visibility': visibility if visibility in VISIBILITIES else 'public',
        'viewers': list(entry.get('viewers', [])),
        'roles': list(entry.get('roles', [])),
        'css': entry.get('css', ''),
        'js': entry.get('js', ''),
    }


def update_note_meta(slug, **changes):
    """Change some of a note's metadata, dropping values left at defaults."""
    meta = load_meta()
    entry = {**note_meta(slug, meta), **changes}
    tidy = {k: v for k, v in entry.items() if v}
    if tidy.get('visibility') == 'public':
        tidy.pop('visibility')
    if tidy:
        meta['notes'][slug] = tidy
    else:
        meta['notes'].pop(slug, None)
    save_meta(meta)


def hash_key(key):
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def generate_key():
    return secrets.token_urlsafe(12)


def find_viewer_by_key(key):
    """Return the id of the viewer owning this access key, or None."""
    if not key:
        return None
    digest = hash_key(key.strip())
    for viewer_id, viewer in load_meta()['viewers'].items():
        if hmac.compare_digest(viewer.get('key_hash', ''), digest):
            return None if viewer_expired(viewer) else viewer_id
    return None


def viewer_expired(viewer):
    """Keys stop working after the viewer's "expires" day."""
    try:
        return date.fromisoformat(viewer['expires']) < date.today()
    except (KeyError, TypeError, ValueError):
        return False


def parse_expiry(text):
    """Validate an <input type="date"> value, '' meaning never."""
    try:
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        return ''


def record_visit(viewer_id, slug=None):
    """Remember when a viewer was last around, and which notes they opened."""
    meta = load_meta()
    viewer = meta['viewers'].get(viewer_id)
    if viewer is None:
        return
    now = datetime.now().isoformat(timespec='seconds')
    viewer['last_seen'] = now
    if slug:
        viewer.setdefault('activity', {})[slug] = now
    save_meta(meta)


def current_viewer(meta=None):
    """Return the viewer signed in with an access key, if any."""
    viewer_id = session.get('viewer_id')
    if not viewer_id:
        return None
    viewer = (meta or load_meta())['viewers'].get(viewer_id)
    if viewer is None or viewer_expired(viewer):
        # The viewer was removed or their access ran out.
        session.pop('viewer_id', None)
        return None
    return {'id': viewer_id, 'name': viewer.get('name', viewer_id), 'roles': viewer.get('roles', [])}


def viewer_allowed(viewer, info):
    """Check a viewer against a shared note's allow lists."""
    return viewer['id'] in info['viewers'] or bool(set(viewer['roles']) & set(info['roles']))


def can_view(slug, meta=None):
    """Check whether the current visitor may read a note."""
    if session.get('logged_in'):
        return True
    meta = meta or load_meta()
    info = note_meta(slug, meta)
    if info['visibility'] == 'public':
        return True
    if info['visibility'] == 'private':
        return False
    viewer = current_viewer(meta)
    return bool(viewer) and viewer_allowed(viewer, info)


def all_roles(meta=None):
    """Every role given to at least one viewer."""
    roles = set()
    for viewer in (meta or load_meta())['viewers'].values():
        roles.update(viewer.get('roles', []))
    return sorted(roles)


def parse_roles(text):
    """Turn "family, friends" into ['family', 'friends']."""
    return sorted({slugify(part) for part in text.split(',') if slugify(part)})


def get_notes():
    """Get the markdown files in the notes directory the visitor may read."""
    meta = load_meta()
    notes = []
    for f in sorted(NOTES_DIR.glob('*.md')):
        slug = f.stem
        if not can_view(slug, meta):
            continue
        name = slug.replace('-', ' ').replace('_', ' ').title()
        notes.append({
            'slug': slug,
            'name': name,
            'filename': f.name,
            'visibility': note_meta(slug, meta)['visibility'],
        })
    return notes


def normalize_line_endings(text):
    """Normalize all line endings to LF."""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def read_note(slug):
    """Read a markdown file, return (content, exists)."""
    filepath = NOTES_DIR / f"{slug}.md"
    if filepath.exists():
        # Read with newline='' to preserve original newlines, then normalize.
        with open(filepath, 'r', encoding='utf-8', newline='') as f:
            raw = f.read()
        normalized = normalize_line_endings(raw)

        # Auto-repair legacy files that ended up with malformed Windows newlines.
        if raw != normalized:
            filepath.write_text(normalized, encoding='utf-8', newline='\n')

        return normalized, True
    return '', False


def save_note(slug, content):
    """Save content to a markdown file, keeping the previous version."""
    filepath = NOTES_DIR / f"{slug}.md"
    # Normalize line endings to avoid duplicated blank lines on Windows
    # when browser CRLF content is saved repeatedly.
    normalized = normalize_line_endings(content)
    previous, existed = read_note(slug)
    if existed and previous != normalized:
        snapshot_note(slug, previous)
    filepath.write_text(normalized, encoding='utf-8', newline='\n')


def snapshot_note(slug, content):
    """Store a version in the note's history, dropping the oldest ones."""
    folder = HISTORY_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path, n = folder / f'{stamp}.md', 2
    while path.exists():
        # Several saves within one second each keep their own version.
        path, n = folder / f'{stamp}-{n:02d}.md', n + 1
    path.write_text(content, encoding='utf-8', newline='\n')
    for old in sorted(folder.glob('*.md'), key=lambda p: p.stem)[:-HISTORY_LIMIT]:
        old.unlink()


def list_versions(slug):
    """A note's saved versions, newest first."""
    folder = HISTORY_DIR / slug
    versions = []
    for path in sorted(folder.glob('*.md'), key=lambda p: p.stem, reverse=True) if folder.exists() else []:
        try:
            saved = datetime.strptime(path.stem[:15], '%Y%m%d-%H%M%S')
        except ValueError:
            continue
        versions.append({'id': path.stem, 'saved': saved, 'path': path})
    return versions


def read_version(slug, version_id):
    for version in list_versions(slug):
        if version['id'] == version_id:
            return version, version['path'].read_text(encoding='utf-8')
    abort(404)


def diff_lines(old, new):
    """Line diff for the history page: [(kind, text)] with kind in +, -, ' ', @."""
    lines = []
    for line in list(difflib.unified_diff(old.split('\n'), new.split('\n'), lineterm='', n=3))[2:]:
        kind = line[:1] if line[:1] in '+-@' else ' '
        lines.append((kind, line[1:] if kind != '@' else line))
    return lines


def delete_note(slug):
    """Delete a markdown file."""
    filepath = NOTES_DIR / f"{slug}.md"
    if filepath.exists():
        filepath.unlink()
    # Drop the note's access rules, custom code and viewer activity with it.
    # Its history is kept, so a deleted note can still be recovered.
    meta = load_meta()
    meta['notes'].pop(slug, None)
    for viewer in meta['viewers'].values():
        viewer.get('activity', {}).pop(slug, None)
    save_meta(meta)


def protect_math(text):
    """Hide $...$ / $$...$$ from Markdown so characters like * survive.

    Returns the text with placeholders and the list of original expressions.
    MathJax renders the expressions after Markdown has done its job.
    """
    expressions = []

    def store(match):
        expression = match.group(0)
        digest = hashlib.sha1(expression.encode('utf-8')).hexdigest()[:10]
        token = f'@@notewallmath{digest}@@'
        expressions.append((token, expression))
        return token

    # Display math first, so $$...$$ is not treated as two inline expressions.
    text = re.sub(r'(?<!\\)\$\$(.+?)(?<!\\)\$\$', store, text, flags=re.S)
    # Require non-space characters next to the delimiters so prices like
    # "$5 and $10" are not mistaken for inline math.
    text = re.sub(
        r'(?<!\\)\$(?!\$)(?=\S)([^\n$]+?)(?<=\S)(?<!\\)\$',
        store,
        text,
    )
    return text, expressions


# [[Note name]] or [[Note name|shown text]]
WIKILINK_RE = r'\[\[([^\]|\n]+?)(?:\|([^\]\n]+))?\]\]'


def find_note_slug(target):
    """Match a [[link]] target to an existing note, ignoring case and spacing."""
    wanted = slugify(target)
    for f in NOTES_DIR.glob('*.md'):
        if slugify(f.stem) == wanted:
            return f.stem
    return None


class WikiLinkProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        target = m.group(1).strip()
        label = (m.group(2) or target).strip()
        slug = find_note_slug(target)

        if slug and can_view(slug):
            el = etree.Element('a', {'href': url_for('view_note', slug=slug), 'class': 'wikilink'})
        elif not slug and session.get('logged_in'):
            el = etree.Element('a', {
                'href': url_for('edit_note', slug=slugify(target)),
                'class': 'wikilink wikilink-missing',
                'title': 'This note does not exist yet - click to create it',
            })
        else:
            # Don't reveal whether a note the visitor can't open exists.
            el = etree.Element('span', {'class': 'wikilink-plain'})
        el.text = label
        return el, m.start(0), m.end(0)


class WikiLinkExtension(Extension):
    def extendMarkdown(self, md):
        # Above regular links (160) so [[...]] isn't read as [text], below
        # inline code (190) so it stays literal inside backticks.
        md.inlinePatterns.register(WikiLinkProcessor(WIKILINK_RE, md), 'notewall_wikilink', 175)


def find_backlinks(slug):
    """Readable notes that [[link]] to this one."""
    wanted = slugify(slug)
    backlinks = []
    for note in get_notes():
        if note['slug'] == slug:
            continue
        content, _ = read_note(note['slug'])
        if any(slugify(m.group(1)) == wanted for m in re.finditer(WIKILINK_RE, content)):
            backlinks.append(note)
    return backlinks


def highlight(text, pattern):
    """Escape text and wrap search matches in <mark>."""
    parts, last = [], 0
    for m in pattern.finditer(text):
        parts.append(html_escape(text[last:m.start()]))
        parts.append(f'<mark>{html_escape(m.group(0))}</mark>')
        last = m.end()
    parts.append(html_escape(text[last:]))
    return Markup(''.join(parts))


def search_notes(query, max_snippets=3):
    """Find readable notes containing every word of the query."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    pattern = re.compile('|'.join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)

    results = []
    for note in get_notes():
        content, _ = read_note(note['slug'])
        haystack, name = content.lower(), note['name'].lower()
        if not all(t in haystack or t in name for t in terms):
            continue

        snippets = []
        for line in content.split('\n'):
            m = pattern.search(line)
            if not m:
                continue
            start = max(0, m.start() - 60)
            piece = line[start:start + 180].strip()
            prefix = '… ' if start > 0 else ''
            suffix = ' …' if start + 180 < len(line) else ''
            snippets.append(Markup(prefix) + highlight(piece, pattern) + Markup(suffix))
            if len(snippets) == max_snippets:
                break

        results.append({
            **note,
            'title': highlight(note['name'], pattern),
            'snippets': snippets,
            'score': sum(haystack.count(t) for t in terms) + 10 * sum(t in name for t in terms),
            'first_term': next((t for t in terms if t in haystack), ''),
        })
    return sorted(results, key=lambda r: -r['score'])


def render_markdown(text):
    """Convert markdown to HTML."""
    settings = load_settings()
    toc_depth = '1-2' if settings.get('show_subtopics', True) else '1'

    text = text.replace(LEGACY_UPLOAD_URL, '/uploads/')
    protected_text, math_expressions = protect_math(text)

    md = markdown.Markdown(
        extensions=[
            'fenced_code',
            'tables',
            'toc',
            CodeHiliteExtension(css_class='highlight', guess_lang=False),
            WikiLinkExtension(),
        ],
        extension_configs={
            'toc': {
                'toc_depth': toc_depth,
            }
        }
    )

    html = md.convert(protected_text)

    # Put the math back, escaping HTML metacharacters so a note cannot inject
    # markup through an equation. MathJax reads the decoded text content.
    def restore_math(markup):
        for token, expression in math_expressions:
            markup = markup.replace(token, html_escape(expression, quote=False))
        return markup

    html = restore_math(html)
    toc = restore_math(getattr(md, 'toc', ''))
    return html, toc


@app.template_filter('ago')
def time_ago(value):
    """'3 h ago' style label for an ISO timestamp or datetime."""
    if not value:
        return 'never'
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    seconds = max(0, int((datetime.now() - value).total_seconds()))
    for unit, size in (('d', 86400), ('h', 3600), ('min', 60)):
        if seconds >= size:
            return f'{seconds // size} {unit} ago'
    return 'just now'


def slugify(text):
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text


def require_auth(f):
    """Simple auth check decorator."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# Routes

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        secret = request.form.get('password', '')
        if secret == ADMIN_PASSWORD:
            session.pop('viewer_id', None)
            session['logged_in'] = True
            return redirect(url_for('home'))
        viewer_id = find_viewer_by_key(secret)
        if viewer_id:
            session['viewer_id'] = viewer_id
            record_visit(viewer_id)
            return redirect(url_for('home'))
        return render_template('auth.html', error='Wrong password or key', notes=get_notes())
    return render_template('auth.html', notes=get_notes())


@app.route('/k/<key>')
def key_login(key):
    """One-click sign-in link handed out to viewers."""
    viewer_id = find_viewer_by_key(key)
    if not viewer_id:
        return render_template('auth.html', error='This link is no longer valid', notes=get_notes())
    if not session.get('logged_in'):
        session['viewer_id'] = viewer_id
        record_visit(viewer_id)
    return redirect(url_for('home'))


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('viewer_id', None)
    return redirect(url_for('home'))


@app.route('/')
def home():
    """Home page - list all notes."""
    notes = get_notes()
    return render_template('home.html', notes=notes)


@app.route('/note/<slug>')
def view_note(slug):
    """View a single note."""
    content, exists = read_note(slug)
    if not exists:
        return redirect(url_for('edit_note', slug=slug))

    meta = load_meta()
    if not can_view(slug, meta):
        abort(404)
    info = note_meta(slug, meta)

    viewer = current_viewer(meta)
    if viewer and not session.get('logged_in'):
        record_visit(viewer['id'], slug)

    html, toc = render_markdown(content)
    title = slug.replace('-', ' ').replace('_', ' ').title()
    notes = get_notes()
    has_toc = '<li>' in toc
    return render_template(
        'note.html',
        title=title,
        content=html,
        toc=toc,
        has_toc=has_toc,
        backlinks=find_backlinks(slug),
        raw_content=content,
        slug=slug,
        notes=notes,
        access=info,
        all_viewers=meta['viewers'],
        all_roles=all_roles(meta),
    )


@app.route('/search')
def search():
    """Full-text search over the notes the visitor may read."""
    query = request.args.get('q', '').strip()
    return render_template(
        'search.html',
        query=query,
        results=search_notes(query),
        notes=get_notes(),
    )


@app.route('/cms/history/<slug>')
@require_auth
def note_history(slug):
    """List a note's earlier versions."""
    current, exists = read_note(slug)
    versions = list_versions(slug)
    if not exists and not versions:
        abort(404)

    # Show how much each version differs from the note as it is now.
    for version in versions:
        # Counted as what restoring the version would add and remove.
        old = version['path'].read_text(encoding='utf-8')
        changes = diff_lines(current, old)
        version['added'] = sum(kind == '+' for kind, _ in changes)
        version['removed'] = sum(kind == '-' for kind, _ in changes)

    return render_template(
        'history.html',
        title=slug.replace('-', ' ').replace('_', ' ').title(),
        slug=slug,
        exists=exists,
        versions=versions,
        notes=get_notes(),
    )


@app.route('/cms/history/<slug>/<version_id>')
@require_auth
def note_version(slug, version_id):
    """Show what restoring a version would change."""
    version, old = read_version(slug, version_id)
    current, exists = read_note(slug)
    return render_template(
        'history.html',
        title=slug.replace('-', ' ').replace('_', ' ').title(),
        slug=slug,
        exists=exists,
        version=version,
        diff=diff_lines(current, old),
        notes=get_notes(),
    )


@app.route('/cms/history/<slug>/<version_id>/restore', methods=['POST'])
@require_auth
def restore_version(slug, version_id):
    """Bring back an earlier version; the current text goes into history."""
    _, old = read_version(slug, version_id)
    save_note(slug, old)
    return redirect(url_for('view_note', slug=slug))


@app.route('/cms/new')
@require_auth
def new_note():
    """Form to create a new note."""
    notes = get_notes()
    return render_template(
        'editor.html',
        title='New Note',
        slug='',
        content='',
        is_new=True,
        note=note_meta(''),
        notes=notes,
    )


@app.route('/cms/create', methods=['POST'])
@require_auth
def create_note():
    """Create a new note."""
    slug = slugify(request.form.get('slug', ''))
    content = request.form.get('content', '')
    
    if not slug:
        return redirect(url_for('new_note'))
    
    save_note(slug, content)
    if request.form.get('private') == 'on':
        update_note_meta(slug, visibility='private')
    return redirect(url_for('view_note', slug=slug))


@app.route('/cms/edit/<slug>')
@require_auth
def edit_note(slug):
    """Form to edit an existing note."""
    content, exists = read_note(slug)
    title = slug.replace('-', ' ').replace('_', ' ').title()
    notes = get_notes()
    return render_template(
        'editor.html',
        title=f'Edit: {title}',
        slug=slug,
        content=content,
        is_new=not exists,
        note=note_meta(slug),
        notes=notes,
    )


@app.route('/cms/save/<slug>', methods=['POST'])
@require_auth
def save_note_route(slug):
    """Save an edited note."""
    content = request.form.get('content', '')
    save_note(slug, content)
    if request.form.get('custom_code_present'):
        update_note_meta(
            slug,
            css=normalize_line_endings(request.form.get('note_css', '')).strip(),
            js=normalize_line_endings(request.form.get('note_js', '')).strip(),
        )

    # "Save" stays in the editor, "Update" returns to the note page.
    if request.form.get('action') == 'save':
        return redirect(url_for('edit_note', slug=slug, saved=1))
    return redirect(url_for('view_note', slug=slug))


@app.route('/cms/access/<slug>', methods=['POST'])
@require_auth
def note_access_route(slug):
    """Set who may read a note: everyone, chosen viewers/roles, or only you."""
    _, exists = read_note(slug)
    if not exists:
        abort(404)

    payload = request.get_json(silent=True) or {}
    visibility = payload.get('visibility')
    if visibility not in VISIBILITIES:
        return {'error': 'Unknown visibility'}, 400

    meta = load_meta()
    viewers = sorted(v for v in payload.get('viewers', []) if v in meta['viewers'])
    roles = sorted(r for r in payload.get('roles', []) if r in all_roles(meta))
    update_note_meta(slug, visibility=visibility, viewers=viewers, roles=roles)
    return {'ok': True, 'visibility': visibility, 'viewers': viewers, 'roles': roles}


@app.route('/cms/delete/<slug>', methods=['POST'])
@require_auth
def delete_note_route(slug):
    """Delete a note."""
    delete_note(slug)
    return redirect(url_for('home'))


@app.route('/cms/upload', methods=['POST'])
@app.route('/cms/upload/<note_slug>', methods=['POST'])
@require_auth
def upload_image(note_slug=None):
    """Upload image for markdown editor."""
    if 'image' not in request.files:
        return {'error': 'No field named image'}, 400
    
    file = request.files['image']
    if file.filename == '':
        return {'error': 'Empty filename'}, 400
        
    from werkzeug.utils import secure_filename
    import uuid
    
    ext = os.path.splitext(file.filename)[1]
    filename = secure_filename(f"{uuid.uuid4().hex}{ext}")
    
    if note_slug:
        safe_slug = slugify(note_slug)
        folder = UPLOAD_DIR / safe_slug
        folder.mkdir(parents=True, exist_ok=True)
        file.save(folder / filename)
        url = url_for('uploaded_file', filename=f"{safe_slug}/{filename}")
    else:
        file.save(UPLOAD_DIR / filename)
        url = url_for('uploaded_file', filename=filename)

    return {'url': url}


def can_view_upload(filename):
    """An image is visible to anyone who may read a note that uses it."""
    if session.get('logged_in'):
        return True
    name = filename.rsplit('/', 1)[-1]
    meta = load_meta()
    for f in NOTES_DIR.glob('*.md'):
        if can_view(f.stem, meta) and name in f.read_text(encoding='utf-8'):
            return True
    return False


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    if not can_view_upload(filename):
        abort(404)
    response = send_from_directory(UPLOAD_DIR, filename)
    # Keep non-public images out of shared caches.
    response.headers['Cache-Control'] = 'private, max-age=300'
    return response


@app.route('/settings')
@require_auth
def settings_page():
    """Settings page."""
    notes = get_notes()
    current = load_settings()
    meta = load_meta()

    # Which shared notes each viewer can currently open, for the overview.
    viewers = []
    for viewer_id, data in sorted(meta['viewers'].items(), key=lambda kv: kv[1].get('name', '').lower()):
        viewer = {
            'id': viewer_id,
            'name': data.get('name', viewer_id),
            'roles': data.get('roles', []),
            'expires': data.get('expires', ''),
            'expired': viewer_expired(data),
            'last_seen': data.get('last_seen'),
            # Most recently opened notes first.
            'recent': sorted(
                ((slug, seen) for slug, seen in data.get('activity', {}).items()
                 if (NOTES_DIR / f'{slug}.md').exists()),
                key=lambda item: item[1],
                reverse=True,
            )[:5],
        }
        viewer['notes'] = []
        for f in sorted(NOTES_DIR.glob('*.md')):
            info = note_meta(f.stem, meta)
            if info['visibility'] == 'public' or (
                info['visibility'] == 'shared' and viewer_allowed(viewer, info)
            ):
                viewer['notes'].append({'slug': f.stem, 'visibility': info['visibility']})
        viewers.append(viewer)

    return render_template(
        'settings.html',
        notes=notes,
        current=current,
        viewers=viewers,
        new_key=session.pop('new_key', None),
    )


def issue_key(meta, viewer_id):
    """Give a viewer a fresh key and remember it for one page view."""
    key = generate_key()
    meta['viewers'][viewer_id]['key_hash'] = hash_key(key)
    save_meta(meta)
    session['new_key'] = {
        'name': meta['viewers'][viewer_id]['name'],
        'key': key,
        'link': url_for('key_login', key=key, _external=True),
    }


@app.route('/settings/viewers/add', methods=['POST'])
@require_auth
def add_viewer():
    """Create a viewer and show their access key once."""
    name = request.form.get('name', '').strip()
    base_id = slugify(name)
    if not base_id:
        return redirect(url_for('settings_page') + '#viewers')

    meta = load_meta()
    viewer_id, n = base_id, 2
    while viewer_id in meta['viewers']:
        viewer_id, n = f'{base_id}-{n}', n + 1

    meta['viewers'][viewer_id] = {'name': name, 'roles': parse_roles(request.form.get('roles', ''))}
    expires = parse_expiry(request.form.get('expires', ''))
    if expires:
        meta['viewers'][viewer_id]['expires'] = expires
    issue_key(meta, viewer_id)
    return redirect(url_for('settings_page') + '#viewers')


@app.route('/settings/viewers/<viewer_id>', methods=['POST'])
@require_auth
def update_viewer(viewer_id):
    """Change a viewer's roles and how long their key works."""
    meta = load_meta()
    viewer = meta['viewers'].get(viewer_id)
    if viewer is not None:
        viewer['roles'] = parse_roles(request.form.get('roles', ''))
        expires = parse_expiry(request.form.get('expires', ''))
        if expires:
            viewer['expires'] = expires
        else:
            viewer.pop('expires', None)
        save_meta(meta)
    return redirect(url_for('settings_page') + '#viewers')


@app.route('/settings/viewers/<viewer_id>/regenerate', methods=['POST'])
@require_auth
def regenerate_viewer_key(viewer_id):
    """Replace a viewer's key; the old key and link stop working."""
    meta = load_meta()
    if viewer_id in meta['viewers']:
        issue_key(meta, viewer_id)
    return redirect(url_for('settings_page') + '#viewers')


@app.route('/settings/viewers/<viewer_id>/delete', methods=['POST'])
@require_auth
def delete_viewer(viewer_id):
    """Remove a viewer and take them off every note they were shared on."""
    meta = load_meta()
    if meta['viewers'].pop(viewer_id, None) is not None:
        for entry in meta['notes'].values():
            if viewer_id in entry.get('viewers', []):
                entry['viewers'].remove(viewer_id)
        save_meta(meta)
    return redirect(url_for('settings_page') + '#viewers')


@app.route('/settings/save', methods=['POST'])
@require_auth
def save_settings_route():
    """Save settings."""
    settings = {
        'site_title': request.form.get('site_title', 'Notewall'),
        'theme': request.form.get('theme', 'dark'),
        'accent_color': request.form.get('accent_color', '#6366f1'),
        'editor_height': int(request.form.get('editor_height', 500)),
        'font_size': int(request.form.get('font_size', 16)),
        'editor_font_size': int(request.form.get('editor_font_size', 15)),
        'show_line_numbers': request.form.get('show_line_numbers') == 'on',
        'show_subtopics': request.form.get('show_subtopics') == 'on',
        'auto_save': request.form.get('auto_save') == 'on',
    }
    save_settings(settings)
    return redirect(url_for('settings_page'))


@app.errorhandler(404)
def not_found(e):
    """404 page."""
    return render_template('404.html', notes=get_notes()), 404


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"đź“ť Notewall running at http://localhost:{port}")
    print(f"đź“ Notes directory: {NOTES_DIR}")
    app.run(debug=True, host='0.0.0.0', port=port)




