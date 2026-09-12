"""FastAPI backend for the PDF text editor.

The server is deliberately dumb about editing state. It keeps the *pristine*
uploaded bytes and re-applies the whole edit list on every export, which makes
exports idempotent: the client can undo, redo, change its mind and export again
without the server ever accumulating drift.
"""

from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

import font_matching
import pdf_ops
from pdf_ops import Annotation, Edit, InvalidDocument
from sample_pdf import build_sample

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_STORED_DOCS = 24          # oldest uploads are evicted beyond this
MAX_EDITS_PER_EXPORT = 2000
MAX_MARKUPS_PER_EXPORT = 2000
MAX_MARKUP_IMAGE_BYTES = 12 * 1024 * 1024
MAX_MARKUP_IMAGE_TOTAL = 48 * 1024 * 1024

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Indexing the installed fonts takes about a second. Do it in the background
    # at startup so the first export that needs a font match is not the one that
    # pays for the scan.
    font_matching.warm_cache()
    yield


app = FastAPI(title="PDFLab", version="1.0.0", docs_url="/api/docs",
              lifespan=lifespan)


class StoredDocument:
    """An upload held in memory, exactly as it arrived."""

    __slots__ = ("id", "name", "raw", "pages", "created")

    def __init__(self, doc_id: str, name: str, raw: bytes, pages: list[dict[str, Any]]) -> None:
        self.id = doc_id
        self.name = name
        self.raw = raw
        self.pages = pages
        self.created = time.time()

    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": len(self.raw),
            "page_count": len(self.pages),
            "pages": self.pages,
        }


_DOCS: dict[str, StoredDocument] = {}
_LOCK = threading.Lock()


class EditModel(BaseModel):
    page: int = Field(..., description="zero-based page index")
    rect: list[float] = Field(..., description="[x0, y0, x1, y1] in PDF points, unrotated page space, top-left origin")
    text: str = ""
    mode: str = "replace"
    font: str | None = None
    size: float | None = None
    color: list[float] | None = None
    align: int = 0
    autofit: bool = False
    pad: float = 0.0


class MarkupImage(BaseModel):
    """An inserted image, base64 encoded so the whole export stays one JSON body."""

    data: str = Field(..., description="base64 image bytes (PNG or JPEG)")
    mime: str | None = None


class MarkupModel(BaseModel):
    """One drawn markup. Geometry is in the same space as `EditModel.rect`."""

    kind: str = Field(..., description="text | image | ink | line | arrow | rect | ellipse | highlight")
    page: int = Field(..., description="zero-based page index")
    rect: list[float] | None = Field(None, description="[x0, y0, x1, y1] for box markups")
    points: list[list[float]] = Field(default_factory=list, description="[[x, y], ...] for ink/line/arrow")
    text: str = ""
    color: list[float] | None = Field(None, description="[r, g, b] in 0..1")
    width: float | None = Field(None, description="stroke width in PDF points")
    opacity: float | None = None
    fill_opacity: float | None = None
    size: float | None = Field(None, description="font size in points, for text markups")
    font: str | None = Field(None, description="base-14 code (helv, hebo, tiro, cour, ...); null matches the document")
    align: int = 0
    bold: bool = False
    italic: bool = False
    image: MarkupImage | None = None


class ResolveRequest(BaseModel):
    page: int
    rect: list[float]


class ExportRequest(BaseModel):
    edits: list[EditModel] = []
    annotations: list[MarkupModel] = []
    filename: str | None = None


def _store(name: str, raw: bytes) -> StoredDocument:
    doc = pdf_ops.open_document(raw)          # validates, raises InvalidDocument
    try:
        pages = pdf_ops.page_sizes(doc)
    finally:
        doc.close()
    safe = _safe_name(name)
    record = StoredDocument(uuid.uuid4().hex, safe, raw, pages)
    with _LOCK:
        _DOCS[record.id] = record
        while len(_DOCS) > MAX_STORED_DOCS:
            oldest = min(_DOCS.values(), key=lambda d: d.created)
            _DOCS.pop(oldest.id, None)
    return record


def _safe_name(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "document.pdf").strip())
    cleaned = cleaned.strip("._ ") or "document.pdf"
    return cleaned[:120]


def _get(doc_id: str) -> StoredDocument:
    with _LOCK:
        record = _DOCS.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail="document not found or expired; upload it again")
    return record


def _edits_from(payload: list[EditModel]) -> list[Edit]:
    if len(payload) > MAX_EDITS_PER_EXPORT:
        raise HTTPException(status_code=413, detail=f"too many edits (limit {MAX_EDITS_PER_EXPORT})")
    return [Edit.from_dict(item.model_dump(), index=i) for i, item in enumerate(payload)]


def _markups_from(payload: list[MarkupModel], offset: int) -> list[Annotation]:
    """Validate markups, keeping a per-markup `problem` rather than rejecting the lot.

    Report indices continue after the text edits so the client can map every row
    back to the object it sent.
    """
    if len(payload) > MAX_MARKUPS_PER_EXPORT:
        raise HTTPException(status_code=413,
                            detail=f"too many markups (limit {MAX_MARKUPS_PER_EXPORT})")
    raw = [item.model_dump() for item in payload]
    total_bytes = sum(
        len(item.image.data) for item in payload if item.image and item.image.data
    )
    if total_bytes > MAX_MARKUP_IMAGE_TOTAL:
        raise HTTPException(status_code=413,
                            detail="the attached images are larger than "
                                   f"{MAX_MARKUP_IMAGE_TOTAL // (1024 * 1024)} MB in total")
    for item in payload:
        if item.image and len(item.image.data) > MAX_MARKUP_IMAGE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"an image is larger than "
                                       f"{MAX_MARKUP_IMAGE_BYTES // (1024 * 1024)} MB")
    return [Annotation.from_dict(item, index=offset + i) for i, item in enumerate(raw)]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="index.html is missing next to app.py")
    return FileResponse(INDEX_HTML, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "documents": len(_DOCS), "pymupdf": pymupdf.__version__}


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="the uploaded file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not raw.lstrip()[:5].startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="this does not look like a PDF file")
    try:
        record = _store(file.filename or "document.pdf", raw)
    except InvalidDocument as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(record.meta())


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str) -> Response:
    """The untouched original, streamed to pdf.js."""
    record = _get(doc_id)
    return Response(record.raw, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{record.name}"',
                             "Cache-Control": "no-store"})


@app.get("/api/sample")
def sample() -> JSONResponse:
    """Upload a generated demo document in one call."""
    record = _store("sample-report.pdf", build_sample())
    return JSONResponse(record.meta())


@app.post("/api/documents/{doc_id}/resolve")
def resolve(doc_id: str, payload: ResolveRequest) -> dict[str, Any]:
    """What does the *backend* see under these coordinates?

    The browser calls this the moment a run of text is clicked, so the two
    engines can be compared before anything is exported. If they ever disagree
    the mismatch shows up here instead of silently corrupting an export.
    """
    record = _get(doc_id)
    doc = pdf_ops.open_document(record.raw)
    try:
        if not 0 <= payload.page < doc.page_count:
            raise HTTPException(status_code=400, detail="page index out of range")
        if len(payload.rect) != 4:
            raise HTTPException(status_code=400, detail="rect must have four numbers")
        page = doc[payload.page]
        spans = pdf_ops.collect_spans(page, payload.page)
        found = pdf_ops.find_text(spans, pymupdf.Rect(*payload.rect))
        found["page"] = payload.page
        found["spans_on_page"] = len(spans)
        return found
    finally:
        doc.close()


@app.post("/api/documents/{doc_id}/export")
def export(doc_id: str, payload: ExportRequest) -> JSONResponse:
    """Apply every edit to the original file and hand back the finished PDF."""
    record = _get(doc_id)
    edits = _edits_from(payload.edits)
    markups = _markups_from(payload.annotations, offset=len(payload.edits))
    try:
        out, report = pdf_ops.apply_edits(record.raw, edits, markups)
    except InvalidDocument as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = _safe_name(payload.filename or record.name)
    download = base if base.lower().endswith(".pdf") else f"{base}.pdf"
    download = f"{download[:-4]}-edited.pdf"

    failed = [r for r in report if r["status"] in ("insert_failed", "invalid", "page_out_of_range")]
    unverified = [r for r in report if r["status"] in ("no_text_found", "written_unverified")]
    return JSONResponse({
        "filename": download,
        "size": len(out),
        "pdf": base64.b64encode(out).decode("ascii"),
        "report": report,
        "summary": {
            "applied": len([r for r in report if r["status"] in ("applied", "applied_approx", "deleted")]),
            "total": len(report),
            "failed": len(failed),
            "unverified": len(unverified),
            "annotations": len(markups),
        },
    })
