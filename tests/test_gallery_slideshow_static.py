"""Gallery slideshow: wired into Photos and the photo detail, and polite with the gallery's keys."""
import re
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "static" / "js" / "gallery.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = JS.index(f"function {name}(")
    end = JS.index("\n}\n", start)
    return JS[start:end]


def test_the_slideshow_starts_from_photos_and_from_a_photo():
    assert 'id="gallery-slideshow-start"' in JS and 'id="gallery-slideshow-btn"' in JS
    assert "_startSlideshow(_baseOffset + _firstVisibleIndex())" in JS
    assert re.search(r"_startSlideshow\(at >= 0 \? _baseOffset \+ at : 0\)", JS)


def test_the_slideshow_plays_a_snapshot_of_the_current_list():
    start = _function("_startSlideshow")
    assert "_libraryParams()" in start and "total: _total" in start
    # Pages come from the same library endpoint, and videos are skipped.
    assert "/api/gallery/library?" in _function("_slidePage")
    assert "_isVideoUrl(candidate.url)" in _function("_slideTo")


def test_its_keys_do_not_reach_the_gallery():
    start = _function("_startSlideshow")
    assert "window.addEventListener('keydown', s.keys, true)" in start
    assert "e.stopImmediatePropagation()" in start
    stop = _function("_stopSlideshow")
    assert "window.removeEventListener('keydown', s.keys, true)" in stop and "exitFullscreen" in stop
    assert "_stopSlideshow();" in _function("_doCloseGallery")
