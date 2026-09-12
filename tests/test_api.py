"""HTTP-level tests: the contract the browser actually talks to."""

from __future__ import annotations

import base64

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as app_module
from sample_pdf import build_sample

client = TestClient(app_module.app)


def make_pdf(line: str = "Editable sentence") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 120), line, fontsize=13)
    return doc.tobytes()


def text_of(pdf_bytes: bytes) -> str:
    # Spaces come back non-breaking when text is drawn in an embedded font.
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc[0].get_text().replace("\xa0", " ")
    finally:
        doc.close()


def span_rect(pdf_bytes: bytes, page_index: int = 0):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for block in doc[page_index].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span["text"].strip():
                        return list(span["bbox"])
    finally:
        doc.close()
    raise AssertionError("no text in fixture")


def upload(pdf_bytes: bytes | None = None, name: str = "doc.pdf"):
    raw = pdf_bytes or make_pdf()
    res = client.post("/api/documents", files={"file": (name, raw, "application/pdf")})
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------
# static + health
# --------------------------------------------------------------------------

def test_index_is_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "PDF" in res.text


def test_health_reports_pymupdf():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["pymupdf"]


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------

def test_upload_returns_page_geometry():
    meta = upload()
    assert meta["page_count"] == 1
    assert meta["pages"][0]["width"] == 400
    assert meta["pages"][0]["height"] == 300
    assert meta["id"]


def test_upload_rejects_garbage():
    res = client.post("/api/documents", files={"file": ("x.pdf", b"hello world", "application/pdf")})
    assert res.status_code == 400


def test_upload_rejects_empty_file():
    res = client.post("/api/documents", files={"file": ("x.pdf", b"", "application/pdf")})
    assert res.status_code == 400


def test_upload_rejects_unparseable_pdf_header():
    res = client.post("/api/documents", files={"file": ("x.pdf", b"%PDF-1.7 junk", "application/pdf")})
    assert res.status_code == 400


def test_upload_sanitises_the_filename():
    meta = upload(name="../../etc/weird name!.pdf")
    assert "/" not in meta["name"] and "\\" not in meta["name"]
    assert "!" not in meta["name"]


def test_original_file_is_downloadable_unchanged():
    raw = make_pdf("Original text")
    meta = upload(raw, "original.pdf")
    res = client.get(f"/api/documents/{meta['id']}/file")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert text_of(res.content) == text_of(raw)


def test_missing_document_is_a_404():
    assert client.get("/api/documents/nope/file").status_code == 404
    assert client.post("/api/documents/nope/resolve",
                       json={"page": 0, "rect": [0, 0, 10, 10]}).status_code == 404
    assert client.post("/api/documents/nope/export", json={"edits": []}).status_code == 404


# --------------------------------------------------------------------------
# sample
# --------------------------------------------------------------------------

def test_sample_document_is_generated():
    res = client.get("/api/sample")
    assert res.status_code == 200
    meta = res.json()
    assert meta["page_count"] == 3
    # page 3 is rotated, so its display geometry differs from its page geometry
    assert meta["pages"][2]["rotation"] == 90
    assert meta["pages"][2]["width"] == 595 and meta["pages"][2]["display_width"] == 842
    raw = client.get(f"/api/documents/{meta['id']}/file").content
    assert "Quarterly Operations Report" in text_of(raw)


# --------------------------------------------------------------------------
# resolve: the frontend/backend agreement check
# --------------------------------------------------------------------------

def test_resolve_finds_the_text_under_a_rect():
    raw = make_pdf("Resolve me please")
    meta = upload(raw)
    rect = span_rect(raw)
    res = client.post(f"/api/documents/{meta['id']}/resolve", json={"page": 0, "rect": rect})
    assert res.status_code == 200
    body = res.json()
    assert body["found"] is True
    assert body["text"] == "Resolve me please"
    assert body["defaults"]["font_label"] == "Helvetica"
    assert body["defaults"]["size"] == pytest.approx(13, abs=0.2)
    assert len(body["defaults"]["baseline"]) == 2
    # The client previews with these, so they have to be stated outright.
    assert body["defaults"]["bold"] is False
    assert body["defaults"]["italic"] is False


def test_resolve_reports_the_weight_of_a_bold_run():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 120), "Bold sentence", fontsize=13, fontname="hebo")
    raw = doc.tobytes()
    meta = upload(raw)
    body = client.post(f"/api/documents/{meta['id']}/resolve",
                       json={"page": 0, "rect": span_rect(raw)}).json()
    assert body["defaults"]["bold"] is True
    assert body["defaults"]["italic"] is False


def test_export_report_states_the_weight_it_used():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 120), "Bold sentence", fontsize=13, fontname="hebo")
    raw = doc.tobytes()
    meta = upload(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": span_rect(raw), "text": "Bolder sentence"},
    ]})
    row = res.json()["report"][0]
    assert row["bold"] is True
    assert row["italic"] is False
    assert "Bold" in row["font"]


def test_resolve_reports_nothing_on_blank_space():
    meta = upload(make_pdf())
    res = client.post(f"/api/documents/{meta['id']}/resolve",
                      json={"page": 0, "rect": [40, 250, 300, 270]})
    assert res.json()["found"] is False


def test_resolve_validates_its_input():
    meta = upload()
    assert client.post(f"/api/documents/{meta['id']}/resolve",
                       json={"page": 0, "rect": [1, 2, 3]}).status_code == 400
    assert client.post(f"/api/documents/{meta['id']}/resolve",
                       json={"page": 7, "rect": [1, 2, 3, 4]}).status_code == 400


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def test_export_applies_an_edit_and_returns_a_pdf():
    raw = make_pdf("Replace this line")
    meta = upload(raw)
    rect = span_rect(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": rect, "text": "Replaced line"},
    ]})
    assert res.status_code == 200
    body = res.json()
    pdf = base64.b64decode(body["pdf"])
    after = text_of(pdf)
    assert "Replaced line" in after
    assert "Replace this line" not in after
    assert body["summary"] == {"applied": 1, "total": 1, "failed": 0, "unverified": 0,
                                "annotations": 0}
    assert body["filename"].endswith("-edited.pdf")
    assert body["report"][0]["status"] == "applied"


def test_export_keeps_the_original_untouched_for_a_second_export():
    raw = make_pdf("Faithful original")
    meta = upload(raw)
    rect = span_rect(raw)
    payload = {"edits": [{"page": 0, "rect": rect, "text": "First pass"}]}
    first = base64.b64decode(client.post(f"/api/documents/{meta['id']}/export", json=payload).json()["pdf"])
    second = client.post(f"/api/documents/{meta['id']}/export", json=payload)
    # exporting twice from the same upload must not stack changes
    assert text_of(first) == text_of(base64.b64decode(second.json()["pdf"]))
    assert client.get(f"/api/documents/{meta['id']}/file").content == raw


def test_export_reports_edits_that_found_nothing():
    meta = upload(make_pdf())
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": [40, 250, 300, 270], "text": "ghost"},
    ]})
    body = res.json()
    assert body["report"][0]["status"] == "no_text_found"
    assert body["summary"]["unverified"] == 1


def test_export_reports_bad_edits_without_failing_the_whole_request():
    raw = make_pdf()
    meta = upload(raw)
    rect = span_rect(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": rect, "text": "good"},
        {"page": 9, "rect": rect, "text": "bad page"},
        {"page": 0, "rect": [10, 10, 5, 5], "text": "bad rect"},
    ]})
    assert res.status_code == 200
    body = res.json()
    assert body["report"][0]["status"] == "applied"
    assert body["report"][1]["status"] == "page_out_of_range"
    assert body["report"][2]["status"] == "invalid"
    assert body["summary"]["failed"] == 2
    assert "good" in text_of(base64.b64decode(body["pdf"]))


def test_export_with_no_edits_returns_the_document():
    raw = make_pdf("Untouched")
    meta = upload(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": []})
    assert res.status_code == 200
    assert "Untouched" in text_of(base64.b64decode(res.json()["pdf"]))


def test_export_accepts_delete_mode():
    raw = make_pdf("Delete me now")
    meta = upload(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": span_rect(raw), "mode": "delete", "text": ""},
    ]})
    assert res.json()["report"][0]["method"] == "delete"
    assert "Delete me now" not in text_of(base64.b64decode(res.json()["pdf"]))


def test_export_uses_a_requested_filename():
    meta = upload()
    res = client.post(f"/api/documents/{meta['id']}/export",
                      json={"edits": [], "filename": "my report.pdf"})
    assert res.json()["filename"] == "my report-edited.pdf"


def test_resolve_reports_nothing_once_the_text_was_removed():
    """After a deletion, the same coordinates must come back empty.

    Regression: the locator used to reach for the nearest run and answer with a
    neighbouring row, so a client re-checking a cleared spot was told text was
    still there.
    """
    raw = make_pdf("Remove this line please")
    meta = upload(raw)
    rect = span_rect(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": rect, "mode": "delete"},
    ]})
    produced = base64.b64decode(res.json()["pdf"])
    assert "Remove this line please" not in text_of(produced)

    meta2 = client.post("/api/documents", files={"file": ("r.pdf", produced, "application/pdf")}).json()
    again = client.post(f"/api/documents/{meta2['id']}/resolve", json={"page": 0, "rect": rect}).json()
    assert again["found"] is False


def test_export_matches_the_sample_document_geometry():
    """The real thing: edit the generated sample the way the UI would."""
    meta = client.get("/api/sample").json()
    raw = client.get(f"/api/documents/{meta['id']}/file").content
    doc = pymupdf.open(stream=raw, filetype="pdf")
    target = None
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "Headline metric" in span["text"]:
                    target = list(span["bbox"])
    doc.close()
    assert target, "sample text not found"

    res = client.post(f"/api/documents/{meta['id']}/export", json={"edits": [
        {"page": 0, "rect": target, "text": "Renamed metric"},
    ]})
    body = res.json()
    assert body["report"][0]["status"] == "applied"
    after = text_of(base64.b64decode(body["pdf"]))
    assert "Renamed metric" in after
    assert "Headline metric" not in after
    # the coloured panel it sits on must survive
    final = pymupdf.open(stream=base64.b64decode(body["pdf"]), filetype="pdf")
    pix = final[0].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
    assert pix.pixel(500, 300) == (237, 242, 252)


# --------------------------------------------------------------------------
# markups (the toolbar's pen, arrows, shapes, highlights, text boxes, images)
# --------------------------------------------------------------------------

def png_payload() -> str:
    doc = pymupdf.open()
    page = doc.new_page(width=40, height=40)
    page.draw_rect(pymupdf.Rect(0, 0, 40, 40), color=None, fill=(0.2, 0.7, 0.35), width=0)
    data = page.get_pixmap(dpi=36).tobytes("png")
    doc.close()
    return base64.b64encode(data).decode("ascii")


def test_export_draws_markups_and_reports_them():
    meta = upload()
    res = client.post(f"/api/documents/{meta['id']}/export", json={
        "edits": [],
        "annotations": [
            {"kind": "highlight", "page": 0, "rect": [40.0, 100.0, 200.0, 120.0],
             "color": [1, 1, 0]},
            {"kind": "arrow", "page": 0, "points": [[40.0, 200.0], [200.0, 200.0]],
             "color": [0.8, 0.1, 0.1], "width": 2},
            {"kind": "text", "page": 0, "rect": [40.0, 240.0, 260.0, 258.0],
             "text": "Added on the server", "size": 13, "font": "hebo",
             "color": [0.1, 0.3, 0.8]},
            {"kind": "image", "page": 0, "rect": [280.0, 100.0, 360.0, 180.0],
             "image": {"data": png_payload(), "mime": "image/png"}},
        ],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["summary"]["annotations"] == 4
    assert body["summary"]["total"] == 4
    assert [row["status"] for row in body["report"]] == ["applied"] * 4
    assert [row["label"] for row in body["report"]] == [
        "Highlight", "Arrow", "Text box", "Image"]

    pdf = base64.b64decode(body["pdf"])
    assert "Added on the server" in text_of(pdf)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        assert len(doc[0].get_images(full=True)) == 1
    finally:
        doc.close()


def test_markups_and_text_edits_share_one_export():
    raw = make_pdf("Replace this line")
    meta = upload(raw)
    res = client.post(f"/api/documents/{meta['id']}/export", json={
        "edits": [{"page": 0, "rect": span_rect(raw), "text": "Replaced line"}],
        "annotations": [{"kind": "rect", "page": 0, "rect": [40.0, 200.0, 200.0, 260.0],
                         "color": [0.1, 0.3, 0.8], "width": 2}],
    })
    body = res.json()
    assert [row["index"] for row in body["report"]] == [0, 1]
    assert "kind" not in body["report"][0]  # text edits are not markups
    assert body["report"][1]["kind"] == "annotation"
    after = text_of(base64.b64decode(body["pdf"]))
    assert "Replaced line" in after and "Replace this line" not in after


def test_an_unknown_markup_kind_fails_only_that_row():
    meta = upload()
    res = client.post(f"/api/documents/{meta['id']}/export", json={
        "annotations": [
            {"kind": "sparkle", "page": 0, "rect": [10.0, 10.0, 60.0, 60.0]},
            {"kind": "highlight", "page": 0, "rect": [10.0, 10.0, 60.0, 60.0]},
        ],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["report"][0]["status"] == "invalid"
    assert body["report"][1]["status"] == "applied"
    # An unusable markup is a failure the user should see counted, not a warning.
    assert body["summary"] == {"applied": 1, "total": 2, "failed": 1,
                               "unverified": 0, "annotations": 2}


def test_an_oversized_image_is_refused_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_MARKUP_IMAGE_BYTES", 64)
    meta = upload()
    res = client.post(f"/api/documents/{meta['id']}/export", json={
        "annotations": [{"kind": "image", "page": 0, "rect": [10.0, 10.0, 60.0, 60.0],
                         "image": {"data": png_payload()}}],
    })
    assert res.status_code == 413
    assert "larger than" in res.json()["detail"]


def test_too_many_markups_is_refused(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_MARKUPS_PER_EXPORT", 2)
    meta = upload()
    res = client.post(f"/api/documents/{meta['id']}/export", json={"annotations": [
        {"kind": "ink", "page": 0, "points": [[10.0, 10.0], [20.0, 20.0]]} for _ in range(3)
    ]})
    assert res.status_code == 413
    assert "too many markups" in res.json()["detail"]
