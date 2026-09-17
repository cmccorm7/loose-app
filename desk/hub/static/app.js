/* YM Desk front end. No framework, no build step, no CDN: the page has to work
   on a machine with no network, because that is where the data lives. */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/* ---------------------------------------------------------------- api --- */

async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch { /* empty body is fine */ }
  if (!response.ok) {
    throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
  }
  return payload;
}
const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }),
  put: (path, body) => request(path, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }),
  del: (path) => request(path, { method: 'DELETE' }),
  upload: (file) => {
    const form = new FormData();
    form.append('file', file);
    return request('/api/statements', { method: 'POST', body: form });
  },
};

/* ------------------------------------------------------------ helpers --- */

const state = { settings: {}, instruments: [], overview: null };

const money = (value, digits = 2) =>
  value == null ? '—'
    : `${value < 0 ? '-' : ''}$${Math.abs(value).toLocaleString(undefined, {
        minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const signed = (value, digits = 2) =>
  value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
const percent = (value, digits = 1) =>
  value == null ? '—' : `${value.toFixed(digits)}%`;
const shortDate = (iso) => (iso ? iso.slice(0, 10) : '—');
const plural = (count, word, many = `${word}s`) =>
  `${count} ${count === 1 ? word : many}`;
const escapeHtml = (text) => String(text ?? '').replace(/[&<>"']/g,
  (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind ? `toast-${kind}` : ''}`;
  node.textContent = message;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 8000 : 4000);
}

function openModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; $('#modal-body').innerHTML = ''; }

/* ------------------------------------------------------------- charts --- */
/* Colours come from CSS custom properties so the theme toggle repaints charts
   without re-rendering them. */

const tooltip = () => $('#tooltip');
function showTip(event, html) {
  const node = tooltip();
  node.innerHTML = html;
  node.hidden = false;
  const box = node.getBoundingClientRect();
  const left = Math.min(event.clientX + 14, window.innerWidth - box.width - 8);
  const top = Math.max(8, event.clientY - box.height - 12);
  node.style.left = `${left}px`;
  node.style.top = `${top}px`;
}
function hideTip() { tooltip().hidden = true; }

const svgEl = (name, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, value);
  }
  return node;
};

/** An accessible name for the chart, read by screen readers. */
function titleNode(text) {
  const node = svgEl('title', {});
  node.textContent = text;
  return node;
}

function niceTicks(min, max, count = 5) {
  if (min === max) { return [min]; }
  const raw = (max - min) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude)
    .find((candidate) => candidate >= raw) || magnitude * 10;
  const ticks = [];
  for (let value = Math.ceil(min / step) * step; value <= max + 1e-9; value += step) {
    ticks.push(Number(value.toFixed(10)));
  }
  return ticks;
}

/** A bar anchored to the baseline with only its data-end rounded. */
function barPath(x, width, baseline, value, radius) {
  const height = Math.abs(value - baseline);
  const r = Math.min(radius, width / 2, height);
  const up = value < baseline;              // SVG y grows downward
  const top = up ? value : baseline;
  const bottom = up ? baseline : value;
  if (height < 0.5) { return `M${x} ${baseline}h${width}`; }
  return up
    ? `M${x} ${bottom}V${top + r}a${r} ${r} 0 0 1 ${r} ${-r}h${width - 2 * r}`
      + `a${r} ${r} 0 0 1 ${r} ${r}V${bottom}Z`
    : `M${x} ${top}V${bottom - r}a${r} ${r} 0 0 0 ${r} ${r}h${width - 2 * r}`
      + `a${r} ${r} 0 0 0 ${r} ${-r}V${top}Z`;
}

function lineChart(container, points, options = {}) {
  container.innerHTML = '';
  if (!points.length) {
    container.innerHTML = '<p class="muted small">Nothing to plot yet.</p>';
    return;
  }
  const width = Math.max(container.clientWidth || 720, 320);
  const height = options.height || 260;
  const pad = { top: 12, right: 16, bottom: 26, left: 62 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const values = points.map((point) => point.y);
  let min = Math.min(...values);
  let max = Math.max(...values);
  const span = max - min || Math.abs(max) * 0.02 || 1;
  min -= span * 0.08; max += span * 0.08;

  const xOf = (index) => pad.left
    + (points.length === 1 ? plotW / 2 : (index / (points.length - 1)) * plotW);
  const yOf = (value) => pad.top + plotH - ((value - min) / (max - min)) * plotH;

  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' });
  svg.append(titleNode(options.title || 'Equity over closed trades'));

  for (const tick of niceTicks(min, max, 4)) {
    const y = yOf(tick);
    svg.append(svgEl('line', {
      class: 'grid-line', x1: pad.left, x2: width - pad.right, y1: y, y2: y,
    }));
    const label = svgEl('text', {
      class: 'axis-label', x: pad.left - 8, y: y + 4, 'text-anchor': 'end',
    });
    label.textContent = options.formatY ? options.formatY(tick) : tick;
    svg.append(label);
  }

  const path = points.map((point, index) =>
    `${index ? 'L' : 'M'}${xOf(index).toFixed(1)} ${yOf(point.y).toFixed(1)}`).join('');
  svg.append(svgEl('path', {
    class: 'series-area',
    d: `${path}L${xOf(points.length - 1)} ${pad.top + plotH}L${xOf(0)} ${pad.top + plotH}Z`,
  }));
  svg.append(svgEl('path', { class: 'series-line', d: path }));

  const first = svgEl('text', {
    class: 'axis-label', x: pad.left, y: height - 8, 'text-anchor': 'start',
  });
  first.textContent = points[0].label || '';
  const last = svgEl('text', {
    class: 'axis-label', x: width - pad.right, y: height - 8, 'text-anchor': 'end',
  });
  last.textContent = points.at(-1).label || '';
  svg.append(first, last);

  /* Crosshair: the hover layer is part of the chart, not an extra. */
  const crosshair = svgEl('line', {
    class: 'crosshair', y1: pad.top, y2: pad.top + plotH, opacity: 0,
  });
  const dot = svgEl('circle', { class: 'focus-dot', r: 4.5, opacity: 0 });
  svg.append(crosshair, dot);

  const surface = svgEl('rect', {
    x: pad.left, y: pad.top, width: plotW, height: plotH, fill: 'transparent',
  });
  surface.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const x = (event.clientX - box.left) * scale;
    const ratio = points.length === 1 ? 0 : (x - pad.left) / plotW;
    const index = Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1))));
    const point = points[index];
    crosshair.setAttribute('x1', xOf(index));
    crosshair.setAttribute('x2', xOf(index));
    crosshair.setAttribute('opacity', 1);
    dot.setAttribute('cx', xOf(index));
    dot.setAttribute('cy', yOf(point.y));
    dot.setAttribute('opacity', 1);
    showTip(event, `<div class="tooltip-title">${escapeHtml(point.label || '')}</div>`
      + `${options.formatTip ? options.formatTip(point) : point.y}`);
  });
  surface.addEventListener('mouseleave', () => {
    crosshair.setAttribute('opacity', 0);
    dot.setAttribute('opacity', 0);
    hideTip();
  });
  svg.append(surface);
  container.append(svg);
}

function barChart(container, bars, options = {}) {
  container.innerHTML = '';
  if (!bars.length) {
    container.innerHTML = '<p class="muted small">Nothing to plot yet.</p>';
    return;
  }
  const width = Math.max(container.clientWidth || 720, 320);
  const height = options.height || 240;
  const pad = { top: 14, right: 16, bottom: 34, left: 62 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const values = bars.map((bar) => bar.value);
  const max = Math.max(0, ...values);
  const min = Math.min(0, ...values);
  const span = (max - min) || 1;
  const yOf = (value) => pad.top + plotH - ((value - min) / span) * plotH;
  const zero = yOf(0);

  const gap = 2;                                   // surface gap between bars
  const slot = plotW / bars.length;
  const barWidth = Math.max(1, Math.min(48, slot - gap));

  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' });
  svg.append(titleNode(options.title || 'Breakdown'));

  for (const tick of niceTicks(min, max, 4)) {
    const y = yOf(tick);
    svg.append(svgEl('line', {
      class: tick === 0 ? 'axis-line' : 'grid-line',
      x1: pad.left, x2: width - pad.right, y1: y, y2: y,
    }));
    const label = svgEl('text', {
      class: 'axis-label', x: pad.left - 8, y: y + 4, 'text-anchor': 'end',
    });
    label.textContent = options.formatY ? options.formatY(tick) : tick;
    svg.append(label);
  }

  const labelEvery = Math.ceil(bars.length / Math.max(1, Math.floor(plotW / 54)));
  bars.forEach((bar, index) => {
    const x = pad.left + index * slot + (slot - barWidth) / 2;
    const y = yOf(bar.value);
    svg.append(svgEl('path', {
      class: bar.value >= 0 ? 'bar-pos' : 'bar-neg',
      d: barPath(x, barWidth, zero, y, 4),
    }));
    if (index % labelEvery === 0) {
      const label = svgEl('text', {
        class: 'axis-label', x: x + barWidth / 2, y: height - 14, 'text-anchor': 'middle',
      });
      label.textContent = bar.label;
      svg.append(label);
    }
    const hit = svgEl('rect', {
      class: 'bar-hit', x: pad.left + index * slot, y: pad.top,
      width: slot, height: plotH,
    });
    hit.addEventListener('mousemove', (event) => showTip(event,
      `<div class="tooltip-title">${escapeHtml(bar.label)}</div>`
      + (options.formatTip ? options.formatTip(bar) : signed(bar.value))));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(hit);
  });

  container.append(svg);
}

/* --------------------------------------------------------------- views --- */

function tile(label, value, note = '', tone = '') {
  return `<div class="tile">
    <div class="tile-label">${escapeHtml(label)}</div>
    <div class="tile-value ${tone}">${value}</div>
    ${note ? `<div class="tile-note">${escapeHtml(note)}</div>` : ''}
  </div>`;
}

async function renderDashboard() {
  const overview = await api.get('/api/overview');
  state.overview = overview;
  $('#nav-statement-count').textContent = overview.statement_count || '';

  if (!overview.has_data) {
    $('#dashboard-empty').hidden = false;
    $('#dashboard-body').hidden = true;
    $('#dashboard-period').textContent = 'No trades yet.';
    return;
  }
  $('#dashboard-empty').hidden = true;
  $('#dashboard-body').hidden = false;

  const m = overview.metrics;
  $('#dashboard-period').textContent =
    `${plural(overview.trade_count, 'trade')}, ${shortDate(overview.period.first)} to `
    + `${shortDate(overview.period.last)}, across ${plural(m.trading_days, 'trading day')}.`;

  const tone = (value) => (value > 0 ? 'pos' : value < 0 ? 'neg' : '');
  $('#stat-tiles').innerHTML = [
    tile('Net P&L', money(m.net_pnl), `after ${money(m.commission)} commission`, tone(m.net_pnl)),
    tile('Expectancy', signed(m.expectancy_r, 3) + 'R',
      m.expectancy_r == null ? 'no stops recorded' : 'per trade',
      tone(m.expectancy_r)),
    tile('Win rate', percent(m.win_rate), `${m.wins}W / ${m.losses}L`),
    tile('Profit factor',
      m.profit_factor == null ? (m.profit_factor_note || '—') : m.profit_factor.toFixed(2),
      'gross win / gross loss'),
    tile('Max drawdown', money(m.max_drawdown), percent(m.max_drawdown_pct, 2), 'neg'),
    tile('Avg win / loss', `${money(m.avg_win, 0)} / ${money(m.avg_loss, 0)}`,
      `worst streak ${m.max_consecutive_losses}L`),
  ].join('');

  const equity = await api.get('/api/equity');
  lineChart($('#equity-chart'), equity.curve.map((point, index) => ({
    y: point.equity,
    label: point.t ? point.t.slice(0, 16).replace('T', ' ') : 'start',
    index,
  })), {
    formatY: (value) => money(value, 0),
    formatTip: (point) => `Equity ${money(point.y)}`,
    title: 'Equity after each closed trade',
  });

  barChart($('#daily-chart'), equity.daily.map((day) => ({
    label: day.day.slice(5), value: day.pnl, full: day.day,
  })), {
    formatY: (value) => money(value, 0),
    formatTip: (bar) => `${money(bar.value)} on ${bar.full}`,
    title: 'Profit and loss by trading day',
  });
}

function statementRow(record) {
  const statusClass = `status-${record.status}`;
  const detail = record.status === 'imported'
    ? `${plural(record.rows_imported, 'trade')} imported`
    : record.status === 'failed'
      ? record.error
      : `${plural(record.rows_detected, 'row')} detected — not imported yet`;
  const period = record.period?.first
    ? ` · ${shortDate(record.period.first)} to ${shortDate(record.period.last)}` : '';
  return `<div class="statement" data-id="${record.id}">
    <div class="statement-main">
      <div class="statement-name">${escapeHtml(record.filename)}</div>
      <div class="statement-meta">${escapeHtml(detail)}${escapeHtml(period)}</div>
    </div>
    <span class="status ${statusClass}">${record.status}</span>
    <div class="statement-actions">
      ${record.status === 'pending'
        ? `<button class="primary-button" data-action="review" data-id="${record.id}">Review</button>` : ''}
      <button class="danger-button" data-action="delete" data-id="${record.id}">Delete</button>
    </div>
  </div>`;
}

async function renderStatements() {
  const { statements } = await api.get('/api/statements');
  $('#nav-statement-count').textContent = statements.length || '';
  $('#statement-list').innerHTML = statements.length
    ? statements.map(statementRow).join('')
    : '<p class="muted">No statements uploaded yet.</p>';
}

function previewHtml(statement, preview) {
  const notes = (preview.notes || []).map(
    (note) => `<div class="note note-warn">${escapeHtml(note)}</div>`).join('');
  const warnings = preview.warnings?.length
    ? `<div class="note note-warn"><strong>${plural(preview.warnings.length, 'row')}
        could not be read.</strong><br>${preview.warnings.slice(0, 5).map(escapeHtml).join('<br>')}</div>`
    : '';
  const rows = (preview.sample || []).map((trade) => `<tr>
      <td>${escapeHtml(trade.entry_time.slice(0, 16).replace('T', ' '))}</td>
      <td>${escapeHtml(trade.symbol)}</td>
      <td>${escapeHtml(trade.direction)}</td>
      <td>${trade.contracts}</td>
      <td>${trade.entry_price}</td>
      <td>${trade.exit_price ?? '—'}</td>
      <td class="${trade.net_pnl > 0 ? 'pos' : trade.net_pnl < 0 ? 'neg' : ''}">${money(trade.net_pnl)}</td>
    </tr>`).join('');

  if (preview.kind === 'bars') {
    return `<p><strong>${escapeHtml(preview.format_label)}</strong> — ${preview.row_count} bars.</p>
      ${notes}<pre class="muted small">${escapeHtml(preview.summary)}</pre>
      <div class="modal-actions">
        <button class="ghost-button" data-action="close">Close</button>
      </div>`;
  }
  return `
    <p><strong>${escapeHtml(preview.format_label)}</strong> — ${escapeHtml(preview.summary)}.
       ${preview.period.days ? `${plural(preview.period.days, 'trading day')}, ` : ''}
       ${shortDate(preview.period.first)} to ${shortDate(preview.period.last)}.</p>
    ${notes}${warnings}
    <div class="table-wrap"><table>
      <thead><tr><th>Entry</th><th>Symbol</th><th>Side</th><th>Qty</th>
        <th>In</th><th>Out</th><th>Net</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <p class="muted small">Showing the first ${preview.sample.length} of ${preview.row_count}.</p>
    <div class="form-grid" style="margin-top:14px">
      <label>Default stop distance (points)
        <input type="number" id="import-stop" step="1"
               value="${state.settings.default_stop_points ?? ''}"
               placeholder="leave blank to import without R-multiples">
        <span class="hint">Applied to trades with no stop of their own. This is what
          makes R-multiples — and most of the behavioral analysis — possible.</span>
      </label>
    </div>
    <div class="modal-actions">
      <button class="ghost-button" data-action="close">Cancel</button>
      <button class="primary-button" data-action="import" data-id="${statement.id}">
        Import ${plural(preview.row_count, 'trade')}
      </button>
    </div>`;
}

async function uploadFiles(files) {
  for (const file of files) {
    try {
      const result = await api.upload(file);
      await renderStatements();
      if (result.error) {
        toast(`${file.name}: ${result.error}`, 'error');
        continue;
      }
      openModal(`Preview — ${file.name}`,
        previewHtml(result.statement, result.preview));
    } catch (error) {
      toast(`${file.name}: ${error.message}`, 'error');
    }
  }
}

async function renderAnalysis() {
  const by = $('#breakdown-by').value;
  const data = await api.get(`/api/breakdown?by=${encodeURIComponent(by)}`);
  const label = $('#breakdown-by').selectedOptions[0].textContent;
  $('#breakdown-title').textContent = `Expectancy by ${label.toLowerCase()}`;

  const usable = data.buckets.filter((bucket) => bucket.expectancy_r !== null);
  const bars = (usable.length ? usable : data.buckets).map((bucket) => ({
    label: bucket.name,
    value: usable.length ? bucket.expectancy_r : bucket.net_pnl,
    bucket,
  }));
  barChart($('#breakdown-chart'), bars, {
    formatY: (value) => (usable.length ? signed(value, 2) : money(value, 0)),
    formatTip: (bar) => `${bar.bucket.trades} trades · ${money(bar.bucket.net_pnl)}`
      + `<br>expectancy ${bar.bucket.expectancy_r == null ? money(bar.bucket.expectancy)
        : `${signed(bar.bucket.expectancy_r, 3)}R`}`,
    title: `Expectancy by ${label}`,
  });

  $('#breakdown-table').innerHTML = `
    <thead><tr><th>${escapeHtml(label)}</th><th>Trades</th><th>Win%</th>
      <th>Net P&L</th><th>Exp $</th><th>Exp R</th><th>PF</th></tr></thead>
    <tbody>${data.buckets.map((bucket) => `<tr>
      <td>${escapeHtml(bucket.name)}</td>
      <td>${bucket.trades}</td>
      <td>${percent(bucket.win_rate)}</td>
      <td class="${bucket.net_pnl > 0 ? 'pos' : bucket.net_pnl < 0 ? 'neg' : ''}">${money(bucket.net_pnl)}</td>
      <td>${money(bucket.expectancy)}</td>
      <td class="${bucket.expectancy_r > 0 ? 'pos' : bucket.expectancy_r < 0 ? 'neg' : ''}">
        ${bucket.expectancy_r == null ? '—' : signed(bucket.expectancy_r, 3)}</td>
      <td>${bucket.profit_factor == null ? '—' : bucket.profit_factor.toFixed(2)}</td>
    </tr>`).join('')}</tbody>`;

  const { trades, shown, total } = await api.get('/api/trades?limit=50');
  $('#trades-table').innerHTML = `
    <thead><tr><th>Day</th><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th>
      <th>Net</th><th>R</th><th>Setup</th></tr></thead>
    <tbody>${[...trades].reverse().map((trade) => `<tr>
      <td>${escapeHtml(trade.session_day)}</td>
      <td>${escapeHtml(trade.entry_time.slice(11, 16))}</td>
      <td>${escapeHtml(trade.symbol)}</td>
      <td>${escapeHtml(trade.direction)}</td>
      <td>${trade.contracts}</td>
      <td class="${trade.net_pnl > 0 ? 'pos' : trade.net_pnl < 0 ? 'neg' : ''}">${money(trade.net_pnl)}</td>
      <td class="${trade.r_multiple > 0 ? 'pos' : trade.r_multiple < 0 ? 'neg' : ''}">
        ${trade.r_multiple == null ? '—' : signed(trade.r_multiple, 2)}</td>
      <td>${escapeHtml(trade.setup || '—')}</td>
    </tr>`).join('')}</tbody>`;
  if (total > shown) {
    $('#trades-table').insertAdjacentHTML('afterend',
      `<p class="muted small">Showing the most recent ${shown} of ${total}.</p>`);
  }
}

async function renderBehavior() {
  const report = await api.get('/api/behavior');
  const body = $('#behavior-body');
  $('#nav-finding-count').textContent =
    report.actionable_count ? report.actionable_count : '';

  if (!report.findings.length) {
    body.innerHTML = `<div class="empty-state">
      <h2>Nothing to report yet</h2>
      <p>${escapeHtml(report.notes[0]
        || 'No pattern cleared the minimum sample size. Most detectors need 40+ trades before they can say anything.')}</p>
    </div>`;
    return;
  }

  const guardrails = Object.entries(report.guardrails).map(([key, value]) =>
    `<span class="guardrail-chip">${escapeHtml(key)} = ${escapeHtml(value)}</span>`).join('');

  body.innerHTML = `
    <div class="card">
      <h2>Suggested guardrails</h2>
      <p class="muted small">Merged from the findings below. Review before adopting —
        a guardrail you do not believe in will not hold.</p>
      <div>${guardrails || '<span class="muted">None yet.</span>'}</div>
    </div>
    ${report.notes.map((note) => `<div class="note">${escapeHtml(note)}</div>`).join('')}
    ${report.findings.map((finding) => `
      <article class="finding finding-${finding.severity}">
        <div class="finding-head">
          <h3>${escapeHtml(finding.headline)}</h3>
          <span class="severity severity-${finding.severity}">${finding.severity}</span>
        </div>
        <p class="finding-detail">${escapeHtml(finding.detail)}</p>
        <div class="finding-suggestion">${escapeHtml(finding.suggestion)}</div>
        <div class="finding-stats">
          n=${finding.sample} · effect ${signed(finding.effect, 2)}${escapeHtml(finding.effect_unit || report.unit)}
          · p≈${finding.p_value == null ? 'n/a' : finding.p_value.toFixed(3)}
          · confidence ${escapeHtml(finding.confidence)}
        </div>
      </article>`).join('')}
    <p class="muted small">These are hypotheses drawn from ${report.trades} trades over
      ${report.days} days, not laws. Act on the ones you recognise.</p>`;
}

async function renderSizing() {
  const form = $('#size-form');
  $('#size-equity').value = state.settings.equity;
  if (!form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const result = await api.post('/api/size', {
          symbol: $('#size-symbol').value,
          equity: $('#size-equity').value,
          entry: $('#size-entry').value,
          stop: $('#size-stop').value,
          target: $('#size-target').value,
        });
        $('#size-result').innerHTML = `
          <div class="note ${result.approved ? '' : 'note-warn'}" style="margin-top:14px">
            <strong>${result.approved
              ? `${result.contracts} contract${result.contracts === 1 ? '' : 's'}`
              : 'Not a trade this account can take'}</strong>
            ${result.approved ? ` risking ${money(result.risk_dollars)}` : ''}
          </div>
          <table style="margin-top:8px">
            <tbody>
              <tr><td>Stop distance</td><td>${result.risk_points} pts / ${result.risk_ticks} ticks</td></tr>
              <tr><td>Per contract</td><td>${money(result.per_contract_risk)}</td></tr>
              ${result.reward_risk != null
                ? `<tr><td>Reward : risk</td><td>${result.reward_risk.toFixed(2)}R</td></tr>` : ''}
              <tr><td>Commission (round turn)</td><td>${money(result.commission)}</td></tr>
            </tbody>
          </table>
          ${result.blockers.map((item) =>
            `<div class="note note-warn">${escapeHtml(item)}</div>`).join('')}
          ${result.warnings.map((item) =>
            `<div class="note">${escapeHtml(item)}</div>`).join('')}`;
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }
}

async function renderSettings() {
  const settings = state.settings;
  $('#set-equity').value = settings.equity;
  $('#set-risk').value = settings.risk_per_trade_pct;
  $('#set-daily').value = settings.max_daily_loss_pct;
  $('#set-drawdown').value = settings.max_drawdown_pct;
  $('#set-tz').value = settings.timezone;
  $('#set-stop').value = settings.default_stop_points ?? '';
  const form = $('#settings-form');
  if (!form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const stop = $('#set-stop').value;
      try {
        state.settings = await api.put('/api/settings', {
          symbol: $('#set-symbol').value,
          equity: Number($('#set-equity').value),
          risk_per_trade_pct: Number($('#set-risk').value),
          max_daily_loss_pct: Number($('#set-daily').value),
          max_drawdown_pct: Number($('#set-drawdown').value),
          timezone: $('#set-tz').value.trim(),
          default_stop_points: stop === '' ? null : Number(stop),
        });
        toast('Settings saved', 'success');
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }
}

/* ------------------------------------------------------------- routing --- */

const renderers = {
  dashboard: renderDashboard,
  statements: renderStatements,
  analysis: renderAnalysis,
  behavior: renderBehavior,
  sizing: renderSizing,
  settings: renderSettings,
};

async function show(view) {
  $$('.nav-item').forEach((button) =>
    button.classList.toggle('is-active', button.dataset.view === view));
  $$('.view').forEach((section) =>
    section.classList.toggle('is-active', section.id === `view-${view}`));
  location.hash = view;
  try {
    await renderers[view]();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ---------------------------------------------------------------- init --- */

async function init() {
  try {
    state.settings = await api.get('/api/settings');
    const { instruments } = await api.get('/api/instruments');
    state.instruments = instruments;
    const optionsHtml = instruments.map((instrument) =>
      `<option value="${instrument.symbol}">${instrument.symbol} — ${escapeHtml(instrument.name)}</option>`
    ).join('');
    for (const id of ['#set-symbol', '#size-symbol']) {
      $(id).innerHTML = optionsHtml;
      $(id).value = state.settings.symbol;
    }
    const health = await api.get('/api/health');
    $('#engine-version').textContent = `v${health.version}`;
    $('#data-folder').textContent = health.data_dir;
  } catch (error) {
    toast(`Could not reach the app: ${error.message}`, 'error');
  }

  $$('.nav-item').forEach((button) =>
    button.addEventListener('click', () => show(button.dataset.view)));
  $$('[data-goto]').forEach((button) =>
    button.addEventListener('click', () => show(button.dataset.goto)));

  $('#theme-toggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme === 'dark'
      || (!document.documentElement.dataset.theme
        && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    localStorage.setItem('ym-desk-theme', document.documentElement.dataset.theme);
  });
  const savedTheme = localStorage.getItem('ym-desk-theme');
  if (savedTheme) { document.documentElement.dataset.theme = savedTheme; }

  /* uploads */
  const dropzone = $('#dropzone');
  $('#browse-button').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', (event) => {
    uploadFiles([...event.target.files]);
    event.target.value = '';
  });
  ['dragenter', 'dragover'].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add('is-over');
    }));
  ['dragleave', 'drop'].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove('is-over');
    }));
  dropzone.addEventListener('drop', (event) =>
    uploadFiles([...event.dataTransfer.files]));

  /* statement actions */
  $('#statement-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) { return; }
    const { action, id } = button.dataset;
    try {
      if (action === 'delete') {
        const record = (await api.get('/api/statements')).statements
          .find((item) => item.id === id);
        const extra = record?.status === 'imported'
          ? ` This also removes the ${record.rows_imported} trades it added.` : '';
        if (!confirm(`Delete ${record?.filename || 'this statement'}?${extra}`)) { return; }
        const result = await api.del(`/api/statements/${id}`);
        toast(`Deleted. ${plural(result.trades_removed, 'trade')} removed.`, 'success');
        await renderStatements();
        await renderDashboard();
      }
      if (action === 'review') {
        toast('Re-upload the file to review it again.', '');
      }
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  /* modal actions */
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (event) => {
    if (event.target.id === 'modal') { closeModal(); }
  });
  $('#modal-body').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) { return; }
    if (button.dataset.action === 'close') { closeModal(); return; }
    if (button.dataset.action === 'import') {
      button.disabled = true;
      const stop = $('#import-stop')?.value;
      try {
        const result = await api.post(`/api/statements/${button.dataset.id}/import`,
          stop === '' || stop == null ? {} : { default_stop_points: Number(stop) });
        closeModal();
        toast(`Imported ${plural(result.imported, 'trade')}.`, 'success');
        await renderStatements();
        await show('dashboard');
      } catch (error) {
        toast(error.message, 'error');
        button.disabled = false;
      }
    }
  });

  $('#breakdown-by').addEventListener('change', renderAnalysis);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeModal(); }
  });

  let resizeTimer;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const active = $('.view.is-active')?.id.replace('view-', '');
      if (active === 'dashboard' || active === 'analysis') { renderers[active](); }
    }, 180);
  });

  await show(location.hash.slice(1) in renderers ? location.hash.slice(1) : 'dashboard');
}

init();
