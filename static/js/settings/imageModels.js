// Image-model settings panel.
// Extracted verbatim from static/js/settings.js. Behaviour is unchanged: the
// functions moved, their bodies did not. settings.js imports them and calls
// them from the same places, so load order and init sequence are untouched.

import { byId as el } from './dom.js';
import { saveImagePreferences as _postSettings, readImagePreferences } from './imagePreferences.js';
import { modelCaps } from '../editor/ai-models.js';
import { sortModelIds } from '../modelSort.js';
let preferencesListener = null;

const IMAGE_SERVER_REFRESH_MS = 3000;
const IDLE_CHOICES = [[5, '5 minutes'], [10, '10 minutes'], [30, '30 minutes'], [60, '1 hour'], [0, 'Never']];

function imageServerStateText(server) {
  if (server.state === 'starting') return 'Loading the model, this takes a minute or two…';
  if (server.state === 'stopping') return 'Stopping…';
  if (server.state === 'running') {
    if (server.in_flight) return 'Running · making an image';
    if (server.stops_in_seconds == null) return 'Running';
    return `Running · stops in ${Math.max(1, Math.ceil(server.stops_in_seconds / 60))} min`;
  }
  return server.last_error || 'Stopped · starts by itself with the next image';
}

function imageServerRow(server, onCommand, onIdleChange) {
  const row = document.createElement('div');
  row.className = 'image-server-row';
  row.dataset.serverId = server.id;

  const name = document.createElement('span');
  name.className = 'image-server-name';
  name.textContent = server.name;

  const state = document.createElement('span');
  state.className = 'image-server-state';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'admin-btn-sm image-server-toggle';
  button.addEventListener('click', () => onCommand(server.id, button.dataset.action));

  const idle = document.createElement('label');
  idle.className = 'image-server-idle';
  idle.textContent = 'Stop when idle for';
  const select = document.createElement('select');
  select.className = 'settings-select';
  for (const [minutes, label] of IDLE_CHOICES) select.add(new Option(label, String(minutes)));
  select.addEventListener('change', () => onIdleChange(server.id, Number(select.value)));
  idle.appendChild(select);

  row.append(name, state, button, idle);
  return row;
}

function updateImageServerRow(row, server) {
  const state = row.querySelector('.image-server-state');
  state.textContent = imageServerStateText(server);
  state.classList.toggle('is-error', server.state === 'stopped' && !!server.last_error);

  const stopped = server.state === 'stopped';
  const button = row.querySelector('.image-server-toggle');
  button.dataset.action = stopped ? 'start' : 'stop';
  button.textContent = stopped ? 'Start' : 'Stop';
  button.disabled = server.state === 'stopping';

  const select = row.querySelector('select');
  if (document.activeElement === select) return;
  const value = String(server.idle_minutes);
  if (![...select.options].some(option => option.value === value)) {
    select.add(new Option(`${server.idle_minutes} minutes`, value));
  }
  select.value = value;
}

// Local image servers that run under scripts/image_server_supervisor.py: shows
// their state and offers Start, Stop and the idle time. Hidden when there are none.
function initImageServers(msg) {
  const container = el('set-imgServers');
  if (!container) return;
  const card = container.closest('.admin-card');
  let timer = null;

  function showError(text) {
    if (!msg) return;
    msg.textContent = text;
    msg.style.color = 'var(--red)';
  }

  async function refresh() {
    let servers;
    try {
      const res = await fetch('/api/image-servers', { credentials: 'same-origin' });
      if (res.status === 401 || res.status === 403) {
        clearInterval(timer);
        return;
      }
      if (!res.ok) return;
      servers = (await res.json()).servers || [];
    } catch (_) {
      return;
    }
    const rows = new Map([...container.children].map(row => [row.dataset.serverId, row]));
    for (const server of servers) {
      let row = rows.get(server.id);
      if (!row) {
        row = imageServerRow(server, onCommand, onIdleChange);
        container.appendChild(row);
      }
      rows.delete(server.id);
      updateImageServerRow(row, server);
    }
    rows.forEach(row => row.remove());
    container.hidden = servers.length === 0;
  }

  async function post(id, path, body) {
    try {
      const res = await fetch(`/api/image-servers/${encodeURIComponent(id)}/${path}`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showError(data.detail || 'The image server did not respond');
      }
    } catch (_) {
      showError('The image server did not respond');
    }
    refresh();
  }

  const onCommand = (id, action) => post(id, action);
  const onIdleChange = (id, minutes) => post(id, 'settings', { idle_minutes: minutes });

  refresh();
  timer = setInterval(() => {
    if (!document.hidden && card?.offsetParent) refresh();
  }, IMAGE_SERVER_REFRESH_MS);
}

export async function initImageSettings() {
  const modelSel = el('set-imgModelSelect');
  const editModelSel = el('set-imgEditModelSelect');
  const qualSel = el('set-imgQualitySelect');
  const msg = el('set-imgSettingsMsg');
  const enabledToggle = el('set-imgEnabledToggle');
  const configWrap = modelSel ? modelSel.closest('div[style*="flex-direction"]') : null;
  initImageServers(msg);
  try {
    const endpointsRes = await fetch('/api/model-endpoints', { credentials: 'same-origin' });
    const endpoints = await endpointsRes.json();
    const imageModels = new Set();
    const editModels = new Set();
    (Array.isArray(endpoints) ? endpoints : []).forEach(endpoint => {
      if (!endpoint.is_enabled || !endpoint.online) return;
      (Array.isArray(endpoint.models) ? endpoint.models : []).forEach(modelId => {
        if (modelId && modelCaps(modelId, '', endpoint.model_type).gen) imageModels.add(String(modelId));
        if (modelId && modelCaps(modelId, '', endpoint.model_type).inpaint) editModels.add(String(modelId));
      });
    });
    modelSel.length = 1;
    sortModelIds(Array.from(imageModels)).forEach(mid => {
      const opt = document.createElement('option');
      opt.value = mid;
      opt.textContent = mid;
      modelSel.appendChild(opt);
    });
    if (editModelSel) {
      editModelSel.length = 1;
      for (const mid of sortModelIds(Array.from(editModels))) {
        const option = document.createElement('option');
        option.value = mid;
        option.textContent = mid;
        editModelSel.appendChild(option);
      }
    }
  } catch (e) { console.warn('Failed to load models for image settings', e); }
  let currentEnabled = enabledToggle ? enabledToggle.checked : false;
  try {
    const settings = await readImagePreferences();
    if (modelSel && 'image_model' in settings) modelSel.value = settings.image_model || '';
    if (editModelSel && 'image_edit_model' in settings) editModelSel.value = settings.image_edit_model || '';
    if (qualSel && 'image_quality' in settings) qualSel.value = settings.image_quality || 'medium';
    if (enabledToggle) {
      enabledToggle.checked = settings.image_gen_enabled === true;
      currentEnabled = enabledToggle.checked;
    }
  } catch (e) { console.warn('Failed to load settings', e); }

  function syncImgDisabled() {
    var off = enabledToggle && !enabledToggle.checked;
    var card = enabledToggle ? enabledToggle.closest('.admin-card') : null;
    if (card) card.style.opacity = off ? '0.45' : '';
    if (configWrap) configWrap.style.pointerEvents = off ? 'none' : '';
  }
  syncImgDisabled();

  function checkIsAdmin() {
    return typeof window !== 'undefined' && !!window._isAdmin;
  }

  function applyAdminToggleState() {
    if (!enabledToggle) return;
    const admin = checkIsAdmin();
    if (!admin) {
      enabledToggle.disabled = true;
      const adminTitle = 'Admin access is required to change image generation availability.';
      enabledToggle.title = adminTitle;
      if (enabledToggle.parentElement) enabledToggle.parentElement.title = adminTitle;
    } else {
      enabledToggle.disabled = false;
      const adminTitle = 'Enable or disable image generation globally';
      enabledToggle.title = adminTitle;
      if (enabledToggle.parentElement) enabledToggle.parentElement.title = adminTitle;
    }
  }
  applyAdminToggleState();

  async function saveModelPreferences() {
    try {
      const res = await _postSettings({
        image_model: modelSel ? modelSel.value : '',
        image_edit_model: editModelSel?.value || '',
        image_quality: qualSel ? qualSel.value : 'medium',
      });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
      msg.textContent = 'Saved'; msg.style.color = 'var(--fg)'; setTimeout(() => { msg.textContent = ''; }, 2000);
    } catch (e) {
      if (e.globalSaved) {
        msg.textContent = 'Policy saved, but failed to clear preferences: ' + (e.message || 'cleanup error');
        msg.style.color = 'var(--red)';
      } else {
        msg.textContent = e.status === 403 ? 'Admin access is required to change these settings.' : 'Failed to save';
        msg.style.color = 'var(--red)';
      }
    }
  }

  async function saveToggle() {
    if (!checkIsAdmin()) {
      if (enabledToggle) enabledToggle.checked = currentEnabled;
      syncImgDisabled();
      msg.textContent = 'Admin access is required to change these settings.';
      msg.style.color = 'var(--red)';
      return;
    }
    const targetState = enabledToggle ? enabledToggle.checked : false;
    try {
      const res = await _postSettings({ image_gen_enabled: targetState });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
      if (enabledToggle) currentEnabled = enabledToggle.checked;
      msg.textContent = 'Saved'; msg.style.color = 'var(--fg)'; setTimeout(() => { msg.textContent = ''; }, 2000);
    } catch (e) {
      if (e.globalSaved) {
        currentEnabled = targetState;
        try {
          const authSettings = await readImagePreferences();
          if (typeof authSettings.image_gen_enabled === 'boolean') {
            currentEnabled = authSettings.image_gen_enabled;
          }
        } catch (_) {}
        if (enabledToggle) enabledToggle.checked = currentEnabled;
        syncImgDisabled();
        msg.textContent = 'Policy saved, but failed to clear preferences: ' + (e.message || 'cleanup error');
        msg.style.color = 'var(--red)';
      } else {
        try {
          const authSettings = await readImagePreferences();
          if (typeof authSettings.image_gen_enabled === 'boolean') {
            currentEnabled = authSettings.image_gen_enabled;
          }
        } catch (_) {}
        if (enabledToggle) enabledToggle.checked = currentEnabled;
        syncImgDisabled();
        msg.textContent = e.status === 403 ? 'Admin access is required to change these settings.' : 'Failed to save';
        msg.style.color = 'var(--red)';
      }
    }
  }

  if (modelSel) modelSel.addEventListener('change', saveModelPreferences);
  if (editModelSel) editModelSel.addEventListener('change', saveModelPreferences);
  if (qualSel) qualSel.addEventListener('change', saveModelPreferences);
  if (enabledToggle) {
    enabledToggle.addEventListener('change', async function() {
      if (!checkIsAdmin()) {
        enabledToggle.checked = currentEnabled;
        syncImgDisabled();
        msg.textContent = 'Admin access is required to change these settings.';
        msg.style.color = 'var(--red)';
        return;
      }
      syncImgDisabled();
      await saveToggle();
    });
  }

  if (preferencesListener) window.removeEventListener('image-preferences-changed', preferencesListener);
  preferencesListener = event => {
    if ('image_model' in event.detail && modelSel) modelSel.value = event.detail.image_model || '';
    if (editModelSel && 'image_edit_model' in event.detail) editModelSel.value = event.detail.image_edit_model || '';
    if ('image_gen_enabled' in event.detail && enabledToggle) {
      enabledToggle.checked = event.detail.image_gen_enabled === true;
      currentEnabled = enabledToggle.checked;
      syncImgDisabled();
    }
  };
  window.addEventListener('image-preferences-changed', preferencesListener);
}
