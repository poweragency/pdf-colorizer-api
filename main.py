from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import fitz  # PyMuPDF
import base64
import json
from typing import List, Optional

app = FastAPI(title="PDF Colorizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MODELLI ───────────────────────────────────────────────
class Zone(BaseModel):
    id: str
    label: str
    x0: float
    y0: float
    x1: float
    y1: float
    color: Optional[str] = None   # hex, es. "#C8A882"
    opacity: Optional[float] = 0.65

class DetectResponse(BaseModel):
    zones: List[Zone]
    page_width: float
    page_height: float

class ColorizeRequest(BaseModel):
    pdf_base64: str
    zones: List[Zone]

class ColorizeResponse(BaseModel):
    pdf_base64: str
    zones_applied: int

# ─── UTILS ─────────────────────────────────────────────────
def extract_zones(page: fitz.Page) -> List[Zone]:
    """
    Rileva automaticamente le zone dal PDF CAD:
    1. Estrae tutte le linee per ricostruire la griglia
    2. Trova i testi (label) all'interno di ogni cella
    3. Ritorna zone con id, label e coordinate
    """
    # Linee orizzontali e verticali
    h_lines = set()
    v_lines = set()

    for p in page.get_drawings():
        for item in p.get("items", []):
            if item[0] == 'l':
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2 and abs(p1.x - p2.x) > 20:
                    h_lines.add(round((p1.y + p2.y) / 2, 1))
                elif abs(p1.x - p2.x) < 2 and abs(p1.y - p2.y) > 20:
                    v_lines.add(round((p1.x + p2.x) / 2, 1))

    # Bounding box principale del disegno
    all_x = sorted(v_lines)
    all_y = sorted(h_lines)

    if len(all_x) < 2 or len(all_y) < 2:
        return []

    # Estrai testi con coordinate (solo label significative)
    skip_kw = ['email', 'www', 'cliente', 'arch', 'prospetto', 'data',
               'binovamilano', 'mirko', 'buonocore', '@', 'http']

    text_items = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if not text or not any(c.isalpha() for c in text) or len(text) < 3:
                    continue
                if any(kw in text.lower() for kw in skip_kw):
                    continue
                cx = (span["bbox"][0] + span["bbox"][2]) / 2
                cy = (span["bbox"][1] + span["bbox"][3]) / 2
                text_items.append({"text": text, "cx": cx, "cy": cy})

    # Mappa ogni testo alla cella della griglia
    cells: dict = {}
    for item in text_items:
        # Trova colonna X
        col = None
        for i in range(len(all_x) - 1):
            if all_x[i] <= item["cx"] <= all_x[i + 1]:
                col = (all_x[i], all_x[i + 1])
                break
        # Trova riga Y
        row = None
        for i in range(len(all_y) - 1):
            if all_y[i] <= item["cy"] <= all_y[i + 1]:
                row = (all_y[i], all_y[i + 1])
                break

        if col and row:
            key = (col[0], row[0], col[1], row[1])
            if key not in cells:
                cells[key] = []
            cells[key].append(item["text"])

    # Crea zone
    zones = []
    seen_labels = {}
    for (x0, y0, x1, y1), texts in cells.items():
        # Pulisce e unisce i testi della cella
        label = " ".join(dict.fromkeys(texts))  # dedup mantendo ordine
        label = " ".join(label.split())  # normalizza spazi

        # ID univoco dalla label
        base_id = label.lower()
        base_id = "".join(c if c.isalnum() else "_" for c in base_id)
        base_id = base_id[:40]

        # Gestisce duplicati
        if base_id in seen_labels:
            seen_labels[base_id] += 1
            zone_id = f"{base_id}_{seen_labels[base_id]}"
        else:
            seen_labels[base_id] = 0
            zone_id = base_id

        zones.append(Zone(
            id=zone_id,
            label=label,
            x0=round(x0, 1),
            y0=round(y0, 1),
            x1=round(x1, 1),
            y1=round(y1, 1),
        ))

    return zones


def hex_to_rgb_float(hex_color: str):
    h = hex_color.lstrip('#')
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


# ─── ENDPOINT 1: RILEVA ZONE ───────────────────────────────
@app.post("/detect", response_model=DetectResponse)
async def detect_zones(file: UploadFile = File(...)):
    """
    Riceve un PDF, rileva automaticamente le zone con i loro nomi.
    """
    try:
        pdf_bytes = await file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        zones = extract_zones(page)
        return DetectResponse(
            zones=zones,
            page_width=round(page.rect.width, 1),
            page_height=round(page.rect.height, 1),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── ENDPOINT 2: COLORA PDF ────────────────────────────────
@app.post("/colorize", response_model=ColorizeResponse)
async def colorize_pdf(req: ColorizeRequest):
    """
    Riceve PDF in base64 + lista zone con colori.
    Ritorna PDF colorato in base64.
    """
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]

        applied = 0
        for zone in req.zones:
            if not zone.color:
                continue
            r, g, b = hex_to_rgb_float(zone.color)
            rect = fitz.Rect(zone.x0, zone.y0, zone.x1, zone.y1)
            page.draw_rect(
                rect,
                color=None,
                fill=(r, g, b),
                fill_opacity=zone.opacity or 0.65,
                overlay=True,
            )
            applied += 1

        output = doc.tobytes()
        return ColorizeResponse(
            pdf_base64=base64.b64encode(output).decode(),
            zones_applied=applied,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── ENDPOINT 3: DETECT + COLORIZE IN UNO ──────────────────
@app.post("/detect-and-colorize")
async def detect_and_colorize(
    file: UploadFile = File(...),
    colors: str = "{}"   # JSON string: {"zone_id": "#hexcolor"}
):
    """
    All-in-one: rileva zone E applica colori in un'unica chiamata.
    """
    try:
        color_map = json.loads(colors)
        pdf_bytes = await file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]

        zones = extract_zones(page)

        for zone in zones:
            if zone.id in color_map:
                r, g, b = hex_to_rgb_float(color_map[zone.id])
                rect = fitz.Rect(zone.x0, zone.y0, zone.x1, zone.y1)
                page.draw_rect(rect, color=None, fill=(r, g, b), fill_opacity=0.65, overlay=True)

        output = doc.tobytes()
        return {
            "zones": [z.dict() for z in zones],
            "pdf_base64": base64.b64encode(output).decode(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
    if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
