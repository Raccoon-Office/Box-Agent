from tests.pptx_test_support import PPT_PLUGIN_ROOT


BUNDLE_PATH = (
    PPT_PLUGIN_ROOT
    / "skills"
    / "document-skills"
    / "pptx"
    / "scripts"
    / "dom-to-pptx.bundle.js"
)


def test_dom_to_pptx_bundle_documents_vendor_inflate_mode_resume() -> None:
    """Keep pako's upstream TYPEDO state documented as vendor code."""
    source = BUNDLE_PATH.read_text(encoding="utf-8")

    assert "const        TYPEDO = 16192" in source
    assert "if (state.mode === TYPE) {" in source
    assert "state.mode = TYPEDO;" in source


def test_dom_to_pptx_bundle_exports_browser_api() -> None:
    source = BUNDLE_PATH.read_text(encoding="utf-8")

    assert "factory(global.domToPptx = {})" in source
    assert "exports.exportToPptx = exportToPptx" in source
