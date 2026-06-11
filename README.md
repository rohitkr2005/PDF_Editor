# FreePDF Editor

A 100% free, open-source PDF editor web app.

## Features
- **Edit PDF Text** — click any text, edit it with the same font/size/colour preserved
- Merge PDFs
- Split PDF (by page range → ZIP)
- Rotate pages
- Compress PDF
- Password protect & unlock
- Add watermark
- Extract text
- PDF metadata info

## Setup & Run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the server
python app.py

# 3. Open in browser
# http://localhost:5000
```

## Deployment (Free)
- **Render.com** — connect GitHub repo, set start command: `python app.py`
- **Railway.app** — same as above
- **PythonAnywhere** — upload files, set WSGI to point to app.py

## Tech Stack
- Backend: Flask (Python)
- PDF editing: PyMuPDF (fitz)
- PDF manipulation: pypdf, pdfplumber, reportlab
- Frontend: Vanilla HTML/CSS/JS (no framework)
