"""파서 서비스 실행(parser-service/1)·원본 미리보기(docs/21). 모델·OCR 호출 없음."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from layout_pipeline import pages, preview, service  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402


def _pdf_bytes(sizes=((200, 100), (100, 150))) -> bytes:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    for w, h in sizes:
        pdf.new_page(w, h)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def _request(tmp: Path, **extra) -> dict:
    src = tmp / "in" / "광고 원본.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(_pdf_bytes())
    return {"submission_id": "ad-0001", "files": [{"file_id": "FILE-0001", "path": str(src)}]} | extra


def test_validate_normalizes_request_and_rejects_bad_input(tmp_path):
    req = service.validate(_request(tmp_path, template_request={"template_ids": ["예금성상품-적립식", "예금성상품-적립식"]}))
    assert req["submission_id"] == "ad-0001" and req["template_ids"] == ["예금성상품-적립식"]
    assert req["files"][0]["file_name"] == "광고 원본.pdf" and Path(req["files"][0]["path"]).is_absolute()
    assert service.validate(_request(tmp_path))["template_ids"] == []
    bad = [
        _request(tmp_path) | {"submission_id": "광고"},                                  # ID 형식
        _request(tmp_path) | {"files": []},
        _request(tmp_path) | {"files": [{"file_id": "F1", "path": str(tmp_path / "없음.pdf")}]},
        _request(tmp_path, template_request={"template_ids": ["카드상품-상품명 미노출"]}),   # 분류하지 않는 템플릿
        _request(tmp_path, template_request={"template_ids": "예금성상품-적립식"}),
    ]
    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    bad.append(_request(tmp_path) | {"files": [{"file_id": "F1", "path": str(txt)}]})
    dup = _request(tmp_path)
    bad.append(dup | {"files": dup["files"] * 2})
    for r in bad:
        with pytest.raises(service.RequestError):
            service.validate(r)


def test_manifest_carries_template_request(tmp_path):
    req = service.validate(_request(tmp_path, template_request={"template_ids": ["예금성상품-입출식", "예금성상품-적립식"]}))
    m = service.write_manifest(req, tmp_path / "run.manifest.json")
    sub = pages.load_manifest(m)[0]
    assert sub.submission_id == "ad-0001" and sub.template_ids == ("예금성상품-입출식", "예금성상품-적립식")
    assert pages.template_requests([m]) == {"ad-0001": ["예금성상품-입출식", "예금성상품-적립식"]}
    assert pages.template_requests([service.write_manifest(service.validate(_request(tmp_path)), tmp_path / "b.json")]) == {}
    assert "예금성상품-상품명 미노출" in service.supported_templates() and len(service.supported_templates()) == 12


def _run_dir(tmp: Path, *, status="READY", semantic="completed", read="read", passed=True) -> tuple[Path, dict]:
    out = tmp / "run"
    req = service.validate(_request(tmp))
    doc = {"semantic": {"status": semantic},
           "pages": [{"page_id": "FILE-0001_p01", "page_no": 1, "execution_status": status},
                     {"page_id": "FILE-0001_p02", "page_no": 2, "execution_status": "READY"}],
           "regions": [{"text_status": read}, {"text_status": read}]}
    dump(out / "semantic" / "files" / "FILE-0001" / "result.json", doc)
    dump(out / "semantic" / "files" / "index.json", {"files": [{"file_id": "FILE-0001", "file_name": "광고 원본.pdf", "input_order": 1,
                                                               "result": "files/FILE-0001/result.json"}]})
    for p in (out / "files" / "contract-check.json", out / "semantic" / "files" / "contract-check.json",
              out / "semantic" / "handoff" / "check.json"):
        dump(p, {"passed": passed})
    dump(out / "semantic" / "handoff" / "ad-0001.json", {"contract": "review-input/2"})
    dump(out / "trace" / "timing.json", {"end_to_end_wall_seconds": 12.5})
    return out, req


@pytest.mark.parametrize("kw,status,code,exit_code", [
    ({}, "completed", None, 0),
    ({"semantic": "partial"}, "partial", None, 0),
    ({"status": "OCR_FAILED"}, "failed", "PAGES_NOT_PROCESSED", 3),
    ({"read": "read_failed"}, "failed", "READ_FAILED", 3),
    ({"semantic": "failed"}, "failed", "SEMANTIC_FAILED", 3),
    ({"passed": False}, "failed", "CONTRACT_CHECK_FAILED", 4),
])
def test_assess_status_and_exit_code(tmp_path, kw, status, code, exit_code):
    out, req = _run_dir(tmp_path, **kw)
    doc = service.assess(out, req)
    assert doc["status"] == status and (doc["failure"] or {}).get("code") == code and service.exit_code(doc) == exit_code
    assert doc["contracts"] == {"result": "layout-result/3", "review_input": "review-input/2"}
    assert (doc["review_input"] == "semantic/handoff/ad-0001.json") == (status != "failed")
    assert doc["files"][0]["result"] == "semantic/files/FILE-0001/result.json" and doc["timing"]["end_to_end_wall_seconds"] == 12.5
    assert doc["request"]["template_request"] == {"template_ids": [], "mode": "auto"}


def test_service_cli_reports_input_error_without_running(tmp_path):
    import run_service
    req = tmp_path / "req.json"
    dump(req, _request(tmp_path) | {"submission_id": "광고 한 건"})
    code = run_service.main(["--request", str(req), "--out", str(tmp_path / "out")])
    doc = load(tmp_path / "out" / "service.json")
    assert code == 2 and doc["status"] == "failed" and doc["failure"]["code"] == "INPUT_INVALID" and doc["review_input"] is None


def test_preview_draws_the_same_pixels_as_parsing(tmp_path):
    data = _pdf_bytes()
    src = tmp_path / "a.pdf"
    src.write_bytes(data)
    sub = pages.Submission("s", (pages.InputFile("f1", src),))
    parsed = pages.render_submission(sub, tmp_path / "pages", pdf_dpi=preview.PDF_DPI)
    assert preview.pdf_page_count(data) == 2
    img, total = preview.render_pdf(data, 2)
    assert total == 2 and img[0].size == (parsed[1].width, parsed[1].height) == (round(100 * 200 / 72), round(150 * 200 / 72))
    assert preview.png_bytes(img[0]) == Path(parsed[1].image_path).read_bytes()
    with pytest.raises(IndexError):
        preview.render_pdf(data, 3)
    assert preview.main([str(src), str(tmp_path / "prev")]) == 0
    assert sorted(p.name for p in (tmp_path / "prev").glob("page-*.png")) == ["page-0001.png", "page-0002.png"]
    assert (tmp_path / "prev" / "page-0001.png").read_bytes() == Path(parsed[0].image_path).read_bytes()


def test_review_input_schema_file_is_valid():
    import jsonschema
    schema = json.loads((ROOT / "schemas" / "review-input-2.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "review-input/2"


def test_asset_hashes_do_not_depend_on_line_endings(tmp_path):
    """자산 해시는 줄바꿈을 LF 로 맞춰 계산한다: Windows(CRLF) 체크아웃과 Linux(LF) 컨테이너가 같은 manifest 로 통과한다."""
    from layout_pipeline import semantic_assets as A
    from layout_pipeline.jsonio import sha256_text_file
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(b'{\n "a": 1\n}\n')
    crlf.write_bytes(b'{\r\n "a": 1\r\n}\r\n')
    assert sha256_text_file(lf) == sha256_text_file(crlf)
    policy = (A.ASSET_DIR / "classification-policy.json").read_bytes()
    other = tmp_path / "policy.json"
    other.write_bytes(policy.replace(b"\r\n", b"\n") if b"\r\n" in policy else policy.replace(b"\n", b"\r\n"))
    assert sha256_text_file(other) == sha256_text_file(A.ASSET_DIR / "classification-policy.json")
    assert A.verify_assets() == []
