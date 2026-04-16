# ??? Notewall

A lightweight, file-based Markdown notes app with a simple CMS. Built with Python/Flask.

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ? Features

- **File-based storage** — Notes are plain .md files, no database needed
- **Markdown rendering** — Full support with syntax highlighting, automatic TOC, and subtopic toggle
- **Upload Images** — Image upload system integrated directly into the editor
- **Simple CMS** — Create, edit, and delete notes from the browser
- **Source Copy feature** — Preview markdown source code without entering edit mode
- **Dark/Light themes** — Easy on the eyes (not lightmode, tfu, I struggle not to delete it)
- **Customizable** — Accent color, font sizes, editor height
- **Lightweight** — Just Python + Flask, minimal dependencies
- **Portable** — Run anywhere with Python installed

## ?? Screenshots

*(Screenshots coming soon...)*

## ?? Quick Start

`ash
# Just clone the repo
git clone https://github.com/yourusername/notewall.git
cd notewall

# Create virtual environment (optional)
python -m venv .venv
source .venv/Scripts/activate   # Or on Linux: .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env file and write inside:
NOTEWALL_PASSWORD=yourpassword
SECRET_KEY=somerandomstring
PORT=5000

# just run the file and server starts
python app.py
`

Opens on http://localhost:5000 (or whichever port you configured).
Images uploaded to notes are saved within the static/uploads directory.

#### ! - Beware, it might differ based on your OS. - !

## ?? Settings

Click the ?? gear icon in the sidebar to customize. Settings are saved to settings.json.

## ?? License

MIT License — feel free to use this however you want, I own this as much as you do.
