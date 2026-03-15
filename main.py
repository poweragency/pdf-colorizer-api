import os
import fitz
import base64
import json
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class Zone(BaseModel):
    id: str
    label: str
    x0: float
    y0: float
    x1: float
    y1: float
    color: Optional[str] = None
    opacity: Optional[float] = 0.65

class ColorizeRequest(BaseModel):
    pdf_base64: str
    zones: List[Zone]

def extract_zones(page):
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
    all_x = sorted(v_lines)
    all_y = sorted(h_lines)
    if len(all_x) < 2 or len(all_y) < 2:
        return []
    skip = ['email','www','cliente','arch','prospetto','data','binovamilano','mirko','buonocore','@']
    text_items = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if not text or not any(c.isalpha() for c in text) or len(text) < 3:
                    continue
                if any(kw in text.lower() for kw in skip):
                    continue
                cx = (span["bbox"][0] + span["bbox"][2]) / 2
                cy = (span["bbox"][1] + span["bbox"][3]) / 2
                text_items.append({"text": text, "cx": cx, "cy": cy})
    cells = {}
    for item in text_items:
        col = next(((all_x[i], all_x[i+1]) for i in range(len(all_x)-1) if all_x[i] <= item["cx"] <= all_x[i+1]), None)
        row = next(((all_y[i], all_y[i+1]) for i in range(len(all_y)-1) if all_y[i] <= item["cy"] <= all_y[i+1]), None)
        if col and row:
            key = (col[0], row[0], col[1], row[1])
            cells.setdefault(key, []).append(item["text"])
    zones = []
    seen = {}
    for (x0, y0, x1, y1), texts in cells.items():
        label = " ".join(dict.fromkeys(texts))
        label = " ".join(label.split())
        base_id = "".join(c if c.isalnum() else "_" for c in label.lower())[:40]
        if base_id in seen:
            seen[base_id] += 1
            zid = f"{base_id}_{seen[base_id]}"
        else:
            seen[base_id] = 0
            zid = base_id
        zones.append(Zone(id=zid, label=label, x0=round(x0,1), y0=round(y0,1), x1=round(x1,1), y1=round(y1,1)))
    return zones

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/detect")
async def detect(file: UploadFile = File(...)):
    try:
        pdf_bytes = await file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        zones = extract_zones(page)
        return {"zones": [z.dict() for z in zones], "page_width": round(page.rect.width,1), "page_height": round(page.rect.height,1)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/colorize")
async def colorize(req: ColorizeRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        applied = 0
        for zone in req.zones:
            if not zone.color:
                continue
            h = zone.color.lstrip('#')
            r, g, b = int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255
            page.draw_rect(fitz.Rect(zone.x0, zone.y0, zone.x1, zone.y1), color=None, fill=(r,g,b), fill_opacity=zone.opacity or 0.65, overlay=True)
            applied += 1
        return {"pdf_base64": base64.b64encode(doc.tobytes()).decode(), "zones_applied": applied}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
