/**
 * Gallery Module — photo backup + AI-generated image library.
 */

import uiModule from './ui.js?v=20260916largetoolscroll1';
import { loadPanel } from './panels.js?v=20260909movepicklayer1';
import spinnerModule from './spinner.js';
import { makeWindowDraggable } from './windowDrag.js';
import { bindMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import sessionModule from './sessions.js';
import fileHandlerModule from './fileHandler.js?v=20260909mobileattachmentedit1';

const API_BASE = window.location.origin;
const ACTIVE_EDITOR_SESSION_KEY = 'odysseus-gallery-active-editor-v1';
let _open = false;
let _galleryResizeHandler = null;

// ── Image editor, loaded on first use ──
// galleryEditor.js plus everything under js/editor/ is 54 modules / 576 KB.
// It used to be a static import here, so every page load paid for it even
// though most sessions never touch the Edit tab. The wrappers below keep the
// three call shapes the rest of this file already uses.
//
// closeEditor() and isEditorOpen() stay synchronous on purpose: if the module
// was never loaded there is no edit session to close, and none can be open.
let _editorMod = null;
let _editorLoading = false;

// The editor shell is mounted before the size prompt resolves. A cancellation
// can therefore finish while the Gallery loader's transient flag is stale;
// restore the visible landing from the DOM after the editor tears down.
if (!window.__galleryEditorSetupCancelHandler) {
  window.__galleryEditorSetupCancelHandler = () => {
    const container = document.getElementById('gallery-editor-container');
    const tab = document.querySelector('#gallery-editor-tab.active');
    if (container && tab && !container.querySelector('.gallery-editor')) {
      _renderEditorLanding();
    }
  };
  window.addEventListener('gallery-editor-setup-cancelled', window.__galleryEditorSetupCancelHandler);
}

async function _loadEditor() {
  _editorLoading = true;
  try {
    _editorMod = await loadPanel('editor');
    return _editorMod;
  } finally {
    _editorLoading = false;
  }
}

async function openEditor(...args) {
  let mod = _editorMod;
  if (!mod) {
    try {
      mod = await _loadEditor();
    } catch (e) {
      // Previously unreachable — a static import either loaded or the whole
      // page failed. Now it can fail on its own (offline before the panel was
      // ever cached), so say so instead of doing nothing.
      console.error('[gallery] image editor failed to load', e);
      uiModule?.showError?.('Failed to load the image editor');
      return;
    }
  }
  return mod.openEditor(...args);
}

function closeEditor(...args) {
  return _editorMod ? _editorMod.closeEditor(...args) : undefined;
}

// True while the module is still in flight as well — the gallery-close paths
// use this to refuse to tear the container down under an edit that is opening.
function isEditorOpen() {
  return _editorLoading || (_editorMod ? _editorMod.isEditorOpen() : false);
}

function _readActiveEditorSession() {
  try {
    const raw = sessionStorage.getItem(ACTIVE_EDITOR_SESSION_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!value || (!value.draftId && !value.imageUrl && !value.imageId
      && !(Number(value.width) > 0 && Number(value.height) > 0))) return null;
    return value;
  } catch (_) {
    return null;
  }
}

// Gallery is normally lazy and transient. If an edit was active in this tab
// before refresh, restore that edit after the gallery shell has been mounted.
async function _restoreActiveEditorAfterRefresh() {
  const saved = _readActiveEditorSession();
  if (!saved) return;
  try {
    openGallery();
    let editorTab = null;
    // The gallery module is lazy-loaded during startup. Give its synchronous
    // mount a few frames to complete before treating the restore as failed.
    for (let attempt = 0; attempt < 12 && !editorTab; attempt += 1) {
      editorTab = document.querySelector('#gallery-modal .gallery-tab[data-tab="editor"]');
      if (!editorTab) await new Promise(resolve => requestAnimationFrame(resolve));
    }
    if (!editorTab) return;
    editorTab.click();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await openEditor(
      saved.draftId ? null : (saved.imageUrl || null),
      saved.imageId || null,
      saved.width > 0 && saved.height > 0 ? { w: saved.width, h: saved.height } : null,
      saved.draftName || null,
      saved.draftId || null,
    );
  } catch (error) {
    console.warn('[gallery] active editor restore failed', error);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(_restoreActiveEditorAfterRefresh, 0), { once: true });
} else {
  setTimeout(_restoreActiveEditorAfterRefresh, 0);
}

// Auto-refresh gallery when new image is generated
window.addEventListener('gallery-refresh', (e) => {
  if (e?.detail?.source === 'chat-upload' && _sort !== 'recent') {
    _sort = 'recent';
    const sortSel = document.getElementById('gallery-sort');
    if (sortSel) sortSel.value = 'recent';
  }
  if (_open) _fetchLibrary(false);
});
let _items = [];
let _total = 0;
let _totalTagged = 0;

// Update the "X/Y tagged" badge in the AI-tagging settings header.
function _updateTagCount() {
  const el = document.getElementById('gallery-tag-count');
  if (el) el.textContent = _total ? `${_totalTagged}/${_total} tagged` : '';
}
let _search = '';
// Stack of active tag filters. Multiple tags AND together — the user
// builds this up by clicking tag chips or by hitting Enter in the
// search box, and tears it down with the × on each pill.
let _activeTags = [];
let _activeModel = null;
let _activeAlbum = null;
let _galleryCascaded = false;   // play the domino-in cascade once per open
let _favoritesOnly = false;
let _sort = 'recent';
// Read-only library folders (Gallery → Settings → Library folders).
// `_activeFolder` = { source, path, label } or null; `_folders` = folder tree.
let _folders = [];
let _activeFolder = null;
// ✨ Smart search ranks photos by what they show (embedding index) instead
// of matching words in names and tags.
let _smart = false;
let _shuffleSeed = Math.floor(Math.random() * 2 ** 31);
let _offset = 0;
// Page size — computed from the grid's visible area so taller / wider
// windows (fullscreen) fetch enough photos to fill the screen instead
// of leaving blank space below a fixed 24-photo page. Capped at the
// backend's max (100).
let _limit = 24;
function _computeFetchLimit() {
  const grid = document.getElementById('gallery-grid');
  const COL_W = 168; // 160px min column + 8px gap
  const ROW_H = 200; // ~160px image + caption + gap
  const gridW = (grid && grid.clientWidth) || Math.min(window.innerWidth * 0.9, 1100);
  const cols = Math.max(2, Math.floor(gridW / COL_W));
  // The grid scroll viewport is max-height:60vh.
  const gridH = window.innerHeight * 0.6;
  const rows = Math.ceil(gridH / ROW_H) + 2; // +2 buffer rows for scroll
  return Math.min(100, Math.max(24, cols * rows));
}
let _searchDebounce = null;
let _escHandler = null;
let _albums = [];
// Albums tab — search filter + multi-select state. Mirrors what the
// Photos tab does (_search, _selectMode) but scoped to the albums grid.
let _albumSearch = '';
let _albumSelectMode = false;
const _albumSelected = new Set();

// ---- API helpers ----

let _fetchSeq = 0;   // only the newest request may update the grid

async function _fetchLibrary(append) {
  const seq = ++_fetchSeq;
  // Recompute the page size each fetch so resizing / fullscreening the
  // window between loads pulls the right number of photos.
  _limit = _computeFetchLimit();
  // First load with nothing on screen → show skeleton tiles instead of a blank
  // grid that then snaps to full. BUT: if the last successful load returned
  // zero items, skip the skeleton entirely — otherwise empty accounts flash
  // 8-20 placeholder tiles for ~200ms before snapping to the "No photos yet"
  // message, which read as glitchy.
  if (!append && _items.length === 0) {
    let _knownEmpty = false;
    try { _knownEmpty = localStorage.getItem('gallery-known-empty') === '1'; } catch (_) {}
    if (!_knownEmpty) _renderSkeletons(_limit);
  }
  if (!append) {
    _offset = 0;
    // Leave _items untouched until the response arrives — that's the
    // stale-while-revalidate trick that lets the gallery feel instant on
    // re-open. The new list replaces _items on success below; if the fetch
    // fails, the previous photos stay visible.
  }
  const params = new URLSearchParams({ sort: _sort, offset: _offset, limit: _limit });
  if (_sort === 'shuffle') params.set('seed', String(_shuffleSeed));
  if (_search) params.set('search', _search);
  if (_search && _smart) params.set('semantic', 'true');
  if (_activeTags.length) params.set('tag', _activeTags.join(','));
  if (_activeModel) params.set('model', _activeModel);
  if (_activeAlbum) params.set('album', _activeAlbum);
  if (_favoritesOnly) params.set('favorites', 'true');
  if (_activePeople.length) params.set('person', _activePeople.map(p => p.id).join(','));
  if (_activeExactTag) params.set('exact_tag', _activeExactTag);
  if (_activeSimilar) params.set('similar_to', _activeSimilar.id);
  if (_activeNear) params.set('near', `${_activeNear.lat},${_activeNear.lng}`);
  if (_activeDates) {
    params.set('since', _activeDates.since);
    params.set('until', _activeDates.until);
  }
  if (_activeFolder) {
    params.set('source', _activeFolder.source);
    if (_activeFolder.path) params.set('folder', _activeFolder.path);
  }
  try {
    const res = await fetch(`${API_BASE}/api/gallery/library?${params}`, { credentials: 'same-origin' });
    if (seq !== _fetchSeq) return;   // a newer search / filter has been sent
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (_) {}
      if (_activeSimilar) {
        uiModule.showError(detail || 'Could not find similar photos');
        _activeSimilar = null;   // don't leave a filter pill up that shows nothing
        _renderAlbums();
        return _fetchLibrary(false);
      }
      if (_smart && _search) uiModule.showError(detail || 'Smart search failed');
      else console.error('Gallery fetch failed:', res.status, detail);
      return;
    }
    const data = await res.json();
    if (seq !== _fetchSeq) return;
    if (append) {
      _items = _items.concat(data.items || []);
    } else {
      _items = data.items || [];
    }
    // Cache an "empty" verdict so the next open of an empty gallery doesn't
    // flash skeleton tiles before the real "No photos yet" message.
    try {
      const _noFilters = !_search && !_activeTags.length && !_activeModel && !_activeAlbum && !_favoritesOnly && !_activeFolder && !_activePeople.length && !_activeExactTag && !_activeSimilar && !_activeNear && !_activeDates;
      if (_noFilters) {
        if (_items.length === 0) localStorage.setItem('gallery-known-empty', '1');
        else localStorage.removeItem('gallery-known-empty');
      }
    } catch (_) {}
    _total = data.total || 0;
    if (typeof data.total_tagged === 'number') _totalTagged = data.total_tagged;
    _updateTagCount();
    _renderGrid();
    _renderTags(data.tags || []);
    _renderModels(data.models || []);
    _renderAlbums();
    _renderStats();
  } catch (e) {
    console.error('Gallery fetch error:', e);
  }
}

async function _fetchAlbums() {
  try {
    const res = await fetch(`${API_BASE}/api/gallery/albums`, { credentials: 'same-origin' });
    const data = await res.json();
    _albums = data.albums || [];
    _renderAlbums();
  } catch (e) { console.error('Albums fetch error:', e); }
}


// v2 review HIGH-7: return a boolean so callers can stop showing
// "Tags saved" / "Photo deleted" toasts when the server actually
// returned 4xx/5xx. The previous swallow-and-return-undefined caused
// silent UI lies on permission failures.
async function _patchImage(id, patch) {
  try {
    const r = await fetch(`${API_BASE}/api/gallery/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(patch),
    });
    if (!r.ok) {
      console.warn('Gallery patch returned', r.status);
      return false;
    }
    return true;
  } catch (e) {
    console.error('Gallery patch error:', e);
    return false;
  }
}

async function _deleteImage(id) {
  try {
    const r = await fetch(`${API_BASE}/api/gallery/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    if (!r.ok) {
      console.warn('Gallery delete returned', r.status);
      return false;
    }
    return true;
  } catch (e) {
    console.error('Gallery delete error:', e);
    return false;
  }
}

// ---- Bulk upload with progress ----

// Accepts either File[] (uploads all into fallbackAlbumId) or
// {file, albumId}[] (per-file album targeting — used for folder drops).
async function _bulkUpload(filesOrItems, fallbackAlbumId) {
  const bar = document.getElementById('gallery-upload-bar');
  const progress = document.getElementById('gallery-upload-progress');
  const status = document.getElementById('gallery-upload-status');
  if (!bar) return;

  const items = filesOrItems.map(it =>
    it instanceof File ? { file: it, albumId: fallbackAlbumId } : it
  );

  bar.style.display = '';
  let done = 0, dupes = 0, errors = 0;
  const total = items.length;

  // Concurrency pool — N workers pulling from the queue. 4 is a reasonable
  // default for a local server: enough to overlap network + EXIF + disk
  // without flooding SQLite (which serializes writes anyway). Videos in
  // particular benefit because they're large enough to be I/O-bound.
  const CONCURRENCY = 4;
  let cursor = 0;
  async function worker() {
    while (true) {
      const idx = cursor++;
      if (idx >= items.length) return;
      const it = items[idx];
      const fd = new FormData();
      fd.append('file', it.file);
      if (it.albumId) fd.append('album_id', it.albumId);
      try {
        const res = await fetch(`${API_BASE}/api/gallery/upload`, {
          method: 'POST', body: fd, credentials: 'same-origin',
        });
        const data = await res.json();
        if (data.duplicate) dupes++;
        else if (!data.ok) errors++;
      } catch (e) { errors++; }
      done++;
      if (progress) progress.style.width = `${(done / total) * 100}%`;
      if (status) status.textContent = `${done}/${total}${dupes ? ` (${dupes} duplicates)` : ''}`;
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, total) }, worker));

  const msg = `${done - dupes - errors} imported` +
    (dupes ? `, ${dupes} duplicates skipped` : '') +
    (errors ? `, ${errors} errors` : '');
  if (status) status.textContent = msg;
  uiModule.showToast(msg);
  setTimeout(() => { bar.style.display = 'none'; }, 3000);
  // Auto-switch to Recent so the just-uploaded photos are immediately
  // visible at the top (otherwise Shuffle would scatter them).
  if (done - dupes - errors > 0 && _sort !== 'recent') {
    _sort = 'recent';
    const sortSel = document.getElementById('gallery-sort');
    if (sortSel) sortSel.value = 'recent';
  }
  _fetchLibrary(false);
  _fetchAlbums();
}

// True if this File / filename should be uploaded — images and common videos.
function _isMediaFile(f) {
  const t = (f?.type || '').toLowerCase();
  if (t.startsWith('image/') || t.startsWith('video/')) return true;
  // Some Linux file managers and older browsers leave .type blank; fall
  // back to the extension.
  const ext = (f?.name || '').toLowerCase().split('.').pop() || '';
  return ['png','jpg','jpeg','webp','gif','mp4','mov','webm','mkv','m4v'].includes(ext);
}

// True if a URL/filename refers to a video — used to pick <video> vs <img>.
function _isVideoUrl(url) {
  const ext = (url || '').toLowerCase().split('?')[0].split('.').pop();
  return ['mp4','mov','webm','mkv','m4v'].includes(ext);
}

// Recursively walk a webkit FileSystemEntry, returning all media Files under it.
async function _walkEntryForImages(entry) {
  if (entry.isFile) {
    return new Promise(res => {
      entry.file(
        f => res(_isMediaFile(f) ? [f] : []),
        () => res([])
      );
    });
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const out = [];
  while (true) {
    const batch = await new Promise(res => reader.readEntries(res, () => res([])));
    if (!batch.length) break;
    const subs = await Promise.all(batch.map(_walkEntryForImages));
    subs.forEach(s => out.push(...s));
  }
  return out;
}

// Handle a native drop: split into folders (→ new/existing albums) and loose files
// (→ current album). Returns when the whole upload is complete.
async function _handleGalleryDrop(e) {
  const dtItems = [...(e.dataTransfer?.items || [])];
  const entries = dtItems
    .map(it => (typeof it.webkitGetAsEntry === 'function' ? it.webkitGetAsEntry() : null))
    .filter(Boolean);

  const uploadItems = [];
  let sawFolderEntry = false;

  for (const entry of entries) {
    if (entry.isDirectory) {
      sawFolderEntry = true;
      let album = _albums.find(a => a.name === entry.name);
      if (!album) {
        try {
          const res = await fetch(`${API_BASE}/api/gallery/albums`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ name: entry.name }),
          });
          const data = await res.json();
          if (data && data.id) album = { id: data.id, name: data.name || entry.name };
        } catch (err) { console.error('Failed to create album for', entry.name, err); }
      }
      if (!album) continue;
      const files = await _walkEntryForImages(entry);
      files.forEach(f => uploadItems.push({ file: f, albumId: album.id }));
    } else if (entry.isFile) {
      const f = await new Promise(res => entry.file(res, () => res(null)));
      if (f && _isMediaFile(f)) {
        uploadItems.push({ file: f, albumId: _activeAlbum });
      }
    }
  }

  // Fallback: some drag sources (Linux file managers like Thunar/Nautilus,
  // or older browsers) don't populate FileSystemEntry but DO populate
  // dataTransfer.files for loose files. Pick those up too.
  if (!uploadItems.length) {
    const files = [...(e.dataTransfer?.files || [])].filter(_isMediaFile);
    files.forEach(f => uploadItems.push({ file: f, albumId: _activeAlbum }));
  }

  if (uploadItems.length) {
    await _bulkUpload(uploadItems);
    return;
  }

  // Nothing usable — either an empty folder, an unreadable folder URI, or a
  // non-image drop. If the dataTransfer types hint at a folder/URI drop,
  // explain the limitation and point at the Upload album button.
  const types = [...(e.dataTransfer?.types || [])];
  const looksLikeFolderUri = !sawFolderEntry && (
    types.includes('text/uri-list') ||
    types.includes('text/x-moz-url') ||
    dtItems.some(it => it.kind === 'string')
  );
  if (looksLikeFolderUri) {
    uiModule.showError('Browsers can’t read folders dropped from native file managers (Thunar/Nautilus). Use the "Upload album" tile in the Albums tab instead.');
  } else if (entries.length || dtItems.length) {
    uiModule.showToast('No images found in that drop');
  }
}

// ---- Render helpers ----

function _renderStats() {
  const el = document.getElementById('gallery-stats');
  if (el) el.textContent = `${_total} photo${_total !== 1 ? 's' : ''}`;
}

function _filterPill(label, value, attr) {
  return `<span class="gallery-chip gallery-active-filter-pill" title="${_esc(label)}"><span>${_esc(value)}</span><button class="gallery-chip-clear" ${attr} aria-label="Clear ${_esc(label)} filter">&times;</button></span>`;
}

function _renderTags(tags) {
  // The global "every tag in the gallery" chip row under the search is gone —
  // it just piled up every user-added tag with no way to remove it. Filter by
  // tapping a tag on a photo (→ a removable pill in the header) or via search.
  const container = document.getElementById('gallery-tag-chips');
  if (!container) return;
  container.innerHTML = '';
  container.style.display = 'none';
}

function _renderModels(models) {
  const sel = document.getElementById('gallery-model-filter');
  if (!sel) return;
  let html = '<option value="">All sources</option>';
  models.forEach(m => {
    const selected = _activeModel === m ? ' selected' : '';
    html += `<option value="${_esc(m)}"${selected}>${_esc(m)}</option>`;
  });
  sel.innerHTML = html;
}

// ---- Library folders (read-only photo sources) ----

function _syncSmartButton() {
  const btn = document.getElementById('gallery-smart-btn');
  const input = document.getElementById('gallery-search');
  if (btn) {
    btn.classList.toggle('active', _smart);
    btn.setAttribute('aria-pressed', _smart ? 'true' : 'false');
  }
  if (input) input.placeholder = _smart ? 'Describe a photo… (smart search)' : 'Search photos, tags...';
}

async function _fetchFolders() {
  try {
    const res = await fetch(`${API_BASE}/api/gallery/folders`, { credentials: 'same-origin' });
    _folders = res.ok ? ((await res.json()).folders || []) : [];
  } catch (_) {
    _folders = [];
  }
  _renderFolders();
}

function _folderValue(f) { return `${f.source_id}|${f.path}`; }

function _renderFolders() {
  const sel = document.getElementById('gallery-folder-filter');
  if (!sel) return;
  if (!_folders.length) {
    sel.style.display = 'none';
    sel.innerHTML = '';
    return;
  }
  sel.style.display = '';
  const current = _activeFolder ? `${_activeFolder.source}|${_activeFolder.path}` : '';
  let html = '<option value="">All folders</option>';
  for (const f of _folders) {
    const parts = f.path ? f.path.split('/') : [];
    const name = parts.length ? parts[parts.length - 1] : f.source_name;
    const indent = '   '.repeat(parts.length);
    const val = _folderValue(f);
    html += `<option value="${_esc(val)}"${val === current ? ' selected' : ''}>${indent}${_esc(name)} (${f.count})</option>`;
  }
  sel.innerHTML = html;
}

function _setFolderFilter(value) {
  if (!value) {
    _activeFolder = null;
  } else {
    const f = _folders.find(x => _folderValue(x) === value);
    const i = value.indexOf('|');
    _activeFolder = {
      source: value.slice(0, i),
      path: value.slice(i + 1),
      label: f ? [f.source_name, f.path].filter(Boolean).join(' / ') : value.slice(i + 1),
    };
    // A photo folder reads best in the order the photos were taken.
    if (_sort !== 'taken_asc' && _sort !== 'taken_desc') {
      _sort = 'taken_asc';
      const sortSel = document.getElementById('gallery-sort');
      if (sortSel) sortSel.value = _sort;
    }
  }
  const sel = document.getElementById('gallery-folder-filter');
  if (sel) sel.value = value || '';
  _fetchLibrary(false);
  _renderAlbums();
}

let _libraryPoll = null;

function _scanTime(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (isNaN(d)) return 'never';
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return d.toLocaleDateString();
}

// "0 3 * * *" → "daily at 03:00 UTC"; anything else is shown as the raw cron.
function _cronText(cron) {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec((cron || '').trim());
  if (m) return `daily at ${m[2].padStart(2, '0')}:${m[1].padStart(2, '0')} UTC`;
  return cron ? `cron “${cron}” (UTC)` : 'no schedule';
}

function _inText(iso) {
  if (!iso) return '';
  const mins = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (isNaN(mins)) return '';
  if (mins <= 1) return 'in a moment';
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.round(mins / 60);
  return hours < 48 ? `in ${hours} h` : new Date(iso).toLocaleDateString();
}

// New photos are picked up by the scheduled "Gallery Library Index" task.
function _renderIndexTask(task) {
  const box = document.getElementById('gallery-index-task');
  if (!box) return;
  if (!task) {
    box.innerHTML = 'New photos are picked up by the scheduled task <strong>Gallery Library Index</strong> — it appears in Tasks after the next restart.';
    return;
  }
  const paused = task.status !== 'active';
  const when = paused
    ? '<strong>paused</strong> — new photos are only added when you click Scan now'
    : `${_esc(_cronText(task.cron_expression))}${task.next_run ? ` · next run ${_esc(_inText(task.next_run))}` : ''}`;
  box.innerHTML = `New photos are picked up by the task <strong>${_esc(task.name)}</strong>: ${when}. `
    + `<a href="#" class="gallery-index-task-link" data-task-id="${_esc(task.id)}">Schedule or pause it in Tasks</a>`;
}

function _unreadableList(stats) {
  const files = Object.keys((stats && stats.unreadable_files) || {}).sort();
  return files.length ? `Unreadable files (empty or damaged, left untouched):\n${files.join('\n')}` : '';
}

function _scanSummary(stats) {
  if (!stats || !Object.keys(stats).length) return 'Not scanned yet';
  const bits = [];
  if (stats.new) bits.push(`${stats.new} new`);
  if (stats.updated) bits.push(`${stats.updated} updated`);
  if (stats.missing) bits.push(`${stats.missing} no longer in the folder`);
  if (stats.skipped_videos) bits.push(`${stats.skipped_videos} videos skipped`);
  const unreadable = stats.unreadable ?? stats.errors;
  if (unreadable) bits.push(`${unreadable} unreadable${_unreadableList(stats) ? ' (hover for the list)' : ''}`);
  if (stats.tagging && stats.tagging.error) bits.push(`tagging stopped: ${stats.tagging.error}`);
  return bits.join(' · ') || 'Up to date';
}

async function _loadLibrarySources() {
  const card = document.getElementById('gallery-library-card');
  if (!card) return;
  let data;
  try {
    const res = await fetch(`${API_BASE}/api/gallery/sources`, { credentials: 'same-origin' });
    if (!res.ok) { card.style.display = 'none'; return; }   // admins only
    data = await res.json();
  } catch (_) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  const list = document.getElementById('gallery-sources-list');
  const hint = document.getElementById('gallery-source-hint');
  const count = document.getElementById('gallery-library-count');
  const sources = data.sources || [];
  const photos = sources.reduce((n, src) => n + (src.photo_count || 0), 0);
  if (count) count.textContent = sources.length ? `${photos} photos` : '';
  if (hint) {
    hint.textContent = (data.mounts || []).length
      ? `Allowed locations: ${data.mounts.join(' · ')}`
      : 'No library locations are configured on the server yet — set ODYSSEUS_GALLERY_LIBRARY_MOUNTS (see docker/gallery-library.yml).';
  }
  if (list) {
    list.innerHTML = sources.map(src => `
      <div class="gallery-source-row" data-id="${_esc(src.id)}">
        <div class="gallery-source-main">
          <div class="gallery-source-name">${_esc(src.name)} <span class="gallery-source-ro" title="Odysseus never writes to this folder">read-only</span></div>
          <div class="gallery-source-path">${_esc(src.path)}${(src.include_paths || []).length ? ` · only ${_esc(src.include_paths.join(', '))}` : ''}</div>
          <div class="gallery-source-stats" title="${_esc(_unreadableList(src.last_scan_stats))}">${src.photo_count} photos · ${src.tagged_count} AI-tagged · scanned ${_esc(_scanTime(src.last_scan_at))}<br>${_esc(_scanSummary(src.last_scan_stats))}</div>
        </div>
        <div class="gallery-source-actions">
          <label title="Add new photos to the ✨ smart-search index"><input type="checkbox" class="gallery-source-opt-embed"${src.sync?.embed === false ? '' : ' checked'}> Smart search</label>
          <label title="AI-tag new photos (tagging model below)"><input type="checkbox" class="gallery-source-opt-tag"${src.sync?.tag ? ' checked' : ''}> AI tags</label>
          <label title="Find faces in new photos and recognise people (People tab)"><input type="checkbox" class="gallery-source-opt-faces"${src.sync?.faces ? ' checked' : ''}> People</label>
          <label title="Make one album per top-level folder and keep it up to date"><input type="checkbox" class="gallery-source-opt-albums"${src.sync?.albums ? ' checked' : ''}> Folder albums</label>
          <button class="memory-toolbar-btn gallery-source-edit" data-include="${_esc((src.include_paths || []).join(', '))}" title="Choose which subfolders are included">Subfolders</button>
          <button class="memory-toolbar-btn gallery-source-scan" title="Scan now (only new or changed photos are processed)">Scan now</button>
          <button class="memory-toolbar-btn gallery-source-remove" title="Forget this folder in Odysseus — the photos themselves are not touched">Remove</button>
        </div>
      </div>`).join('');
  }
  _renderIndexTask(data.index_task);
  _renderLibraryJob(data.job);
  if (data.job && data.job.state === 'running') _pollLibraryJob();
}

function _renderLibraryJob(job) {
  const box = document.getElementById('gallery-library-job');
  if (!box) return;
  if (!job) { box.style.display = 'none'; return; }
  box.style.display = '';
  const bar = document.getElementById('gallery-library-job-progress');
  const status = document.getElementById('gallery-library-job-status');
  const cancel = document.getElementById('gallery-library-job-cancel');
  const running = job.state === 'running';
  const pct = job.total ? Math.round((job.done / job.total) * 100) : (running ? 3 : 100);
  if (bar) bar.style.width = `${pct}%`;
  const phase = {
    starting: 'Starting', listing: 'Listing files', scanning: 'Reading photos and making thumbnails',
    embedding: 'Building the smart-search index', faces: 'Finding faces', tagging: 'AI-tagging photos',
    done: 'Done', cancelled: 'Cancelled', error: 'Failed',
  }[job.phase] || job.phase;
  let text = running && job.total ? `${phase} — ${job.done}/${job.total}` : phase;
  if (job.trigger === 'task') text = `Scheduled index: ${text}`;
  if (job.state === 'error' && job.error) text += `: ${job.error}`;
  if (!running && job.result) {
    const r = job.result;
    const bits = [];
    if (r.scan) bits.push(`${r.scan.new} new, ${r.scan.updated} updated, ${r.scan.unchanged} unchanged`);
    if (r.albums?.created) bits.push(`${r.albums.created} new album${r.albums.created === 1 ? '' : 's'}`);
    if (r.embedding) bits.push(`${r.embedding.embedded} added to smart search`);
    if (r.faces) bits.push(`${r.faces.faces} faces found, ${r.faces.matched || 0} recognised, ${r.faces.new_people || 0} new people`);
    if (r.tagging) bits.push(r.tagging.error ? `tagging stopped: ${r.tagging.error}` : `${r.tagging.tagged} AI-tagged`);
    if (bits.length) text += ` — ${bits.join(' · ')}`;
  }
  if (status) status.textContent = text;
  if (cancel) cancel.style.display = running ? '' : 'none';
}

function _pollLibraryJob() {
  if (_libraryPoll) return;
  _libraryPoll = setInterval(async () => {
    let job = null;
    try {
      const res = await fetch(`${API_BASE}/api/gallery/sources/job`, { credentials: 'same-origin' });
      job = res.ok ? (await res.json()).job : null;
    } catch (_) {}
    _renderLibraryJob(job);
    if (!job || job.state !== 'running') {
      clearInterval(_libraryPoll);
      _libraryPoll = null;
      _loadLibrarySources();
      _fetchFolders();
      _fetchAlbums();
      _fetchLibrary(false);
    }
  }, 1500);
}

function _wireLibrarySettings() {
  const addBtn = document.getElementById('gallery-source-add-btn');
  addBtn?.addEventListener('click', async () => {
    const pathEl = document.getElementById('gallery-source-path');
    const incEl = document.getElementById('gallery-source-include');
    const path = (pathEl?.value || '').trim();
    if (!path) { pathEl?.focus(); return; }
    const include_paths = (incEl?.value || '').split(',').map(x => x.trim()).filter(Boolean);
    addBtn.disabled = true;
    try {
      const res = await fetch(`${API_BASE}/api/gallery/sources`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, include_paths }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { uiModule.showError(data.detail || 'Could not add the folder'); return; }
      if (pathEl) pathEl.value = '';
      if (incEl) incEl.value = '';
      uiModule.showToast?.(`Added “${data.source.name}” — click Scan to index it`, 4000);
      _loadLibrarySources();
    } finally {
      addBtn.disabled = false;
    }
  });
  document.getElementById('gallery-sources-list')?.addEventListener('click', async (e) => {
    const row = e.target.closest('.gallery-source-row');
    if (!row) return;
    const id = row.dataset.id;
    const editBtn = e.target.closest('.gallery-source-edit');
    if (editBtn) {
      const value = await uiModule.styledPrompt(
        'Only include these subfolders, separated by commas (e.g. 2005, 2006). Leave empty for the whole folder. Photos already indexed keep their tags.',
        { title: 'Subfolders', defaultValue: editBtn.dataset.include || '', placeholder: '2005, 2006', maxLength: 1000 },
      );
      if (value === null || value === undefined) return;
      const include_paths = String(value).split(',').map(x => x.trim()).filter(Boolean);
      const res = await fetch(`${API_BASE}/api/gallery/sources/${encodeURIComponent(id)}`, {
        method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include_paths }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { uiModule.showError(data.detail || 'Could not save the subfolders'); return; }
      uiModule.showToast?.('Saved — click Scan now (or wait for auto-sync) to index them', 4000);
      _loadLibrarySources();
      return;
    }
    if (e.target.closest('.gallery-source-scan')) {
      const embed = row.querySelector('.gallery-source-opt-embed')?.checked ?? true;
      const tag = row.querySelector('.gallery-source-opt-tag')?.checked ?? false;
      const faces = row.querySelector('.gallery-source-opt-faces')?.checked ?? false;
      const albums = row.querySelector('.gallery-source-opt-albums')?.checked ?? false;
      const res = await fetch(`${API_BASE}/api/gallery/sources/${encodeURIComponent(id)}/scan`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ embed, tag, faces, albums }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { uiModule.showError(data.detail || 'Could not start the scan'); return; }
      _renderLibraryJob(data.job);
      _pollLibraryJob();
    } else if (e.target.closest('.gallery-source-remove')) {
      const ok = await uiModule.styledConfirm(
        'Remove this folder from the gallery? Odysseus forgets its photos, tags and search index. The folder and its photos are not touched.',
        { confirmText: 'Remove', danger: true },
      );
      if (!ok) return;
      const res = await fetch(`${API_BASE}/api/gallery/sources/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { uiModule.showError(data.detail || 'Could not remove the folder'); return; }
      if (_activeFolder && _activeFolder.source === id) _activeFolder = null;
      _loadLibrarySources();
      _fetchFolders();
      _fetchAlbums();
      _fetchLibrary(false);
      _renderAlbums();
    }
  });
  // The Smart search / AI tags choices are saved per folder (used by Scan now
  // and by the scheduled index task).
  document.getElementById('gallery-sources-list')?.addEventListener('change', async (e) => {
    if (!e.target.closest('.gallery-source-opt-embed, .gallery-source-opt-tag, .gallery-source-opt-faces, .gallery-source-opt-albums')) return;
    const row = e.target.closest('.gallery-source-row');
    if (!row) return;
    const sync = {
      embed: !!row.querySelector('.gallery-source-opt-embed')?.checked,
      tag: !!row.querySelector('.gallery-source-opt-tag')?.checked,
      faces: !!row.querySelector('.gallery-source-opt-faces')?.checked,
      albums: !!row.querySelector('.gallery-source-opt-albums')?.checked,
    };
    const res = await fetch(`${API_BASE}/api/gallery/sources/${encodeURIComponent(row.dataset.id)}`, {
      method: 'PATCH', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sync }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { uiModule.showError(data.detail || 'Could not save the sync settings'); return; }
    _loadLibrarySources();
    _fetchAlbums();
  });
  document.getElementById('gallery-index-task')?.addEventListener('click', async (e) => {
    const link = e.target.closest('.gallery-index-task-link');
    if (!link) return;
    e.preventDefault();
    try {
      const m = await import('./tasks.js?v=20260914taskmodel1');
      m.openTasks(link.dataset.taskId || null, { filter: 'Gallery' });
    } catch (err) {
      console.error('Could not open Tasks:', err);
    }
  });
  document.getElementById('gallery-library-job-cancel')?.addEventListener('click', () => {
    fetch(`${API_BASE}/api/gallery/sources/job/cancel`, { method: 'POST', credentials: 'same-origin' }).catch(() => {});
  });
}

async function _loadTagModelPicker() {
  const row = document.getElementById('gallery-tag-model-row');
  const sel = document.getElementById('gallery-tag-model');
  if (!row || !sel) return;
  try {
    const [settingsRes, modelsRes] = await Promise.all([
      fetch(`${API_BASE}/api/auth/settings`, { credentials: 'same-origin' }),
      fetch(`${API_BASE}/api/models`, { credentials: 'same-origin' }),
    ]);
    if (!settingsRes.ok || !modelsRes.ok) { row.style.display = 'none'; return; }
    const settings = await settingsRes.json();
    if (!('gallery_tag_model' in settings)) { row.style.display = 'none'; return; }
    const models = await modelsRes.json();
    const ids = [];
    for (const item of (models.items || [])) {
      if (item.model_type && item.model_type !== 'llm') continue;
      for (const m of (item.models || [])) if (!ids.includes(m)) ids.push(m);
    }
    const current = settings.gallery_tag_model || '';
    if (current && !ids.includes(current)) ids.unshift(current);
    sel.innerHTML = '<option value="">Same as the Vision model</option>'
      + ids.map(m => `<option value="${_esc(m)}"${m === current ? ' selected' : ''}>${_esc(m)}</option>`).join('');
    row.style.display = '';
    if (!sel._wired) {
      sel._wired = true;
      sel.addEventListener('change', async () => {
        const res = await fetch(`${API_BASE}/api/auth/settings`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gallery_tag_model: sel.value }),
        });
        if (!res.ok) uiModule.showError('Could not save the tagging model');
        else uiModule.showToast?.(sel.value ? `AI tags now use ${sel.value}` : 'AI tags now use the Vision model', 2500);
      });
    }
  } catch (_) {
    row.style.display = 'none';
  }
}


// ---- People (recognised faces) ----

let _activePeople = [];       // [{ id, name }] — Photos filter: photos showing all of them
let _activeSimilar = null;     // { id, label } — Photos ranked by how much they look like this one
let _activeExactTag = null;    // whole tag picked in the Tags tab — Photos filter
let _activeNear = null;        // { lat, lng, label } — Photos with a location, nearest to this spot first
let _activeDates = null;       // { since, until, label, icon } — Photos taken in this time (a trip, a day)
let _peopleView = null;       // null = all people, { type: 'person', id } or { type: 'unknown' }
let _faceOverlayCleanup = null;
const _FACES_PAGE = 200;

// Every Photos filter off (search, tags, source, album, favorites, folder, person).
function _resetPhotoFilters() {
  _search = '';
  _activeTags = [];
  _activeModel = null;
  _activeAlbum = null;
  _favoritesOnly = false;
  _activeFolder = null;
  _activePeople = [];
  _activeExactTag = null;
  _activeSimilar = null;
  _activeNear = null;
  _activeDates = null;
  const searchInput = document.getElementById('gallery-search');
  const modelSel = document.getElementById('gallery-model-filter');
  const folderSel = document.getElementById('gallery-folder-filter');
  if (searchInput) searchInput.value = '';
  if (modelSel) modelSel.value = '';
  if (folderSel) folderSel.value = '';
}

// Show only the photos of one person / one tag. Other filters left over from
// browsing would hide some of them, so they are cleared first.
function _showPhotosFiltered(set) {
  _resetPhotoFilters();
  set();
  const modal = document.getElementById('gallery-modal');
  modal?.querySelector('.gallery-tab[data-tab="images"]')?.click();
  const loading = _fetchLibrary(false);
  _renderAlbums();
  return loading;
}

// Loads further pages into a grid (faces, tags) as the user scrolls to its end.
function _pagedGrid(c, grid, total, loaded, fetchPage, renderTiles) {
  let busy = false;
  const more = document.createElement('button');
  more.className = 'gallery-load-more';
  grid.after(more);
  const update = () => {
    more.style.display = loaded < total ? '' : 'none';
    more.textContent = `Show more (${total - loaded} left)`;
  };
  const next = async () => {
    if (busy || loaded >= total) return;
    busy = true;
    try {
      const faces = await fetchPage(loaded);
      if (!faces.length) { total = loaded; return; }
      grid.insertAdjacentHTML('beforeend', renderTiles(faces));
      loaded += faces.length;
    } catch (err) {
      uiModule.showError(err.message);
    } finally {
      busy = false;
      update();
    }
  };
  more.addEventListener('click', next);
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(entries => {
      if (!c.isConnected || !more.isConnected) { io.disconnect(); return; }
      if (entries.some(e => e.isIntersecting)) next();
    }, { rootMargin: '400px' });
    io.observe(more);
  }
  update();
  // A face taken out of the list shifts the server's pages by one.
  return { dropOne() { loaded = Math.max(0, loaded - 1); total = Math.max(0, total - 1); update(); } };
}

async function _jsonFetch(url, opts = {}) {
  const res = await fetch(`${API_BASE}${url}`, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function _postJson(url, body, method = 'POST') {
  return _jsonFetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

// Ask for a name and assign it to one face. Similar faces join automatically.
async function _nameFace(faceId, current) {
  const name = await uiModule.styledPrompt(
    'Who is this? Photos with the same person are recognised automatically.',
    { title: 'Name this person', defaultValue: current || '', placeholder: 'Name', maxLength: 80 },
  );
  if (!name || !String(name).trim()) return null;
  try {
    const data = await _postJson(`/api/gallery/faces/${encodeURIComponent(faceId)}`, { name: String(name).trim() });
    _forgetPeopleIndex();
    const more = data.regrouped?.matched || 0;
    uiModule.showToast?.(more ? `${String(name).trim()} — ${more} more face${more === 1 ? '' : 's'} recognised` : `Saved: ${String(name).trim()}`, 3500);
    return data;
  } catch (e) {
    uiModule.showError(e.message || 'Could not save the name');
    return null;
  }
}

function _setPersonFilter(person) {
  _showPhotosFiltered(() => { _activePeople = [person]; });
}

function _setExactTagFilter(tag) {
  _showPhotosFiltered(() => { _activeExactTag = tag; });
}

// ---- Search box: named people as type-ahead filters (several at once) ----

let _peopleIndex = null;          // named people [{ id, name, photo_count, cover_url }]
let _peopleIndexLoading = null;

function _forgetPeopleIndex() { _peopleIndex = null; }

function _namedPeople() {
  if (_peopleIndex) return Promise.resolve(_peopleIndex);
  if (!_peopleIndexLoading) {
    _peopleIndexLoading = _jsonFetch('/api/gallery/people')
      .then(d => (d.people || []).filter(p => p.name))
      .catch(() => [])
      .then(list => { _peopleIndex = list; _peopleIndexLoading = null; return list; });
  }
  return _peopleIndexLoading;
}

// "le" → Lena, "h" → Hans Müller and Anna Hofer: any word of the name starts with the text.
function _matchPeople(people, text, exclude) {
  const t = text.toLowerCase();
  const starts = (p) => p.name.toLowerCase().startsWith(t);
  return people
    .filter(p => !exclude.has(p.id) && p.name.toLowerCase().split(/[\s\-.]+/).some(w => w.startsWith(t)))
    .sort((a, b) => (starts(b) - starts(a)) || (b.photo_count - a.photo_count))
    .slice(0, 8);
}

function _wirePeopleSuggest(input) {
  const box = document.createElement('div');
  box.className = 'gallery-search-suggest';
  box.hidden = true;
  box.setAttribute('role', 'listbox');
  input.parentElement.appendChild(box);
  input.setAttribute('placeholder', 'Search photos, tags, people...');
  let matches = [];
  let active = -1;
  const hide = () => { box.hidden = true; matches = []; active = -1; };
  const paint = () => {
    box.innerHTML = `<div class="gallery-suggest-head">People</div>` + matches.map((p, i) => `
      <button type="button" role="option" class="gallery-suggest-item${i === active ? ' active' : ''}" data-i="${i}">
        <img src="${_esc(p.cover_url || '')}" alt="" />
        <span class="gallery-suggest-name">${_esc(p.name)}</span>
        <span class="gallery-suggest-count">${p.photo_count} photo${p.photo_count === 1 ? '' : 's'}</span>
      </button>`).join('');
    box.hidden = !matches.length;
  };
  const pick = (p) => {
    hide();
    clearTimeout(_searchDebounce);
    input.value = '';
    _search = '';
    if (!_activePeople.some(x => x.id === p.id)) _activePeople.push({ id: p.id, name: p.name });
    _fetchLibrary(false);
    _renderAlbums();
  };
  const update = async () => {
    const text = input.value.trim();
    if (!text) return hide();
    const people = await _namedPeople();
    if (input.value.trim() !== text) return;   // the user typed on meanwhile
    matches = _matchPeople(people, text, new Set(_activePeople.map(p => p.id)));
    active = matches.length ? 0 : -1;
    paint();
  };
  input.addEventListener('input', update);
  input.addEventListener('focus', update);
  input.addEventListener('blur', hide);
  input.addEventListener('keydown', (e) => {
    if (box.hidden || !matches.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length;
      paint();
    } else if ((e.key === 'Enter' || e.key === 'Tab') && active >= 0) {
      e.preventDefault();
      e.stopImmediatePropagation();   // a picked person, not a #tag / search
      pick(matches[active]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();            // close the list, not the gallery
      hide();
    }
  });
  // mousedown (not click) so the input keeps focus and blur doesn't close the list first
  box.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const item = e.target.closest('.gallery-suggest-item');
    if (item) pick(matches[Number(item.dataset.i)]);
  });
}

// Face boxes + "People in this photo" in the photo detail view.
async function _loadDetailFaces(img) {
  if (_faceOverlayCleanup) { _faceOverlayCleanup(); _faceOverlayCleanup = null; }
  const section = document.getElementById('gallery-detail-people-section');
  const list = document.getElementById('gallery-detail-people-list');
  const overlay = document.getElementById('gallery-detail-face-overlay');
  const imgEl = document.getElementById('gallery-detail-img');
  if (!overlay || !imgEl || imgEl.tagName !== 'IMG') return;
  let faces = [];
  try {
    faces = (await _jsonFetch(`/api/gallery/${encodeURIComponent(img.id)}/faces`)).faces || [];
  } catch (_) {
    faces = [];
  }
  if (document.getElementById('gallery-detail-img') !== imgEl) return;  // user moved on
  if (!faces.length) {
    if (section) section.style.display = 'none';
    overlay.innerHTML = '';
    return;
  }
  if (section && list) {
    section.style.display = '';
    list.innerHTML = faces.map(f => (f.person_name
      ? `<button class="gallery-ai-chip gallery-person-chip" data-person-id="${_esc(f.person_id)}" data-person-name="${_esc(f.person_name)}" title="All photos of ${_esc(f.person_name)}">${_esc(f.person_name)}</button>`
      : `<button class="gallery-ai-chip gallery-person-chip unknown" data-face-id="${_esc(f.id)}" title="Name this face">? Who is this</button>`)).join('');
    list.onclick = async (e) => {
      const chip = e.target.closest('.gallery-person-chip');
      if (!chip) return;
      if (chip.dataset.personId) {
        _setPersonFilter({ id: chip.dataset.personId, name: chip.dataset.personName });
      } else if (await _nameFace(chip.dataset.faceId, '')) {
        _loadDetailFaces(img);
      }
    };
  }
  const place = () => {
    const frame = overlay.parentElement;
    if (!frame || !imgEl.naturalWidth) return;
    const fr = frame.getBoundingClientRect();
    const ir = imgEl.getBoundingClientRect();
    const scale = Math.min(ir.width / imgEl.naturalWidth, ir.height / imgEl.naturalHeight);
    const dw = imgEl.naturalWidth * scale;
    const dh = imgEl.naturalHeight * scale;
    const left = ir.left - fr.left + (ir.width - dw) / 2;
    const top = ir.top - fr.top + (ir.height - dh) / 2;
    overlay.innerHTML = faces.map(f => `
      <div class="gallery-face-box${f.person_name ? '' : ' unknown'}" data-face-id="${_esc(f.id)}" data-name="${_esc(f.person_name || '')}"
           style="left:${left + f.box.x * dw}px;top:${top + f.box.y * dh}px;width:${f.box.w * dw}px;height:${f.box.h * dh}px"
           title="${f.person_name ? `${_esc(f.person_name)} — click to change` : 'Click to name this person'}">
        <span>${_esc(f.person_name || '?')}</span>
      </div>`).join('');
  };
  overlay.onclick = async (e) => {
    const box = e.target.closest('.gallery-face-box');
    if (box && await _nameFace(box.dataset.faceId, box.dataset.name)) _loadDetailFaces(img);
  };
  if (imgEl.complete) place(); else imgEl.addEventListener('load', place, { once: true });
  window.addEventListener('resize', place);
  _faceOverlayCleanup = () => window.removeEventListener('resize', place);
}

async function _renderPeopleTab() {
  const c = document.getElementById('gallery-people-container');
  if (!c) return;
  if (_peopleView?.type === 'person') return _renderPersonView(c, _peopleView.id);
  if (_peopleView?.type === 'unknown') return _renderUnknownFaces(c);
  let data;
  try {
    data = await _jsonFetch('/api/gallery/people');
  } catch (e) {
    c.innerHTML = `<div class="gallery-people-empty">${_esc(e.message)}</div>`;
    return;
  }
  const people = data.people || [];
  _peopleIndex = people.filter(p => p.name);   // fresh list for the search box too
  if (!people.length && !data.unknown_faces) {
    c.innerHTML = '<div class="gallery-people-empty">No faces found yet. In <strong>Settings → Library folders</strong>, tick <strong>People</strong> for a folder and click Scan now.</div>';
    return;
  }
  const named = people.filter(p => p.name).length;
  c.innerHTML = `
    <div class="gallery-people-head">
      <span class="gallery-people-summary">${named} named · ${people.length - named} not named yet</span>
      ${data.unknown_faces ? `<button class="memory-toolbar-btn" id="gallery-people-unknown">Unknown faces (${data.unknown_faces})</button>` : ''}
    </div>
    <div class="gallery-people-grid">${people.map(p => `
      <div class="gallery-person-card" data-id="${_esc(p.id)}" title="${p.name ? _esc(p.name) : 'Click to name'}">
        <img src="${_esc(p.cover_url || '')}" alt="" loading="lazy" />
        <div class="gallery-person-name">${p.name ? _esc(p.name) : '<span class="unnamed">Add a name</span>'}</div>
        <div class="gallery-person-count">${p.photo_count} photo${p.photo_count === 1 ? '' : 's'}</div>
      </div>`).join('')}
    </div>`;
  c.querySelector('#gallery-people-unknown')?.addEventListener('click', () => { _peopleView = { type: 'unknown' }; _renderPeopleTab(); });
  c.querySelectorAll('.gallery-person-card').forEach(card => card.addEventListener('click', () => {
    _peopleView = { type: 'person', id: card.dataset.id };
    _renderPeopleTab();
  }));
}

async function _renderPersonView(c, personId) {
  const url = (offset) => `/api/gallery/people/${encodeURIComponent(personId)}/faces?offset=${offset}&limit=${_FACES_PAGE}`;
  let data;
  try {
    data = await _jsonFetch(url(0));
  } catch (_) {
    _peopleView = null;
    return _renderPeopleTab();
  }
  const p = data.person;
  const label = p.name || 'this person';
  const tiles = (faces) => faces.map(f => `
      <div class="gallery-face-tile" data-face-id="${_esc(f.id)}" data-image-id="${_esc(f.image_id)}" title="Open the photo">
        <img src="${_esc(f.crop_url)}" alt="" loading="lazy" />
        <button class="gallery-face-remove" title="Not ${_esc(label)}">×</button>
        ${f.assigned === 'user' ? '' : '<span class="gallery-face-auto" title="Recognised automatically">auto</span>'}
      </div>`).join('');
  const photos = data.photo_count ?? data.faces.length;
  c.innerHTML = `
    <div class="gallery-people-head">
      <button class="gallery-detail-back" id="gallery-people-back">&larr; People</button>
      <input class="gallery-tag-input gallery-person-name-input" id="gallery-person-name" value="${_esc(p.name || '')}" placeholder="Who is this? (Enter to save)" />
      <button class="memory-toolbar-btn" id="gallery-person-photos">Show ${photos} photo${photos === 1 ? '' : 's'}</button>
      <button class="memory-toolbar-btn" id="gallery-person-forget" title="Forget this person — the faces go back to Unknown faces">Forget</button>
    </div>
    <div class="gallery-people-hint">Click × on any face that is not ${_esc(label)}. Giving two people the same name merges them.</div>
    <div class="gallery-faces-grid">${tiles(data.faces)}</div>`;
  const pager = _pagedGrid(c, c.querySelector('.gallery-faces-grid'), data.total ?? data.faces.length, data.faces.length,
    async (offset) => (await _jsonFetch(url(offset))).faces || [], tiles);
  c.querySelector('#gallery-people-back').addEventListener('click', () => { _peopleView = null; _renderPeopleTab(); });
  const input = c.querySelector('#gallery-person-name');
  input.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter' || !input.value.trim()) return;
    e.preventDefault();
    try {
      const res = await _postJson(`/api/gallery/people/${encodeURIComponent(p.id)}`, { name: input.value.trim() }, 'PATCH');
      _forgetPeopleIndex();
      const more = res.regrouped?.matched || 0;
      uiModule.showToast?.(res.merged_into ? `Merged into ${res.person.name}` : `Saved: ${res.person.name}${more ? ` — ${more} more faces recognised` : ''}`, 3500);
      _peopleView = { type: 'person', id: res.person.id };
      _renderPeopleTab();
    } catch (err) {
      uiModule.showError(err.message);
    }
  });
  c.querySelector('#gallery-person-photos').addEventListener('click', () => _setPersonFilter({ id: p.id, name: p.name || 'Unnamed person' }));
  c.querySelector('#gallery-person-forget').addEventListener('click', async () => {
    const ok = await uiModule.styledConfirm(`Forget ${label}? The faces go back to Unknown faces. Photos are not changed.`, { confirmText: 'Forget', danger: true });
    if (!ok) return;
    try {
      await _jsonFetch(`/api/gallery/people/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
      _forgetPeopleIndex();
      _peopleView = null;
      _renderPeopleTab();
    } catch (err) {
      uiModule.showError(err.message);
    }
  });
  c.querySelector('.gallery-faces-grid').addEventListener('click', async (e) => {
    const tile = e.target.closest('.gallery-face-tile');
    if (!tile) return;
    if (e.target.closest('.gallery-face-remove')) {
      try {
        await _postJson(`/api/gallery/faces/${encodeURIComponent(tile.dataset.faceId)}`, { person_id: null });
        tile.remove();
        pager.dropOne();
      } catch (err) {
        uiModule.showError(err.message);
      }
      return;
    }
    openGalleryImage(tile.dataset.imageId);
  });
}

async function _renderUnknownFaces(c) {
  const url = (offset) => `/api/gallery/faces/unknown?offset=${offset}&limit=${_FACES_PAGE}`;
  let data = { faces: [], total: 0 };
  try {
    data = await _jsonFetch(url(0));
  } catch (_) {}
  const faces = data.faces || [];
  const total = data.total ?? faces.length;
  const tiles = (list) => list.map(f => `
      <div class="gallery-face-tile" data-face-id="${_esc(f.id)}" title="Name this face">
        <img src="${_esc(f.crop_url)}" alt="" loading="lazy" />
        <button class="gallery-face-remove" title="Not a face">×</button>
      </div>`).join('');
  c.innerHTML = `
    <div class="gallery-people-head">
      <button class="gallery-detail-back" id="gallery-people-back">&larr; People</button>
      <span class="gallery-people-summary">${total} faces not recognised yet — click one to name it</span>
    </div>
    <div class="gallery-faces-grid">${tiles(faces)}</div>`;
  const pager = _pagedGrid(c, c.querySelector('.gallery-faces-grid'), total, faces.length,
    async (offset) => (await _jsonFetch(url(offset))).faces || [], tiles);
  c.querySelector('#gallery-people-back').addEventListener('click', () => { _peopleView = null; _renderPeopleTab(); });
  c.querySelector('.gallery-faces-grid').addEventListener('click', async (e) => {
    const tile = e.target.closest('.gallery-face-tile');
    if (!tile) return;
    if (e.target.closest('.gallery-face-remove')) {
      try {
        await _postJson(`/api/gallery/faces/${encodeURIComponent(tile.dataset.faceId)}`, { ignored: true });
        tile.remove();
        pager.dropOne();
      } catch (err) {
        uiModule.showError(err.message);
      }
      return;
    }
    if (await _nameFace(tile.dataset.faceId, '')) _renderUnknownFaces(c);
  });
}

// ---- Tags tab: every tag (yours and AI) with its photo count, most used first ----

const _TAGS_PAGE = 120;
let _tagsQuery = '';
let _tagsLoadSeq = 0;

function _renderTagsTab() {
  const c = document.getElementById('gallery-tags-container');
  if (!c) return;
  if (!c.querySelector('.gallery-tags-results')) {
    c.innerHTML = `
      <div class="gallery-people-head">
        <input type="text" class="gallery-search gallery-tags-filter" id="gallery-tags-filter" placeholder="Find a tag…" />
        <span class="gallery-people-summary" id="gallery-tags-summary"></span>
      </div>
      <div class="gallery-tags-results"></div>`;
    const input = c.querySelector('#gallery-tags-filter');
    input.value = _tagsQuery;
    let debounce;
    input.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => { _tagsQuery = input.value.trim(); _loadTags(c); }, 250);
    });
    c.querySelector('.gallery-tags-results').addEventListener('click', (e) => {
      const card = e.target.closest('.gallery-tag-card');
      if (card) _setExactTagFilter(card.dataset.tag);
    });
  }
  _loadTags(c);
}

async function _loadTags(c) {
  const seq = ++_tagsLoadSeq;
  const url = (offset) => `/api/gallery/tag-counts?offset=${offset}&limit=${_TAGS_PAGE}&q=${encodeURIComponent(_tagsQuery)}`;
  const results = c.querySelector('.gallery-tags-results');
  const summary = c.querySelector('#gallery-tags-summary');
  let data;
  try {
    data = await _jsonFetch(url(0));
  } catch (e) {
    if (seq === _tagsLoadSeq) results.innerHTML = `<div class="gallery-people-empty">${_esc(e.message)}</div>`;
    return;
  }
  if (seq !== _tagsLoadSeq) return;   // a newer search is already on its way
  const tags = data.tags || [];
  const total = data.total ?? tags.length;
  if (summary) summary.textContent = `${total.toLocaleString()} tag${total === 1 ? '' : 's'} · most used first`;
  if (!tags.length) {
    results.innerHTML = `<div class="gallery-people-empty">${_tagsQuery
      ? `No tag contains “${_esc(_tagsQuery)}”.`
      : 'No tags yet. Tag photos yourself, or turn on <strong>AI tags</strong> for a library folder in Settings.'}</div>`;
    return;
  }
  const cards = (list) => list.map(t => `
      <div class="gallery-tag-card" data-tag="${_esc(t.tag)}" title="Show the ${t.count} photo${t.count === 1 ? '' : 's'} tagged #${_esc(t.tag)}">
        <img src="${_esc(t.cover_url || '')}" alt="" loading="lazy" />
        <div class="gallery-tag-card-name">#${_esc(t.tag)}</div>
        <div class="gallery-person-count">${t.count.toLocaleString()} photo${t.count === 1 ? '' : 's'}</div>
      </div>`).join('');
  results.innerHTML = `<div class="gallery-tags-grid">${cards(tags)}</div>`;
  _pagedGrid(c, results.querySelector('.gallery-tags-grid'), total, tags.length,
    async (offset) => (seq === _tagsLoadSeq ? (await _jsonFetch(url(offset))).tags || [] : []), cards);
}

// ---- Map tab: where photos were taken, as bubbles sized by how many there are ----
//
// /api/gallery/map sends every spot with photos once; bubbles are worked out
// here for each zoom level, so zooming and panning need no further requests.
// The list beside the map is /api/gallery/library?near=…, nearest first.
// Leaflet is loaded on first use. Map pictures come from OpenStreetMap's tile
// server and place names from its Nominatim search (through our server).

const _LEAFLET_JS = '/static/lib/leaflet/leaflet.js';
const _LEAFLET_CSS = '/static/lib/leaflet/leaflet.css';
const _OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const _OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
const _BUBBLE_MERGE_PX = 50;     // spots closer than this on screen share one bubble
const _MAP_PAGE = 60;
const _MAP_REFRESH_MS = 2 * 60 * 1000;
let _leafletPromise = null;
let _mapPin = null;              // { lat, lng, label } — what the list measures from; kept while the app is open
let _mapView = null;             // { center, zoom } — where the map was, for the next visit
let _nearestSeq = 0;
const _MAP = {
  map: null, bubbles: null, pinMarker: null, hoverMarker: null, resize: null,
  points: null, located: 0, photos: 0, loadedAt: 0,
  clusters: new Map(),           // zoom → bubbles
  listedFor: null,               // pin the list was loaded for
  listed: new Map(),             // id → photo shown in the list
  pending: null,                 // { zoom } — fly to the pin once the map is up
};

// In the reader's number format: "3,4 km" and "4.227 km" in German.
function _formatDistance(km) {
  if (km < 1) return `${Math.round(km * 1000)} m`;
  if (km < 10) return `${km.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} km`;
  return `${Math.round(km).toLocaleString()} km`;
}

function _loadAsset(kind, url) {
  return new Promise((resolve, reject) => {
    const el = document.createElement(kind);
    if (kind === 'link') { el.rel = 'stylesheet'; el.href = url; } else { el.src = url; }
    el.addEventListener('load', () => resolve(), { once: true });
    el.addEventListener('error', () => reject(new Error(`Could not load ${url}`)), { once: true });
    document.head.appendChild(el);
  });
}

function _ensureLeaflet() {
  if (window.L?.map) return Promise.resolve(window.L);
  return (_leafletPromise ??= Promise.all([_loadAsset('link', _LEAFLET_CSS), _loadAsset('script', _LEAFLET_JS)])
    .then(() => {
      if (!window.L?.map) throw new Error('The map library did not load');
      return window.L;
    })
    .catch((err) => { _leafletPromise = null; throw err; }));
}

function _destroyMap() {
  _MAP.resize?.disconnect();
  _MAP.map?.remove();
  Object.assign(_MAP, { map: null, bubbles: null, pinMarker: null, hoverMarker: null, resize: null,
    points: null, loadedAt: 0, clusters: new Map(), listedFor: null, listed: new Map(), pending: null });
}

// "Show on map" in a photo's details.
function _showOnMap(spot) {
  _mapPin = spot;
  _MAP.pending = { zoom: 16 };
  document.querySelector('#gallery-modal .gallery-tab[data-tab="map"]')?.click();
}

// OpenStreetMap's pictures are light; on a dark theme they are dimmed to match.
function _themeIsDark() {
  const m = getComputedStyle(document.body).backgroundColor.match(/\d+(\.\d+)?/g);
  if (!m || m.length < 3) return true;
  const [r, g, b] = m.map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.45;
}

function _renderMapTab() {
  const c = document.getElementById('gallery-map-container');
  if (!c) return;
  if (!c.querySelector('.gallery-map-layout')) {
    c.innerHTML = `
      <div class="gallery-map-layout">
        <div class="gallery-map-main">
          <div id="gallery-map" class="gallery-map" role="application" aria-label="Map of where your photos were taken"></div>
          <form class="gallery-map-search" id="gallery-map-search-form" role="search" autocomplete="off">
            <input type="search" class="gallery-search" id="gallery-map-search" placeholder="Search a place or address…" aria-label="Search a place" maxlength="160" />
            <div class="gallery-search-suggest gallery-map-places" id="gallery-map-places" hidden></div>
          </form>
          <div class="gallery-map-note" id="gallery-map-note" hidden></div>
        </div>
        <aside class="gallery-map-side" id="gallery-map-side" aria-live="polite"></aside>
      </div>`;
    _wireMapSearch(c);
    c.querySelector('#gallery-map-side').addEventListener('click', _onMapSideClick);
    c.querySelector('#gallery-map-side').addEventListener('mouseover', (e) => _hoverMapPhoto(e.target.closest('.gallery-map-photo')));
    c.querySelector('#gallery-map-side').addEventListener('mouseleave', () => _hoverMapPhoto(null));
  }
  c.querySelector('.gallery-map').classList.toggle('gallery-map-dark', _themeIsDark());
  _startMap(c);
}

async function _startMap(c) {
  const note = c.querySelector('#gallery-map-note');
  let L;
  try {
    L = await _ensureLeaflet();
  } catch (e) {
    note.hidden = false;
    note.textContent = `${e.message}. Reload the page and try again.`;
    return;
  }
  if (!c.isConnected) return;
  const firstTime = !_MAP.map;
  if (firstTime) _createMap(L, c.querySelector('#gallery-map'));
  else _MAP.map.invalidateSize();
  await _loadMapPoints(note);
  if (!c.isConnected || !_MAP.map) return;
  const map = _MAP.map;
  if (_mapPin) _placePin();
  const pending = _MAP.pending;
  _MAP.pending = null;
  if (pending && _mapPin) {
    map.setView([_mapPin.lat, _mapPin.lng], Math.max(map.getZoom(), pending.zoom));
  } else if (firstTime) {
    if (_mapView) map.setView(_mapView.center, _mapView.zoom);
    else if (_MAP.points?.length) {
      const lats = _MAP.points.map(p => p[0]), lngs = _MAP.points.map(p => p[1]);
      map.fitBounds([[Math.min(...lats), Math.min(...lngs)], [Math.max(...lats), Math.max(...lngs)]], { padding: [30, 30], maxZoom: 12 });
    } else map.setView([30, 10], 2);
  }
  _drawBubbles();
  _renderNearest();
}

function _createMap(L, el) {
  const map = L.map(el, { minZoom: 2, maxZoom: 19, worldCopyJump: true });
  // Only the site's origin goes along (the app sends no Referer at all
  // otherwise), which OpenStreetMap's tile policy asks for.
  L.tileLayer(_OSM_TILES, { maxZoom: 19, attribution: _OSM_ATTRIBUTION, referrerPolicy: 'strict-origin-when-cross-origin' }).addTo(map);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
  _MAP.map = map;
  _MAP.bubbles = L.layerGroup().addTo(map);
  map.on('moveend', () => {
    _mapView = { center: map.getCenter(), zoom: map.getZoom() };
    _drawBubbles();
  });
  map.on('click', (e) => {
    document.getElementById('gallery-map-places')?.setAttribute('hidden', '');
    const at = e.latlng.wrap();
    _setMapPin({ lat: at.lat, lng: at.lng, label: 'the marked spot' });
  });
  // The gallery window can be resized, tiled or maximised.
  if ('ResizeObserver' in window) {
    _MAP.resize = new ResizeObserver(() => _MAP.map?.invalidateSize({ debounceMoveend: true }));
    _MAP.resize.observe(el);
  }
}

async function _loadMapPoints(note) {
  if (_MAP.points && Date.now() - _MAP.loadedAt < _MAP_REFRESH_MS) return;
  let data;
  try {
    data = await _jsonFetch('/api/gallery/map');
  } catch (e) {
    note.hidden = false;
    note.textContent = `Could not load photo locations: ${e.message}`;
    return;
  }
  // Busiest spots first, so they anchor the bubbles around them.
  _MAP.points = (data.points || []).sort((a, b) => b[2] - a[2]);
  _MAP.located = data.located || 0;
  _MAP.photos = data.photos || 0;
  _MAP.loadedAt = Date.now();
  _MAP.clusters = new Map();
  note.hidden = _MAP.located > 0;
  note.innerHTML = _MAP.located ? '' : 'None of your photos has a location yet. Phones and GPS cameras store where a photo was taken; Odysseus reads it when the photo is uploaded or its library folder is scanned.';
}

// Spots that would overlap on screen at this zoom become one bubble, placed
// at their photos' centre. Worked out once per zoom level.
function _clustersAt(zoom) {
  const cached = _MAP.clusters.get(zoom);
  if (cached) return cached;
  const R = _BUBBLE_MERGE_PX;
  const grid = new Map();
  const out = [];
  for (const [lat, lng, n] of _MAP.points || []) {
    const p = _MAP.map.project([lat, lng], zoom);
    const gx = Math.floor(p.x / R), gy = Math.floor(p.y / R);
    let best = null, bestD = R * R;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (const c of grid.get(`${gx + dx},${gy + dy}`) || []) {
          const d = (c.x - p.x) ** 2 + (c.y - p.y) ** 2;
          if (d < bestD) { bestD = d; best = c; }
        }
      }
    }
    if (!best) {
      best = { x: p.x, y: p.y, n: 0, spots: 0, sx: 0, sy: 0, south: lat, north: lat, west: lng, east: lng };
      const key = `${gx},${gy}`;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(best);
      out.push(best);
    }
    best.n += n;
    best.spots += 1;
    best.sx += p.x * n;
    best.sy += p.y * n;
    best.south = Math.min(best.south, lat); best.north = Math.max(best.north, lat);
    best.west = Math.min(best.west, lng); best.east = Math.max(best.east, lng);
  }
  for (const c of out) c.center = _MAP.map.unproject([c.sx / c.n, c.sy / c.n], zoom);
  _MAP.clusters.set(zoom, out);
  return out;
}

// More photos, bigger bubble: 16 px for one photo, 72 px for 10,000.
function _bubbleSize(n) { return Math.round(2 * (8 + 7 * Math.log10(n))); }

function _bubbleLabel(n) {
  if (n < 1000) return String(n);
  if (n < 10000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${Math.round(n / 1000)}k`;
}

function _drawBubbles() {
  const { map, bubbles } = _MAP;
  if (!map || !bubbles || !_MAP.points) return;
  const L = window.L;
  const view = map.getBounds().pad(0.3);
  bubbles.clearLayers();
  for (const c of _clustersAt(map.getZoom())) {
    if (!view.contains(c.center)) continue;
    const size = _bubbleSize(c.n);
    const marker = L.marker(c.center, {
      icon: L.divIcon({
        className: 'gallery-map-bubble-icon',
        html: `<span class="gallery-map-bubble" style="width:${size}px;height:${size}px;font-size:${Math.max(10, Math.round(size * 0.3))}px">${_bubbleLabel(c.n)}</span>`,
        iconSize: [size, size],
      }),
      title: `${c.n.toLocaleString()} photo${c.n === 1 ? '' : 's'}`,
      zIndexOffset: -Math.min(c.n, 100000),   // smaller bubbles stay clickable on top of big ones
      riseOnHover: true,
    });
    marker.on('click', () => _openBubble(c));
    bubbles.addLayer(marker);
  }
}

// A bubble that zooming in would split: zoom in. Either way the list shows
// the photos nearest to its centre.
function _openBubble(c) {
  const map = _MAP.map;
  const bounds = window.L.latLngBounds([c.south, c.west], [c.north, c.east]);
  const fits = Math.min(map.getBoundsZoom(bounds, false, window.L.point(80, 80)), map.getMaxZoom());
  if (c.spots > 1 && fits > map.getZoom()) map.flyToBounds(bounds, { padding: [40, 40], maxZoom: fits, duration: 0.6 });
  _setMapPin({ lat: c.center.lat, lng: c.center.lng, label: 'the marked spot' });
}

function _setMapPin(spot, view = {}) {
  _mapPin = spot;
  const map = _MAP.map;
  if (map) {
    _placePin();
    if (view.bbox) {
      const [south, north, west, east] = view.bbox;
      map.flyToBounds([[south, west], [north, east]], { maxZoom: 15, duration: 0.8 });
    } else if (view.zoom) {
      map.flyTo([spot.lat, spot.lng], Math.max(map.getZoom(), view.zoom), { duration: 0.8 });
    }
  }
  _renderNearest();
}

function _placePin() {
  const L = window.L;
  if (!_MAP.map || !_mapPin || !L) return;
  const at = [_mapPin.lat, _mapPin.lng];
  if (_MAP.pinMarker) { _MAP.pinMarker.setLatLng(at); return; }
  _MAP.pinMarker = L.marker(at, {
    icon: L.divIcon({
      className: 'gallery-map-pin-icon',
      html: '<svg width="28" height="36" viewBox="0 0 28 36" aria-hidden="true"><path d="M14 35s12-11.3 12-21A12 12 0 0 0 2 14c0 9.7 12 21 12 21z"/><circle cx="14" cy="14" r="4.5"/></svg>',
      iconSize: [28, 36],
      iconAnchor: [14, 35],
    }),
    interactive: false,
    keyboard: false,
    zIndexOffset: 200000,
  }).addTo(_MAP.map);
}

function _clearMapPin() {
  _mapPin = null;
  _MAP.pinMarker?.remove();
  _MAP.pinMarker = null;
  _renderNearest();
}

function _hoverMapPhoto(tile) {
  const L = window.L;
  _MAP.hoverMarker?.remove();
  _MAP.hoverMarker = null;
  const img = tile && _MAP.listed.get(tile.dataset.id);
  if (!img?.gps || !_MAP.map || !L) return;
  _MAP.hoverMarker = L.circleMarker([Number(img.gps.lat), Number(img.gps.lng)], {
    radius: 9, weight: 3, className: 'gallery-map-hover', interactive: false,
  }).addTo(_MAP.map);
}

// The list beside the map: every photo with a location, nearest to the pin first.
async function _renderNearest() {
  const side = document.getElementById('gallery-map-side');
  if (!side) return;
  const key = _mapPin ? `${_mapPin.lat},${_mapPin.lng}` : '';
  if (key === _MAP.listedFor && side.childElementCount) return;
  _MAP.listedFor = key;
  // A place typed in the search box stays only while the list is for that place.
  const box = document.getElementById('gallery-map-search');
  if (box && box.value.trim() !== (_mapPin?.label || '')) box.value = '';
  _MAP.listed = new Map();
  _hoverMapPhoto(null);
  const seq = ++_nearestSeq;
  const counts = _MAP.located
    ? `${_MAP.located.toLocaleString()} of ${_MAP.photos.toLocaleString()} photos have a location.`
    : '';
  if (!_mapPin) {
    side.innerHTML = `<div class="gallery-map-side-empty">
        <strong>Find photos by place</strong>
        <p>Search for a place, or click a bubble or anywhere on the map. The photos taken nearest to it are listed here, closest first.</p>
        <p class="gallery-map-side-count">${counts}</p>
      </div>`;
    return;
  }
  const pin = _mapPin;
  const url = (offset) => `/api/gallery/library?near=${encodeURIComponent(`${pin.lat},${pin.lng}`)}&offset=${offset}&limit=${_MAP_PAGE}`;
  side.innerHTML = `
    <div class="gallery-map-side-head">
      <div class="gallery-map-side-title">Nearest to <strong>${_esc(pin.label)}</strong></div>
      <button class="gallery-map-side-clear" data-map-action="clear" title="Remove the pin" aria-label="Remove the pin">×</button>
      <div class="gallery-map-side-sub">${pin.lat.toFixed(5)}, ${pin.lng.toFixed(5)}</div>
      <button class="memory-toolbar-btn gallery-map-side-open" data-map-action="photos" title="Show this list in Photos, where you can select, download and browse them">Open in Photos</button>
    </div>
    <div class="gallery-map-photos"><div class="gallery-people-empty">Loading…</div></div>`;
  let data;
  try {
    data = await _jsonFetch(url(0));
  } catch (e) {
    if (seq === _nearestSeq) side.querySelector('.gallery-map-photos').innerHTML = `<div class="gallery-people-empty">${_esc(e.message)}</div>`;
    return;
  }
  if (seq !== _nearestSeq) return;   // the pin moved on while this was loading
  const grid = side.querySelector('.gallery-map-photos');
  const items = data.items || [];
  if (!items.length) {
    grid.innerHTML = '<div class="gallery-people-empty">None of your photos has a location yet.</div>';
    return;
  }
  const tiles = (list) => list.map((img) => {
    _MAP.listed.set(img.id, img);
    const name = (img.source_path || img.prompt || img.filename || '').split('/').pop();
    const when = img.taken_at ? new Date(img.taken_at).toLocaleDateString() : '';
    return `
      <button class="gallery-map-photo" data-id="${_esc(img.id)}" title="${_esc([name, when].filter(Boolean).join(' · '))}">
        <img src="${_esc(img.thumb_url || img.url)}" alt="" loading="lazy" />
        <span class="gallery-map-photo-distance">${_esc(_formatDistance(img.distance_km ?? 0))}</span>
      </button>`;
  }).join('');
  grid.innerHTML = tiles(items);
  const sub = side.querySelector('.gallery-map-side-sub');
  if (sub) sub.textContent += ` · ${(data.total || 0).toLocaleString()} photos, closest first`;
  _pagedGrid(side, grid, data.total || items.length, items.length,
    async (offset) => (seq === _nearestSeq ? (await _jsonFetch(url(offset))).items || [] : []), tiles);
}

async function _onMapSideClick(e) {
  const action = e.target.closest('[data-map-action]')?.dataset.mapAction;
  if (action === 'clear') return _clearMapPin();
  if (action === 'photos' && _mapPin) {
    const spot = { ..._mapPin };
    return _showPhotosFiltered(() => { _activeNear = spot; });
  }
  const tile = e.target.closest('.gallery-map-photo');
  const img = tile && _MAP.listed.get(tile.dataset.id);
  if (!img || !_mapPin) return;
  // Open it in Photos with the same nearest-first list, so ← → keep walking outwards.
  const spot = { ..._mapPin };
  await _showPhotosFiltered(() => { _activeNear = spot; });
  _openDetail(_items.find(i => i.id === img.id) || img);
}

function _wireMapSearch(c) {
  const form = c.querySelector('#gallery-map-search-form');
  const input = c.querySelector('#gallery-map-search');
  const list = c.querySelector('#gallery-map-places');
  let places = [];
  let active = 0;
  let searched = '';
  let seq = 0;

  const pick = (place) => {
    list.hidden = true;
    input.value = place.name;
    _setMapPin({ lat: place.lat, lng: place.lng, label: place.name },
      place.bbox ? { bbox: place.bbox } : { zoom: 15 });
  };
  const show = () => {
    list.innerHTML = places.map((p, i) => `
      <button type="button" class="gallery-suggest-item${i === active ? ' active' : ''}" data-i="${i}">
        <span class="gallery-suggest-name">${_esc(p.name)}</span>
        <span class="gallery-suggest-count gallery-map-place-label">${_esc(p.label === p.name ? '' : p.label)}</span>
      </button>`).join('') + '<div class="gallery-suggest-head gallery-map-places-credit">Search by OpenStreetMap Nominatim</div>';
    list.hidden = false;
  };
  const search = async (text) => {
    const mine = ++seq;
    searched = text;
    list.innerHTML = '<div class="gallery-suggest-head">Searching…</div>';
    list.hidden = false;
    try {
      const data = await _jsonFetch(`/api/gallery/places?q=${encodeURIComponent(text)}`);
      if (mine !== seq) return;
      places = data.places || [];
      active = 0;
      if (places.length === 1) return pick(places[0]);
      if (!places.length) {
        list.innerHTML = `<div class="gallery-suggest-head">No place called “${_esc(text)}” found</div>`;
        return;
      }
      show();
    } catch (err) {
      if (mine === seq) list.innerHTML = `<div class="gallery-suggest-head">${_esc(err.message)}</div>`;
    }
  };

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    // Enter on a list that is already up picks the highlighted place.
    if (!list.hidden && places.length && text === searched) return pick(places[active]);
    search(text);
  });
  input.addEventListener('keydown', (e) => {
    if (list.hidden || !places.length || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
    e.preventDefault();
    active = (active + (e.key === 'ArrowDown' ? 1 : places.length - 1)) % places.length;
    show();
  });
  input.addEventListener('input', () => { if (!input.value.trim()) list.hidden = true; });
  list.addEventListener('mousedown', (e) => e.preventDefault());   // keep focus in the box
  list.addEventListener('click', (e) => {
    const item = e.target.closest('.gallery-suggest-item');
    if (item) pick(places[Number(item.dataset.i)]);
  });
  c.addEventListener('click', (e) => { if (!e.target.closest('.gallery-map-search')) list.hidden = true; });
}

// ---- Trips tab and "On this day" ----
// The server finds trips in where photos were taken (and, before phones
// recorded that, in folder albums spanning a few weeks). Opening a trip or a
// day shows Photos taken in that time: the `since` / `until` filter.

let _tripsData = null;        // { trips } — kept while the gallery is open, refreshed on each visit
let _tripsSeq = 0;
let _tripsShowVisits = false;
let _memories = null;         // { date, years } — "On this day" for _memories.date

function _localIsoDate(d = new Date()) {
  const two = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
}

// The server sends local times with microseconds; Date only parses up to seconds everywhere.
function _localDate(iso) { return new Date(String(iso).slice(0, 19)); }

let _regionNames = null;
function _countryName(code) {
  try {
    _regionNames = _regionNames || new Intl.DisplayNames([navigator.language || 'en'], { type: 'region' });
    return _regionNames.of(code) || code;
  } catch (_) { return code; }
}

function _tripTitle(t) {
  if (t.album) return t.album.name;
  const abroad = (t.countries || []).filter(c => c && c !== t.home_country);
  if (abroad.length) return abroad.map(_countryName).join(' & ');
  return t.main_place || 'Trip';
}

// "12–18 Aug 2025". `until` is exclusive, so the last day is the moment before it.
function _dateRangeText(since, until) {
  const first = _localDate(since);
  const last = new Date(_localDate(until).getTime() - 1);
  const fmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  try { return fmt.formatRange(first, last); } catch (_) { return `${fmt.format(first)} – ${fmt.format(last)}`; }
}

function _plural(n, word) { return `${Number(n).toLocaleString()} ${word}${n === 1 ? '' : 's'}`; }

// Trips and days read best in the order they happened.
function _setChronological() {
  _sort = 'taken_asc';
  const sel = document.getElementById('gallery-sort');
  if (sel) sel.value = _sort;
}

function _openTrip(t) {
  const label = _tripTitle(t);
  return _showPhotosFiltered(() => {
    _setChronological();
    if (t.kind === 'album' && t.album) _activeAlbum = t.album.id;
    else _activeDates = { since: t.start, until: t.end, label, icon: '🧳' };
  });
}

function _tripsShown() {
  const all = _tripsData?.trips || [];
  return _tripsShowVisits ? all : all.filter(t => !t.regular);
}

function _tripCard(t, index) {
  const covers = t.covers || [];
  // The first photo of a trip is often the airport; lead with one from the middle.
  const pics = covers.length >= 4 ? covers.slice(1, 4) : covers.slice(0, 3);
  const meta = [_plural(t.photos, 'photo')];
  if (t.far_km) meta.push(`${_formatDistance(t.far_km)} from home`);
  return `
    <button type="button" class="gallery-trip-card" data-trip="${index}" title="Show the photos of this trip">
      <span class="gallery-trip-covers gallery-trip-covers-${pics.length}">${pics.map(u => `<img src="${_esc(u)}" alt="" loading="lazy">`).join('')}</span>
      <span class="gallery-trip-info">
        <span class="gallery-trip-title">${_esc(_tripTitle(t))}</span>
        <span class="gallery-trip-when">${_esc(_dateRangeText(t.start, t.end))} · ${_plural(t.days, 'day')}</span>
        ${t.places?.length ? `<span class="gallery-trip-places">${t.places.map(_esc).join(' · ')}</span>` : ''}
        <span class="gallery-trip-meta">${_esc(meta.join(' · '))}</span>
      </span>
    </button>`;
}

function _drawTrips(c) {
  const all = _tripsData?.trips || [];
  if (!all.length) {
    c.innerHTML = `<div class="gallery-people-empty">No trips yet. A trip is a few days with photos taken more than 50 km from home, so photos need a location — or a library folder album of a few weeks.</div>`;
    return;
  }
  const visits = all.filter(t => t.regular);
  const shown = _tripsShown();
  const often = [...new Set(visits.map(t => t.main_place))].slice(0, 3).join(', ');
  let html = `
    <div class="gallery-people-head">
      <span class="gallery-people-summary">${_plural(shown.length, 'trip')}</span>
      ${visits.length ? `<label class="gallery-trips-visits" title="Places you go to again and again, like ${_esc(often)}">
        <input type="checkbox" id="gallery-trips-visits"${_tripsShowVisits ? ' checked' : ''}> Also show ${_plural(visits.length, 'visit')} to places you go often</label>` : ''}
    </div>
    <p class="gallery-people-hint">Found from where your photos were taken; before that, from library folder albums that span a few weeks.</p>`;
  let year = null;
  shown.forEach((t, i) => {
    const y = String(t.start).slice(0, 4);
    if (y !== year) {
      if (year !== null) html += '</div>';
      year = y;
      html += `<h3 class="gallery-trips-year">${_esc(y)}</h3><div class="gallery-trips-grid">`;
    }
    html += _tripCard(t, i);
  });
  c.innerHTML = html + (year !== null ? '</div>' : '');
}

async function _renderTripsTab() {
  const c = document.getElementById('gallery-trips-container');
  if (!c) return;
  if (!c.dataset.wired) {
    c.dataset.wired = '1';
    c.addEventListener('click', (e) => {
      const card = e.target.closest('.gallery-trip-card');
      const trip = card && _tripsShown()[Number(card.dataset.trip)];
      if (trip) _openTrip(trip);
    });
    c.addEventListener('change', (e) => {
      if (e.target.id !== 'gallery-trips-visits') return;
      _tripsShowVisits = e.target.checked;
      _drawTrips(c);
    });
  }
  if (_tripsData) _drawTrips(c);
  else c.innerHTML = '<div class="gallery-people-empty">Looking for trips…</div>';
  const seq = ++_tripsSeq;
  try {
    const data = await _jsonFetch('/api/gallery/trips');
    if (seq !== _tripsSeq) return;
    const changed = JSON.stringify(data) !== JSON.stringify(_tripsData);
    _tripsData = data;
    if (changed) _drawTrips(c);
  } catch (err) {
    if (seq === _tripsSeq && !_tripsData) {
      c.innerHTML = `<div class="gallery-people-empty">Could not load trips: ${_esc(err.message)}</div>`;
    }
  }
}

function _memoriesHiddenToday() {
  try { return localStorage.getItem('gallery-memories-hidden') === _localIsoDate(); } catch (_) { return false; }
}

async function _loadMemories() {
  const today = _localIsoDate();
  if (_memories && _memories.date !== today) _memories = null;
  _renderMemories();
  try {
    _memories = await _jsonFetch(`/api/gallery/on-this-day?date=${today}`);
  } catch (_) {
    return;   // a nice extra; the gallery works without it
  }
  _renderMemories();
}

// The strip above the grid: one card per earlier year with photos from today's date.
function _renderMemories() {
  const el = document.getElementById('gallery-memories');
  if (!el) return;
  const years = _memories?.years || [];
  const filtered = _search || _activeTags.length || _activeModel || _activeAlbum || _favoritesOnly || _activeFolder
    || _activePeople.length || _activeExactTag || _activeSimilar || _activeNear || _activeDates;
  if (!years.length || filtered || _memoriesHiddenToday()) {
    el.hidden = true;
    return;
  }
  const key = `${_memories.date}|${years.map(y => `${y.year}:${y.count}:${y.items[0]?.id || ''}`).join(',')}`;
  if (el.dataset.key !== key) {
    el.dataset.key = key;
    const day = new Date(`${_memories.date}T12:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'long' });
    el.innerHTML = `
      <div class="gallery-memories-head">
        <span class="gallery-memories-title">On this day</span>
        <span class="gallery-memories-date">${_esc(day)}</span>
        <button type="button" class="gallery-memories-close" title="Hide until tomorrow" aria-label="Hide On this day until tomorrow">&times;</button>
      </div>
      <div class="gallery-memories-row">${years.map(y => {
        const cover = y.items[Math.floor(y.items.length / 2)];
        return `
          <button type="button" class="gallery-memory-card" data-year="${y.year}" title="Show the photos taken on ${_esc(day)} ${y.year}">
            ${cover ? `<img src="${_esc(cover.thumb_url || cover.url)}" alt="" loading="lazy">` : ''}
            <span class="gallery-memory-label">
              <strong>${_plural(y.years_ago, 'year')} ago</strong>
              <span>${y.year} · ${_plural(y.count, 'photo')}</span>
            </span>
          </button>`;
      }).join('')}</div>`;
  }
  if (!el.dataset.wired) {
    el.dataset.wired = '1';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.gallery-memories-close')) {
        try { localStorage.setItem('gallery-memories-hidden', _localIsoDate()); } catch (_) {}
        el.hidden = true;
        return;
      }
      const card = e.target.closest('.gallery-memory-card');
      if (!card || !_memories) return;
      const [, month, dayOfMonth] = _memories.date.split('-').map(Number);
      const year = Number(card.dataset.year);
      const first = new Date(year, month - 1, dayOfMonth);
      const next = new Date(year, month - 1, dayOfMonth + 1);
      _showPhotosFiltered(() => {
        _setChronological();
        _activeDates = {
          since: _localIsoDate(first),
          until: _localIsoDate(next),
          label: first.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }),
          icon: '📅',
        };
      });
    });
  }
  el.hidden = false;
}

function _renderAlbums() {
  const container = document.getElementById('gallery-album-chips');   // above search: active-filter indicators
  const filterC = document.getElementById('gallery-filter-chips');    // below search: All / Favorites
  if (!container && !filterC) return;
  // Below the search bar: the All / Favorites filters PLUS any active tag chips
  // (so a tag you searched/clicked sits right next to All and the heart).
  if (filterC) {
    // Order: All, then the heart, then any active tag chips (to the right of
    // both), then the active-album chip. No favorites-within-an-album view, so
    // the heart is hidden while an album is active.
    let fhtml = `<button class="gallery-chip${!_activeAlbum && !_favoritesOnly ? ' active' : ''}" data-album="">All</button>`;
    if (!_activeAlbum) {
      fhtml += `<button class="gallery-chip gallery-chip-fav${_favoritesOnly ? ' active' : ''}" data-fav="true" title="Favorites">&#9829;</button>`;
    }
    if (_search) fhtml += _filterPill('Search', `${_smart ? '✨ ' : ''}Search: ${_search}`, 'data-clear="search"');
    if (_activeFolder) fhtml += _filterPill('Folder', _activeFolder.label || _activeFolder.path, 'data-clear="folder"');
    _activePeople.forEach(p => { fhtml += _filterPill('Person', p.name, `data-clear-person="${_esc(p.id)}"`); });
    if (_activeSimilar) fhtml += _filterPill('Similar', `Similar to ${_activeSimilar.label}`, 'data-clear="similar"');
    if (_activeNear) fhtml += _filterPill('Near', `📍 Nearest to ${_activeNear.label}`, 'data-clear="near"');
    if (_activeDates) fhtml += _filterPill('Taken', `${_activeDates.icon || '📅'} ${_activeDates.label}`, 'data-clear="dates"');
    if (_activeExactTag) fhtml += _filterPill('Tag', `#${_activeExactTag}`, 'data-clear="exact-tag"');
    if (_activeModel) fhtml += _filterPill('Source', _activeModel, 'data-clear="model"');
    _activeTags.forEach(t => {
      fhtml += _filterPill('Tag', `#${t}`, `data-clear-tag="${_esc(t)}"`);
    });
    if (_activeAlbum) {
      const a = _albums.find(x => x.id === _activeAlbum);
      if (a) {
        fhtml += _filterPill('Album', a.name, 'data-clear="album"');
      }
    }
    if (_search || _activeTags.length || _activeModel || _activeAlbum || _favoritesOnly || _activeFolder || _activePeople.length || _activeExactTag || _activeSimilar || _activeNear || _activeDates) {
      fhtml += `<button class="gallery-chip gallery-clear-all-filters" data-clear="all" title="Clear every gallery filter">Clear all</button>`;
    }
    filterC.innerHTML = fhtml;
    filterC.querySelector('.gallery-chip[data-album=""]')?.addEventListener('click', () => {
      _favoritesOnly = false;
      _activeAlbum = null;
      _activeTags = [];
      _activeExactTag = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-fav')?.addEventListener('click', () => {
      _favoritesOnly = !_favoritesOnly;
      _activeAlbum = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="album"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeAlbum = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="search"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _search = '';
      const searchInput = document.getElementById('gallery-search');
      if (searchInput) searchInput.value = '';
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelectorAll('.gallery-chip-clear[data-clear-person]').forEach(x => {
      x.addEventListener('click', (e) => {
        e.stopPropagation();
        _activePeople = _activePeople.filter(p => p.id !== x.dataset.clearPerson);
        _fetchLibrary(false);
        _renderAlbums();
      });
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="similar"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeSimilar = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="exact-tag"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeExactTag = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="near"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeNear = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="dates"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeDates = null;
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="folder"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _setFolderFilter('');
    });
    filterC.querySelector('.gallery-chip-clear[data-clear="model"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _activeModel = null;
      const modelSel = document.getElementById('gallery-model-filter');
      if (modelSel) modelSel.value = '';
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelector('.gallery-clear-all-filters')?.addEventListener('click', (e) => {
      e.stopPropagation();
      _resetPhotoFilters();
      _fetchLibrary(false);
      _renderAlbums();
    });
    filterC.querySelectorAll('.gallery-chip-clear[data-clear-tag]').forEach(x => {
      x.addEventListener('click', (e) => {
        e.stopPropagation();
        const tag = x.dataset.clearTag;
        _activeTags = _activeTags.filter(t => t !== tag);
        _fetchLibrary(false);
        _renderAlbums();
      });
    });
  }
  // The above-search row is no longer used — all filter chips live below now.
  if (container) container.innerHTML = '';
  _syncSortSelect();
  _renderMemories();
}

// "Nearest to …" orders by distance, so the sort menu shows that and waits.
function _syncSortSelect() {
  const sel = document.getElementById('gallery-sort');
  if (!sel) return;
  sel.disabled = !!_activeNear;
  sel.title = _activeNear ? 'Sorted by distance while “Nearest to …” is on' : '';
  sel.value = _activeNear ? 'nearest' : _sort;
}

// Albums tab — renders the album list as a grid of cover-thumbnailed cards.
// Clicking an album switches to the Photos tab filtered by that album.
//
// Structure mirrors the Photos tab: persistent toolbar (search + Select)
// and bulk bar built once, only the inner #gallery-albums-grid-wrap
// re-renders so the search input keeps focus while typing.
function _renderAlbumsTab() {
  const container = document.getElementById('gallery-albums-container');
  if (!container) return;
  _ensureAlbumsToolbar(container);
  _renderAlbumsGrid();
}

function _filteredAlbums() {
  const q = _albumSearch.trim().toLowerCase();
  if (!q) return _albums;
  return _albums.filter(a => (a.name || '').toLowerCase().includes(q));
}

function _ensureAlbumsToolbar(container) {
  if (container.querySelector('#gallery-albums-toolbar')) return;
  container.innerHTML = `
    <div class="gallery-toolbar" id="gallery-albums-toolbar">
      <div class="gallery-search-wrap">
        <input type="text" class="gallery-search" id="gallery-albums-search" placeholder="Search albums..." />
      </div>
      <button class="gallery-select-btn gallery-toolbar-action" id="gallery-albums-select-btn" title="Select for bulk actions" style="position:relative;top:2px;"><span style="position:relative;top:1px;">Select</span></button>
    </div>
    <div class="memory-bulk-bar gallery-selection-bar hidden" id="gallery-albums-bulk-bar">
      <label class="memory-bulk-check-all" style="position:relative;top:-1px;"><input type="checkbox" id="gallery-albums-bulk-all"> All</label>
      <span id="gallery-albums-bulk-count" style="position:relative;top:-1px;">0 selected</span>
      <button class="memory-toolbar-btn" id="gallery-albums-bulk-delete" title="Delete selected" style="margin-left:auto;color:var(--color-error, #f44);position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>Delete</button>
      <button class="memory-toolbar-btn" id="gallery-albums-bulk-cancel" title="Cancel (Esc)" style="margin-left:4px;padding:3px 6px;position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div id="gallery-albums-grid-wrap"></div>
  `;

  // Wire search — debounced re-render, same pattern as Photos.
  const searchInput = container.querySelector('#gallery-albums-search');
  let _albumSearchDebounce = null;
  searchInput.addEventListener('input', () => {
    clearTimeout(_albumSearchDebounce);
    _albumSearchDebounce = setTimeout(() => {
      _albumSearch = searchInput.value;
      _renderAlbumsGrid();
    }, 150);
  });

  // Wire Select + bulk bar — Cancel restores the normal click-to-open
  // behavior; Actions opens a dropdown anchored on the button.
  container.querySelector('#gallery-albums-select-btn').addEventListener('click', () => {
    _setAlbumSelectMode(!_albumSelectMode);
  });
  container.querySelector('#gallery-albums-bulk-cancel').addEventListener('click', () => {
    _setAlbumSelectMode(false);
  });
  container.querySelector('#gallery-albums-bulk-all').addEventListener('change', (e) => {
    const on = e.target.checked;
    const list = _filteredAlbums();
    if (on) list.forEach(a => _albumSelected.add(a.id));
    else _albumSelected.clear();
    _renderAlbumsGrid();
  });
  container.querySelector('#gallery-albums-bulk-delete').addEventListener('click', (e) => {
    e.stopPropagation();
    if (!_albumSelected.size) { uiModule.showToast('Select albums first'); return; }
    _bulkDeleteAlbums([..._albumSelected]);
  });
}

function _setAlbumSelectMode(on) {
  _albumSelectMode = on;
  if (!on) _albumSelected.clear();
  const container = document.getElementById('gallery-albums-container');
  container.querySelector('#gallery-albums-select-btn span').textContent = on ? 'Cancel' : 'Select';
  container.querySelector('#gallery-albums-select-btn').classList.toggle('active', on);
  container.querySelector('#gallery-albums-bulk-bar').classList.toggle('hidden', !on);
  _renderAlbumsGrid();
}

function _updateAlbumBulkCount() {
  const container = document.getElementById('gallery-albums-container');
  if (!container) return;
  const sel = _albumSelected.size;
  const cnt = container.querySelector('#gallery-albums-bulk-count');
  if (cnt) cnt.textContent = sel + ' selected';
  const all = container.querySelector('#gallery-albums-bulk-all');
  const total = _filteredAlbums().length;
  if (all) { all.checked = total > 0 && sel === total; all.indeterminate = sel > 0 && sel < total; }
  const del = container.querySelector('#gallery-albums-bulk-delete');
  if (del) del.style.opacity = sel > 0 ? '1' : '0.5';
}

function _albumActionTiles() {
  return `
    <div class="gallery-card gallery-card-upload gallery-album-action-tile" id="gallery-albums-new">
      <div class="gallery-card-upload-inner">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h6l2 2h10v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/><line x1="12" y1="12" x2="12" y2="18"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
        <div class="gallery-card-upload-label">New album</div>
      </div>
    </div>
    <div class="gallery-card gallery-card-upload gallery-album-action-tile" id="gallery-albums-upload">
      <div class="gallery-card-upload-inner">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <div class="gallery-card-upload-label">Upload</div>
      </div>
    </div>`;
}

function _renderAlbumsGrid() {
  const container = document.getElementById('gallery-albums-container');
  if (!container) return;
  const wrap = container.querySelector('#gallery-albums-grid-wrap');
  if (!wrap) return;

  const albums = _filteredAlbums();
  if (!_albums.length) {
    wrap.innerHTML = `<div class="gallery-albums-grid">${_albumActionTiles()}</div>`;
    _wireAlbumsEvents(wrap);
    return;
  }
  if (!albums.length) {
    wrap.innerHTML = `<div class="gallery-albums-empty"><p>No albums match "${_esc(_albumSearch)}".</p></div>`;
    return;
  }

  let html = '<div class="gallery-albums-grid">';
  // Action tiles (New / Upload) — hidden in select mode so they don't
  // visually compete with the selection dots and can't be accidentally
  // toggled like real albums.
  if (!_albumSelectMode) {
    html += _albumActionTiles();
  }
  albums.forEach(a => {
    // Empty albums get the placeholder icon even if cover_url is set —
    // a stale cover from before the album was emptied looks like the
    // album still has photos in it.
    const cover = (a.cover_url && a.count > 0)
      ? `<img src="${_esc(a.cover_url)}" alt="" loading="lazy" />`
      : `<div class="gallery-album-placeholder">
           <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
         </div>`;
    const isSel = _albumSelected.has(a.id);
    const dot = _albumSelectMode
      ? `<span class="gallery-select-dot${isSel ? ' selected' : ''}" style="display:flex;"></span>`
      : '';
    const cls = 'gallery-album-card' + (_albumSelectMode ? ' gallery-card-selectable' : '') + (isSel ? ' selected' : '');
    html += `
      <div class="${cls}" data-album="${_esc(a.id)}">
        ${dot}
        <button class="gallery-album-menu-btn" data-album="${_esc(a.id)}" title="Options" aria-label="Album options"${_albumSelectMode ? ' style="display:none"' : ''}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="position:relative;top:2px;"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
        </button>
        <div class="gallery-album-menu-pop dropdown" data-album="${_esc(a.id)}" hidden>
          <div class="dropdown-item-compact" data-action="upload">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></span>
            <span>Upload here</span>
          </div>
          <div class="dropdown-item-compact" data-action="rename">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg></span>
            <span>Rename</span>
          </div>
          <div class="dropdown-divider"></div>
          <div class="dropdown-item-compact dropdown-item-danger" data-action="delete">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></span>
            <span>Delete</span>
          </div>
        </div>
        <div class="gallery-album-cover">${cover}</div>
        <div class="gallery-album-info">
          <div class="gallery-album-name">${_esc(a.name)}</div>
          <div class="gallery-album-count">${a.count} photo${a.count === 1 ? '' : 's'}</div>
        </div>
      </div>`;
  });
  html += '</div>';
  wrap.innerHTML = html;
  _updateAlbumBulkCount();
  _wireAlbumsEvents(wrap);
}

// Per-card / per-popmenu event wiring — extracted so both the empty
// state and the real grid can reuse it.
function _wireAlbumsEvents(scope) {
  const container = document.getElementById('gallery-albums-container');
  if (!container) return;

  container.querySelectorAll('.gallery-album-card[data-album]').forEach(card => {
    card.addEventListener('click', (e) => {
      // Clicks on the menu button or any pop-menu item are handled below;
      // don't navigate into the album in that case.
      if (e.target.closest('.gallery-album-menu-btn')) return;
      if (e.target.closest('.gallery-album-menu-pop')) return;
      // In select mode, clicking a card toggles its selection instead
      // of opening it. Mirrors the Photos tab's behaviour.
      if (_albumSelectMode) {
        const id = card.dataset.album;
        if (_albumSelected.has(id)) _albumSelected.delete(id);
        else _albumSelected.add(id);
        const dot = card.querySelector('.gallery-select-dot');
        if (dot) dot.classList.toggle('selected', _albumSelected.has(id));
        card.classList.toggle('selected', _albumSelected.has(id));
        _updateAlbumBulkCount();
        return;
      }
      _activeAlbum = card.dataset.album || null;
      _favoritesOnly = false;
      // Hide any open photo detail before swapping context — otherwise the
      // previously-viewed photo lingers on top when the user lands back on
      // the Photos tab.
      const _detail = document.getElementById('gallery-detail');
      if (_detail) _detail.style.display = 'none';
      _renderAlbums();
      _fetchLibrary(false);
      // Switch back to the Photos tab so they immediately see the contents.
      const modal = document.getElementById('gallery-modal');
      const photosTab = modal?.querySelector('.gallery-tab[data-tab="images"]');
      photosTab?.click();
    });
  });

  // Hover menu: toggle the per-card pop on ⋯ click, close any others.
  container.querySelectorAll('.gallery-album-menu-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = btn.dataset.album;
      const pop = container.querySelector(`.gallery-album-menu-pop[data-album="${CSS.escape(id)}"]`);
      const wasOpen = pop && !pop.hidden;
      container.querySelectorAll('.gallery-album-menu-pop').forEach(p => { p.hidden = true; });
      if (pop && !wasOpen) pop.hidden = false;
    });
  });
  // Click anywhere else closes any open pop.
  if (!container._popDismissWired) {
    document.addEventListener('click', (e) => {
      if (e.target.closest('.gallery-album-menu-btn')) return;
      if (e.target.closest('.gallery-album-menu-pop')) return;
      container.querySelectorAll('.gallery-album-menu-pop').forEach(p => { p.hidden = true; });
    });
    container._popDismissWired = true;
  }

  container.querySelectorAll('.gallery-album-menu-pop').forEach(pop => {
    const id = pop.dataset.album;
    pop.querySelector('[data-action="upload"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      pop.hidden = true;
      // Spawn an ephemeral file picker scoped to this album.
      const picker = document.createElement('input');
      picker.type = 'file';
      picker.accept = 'image/*,video/*';
      picker.multiple = true;
      picker.style.display = 'none';
      picker.addEventListener('change', async () => {
        const files = [...(picker.files || [])];
        if (files.length) await _bulkUpload(files, id);
        picker.remove();
      });
      document.body.appendChild(picker);
      picker.click();
    });
    pop.querySelector('[data-action="rename"]')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      pop.hidden = true;
      const album = _albums.find(a => a.id === id);
      const newName = prompt('Rename album:', album?.name || '');
      if (!newName || !newName.trim() || newName.trim() === album?.name) return;
      const r = await fetch(`${API_BASE}/api/gallery/albums/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin', body: JSON.stringify({ name: newName.trim() }),
      });
      if (r.ok) {
        await _fetchAlbums();
        _renderAlbumsTab();
        if (uiModule) uiModule.showToast('Album renamed');
      } else if (uiModule) {
        uiModule.showError('Rename failed');
      }
    });
    pop.querySelector('[data-action="delete"]')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      pop.hidden = true;
      const album = _albums.find(a => a.id === id);
      const ok = await uiModule.styledConfirm(
        `Delete album "${album?.name || ''}"? Photos inside will stay in your library.`,
        { confirmText: 'Delete', danger: true },
      );
      if (!ok) return;
      const r = await fetch(`${API_BASE}/api/gallery/albums/${id}`, {
        method: 'DELETE', credentials: 'same-origin',
      });
      if (r.ok) {
        if (_activeAlbum === id) _activeAlbum = null;
        await _fetchAlbums();
        _renderAlbumsTab();
        _renderAlbums();
        if (uiModule) uiModule.showToast('Album deleted');
      } else if (uiModule) {
        uiModule.showError('Delete failed');
      }
    });
  });

  document.getElementById('gallery-albums-new')?.addEventListener('click', async () => {
    const name = (uiModule.styledPrompt
      ? await uiModule.styledPrompt('Name your new album.', { title: 'New album', placeholder: 'e.g. Vacation 2026', confirmText: 'Create' })
      : prompt('Album name:'));
    if (!name?.trim()) return;
    await fetch(`${API_BASE}/api/gallery/albums`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin', body: JSON.stringify({ name: name.trim() }),
    });
    await _fetchAlbums();
    _renderAlbumsTab();
  });

  document.getElementById('gallery-albums-upload')?.addEventListener('click', () => {
    // <input webkitdirectory> picks a folder; we create an album with the
    // folder name and upload every image inside.
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.multiple = true;
    picker.webkitdirectory = true;
    picker.style.display = 'none';
    picker.addEventListener('change', async () => {
      const all = [...(picker.files || [])];
      const images = all.filter(_isMediaFile);
      picker.remove();
      if (!images.length) {
        if (uiModule) uiModule.showToast('No images or videos in that folder');
        return;
      }
      // Derive folder name from the first file's relative path (e.g.
      // "MyTrip/photo.jpg" → "MyTrip"). Fall back to a prompt.
      const rel = images[0].webkitRelativePath || '';
      let folderName = rel.split('/')[0] || '';
      if (!folderName) {
        folderName = prompt('Album name for these photos:') || '';
        if (!folderName.trim()) return;
      }
      // Reuse an existing album with the same name; otherwise create one.
      let album = _albums.find(a => a.name === folderName);
      if (!album) {
        const r = await fetch(`${API_BASE}/api/gallery/albums`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin', body: JSON.stringify({ name: folderName }),
        });
        const data = await r.json().catch(() => ({}));
        if (data?.id) {
          album = { id: data.id, name: folderName, count: 0 };
          _albums.push(album);
        }
      }
      if (!album) {
        if (uiModule) uiModule.showError('Could not create album');
        return;
      }
      await _bulkUpload(images, album.id);
      await _fetchAlbums();
      _renderAlbumsTab();
    });
    document.body.appendChild(picker);
    picker.click();
  });
}

async function _bulkDeleteAlbums(ids) {
  if (!ids.length) return;
  const ok = await uiModule.styledConfirm(
    `Delete ${ids.length} album${ids.length > 1 ? 's' : ''}? Photos inside will stay in your library.`,
    { confirmText: 'Delete', danger: true },
  );
  if (!ok) return;
  let failed = 0;
  for (const id of ids) {
    const r = await fetch(`${API_BASE}/api/gallery/albums/${id}`, {
      method: 'DELETE', credentials: 'same-origin',
    });
    if (!r.ok) failed++;
    else if (_activeAlbum === id) _activeAlbum = null;
  }
  if (failed) uiModule.showError(`Failed to delete ${failed} of ${ids.length} albums`);
  else if (uiModule) uiModule.showToast(`Deleted ${ids.length} album${ids.length > 1 ? 's' : ''}`);
  _setAlbumSelectMode(false);
  await _fetchAlbums();
  _renderAlbumsTab();
  _renderAlbums();
}

// Fetch the user's persisted editor drafts and render them as a thumbnail
// grid under the new-canvas / browse buttons. Each card resumes the draft
// in the editor on click; the × trashes it server-side.
// Frosted whirlpool overlay over the drafts area while fetching the
// list. Lives inside the drafts section so it sits above the grid.
let _draftsSpinner = null;
function _draftsShowLoading(section) {
  if (!section) return;
  let ov = section.querySelector('.gallery-editor-drafts-loading');
  if (!ov) {
    ov = document.createElement('div');
    ov.className = 'gallery-editor-drafts-loading';
    try {
      _draftsSpinner = spinnerModule.createWhirlpool(28);
      _draftsSpinner.element.style.cssText = 'width:28px;height:28px;margin:0;';
      ov.appendChild(_draftsSpinner.element);
    } catch (_) {
      ov.textContent = 'Loading…';
    }
    section.appendChild(ov);
  }
  // Start the overlay exactly at the grid's top so it covers ONLY the projects
  // list — not the header's search/select above it (the old fixed 30px offset
  // assumed a short header and ended up covering half the search/select).
  const _grid = section.querySelector('.gallery-editor-drafts-grid');
  const _hdr = section.querySelector('.gallery-editor-drafts-header');
  const _top = _grid ? _grid.offsetTop : (_hdr ? _hdr.offsetHeight : 30);
  ov.style.top = _top + 'px';
  ov.style.display = '';
}
function _draftsHideLoading(section) {
  if (!section) return;
  const ov = section.querySelector('.gallery-editor-drafts-loading');
  if (ov) ov.style.display = 'none';
}

// Held between renders so search + select state survive a re-render.
let _draftsCache = [];
let _draftsSearch = '';
let _draftsSelectMode = false;
let _draftsSelected = new Set();
let _draftsCascaded = false;
let _draftsView = (() => {
  try { return localStorage.getItem('gallery-drafts-view') === 'list' ? 'list' : 'grid'; }
  catch (_) { return 'grid'; }
})();

async function _renderEditorDrafts() {
  const section = document.getElementById('gallery-editor-drafts');
  const grid = document.getElementById('gallery-editor-drafts-grid');
  if (!section || !grid) return;
  // Show a frosted whirlpool overlay over the drafts area while the
  // list is fetching. The section becomes visible BEFORE the fetch so
  // the user sees the loading indicator instead of a blank space.
  section.hidden = false;
  _draftsShowLoading(section);
  try {
    const res = await fetch(`${API_BASE}/api/editor-drafts`, { credentials: 'same-origin' });
    if (res.ok) {
      const out = await res.json();
      _draftsCache = Array.isArray(out.drafts) ? out.drafts : [];
    }
  } catch (_) {
    _draftsCache = [];
  }
  _draftsHideLoading(section);
  if (!_draftsCache.length) {
    section.hidden = true;
    grid.innerHTML = '';
    _draftsSelected.clear();
    _draftsSelectMode = false;
    _draftsSyncBulkBar();
    return;
  }
  section.hidden = false;
  // Drop selections for drafts that no longer exist.
  const present = new Set(_draftsCache.map(d => d.id));
  for (const id of [..._draftsSelected]) if (!present.has(id)) _draftsSelected.delete(id);
  _draftsPaint();
  if (!_draftsCascaded) {
    _draftsCascaded = true;
    grid.classList.add('gallery-drafts-just-opened');
    setTimeout(() => grid.classList.remove('gallery-drafts-just-opened'), 900);
  }
  _draftsWireOnce();
}

// Re-render only the grid (and bulk bar) from cached drafts + search +
// selection state. Used by search/select-mode/checkbox updates.
function _draftsPaint() {
  const grid = document.getElementById('gallery-editor-drafts-grid');
  if (!grid) return;
  grid.classList.toggle('list-view', _draftsView === 'list');
  document.querySelectorAll('#gallery-editor-drafts-view [data-view]').forEach(btn => {
    const active = btn.dataset.view === _draftsView;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
  const q = _draftsSearch.trim().toLowerCase();
  const filtered = _draftsCache.filter(d => {
    if (!q) return true;
    const name = String(d.name || '').toLowerCase();
    return name.includes(q);
  });
  grid.innerHTML = filtered.map(d => {
    const updated = d.updated_at ? _humanRelativeDate(new Date(d.updated_at)) : '';
    const dims = (d.width && d.height) ? `${d.width}×${d.height}` : '';
    const thumb = d.thumbnail
      ? `<img class="gallery-editor-draft-thumb" src="${_esc(d.thumbnail)}" alt="" />`
      : '<div class="gallery-editor-draft-thumb gallery-editor-draft-thumb-empty"></div>';
    const checked = _draftsSelected.has(d.id);
    const checkbox = _draftsSelectMode
      ? `<span class="gallery-select-dot${checked ? ' selected' : ''}" data-draft-id="${_esc(d.id)}"></span>`
      : '';
    return `
      <div class="gallery-editor-draft-card${checked ? ' selected' : ''}${_draftsSelectMode ? ' select-mode' : ''}" data-draft-id="${_esc(d.id)}" tabindex="0" title="Resume ${_esc(d.name || 'project')}">
        ${checkbox}
        ${thumb}
        <div class="gallery-editor-draft-info">
          <div class="gallery-editor-draft-name">${_esc(d.name || 'Untitled')}</div>
          <div class="gallery-editor-draft-meta">${_esc([dims, updated].filter(Boolean).join(' · '))}</div>
        </div>
        <button class="gallery-editor-draft-delete" data-draft-id="${_esc(d.id)}" title="Delete project" aria-label="Delete project"><span class="gallery-editor-draft-delete-x">×</span><svg class="gallery-editor-draft-delete-trash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
      </div>`;
  }).join('');
  grid.querySelectorAll('.gallery-editor-draft-card').forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('.gallery-editor-draft-delete')) return;
      const id = card.dataset.draftId;
      if (!id) return;
      if (_draftsSelectMode) {
        if (_draftsSelected.has(id)) _draftsSelected.delete(id);
        else _draftsSelected.add(id);
        _draftsPaint();
        _draftsSyncBulkBar();
        return;
      }
      // Pass the cached dims as the preset size so the editor can show a
      // correctly-proportioned placeholder while the draft loads.
      const draft = _draftsCache.find(d => d.id === id);
      const presetSize = (draft && draft.width && draft.height)
        ? { w: draft.width, h: draft.height }
        : null;
      openEditor(null, null, presetSize, draft?.name || null, id);
    });
  });
  grid.querySelectorAll('.gallery-editor-draft-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = btn.dataset.draftId;
      if (!id) return;
      const ok = await uiModule.styledConfirm('Delete this project?', {
        confirmText: 'Delete', cancelText: 'Cancel', danger: true,
      });
      if (!ok) return;
      // Graceful exit: fade + shrink the card before the grid re-renders.
      const card = btn.closest('.gallery-editor-draft-card');
      if (card) card.classList.add('gallery-draft-removing');
      try {
        await fetch(`${API_BASE}/api/editor-drafts/${encodeURIComponent(id)}`, {
          method: 'DELETE', credentials: 'same-origin',
        });
      } catch (_) { /* swallow — refresh below */ }
      await new Promise(r => setTimeout(r, 240));   // let the animation finish
      _draftsSelected.delete(id);
      _renderEditorDrafts();
    });
  });
  _draftsSyncBulkBar();
}

function _draftsSyncBulkBar() {
  const bar = document.getElementById('gallery-editor-drafts-bulk');
  const countEl = document.getElementById('gallery-editor-drafts-bulk-count');
  const selectBtn = document.getElementById('gallery-editor-drafts-select');
  if (bar) bar.classList.toggle('hidden', !_draftsSelectMode);
  if (countEl) countEl.textContent = `${_draftsSelected.size} selected`;
  if (selectBtn) {
    selectBtn.textContent = _draftsSelectMode ? 'Cancel' : 'Select';
    selectBtn.classList.toggle('active', _draftsSelectMode);
  }
  // "All" checkbox state — checked when all visible drafts are selected,
  // indeterminate when only some (matches the Photos tab).
  const all = document.getElementById('gallery-editor-drafts-select-all');
  if (all) {
    const q = _draftsSearch.trim().toLowerCase();
    const visible = _draftsCache.filter(d => !q || String(d.name || '').toLowerCase().includes(q));
    const selVis = visible.filter(d => _draftsSelected.has(d.id)).length;
    all.checked = visible.length > 0 && selVis === visible.length;
    all.indeterminate = selVis > 0 && selVis < visible.length;
  }
  const deleteBtn = document.getElementById('gallery-editor-drafts-bulk-delete');
  if (deleteBtn) deleteBtn.disabled = _draftsSelected.size === 0;
}

let _draftsWired = false;
function _draftsWireOnce() {
  if (_draftsWired) return;
  _draftsWired = true;
  document.getElementById('gallery-editor-drafts-search')?.addEventListener('input', (e) => {
    _draftsSearch = e.target.value || '';
    _draftsPaint();
  });
  document.querySelectorAll('#gallery-editor-drafts-view [data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      _draftsView = btn.dataset.view === 'list' ? 'list' : 'grid';
      try { localStorage.setItem('gallery-drafts-view', _draftsView); } catch (_) {}
      _draftsPaint();
    });
  });
  document.getElementById('gallery-editor-drafts-select')?.addEventListener('click', () => {
    _draftsSelectMode = !_draftsSelectMode;
    if (!_draftsSelectMode) _draftsSelected.clear();
    _draftsPaint();
  });
  document.getElementById('gallery-editor-drafts-select-all')?.addEventListener('change', (e) => {
    // Same "All" checkbox behavior as Photos: checked selects every visible
    // draft, unchecked clears them (respects the search filter).
    const q = _draftsSearch.trim().toLowerCase();
    const visible = _draftsCache.filter(d => !q || String(d.name || '').toLowerCase().includes(q));
    if (e.target.checked) for (const d of visible) _draftsSelected.add(d.id);
    else for (const d of visible) _draftsSelected.delete(d.id);
    _draftsPaint();
  });
  document.getElementById('gallery-editor-drafts-bulk-cancel')?.addEventListener('click', () => {
    _draftsSelectMode = false;
    _draftsSelected.clear();
    _draftsPaint();
  });
  document.getElementById('gallery-editor-drafts-bulk-delete')?.addEventListener('click', async () => {
    if (!_draftsSelected.size) return;
    const n = _draftsSelected.size;
    const ok = await uiModule.styledConfirm(`Delete ${n} project${n === 1 ? '' : 's'}?`, {
      confirmText: 'Delete', cancelText: 'Cancel', danger: true,
    });
    if (!ok) return;
    const ids = [..._draftsSelected];
    // Graceful exit on the selected cards before they're removed.
    const grid = document.getElementById('gallery-editor-drafts-grid');
    if (grid) ids.forEach(id => grid.querySelector(`.gallery-editor-draft-card[data-draft-id="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`)?.classList.add('gallery-draft-removing'));
    await new Promise(r => setTimeout(r, 240));
    await Promise.allSettled(ids.map(id =>
      fetch(`${API_BASE}/api/editor-drafts/${encodeURIComponent(id)}`, {
        method: 'DELETE', credentials: 'same-origin',
      })
    ));
    _draftsSelected.clear();
    _draftsSelectMode = false;
    _renderEditorDrafts();
  });
}

// Human-readable "x minutes ago" / "y days ago" for the drafts list.
function _humanRelativeDate(when) {
  const diff = (Date.now() - when.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + 'd ago';
  return when.toLocaleDateString();
}

// Edit tab empty state — shown when the user clicks the tab without a photo
// loaded. Lets them start a blank canvas or jump back to pick a photo.
function _renderEditorLanding() {
  const container = document.getElementById('gallery-editor-container');
  if (!container) return;
  // openEditor()/closeEditor() may have left the container hidden; the Edit
  // tab is still active so make sure the landing is actually visible.
  container.style.display = 'flex';
  const presets = [
    { w: 1024, h: 1024, label: 'Square HD — 1024 × 1024' },
    { w: 1920, h: 1080, label: 'Widescreen — 1920 × 1080' },
    { w: 1080, h: 1920, label: 'Portrait — 1080 × 1920' },
    { w: 1080, h: 1080, label: 'Instagram — 1080 × 1080' },
    { w: 1500, h: 1050, label: 'Postcard — 1500 × 1050' },
    { w: 2480, h: 3508, label: 'A4 (300dpi) — 2480 × 3508' },
    { w: 2550, h: 3300, label: 'Letter (300dpi) — 2550 × 3300' },
    { w: 3840, h: 2160, label: '4K — 3840 × 2160' },
  ];
  const templateButtonsHtml = presets
    .map((p, i) => {
      const ratio = p.w / p.h;
      const maxW = 30;
      const maxH = 18;
      const shapeW = ratio >= maxW / maxH ? maxW : Math.max(9, Math.round(maxH * ratio));
      const shapeH = ratio >= maxW / maxH ? Math.max(9, Math.round(maxW / ratio)) : maxH;
      const x = Math.round((40 - shapeW) / 2);
      const y = Math.round((28 - shapeH) / 2);
      const [name, dimensions = `${p.w} × ${p.h}`] = p.label.split(' — ');
      return `<button type="button" class="gallery-editor-template-option" data-template-index="${i}" title="${_esc(p.label)}">
        <svg width="40" height="28" viewBox="0 0 40 28" fill="none" aria-hidden="true"><rect x="${x}" y="${y}" width="${shapeW}" height="${shapeH}" rx="1" stroke="currentColor" stroke-width="1.5"/></svg>
        <span>${_esc(name)}</span><small>${_esc(dimensions)}</small>
      </button>`;
    })
    .join('');
  container.innerHTML = `
    <div class="gallery-editor-landing">
      <div class="gallery-editor-heading">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg>
        <h3>Image Editor <span class="ge-alpha-tag">Alpha</span></h3>
      </div>
      <p>Start a blank canvas, or open a photo from your gallery to edit it.</p>
      <div class="gallery-editor-landing-actions">
        <button class="gallery-select-btn" id="gallery-editor-new">New canvas...</button>
        <button class="gallery-select-btn" id="gallery-editor-import"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-1px;margin-right:4px;"><path d="M12 3v12"/><path d="M7 8l5-5 5 5"/><path d="M5 21h14a2 2 0 0 0 2-2v-4"/><path d="M3 15v4a2 2 0 0 0 2 2"/></svg>Import</button>
        <button class="gallery-select-btn" id="gallery-editor-pick"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-1px;margin-right:4px;"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>Browse gallery</button>
      </div>
      <section class="gallery-editor-template-picker" aria-labelledby="gallery-editor-template-title">
        <h4 id="gallery-editor-template-title">Pick template</h4>
        <div class="gallery-editor-template-options">${templateButtonsHtml}</div>
      </section>
      <div class="gallery-editor-drafts" id="gallery-editor-drafts" hidden>
        <div class="gallery-editor-drafts-header">
          <h4 class="gallery-editor-drafts-title">Saved projects</h4>
          <input type="search" class="gallery-editor-drafts-search" id="gallery-editor-drafts-search" placeholder="Search projects…" autocomplete="off" />
          <div class="gallery-drafts-view-toggle" id="gallery-editor-drafts-view" role="group" aria-label="Project view">
            <button type="button" data-view="grid" class="active" title="Grid view" aria-label="Grid view" aria-pressed="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg></button>
            <button type="button" data-view="list" title="List view" aria-label="List view" aria-pressed="false"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></button>
          </div>
          <button class="gallery-select-btn gallery-toolbar-action" id="gallery-editor-drafts-select" title="Toggle multi-select" style="position:relative;top:0;"><span>Select</span></button>
        </div>
        <div class="memory-bulk-bar gallery-selection-bar hidden" id="gallery-editor-drafts-bulk">
          <label class="memory-bulk-check-all" style="position:relative;top:-1px;"><input type="checkbox" id="gallery-editor-drafts-select-all"> All</label>
          <span id="gallery-editor-drafts-bulk-count" style="position:relative;top:0;">0 selected</span>
          <button class="memory-toolbar-btn" id="gallery-editor-drafts-bulk-delete" style="color:var(--red);position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>Delete</button>
          <button class="memory-toolbar-btn" id="gallery-editor-drafts-bulk-cancel" title="Cancel (Esc)" style="margin-left:4px;padding:3px 6px;position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
        </div>
        <div class="gallery-editor-drafts-grid" id="gallery-editor-drafts-grid"></div>
      </div>
    </div>`;
  // Each remount of the editor landing rebuilds the drafts header
  // markup, so the cached event listener references are stale. Reset.
  _draftsWired = false;
  _draftsCascaded = false;
  _renderEditorDrafts();
  document.querySelectorAll('.gallery-editor-template-option').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.templateIndex, 10);
      if (Number.isNaN(idx)) return;
      const p = presets[idx];
      if (p) openEditor(null, null, { w: p.w, h: p.h }, `${p.w}×${p.h}`);
    });
  });
  document.getElementById('gallery-editor-new')?.addEventListener('click', async () => {
    // openEditor() now returns a Promise — it's async because the size
    // prompt is a styled modal. Await it before checking whether the
    // editor actually opened (the user may have cancelled).
    try {
      await openEditor(null, null, null, 'New canvas');
    } catch (error) {
      // openEditor reports the initialization error and tears down its shell.
      console.error('[gallery] New canvas failed', error);
    }
    if (!document.querySelector('#gallery-editor-container .gallery-editor')) {
      _renderEditorLanding();
    }
  });
  document.getElementById('gallery-editor-pick')?.addEventListener('click', () => {
    document.querySelector('#gallery-modal .gallery-tab[data-tab="images"]')?.click();
  });
  document.getElementById('gallery-editor-import')?.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const name = (file.name || 'Imported image').replace(/\.[^.]+$/, '') || 'Imported image';
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          await openEditor(reader.result, null, null, name);
        } catch (e) {
          console.error('[gallery] image import failed', e);
          uiModule?.showError?.('Could not import that image');
        }
      };
      reader.onerror = () => uiModule?.showError?.('Could not read that file');
      reader.readAsDataURL(file);
    });
    input.click();
  });
}

// Wire the first-tile Upload affordance in the Photos grid. Opens the same
// multi-file picker the old Import button used.
function _wireUploadTile() {
  const tile = document.getElementById('gallery-upload-tile');
  if (!tile) return;
  tile.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*,video/*';
    input.multiple = true;
    input.addEventListener('change', async () => {
      if (!input.files.length) return;
      const files = [];
      for (const file of [...input.files]) {
        const nextFile = await fileHandlerModule.cropForMobileUpload(file);
        if (nextFile) files.push(nextFile);
      }
      if (files.length) _bulkUpload(files, _activeAlbum);
    });
    input.click();
  });
  tile.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    e.preventDefault();
    tile.click();
  });
}

// Shimmer placeholder tiles shown while the FIRST page loads, so the grid
// doesn't pop from empty → full (re-opens keep the old photos via
// stale-while-revalidate, so skeletons only show when there's nothing yet).
function _renderSkeletons(n) {
  const grid = document.getElementById('gallery-grid');
  if (!grid) return;
  const count = Math.max(8, Math.min(n || 12, 20));
  let html = '';
  for (let i = 0; i < count; i++) html += '<div class="gallery-card gallery-card-skeleton" aria-hidden="true"></div>';
  grid.innerHTML = html;
  const lm = document.getElementById('gallery-load-more');
  if (lm) lm.style.display = 'none';
}

function _renderGrid() {
  const grid = document.getElementById('gallery-grid');
  const loadMore = document.getElementById('gallery-load-more');
  if (!grid) return;

  // First tile: always-visible "Upload" affordance. Mirrors the Upload album
  // tile in the Albums tab so the upload entry point is consistent across
  // both grids.
  const uploadTile = `
    <div class="gallery-card gallery-card-upload" id="gallery-upload-tile" title="Upload photos or videos">
      <div class="gallery-card-upload-inner">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <div class="gallery-card-upload-label">Upload</div>
      </div>
    </div>`;

  if (_items.length === 0) {
    grid.innerHTML = uploadTile + '<div class="gallery-empty">No photos yet. Click Upload or drag-and-drop to get started!</div>';
    _wireUploadTile();
    if (loadMore) loadMore.style.display = 'none';
    return;
  }

  let html = uploadTile;
  _items.forEach(img => {
    const date = img.taken_at
      ? new Date(img.taken_at).toLocaleDateString()
      : (img.created_at ? new Date(img.created_at).toLocaleDateString() : '');
    // Card label: prefer the prompt (which doubles as the user-editable
    // name for uploaded photos). Fall back to a cleaned filename so
    // imported photos with empty prompts still show something useful
    // instead of a blank row.
    const fallbackName = (img.filename || '')
      .replace(/^\d{4,}[_-]/, '')   // drop date-prefix on uploads
      .replace(/\.[^.]+$/, '')       // drop extension
      .replace(/[_-]+/g, ' ')
      .trim();
    const labelText = (img.caption || '').trim() || (img.prompt || '').trim() || fallbackName || 'Photo';
    const promptPreview = labelText.length > 60 ? labelText.substring(0, 58) + '...' : labelText;
    const favCls = img.favorite ? ' gallery-fav-active' : '';
    const sourceLabel = img.model === 'imported' ? 'Imported' : (img.model || 'Image');
    const badges = [
      img.favorite ? '<span class="gallery-card-badge gallery-card-badge-fav">&#9829;</span>' : '',
      img.album_id ? '<span class="gallery-card-badge">Album</span>' : '',
      img.ai_tags ? '<span class="gallery-card-badge">AI tags</span>' : '',
      _isVideoUrl(img.url) ? '<span class="gallery-card-badge">Video</span>' : '',
    ].filter(Boolean).join('');
    html += `
      <div class="gallery-card" data-id="${_esc(img.id)}">
        <span class="gallery-select-dot" style="display:none;"></span>
        <button class="gallery-fav-btn${favCls}" data-id="${_esc(img.id)}" title="Favorite">&#9829;</button>
        <button class="gallery-dl-btn" data-id="${_esc(img.id)}" data-url="${_esc(img.url)}" data-filename="${_esc(img.filename || '')}" title="Download">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </button>
        ${_isVideoUrl(img.url)
          ? `<video src="${_esc(img.url)}" preload="metadata" muted playsinline></video>
             <span class="gallery-card-play" aria-hidden="true">
               <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
             </span>`
          : `<img src="${_esc(img.thumb_url || img.url)}" alt="${_esc(img.prompt)}" loading="lazy" />`}
        <div class="gallery-card-overlay" aria-hidden="true">
          ${badges ? `<span class="gallery-card-badges">${badges}</span>` : ''}
        </div>
        <div class="gallery-card-info">
          <div class="gallery-card-prompt">${_esc(promptPreview)}</div>
          <div class="gallery-card-meta">
            ${typeof img.distance_km === 'number'
              ? `<span class="gallery-card-model gallery-card-distance" title="Distance from ${_esc(_activeNear?.label || 'the chosen spot')}">📍 ${_esc(_formatDistance(img.distance_km))}</span>`
              : (sourceLabel ? `<span class="gallery-card-model">${_esc(sourceLabel)}</span>` : '')}
            <span class="gallery-card-date">${date}</span>
          </div>
        </div>
      </div>`;
  });
  grid.innerHTML = html;
  _wireUploadTile();

  // Domino-in cascade the first render after opening (not on filter/sort/
  // load-more re-renders) — mirrors the document library.
  if (!_galleryCascaded) {
    _galleryCascaded = true;
    grid.classList.add('gallery-just-opened');
    setTimeout(() => grid.classList.remove('gallery-just-opened'), 900);
  }

  if (loadMore) {
    loadMore.style.display = _items.length < _total ? 'block' : 'none';
  }

  // Card click → detail (skip the upload tile, it has its own handler)
  grid.querySelectorAll('.gallery-card[data-id]').forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('.gallery-fav-btn')) return;
      if (e.target.closest('.gallery-dl-btn')) return;
      const selectBtn = document.getElementById('gallery-select-btn');
      if (selectBtn && selectBtn.classList.contains('active')) return;
      const img = _items.find(i => i.id === card.dataset.id);
      if (img) _openDetail(img);
    });
    card.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
      e.preventDefault();
      const selectBtn = document.getElementById('gallery-select-btn');
      if (selectBtn && selectBtn.classList.contains('active')) return;
      const img = _items.find(i => i.id === card.dataset.id);
      if (img) _openDetail(img);
    });
  });

  // Download buttons
  grid.querySelectorAll('.gallery-dl-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const url = btn.dataset.url;
      const filename = btn.dataset.filename || `image-${btn.dataset.id}.png`;
      try {
        const res = await fetch(url, { credentials: 'same-origin' });
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
      } catch (_) {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
    });
  });

  // Favorite buttons
  grid.querySelectorAll('.gallery-fav-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      const res = await fetch(`${API_BASE}/api/gallery/${id}/favorite`, {
        method: 'POST', credentials: 'same-origin',
      });
      const data = await res.json();
      if (data.ok) {
        btn.classList.toggle('gallery-fav-active', data.favorite);
        const item = _items.find(i => i.id === id);
        if (item) item.favorite = data.favorite;
      }
    });
  });
}

// ---- Detail overlay ----

function _openDetail(img) {
  const detail = document.getElementById('gallery-detail');
  if (!detail) return;
  // Drop any face-overlay resize listener from the previous photo
  // before the new render attaches its own.
  if (_faceOverlayCleanup) { _faceOverlayCleanup(); _faceOverlayCleanup = null; }
  setTimeout(() => _loadDetailFaces(img), 0);

  const _dateSrc = img.taken_at || img.created_at || null;
  const _dateObj = _dateSrc ? new Date(_dateSrc) : null;
  const _relAgo = (d) => {
    if (!d || isNaN(d.getTime())) return '';
    const secs = (Date.now() - d.getTime()) / 1000;
    if (secs < 0) return '';
    if (secs < 60) return 'just now';
    if (secs < 3600) { const m = Math.floor(secs / 60); return `${m} minute${m !== 1 ? 's' : ''} ago`; }
    if (secs < 86400) { const h = Math.floor(secs / 3600); return `${h} hour${h !== 1 ? 's' : ''} ago`; }
    if (secs < 86400 * 7) { const d2 = Math.floor(secs / 86400); return `${d2} day${d2 !== 1 ? 's' : ''} ago`; }
    if (secs < 86400 * 30) { const w = Math.floor(secs / (86400 * 7)); return `${w} week${w !== 1 ? 's' : ''} ago`; }
    if (secs < 86400 * 365) { const mo = Math.floor(secs / (86400 * 30)); return `${mo} month${mo !== 1 ? 's' : ''} ago`; }
    const y = Math.floor(secs / (86400 * 365));
    return `${y} year${y !== 1 ? 's' : ''} ago`;
  };
  const date = _dateObj
    ? `${_dateObj.toLocaleString()}<span class="gallery-date-rel"> (${_relAgo(_dateObj)})</span>`
    : 'Unknown';
  const userTags = img.user_tags || img.tags || '';
  const aiTags = img.ai_tags || '';
  const dims = img.width && img.height ? `${img.width} x ${img.height}` : (img.size || 'Unknown');
  const fileSize = img.file_size ? _humanSize(img.file_size) : '';
  const fallbackName = (img.filename || '').replace(/^\d{4,}[_-]/, '').replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim();
  const displayTitle = (img.prompt || img.caption || fallbackName || 'Photo').trim();
  const sourceLabel = img.model === 'imported' ? 'Imported' : (img.model || 'Unknown');
  const sessionLabel = img.session_name || (img.session_id ? 'Source chat' : '');
  // "Edited" row: only show when updated_at is meaningfully later than
  // created_at (>10s). Every photo bumps updated_at on insert via the
  // ORM timestamp mixin, so the gap filters out the trivial case.
  let editedHtml = '';
  // Library photos are never edited: their row only changes when Odysseus
  // adds tags/faces, which must not read as "Edited".
  if (img.updated_at && img.created_at && !img.read_only) {
    const u = new Date(img.updated_at);
    const c = new Date(img.created_at);
    if (!isNaN(u) && !isNaN(c) && (u.getTime() - c.getTime() > 10000)) {
      editedHtml = `<div class="gallery-detail-section"><label>Edited</label><div>${u.toLocaleString()}<span class="gallery-date-rel"> (${_relAgo(u)})</span></div></div>`;
    }
  }

  detail.innerHTML = `
    <div class="gallery-detail-header">
      <button class="gallery-detail-back" id="gallery-detail-back">&larr; Back</button>
      <div class="gallery-detail-title" title="${_esc(displayTitle)}">${_esc(displayTitle)}</div>
      <div style="flex:1"></div>
      <button class="gallery-detail-back" id="gallery-edit-direct-btn" title="Edit (E)" aria-label="Edit photo" style="display:inline-flex;align-items:center;gap:4px;">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg>
        Edit
      </button>
      <button class="gallery-detail-back" id="gallery-download-btn" title="Download" aria-label="Download photo" style="display:inline-flex;align-items:center;gap:4px;">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download
      </button>
      <button class="gallery-detail-back gallery-detail-fav-header${img.favorite ? ' active' : ''}" id="gallery-detail-fav-header" title="${img.favorite ? 'Unfavorite' : 'Favorite'}" aria-label="Favorite" aria-pressed="${img.favorite ? 'true' : 'false'}" style="display:inline-flex;align-items:center;justify-content:center;padding:4px 8px;">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="${img.favorite ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
      </button>
      <div class="gallery-detail-menu-wrap">
        <button class="gallery-detail-action gallery-detail-menu-btn" id="gallery-detail-menu-btn" title="Actions" aria-label="Photo actions">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
        </button>
        <div class="gallery-detail-menu dropdown" id="gallery-detail-menu" hidden>
          <button class="dropdown-item-compact" id="gallery-chat-photo-btn">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/></svg></span>
            ${img.session_id ? 'Open chat' : 'Discuss in chat'}
          </button>
          <button class="dropdown-item-compact" id="gallery-fav-detail">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="${img.favorite ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg></span>
            ${img.favorite ? 'Favorited' : 'Favorite'}
          </button>
          <button class="dropdown-item-compact" id="gallery-ai-tag-btn" data-mode="${aiTags ? 'clear' : 'tag'}">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg></span>
            ${aiTags ? 'Clear AI tags' : 'AI Tag'}
          </button>
          ${img.album_id ? `<button class="dropdown-item-compact" id="gallery-set-cover-btn">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></span>
            Set as album cover
          </button>` : ''}
          <div class="dropdown-divider"></div>
          <button class="dropdown-item-compact dropdown-item-danger" id="gallery-delete-btn">
            <span class="dropdown-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></span>
            ${img.read_only ? 'Hide from gallery' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
    <div class="gallery-detail-body">
      <div class="gallery-detail-image" id="gallery-detail-image-wrap" style="position:relative">
        ${img.read_only ? '' : `<button class="gallery-detail-rotate gallery-detail-rotate-ccw" id="gallery-rotate-ccw-btn" title="Rotate 90° counter-clockwise" aria-label="Rotate left">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
        </button>
        <button class="gallery-detail-rotate gallery-detail-rotate-cw" id="gallery-rotate-btn" title="Rotate 90° clockwise" aria-label="Rotate right">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>`}
        <button class="gallery-detail-nav gallery-detail-nav-prev" id="gallery-detail-prev" title="Previous (←)" aria-label="Previous">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <div class="gallery-detail-img-frame">
          ${_isVideoUrl(img.url)
            ? `<video id="gallery-detail-img" src="${_esc(img.url)}" controls preload="metadata" playsinline></video>`
            : `<img id="gallery-detail-img" src="${_esc(img.url)}" alt="${_esc(img.prompt)}" />`}
          <div id="gallery-detail-face-overlay" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none"></div>
        </div>
        <button class="gallery-detail-nav gallery-detail-nav-next" id="gallery-detail-next" title="Next (→)" aria-label="Next">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        </button>
      </div>
      <div class="gallery-detail-sidebar">
        <div class="gallery-detail-inspector-head">
          <div class="gallery-detail-eyebrow">Inspector</div>
          <div class="gallery-detail-inspector-title">${_esc(displayTitle)}</div>
        </div>
        <div class="gallery-detail-section">
          <label>Name</label>
          <div class="gallery-name-wrap">
            <input type="text" class="gallery-detail-name-input" id="gallery-detail-name-input"
              value="${_esc(img.prompt || '')}" placeholder="Untitled photo (press Enter to save)" />
            <svg class="gallery-name-enter" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>
          </div>
        </div>
        ${img.read_only ? `<div class="gallery-detail-section"><label>Library</label><div class="gallery-detail-library-path" title="Read-only: the original stays untouched in your folder. Edits are saved as copies.">🔒 ${_esc(img.source_path || '')}</div><div class="gallery-detail-mini-actions"><button class="gallery-detail-mini-btn" id="gallery-detail-similar" title="Photos that look like this one, most similar first"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="7" width="10" height="10" rx="2"/><rect x="12" y="4" width="10" height="10" rx="2"/></svg>Similar photos</button></div></div>` : ''}
        ${img.caption ? `<div class="gallery-detail-section"><label>OCR Caption</label><div class="gallery-detail-prompt">${_esc(img.caption)}</div></div>` : ''}
        ${img.prompt && img.model !== 'imported' && img.model !== 'library' ? `<div class="gallery-detail-section gallery-detail-prompt-section"><label>Prompt</label><div class="gallery-detail-prompt">${_esc(img.prompt)}</div><div class="gallery-detail-mini-actions"><button class="gallery-detail-mini-btn" id="gallery-detail-copy-prompt"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy</button><button class="gallery-detail-mini-btn" id="gallery-detail-reuse-prompt"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>Reuse in chat</button></div></div>` : ''}
        <div class="gallery-detail-meta-grid">
          <div class="gallery-detail-meta-card"><span>Date</span><strong>${date}</strong></div>
          <div class="gallery-detail-meta-card"><span>Size</span><strong>${dims}${fileSize ? ` · ${fileSize}` : ''}</strong></div>
          <div class="gallery-detail-meta-card"><span>Source</span><strong>${_esc(sourceLabel)}</strong></div>
          ${sessionLabel ? `<div class="gallery-detail-meta-card"><span>Session</span><strong>${img.session_id ? `<button class="gallery-detail-inline-link" id="gallery-detail-source-link">${_esc(sessionLabel)}</button>` : _esc(sessionLabel)}</strong></div>` : ''}
        </div>
        ${editedHtml}
        ${img.camera ? `<div class="gallery-detail-section"><label>Camera</label><div>${_esc(img.camera)}</div></div>` : ''}
        ${img.gps ? `<div class="gallery-detail-section"><label>Location</label><div>${_esc(img.gps.lat)}, ${_esc(img.gps.lng)}${typeof img.distance_km === 'number' ? ` <span class="gallery-detail-distance">· ${_esc(_formatDistance(img.distance_km))} away</span>` : ''}</div><div class="gallery-detail-mini-actions"><button class="gallery-detail-mini-btn" id="gallery-detail-show-map" title="Show where this photo was taken"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/></svg>Show on map</button><button class="gallery-detail-mini-btn" id="gallery-detail-nearby" title="All photos with a location, nearest to this one first"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/></svg>Photos nearby</button></div></div>` : ''}
        ${aiTags ? `<div class="gallery-detail-section"><label>AI Tags</label><div class="gallery-ai-tags">${aiTags.split(',').map(t => t.trim()).filter(Boolean).map(t => `<button class="gallery-ai-chip gallery-aitag-chip" data-tag-filter="${_esc(t)}" title="AI-generated tag — click to filter to photos tagged “${_esc(t)}”"><span class="gallery-aitag-mark" aria-hidden="true">✦</span>${_esc(t)}</button>`).join('')}</div></div>` : ''}
        <div class="gallery-detail-section">
          <label>Tags</label>
          <div class="gallery-ai-tags" id="gallery-user-tag-chips">${userTags.split(',').map(t => t.trim()).filter(Boolean).map(t => `<button class="gallery-ai-chip gallery-user-chip" data-tag-filter="${_esc(t)}" title="Filter to photos tagged “${_esc(t)}”">${_esc(t)}<span class="gallery-tag-x" title="Remove tag" aria-label="Remove tag">×</span></button>`).join('')}</div>
          <div class="gallery-tag-input-wrap">
            <input type="text" class="gallery-tag-input" id="gallery-tag-input"
              value="" placeholder="Add a tag" title="Type a tag and press Enter to add it" />
            <span class="gallery-tag-enter-hint" aria-hidden="true">↵</span>
          </div>
        </div>
        <div class="gallery-detail-section">
          <label>Album</label>
          <select id="gallery-detail-album" class="gallery-tag-input" style="padding:4px 6px;">
            <option value="">None</option>
            ${_albums.map(a => `<option value="${a.id}" ${img.album_id === a.id ? 'selected' : ''}>${_esc(a.name)}</option>`).join('')}
          </select>
        </div>
        <div class="gallery-detail-section" id="gallery-detail-people-section" style="display:none">
          <label>People in this photo</label>
          <div id="gallery-detail-people-list" class="gallery-detail-people"></div>
        </div>
      </div>
    </div>
  `;
  detail.style.display = 'flex';

  document.getElementById('gallery-detail-back').addEventListener('click', () => {
    detail.style.display = 'none';
  });

  const _openSourceChat = async () => {
    if (!img.session_id) return false;
    closeGallery();
    try {
      await sessionModule.selectSession(img.session_id);
      return true;
    } catch (e) {
      console.error('Open source chat failed:', e);
      uiModule.showError && uiModule.showError('Could not open source chat');
      return false;
    }
  };

  document.getElementById('gallery-chat-photo-btn')?.addEventListener('click', async () => {
    if (img.session_id) {
      await _openSourceChat();
      return;
    }

    try {
      const dcRes = await fetch('/api/default-chat', { credentials: 'same-origin' });
      const dc = await dcRes.json();
      if (!dc?.endpoint_url || !dc?.model) {
        uiModule.showError && uiModule.showError('Pick a chat model first');
        return;
      }
      sessionModule.createDirectChat(dc.endpoint_url, dc.model, dc.endpoint_id);
      closeGallery();
      const res = await fetch(img.url, { credentials: 'same-origin' });
      if (!res.ok) throw new Error('image fetch ' + res.status);
      const blob = await res.blob();
      const name = img.filename || 'gallery-photo.jpg';
      const file = new File([blob], name, { type: blob.type || 'image/jpeg' });
      fileHandlerModule.addFiles([file]);
      const input = document.getElementById('message');
      if (input) {
        input.value = 'Let’s discuss this photo.';
        input.dispatchEvent(new Event('input'));
        input.focus();
      }
    } catch (e) {
      console.error('Discuss photo failed:', e);
      uiModule.showError && uiModule.showError('Could not start photo chat');
    }
  });

  document.getElementById('gallery-detail-source-link')?.addEventListener('click', _openSourceChat);
  document.getElementById('gallery-detail-similar')?.addEventListener('click', () => {
    const label = (img.source_path || img.prompt || 'this photo').split('/').pop();
    _showPhotosFiltered(() => { _activeSimilar = { id: img.id, label }; });
  });
  const _photoSpot = () => ({
    lat: Number(img.gps.lat), lng: Number(img.gps.lng),
    label: (img.source_path || img.prompt || img.filename || 'this photo').split('/').pop(),
  });
  document.getElementById('gallery-detail-show-map')?.addEventListener('click', () => _showOnMap(_photoSpot()));
  document.getElementById('gallery-detail-nearby')?.addEventListener('click', () => {
    const spot = _photoSpot();
    _showPhotosFiltered(() => { _activeNear = spot; });
  });
  document.getElementById('gallery-detail-copy-prompt')?.addEventListener('click', async (event) => {
    const text = img.prompt || '';
    if (!text) return;
    const button = event.currentTarget;
    try {
      // navigator.clipboard is unavailable on many HTTP/IP deployments.
      // ui.copyToClipboard falls back to a transient textarea + execCommand.
      await uiModule.copyToClipboard(text);
      const original = button.innerHTML;
      button.textContent = 'Copied';
      button.disabled = true;
      setTimeout(() => {
        button.innerHTML = original;
        button.disabled = false;
      }, 1200);
    } catch (_) {
      uiModule.showError && uiModule.showError('Could not copy prompt');
    }
  });
  document.getElementById('gallery-detail-reuse-prompt')?.addEventListener('click', async () => {
    const input = document.getElementById('message');
    if (!input || !img.prompt) return;
    try {
      const res = await fetch(img.url, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`image fetch ${res.status}`);
      const blob = await res.blob();
      const file = new File([blob], img.filename || 'gallery-image.png', {
        type: blob.type || 'image/png',
      });
      fileHandlerModule.addFiles([file]);
      closeGallery();
      input.value = img.prompt;
      input.dispatchEvent(new Event('input'));
      input.focus();
    } catch (error) {
      console.error('Reuse gallery image failed:', error);
      uiModule.showError && uiModule.showError('Could not attach gallery image');
    }
  });

  // Clickable tag chips — both AI Tags and User Tags. Clicking a chip
  // closes the detail, sets the tag filter on the main grid, and
  // re-fetches so the user sees other photos with that tag.
  // Remove a user tag from this photo (the × on a tag chip).
  const _removeUserTag = async (tag, chip) => {
    const existing = (img.user_tags || img.tags || '').split(',').map(t => t.trim()).filter(Boolean);
    const remaining = existing.filter(e => e.toLowerCase() !== String(tag).toLowerCase());
    const cleaned = remaining.join(', ');
    const ok = await _patchImage(img.id, { tags: cleaned });
    if (!ok) { if (uiModule) uiModule.showError('Failed to remove tag'); return; }
    img.tags = cleaned;
    img.user_tags = cleaned;
    chip.remove();
  };
  detail.querySelectorAll('[data-tag-filter]').forEach(chip => {
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      // × removes the tag (user chips only) instead of filtering.
      if (e.target.closest('.gallery-tag-x')) { _removeUserTag(chip.dataset.tagFilter, chip); return; }
      const tag = chip.dataset.tagFilter;
      if (!tag) return;
      if (!_activeTags.includes(tag)) _activeTags.push(tag);
      _activeAlbum = null;
      _favoritesOnly = false;
      detail.style.display = 'none';
      // Ensure we're looking at the Photos tab.
      const photosTab = document.querySelector('#gallery-modal .gallery-tab[data-tab="images"]');
      photosTab?.click();
      _fetchLibrary(false);
      _renderAlbums();
    });
  });

  // Overflow menu — single ⋮ button on the right that hosts all the action
  // items. Clicking any item closes the menu (per-item handlers also fire).
  const menuBtn = document.getElementById('gallery-detail-menu-btn');
  const menu = document.getElementById('gallery-detail-menu');
  if (menuBtn && menu) {
    // `.dropdown { display:none }` isn't tied to [hidden] — set inline display.
    const _setMenu = (show) => { menu.hidden = !show; menu.style.display = show ? 'block' : 'none'; };
    _setMenu(false);
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _setMenu(menu.hidden);
    });
    menu.addEventListener('click', () => { _setMenu(false); });
    // Click outside closes the menu.
    document.addEventListener('click', (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== menuBtn) _setMenu(false);
    });
  }

  const _toggleDetailFavorite = async () => {
    const res = await fetch(`${API_BASE}/api/gallery/${img.id}/favorite`, {
      method: 'POST', credentials: 'same-origin',
    });
    const data = await res.json();
    if (!data.ok) return;
    img.favorite = data.favorite;
    const menuItem = document.getElementById('gallery-fav-detail');
    if (menuItem) menuItem.innerHTML = data.favorite ? '&#9829; Favorited' : '&#9825; Favorite';
    const headerBtn = document.getElementById('gallery-detail-fav-header');
    if (headerBtn) {
      headerBtn.setAttribute('aria-pressed', data.favorite ? 'true' : 'false');
      headerBtn.setAttribute('title', data.favorite ? 'Unfavorite' : 'Favorite');
      const svg = headerBtn.querySelector('svg');
      if (svg) svg.setAttribute('fill', data.favorite ? 'currentColor' : 'none');
    }
  };
  document.getElementById('gallery-fav-detail').addEventListener('click', _toggleDetailFavorite);
  document.getElementById('gallery-detail-fav-header')?.addEventListener('click', _toggleDetailFavorite);

  document.getElementById('gallery-ai-tag-btn').addEventListener('click', async (e) => {
    // When the photo already has AI tags this button is "Clear AI tags".
    const clearMode = e.currentTarget.dataset.mode === 'clear';
    // The button lives in the ⋮ menu which closes on click, so its text never
    // shows — surface a whirlpool overlay on the image instead.
    const stage = document.getElementById('gallery-detail-image-wrap') || document.getElementById('gallery-detail-img')?.parentElement;
    let overlay = null, spinner = null;
    if (stage) {
      overlay = document.createElement('div');
      overlay.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;gap:8px;align-items:center;justify-content:center;background:color-mix(in srgb, var(--bg) 55%, transparent);z-index:5;';
      try {
        spinner = spinnerModule.createWhirlpool(36);
        spinner.element.style.cssText = 'width:36px;height:36px;margin:0;';
        overlay.appendChild(spinner.element);
        const label = document.createElement('div');
        label.textContent = clearMode ? 'Clearing…' : 'AI tagging…';
        label.style.cssText = 'font-size:11px;opacity:0.7;';
        overlay.appendChild(label);
      } catch (_) { overlay.textContent = clearMode ? 'Clearing…' : 'AI tagging…'; }
      if (getComputedStyle(stage).position === 'static') stage.style.position = 'relative';
      stage.appendChild(overlay);
    }
    const cleanup = () => { try { spinner?.destroy?.(); } catch {} overlay?.remove(); };
    try {
      const url = clearMode
        ? `${API_BASE}/api/gallery/clear-ai-tags?image_id=${encodeURIComponent(img.id)}`
        : `${API_BASE}/api/gallery/${img.id}/ai-tag`;
      const res = await fetch(url, { method: 'POST', credentials: 'same-origin' });
      const data = await res.json();
      cleanup();
      if (data.ok) {
        img.ai_tags = clearMode ? '' : data.ai_tags;
        uiModule.showToast(clearMode ? 'AI tags cleared' : 'AI tags added');
        _openDetail(img); // re-render detail
      } else {
        uiModule.showError(data.error || (clearMode ? 'Clear failed' : 'AI tagging failed'));
      }
    } catch (e2) {
      cleanup();
      uiModule.showError(clearMode ? 'Clear failed' : 'AI tagging failed');
    }
  });

  document.getElementById('gallery-download-btn').addEventListener('click', async () => {
    try {
      const res = await fetch(img.url, { credentials: 'same-origin' });
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objUrl;
      a.download = img.filename || `image-${img.id}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
    } catch (e) {
      // Fallback: direct link
      const a = document.createElement('a');
      a.href = img.url;
      a.download = img.filename || `image-${img.id}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  });

  // Whirlpool while the (newly opened/navigated) image loads — cached images
  // report `complete` immediately, so no spinner flash for those.
  const _imgEl = document.getElementById('gallery-detail-img');
  const _frame = detail.querySelector('.gallery-detail-img-frame');
  if (_imgEl && _frame && _imgEl.tagName === 'IMG' && !_imgEl.complete) {
    const ld = document.createElement('div');
    ld.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:color-mix(in srgb, var(--bg) 40%, transparent);z-index:4;pointer-events:none;border-radius:6px;';
    let _sp = null;
    try { _sp = spinnerModule.createWhirlpool(34); _sp.element.style.cssText = 'width:34px;height:34px;margin:0;'; ld.appendChild(_sp.element); } catch (_) {}
    _frame.appendChild(ld);
    const _done = () => { try { _sp?.destroy?.(); } catch {} ld.remove(); };
    _imgEl.addEventListener('load', _done, { once: true });
    _imgEl.addEventListener('error', _done, { once: true });
  }

  // Prev/Next navigation
  const curIdx = _items.findIndex(i => i.id === img.id);
  const prevBtn = document.getElementById('gallery-detail-prev');
  const nextBtn = document.getElementById('gallery-detail-next');
  if (curIdx <= 0) prevBtn.classList.add('gallery-detail-nav-disabled');
  if (curIdx < 0 || curIdx >= _items.length - 1) nextBtn.classList.add('gallery-detail-nav-disabled');

  prevBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (curIdx > 0) _openDetail(_items[curIdx - 1]);
  });
  nextBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (curIdx >= 0 && curIdx < _items.length - 1) _openDetail(_items[curIdx + 1]);
  });

  // Mobile swipe — horizontal one-finger swipe across the image wrap moves
  // between photos. Skips multi-touch (pinch-zoom) and lets the video
  // controls handle their own touches.
  const wrap = document.getElementById('gallery-detail-image-wrap');
  if (wrap) {
    let sx = 0, sy = 0, st = 0, tracking = false;
    wrap.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) { tracking = false; return; }
      if (e.target.closest('video, button')) { tracking = false; return; }
      const t = e.touches[0];
      sx = t.clientX; sy = t.clientY; st = Date.now();
      tracking = true;
    }, { passive: true });
    wrap.addEventListener('touchend', (e) => {
      if (!tracking) return;
      tracking = false;
      const t = e.changedTouches[0];
      const dx = t.clientX - sx;
      const dy = t.clientY - sy;
      const dt = Date.now() - st;
      // Horizontal flick: > 40px, dominantly horizontal, under 800ms.
      if (dt > 800) return;
      if (Math.abs(dx) < 40) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.2) return;
      if (dx < 0 && curIdx < _items.length - 1) _openDetail(_items[curIdx + 1]);
      else if (dx > 0 && curIdx > 0) _openDetail(_items[curIdx - 1]);
    }, { passive: true });
  }

  const _openInEditor = () => {
    try {
      detail.style.display = 'none';
      const modal = document.getElementById('gallery-modal');
      if (modal) {
        modal.querySelectorAll('.gallery-tab').forEach(t => t.classList.remove('active'));
        modal.querySelector('.gallery-tab[data-tab="editor"]')?.classList.add('active');
      }
      const imagesContainer = document.getElementById('gallery-images-container');
      const albumsContainer = document.getElementById('gallery-albums-container');
      if (imagesContainer) imagesContainer.style.display = 'none';
      if (albumsContainer) albumsContainer.style.display = 'none';
      const _peopleC = document.getElementById('gallery-people-container');
      if (_peopleC) _peopleC.style.display = 'none';
      const _tagsC = document.getElementById('gallery-tags-container');
      if (_tagsC) _tagsC.style.display = 'none';
      const editorContainer = document.getElementById('gallery-editor-container');
      if (editorContainer) editorContainer.style.display = 'flex';
      const baseFilename = (img.filename || '').replace(/\.[^.]+$/, '');
      const label = img.prompt?.trim() || baseFilename || 'Photo';
      // Library photos are read-only: edit a copy, so Save adds a new gallery image.
      openEditor(img.url, img.read_only ? null : img.id, null, label);
    } catch (e) {
      console.error('[edit] failed:', e);
      if (uiModule) uiModule.showError('Failed to open editor: ' + (e?.message || 'unknown'));
    }
  };
  document.getElementById('gallery-edit-btn')?.addEventListener('click', _openInEditor);
  document.getElementById('gallery-edit-direct-btn')?.addEventListener('click', _openInEditor);

  // Rotate — server-side image rotation. Forces a fresh URL afterwards
  // so the browser doesn't show the old cached version. Shows a
  // whirlpool over the detail image while the request + reload are in
  // flight so the user sees the action is processing.
  const _rotate = async (angle) => {
    const stage = document.querySelector('.gallery-detail-img-stage') || document.getElementById('gallery-detail-img')?.parentElement;
    let overlay = null;
    let spinner = null;
    if (stage) {
      overlay = document.createElement('div');
      overlay.className = 'gallery-detail-rotate-loading';
      overlay.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:color-mix(in srgb, var(--bg) 55%, transparent);z-index:5;pointer-events:none;';
      try {
        spinner = spinnerModule.createWhirlpool(36);
        spinner.element.style.cssText = 'width:36px;height:36px;margin:0;';
        overlay.appendChild(spinner.element);
      } catch (_) { overlay.textContent = 'Rotating…'; }
      if (getComputedStyle(stage).position === 'static') stage.style.position = 'relative';
      stage.appendChild(overlay);
    }
    const cleanup = () => {
      try { spinner?.destroy?.(); } catch {}
      overlay?.remove();
    };
    try {
      const r = await fetch(`${API_BASE}/api/gallery/${img.id}/rotate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ angle }),
      });
      if (!r.ok) { cleanup(); uiModule.showError('Rotate failed'); return; }
      // Cache-bust the image in the detail view, then wait for the new
      // image to actually load before clearing the spinner so the user
      // doesn't see a flash of the old/blank image.
      const imgEl = document.getElementById('gallery-detail-img');
      if (imgEl) {
        const newSrc = img.url + (img.url.includes('?') ? '&' : '?') + 't=' + Date.now();
        await new Promise((resolve) => {
          imgEl.onload = imgEl.onerror = () => { imgEl.onload = null; imgEl.onerror = null; resolve(); };
          imgEl.src = newSrc;
        });
      }
      cleanup();
      uiModule.showToast('Rotated');
      _fetchLibrary(false);
    } catch (e) {
      cleanup();
      uiModule.showError('Rotate failed');
    }
  };
  document.getElementById('gallery-rotate-btn')?.addEventListener('click', () => _rotate(90));
  document.getElementById('gallery-rotate-ccw-btn')?.addEventListener('click', () => _rotate(-90));

  // Set as album cover — only present if the photo is currently in an album.
  document.getElementById('gallery-set-cover-btn')?.addEventListener('click', async () => {
    if (!img.album_id) return;
    try {
      const r = await fetch(`${API_BASE}/api/gallery/albums/${img.album_id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ cover_id: img.id }),
      });
      if (r.ok) {
        uiModule.showToast('Album cover updated');
        await _fetchAlbums();
      } else {
        uiModule.showError('Failed to set cover');
      }
    } catch (e) {
      uiModule.showError('Failed to set cover');
    }
  });

  document.getElementById('gallery-delete-btn').addEventListener('click', async () => {
    const _confirmText = img.read_only
      ? 'Hide this photo from the gallery? The original stays untouched in your folder.'
      : 'Delete this photo? This cannot be undone.';
    if (!await uiModule.styledConfirm(_confirmText, { confirmText: img.read_only ? 'Hide' : 'Delete', danger: true })) return;
    const ok = await _deleteImage(img.id);
    if (!ok) {
      uiModule.showError('Failed to delete photo');
      return;
    }
    detail.style.display = 'none';
    _items = _items.filter(i => i.id !== img.id);
    _total = Math.max(0, _total - 1);
    _renderGrid();
    _renderStats();
    if (uiModule) uiModule.showToast('Photo deleted');
  });

  // Tag input — Enter saves; also strips a leading '#' from each tag so
  // typing "#person, #beach" stores as "person, beach".
  // Rename input — saves to the prompt column on Enter/blur via the
  // dedicated rename endpoint.
  const _nameInput = document.getElementById('gallery-detail-name-input');
  if (_nameInput) {
    const _saveName = async () => {
      const newName = _nameInput.value.trim();
      if (newName === (img.prompt || '')) return;
      try {
        const r = await fetch(`${API_BASE}/api/gallery/${img.id}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ name: newName }),
        });
        if (!r.ok) throw new Error('Failed');
        img.prompt = newName;
        if (uiModule) uiModule.showToast('Renamed');
        window.dispatchEvent(new CustomEvent('gallery-refresh'));
      } catch {
        if (uiModule) uiModule.showError('Failed to rename');
      }
    };
    _nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); _saveName(); _nameInput.blur(); }
    });
    _nameInput.addEventListener('blur', _saveName);
  }
  const _tagInput = document.getElementById('gallery-tag-input');
  if (_tagInput) {
    // Wire a tag chip's click-to-filter (same behavior as the chips rendered at
    // open) so chips we add live still work.
    const _wireTagChip = (chip) => {
      chip.addEventListener('click', (e) => {
        e.stopPropagation();
        if (e.target.closest('.gallery-tag-x')) { _removeUserTag(chip.dataset.tagFilter, chip); return; }
        const tag = chip.dataset.tagFilter;
        if (!tag) return;
        if (!_activeTags.includes(tag)) _activeTags.push(tag);
        _activeAlbum = null;
        _favoritesOnly = false;
        detail.style.display = 'none';
        document.querySelector('#gallery-modal .gallery-tab[data-tab="images"]')?.click();
        _fetchLibrary(false);
        _renderAlbums();
      });
    };
    // The input is an ADD field: type a tag, press Enter → it's appended to the
    // photo's tags, the field clears, and a chip appears immediately. No re-render.
    const _addTags = async () => {
      const newTags = _tagInput.value.split(',').map(t => t.trim().replace(/^#+/, '').trim()).filter(Boolean);
      _tagInput.value = '';
      if (!newTags.length) return;
      const existing = (img.user_tags || img.tags || '').split(',').map(t => t.trim()).filter(Boolean);
      const merged = existing.slice();
      const added = [];
      for (const t of newTags) {
        if (!merged.some(e => e.toLowerCase() === t.toLowerCase())) { merged.push(t); added.push(t); }
      }
      if (!added.length) return;
      const cleaned = merged.join(', ');
      const ok = await _patchImage(img.id, { tags: cleaned });
      if (!ok) { if (uiModule) uiModule.showError('Failed to save tags'); return; }
      img.tags = cleaned;
      img.user_tags = cleaned;
      const chips = document.getElementById('gallery-user-tag-chips');
      if (chips) {
        added.forEach(t => {
          const b = document.createElement('button');
          b.className = 'gallery-ai-chip gallery-user-chip';
          b.dataset.tagFilter = t;
          b.title = `Filter to photos tagged “${t}”`;
          b.textContent = t;
          const x = document.createElement('span');
          x.className = 'gallery-tag-x';
          x.title = 'Remove tag';
          x.setAttribute('aria-label', 'Remove tag');
          x.textContent = '×';
          b.appendChild(x);
          chips.appendChild(b);
          _wireTagChip(b);
        });
      }
    };
    _tagInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); _addTags(); }
    });
    // Tap-away on mobile still adds whatever's typed.
    _tagInput.addEventListener('blur', () => { if (_tagInput.value.trim()) _addTags(); });
  }

  document.getElementById('gallery-detail-album').addEventListener('change', async (e) => {
    const albumId = e.target.value;
    const ok = await _patchImage(img.id, { album_id: albumId || '' });
    if (!ok) { uiModule.showError('Failed to update album'); return; }
    img.album_id = albumId || null;
    uiModule.showToast(albumId ? 'Added to album' : 'Removed from album');
  });
}


function _makeGalleryDraggable(content) {
  if (!content) return;
  const header = content.querySelector('.modal-header');
  if (!header) return;
  const modal = content.closest('.modal') || content;
  makeWindowDraggable(modal, { content, header });
}

// ---- Open / Close ----

// Re-export the manager for the rail click handler
import * as Modals from './modalManager.js';

// Module-owned listeners resolve the current picker; closed modals are never retained.
window.addEventListener('ge-ai-command-model-change', (e) => {
  const imageModelSelect = document.getElementById('gallery-image-model');
  if (!imageModelSelect || !imageModelSelect._modelsReady) return;
  let val = (e && e.detail && typeof e.detail.value === 'string') ? e.detail.value : '';
  if (val && ![...imageModelSelect.options].some(o => o.value === val)) {
    if (localStorage.getItem('ge-ai-command-model') === val) localStorage.removeItem('ge-ai-command-model');
    val = '';
  }
  if (imageModelSelect.value !== val) {
    imageModelSelect.value = val;
    if (imageModelSelect.value !== val) imageModelSelect.value = '';
  }
});
window.addEventListener('storage', (e) => {
  const imageModelSelect = document.getElementById('gallery-image-model');
  if (e && e.key === 'ge-ai-command-model' && imageModelSelect?._modelsReady) {
    let val = e.newValue || '';
    if (val && ![...imageModelSelect.options].some(o => o.value === val)) {
      if (localStorage.getItem('ge-ai-command-model') === val) localStorage.removeItem('ge-ai-command-model');
      val = '';
    }
    if (imageModelSelect.value !== val) {
      imageModelSelect.value = val;
      if (imageModelSelect.value !== val) imageModelSelect.value = '';
    }
  }
});


export function openGallery() {
  // If already minimized — restore in place, preserve all state
  if (Modals.isRegistered('gallery-modal') && Modals.isMinimized('gallery-modal')) {
    Modals.restore('gallery-modal');
    return;
  }
  if (_open) {
    // A cancelled lazy-open or an external modal teardown can leave the
    // module flag set after the DOM node has gone away. Do not let that stale
    // flag permanently block opening or refresh restoration.
    if (document.getElementById('gallery-modal')) return;
    _open = false;
  }
  _open = true;
  _galleryCascaded = false;   // replay the domino-in cascade on each open
  let _freshChatUpload = false;
  try {
    const ts = Number(localStorage.getItem('gallery-fresh-chat-upload') || '0');
    _freshChatUpload = ts > 0 && Date.now() - ts < 5 * 60 * 1000;
    if (_freshChatUpload) localStorage.removeItem('gallery-fresh-chat-upload');
  } catch (_) {}
  if (_freshChatUpload) _sort = 'recent';
  // State is preserved across close/reopen — filters, album, sort, items,
  // albums, people — so reopening the gallery feels instant. Use the search
  // input or "All" chip to clear the active filter.
  // Exception: when sort is shuffle, regenerate the seed every open so the
  // user gets a fresh order each visit (the whole point of shuffle). Also
  // CLEAR the cached items so the user doesn't see the stale random order
  // flash up and then swap to the new order when the fetch resolves —
  // skeletons during the brief refetch read as intentional, the swap doesn't.
  if (_sort === 'shuffle') {
    _shuffleSeed = Math.floor(Math.random() * 2 ** 31);
    _items = [];
  }

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'gallery-modal';
  modal.innerHTML = `
    <div class="modal-content gallery-modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>Gallery <span id="gallery-stats" class="memory-count" style="font-size:0.6em;opacity:0.6;font-weight:normal;margin-left:8px"></span></h4>
        <button class="modal-close" id="gallery-close">&times;</button>
      </div>
      <div class="gallery-tabs">
        <button class="gallery-tab active" data-tab="images">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg></span>
          <span class="gallery-tab-label">Photos</span>
        </button>
        <button class="gallery-tab" data-tab="people">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="4"/><path d="M2 21v-1a6 6 0 0 1 6-6h2a6 6 0 0 1 6 6v1"/><path d="M16 4a4 4 0 0 1 0 8"/><path d="M22 21v-1a6 6 0 0 0-4-5.6"/></svg></span>
          <span class="gallery-tab-label">People</span>
        </button>
        <button class="gallery-tab" data-tab="tags">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg></span>
          <span class="gallery-tab-label">Tags</span>
        </button>
        <button class="gallery-tab" data-tab="map">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/></svg></span>
          <span class="gallery-tab-label">Map</span>
        </button>
        <button class="gallery-tab" data-tab="trips">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="8" y1="11" x2="8" y2="16"/><line x1="16" y1="11" x2="16" y2="16"/></svg></span>
          <span class="gallery-tab-label">Trips</span>
        </button>
        <button class="gallery-tab" data-tab="albums">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></span>
          <span class="gallery-tab-label">Albums</span>
        </button>
        <button class="gallery-tab" data-tab="editor" id="gallery-editor-tab">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg></span>
          <span class="gallery-tab-label">Edit</span>
          <span class="gallery-tab-close" id="gallery-editor-tab-close" title="Close edit" aria-label="Close edit">×</span>
        </button>
        <span class="gallery-editor-tabs-extra" id="gallery-editor-tabs-extra"></span>
        <button class="gallery-tab" data-tab="settings">
          <span class="gallery-tab-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></span>
          <span class="gallery-tab-label">Settings</span>
        </button>
      </div>
      <div class="modal-body">
        <div id="gallery-upload-bar" style="display:none;padding:4px 8px 0;">
          <div style="background:var(--border);border-radius:4px;overflow:hidden;height:6px;">
            <div id="gallery-upload-progress" style="height:100%;background:var(--accent, var(--red));width:0%;transition:width 0.2s;"></div>
          </div>
          <div id="gallery-upload-status" style="font-size:10px;opacity:0.5;margin-top:2px;"></div>
        </div>
        <div class="gallery-images-container" id="gallery-images-container" style="margin-top:2px">
        <div class="gallery-album-chips" id="gallery-album-chips"></div>
        <div class="gallery-album-chips gallery-people-chips" id="gallery-people-chips" style="display:none"></div>
        <div class="gallery-toolbar">
          <div class="gallery-search-wrap">
            <input type="text" class="gallery-search" id="gallery-search" placeholder="Search photos, tags..." />
            <button type="button" class="gallery-smart-btn" id="gallery-smart-btn" aria-pressed="false" title="Smart search: find photos by what they show (e.g. “Kinder im Schnee”) instead of by name and tags">✨</button>
            <span class="gallery-search-enter-hint" aria-hidden="true"><svg class="gallery-enter-key" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>to tag</span>
          </div>
          <span class="gallery-toolbar-break" aria-hidden="true"></span>
          <select class="gallery-model-filter" id="gallery-model-filter">
            <option value="">All sources</option>
          </select>
          <select class="gallery-model-filter gallery-folder-filter" id="gallery-folder-filter" title="Library folder" style="display:none"></select>
          <select class="gallery-sort" id="gallery-sort">
            <option value="shuffle"${_sort === 'shuffle' ? ' selected' : ''}>↭ Random order</option>
            <option value="recent"${_sort === 'recent' ? ' selected' : ''}>↓ Newest first</option>
            <option value="oldest"${_sort === 'oldest' ? ' selected' : ''}>↑ Oldest first</option>
            <option value="taken_desc"${_sort === 'taken_desc' ? ' selected' : ''}>↓ Date taken</option>
            <option value="taken_asc"${_sort === 'taken_asc' ? ' selected' : ''}>↑ Date taken</option>
            <option value="nearest" hidden>📍 Nearest first</option>
          </select>
          <button class="gallery-select-btn gallery-toolbar-action" id="gallery-select-btn" title="Select for bulk actions" style="position:relative;top:0;"><span>Select</span></button>
        </div>
        <div class="gallery-album-chips" id="gallery-filter-chips" style="margin-top:0;"></div>
        <div class="memory-bulk-bar gallery-selection-bar hidden" id="gallery-bulk-bar">
          <label class="memory-bulk-check-all" style="position:relative;top:-1px;"><input type="checkbox" id="gallery-bulk-select-all"> All</label>
          <span id="gallery-bulk-count" style="position:relative;top:-1px;">0 selected</span>
          <button class="memory-toolbar-btn" id="gallery-bulk-actions" style="position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px;"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>Actions <span style="opacity:0.55;font-size:9px;">▼</span></button>
          <button class="memory-toolbar-btn" id="gallery-bulk-delete" style="color:var(--red);position:relative;top:-3px;" disabled><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>Delete</button>
          <button class="memory-toolbar-btn" id="gallery-bulk-cancel" title="Cancel (Esc)" style="margin-left:4px;padding:3px 6px;position:relative;top:-3px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
        </div>
        <div class="gallery-tag-chips" id="gallery-tag-chips"></div>
        <div class="gallery-memories" id="gallery-memories" hidden></div>
        <div class="gallery-grid" id="gallery-grid"></div>
        <button class="gallery-load-more" id="gallery-load-more" style="display:none">Load more</button>
        <div class="gallery-detail" id="gallery-detail" style="display:none"></div>
        </div>
        <div class="gallery-albums-container" id="gallery-albums-container" style="display:none;"></div>
        <div class="gallery-people-container" id="gallery-people-container" style="display:none;"></div>
        <div class="gallery-people-container gallery-tags-container" id="gallery-tags-container" style="display:none;"></div>
        <div class="gallery-map-container" id="gallery-map-container" style="display:none;"></div>
        <div class="gallery-people-container gallery-trips-container" id="gallery-trips-container" style="display:none;"></div>
        <div class="gallery-editor-container" id="gallery-editor-container" style="display:none;"></div>
        <div class="gallery-settings-container" id="gallery-settings-container" style="display:none;">
          <div class="admin-card" id="gallery-library-card" style="display:none;">
            <h2>Library folders <span id="gallery-library-count" class="memory-count" style="font-size:0.6em;opacity:0.6;font-weight:normal;"></span></h2>
            <p class="memory-desc doclib-desc">Show photo folders in the gallery where they are. Odysseus only reads them — nothing in these folders is ever changed, moved or deleted. Thumbnails, tags and the smart-search index are kept in Odysseus's own data folder.</p>
            <div id="gallery-sources-list" class="gallery-sources-list"></div>
            <div id="gallery-index-task" class="gallery-index-task"></div>
            <div class="gallery-source-add">
              <input type="text" id="gallery-source-path" class="gallery-tag-input" placeholder="Folder, e.g. H:\\Pictures\\Privat" />
              <input type="text" id="gallery-source-include" class="gallery-tag-input" placeholder="Only these subfolders (optional), e.g. 2005, 2006" />
              <button class="memory-toolbar-btn" id="gallery-source-add-btn">Add folder</button>
            </div>
            <div id="gallery-source-hint" class="gallery-source-hint"></div>
            <div id="gallery-library-job" class="gallery-library-job" style="display:none;">
              <div style="background:var(--border);border-radius:4px;overflow:hidden;height:6px;">
                <div id="gallery-library-job-progress" style="height:100%;background:var(--accent, var(--red));width:0%;transition:width 0.3s;"></div>
              </div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;gap:8px;">
                <div id="gallery-library-job-status" style="font-size:10px;opacity:0.6;"></div>
                <button id="gallery-library-job-cancel" class="gallery-select-btn" style="font-size:10px;padding:1px 6px;">Cancel</button>
              </div>
            </div>
          </div>
          <div class="admin-card">
            <h2>AI Tagging <span id="gallery-tag-count" class="memory-count" style="font-size:0.6em;opacity:0.6;font-weight:normal;"></span></h2>
            <p class="memory-desc doclib-desc">Auto-tag photos by content with your <a href="#" id="gallery-vision-link" class="ge-vision-link">vision model</a>. Your own tags are kept.</p>
            <div class="gallery-tag-model-row" id="gallery-tag-model-row" style="display:none;">
              <label for="gallery-tag-model">Tagging model</label>
              <select id="gallery-tag-model"><option value="">Same as the Vision model</option></select>
            </div>
            <div id="gallery-tag-bar" style="display:none;padding:8px 0 0;">
              <div style="background:var(--border);border-radius:4px;overflow:hidden;height:6px;">
                <div id="gallery-tag-progress" style="height:100%;background:var(--accent, var(--red));width:0%;transition:width 0.2s;"></div>
              </div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
                <div id="gallery-tag-status" style="font-size:10px;opacity:0.5;"></div>
                <button id="gallery-tag-cancel" class="gallery-select-btn" style="font-size:10px;padding:1px 6px;">Cancel</button>
              </div>
            </div>
            <div class="memory-toolbar" style="display:flex;flex-direction:row;gap:6px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:32px;">
              <button class="memory-toolbar-btn" id="gallery-clear-ai-tags-btn" title="Remove all AI-generated tags from every photo">Clear AI tags</button>
              <button class="memory-toolbar-btn" id="gallery-tag-all-btn" title="AI-tag all untagged photos (in the current album, if any)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-2px;margin-right:5px;"><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
                Start AI tag
              </button>
            </div>
          </div>
          <div class="admin-card">
            <h2>Image model</h2>
            <p class="memory-desc doclib-desc">Model used for AI edits and generated content in the editor (Quick Edit). Shared with the Quick Edit model picker.</p>
            <select id="gallery-image-model" style="margin-top:8px;width:100%;">
              <option value="">Loading…</option>
            </select>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  Modals.register('gallery-modal', {
    railBtnId: 'rail-gallery',
    sidebarBtnId: 'tool-gallery-btn',
    closeFn: () => _doCloseGallery(),
    restoreFn: () => {},
  });

  // Allow dragging the modal by its header — same pattern as Email Library,
  // Sessions, etc. The tileManager (corner/edge snap-tiling) listens on
  // pointer events too; it only shows a ghost on move and snaps on release,
  // so the two coexist.
  _makeGalleryDraggable(modal.querySelector('.modal-content'));

  document.getElementById('gallery-close').addEventListener('click', async () => {
    if (isEditorOpen()) {
      const ok = await uiModule.styledConfirm(
        'Close Gallery and the active edit?',
        { confirmText: 'Close', danger: true },
      );
      if (!ok) return;
      window.__galleryAllowCloseEditor = true;
    }
    closeGallery();
  });

  // Double-click the Edit tab to rename what's being edited. The label
  // shows up everywhere it's referenced by id (#gallery-editor-tab), so a
  // simple inline contenteditable swap is enough.
  const editorTab = modal.querySelector('.gallery-tab[data-tab="editor"]');
  // Close × on the Edit tab — appears on hover. Confirms if the editor
  // has an open session (any in-progress edit), otherwise just closes.
  const editorTabClose = modal.querySelector('#gallery-editor-tab-close');
  if (editorTabClose) {
    editorTabClose.addEventListener('click', async (e) => {
      e.stopPropagation();
      // With several document tabs open the × closes only the active one and
      // switches to its neighbour; the editor itself stays open.
      if (window.geEditorCloseActiveDocument?.()) return;
      if (isEditorOpen()) {
        const ok = await uiModule.styledConfirm(
          'Close the edit? Any unsaved changes will be lost.',
          { confirmText: 'Close', danger: true },
        );
        if (!ok) return;
      }
      window.__galleryAllowCloseEditor = true;
      closeEditor();
      window.__galleryAllowCloseEditor = false;
      // If user is currently on the Edit tab, swap back to Photos.
      const activeTab = modal.querySelector('.gallery-tab.active');
      if (activeTab?.dataset.tab === 'editor') {
        modal.querySelector('.gallery-tab[data-tab="images"]')?.click();
      }
    });
  }
  if (editorTab) {
    editorTab.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      const labelEl = editorTab.querySelector('.gallery-tab-label') || editorTab;
      const current = labelEl.textContent.replace(/^Edit:\s*/, '');
      const oldText = labelEl.textContent;
      const input = document.createElement('input');
      input.type = 'text';
      input.value = current === 'Edit' ? '' : current;
      input.placeholder = 'Edit name';
      input.className = 'gallery-tab-rename-input';
      // Replace only the label span's contents so the icon SVG next to
      // it stays visible during the rename.
      labelEl.textContent = '';
      labelEl.appendChild(input);
      input.focus();
      input.select();
      const finish = (commit) => {
        if (commit && input.value.trim()) {
          labelEl.textContent = `Edit: ${input.value.trim().slice(0, 24)}`;
        } else {
          labelEl.textContent = oldText;
        }
      };
      input.addEventListener('blur', () => finish(true));
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
      });
    });
  }

  // ── Tab switching ──
  modal.querySelectorAll('.gallery-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      modal.querySelectorAll('.gallery-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.tab;
      // Always close the photo detail when changing tabs — leaving it open
      // means it pops back the next time the user returns to Photos.
      const _detail = document.getElementById('gallery-detail');
      if (_detail) _detail.style.display = 'none';
      const imagesContainer = document.getElementById('gallery-images-container');
      const albumsContainer = document.getElementById('gallery-albums-container');
      const editorContainer = document.getElementById('gallery-editor-container');
      const settingsContainer = document.getElementById('gallery-settings-container');
      if (imagesContainer) imagesContainer.style.display = target === 'images' ? '' : 'none';
      if (albumsContainer) albumsContainer.style.display = target === 'albums' ? '' : 'none';
      const peopleContainer = document.getElementById('gallery-people-container');
      if (peopleContainer) peopleContainer.style.display = target === 'people' ? '' : 'none';
      const tagsContainer = document.getElementById('gallery-tags-container');
      if (tagsContainer) tagsContainer.style.display = target === 'tags' ? '' : 'none';
      const mapContainer = document.getElementById('gallery-map-container');
      if (mapContainer) mapContainer.style.display = target === 'map' ? '' : 'none';
      const tripsContainer = document.getElementById('gallery-trips-container');
      if (tripsContainer) tripsContainer.style.display = target === 'trips' ? '' : 'none';
      if (editorContainer) editorContainer.style.display = target === 'editor' ? 'flex' : 'none';
      if (settingsContainer) settingsContainer.style.display = target === 'settings' ? '' : 'none';
      if (target === 'images') {
        // Keep active edits alive when leaving the Edit tab. The edit
        // session is only torn down by the explicit Edit-tab close.
      } else if (target === 'albums') {
        _renderAlbumsTab();
      } else if (target === 'people') {
        _peopleView = null;
        _renderPeopleTab();
      } else if (target === 'tags') {
        _renderTagsTab();
      } else if (target === 'map') {
        _renderMapTab();
      } else if (target === 'trips') {
        _renderTripsTab();
      } else if (target === 'settings') {
        _loadLibrarySources();
        _loadTagModelPicker();
      } else if (target === 'editor') {
        // If the editor isn't already holding an image, render a chooser so the
        // tab does something useful instead of opening an empty grey pane.
        if (!isEditorOpen()) _renderEditorLanding();
      }
    });
  });

  const searchInput = document.getElementById('gallery-search');
  _wirePeopleSuggest(searchInput);   // before the Enter-to-tag handler, so a picked name wins
  searchInput.addEventListener('input', () => {
    clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(() => {
      _search = searchInput.value.trim();
      _fetchLibrary(false);
    }, _smart ? 600 : 300);
  });
  // Pressing Enter in the search box converts the current query into a
  // stacked tag-filter pill (strips a leading "#"), clears the box, and
  // re-fetches. Lets the user keep narrowing by tag without having to
  // click chips.
  searchInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (_smart) {
      // Smart search: Enter runs the search now instead of turning the text into a tag.
      clearTimeout(_searchDebounce);
      _search = searchInput.value.trim();
      _fetchLibrary(false);
      _renderAlbums();
      return;
    }
    const raw = searchInput.value.trim().replace(/^#/, '');
    if (!raw) return;
    if (!_activeTags.includes(raw)) _activeTags.push(raw);
    // Searching a tag swaps the active filter to All (clear Favorites/album) so
    // the heart deactivates and All lights up.
    _favoritesOnly = false;
    _activeAlbum = null;
    // Filtering by a tag reads best newest-first — switch the sort to Recent.
    if (_sort !== 'recent') {
      _sort = 'recent';
      const sortSel = document.getElementById('gallery-sort');
      if (sortSel) sortSel.value = 'recent';
    }
    searchInput.value = '';
    _search = '';
    clearTimeout(_searchDebounce);
    _fetchLibrary(false);
    _renderAlbums();
  });

  document.getElementById('gallery-sort').addEventListener('change', (e) => {
    _sort = e.target.value;
    // New shuffle session each time the user picks Shuffle so it actually
    // re-shuffles, not just re-renders the same seeded order.
    if (_sort === 'shuffle') _shuffleSeed = Math.floor(Math.random() * 2 ** 31);
    _fetchLibrary(false);
  });

  document.getElementById('gallery-model-filter').addEventListener('change', (e) => {
    _activeModel = e.target.value || null;
    _fetchLibrary(false);
  });

  document.getElementById('gallery-folder-filter').addEventListener('change', (e) => {
    _setFolderFilter(e.target.value);
  });
  document.getElementById('gallery-smart-btn').addEventListener('click', () => {
    _smart = !_smart;
    _syncSmartButton();
    // Have the server load its search model while the user types the query.
    if (_smart) fetch(`${API_BASE}/api/gallery/smart-search/warmup`, { method: 'POST', credentials: 'same-origin' }).catch(() => {});
    if (_search) _fetchLibrary(false);
    _renderAlbums();
    document.getElementById('gallery-search')?.focus();
  });
  _syncSmartButton();
  _wireLibrarySettings();

  document.getElementById('gallery-load-more').addEventListener('click', () => {
    _offset = _items.length;
    _fetchLibrary(true);
  });

  // Infinite scroll: auto-load the next page when the Load-more button nears the
  // bottom of the gallery's scroll area. The button stays as a manual fallback.
  // Infinite scroll. A capture-phase scroll listener on document catches scroll
  // from WHICHEVER element actually scrolls (desktop modal-body or the mobile
  // scroll wrapper — IntersectionObserver's root was unreliable across the two).
  // We just test the Load-more button against the viewport bottom.
  let _loadingMore = false;
  let _scrollTick = false;
  const _maybeAutoLoad = () => {
    _scrollTick = false;
    if (!_open || _loadingMore || _items.length >= _total) return;
    const btn = document.getElementById('gallery-load-more');
    if (!btn || btn.style.display === 'none' || !btn.offsetParent) return;  // hidden / nothing more
    const r = btn.getBoundingClientRect();
    if (r.top <= window.innerHeight + 600) {   // within 600px of the viewport bottom
      _loadingMore = true;
      _offset = _items.length;
      Promise.resolve(_fetchLibrary(true)).finally(() => { _loadingMore = false; });
    }
  };
  document.addEventListener('scroll', () => {
    if (_scrollTick) return;
    _scrollTick = true;
    requestAnimationFrame(_maybeAutoLoad);
  }, true);

  // When the window grows (e.g. entering fullscreen), the visible grid
  // can hold more photos than the last page fetched — top up so there's
  // no blank space. Debounced; only fires when the freshly-computed
  // page size exceeds what's loaded and the server has more to give.
  let _resizeTopUpTimer = null;
  const _onGalleryResize = () => {
    clearTimeout(_resizeTopUpTimer);
    _resizeTopUpTimer = setTimeout(() => {
      if (!_open) return;
      if (_items.length >= _total) return;        // already have everything
      if (_computeFetchLimit() <= _items.length) return; // viewport not bigger than current load
      _offset = _items.length;
      _fetchLibrary(true);
    }, 300);
  };
  window.addEventListener('resize', _onGalleryResize);
  // Remember the handler so closeGallery can detach it.
  _galleryResizeHandler = _onGalleryResize;

  // ── Import images ──

  // "vision model" link → open Settings on the AI tab (where the vision model
  // is configured).
  const visionLink = document.getElementById('gallery-vision-link');
  if (visionLink) {
    visionLink.addEventListener('click', (e) => {
      e.preventDefault();
      import('./settings.js?v=20260912writingstyle3').then(m => {
        m.open('ai');
        // The gallery modal gets a bumped z-index from modalManager; settings
        // opens with its lower static z-index and lands BEHIND it. Raise it above.
        const sm = document.getElementById('settings-modal');
        const gm = document.getElementById('gallery-modal');
        if (sm) {
          const gz = gm ? (parseInt(getComputedStyle(gm).zIndex) || 0) : 0;
          sm.style.setProperty('z-index', String(Math.max(gz + 1, 10050)), 'important');
        }
      }).catch(() => {});
    });
  }

  // ── Image model (Settings tab) ──
  // Mirror of the Quick Edit picker: same source (GET /api/editor/image-models)
  // and the SAME localStorage key `ge-ai-command-model` (value `<endpoint_id>::<id>`)
  // so the two stay in sync.
  const _GE_MODEL_KEY = 'ge-ai-command-model';
  const imageModelSelect = document.getElementById('gallery-image-model');
  if (imageModelSelect) {
    const _fillImageModels = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/editor/image-models`, { credentials: 'same-origin' });
        const data = r.ok ? await r.json() : null;
        const models = (data && Array.isArray(data.models)) ? data.models : [];
        const rawSaved = localStorage.getItem(_GE_MODEL_KEY) || '';
        let saved = rawSaved;
        imageModelSelect.innerHTML = '';
        const def = document.createElement('option');
        def.value = '';
        def.textContent = 'Default (configured provider)';
        imageModelSelect.appendChild(def);
        for (const m of models) {
          const opt = document.createElement('option');
          const endpointKey = m.endpoint_id ? String(m.endpoint_id) : (m.source === 'local' ? 'local' : '');
          opt.value = endpointKey ? `${endpointKey}::${m.id}` : m.id;
          opt.textContent = `${m.id} · ${m.provider || ''} (${m.kind || 'edit'})`.trim();
          imageModelSelect.appendChild(opt);
        }
        imageModelSelect._modelsReady = true;
        // Invalidate or migrate legacy localStorage values containing URLs or leading ::
        if (saved.startsWith('http://') || saved.startsWith('https://') || saved.includes('://') || saved.startsWith('::')) {
          const modelPart = saved.includes('::') ? saved.split('::').pop() : '';
          const matching = models.filter(m => m.id === modelPart);
          if (matching.length === 1) {
            const epKey = matching[0].endpoint_id ? String(matching[0].endpoint_id) : (matching[0].source === 'local' ? 'local' : '');
            saved = epKey ? `${epKey}::${matching[0].id}` : matching[0].id;
          } else {
            saved = '';
          }
          try {
            if (saved) localStorage.setItem(_GE_MODEL_KEY, saved);
            else localStorage.removeItem(_GE_MODEL_KEY);
          } catch (_) {}
        }
        imageModelSelect.value = saved;
        if (imageModelSelect.value !== saved) imageModelSelect.value = '';
        if (imageModelSelect.value !== rawSaved) {
          if (imageModelSelect.value) localStorage.setItem(_GE_MODEL_KEY, imageModelSelect.value);
          else localStorage.removeItem(_GE_MODEL_KEY);
          window.dispatchEvent(new CustomEvent('ge-ai-command-model-change', { detail: { value: imageModelSelect.value } }));
        }
      } catch (e) {
        imageModelSelect.innerHTML = '<option value="">Default (configured provider)</option>';
      }
    };
    imageModelSelect.addEventListener('change', () => {
      const val = imageModelSelect.value || '';
      try {
        if (val) localStorage.setItem(_GE_MODEL_KEY, val);
        else localStorage.removeItem(_GE_MODEL_KEY);
      } catch (e) {}
      window.dispatchEvent(new CustomEvent('ge-ai-command-model-change', { detail: { value: val } }));
    });
    _fillImageModels();
  }


  // ── Tag All Untagged ──
  let _tagCancelRequested = false;
  let _tagging = false;
  const tagAllBtn = document.getElementById('gallery-tag-all-btn');
  const _tagAllOrigHTML = tagAllBtn ? tagAllBtn.innerHTML : '';
  if (tagAllBtn) {
    tagAllBtn.addEventListener('click', async () => {
      // While a run is active this button acts as Cancel.
      if (_tagging) {
        _tagCancelRequested = true;
        const _se = document.getElementById('gallery-tag-status');
        if (_se) _se.textContent = 'Cancelling…';
        tagAllBtn.textContent = 'Cancelling…';
        tagAllBtn.disabled = true;
        return;
      }
      if (tagAllBtn.disabled) return;
      const scope = _activeAlbum
        ? (_albums.find(a => a.id === _activeAlbum)?.name || 'this album')
        : 'entire gallery';
      const params = new URLSearchParams();
      if (_activeAlbum) params.set('album_id', _activeAlbum);
      let listRes;
      try {
        const r = await fetch(`${API_BASE}/api/gallery/ai-tag-batch?${params.toString()}`, {
          method: 'POST', credentials: 'same-origin',
        });
        listRes = await r.json();
      } catch (e) { uiModule.showError('Failed to fetch tag queue'); return; }
      if (!listRes.ok || !Array.isArray(listRes.image_ids) || listRes.image_ids.length === 0) {
        uiModule.showToast(`No untagged photos in ${scope}`);
        return;
      }
      const total = listRes.image_ids.length;
      const untagged = listRes.total_untagged || total;
      if (!await uiModule.styledConfirm(
        `Tag ${total} of ${untagged} untagged photo${total > 1 ? 's' : ''} in ${scope}?`,
        { confirmText: 'Tag All' }
      )) return;

      const bar = document.getElementById('gallery-tag-bar');
      const progEl = document.getElementById('gallery-tag-progress');
      const statusEl = document.getElementById('gallery-tag-status');
      const cancelBtn = document.getElementById('gallery-tag-cancel');
      bar.style.display = '';
      progEl.style.width = '0%';
      // The Start button becomes the Cancel control for the run (kept enabled so
      // it's clickable; the click handler above routes to cancel via _tagging).
      _tagging = true;
      _tagCancelRequested = false;
      tagAllBtn.classList.add('active', 'gallery-tag-cancelling');
      tagAllBtn.textContent = 'Cancel';
      if (cancelBtn) cancelBtn.style.display = 'none';   // start button covers it now
      cancelBtn.onclick = () => { _tagCancelRequested = true; statusEl.textContent = 'Cancelling...'; };

      let done = 0, failed = 0;
      for (const id of listRes.image_ids) {
        if (_tagCancelRequested) break;
        try {
          const r = await fetch(`${API_BASE}/api/gallery/${id}/ai-tag`, {
            method: 'POST', credentials: 'same-origin',
          });
          const d = await r.json();
          if (!d.ok) failed++;
        } catch (_) { failed++; }
        done++;
        progEl.style.width = `${Math.round((done / total) * 100)}%`;
        statusEl.textContent = `Tagging ${done}/${total}${failed ? ` — ${failed} failed` : ''}`;
      }

      statusEl.textContent = _tagCancelRequested
        ? `Cancelled after ${done}/${total}${failed ? ` (${failed} failed)` : ''}`
        : `Done — tagged ${done - failed}/${total}${failed ? ` (${failed} failed)` : ''}`;
      // Restore the Start button.
      _tagging = false;
      tagAllBtn.disabled = false;
      tagAllBtn.classList.remove('active', 'gallery-tag-cancelling');
      tagAllBtn.innerHTML = _tagAllOrigHTML;
      if (cancelBtn) cancelBtn.style.display = '';
      setTimeout(() => { bar.style.display = 'none'; }, 3000);
      await _fetchLibrary(false);
      if (uiModule) uiModule.showToast(`Tagged ${done - failed} photo${(done - failed) !== 1 ? 's' : ''}`);
    });
  }

  // ── Toolbar overflow (⋮) ──
  const moreBtn = document.getElementById('gallery-toolbar-more-btn');
  const moreMenu = document.getElementById('gallery-toolbar-more-menu');
  if (moreBtn && moreMenu) {
    // `.dropdown { display:none }` isn't tied to [hidden], so toggling the
    // attribute alone won't reveal it — set inline display too (inline wins).
    const _setMore = (show) => { moreMenu.hidden = !show; moreMenu.style.display = show ? 'block' : 'none'; };
    _setMore(false);
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _setMore(moreMenu.hidden);
    });
    document.addEventListener('click', (e) => {
      if (!moreMenu.hidden && !moreMenu.contains(e.target) && e.target !== moreBtn) _setMore(false);
    });
  }

  // ── Clear AI Tags ──
  const clearAiTagsBtn = document.getElementById('gallery-clear-ai-tags-btn');
  if (clearAiTagsBtn) {
    clearAiTagsBtn.addEventListener('click', async () => {
      if (clearAiTagsBtn.disabled) return;
      if (moreMenu) { moreMenu.hidden = true; moreMenu.style.display = 'none'; }
      if (!await uiModule.styledConfirm(
        'Remove all AI-generated tags from every photo? Your own tags are kept.',
        { confirmText: 'Clear AI Tags', danger: true }
      )) return;
      clearAiTagsBtn.disabled = true;
      try {
        const r = await fetch(`${API_BASE}/api/gallery/clear-ai-tags`, {
          method: 'POST', credentials: 'same-origin',
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || 'Clear failed');
        uiModule.showToast(`Cleared AI tags on ${d.cleared} photo${d.cleared === 1 ? '' : 's'}`);
        await _fetchLibrary(false);
      } catch (e) {
        uiModule.showError(`Failed to clear AI tags: ${e.message || e}`);
      } finally {
        clearAiTagsBtn.disabled = false;
      }
    });
  }


  // ── Select mode + bulk delete ──
  let _selectMode = false;
  const selectBtn = document.getElementById('gallery-select-btn');
  const bulkBar = document.getElementById('gallery-bulk-bar');

  const _selectedDots = () => [...document.querySelectorAll('.gallery-select-dot.selected')];
  const _selectedIds = () => _selectedDots().map(d => d.closest('.gallery-card')?.dataset.id).filter(Boolean);

  function _updateBulkCount() {
    const sel = document.querySelectorAll('.gallery-select-dot.selected').length;
    const total = document.querySelectorAll('.gallery-select-dot').length;
    const el = document.getElementById('gallery-bulk-count');
    if (el) el.textContent = sel + ' selected';
    const all = document.getElementById('gallery-bulk-select-all');
    if (all) { all.checked = total > 0 && sel === total; all.indeterminate = sel > 0 && sel < total; }
    // When something's selected, brighten Actions to the same full --fg color as
    // the "N selected" count (the button is a dimmer 60% --fg by default).
    const actions = document.getElementById('gallery-bulk-actions');
    if (actions) actions.style.color = sel > 0 ? 'var(--fg)' : '';
    const deleteBtn = document.getElementById('gallery-bulk-delete');
    if (deleteBtn) deleteBtn.disabled = sel === 0;
  }

  function _setSelectMode(on) {
    _selectMode = on;
    selectBtn.classList.toggle('active', on);
    // The Select button doubles as Cancel while active (mirrors the library).
    selectBtn.textContent = on ? 'Cancel' : 'Select';
    bulkBar.classList.toggle('hidden', !on);
    // Body-level signal so the CSS rule that hides per-thumbnail overlay
    // buttons (favorite/download) applies to every card — including cards
    // rendered after select mode was already on (load-more etc).
    document.body.classList.toggle('gallery-selecting', on);
    document.querySelectorAll('.gallery-select-dot').forEach(d => {
      d.style.display = on ? '' : 'none';
      if (!on) d.classList.remove('selected');
      d.closest('.gallery-card')?.classList.toggle('gallery-card-selectable', on);
    });
    if (!on) document.querySelectorAll('.gallery-bulk-menu').forEach(m => m.remove());
    _updateBulkCount();
  }

  function _exitSelectMode() { _setSelectMode(false); }

  // Gallery has three bulk-select surfaces. Escape leaves whichever one is
  // active before the app-level modal handler can close the Gallery itself.
  window.__galleryCancelSelection = () => {
    if (_selectMode) {
      _exitSelectMode();
      return true;
    }
    if (_albumSelectMode) {
      _setAlbumSelectMode(false);
      return true;
    }
    if (_draftsSelectMode) {
      _draftsSelectMode = false;
      _draftsSelected.clear();
      _draftsPaint();
      return true;
    }
    return false;
  };

  selectBtn.addEventListener('click', () => _setSelectMode(!_selectMode));
  document.getElementById('gallery-bulk-cancel')?.addEventListener('click', () => _exitSelectMode());

  // Select all / none.
  document.getElementById('gallery-bulk-select-all')?.addEventListener('change', (e) => {
    const on = e.target.checked;
    document.querySelectorAll('.gallery-select-dot').forEach(d => d.classList.toggle('selected', on));
    _updateBulkCount();
  });

  document.getElementById('gallery-grid').addEventListener('click', (e) => {
    if (!_selectMode) return;
    const card = e.target.closest('.gallery-card');
    if (!card) return;
    e.stopPropagation();
    const dot = card.querySelector('.gallery-select-dot');
    if (dot) { dot.classList.toggle('selected'); _updateBulkCount(); }
  });

  // Mobile: long-press on a thumbnail enters select mode and marks that
  // thumbnail as the first selection. Cancelled by movement (so vertical
  // scrolling still works) or by lifting before the timer fires.
  if ('ontouchstart' in window) {
    const gridEl = document.getElementById('gallery-grid');
    let lpTimer = null;
    let lpCard = null;
    let lpStartX = 0, lpStartY = 0;
    let lpFired = false;
    const LONG_PRESS_MS = 380;
    const MOVE_CANCEL_PX = 10;
    const cancel = () => { clearTimeout(lpTimer); lpTimer = null; lpCard = null; };
    gridEl.addEventListener('touchstart', (e) => {
      const card = e.target.closest('.gallery-card');
      if (!card) return;
      const t = e.touches[0];
      lpCard = card;
      lpStartX = t.clientX; lpStartY = t.clientY;
      lpFired = false;
      lpTimer = setTimeout(() => {
        if (!lpCard) return;
        lpFired = true;
        try { if (navigator.vibrate) navigator.vibrate(15); } catch {}
        if (!_selectMode) _setSelectMode(true);
        const dot = lpCard.querySelector('.gallery-select-dot');
        if (dot && !dot.classList.contains('selected')) {
          dot.classList.add('selected');
          _updateBulkCount();
        }
      }, LONG_PRESS_MS);
    }, { passive: true });
    gridEl.addEventListener('touchmove', (e) => {
      if (!lpCard) return;
      const t = e.touches[0];
      if (Math.abs(t.clientX - lpStartX) > MOVE_CANCEL_PX
        || Math.abs(t.clientY - lpStartY) > MOVE_CANCEL_PX) cancel();
    }, { passive: true });
    gridEl.addEventListener('touchend', (e) => {
      if (lpFired) {
        // Swallow the synthesized click that follows the long-press so the
        // tap doesn't immediately toggle the same dot back off.
        e.preventDefault();
      }
      cancel();
    });
    gridEl.addEventListener('touchcancel', cancel);
  }

  // Drag-and-drop import
  const grid = document.getElementById('gallery-grid');
  const imagesContainer = document.getElementById('gallery-images-container');
  ['dragenter', 'dragover'].forEach(ev => {
    imagesContainer.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); grid.classList.add('gallery-dragover'); });
  });
  ['dragleave', 'drop'].forEach(ev => {
    imagesContainer.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); grid.classList.remove('gallery-dragover'); });
  });
  imagesContainer.addEventListener('drop', (e) => {
    _handleGalleryDrop(e).catch(err => console.error('Gallery drop error:', err));
  });

  // Same drop handling on the Albums tab: dropped folders become new albums,
  // loose files go into whatever album is currently active (or stay loose).
  const albumsContainer = document.getElementById('gallery-albums-container');
  if (albumsContainer) {
    ['dragenter', 'dragover'].forEach(ev => {
      albumsContainer.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        albumsContainer.classList.add('gallery-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(ev => {
      albumsContainer.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        albumsContainer.classList.remove('gallery-dragover');
      });
    });
    albumsContainer.addEventListener('drop', (e) => {
      _handleGalleryDrop(e)
        .then(() => _renderAlbumsTab())
        .catch(err => console.error('Gallery drop error:', err));
    });
  }


  // ── Bulk actions menu (Favorite / Add tag / Delete on the selection) ──
  // Built dynamically like the email library's _showBulkActionsMenu so it
  // shares the exact same dropdown style/behaviour.
  const _bulkActionsBtn = document.getElementById('gallery-bulk-actions');
  function _showGalleryBulkMenu(anchor) {
    document.querySelectorAll('.gallery-bulk-menu').forEach(dismissOrRemove);
    // Standard Odysseus dropdown (.dropdown + dropdown-item-compact) so it
    // matches every other menu in the app. Positioned fixed at the button.
    const dropdown = document.createElement('div');
    dropdown.className = 'dropdown gallery-bulk-menu';
    const rect = anchor.getBoundingClientRect();
    const left = Math.min(rect.left, window.innerWidth - 200);
    // Inline the standard dropdown look so it renders correctly even where the
    // `.dropdown` rule is scoped out (e.g. hover-only media queries on mobile).
    dropdown.style.cssText = `position:fixed;display:block;z-index:${topPortalZ()};top:${rect.bottom + 6}px;left:${Math.max(8, left)}px;right:auto;min-width:180px;background:var(--panel,var(--bg));border:1px solid var(--border);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,0.3);padding:6px;font-size:11px;`;
    const _favIco = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 21s-6.7-4.35-9.33-8.04C.9 10.3 1.4 6.9 4.1 5.6c1.9-.9 4 .03 5 1.7 1-1.67 3.1-2.6 5-1.7 2.7 1.3 3.2 4.7 1.43 7.36C18.7 16.65 12 21 12 21z"/></svg>';
    const _tagIco = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>';
    const _dlIco = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
    const _delIco = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';
    const _cancelIco = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    const items = [
      { label: 'Favorite', icon: _favIco, action: () => _bulkFavorite(_selectedIds()) },
      { label: 'Add tag…', icon: _tagIco, action: () => _bulkTag(_selectedIds()) },
      { label: 'Download', icon: _dlIco, action: () => _bulkDownload(_selectedIds()) },
      { separator: true },
      { label: 'Delete', icon: _delIco, danger: true, action: () => _bulkDelete(_selectedIds()) },
      { label: 'Cancel', icon: _cancelIco, action: () => _exitSelectMode() },
    ];
    for (const a of items) {
      if (a.separator) {
        const sep = document.createElement('div');
        sep.className = 'dropdown-divider';
        sep.style.cssText = 'height:1px;background:var(--border);margin:4px 4px;';
        dropdown.appendChild(sep);
        continue;
      }
      const it = document.createElement('div');
      it.className = 'dropdown-item-compact' + (a.danger ? ' dropdown-item-danger' : '');
      it.innerHTML = `<span class="dropdown-icon">${a.icon}</span><span>${a.label}</span>`;
      it.addEventListener('click', (e) => { e.stopPropagation(); close(); a.action(); });
      dropdown.appendChild(it);
    }
    document.body.appendChild(dropdown);
    const close = bindMenuDismiss(dropdown, () => { dropdown.remove(); }, (ev) => !dropdown.contains(ev.target) && ev.target !== anchor);
  }

  _bulkActionsBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    // Toggle: clicking the Actions button while its dropdown is already open
    // should close it. The outside-click handler explicitly skips clicks on
    // the anchor, so the button itself has to do its own dismiss.
    const existing = document.querySelector('.gallery-bulk-menu');
    if (existing) { dismissOrRemove(existing); return; }
    if (!_selectedIds().length) { uiModule.showToast('Select photos first'); return; }
    _showGalleryBulkMenu(e.currentTarget);
  });
  document.getElementById('gallery-bulk-delete')?.addEventListener('click', () => {
    _bulkDelete(_selectedIds());
  });

  async function _bulkDelete(ids) {
    if (!ids.length) return;
    if (!await uiModule.styledConfirm(`Delete ${ids.length} photo${ids.length > 1 ? 's' : ''}? This cannot be undone.`, { confirmText: 'Delete', danger: true })) return;
    const deleted = [], failed = [];
    for (const id of ids) { const ok = await _deleteImage(id); (ok ? deleted : failed).push(id); }
    if (failed.length) uiModule.showError(`Failed to delete ${failed.length} of ${ids.length} photos`);
    _items = _items.filter(i => !deleted.includes(i.id));
    _total = Math.max(0, _total - deleted.length);
    _exitSelectMode();
    if (uiModule) uiModule.showToast(`${deleted.length} photo${deleted.length > 1 ? 's' : ''} deleted`);
    // If we just emptied a FILTERED view (e.g. deleted every photo under a tag),
    // drop the filters and reload the full library so the user isn't stranded on
    // a blank screen with a now-empty tag/album/favorites filter still active.
    if (_items.length === 0 && (_activeTags.length || _activeAlbum || _favoritesOnly)) {
      _activeTags = [];
      _activeAlbum = null;
      _favoritesOnly = false;
      _fetchLibrary(false);
      _renderAlbums();
      return;
    }
    _renderGrid(); _renderStats();
  }

  async function _bulkDownload(ids) {
    if (!ids.length) return;
    // More than 5 → bundle into a single .zip server-side instead of firing a
    // flood of individual downloads.
    if (ids.length > 5) {
      try {
        if (uiModule) uiModule.showToast(`Zipping ${ids.length} photos…`);
        const res = await fetch(`${API_BASE}/api/gallery/download-zip`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
        });
        if (!res.ok) throw new Error('zip failed');
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = 'gallery-photos.zip';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 2000);
        _exitSelectMode();
        if (uiModule) uiModule.showToast(`Downloaded ${ids.length} photos (zip)`);
      } catch (e) {
        if (uiModule) uiModule.showError('Failed to create zip');
      }
      return;
    }
    // 5 or fewer → individual downloads.
    let n = 0;
    for (const id of ids) {
      const it = _items.find(i => i.id === id);
      if (!it) continue;
      try {
        const res = await fetch(it.url, { credentials: 'same-origin' });
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = it.filename || `image-${it.id}.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
        n++;
        // Stagger so the browser doesn't drop simultaneous downloads.
        await new Promise(r => setTimeout(r, 250));
      } catch (_) { /* skip failures */ }
    }
    _exitSelectMode();
    if (uiModule) uiModule.showToast(`Downloading ${n} photo${n === 1 ? '' : 's'}`);
  }

  async function _bulkFavorite(ids) {
    let n = 0;
    for (const id of ids) {
      if (await _patchImage(id, { favorite: true })) {
        n++;
        const it = _items.find(i => i.id === id); if (it) it.favorite = true;
      }
    }
    _renderGrid(); _exitSelectMode();
    if (uiModule) uiModule.showToast(`Favorited ${n} photo${n > 1 ? 's' : ''}`);
  }

  async function _bulkTag(ids) {
    const tag = (await uiModule.styledPrompt('', { title: 'Add tag to selected', placeholder: 'tag', confirmText: 'Add', maxLength: 60 }) || '').trim().replace(/^#+/, '').trim();
    if (!tag) return;
    let n = 0;
    for (const id of ids) {
      const it = _items.find(i => i.id === id);
      const existing = (it?.user_tags || it?.tags || '').split(',').map(t => t.trim()).filter(Boolean);
      if (existing.includes(tag)) { continue; }
      const merged = [...existing, tag].join(', ');
      if (await _patchImage(id, { tags: merged })) {
        n++;
        if (it) { it.tags = merged; it.user_tags = merged; }
      }
    }
    _exitSelectMode();
    if (uiModule) uiModule.showToast(`Tagged ${n} photo${n === 1 ? '' : 's'} “${tag}”`);
  }

  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal()) return;
    if (e.target === modal) closeGallery();
  });

  _escHandler = (e) => {
    if (e.key === 'Escape') {
      if (window.__galleryCancelSelection?.()) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      // An open people list under the search box closes first.
      const suggest = document.querySelector('#gallery-modal .gallery-search-suggest:not([hidden])');
      if (suggest) {
        suggest.hidden = true;
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      // While the image editor is visible, Escape is reserved for the
      // editor (cancel transform/lasso/crop, dismiss size prompt, etc.).
      // Don't close the gallery — users would lose their in-progress edit.
      // We check the editor container's visibility AND the isEditorOpen()
      // flag so a crop popup, transform handles, etc. all keep Esc.
      const editorContainer = document.getElementById('gallery-editor-container');
      const editorVisible = !!(
        editorContainer &&
        getComputedStyle(editorContainer).display !== 'none' &&
        editorContainer.querySelector('.gallery-editor')
      );
      if (editorVisible || isEditorOpen()) {
        // The Gallery owns the earliest capture listener, so delegate to the
        // editor before swallowing Escape. This lets an active transform
        // cancel without allowing the surrounding Gallery modal to close.
        try { window.__galleryEditorHandleEscape?.(); } catch {}
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      const detail = document.getElementById('gallery-detail');
      if (detail && detail.style.display !== 'none') {
        // Click Back so Esc and the visible button always do the same thing —
        // future tweaks to Back's teardown automatically apply to Esc too.
        // stopImmediatePropagation blocks app.js's generic dynamic-modal Esc
        // handler that would otherwise close the whole gallery underneath us.
        e.preventDefault();
        e.stopImmediatePropagation();
        const back = document.getElementById('gallery-detail-back');
        if (back) back.click(); else detail.style.display = 'none';
      } else {
        closeGallery();
      }
      return;
    }
    // Arrow-key navigation inside detail view (ignore when typing in inputs)
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const detail = document.getElementById('gallery-detail');
    if (!detail || detail.style.display === 'none') return;
    const tag = (e.target?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable) return;
    const btn = document.getElementById(e.key === 'ArrowLeft' ? 'gallery-detail-prev' : 'gallery-detail-next');
    if (btn && !btn.classList.contains('gallery-detail-nav-disabled')) {
      e.preventDefault();
      btn.click();
    }
  };
  // Capture phase + stopImmediatePropagation (see _escHandler) so app.js's
  // generic dynamic-modal Esc dismiss doesn't close the whole gallery before
  // we get a chance to close just the photo detail.
  document.addEventListener('keydown', _escHandler, true);

  const btn = document.getElementById('tool-gallery-btn');
  if (btn) btn.classList.add('active');

  // 1) Paint cached state immediately so the user sees photos instantly.
  //    Filters, sort, search box, album chips, and the grid all come from
  //    module-scope state that we no longer reset on open.
  if (_items.length || _albums.length) {
    if (_search) searchInput.value = _search;
    const sortSel = document.getElementById('gallery-sort');
    if (sortSel) sortSel.value = _sort;
    _renderAlbums();
    _renderGrid();
    _renderStats();
  }
  // 2) Refresh in the background so the cache stays close-to-fresh. If the
  //    fetch fails or takes a moment, the cached view sticks around.
  _fetchAlbums();
  _fetchFolders();
  _fetchLibrary(false);
  _loadMemories();
  // Desktop keeps the convenient type-to-search behavior. On mobile this
  // immediately opened the keyboard and dropped the user into Search before
  // they could browse the drawer.
  if (window.innerWidth > 768) searchInput.focus();
}

function _showImagesTab() {
  const modal = document.getElementById('gallery-modal');
  if (!modal) return;
  modal.querySelectorAll('.gallery-tab').forEach(t => t.classList.remove('active'));
  modal.querySelector('.gallery-tab[data-tab="images"]')?.classList.add('active');
  const imagesContainer = document.getElementById('gallery-images-container');
  const albumsContainer = document.getElementById('gallery-albums-container');
  const editorContainer = document.getElementById('gallery-editor-container');
  const settingsContainer = document.getElementById('gallery-settings-container');
  if (imagesContainer) imagesContainer.style.display = '';
  if (albumsContainer) albumsContainer.style.display = 'none';
  if (editorContainer) editorContainer.style.display = 'none';
  if (settingsContainer) settingsContainer.style.display = 'none';
  const peopleContainer = document.getElementById('gallery-people-container');
  if (peopleContainer) peopleContainer.style.display = 'none';
  const tagsContainer = document.getElementById('gallery-tags-container');
  if (tagsContainer) tagsContainer.style.display = 'none';
  const mapContainer = document.getElementById('gallery-map-container');
  if (mapContainer) mapContainer.style.display = 'none';
  const tripsContainer = document.getElementById('gallery-trips-container');
  if (tripsContainer) tripsContainer.style.display = 'none';
}

export async function openGalleryImage(imageId) {
  if (!imageId) {
    openGallery();
    return;
  }
  openGallery();
  _showImagesTab();
  _search = '';
  _activeTags = [];
  _activeModel = null;
  _activeAlbum = null;
  _favoritesOnly = false;
  _activeNear = null;
  _activeDates = null;
  _sort = 'recent';
  const searchInput = document.getElementById('gallery-search');
  if (searchInput) searchInput.value = '';
  const sortSel = document.getElementById('gallery-sort');
  if (sortSel) { sortSel.value = 'recent'; sortSel.disabled = false; }
  const detail = document.getElementById('gallery-detail');
  if (detail) detail.style.display = 'none';

  try {
    await _fetchLibrary(false);
    let img = _items.find(i => String(i.id) === String(imageId));
    if (!img) {
      const res = await fetch(`${API_BASE}/api/gallery/${encodeURIComponent(imageId)}`, { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        img = data.image || data;
      }
    }
    if (!img || !img.id) {
      uiModule.showToast?.('Photo not found in gallery', 3000);
      return;
    }
    _openDetail(img);
    const card = document.querySelector(`.gallery-card[data-id="${CSS.escape(String(imageId))}"]`);
    if (card) {
      card.scrollIntoView({ block: 'center', behavior: 'smooth' });
      card.classList.add('gallery-card-focus');
      setTimeout(() => card.classList.remove('gallery-card-focus'), 1600);
    }
  } catch (err) {
    console.error('[gallery] open image failed', err);
    uiModule.showToast?.('Could not open photo in gallery', 3000);
  }
}

function _doCloseGallery() {
  const editorMounted = !!document.querySelector('#gallery-editor-container .gallery-editor');
  if ((window.__galleryEditLive || isEditorOpen() || editorMounted) && !window.__galleryAllowCloseEditor) {
    if (uiModule) uiModule.showToast('Close the edit tab first');
    return;
  }
  _open = false;
  clearTimeout(_searchDebounce);
  if (_galleryResizeHandler) {
    window.removeEventListener('resize', _galleryResizeHandler);
    _galleryResizeHandler = null;
  }
  // Detach the face-overlay resize listener so we don't leak a
  // handler past close (v2 review HIGH-9).
  _destroyMap();
  closeEditor();
  window.__galleryAllowCloseEditor = false;

  const modal = document.getElementById('gallery-modal');
  if (modal) {
    const content = modal.querySelector('.modal-content, .gallery-modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => { if (modal.parentElement) modal.remove(); }, 250);
    } else {
      modal.remove();
    }
  }

  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler, true);
    _escHandler = null;
  }
  delete window.__galleryCancelSelection;

  const btn = document.getElementById('tool-gallery-btn');
  if (btn) btn.classList.remove('active');
}

export function closeGallery() {
  if (!_open && !Modals.isMinimized('gallery-modal')) return;
  if (Modals.isRegistered('gallery-modal')) {
    Modals.close('gallery-modal');
  } else {
    _doCloseGallery();
  }
}

export function isGalleryOpen() {
  if (Modals.isMinimized('gallery-modal')) return false;
  return _open;
}

// ---- Utilities ----

function _esc(str) {
  if (!str) return '';
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function _humanSize(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

const galleryModule = {
  openGallery,
  openGalleryImage,
  closeGallery,
  isGalleryOpen,
};

export default galleryModule;
