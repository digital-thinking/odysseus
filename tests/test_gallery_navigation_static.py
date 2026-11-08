"""Gallery navigation: browser Back/Forward, the header's Back button, and lists that never mix."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static" / "js" / "gallery.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "09-gallery.css").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = re.search(rf"(async )?function {name}\(", JS).start()
    end = JS.index("\n}\n", start)
    return JS[start:end]


def test_views_are_history_entries():
    commit = _function("_navCommit")
    assert "history.pushState({ odyGallery: { depth: 0, view } }" in commit          # opening the gallery
    assert "history.pushState({ odyGallery: { depth: cur.depth + 1, view } }" in commit
    assert "history.replaceState" in commit and "_navMinor(cur.view, view)" in commit
    # Recorded after a tab switch, a new Photos list, an opened photo, a person and a slideshow.
    assert "_navQueue();\n    });\n  });" in JS                                     # the tab click handler
    for name in ("_fetchLibrary", "_openDetail", "_renderPeopleTab", "_startSlideshow"):
        assert "_navQueue()" in _function(name), name
    view = _function("_navView")
    for key in ("tab:", "sort:", "dates:", "people:", "detail:", "show:"):
        assert key in view


def test_back_puts_the_view_back_and_closing_leaves_the_gallerys_entries():
    assert "window.addEventListener('popstate'" in JS
    apply = _function("_navApply")
    assert "_fetchLibrary(false)" in apply and "_stopSlideshow()" in apply and "_navShowPhoto(v.detail)" in apply
    assert "history.go(-(s.depth + 1))" in _function("_navLeave")
    assert "if (!_navRestoring) _navLeave();" in _function("_doCloseGallery")
    # The photo's and the slideshow's own close buttons go back, so Forward brings them again.
    assert "_navBackFrom('detail'" in JS
    assert JS.count("_navBackFrom('show', _stopSlideshow)") == 2
    # Back returns to where the list was scrolled (a trip found far down the Trips tab).
    assert "scroll: { ..._navScrollPos }" in _function("_navCommit")
    assert "_navRestoreScroll(s.scroll);" in JS


def test_esc_closes_the_open_photo_before_the_gallery():
    # ui.js closes the window under the pointer on Esc before the gallery's own key
    # handler runs; the open photo is an inner layer of the app's Esc stack.
    assert "registerEscapeLayer } from './escMenuStack.js';" in JS
    detail = _function("_openDetail")
    assert "_detailEscLayer = registerEscapeLayer(" in detail
    assert "document.getElementById('gallery-detail-back')?.click()" in detail


def test_the_header_has_a_back_button():
    assert 'id="gallery-nav-back"' in JS and "addEventListener('click', () => history.back())" in JS
    assert "btn.hidden = !(history.state?.odyGallery?.depth > 0)" in _function("_navSyncBack")
    assert ".gallery-nav-back[hidden] { display: none; }" in CSS


def test_a_new_list_does_not_show_or_mix_in_the_old_one():
    fetch = _function("_fetchLibrary")
    # While a new list loads, the old one's next page is not asked for...
    assert fetch.index("if (append && _listLoading) return;") < fetch.index("++_fetchSeq")
    assert "_listLoading" in _function("_loadNewer")
    # ...and its photos leave the grid at once.
    assert re.search(r"if \(key !== _listKey\) \{.*?_items = \[\];", fetch, re.S)
    assert "_listLoading = false;" in fetch.split("} finally {")[1]


def test_trip_cards_can_be_renamed():
    card = _function("_tripCard")
    assert '<div class="gallery-trip-card" role="button" tabindex="0"' in card   # not a button: one sits inside
    assert 'class="gallery-trip-rename"' in card
    rename = _function("_renameTrip")
    assert "/api/gallery/trips/name" in rename and "method: 'PUT'" in rename
    assert "return t.name || _tripAutoTitle(t);" in _function("_tripTitle")
    # Esc while renaming keeps the old name instead of closing the gallery.
    assert "renaming._cancel?.()" in JS
