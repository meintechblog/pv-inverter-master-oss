// ===== Virtual PV Page =====

function renderVirtualPVPage(container) {
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading Virtual PV...</span></div>';

    _activeDeviceContainer = container;

    Promise.all([
        fetch('/api/devices/virtual/snapshot').then(function(r) { return r.json(); }),
        fetch('/api/config').then(function(r) { return r.json(); })
    ])
        .then(function(res) {
            _lastVirtualSnapshot = res[0];
            container.innerHTML = '';
            buildVirtualPVPage(container, res[0], res[1]);
        })
        .catch(function(err) {
            container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load Virtual PV data</div><p class="ve-hint-subtext">' + err.message + '</p></div>';
        });
}

var CONTRIBUTION_COLORS = ['var(--ve-blue)', 'var(--ve-orange)', 'var(--ve-green)', 'var(--ve-text-dim)', 'var(--ve-blue-light)', 'var(--ve-red)'];
var THROTTLE_STATE_COLORS = {
    active: 'var(--ve-green)',
    throttled: 'var(--ve-orange)',
    disabled: 'var(--ve-text-dim)',
    cooldown: 'var(--ve-blue)',
    startup: 'var(--ve-blue-light)'
};

function _buildAggregateScopeCard(config) {
    var card = document.createElement('div');
    card.className = 've-card';
    var vi = (config && config.virtual_inverter) || {};
    var selected = vi.location_ids || [];

    var rows;
    if (!_locations.length) {
        rows = '<p class="ve-hint-subtext">Noch keine Standorte angelegt — alle Inverter zählen zum Aggregat.</p>';
    } else {
        rows = _locations.map(function(l) {
            var chk = selected.indexOf(l.id) !== -1 ? ' checked' : '';
            return '<label class="ve-agg-loc-row">' +
                '<input type="checkbox" class="ve-agg-loc-cb" value="' + esc(l.id) + '"' + chk + '>' +
                '<span class="ve-agg-loc-name">' + esc(l.name || 'Standort') + '</span>' +
                '<span class="ve-text-dim">' + (l.inverter_count || 0) + ' Inverter</span>' +
                '</label>';
        }).join('');
    }

    card.innerHTML =
        '<h2 class="ve-card-title">Standorte im Aggregat</h2>' +
        '<p class="ve-hint-subtext" style="margin-bottom:10px">Wähle, welche Standorte zur Fronius-Proxy-Gesamtleistung zählen. Nichts ausgewählt = alle Standorte.</p>' +
        '<div class="ve-agg-loc-list">' + rows + '</div>' +
        '<span class="ve-btn-pair ve-agg-save-pair" style="display:none;margin-top:12px">' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-agg-cancel">Abbrechen</button>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-agg-save">Speichern</button>' +
        '</span>';

    var savePair = card.querySelector('.ve-agg-save-pair');
    var origKey = selected.slice().sort().join(',');
    function curSel() {
        return Array.prototype.map.call(
            card.querySelectorAll('.ve-agg-loc-cb:checked'),
            function(cb) { return cb.value; }
        );
    }
    function checkDirty() {
        savePair.style.display = (curSel().slice().sort().join(',') !== origKey) ? '' : 'none';
    }
    card.querySelectorAll('.ve-agg-loc-cb').forEach(function(cb) {
        cb.addEventListener('change', checkDirty);
    });

    var cancelBtn = card.querySelector('.ve-agg-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', function() {
        card.querySelectorAll('.ve-agg-loc-cb').forEach(function(cb) {
            cb.checked = selected.indexOf(cb.value) !== -1;
        });
        checkDirty();
    });

    var saveBtn = card.querySelector('.ve-agg-save');
    if (saveBtn) saveBtn.addEventListener('click', function() {
        var sel = curSel();
        saveBtn.disabled = true;
        saveBtn.textContent = 'Speichern…';
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ virtual_inverter: { location_ids: sel } })
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Speichern';
                if (d.success) {
                    showToast('Aggregat-Standorte gespeichert', 'success');
                    selected = sel;
                    origKey = sel.slice().sort().join(',');
                    checkDirty();
                } else {
                    showToast(d.error || 'Speichern fehlgeschlagen', 'error');
                }
            })
            .catch(function(e) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Speichern';
                showToast('Fehler: ' + e.message, 'error');
            });
    });

    return card;
}

function buildVirtualPVPage(container, data, config) {
    var totalW = data.total_power_w || 0;
    var totalRated = data.total_rated_w || 0;
    var contributions = data.contributions || [];

    // Gauge card (same style as inverter pages)
    var gaugeCard = document.createElement('div');
    gaugeCard.className = 've-card ve-gauge-card';
    var ratedW = totalRated > 0 ? totalRated : (totalW > 0 ? totalW * 1.2 : 30000);
    var pct = Math.min(totalW / ratedW, 1.0);
    var arcLength = GAUGE_ARC_LENGTH;
    var offset = arcLength * (1 - pct);
    var gc = gaugeColor(pct);

    // Build power limit dropdowns for virtual inverter
    var clampHtml = '';
    if (ratedW > 0) {
        var ctrl = data.control || {};
        var curMinPct = ctrl.clamp_min_pct || 0;
        var curMaxPct = ctrl.clamp_max_pct != null ? ctrl.clamp_max_pct : 100;
        var steps = [];
        var maxKw = Math.round(ratedW / 1000);
        for (var kw = maxKw - 1; kw >= 1; kw--) steps.push({w: kw * 1000, label: kw + ' kW'});
        var w1pct = Math.round(ratedW * 0.01);
        var label1pct = w1pct >= 1000 ? (w1pct / 1000).toFixed(1) + ' kW' : w1pct + ' W';
        var curMinW = Math.round(curMinPct * ratedW / 100);
        var curMaxW = curMaxPct >= 100 ? ratedW : Math.round(curMaxPct * ratedW / 100);
        function _vClosestStep(watts) {
            if (watts <= w1pct) return watts === 0 ? '0' : 'min';
            var best = steps[0]; var bestD = 99999;
            for (var i = 0; i < steps.length; i++) { var d = Math.abs(steps[i].w - watts); if (d < bestD) { bestD = d; best = steps[i]; } }
            return String(best.w);
        }
        var minOpts = '';
        for (var si = 0; si < steps.length; si++) {
            var s = steps[si];
            minOpts += '<option value="' + s.w + '"' + (_vClosestStep(curMinW) === String(s.w) ? ' selected' : '') + '>' + s.label + '</option>';
        }
        minOpts += '<option value="min"' + (curMinPct === 1 ? ' selected' : '') + '>' + label1pct + '</option>';
        minOpts += '<option value="0"' + (curMinPct === 0 ? ' selected' : '') + '>0 kW</option>';
        var maxOpts = '<option value="max"' + (curMaxPct >= 100 ? ' selected' : '') + '>Max</option>';
        for (var si2 = 0; si2 < steps.length; si2++) {
            var s2 = steps[si2];
            maxOpts += '<option value="' + s2.w + '"' + (curMaxPct < 100 && curMaxPct > 1 && _vClosestStep(curMaxW) === String(s2.w) ? ' selected' : '') + '>' + s2.label + '</option>';
        }
        maxOpts += '<option value="min"' + (curMaxPct === 1 ? ' selected' : '') + '>' + label1pct + '</option>';
        maxOpts += '<option value="0"' + (curMaxPct === 0 ? ' selected' : '') + '>0 kW</option>';
        clampHtml =
            '<div class="ve-gauge-clamp">' +
            '  <select class="ve-ctrl-dropdown ve-clamp-min" title="Minimum power (floor)">' + minOpts + '</select>' +
            '  <span class="ve-text-dim ve-clamp-label">Limit</span>' +
            '  <select class="ve-ctrl-dropdown ve-clamp-max" title="Maximum power (ceiling)">' + maxOpts + '</select>' +
            '</div>';
    }

    gaugeCard.innerHTML =
        '<h2 class="ve-card-title">Total Power Output</h2>' +
        '<svg viewBox="0 0 200 130" class="ve-gauge-svg">' +
        '  <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="var(--ve-border)" stroke-width="12" stroke-linecap="round"/>' +
        '  <path class="ve-gauge-fill ve-virtual-gauge-fill" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="' + gc + '" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + arcLength + '" stroke-dashoffset="' + offset + '"/>' +
        '  <text x="100" y="76" text-anchor="middle" fill="var(--ve-text)" font-size="32" font-weight="700" class="ve-virtual-gauge-value">' + formatW(totalW) + '</text>' +
        '  <text x="100" y="94" text-anchor="middle" fill="var(--ve-text-dim)" font-size="11">' + formatW(ratedW) + ' max</text>' +
        '  <text x="100" y="122" text-anchor="middle" fill="var(--ve-text-dim)" font-size="11">' + esc(data.virtual_name || 'Virtual PV') + '</text>' +
        '</svg>' + clampHtml;
    container.appendChild(gaugeCard);

    // Throttle info card (collapsible)
    var atCard = document.createElement('div');
    atCard.className = 've-card';
    atCard.innerHTML =
        '<details class="ve-auto-throttle-info">' +
        '  <summary>Wie funktioniert die Leistungsverteilung?</summary>' +
        '  <div class="ve-auto-throttle-info-body">' +
        '    <p>Das System verteilt das Leistungslimit automatisch auf alle Inverter \u2014 basierend auf deren Throttle-Score. H\u00f6herer Score = wird zuerst gedrosselt (reagiert am schnellsten).</p>' +
        '    <ul>' +
        '      <li><strong>Schnelle Inverter zuerst drosseln:</strong> Ger\u00e4te mit hohem Score reagieren am schnellsten und werden daher priorisiert gedrosselt</li>' +
        '      <li><strong>Proportional vs. Binary:</strong> Proportionale Inverter (z.B. SolarEdge) k\u00f6nnen stufenlos gedrosselt werden. Binary-Ger\u00e4te (z.B. Shelly) werden nur ein-/ausgeschaltet</li>' +
        '      <li><strong>Selbstlernend:</strong> Das System misst die tats\u00e4chliche Reaktionszeit jedes Inverters und optimiert die Reihenfolge automatisch</li>' +
        '      <li><strong>Cooldown-Schutz:</strong> Binary-Ger\u00e4te werden nicht zu schnell hintereinander geschaltet</li>' +
        '    </ul>' +
        '    <p style="margin-top:12px"><strong>Score-Formel (0\u201310):</strong></p>' +
        '    <p style="font-family:var(--ve-mono);font-size:0.8rem;color:var(--ve-text-secondary);margin:4px 0">Score = Basis + Reaktionsbonus \u2212 Cooldown-Abzug \u2212 Startup-Abzug</p>' +
        '    <ul>' +
        '      <li><strong>Basis:</strong> 7.0 (proportional) oder 3.0 (binary)</li>' +
        '      <li><strong>Reaktionsbonus:</strong> 0\u20133.0 \u2014 je schneller der Inverter reagiert, desto h\u00f6her (3.0 \u00d7 (1 \u2212 Reaktionszeit / 10s))</li>' +
        '      <li><strong>Cooldown-Abzug:</strong> 0\u20132.0 \u2014 l\u00e4ngerer Cooldown = gr\u00f6\u00dferer Abzug (nur binary)</li>' +
        '      <li><strong>Startup-Abzug:</strong> 0\u20131.0 \u2014 l\u00e4ngere Startverz\u00f6gerung = gr\u00f6\u00dferer Abzug (nur binary)</li>' +
        '    </ul>' +
        '    <p style="color:var(--ve-text-dim);margin-top:4px;font-size:0.8rem">Klicke auf eine Zeile in der Throttle-Tabelle um den Score-Breakdown zu sehen.</p>' +
        '  </div>' +
        '</details>';
    container.appendChild(atCard);

    // Wire up virtual power limit dropdown events
    var vClampMin = gaugeCard.querySelector('.ve-clamp-min');
    var vClampMax = gaugeCard.querySelector('.ve-clamp-max');
    if (vClampMin && vClampMax) {
        var _vRatedW = ratedW;
        function _vDdPct(dd) { if (dd.value === 'max') return 100; if (dd.value === 'min') return 1; if (dd.value === '0') return 0; return Math.round(parseInt(dd.value) / _vRatedW * 100); }
        function _vDdLabel(dd) { if (dd.value === 'max') return 'Max'; return dd.options[dd.selectedIndex].text; }
        function vSendClamp() {
            var minPct = _vDdPct(vClampMin); var maxPct = _vDdPct(vClampMax);
            if (minPct > maxPct) { vClampMin.value = vClampMax.value; minPct = maxPct; }
            fetch('/api/power-clamp', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: 'virtual', min_pct: minPct, max_pct: maxPct })
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) showToast('Limit: ' + _vDdLabel(vClampMin) + ' – ' + _vDdLabel(vClampMax), 'success');
                else showToast(d.error || 'Failed', 'error');
            }).catch(function(e) { showToast('Error: ' + e.message, 'error'); });
        }
        vClampMin.addEventListener('change', function() { if (_vDdPct(vClampMin) > _vDdPct(vClampMax)) vClampMax.value = vClampMin.value; vSendClamp(); });
        vClampMax.addEventListener('change', function() { if (_vDdPct(vClampMin) > _vDdPct(vClampMax)) vClampMin.value = vClampMax.value; vSendClamp(); });
    }

    // Contribution bar
    var barCard = document.createElement('div');
    barCard.className = 've-card';
    barCard.innerHTML = '<h2 class="ve-card-title">Power Contribution</h2>';

    var bar = document.createElement('div');
    bar.className = 've-contribution-bar';

    var legend = document.createElement('div');
    legend.className = 've-contribution-legend';

    for (var i = 0; i < contributions.length; i++) {
        var c = contributions[i];
        var pct = totalW > 0 ? (c.power_w / totalW * 100) : 0;
        var color = THROTTLE_STATE_COLORS[c.throttle_state] || CONTRIBUTION_COLORS[i % CONTRIBUTION_COLORS.length];

        var seg = document.createElement('div');
        seg.className = 've-contribution-segment';
        seg.style.width = pct.toFixed(1) + '%';
        seg.style.background = color;
        seg.setAttribute('data-device-id', c.device_id);
        bar.appendChild(seg);

        var legendItem = document.createElement('div');
        legendItem.className = 've-contribution-legend-item';
        legendItem.innerHTML =
            '<span class="ve-contribution-legend-dot" style="background:' + color + '"></span>' +
            '<span class="ve-contribution-legend-name">' + esc(c.name || c.device_id) + '</span>' +
            '<span class="ve-contribution-legend-power">' + formatW(c.power_w) + '</span>';
        legendItem.setAttribute('data-device-id', c.device_id);
        legend.appendChild(legendItem);
    }

    barCard.appendChild(bar);
    barCard.appendChild(legend);
    container.appendChild(barCard);

    // Aggregate Standort scope
    container.appendChild(_buildAggregateScopeCard(config));

    // Throttle table (enhanced 6-column)
    if (contributions.length > 0) {
        var throttleCard = document.createElement('div');
        throttleCard.className = 've-card';
        throttleCard.innerHTML = '<h2 class="ve-card-title">Throttle Overview</h2>';

        var table = document.createElement('table');
        table.className = 've-throttle-table';
        var thead = '<thead><tr><th>Name</th><th>Score</th><th>Modus</th><th>Reaktion</th><th>Limit</th><th>Status</th></tr></thead>';
        var tbody = '<tbody>';
        for (var j = 0; j < contributions.length; j++) {
            var ct = contributions[j];
            var scoreVal = ct.throttle_score != null ? '<span style="font-family:var(--ve-mono)">' + ct.throttle_score.toFixed(1) + '</span>' : '--';
            var modeVal = ct.throttle_mode || '--';
            var respVal = ct.measured_response_time_s != null ? '<span style="font-family:var(--ve-mono)">' + ct.measured_response_time_s.toFixed(1) + 's</span>' : '--';
            var limitVal = ct.current_limit_pct != null ? '<span style="font-family:var(--ve-mono)">' + ct.current_limit_pct.toFixed(1) + '%</span>' : '--';
            var stateColor = THROTTLE_STATE_COLORS[ct.throttle_state] || 'var(--ve-text-dim)';
            var stateDot = '<span class="ve-throttle-state-dot" style="background:' + stateColor + '"></span>';
            tbody += '<tr>' +
                '<td>' + esc(ct.name || ct.device_id) + '</td>' +
                '<td>' + scoreVal + '</td>' +
                '<td>' + modeVal + '</td>' +
                '<td>' + respVal + '</td>' +
                '<td>' + limitVal + '</td>' +
                '<td>' + stateDot + '</td>' +
                '</tr>';
            // Score breakdown row (collapsed by default)
            var sb = ct.score_breakdown;
            if (sb) {
                var formula = '<span class="ve-mono">' + sb.base.toFixed(1) + '</span> Basis (' + ct.throttle_mode + ')';
                if (sb.response_bonus > 0) formula += ' + <span class="ve-mono">' + sb.response_bonus.toFixed(1) + '</span> Reaktionsbonus (' + sb.response_time_s.toFixed(1) + 's)';
                if (sb.cooldown_penalty > 0) formula += ' \u2212 <span class="ve-mono">' + sb.cooldown_penalty.toFixed(1) + '</span> Cooldown (' + sb.cooldown_s + 's)';
                if (sb.startup_penalty > 0) formula += ' \u2212 <span class="ve-mono">' + sb.startup_penalty.toFixed(1) + '</span> Startup (' + sb.startup_delay_s + 's)';
                formula += ' = <strong>' + ct.throttle_score.toFixed(1) + '</strong>';
                tbody += '<tr class="ve-score-breakdown-row"><td colspan="6">' + formula + '</td></tr>';
            }
        }
        tbody += '</tbody>';
        table.innerHTML = thead + tbody;

        // Toggle breakdown rows on score cell click
        table.addEventListener('click', function(e) {
            var row = e.target.closest('tr');
            if (!row || row.classList.contains('ve-score-breakdown-row')) return;
            var next = row.nextElementSibling;
            if (next && next.classList.contains('ve-score-breakdown-row')) {
                next.classList.toggle('ve-score-breakdown-row--open');
            }
        });

        throttleCard.appendChild(table);
        container.appendChild(throttleCard);
    }
}

function updateVirtualPVPage(data) {
    if (!_activeDeviceContainer) return;

    var el = _activeDeviceContainer;

    var totalW = data.total_power_w || 0;
    var totalRated = data.total_rated_w || 0;
    var contributions = data.contributions || [];

    // Update gauge
    var gaugeFill = el.querySelector('.ve-virtual-gauge-fill');
    var gaugeVal = el.querySelector('.ve-virtual-gauge-value');
    if (gaugeFill && totalRated > 0) {
        var pct = Math.min(totalW / totalRated, 1.0);
        gaugeFill.style.strokeDashoffset = GAUGE_ARC_LENGTH * (1 - pct);
        gaugeFill.style.stroke = gaugeColor(pct);
    }
    if (gaugeVal) gaugeVal.textContent = formatW(totalW);

    // Check if contribution count changed -- rebuild if so
    var segments = el.querySelectorAll('.ve-contribution-segment');
    if (segments.length !== contributions.length) {
        // Rebuild entire page
        el.innerHTML = '';
        buildVirtualPVPage(el, data);
        return;
    }

    // Update bar segments with throttle state colors
    for (var i = 0; i < segments.length && i < contributions.length; i++) {
        var pct = totalW > 0 ? (contributions[i].power_w / totalW * 100) : 0;
        segments[i].style.width = pct.toFixed(1) + '%';
        var segColor = THROTTLE_STATE_COLORS[contributions[i].throttle_state] || CONTRIBUTION_COLORS[i % CONTRIBUTION_COLORS.length];
        segments[i].style.background = segColor;
    }

    // Update legend powers and dot colors
    var legendItems = el.querySelectorAll('.ve-contribution-legend-item');
    for (var j = 0; j < legendItems.length && j < contributions.length; j++) {
        var pwrEl = legendItems[j].querySelector('.ve-contribution-legend-power');
        if (pwrEl) pwrEl.textContent = formatW(contributions[j].power_w);
        var dotEl = legendItems[j].querySelector('.ve-contribution-legend-dot');
        if (dotEl) {
            var dotColor = THROTTLE_STATE_COLORS[contributions[j].throttle_state] || CONTRIBUTION_COLORS[j % CONTRIBUTION_COLORS.length];
            dotEl.style.background = dotColor;
        }
    }

    // Update enhanced throttle table (6 columns)
    var tds = el.querySelectorAll('.ve-throttle-table tbody tr');
    for (var k = 0; k < tds.length && k < contributions.length; k++) {
        var cells = tds[k].querySelectorAll('td');
        var ck = contributions[k];
        if (cells.length >= 6) {
            cells[1].innerHTML = ck.throttle_score != null ? '<span style="font-family:var(--ve-mono)">' + ck.throttle_score.toFixed(1) + '</span>' : '--';
            cells[2].textContent = ck.throttle_mode || '--';
            cells[3].innerHTML = ck.measured_response_time_s != null ? '<span style="font-family:var(--ve-mono)">' + ck.measured_response_time_s.toFixed(1) + 's</span>' : '--';
            cells[4].innerHTML = ck.current_limit_pct != null ? '<span style="font-family:var(--ve-mono)">' + ck.current_limit_pct.toFixed(1) + '%</span>' : '--';
            var stColor = THROTTLE_STATE_COLORS[ck.throttle_state] || 'var(--ve-text-dim)';
            var stDot = cells[5].querySelector('.ve-throttle-state-dot');
            if (stDot) stDot.style.background = stColor;
        }
    }
}

