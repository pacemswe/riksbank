/* Swedish Radar - browser client. No framework, no build step. */
'use strict';

const CRITERION_NAMES = {
  artist: 'Swedish artist',
  label: 'Swedish independent label',
  writer: 'Swedish songwriters',
};

const el = (id) => document.getElementById(id);

const state = {
  run: null,          // the run payload currently displayed
  matches: [],        // matches of that run
  activeChips: new Set(),
  pollTimer: null,
};

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { error: text }; }
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

function selectedCriteria() {
  return ['artist', 'label', 'writer'].filter((c) => el(`crit-${c}`).checked);
}

function formatDate(value) {
  if (!value) return 'unknown date';
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function relativeAge(days) {
  if (days === null || days === undefined) return '';
  if (days < 0) return 'upcoming';
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  if (days < 14) return 'last week';
  return `${Math.round(days / 7)} weeks ago`;
}

/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */

function renderChips() {
  const chips = el('chips');
  chips.textContent = '';
  const counts = { artist: 0, label: 0, writer: 0 };
  state.matches.forEach((m) => m.matched_criteria.forEach((c) => { counts[c] = (counts[c] || 0) + 1; }));

  Object.entries(CRITERION_NAMES).forEach(([key, name]) => {
    if (!counts[key]) return;
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = `${name} (${counts[key]})`;
    chip.setAttribute('aria-pressed', state.activeChips.has(key) ? 'true' : 'false');
    chip.addEventListener('click', () => {
      if (state.activeChips.has(key)) state.activeChips.delete(key);
      else state.activeChips.add(key);
      renderChips();
      renderResults();
    });
    chips.appendChild(chip);
  });
}

function visibleMatches() {
  const needle = el('filter').value.trim().toLowerCase();
  let rows = state.matches;

  if (state.activeChips.size) {
    rows = rows.filter((m) => m.matched_criteria.some((c) => state.activeChips.has(c)));
  }
  if (needle) {
    rows = rows.filter((m) => {
      const haystack = [
        m.name,
        m.label,
        ...(m.artists || []).map((a) => a.name),
      ].join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }

  const sort = el('sort').value;
  const sorted = rows.slice();
  if (sort === 'date') {
    sorted.sort((a, b) => (b.release_date || '').localeCompare(a.release_date || ''));
  } else if (sort === 'artist') {
    const nameOf = (m) => (m.artists && m.artists[0] ? m.artists[0].name : m.name);
    sorted.sort((a, b) => nameOf(a).localeCompare(nameOf(b), 'sv'));
  } else {
    sorted.sort((a, b) => b.score - a.score);
  }
  return sorted;
}

function buildEvidence(match) {
  const container = document.createElement('div');
  Object.entries(match.verdicts).forEach(([criterion, verdict]) => {
    const group = document.createElement('div');
    group.className = 'evidence-group';

    const heading = document.createElement('h4');
    heading.textContent = CRITERION_NAMES[criterion] || criterion;
    const verdictTag = document.createElement('span');
    verdictTag.className = `verdict ${verdict.matched ? 'yes' : 'no'}`;
    verdictTag.textContent = `${verdict.matched ? 'match' : 'no match'} · ${verdict.confidence.toFixed(2)}`;
    heading.appendChild(verdictTag);
    group.appendChild(heading);

    const summary = document.createElement('p');
    summary.className = 'songs';
    summary.textContent = verdict.summary;
    group.appendChild(summary);

    if (verdict.signals.length) {
      const list = document.createElement('ul');
      verdict.signals.forEach((signal) => {
        const item = document.createElement('li');
        const weight = document.createElement('span');
        weight.className = `weight ${signal.weight >= 0 ? 'pos' : 'neg'}`;
        weight.textContent = `${signal.weight >= 0 ? '+' : '−'}${Math.abs(signal.weight).toFixed(2)}`;
        const detail = document.createElement('span');
        detail.textContent = signal.detail;
        const source = document.createElement('span');
        source.className = 'src';
        source.textContent = ` (${signal.source})`;
        detail.appendChild(source);
        item.append(weight, detail);
        list.appendChild(item);
      });
      group.appendChild(list);
    }

    if (criterion === 'writer' && verdict.data && verdict.data.tracks_resolved) {
      const songs = document.createElement('p');
      songs.className = 'songs';
      const names = verdict.data.qualifying_track_names || [];
      songs.textContent = names.length
        ? `Swedish-written songs: ${names.join(', ')}`
        : `Credits resolved for ${verdict.data.tracks_resolved} of ${verdict.data.tracks_attempted} songs checked.`;
      group.appendChild(songs);
    }

    container.appendChild(group);
  });
  return container;
}

function renderResults() {
  const host = el('results');
  const template = el('card-template');
  const rows = visibleMatches();
  host.textContent = '';

  el('summary').textContent = state.run
    ? `${rows.length} of ${state.matches.length} match(es) shown · ` +
      `${(state.run.stats && state.run.stats.albums_in_window) || 0} release(s) examined · ` +
      `run from ${state.run.stored_at || state.run.generated_at || 'unknown time'}`
    : '';

  const empty = el('empty');
  if (!rows.length) {
    empty.hidden = false;
    empty.textContent = state.matches.length
      ? 'No releases match the current filter.'
      : 'No results yet. Choose your criteria and run a discovery pass.';
    return;
  }
  empty.hidden = true;

  const fragment = document.createDocumentFragment();
  rows.forEach((match) => {
    const node = template.content.cloneNode(true);
    // The artist and title are already in the text, so the cover is decorative.
    // Spotify's CDN URLs expire, so a failed load falls back to a placeholder
    // rather than leaving a broken-image icon in the card.
    const cover = node.querySelector('.cover');
    const image = cover.querySelector('img');
    image.alt = '';
    if (match.image_url) {
      image.src = match.image_url;
      image.addEventListener('error', () => {
        image.remove();
        cover.classList.add('placeholder');
        cover.textContent = '\u266a';
      }, { once: true });
    } else {
      image.remove();
      cover.classList.add('placeholder');
      cover.textContent = '\u266a';
    }

    node.querySelector('.card-artist').textContent =
      (match.artists || []).map((a) => a.name).join(', ') || 'Unknown artist';
    node.querySelector('.card-title').textContent = match.name;

    const age = relativeAge(match.age_days);
    node.querySelector('.card-meta').textContent =
      [formatDate(match.release_date) + (age ? ` (${age})` : ''),
       match.album_type,
       `${match.total_tracks} track${match.total_tracks === 1 ? '' : 's'}`]
      .filter(Boolean).join(' · ');

    const labelLine = node.querySelector('.card-label');
    if (match.label) {
      labelLine.append('Label: ');
      const strong = document.createElement('strong');
      strong.textContent = match.label;
      labelLine.appendChild(strong);
    } else {
      labelLine.remove();
    }

    const badges = node.querySelector('.badges');
    match.matched_criteria.forEach((criterion) => {
      const badge = document.createElement('span');
      badge.className = `badge ${criterion}`;
      badge.textContent = CRITERION_NAMES[criterion] || criterion;
      badges.appendChild(badge);
    });

    const reasons = node.querySelector('.reasons');
    match.matched_criteria.forEach((criterion) => {
      const verdict = match.verdicts[criterion];
      const item = document.createElement('li');
      const conf = document.createElement('span');
      conf.className = 'conf';
      conf.textContent = `${verdict.confidence.toFixed(2)} `;
      item.append(conf, document.createTextNode(verdict.summary));
      reasons.appendChild(item);
    });

    const listen = node.querySelector('.listen');
    listen.href = match.spotify_url;

    const toggle = node.querySelector('.evidence-toggle');
    const evidence = node.querySelector('.evidence');
    toggle.addEventListener('click', () => {
      const open = evidence.hidden;
      if (open && !evidence.childElementCount) evidence.appendChild(buildEvidence(match));
      evidence.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.textContent = open ? 'Hide evidence' : 'Show evidence';
    });

    fragment.appendChild(node);
  });
  host.appendChild(fragment);
}

function showRun(run) {
  state.run = run;
  state.matches = (run && run.matches) || [];
  state.activeChips.clear();
  renderChips();
  renderResults();
}

/* ------------------------------------------------------------------ */
/* run lifecycle                                                       */
/* ------------------------------------------------------------------ */

function setJobView(job) {
  const box = el('job');
  box.hidden = false;
  box.dataset.status = job.status;
  const labels = { queued: 'Queued…', running: 'Scanning…', done: 'Finished', error: 'Failed' };
  el('job-status').textContent = labels[job.status] || job.status;
  const log = el('job-log');
  log.textContent = (job.error ? [job.error, ...job.log] : job.log).join('\n');
  log.scrollTop = log.scrollHeight;
}

async function pollJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    setJobView(job);
    if (job.status === 'done' || job.status === 'error') {
      el('run').disabled = false;
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      if (job.status === 'done') {
        await loadHistory();
        showRun(await api('/api/runs/latest'));
      }
    }
  } catch (error) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    el('run').disabled = false;
    setJobView({ status: 'error', log: [], error: error.message });
  }
}

async function startRun() {
  const criteria = selectedCriteria();
  if (!criteria.length) {
    setJobView({ status: 'error', log: [], error: 'Select at least one criterion.' });
    return;
  }
  el('run').disabled = true;
  const markets = el('markets').value.split(',').map((m) => m.trim()).filter(Boolean);
  try {
    const job = await api('/api/discover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        criteria,
        days: Number(el('days').value),
        min_confidence: Number(el('confidence').value),
        max_albums: Number(el('max-albums').value),
        markets: markets.length ? markets : ['SE'],
      }),
    });
    setJobView(job);
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => pollJob(job.id), 1200);
  } catch (error) {
    el('run').disabled = false;
    setJobView({ status: 'error', log: [], error: error.message });
  }
}

async function loadHistory() {
  const { runs } = await api('/api/runs?limit=30');
  const select = el('history');
  select.textContent = '';
  if (!runs.length) {
    const option = document.createElement('option');
    option.textContent = 'No previous runs';
    option.value = '';
    select.appendChild(option);
    return;
  }
  runs.forEach((run) => {
    const option = document.createElement('option');
    option.value = String(run.id);
    option.textContent = `#${run.id} · ${run.created_at.replace('T', ' ').replace('+00:00', '')} · ${run.match_count} match(es)`;
    select.appendChild(option);
  });
}

function exportJson() {
  if (!state.run) return;
  const blob = new Blob([JSON.stringify(state.run, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `swedish-radar-run-${state.run.run_id || 'latest'}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

/* ------------------------------------------------------------------ */
/* bootstrap                                                           */
/* ------------------------------------------------------------------ */

async function init() {
  el('confidence').addEventListener('input', (event) => {
    el('confidence-out').textContent = Number(event.target.value).toFixed(2);
  });
  el('run').addEventListener('click', startRun);
  el('filter').addEventListener('input', renderResults);
  el('sort').addEventListener('change', renderResults);
  el('export').addEventListener('click', exportJson);
  el('history').addEventListener('change', async (event) => {
    if (!event.target.value) return;
    showRun(await api(`/api/runs/${event.target.value}`));
  });

  try {
    const config = await api('/api/config');
    el('days').value = String(config.days);
    el('markets').value = config.markets.join(',');
    el('confidence').value = String(config.min_confidence);
    el('confidence-out').textContent = Number(config.min_confidence).toFixed(2);
    el('max-albums').value = String(config.max_albums);

    const status = el('config-status');
    if (!config.has_credentials) {
      status.className = 'topbar-status warn';
      status.textContent = 'Spotify credentials missing — set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.';
      el('run').disabled = true;
    } else {
      status.textContent = `${config.curated_labels} curated labels · MusicBrainz ${config.musicbrainz ? 'on' : 'off'}`;
    }
  } catch (error) {
    el('config-status').textContent = `Could not load configuration: ${error.message}`;
  }

  await loadHistory();
  try {
    const latest = await api('/api/runs/latest');
    if (!latest.empty) showRun(latest);
    else renderResults();
  } catch {
    renderResults();
  }
}

document.addEventListener('DOMContentLoaded', init);
