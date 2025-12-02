import os
import re
import json
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, abort

import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.tables import TableExtension
from markdown.extensions.toc import TocExtension

app = Flask(__name__)

# Notes directory (notes/ folder inside Notewall, or parent folder if notes/ doesn't exist)
NOTEWALL_DIR = Path(__file__).parent
NOTES_DIR = NOTEWALL_DIR / 'notes'
if not NOTES_DIR.exists():
    NOTES_DIR = NOTEWALL_DIR.parent  # Fallback to parent for existing setups

SETTINGS_FILE = NOTEWALL_DIR / 'settings.json'

# Default settings
DEFAULT_SETTINGS = {
    'site_title': 'Notewall',
    'theme': 'dark',
    'accent_color': '#6366f1',
    'editor_height': 500,
    'font_size': 16,
    'editor_font_size': 15,
    'show_line_numbers': False,
    'auto_save': False
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
    return {'settings': load_settings()}

# Markdown processor
md = markdown.Markdown(extensions=[
    'fenced_code',
    'tables',
    'toc',
    CodeHiliteExtension(css_class='highlight', guess_lang=False),
])


def get_notes():
    """Get all markdown files from notes directory."""
    notes = []
    for f in sorted(NOTES_DIR.glob('*.md')):
        slug = f.stem
        name = slug.replace('-', ' ').replace('_', ' ').title()
        notes.append({'slug': slug, 'name': name, 'filename': f.name})
    return notes


def read_note(slug):
    """Read a markdown file, return (content, exists)."""
    filepath = NOTES_DIR / f"{slug}.md"
    if filepath.exists():
        return filepath.read_text(encoding='utf-8'), True
    return '', False


def save_note(slug, content):
    """Save content to a markdown file."""
    filepath = NOTES_DIR / f"{slug}.md"
    filepath.write_text(content, encoding='utf-8')


def delete_note(slug):
    """Delete a markdown file."""
    filepath = NOTES_DIR / f"{slug}.md"
    if filepath.exists():
        filepath.unlink()


def render_markdown(text):
    """Convert markdown to HTML."""
    md.reset()
    return md.convert(text)


def slugify(text):
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text


# Routes

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
    
    html = render_markdown(content)
    title = slug.replace('-', ' ').replace('_', ' ').title()
    notes = get_notes()
    return render_template('note.html', title=title, content=html, slug=slug, notes=notes)


@app.route('/cms/new')
def new_note():
    """Form to create a new note."""
    notes = get_notes()
    return render_template('editor.html', title='New Note', slug='', content='', is_new=True, notes=notes)


@app.route('/cms/create', methods=['POST'])
def create_note():
    """Create a new note."""
    slug = slugify(request.form.get('slug', ''))
    content = request.form.get('content', '')
    
    if not slug:
        return redirect(url_for('new_note'))
    
    save_note(slug, content)
    return redirect(url_for('view_note', slug=slug))


@app.route('/cms/edit/<slug>')
def edit_note(slug):
    """Form to edit an existing note."""
    content, exists = read_note(slug)
    title = slug.replace('-', ' ').replace('_', ' ').title()
    notes = get_notes()
    return render_template('editor.html', title=f'Edit: {title}', slug=slug, content=content, is_new=not exists, notes=notes)


@app.route('/cms/save/<slug>', methods=['POST'])
def save_note_route(slug):
    """Save an edited note."""
    content = request.form.get('content', '')
    save_note(slug, content)
    return redirect(url_for('view_note', slug=slug))


@app.route('/cms/delete/<slug>', methods=['POST'])
def delete_note_route(slug):
    """Delete a note."""
    delete_note(slug)
    return redirect(url_for('home'))


@app.route('/settings')
def settings_page():
    """Settings page."""
    notes = get_notes()
    current = load_settings()
    return render_template('settings.html', notes=notes, current=current)


@app.route('/settings/save', methods=['POST'])
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
        'auto_save': request.form.get('auto_save') == 'on'
    }
    save_settings(settings)
    return redirect(url_for('settings_page'))


@app.errorhandler(404)
def not_found(e):
    """404 page."""
    return render_template('404.html', notes=get_notes()), 404


if __name__ == '__main__':
    print(f"📝 Notewall running at http://localhost:5000")
    print(f"📁 Notes directory: {NOTES_DIR}")
    app.run(debug=True, host='0.0.0.0', port=5000)
