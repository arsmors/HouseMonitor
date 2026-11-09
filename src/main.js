import { createIcons, House, Pencil, Trash2, Scan, MapPin, ArrowUpRight, Pause, Play, Send, EllipsisVertical, CheckCheck, Paperclip, Mic, BellRing, Plus, ListFilter, Radar, Search, ArrowRight, Leaf, LayoutGrid, Activity, Settings2, ChevronsUpDown, ChevronRight, X, Check, CircleCheck, Circle, Map, Info, Radio } from 'lucide';
import './style.css';
import './live.css';

const icons = { House, Pencil, Trash2, Scan, MapPin, ArrowUpRight, Pause, Play, Send, EllipsisVertical, CheckCheck, Paperclip, Mic, BellRing, Plus, ListFilter, Radar, Search, ArrowRight, Leaf, LayoutGrid, Activity, Settings2, ChevronsUpDown, ChevronRight, X, Check, CircleCheck, Circle, Map, Info, Radio };
const icon = (name, cls = '') => `<i data-lucide="${name}" class="${cls}" aria-hidden="true"></i>`;
const escape = (value) => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
let listings = [];
let searches = [];
let catalog = {};
let backendStatus = { sources: {}, events: [], queue: {}, telegram: {}, interval: 3600 };
let connectionError = '';
let loaded = false;
let view = 'listings';
let filter = 'all';
let selectedSearch = null;
let selectedLocation = null;
let selectedListing = null;
let notificationEnabled = true;
const app = document.querySelector('#app');
const dialog = document.querySelector('#search-dialog');
let toastTimeout;
function toast(message) { const element = document.querySelector('#toast'); element.textContent = message; element.classList.add('visible'); clearTimeout(toastTimeout); toastTimeout = setTimeout(() => element.classList.remove('visible'), 3200); }
/** Use same-origin requests; mutation headers protect the local API from cross-site writes. */
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', 'X-House-Monitor': '1', ...options.headers }, signal: AbortSignal.timeout(20000) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || (response.status === 409 ? 'A scan is running or was requested recently.' : `Backend request failed (${response.status})`));
  return data;
}
/** Resolve a stable geographic ID to the source-provided name. */
function locationLabel(id) { return catalog[id]?.label || id; }
/** Display the publication date used for sorting, or clearly label its fallback. */
function listingDateLabel(listing) {
  const published = new Date(listing.published_at || '');
  const hasPublication = !Number.isNaN(published.getTime());
  const timestamp = listing.relisted_at ? new Date(listing.relisted_at) : hasPublication ? published : new Date(listing.first_seen_at);
  if (Number.isNaN(timestamp.getTime())) return 'Date unavailable';
  const date = timestamp.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return `${listing.relisted_at ? 'Relisted' : hasPublication ? 'Published' : 'Discovered'} ${date}`;
}
/** Refresh shared server state without interrupting an open search form. */
async function refresh() {
  let contentChanged = true;
  try {
    const data = await api('/api/state');
    const nextCatalog = Object.fromEntries(data.locations.map(location => [location.id, location]));
    contentChanged = !loaded || Boolean(connectionError)
      || JSON.stringify([searches, listings, catalog]) !== JSON.stringify([data.searches, data.listings, nextCatalog]);
    searches = data.searches;
    listings = data.listings;
    catalog = nextCatalog;
    locationGroups = {};
    for (const location of data.locations) (locationGroups[location.group || 'Latvia'] ||= []).push(location.id);
    backendStatus = data.status;
    notificationEnabled = backendStatus.notifications_enabled;
    loaded = true;
    connectionError = '';
  } catch (error) {
    connectionError = error.message || 'Backend unavailable';
    backendStatus = { ...backendStatus, scanning: false };
  }
  if (!dialog.open) {
    if (contentChanged || connectionError || ['activity', 'settings'].includes(view) || !app.querySelector('.listing-card')) render();
    else updateProgress();
  }
}
function drawIcons() { createIcons({ icons, attrs: { 'stroke-width': 1.7 } }); }
function areaLabel(search) { return search.min && search.max ? `${search.min}–${search.max} m²` : search.min ? `From ${search.min} m²` : search.max ? `Up to ${search.max} m²` : 'Any property area'; }
/** Display the server-configured refresh interval consistently. */
function intervalLabel() {
  const hours = backendStatus.interval % 3600 === 0;
  const amount = backendStatus.interval / (hours ? 3600 : 60);
  const unit = hours ? 'hour' : 'minute';
  return `${amount} ${unit}${amount === 1 ? '' : 's'}`;
}
/** Identify the active source rather than displaying an indefinite generic spinner. */
function checkingLabel() {
  const active = Object.entries(backendStatus.sources).find(([, source]) => source.status === 'scanning');
  return active ? `Checking ${active[0]}…` : 'Checking sources…';
}
/** Match real listings against stable geographic IDs and inclusive house-area limits. */
function matchesSearch(listing, search) {
  return (!search.locations.length || search.locations.some(location => listing.location_ids.includes(location)))
    && (search.min == null || listing.area != null && listing.area >= search.min)
    && (search.max == null || listing.area != null && listing.area <= search.max);
}
function listingFilterLocation(listing) {
  return (listing.location_ids || []).filter(id => catalog[id]).sort((left, right) => right.split('/').length - left.split('/').length)[0] || null;
}
function priceHistory(listing) {
  const history = listing.price_history || [];
  if (!history.length) return '';
  return `<details class="price-history"><summary>Price history · ${history.length} ${history.length === 1 ? 'record' : 'records'}</summary><ol>${history.map((record, index) => {
    const date = new Date(record.observed_at);
    const label = Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    const relisted = index > 0 && history[index - 1].listing_id !== record.listing_id;
    return `<li><time>${escape(label)}</time><strong>${escape(record.price)}</strong>${relisted ? '<span>Relisted</span>' : ''}</li>`;
  }).join('')}</ol></details>`;
}
function matchingListings() {
  return listings;
}
/** Use the same filtered listings for cards and the notification preview. */
function visibleListings() {
  return matchingListings().filter(listing => !selectedLocation || listing.location_ids.includes(selectedLocation));
}
function locationFilterTags() {
  const matching = matchingListings();
  const counts = new globalThis.Map();
  for (const listing of matching) {
    const id = listingFilterLocation(listing);
    if (id) counts.set(id, matching.filter(candidate => candidate.location_ids.includes(id)).length);
  }
  const tags = [...counts].sort(([left], [right]) => locationLabel(left).localeCompare(locationLabel(right), 'lv'));
  return `<div class="location-filter-tags" role="group" aria-label="Filter ads by location"><span class="location-filter-caption">Locations</span><button class="location-tag" type="button" data-clear-location aria-label="Show ads in all locations (${matching.length} listings)" aria-pressed="${!selectedLocation}">All <span class="location-tag-count">${matching.length}</span></button>${tags.map(([id, count]) => `<button class="location-tag" type="button" data-location-filter="${escape(id)}" aria-label="Filter ads in ${escape(locationLabel(id))} (${count} listings)" aria-pressed="${selectedLocation === id}" title="Filter ads in ${escape(locationLabel(id))}"><span>${escape(locationLabel(id))}</span><span class="location-tag-count">${count}</span></button>`).join('')}</div>`;
}
function searchCard(search) {
  return `<article class="search-card ${search.active ? '' : 'is-paused'}">
    <div class="search-top"><div class="search-icon">${icon('house')}</div><span class="status ${search.active ? 'active' : 'paused'}"><span></span>${search.active ? 'Active' : 'Paused'}</span><div class="card-actions"><button class="icon-button" title="Edit search" aria-label="Edit ${escape(search.name)}" data-edit="${search.id}">${icon('pencil')}</button><button class="icon-button" title="Delete search" aria-label="Delete ${escape(search.name)}" data-delete="${search.id}">${icon('trash-2')}</button></div></div>
    <h3>${escape(search.name)}</h3><div class="category">Houses for sale</div>
    <div class="criteria"><div>${icon('scan')}<span>${areaLabel(search)}</span></div><div>${icon('map-pin')}<span>${search.locations.length ? search.locations.map(locationLabel).map(escape).join(', ') : 'All Latvia'}</span></div></div>
    <div class="search-bottom"><button class="match-link" data-matches="${search.id}"><strong>${listings.filter(listing => matchesSearch(listing, search)).length}</strong> matches ${icon('arrow-up-right')}</button><button class="pause-button" data-toggle="${search.id}" aria-label="${search.active ? 'Pause' : 'Resume'} ${escape(search.name)}">${icon(search.active ? 'pause' : 'play')} ${search.active ? 'Pause' : 'Resume'}</button></div>
  </article>`;
}
function listingCard(listing) {
  const relistBadge = listing.relisted_at ? '<div class="relisted-badge">Relisted</div>' : '';
  const locationId = listingFilterLocation(listing);
  const locationLabelText = locationId ? locationLabel(locationId) : '';
  const location = locationId
    ? `<button class="location-filter-trigger" type="button" data-location-filter="${escape(locationId)}" aria-label="Filter ads in ${escape(locationLabelText)}" aria-pressed="${selectedLocation === locationId}" title="Filter ads in ${escape(locationLabelText)}">${escape(listing.location)}</button>`
    : `<span>${escape(listing.location)}</span>`;
  return `<article class="listing-card ${selectedListing === listing.id ? 'selected' : ''}"><a class="listing-image" href="${escape(listing.url)}" target="_blank" rel="noopener noreferrer" aria-label="Open advertisement on ${escape(listing.source)}: ${escape(listing.title)}">${listing.image ? `<img src="${escape(listing.image)}" alt="${escape(listing.title)}" loading="lazy" referrerpolicy="no-referrer" />` : `<div class="no-image">${icon('house')}</div>`}<span>${escape(listing.source)}</span></a><div class="listing-body">${relistBadge}<div class="listing-price">${escape(listing.price)}<span>${listing.area != null ? `${listing.area} m²` : 'Area not provided'}</span></div>${priceHistory(listing)}<h3><a class="listing-title-link" href="${escape(listing.url)}" target="_blank" rel="noopener noreferrer">${escape(listing.title)}</a></h3><div class="listing-location">${icon('map-pin')} ${location}</div><div class="listing-footer"><span>${escape(listing.source)}</span><span>${escape(listingDateLabel(listing))}</span><button class="icon-button" data-listing="${escape(listing.id)}" title="Preview Telegram notification" aria-label="Preview Telegram notification for ${escape(listing.title)}">${icon('send')}</button></div></div></article>`;
}
function telegramPreview() {
  const matching = visibleListings();
  const listing = matching.find(item => item.id === selectedListing) || matching[0];
  if (!listing) return `<aside class="preview-panel"><div class="panel-eyebrow"><span>${icon('send')} YOUR TELEGRAM</span><span class="preview-tag">Preview</span></div><div class="empty-state">${loaded ? 'No matching houses discovered yet.' : 'Waiting for the backend…'}</div></aside>`;
  const search = searches.find(item => (selectedSearch ? item.id === selectedSearch : item.active) && matchesSearch(listing, item));
  return `<aside class="preview-panel"><div class="panel-eyebrow"><span>${icon('send')} YOUR TELEGRAM</span><span class="preview-tag">Preview</span></div>
    <div class="telegram-window"><div class="telegram-header"><div class="telegram-avatar">${icon('house')}</div><div><strong>House Monitor</strong><span>bot</span></div>${icon('ellipsis-vertical')}</div>
    <div class="telegram-chat"><div class="chat-date">${escape(listingDateLabel(listing))}</div><div class="telegram-message">${listing.image ? `<img src="${escape(listing.image)}" alt="${escape(listing.title)}" referrerpolicy="no-referrer" />` : ''}<div class="message-body"><span class="new-label">${escape(listing.source)}</span><h3>${escape(listing.title)}</h3><div class="message-price">${escape(listing.price)}</div><div class="message-detail">${icon('scan')} ${listing.area != null ? `${listing.area} m²` : 'Area not provided'}</div><p>${escape(listing.location)}</p><p>Search: ${escape(search?.name || 'Saved search')}</p></div><a class="telegram-open" href="${escape(listing.url)}" target="_blank" rel="noopener noreferrer">Open original advertisement ${icon('arrow-up-right')}</a></div><div class="sample-note">Notification preview · Actual source listing</div></div>
    <div class="telegram-input"><span>Message</span>${icon('paperclip')}${icon('mic')}</div></div>
    <div class="delivery-note">${icon('bell-ring')}<div><strong>A new home. Straight to you.</strong><p>Matching listings, delivered to Telegram.</p></div></div>
    <div class="source-note"><span>YOUR SOURCES</span>${Object.entries(backendStatus.sources).map(([source, state]) => `<div><strong>${escape(source)}</strong><span class="source-state">${escape(state.status.replaceAll('_', ' '))}</span></div>`).join('')}<p>Houses for sale. Your selected areas.</p></div>
  </aside>`;
}
function mainContent() {
  const filtered = searches.filter(search => filter === 'all' || (filter === 'active' ? search.active : !search.active));
  const recent = visibleListings();
  const selectedAreas = [...new Set(searches.filter(search => selectedSearch ? search.id === selectedSearch : search.active).flatMap(search => search.locations))].map(locationLabel);
  if (view === 'settings') return `<div class="page-heading"><div><div class="eyebrow">MAKE IT YOURS</div><h1>Settings</h1><p>The essentials, kept simple.</p></div></div><section class="settings-section"><h2>Telegram notifications</h2><div class="setting-row"><div><strong>Listing notifications</strong><p>Notifications for active searches</p></div><button class="switch ${notificationEnabled ? 'on' : ''}" role="switch" aria-checked="${notificationEnabled}" aria-label="Listing notifications" id="notifications-toggle"><span></span></button></div><div class="setting-row"><div><strong>Telegram connection</strong><p>${escape(backendStatus.telegram.error || (backendStatus.telegram.ready ? `@${backendStatus.telegram.username}` : 'Bot connected; waiting for /start from owner'))}</p></div><span class="neutral-badge">${backendStatus.telegram.ready ? 'Connected' : backendStatus.telegram.configured ? 'Awaiting /start' : 'Not configured'}</span></div><h2>Monitoring</h2><div class="setting-row"><div><strong>Polling interval</strong><p>Non-overlapping source scans</p></div><span class="neutral-badge">${backendStatus.interval / 60} minutes</span></div>${Object.entries(backendStatus.sources).map(([source, state]) => `<div class="setting-row"><div><strong>${escape(source)}</strong><p>${escape(state.error || (state.last_success ? `Last success: ${new Date(state.last_success).toLocaleString()}` : 'Waiting for first scan'))}</p></div><span class="neutral-badge">${escape(state.status.replaceAll('_', ' '))}</span></div>`).join('')}<h2>Your data</h2><div class="setting-row"><div><strong>Searches and listing history</strong><p>Persistent SQLite database on the server</p></div><span class="neutral-badge">${searches.length} searches</span></div></section>`;
  if (view === 'activity') return `<div class="page-heading"><div><div class="eyebrow">BEHIND THE SCENES</div><h1>Monitoring activity</h1><p>${backendStatus.scanning ? 'Collecting source listings now' : backendStatus.last_scan ? `Last cycle: ${new Date(backendStatus.last_scan).toLocaleString()}` : 'Waiting for the first cycle'}</p></div><button class="secondary-button" data-scan ${backendStatus.scanning ? 'disabled' : ''}>${icon('radar')} Check now</button></div><section class="settings-section">${Object.entries(backendStatus.sources).map(([source, state]) => `<div class="setting-row"><div><strong>${escape(source)}</strong><p>${escape(state.error || `${state.total || 0} listings checked · ${state.queued || 0} alerts queued`)}</p></div><span class="neutral-badge">${escape(state.status.replaceAll('_', ' '))}</span></div>`).join('')}<h2>Notification queue</h2>${Object.entries(backendStatus.queue).map(([status, count]) => `<div class="activity-detail"><span>${escape(status)}</span><strong>${count}</strong></div>`).join('') || '<p class="field-hint">No queued notifications. First scans establish a baseline without alerts.</p>'}${backendStatus.queue.uncertain ? '<p class="form-error">Unconfirmed deliveries need a chat check before manual retry to avoid duplicate alerts.</p>' : ''}<h2>Recent activity</h2>${backendStatus.events.map(event => `<div class="activity-detail"><span>${escape(new Date(event.created_at).toLocaleString())}</span><strong>${escape(event.message)}</strong></div>`).join('') || '<p class="field-hint">No monitoring events yet.</p>'}</section>`;
  if (view === 'listings') return `<div class="page-heading"><div><div class="eyebrow">YOUR NEXT CHAPTER</div><h1>Recent listings</h1><p>Vecdaugava · Vecāķi · Carnikavas novads · Trīsciems · Jaunciems</p></div></div>${locationFilterTags()}<div class="section-heading"><h2>Latest discoveries <span>${recent.length}</span></h2><button class="text-button" data-scan ${backendStatus.scanning ? 'disabled' : ''}>${icon('radar')} ${backendStatus.scanning ? 'Checking sources…' : 'Check now'}</button></div><div class="listing-grid full-listings">${recent.length ? recent.map(listingCard).join('') : `<div class="empty-state">${!loaded ? 'Connecting to the monitoring service…' : backendStatus.scanning ? 'Checking sources for houses…' : selectedLocation ? 'No ads match this location filter.' : 'No houses discovered yet.'}</div>`}</div>`;
  return `<div class="page-heading"><div><div class="eyebrow">LESS SEARCHING. MORE LIVING.</div><h1>Your next home is out there.</h1><p>We’ll keep an eye out. You carry on with your day.</p></div><button class="primary-button" id="new-search">${icon('plus')} New search</button></div>
    <div class="monitor-strip"><span class="monitor-symbol">${icon('radar')}</span><div><strong>Your house hunt, on autopilot.</strong><span>${searches.filter(search => search.active).length} active searches <b>·</b> Every ${intervalLabel()}</span></div><span class="demo-status"><span></span>${backendStatus.scanning ? 'Scanning' : 'Monitoring'}</span></div>
    <section class="search-section"><div class="section-heading"><h2>Saved searches <span>${searches.length}</span></h2><div class="segmented" aria-label="Filter searches">${['all','active','paused'].map(item => `<button data-filter="${item}" class="${filter === item ? 'selected' : ''}" aria-pressed="${filter === item}">${item[0].toUpperCase() + item.slice(1)}</button>`).join('')}</div></div><div class="search-grid">${filtered.length ? filtered.map(searchCard).join('') : `<div class="empty-state">${icon('search')}<h3>No ${filter === 'all' ? 'saved' : filter} searches</h3><p>${filter === 'all' ? 'Create a search to start your house hunt.' : 'Your searches will appear here.'}</p><button class="secondary-button" data-add-search>${icon('plus')} New search</button></div>`}</div></section>
    <section class="recent-section"><div class="section-heading"><div class="heading-with-note"><h2>Latest discoveries</h2></div><button class="text-button" id="view-listings">View all listings ${icon('arrow-right')}</button></div><div class="listing-grid">${recent.length ? recent.slice(0, 3).map(listingCard).join('') : '<div class="empty-state">No matching houses discovered yet.</div>'}</div></section>
    <div class="quiet-footer">${icon('leaf')} A little less refreshing. A little closer to home.<span>Made for house hunting in Latvia.</span></div>`;
}
function render() {
  const openHistories = new Set([...app.querySelectorAll('.listing-card')].filter(card => card.querySelector('.price-history')?.open).map(card => card.querySelector('[data-listing]').dataset.listing));
  app.innerHTML = `<aside class="sidebar"><a href="#" class="brand" aria-label="House Monitor home"><span class="brand-mark">${icon('house')}</span><span>house<span class="brand-light">monitor</span><small>FIND YOUR PLACE.</small></span></a><div class="workspace-label">WORKSPACE</div><nav>${[{ id: 'listings', icon: 'house', label: 'Recent listings' }, { id: 'activity', icon: 'activity', label: 'Activity' }].map(item => `<button data-nav="${item.id}" class="nav-item ${view === item.id ? 'selected' : ''}">${icon(item.icon)}<span>${item.label}</span>${item.id === 'listings' ? '<span class="tiny-dot"></span>' : ''}</button>`).join('')}<div class="nav-divider"></div><button data-nav="settings" class="nav-item ${view === 'settings' ? 'selected' : ''}">${icon('settings-2')}<span>Settings</span></button></nav><div class="sidebar-bottom"><div class="latvia-mark"><span></span><span></span><span></span></div><strong>A home, somewhere in Latvia.</strong><p>We’ll help you find it.</p><div class="profile"><div class="profile-avatar">A</div><div><strong>My workspace</strong><small>Personal account</small></div><button class="icon-button" data-nav="settings" title="Account settings" aria-label="Account settings">${icon('chevrons-up-down')}</button></div></div></aside>
    <div class="workspace"><header class="topbar"><div class="breadcrumb">Workspace ${icon('chevron-right')} <strong>${({ listings: 'Recent listings', activity: 'Activity', settings: 'Settings' })[view]}</strong></div><div class="topbar-right"><span class="timezone">${icon('map-pin')} Latvia <span>·</span> EET</span><span class="topbar-divider"></span><button class="telegram-status" data-nav="settings">${icon('send')} Telegram <span>Not connected</span></button></div></header><div class="page-layout"><main>${mainContent()}</main>${telegramPreview()}</div></div>`;
  app.querySelectorAll('.nav-item[data-nav]').forEach(button => {
    const label = { searches: 'My searches', listings: 'Recent listings', activity: 'Activity', settings: 'Settings' }[button.dataset.nav];
    button.setAttribute('aria-label', label);
    button.title = label;
  });
  app.querySelectorAll('.listing-card').forEach(card => {
    const history = card.querySelector('.price-history');
    if (history && openHistories.has(card.querySelector('[data-listing]').dataset.listing)) history.open = true;
  });
  updateProgress();
  if (view === 'activity') {
    const summary = app.querySelector('.page-heading p');
    if (backendStatus.scanning) summary.textContent = checkingLabel();
    else if (backendStatus.mode === 'scheduled') summary.textContent = 'Hourly checks via GitHub Actions; scheduled runs may be delayed.';
    else if (backendStatus.next_scan) summary.textContent = `Next check: ${new Date(backendStatus.next_scan * 1000).toLocaleString()}`;
  }
  app.querySelectorAll('.setting-row').forEach(row => {
    if (row.querySelector('strong')?.textContent === 'Polling interval') row.querySelector('.neutral-badge').textContent = intervalLabel();
  });
  if (view === 'settings' && backendStatus.mode === 'scheduled') {
    const dataRow = [...app.querySelectorAll('.setting-row')].find(row => row.querySelector('strong')?.textContent === 'Searches and listing history');
    dataRow.querySelector('p').textContent = 'Persistent hosted libSQL database';
    const manage = document.createElement('button');
    manage.className = 'secondary-button';
    manage.dataset.nav = 'searches';
    manage.textContent = 'Manage searches';
    dataRow.after(manage);
  }
  if (connectionError) {
    const notice = document.createElement('div');
    notice.className = 'connection-error';
    notice.setAttribute('role', 'alert');
    notice.textContent = `Monitoring service unavailable: ${connectionError}. Displayed data may be out of date.`;
    app.querySelector('main').prepend(notice);
  }
  drawIcons();
}

function updateProgress() {
  const telegramStatus = app.querySelector('.telegram-status>span');
  if (telegramStatus) telegramStatus.textContent = backendStatus.telegram.ready ? 'Connected' : 'Not connected';
  app.querySelectorAll('[data-scan]').forEach(button => {
    const scheduled = backendStatus.mode === 'scheduled';
    const label = backendStatus.scanning ? checkingLabel() : scheduled ? 'Hourly checks' : 'Check now';
    if (button.textContent.trim() !== label) button.textContent = label;
    button.disabled = Boolean(backendStatus.scanning) || scheduled;
    button.title = scheduled ? 'Checks run hourly in GitHub Actions. Use Run workflow there for an immediate check.' : backendStatus.scanning ? 'Each source check has a four-minute deadline.' : backendStatus.next_scan ? `Next check: ${new Date(backendStatus.next_scan * 1000).toLocaleString()}` : 'Check sources now';
  });
  const monitorStatus = app.querySelector('.demo-status');
  if (monitorStatus) monitorStatus.innerHTML = `<span></span>${backendStatus.scanning ? 'Scanning' : 'Monitoring'}`;
  app.querySelectorAll('.source-note > div').forEach(row => {
    const state = backendStatus.sources[row.querySelector('strong')?.textContent];
    if (state) row.querySelector('.source-state').textContent = state.status.replaceAll('_', ' ');
  });
}

let locationGroups = {};
let wizard = null;
function openSearch(id = null) {
  if (!loaded || connectionError) { toast('Connect the monitoring service before editing searches.'); return; }
  const existing = searches.find(search => search.id === id);
  wizard = { step: 1, id, name: existing?.name || '', min: existing?.min ?? '', max: existing?.max ?? '', locations: [...(existing?.locations || [])], active: existing?.active ?? true };
  renderWizard(); dialog.showModal();
}
function renderWizard() {
  const steps = ['The basics', 'Location', 'Review'];
  dialog.innerHTML = `<div class="dialog-header"><div><div class="eyebrow">YOUR HOUSE HUNT</div><h2 id="dialog-title">${wizard.id ? 'Edit search' : 'Create a search'}</h2></div><button class="icon-button" data-close-dialog aria-label="Close dialog">${icon('x')}</button></div><div class="wizard-steps">${steps.map((label, index) => `<div class="${wizard.step === index + 1 ? 'current' : wizard.step > index + 1 ? 'complete' : ''}"><span>${wizard.step > index + 1 ? icon('check') : index + 1}</span>${label}</div>`).join('')}</div><form id="search-form"><div class="wizard-content">${wizard.step === 1 ? `<label class="field-label" for="search-name">Search name</label><input id="search-name" name="name" placeholder="e.g. Our next home" maxlength="60" value="${escape(wizard.name)}" required autofocus /><div class="field-label category-label">Property category</div><div class="category-selection">${icon('house')} Houses for sale ${icon('circle-check')}</div><div class="area-inputs"><div><label class="field-label" for="min-area">Minimum area <span>optional</span></label><div class="unit-input"><input type="number" id="min-area" name="min" min="1" max="1000000" step="any" placeholder="No minimum" value="${wizard.min}" /><span>m²</span></div></div><div><label class="field-label" for="max-area">Maximum area <span>optional</span></label><div class="unit-input"><input type="number" id="max-area" name="max" min="1" max="1000000" step="any" placeholder="No maximum" value="${wizard.max}" /><span>m²</span></div></div></div><p class="field-hint">House area only, not land area.</p>` : wizard.step === 2 ? `<h3>Where would you like to live?</h3><button type="button" class="all-latvia ${wizard.locations.length ? '' : 'chosen'}" id="all-latvia">${icon('map')}<span>All Latvia</span>${icon(wizard.locations.length ? 'circle' : 'circle-check')}</button><div class="location-search">${icon('search')}<input aria-label="Find a location" id="location-query" placeholder="Find a location…" /></div><div class="location-options">${Object.entries(locationGroups).map(([group, locations]) => `<div class="location-group"><h4>${group}</h4>${locations.map(location => `<label class="location-option" data-location-label="${escape(location.toLowerCase())}"><input type="checkbox" name="location" value="${escape(location)}" ${wizard.locations.includes(location) ? 'checked' : ''} /><span>${location}</span></label>`).join('')}</div>`).join('')}</div><p class="field-hint">${wizard.locations.length ? `${wizard.locations.length} locations selected` : 'No locations selected. All Latvia included.'}</p><p class="prototype-caption">Sample locations. The live bot will use the SS.lv hierarchy.</p>` : `<div class="review-icon">${icon('house')}</div><h3 class="review-title">${escape(wizard.name)}</h3><div class="review-row"><span>Category</span><strong>Houses for sale</strong></div><div class="review-row"><span>Property area</span><strong>${areaLabel(wizard)}</strong></div><div class="review-row"><span>Locations</span><strong>${wizard.locations.length ? wizard.locations.map(escape).join(', ') : 'All Latvia'}</strong></div><div class="review-row"><span>Check frequency</span><strong>Every 5 minutes</strong></div><div class="review-info">${icon('info')} Your search will be saved in this browser. Telegram and monitoring are not connected yet.</div>`}<p class="form-error" role="alert" id="form-error"></p></div><div class="dialog-footer"><button class="secondary-button" type="button" id="wizard-back">${wizard.step === 1 ? 'Cancel' : 'Back'}</button><button class="primary-button" type="submit">${wizard.step === 3 ? 'Save search' : 'Continue'} ${icon(wizard.step === 3 ? 'check' : 'arrow-right')}</button></div></form>`;
  dialog.querySelectorAll('.location-option').forEach(label => {
    const input = label.querySelector('input');
    const name = locationLabel(input.value);
    label.querySelector('span').textContent = name;
    label.dataset.locationLabel = name.toLowerCase();
    const browse = document.createElement('button');
    browse.type = 'button';
    browse.className = 'icon-button location-browse';
    browse.dataset.browse = input.value;
    browse.title = `Browse areas within ${name}`;
    browse.setAttribute('aria-label', `Browse areas within ${name}`);
    browse.innerHTML = icon('chevron-right');
    label.append(browse);
  });
  dialog.querySelector('.prototype-caption')?.remove();
  const info = dialog.querySelector('.review-info');
  if (info) info.textContent = 'Search saved on the monitoring server. New source coverage starts with a silent baseline.';
  dialog.querySelectorAll('.review-row').forEach(row => {
    if (row.querySelector('span')?.textContent === 'Locations') row.querySelector('strong').textContent = wizard.locations.length ? wizard.locations.map(locationLabel).join(', ') : 'All Latvia';
    if (row.querySelector('span')?.textContent === 'Check frequency') row.querySelector('strong').textContent = `Every ${intervalLabel()}`;
  });
  drawIcons();
}
dialog.addEventListener('submit', async event => {
  event.preventDefault();
  if (wizard.step === 1) {
    const form = new FormData(event.target);
    wizard.name = String(form.get('name')).trim(); wizard.min = form.get('min') ? Number(form.get('min')) : null; wizard.max = form.get('max') ? Number(form.get('max')) : null;
    if (!wizard.name) { document.querySelector('#form-error').textContent = 'Give your search a name.'; return; }
    if (wizard.min && wizard.max && wizard.min > wizard.max) { document.querySelector('#form-error').textContent = 'Maximum area must be at least the minimum area.'; return; }
  }
  if (wizard.step < 3) { wizard.step++; renderWizard(); return; }
  const submit = dialog.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api(wizard.id ? `/api/searches/${wizard.id}` : '/api/searches', { method: wizard.id ? 'PUT' : 'POST', body: JSON.stringify(wizard) });
    dialog.close();
    await refresh();
    toast(wizard.id ? 'Search updated. Listing history preserved.' : 'Your new search is saved.');
  } catch (error) { dialog.querySelector('#form-error').textContent = error.message; submit.disabled = false; }
});
dialog.addEventListener('click', async event => {
  const browse = event.target.closest('[data-browse]');
  if (browse) {
    event.preventDefault();
    const path = catalog[browse.dataset.browse]?.path;
    if (!path) return;
    browse.disabled = true;
    try {
      const children = await api(`/api/locations?path=${encodeURIComponent(path)}`);
      for (const child of children) catalog[child.id] = child;
      locationGroups = {};
      for (const location of Object.values(catalog)) (locationGroups[location.group || 'Latvia'] ||= []).push(location.id);
      renderWizard();
      if (!children.length) toast('This is the most specific available area.');
    } catch (error) { toast(error.message); browse.disabled = false; }
    return;
  }
  if (event.target === dialog) { const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close(); }
  if (event.target.closest('[data-close-dialog]')) dialog.close();
  if (event.target.closest('#wizard-back')) { if (wizard.step === 1) dialog.close(); else { wizard.step--; renderWizard(); } }
  if (event.target.closest('#all-latvia')) { wizard.locations = []; renderWizard(); }
});
dialog.addEventListener('change', event => { if (event.target.name === 'location') { wizard.locations = [...dialog.querySelectorAll('input[name="location"]:checked')].map(input => input.value); const button = dialog.querySelector('#all-latvia'); button.classList.toggle('chosen', !wizard.locations.length); dialog.querySelector('.field-hint').textContent = wizard.locations.length ? `${wizard.locations.length} locations selected` : 'No locations selected. All Latvia included.'; } });
dialog.addEventListener('input', event => { if (event.target.id === 'location-query') { const query = event.target.value.toLowerCase(); dialog.querySelectorAll('[data-location-label]').forEach(label => label.hidden = !label.dataset.locationLabel.includes(query)); dialog.querySelectorAll('.location-group').forEach(group => group.hidden = ![...group.querySelectorAll('.location-option')].some(option => !option.hidden)); } });
app.addEventListener('click', async event => {
  const button = event.target.closest('button, a.brand'); if (!button) return;
  try {
  if (button.matches('.brand')) { event.preventDefault(); view = 'listings'; selectedSearch = null; selectedLocation = null; render(); }
  if (button.dataset.nav) { view = button.dataset.nav; selectedSearch = null; selectedLocation = null; render(); }
  if (button.id === 'new-search' || button.hasAttribute('data-add-search')) openSearch();
  if (button.dataset.edit) openSearch(Number(button.dataset.edit));
  if (button.dataset.filter) { filter = button.dataset.filter; render(); }
  if (button.dataset.toggle) { const search = searches.find(item => item.id === Number(button.dataset.toggle)); await api(`/api/searches/${search.id}`, { method: 'PUT', body: JSON.stringify({ ...search, active: !search.active }) }); await refresh(); toast(`${search.name} ${search.active ? 'paused' : 'resumed'}.`); }
  if (button.dataset.delete) { const search = searches.find(item => item.id === Number(button.dataset.delete)); dialog.innerHTML = `<div class="dialog-header"><h2 id="dialog-title">Delete search?</h2><button class="icon-button" data-close-dialog aria-label="Close">${icon('x')}</button></div><div class="wizard-content"><p>“${escape(search.name)}” will be removed from this workspace. Listing history is retained.</p></div><div class="dialog-footer"><button class="secondary-button" data-close-dialog>Keep search</button><button class="danger-button" id="confirm-delete">Delete search</button></div>`; drawIcons(); dialog.showModal(); dialog.querySelector('#confirm-delete').onclick = async () => { try { await api(`/api/searches/${search.id}`, { method: 'DELETE' }); dialog.close(); await refresh(); toast('Search deleted.'); } catch (error) { toast(error.message); } }; }
  if (button.dataset.matches) { selectedSearch = Number(button.dataset.matches); selectedLocation = null; view = 'listings'; render(); }
  if (button.dataset.locationFilter) { selectedLocation = button.dataset.locationFilter; view = 'listings'; render(); }
  if (button.hasAttribute('data-clear-location')) { selectedLocation = null; render(); }
  if (button.id === 'view-listings' || button.id === 'show-all-listings') { view = 'listings'; selectedSearch = null; render(); }
  if (button.dataset.listing) { selectedListing = button.dataset.listing; render(); if (window.innerWidth < 1100) document.querySelector('.preview-panel').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  if (button.id === 'notifications-toggle') { await api('/api/settings', { method: 'PUT', body: JSON.stringify({ notifications_enabled: !notificationEnabled }) }); await refresh(); toast(`Notifications ${notificationEnabled ? 'enabled' : 'disabled'}.`); }
  if (button.hasAttribute('data-scan')) { await api('/api/scan', { method: 'POST', body: '{}' }); toast('Source check requested.'); await scheduleRefresh(); }
  } catch (error) { toast(error.message); }
});
let refreshTimer;
/** Follow the server interval when idle, checking in-flight scan progress briefly. */
async function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (!document.hidden && !dialog.open) await refresh();
  const idle = loaded && !connectionError && !backendStatus.scanning;
  refreshTimer = setTimeout(scheduleRefresh, idle ? backendStatus.interval * 1000 : 15000);
}
render();
scheduleRefresh();
document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleRefresh(); });