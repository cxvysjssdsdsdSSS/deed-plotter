# Deed Plotter

Desktop app that turns deeds and legal descriptions into plotted metes-and-bounds boundaries.

## Sharing this project

The source is meant to be shared. There is no credits page inside the app
for that — this file is the place.

PyQt6 is used here under a **paid commercial license**. That purchase is
only for the author. It is not in this folder, and it does not transfer to
anyone else.

Other people who run the code from source need their own PyQt6: either
Riverbank’s free GPL terms, or a commercial license they buy themselves.

Do not publish a Cursor API key, `~/.deed_plotter` (jobs, history, page/parse
cache), a PyQt license file / key, `tests/`, `Samples/`, leftover
DXF/PDF/image deeds, or the nested `Health/` folder.

## Features

- Load multipage PDFs and images (PNG, JPG, TIFF, etc.) with a page viewer
- AI-powered parsing of metes-and-bounds calls (bearings, distances, curves, archaic units like chains/rods/varas)
- Editable call table — fix or add calls by hand, plot updates live
- Automatic boundary plot (pyqtgraph) with corner markers, POB label, and misclosure line
- Closure report: perimeter, area (sq ft / acres), misclosure distance & bearing, precision ratio
- CSV export of the call table

## Setup

```powershell
pip install -r requirements.txt
python main.py
```

## Configuration

Open **Settings…** in the toolbar and enter:

- **Cursor API key** — from cursor.com/dashboard → Integrations (parsing runs through your Cursor agent usage)
- **Model** — defaults to `composer-2.5`

## Usage

1. **Open Deed…** and pick a PDF or image (or paste the description text in the box).
2. Click **Parse with AI** — a Cursor agent reads all pages and extracts the calls in order.
3. Review the **Call Table** (low-confidence calls are highlighted), edit as needed.
4. The **Plot** tab shows the boundary; the status line shows closure/area.
5. **Export CSV…** to take the calls into your CAD workflow.
