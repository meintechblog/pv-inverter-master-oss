// ===== Inverter Dashboard Renderer =====

// Compact summary of a string's panel groups for the DC table:
//   one group  → "8 × 545 Wp"
//   N groups   → "8×545 + 4×440 Wp (Σ 6120)"
//   no panels  → "—"
function _formatPanelGroups(s) {
    var groups = (s && s.panels) || [];
    if (!groups.length) return '—';
    if (groups.length === 1) {
        return groups[0].count + ' × ' + groups[0].wp_each + ' Wp';
    }
    var total = 0;
    var parts = groups.map(function(g) {
        total += (g.count || 0) * (g.wp_each || 0);
        return (g.count || 0) + '×' + (g.wp_each || 0);
    });
    return parts.join(' + ') + ' Wp (Σ ' + total + ')';
}

function renderInverterDashboard(container, deviceId, deviceType) {
    _gaugePeakW = 0;   // reset auto-scale peak for the device being opened
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading device data...</span></div>';

    fetch('/api/devices/' + deviceId + '/snapshot')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            container.innerHTML = '';
            buildInverterDashboard(container, data, deviceType);
        })
        .catch(function(err) {
            container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load device data</div><p class="ve-hint-subtext">' + err.message + '</p></div>';
        });
}

function buildInverterDashboard(container, data, deviceType) {
    var inv = data.inverter || {};

    // Check disabled state
    if (data.enabled === false) {
        container.style.position = 'relative';
        var overlay = document.createElement('div');
        overlay.className = 've-device-disabled-overlay';
        overlay.textContent = 'Device deaktiviert';
        container.appendChild(overlay);
    }

    // Row 1: Gauge + Type-specific data
    var topRow = document.createElement('div');
    topRow.className = 've-dashboard-top';

    // Gauge card
    var gaugeCard = document.createElement('div');
    gaugeCard.className = 've-card ve-gauge-card';
    var ratedW = _gaugeMaxW(data);
    var acPower = inv.ac_power_w || 0;
    var pct = Math.min(acPower / ratedW, 1.0);
    var arcLength = GAUGE_ARC_LENGTH;
    var offset = arcLength * (1 - pct);
    var gc = gaugeColor(pct);

    // Power limit dropdowns
    var clampHtml = '';
    if (ratedW > 0) {
        var isBinary = deviceType === 'shelly';
        var ctrl = data.control || {};
        var curMinPct = ctrl.clamp_min_pct || 0;
        var curMaxPct = ctrl.clamp_max_pct != null ? ctrl.clamp_max_pct : 100;

        var zeroLabel = ratedW >= 2000 ? '0 kW' : '0 W';
        var maxLabel = ratedW >= 2000 ? (ratedW / 1000) + ' kW' : ratedW + ' W';
        var minOpts, maxOpts;

        if (isBinary) {
            // Binary: only 0 and Max (on/off)
            minOpts = '<option value="max"' + (curMinPct >= 100 ? ' selected' : '') + '>Max</option>' +
                '<option value="0"' + (curMinPct === 0 ? ' selected' : '') + '>' + zeroLabel + '</option>';
            maxOpts = '<option value="max"' + (curMaxPct >= 100 ? ' selected' : '') + '>Max</option>' +
                '<option value="0"' + (curMaxPct === 0 ? ' selected' : '') + '>' + zeroLabel + '</option>';
        } else {
            // Proportional: full step list
            // ≥ 2kW: 1kW steps, < 2kW: 50W steps
            var steps = [];
            if (ratedW >= 2000) {
                var maxKw = Math.round(ratedW / 1000);
                for (var kw = maxKw - 1; kw >= 1; kw--) steps.push({w: kw * 1000, label: kw + ' kW'});
            } else {
                var stepW = 50;
                for (var w = ratedW - stepW; w >= stepW; w -= stepW) steps.push({w: w, label: w + ' W'});
            }
            var w1pct = Math.round(ratedW * 0.01);
            var label1pct = w1pct >= 1000 ? (w1pct / 1000).toFixed(1) + ' kW' : w1pct + ' W';

            var curMinW = Math.round(curMinPct * ratedW / 100);
            var curMaxW = curMaxPct >= 100 ? ratedW : Math.round(curMaxPct * ratedW / 100);

            function _closestStep(watts) {
                if (watts <= w1pct) return watts === 0 ? '0' : 'min';
                var best = steps[0]; var bestD = 99999;
                for (var i = 0; i < steps.length; i++) {
                    var d = Math.abs(steps[i].w - watts);
                    if (d < bestD) { bestD = d; best = steps[i]; }
                }
                return String(best.w);
            }

            minOpts = '';
            for (var i = 0; i < steps.length; i++) {
                var s = steps[i];
                minOpts += '<option value="' + s.w + '"' + (_closestStep(curMinW) === String(s.w) ? ' selected' : '') + '>' + s.label + '</option>';
            }
            minOpts += '<option value="min"' + (curMinPct === 1 ? ' selected' : '') + '>' + label1pct + '</option>';
            minOpts += '<option value="0"' + (curMinPct === 0 ? ' selected' : '') + '>' + zeroLabel + '</option>';

            maxOpts = '<option value="max"' + (curMaxPct >= 100 ? ' selected' : '') + '>Max</option>';
            for (var i = 0; i < steps.length; i++) {
                var s = steps[i];
                maxOpts += '<option value="' + s.w + '"' + (curMaxPct < 100 && curMaxPct > 1 && _closestStep(curMaxW) === String(s.w) ? ' selected' : '') + '>' + s.label + '</option>';
            }
            maxOpts += '<option value="min"' + (curMaxPct === 1 ? ' selected' : '') + '>' + label1pct + '</option>';
            maxOpts += '<option value="0"' + (curMaxPct === 0 ? ' selected' : '') + '>' + zeroLabel + '</option>';
        }

        clampHtml =
            '<div class="ve-gauge-clamp">' +
            '  <select class="ve-ctrl-dropdown ve-clamp-min" title="Minimum power (floor)">' + minOpts + '</select>' +
            '  <span class="ve-text-dim ve-clamp-label">Limit</span>' +
            '  <select class="ve-ctrl-dropdown ve-clamp-max" title="Maximum power (ceiling)">' + maxOpts + '</select>' +
            '</div>';
    }

    gaugeCard.innerHTML =
        '<h2 class="ve-card-title">Power Output</h2>' +
        '<svg viewBox="0 0 200 130" class="ve-gauge-svg">' +
        '  <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="var(--ve-border)" stroke-width="12" stroke-linecap="round"/>' +
        '  <path class="ve-gauge-fill" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="' + gc + '" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + arcLength + '" stroke-dashoffset="' + offset + '"/>' +
        '  <text x="100" y="76" text-anchor="middle" fill="var(--ve-text)" font-size="32" font-weight="700" class="ve-gauge-value-text">' + formatW(acPower) + '</text>' +
        '  <text x="100" y="94" text-anchor="middle" fill="var(--ve-text-dim)" font-size="11">' + formatW(ratedW) + ' max</text>' +
        '  <text x="100" y="122" text-anchor="middle" fill="var(--ve-text-dim)" font-size="11" class="ve-gauge-status-text">' + esc(data.display_name || data.inverter_name || '--') + '</text>' +
        '</svg>' + clampHtml;
    topRow.appendChild(gaugeCard);

    // Wire up power limit dropdown events
    var clampMinDD = gaugeCard.querySelector('.ve-clamp-min');
    var clampMaxDD = gaugeCard.querySelector('.ve-clamp-max');
    if (clampMinDD && clampMaxDD) {
        var _ratedW = ratedW;
        function _ddPct(dd) {
            if (dd.value === 'max') return 100;
            if (dd.value === 'min') return 1;
            if (dd.value === '0') return 0;
            return Math.round(parseInt(dd.value) / _ratedW * 100);
        }
        function _ddLabel(dd) {
            if (dd.value === 'max') return 'Max';
            return dd.options[dd.selectedIndex].text;
        }
        function sendClamp() {
            var minPct = _ddPct(clampMinDD);
            var maxPct = _ddPct(clampMaxDD);
            if (minPct > maxPct) { clampMinDD.value = clampMaxDD.value; minPct = maxPct; }
            fetch('/api/power-clamp', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: _activeDeviceId, min_pct: minPct, max_pct: maxPct })
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) showToast('Limit: ' + _ddLabel(clampMinDD) + ' – ' + _ddLabel(clampMaxDD), 'success');
                else showToast(d.error || 'Failed', 'error');
            }).catch(function(e) { showToast('Error: ' + e.message, 'error'); });
        }
        clampMinDD.addEventListener('change', function() {
            if (_ddPct(clampMinDD) > _ddPct(clampMaxDD)) clampMaxDD.value = clampMinDD.value;
            sendClamp();
        });
        clampMaxDD.addEventListener('change', function() {
            if (_ddPct(clampMinDD) > _ddPct(clampMaxDD)) clampMinDD.value = clampMaxDD.value;
            sendClamp();
        });
    }

    // Type-specific card
    if (deviceType === 'opendtu' || deviceType === 'shelly' || deviceType === 'hoymiles' || deviceType === 'senec') {
        topRow.appendChild(buildDCChannelCard(data));
    } else {
        topRow.appendChild(buildPhaseCard(data));
    }

    // Throttle info card (only for devices with throttle capabilities)
    if (data.throttle_mode && data.throttle_mode !== 'none') {
        var throttleInfoCard = document.createElement('div');
        throttleInfoCard.className = 've-card';
        var tiScore = (data.throttle_score || 0).toFixed(1);
        var tiMode = data.throttle_mode.charAt(0).toUpperCase() + data.throttle_mode.slice(1);
        var tiResp = data.measured_response_time_s != null
            ? '<span style="font-family:var(--ve-mono)">' + data.measured_response_time_s.toFixed(1) + 's</span>'
            : 'Measuring...';
        throttleInfoCard.innerHTML =
            '<h2 class="ve-card-title">Throttle Info</h2>' +
            '<div class="ve-throttle-info-grid">' +
            '  <span class="ve-text-dim">Score</span><span style="font-family:var(--ve-mono)">' + tiScore + '</span>' +
            '  <span class="ve-text-dim">Mode</span><span>' + tiMode + '</span>' +
            '  <span class="ve-text-dim">Reaktion</span><span>' + tiResp + '</span>' +
            '</div>';
        topRow.appendChild(throttleInfoCard);
    }

    container.appendChild(topRow);

    // Row 2: Connection + Performance
    var row2 = document.createElement('div');
    row2.className = 've-dashboard-info-row';

    // Connection card
    var connCard = document.createElement('div');
    connCard.className = 've-card ve-conn-card';
    var connState = data.connection ? data.connection.state : 'unknown';
    var connDotClass = connState === 'connected' ? 've-dot--ok' : connState === 'reconnecting' ? 've-dot--warn' : connState === 'night_mode' ? 've-dot--dim' : 've-dot--err';
    connCard.innerHTML =
        '<h2 class="ve-card-title">Connection</h2>' +
        '<div class="ve-status-row"><span class="ve-dot ' + connDotClass + '"></span><span>Inverter: ' + (connState === 'night_mode' ? 'sleeping' : connState) + '</span></div>';

    if (deviceType === 'shelly') {
        var relayStatus = inv.status === 'MPPT' ? 'On' : 'Off';
        var relayDot = inv.status === 'MPPT' ? 've-dot' : 've-dot ve-dot--dim';
        connCard.innerHTML +=
            '<div style="margin-top:10px">' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Relay:</span><span class="' + relayDot + '" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 6px;background:' + (inv.status === 'MPPT' ? 'var(--ve-green)' : 'var(--ve-text-dim)') + '"></span><span class="ve-shelly-relay-state">' + relayStatus + '</span></div>' +
            '</div>' +
            '<div class="ve-shelly-actions" style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">' +
            '  <button class="ve-btn ve-btn--sm ve-shelly-on">Switch On</button>' +
            '  <button class="ve-btn ve-btn--sm ve-btn--cancel ve-shelly-off">Switch Off</button>' +
            '</div>';

        var _shellyDevId = data.device_id || _activeDeviceId;
        function _sendShellySwitch(on) {
            fetch('/api/devices/' + _shellyDevId + '/shelly/switch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ on: on })
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) {
                    showToast('Relay ' + (on ? 'On' : 'Off'), 'success');
                    var stateEl = connCard.querySelector('.ve-shelly-relay-state');
                    if (stateEl) stateEl.textContent = on ? 'On' : 'Off';
                } else {
                    showToast(d.error || 'Failed', 'error');
                }
            }).catch(function(e) { showToast('Error: ' + e.message, 'error'); });
        }
        var shellyOnBtn = connCard.querySelector('.ve-shelly-on');
        var shellyOffBtn = connCard.querySelector('.ve-shelly-off');
        if (shellyOnBtn) shellyOnBtn.addEventListener('click', function() { _sendShellySwitch(true); });
        if (shellyOffBtn) shellyOffBtn.addEventListener('click', function() { _sendShellySwitch(false); });
    }

    if (deviceType === 'opendtu') {
        var dtuCached = data.opendtu_status || null;
        connCard.innerHTML +=
            '<div class="ve-opendtu-status" style="margin-top:10px">' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Producing:</span><span class="ve-opendtu-producing">' + (dtuCached ? (dtuCached.producing ? 'Yes' : 'No') : '...') + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Reachable:</span><span class="ve-opendtu-reachable">' + (dtuCached ? (dtuCached.reachable ? 'Yes' : 'No') : '...') + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Limit:</span><span class="ve-opendtu-limit">' + (dtuCached ? dtuCached.limit_relative + '% (' + dtuCached.limit_absolute + ' W)' : '...') + '</span></div>' +
            '</div>' +
            '<div class="ve-opendtu-actions" style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">' +
            '  <button class="ve-btn ve-btn--sm ve-opendtu-restart">Restart</button>' +
            '  <button class="ve-btn ve-btn--sm ve-opendtu-on">Power On</button>' +
            '  <button class="ve-btn ve-btn--sm ve-btn--cancel ve-opendtu-off">Power Off</button>' +
            '</div>';

        // Fetch live OpenDTU status (periodic refresh every 10s)
        var _devId = data.device_id || _activeDeviceId;
        var _dtuInterval = null;
        function _refreshDtuStatus() {
            // Stop if card is no longer in DOM (page navigated away)
            if (!connCard.parentNode) { if (_dtuInterval) clearInterval(_dtuInterval); return; }
            fetch('/api/devices/' + _devId + '/opendtu/status')
                .then(function(r) { return r.json(); })
                .then(function(s) {
                    var prodEl = connCard.querySelector('.ve-opendtu-producing');
                    var reachEl = connCard.querySelector('.ve-opendtu-reachable');
                    var limEl = connCard.querySelector('.ve-opendtu-limit');
                    if (s.error) {
                        if (prodEl) prodEl.textContent = '--';
                        if (reachEl) reachEl.textContent = '--';
                        if (limEl) limEl.textContent = s.error;
                        return;
                    }
                    if (prodEl) prodEl.textContent = s.producing ? 'Yes' : 'No';
                    if (reachEl) reachEl.textContent = s.reachable ? 'Yes' : 'No';
                    if (limEl) limEl.textContent = s.limit_relative + '% (' + s.limit_absolute + ' W)';
                }).catch(function() {});
        }
        _refreshDtuStatus();
        _dtuInterval = setInterval(_refreshDtuStatus, 10000);

        // Wire power buttons
        function _sendPower(action) {
            fetch('/api/devices/' + _devId + '/opendtu/power', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: action })
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) { showToast('Inverter: ' + action, 'success'); setTimeout(_refreshDtuStatus, 3000); }
                else showToast(d.error || 'Failed', 'error');
            }).catch(function(e) { showToast('Error: ' + e.message, 'error'); });
        }
        var restartBtn = connCard.querySelector('.ve-opendtu-restart');
        var onBtn = connCard.querySelector('.ve-opendtu-on');
        var offBtn = connCard.querySelector('.ve-opendtu-off');
        if (restartBtn) restartBtn.addEventListener('click', function() { _sendPower('restart'); });
        if (onBtn) onBtn.addEventListener('click', function() { _sendPower('on'); });
        if (offBtn) offBtn.addEventListener('click', function() { _sendPower('off'); });
    }

    if (deviceType === 'senec') {
        var snC = data.senec_status || null;
        var snState = '…', snPv = '…', snHouse = '…', snGrid = '…', snBat = '…', snTemp = '…';
        if (snC) {
            snState = esc(snC.state_name || ('Code ' + snC.state_code));
            snPv = (snC.pv_power_w || 0) + ' W';
            snHouse = (snC.house_power_w || 0) + ' W';
            var snGp = snC.grid_power_w || 0;
            snGrid = snGp >= 0 ? (snGp + ' W Bezug') : ((-snGp) + ' W Einspeisung');
            snBat = (snC.battery_temp_c == null && !snC.soc_pct && !snC.battery_power_w)
                ? 'offline'
                : (snC.soc_pct + ' % · ' + (snC.battery_power_w || 0) + ' W');
            snTemp = senecTempStr(snC);
        }
        connCard.innerHTML +=
            '<div class="ve-senec-status" style="margin-top:10px">' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Systemstatus:</span><span class="ve-senec-state">' + snState + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">PV-Leistung:</span><span class="ve-senec-pv">' + snPv + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Hausverbrauch:</span><span class="ve-senec-house">' + snHouse + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Netz:</span><span class="ve-senec-grid">' + snGrid + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Batterie:</span><span class="ve-senec-bat">' + snBat + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Temperatur:</span><span class="ve-senec-temp">' + snTemp + '</span></div>' +
            '</div>';
    }

    if (deviceType === 'hoymiles') {
        var hmCached = data.hoymiles_status || null;
        var hmLimitTxt = hmCached ? (hmCached.limit_pct + ' %') : '...';
        var hmFw = hmCached ? (hmCached.firmware_version || '--') : '...';
        connCard.innerHTML +=
            '<div class="ve-hoymiles-status" style="margin-top:10px">' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Producing:</span><span class="ve-hoymiles-producing">' + (hmCached ? (hmCached.producing ? 'Yes' : 'No') : '...') + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Reachable:</span><span class="ve-hoymiles-reachable">' + (hmCached ? (hmCached.reachable ? 'Yes' : 'No') : '...') + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Limit:</span><span class="ve-hoymiles-limit">' + hmLimitTxt + '</span></div>' +
            '  <div class="ve-status-row"><span class="ve-text-dim">Firmware:</span><span class="ve-hoymiles-fw">' + esc(hmFw) + '</span></div>' +
            '</div>' +
            '<div class="ve-hoymiles-actions" style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">' +
            '  <button class="ve-btn ve-btn--sm ve-hoymiles-restart">Restart</button>' +
            '  <button class="ve-btn ve-btn--sm ve-hoymiles-on">Power On</button>' +
            '  <button class="ve-btn ve-btn--sm ve-btn--cancel ve-hoymiles-off">Power Off</button>' +
            '</div>' +
            '<p class="ve-text-dim" style="margin-top:8px;font-size:0.75rem">' +
            'Power-Limits werden ins EEPROM geschrieben — bitte nicht für kontinuierliches Zero-Feed-In nutzen.' +
            '</p>';

        var _hmDevId = data.device_id || _activeDeviceId;
        var _hmInterval = null;
        function _refreshHmStatus() {
            if (!connCard.parentNode) { if (_hmInterval) clearInterval(_hmInterval); return; }
            fetch('/api/devices/' + _hmDevId + '/hoymiles/status')
                .then(function(r) { return r.json(); })
                .then(function(s) {
                    var prodEl = connCard.querySelector('.ve-hoymiles-producing');
                    var reachEl = connCard.querySelector('.ve-hoymiles-reachable');
                    var limEl = connCard.querySelector('.ve-hoymiles-limit');
                    var fwEl = connCard.querySelector('.ve-hoymiles-fw');
                    if (s.error) { if (limEl) limEl.textContent = s.error; return; }
                    if (prodEl) prodEl.textContent = s.producing ? 'Yes' : 'No';
                    if (reachEl) reachEl.textContent = s.reachable ? 'Yes' : 'No';
                    if (limEl) limEl.textContent = (s.limit_pct != null ? s.limit_pct : '--') + ' %';
                    if (fwEl) fwEl.textContent = s.firmware_version || '--';
                }).catch(function() {});
        }
        _refreshHmStatus();
        _hmInterval = setInterval(_refreshHmStatus, 10000);

        function _sendHmPower(action) {
            fetch('/api/devices/' + _hmDevId + '/hoymiles/power', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: action })
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) { showToast('Inverter: ' + action, 'success'); setTimeout(_refreshHmStatus, 3000); }
                else showToast(d.error || 'Failed', 'error');
            }).catch(function(e) { showToast('Error: ' + e.message, 'error'); });
        }
        var hmRestartBtn = connCard.querySelector('.ve-hoymiles-restart');
        var hmOnBtn = connCard.querySelector('.ve-hoymiles-on');
        var hmOffBtn = connCard.querySelector('.ve-hoymiles-off');
        if (hmRestartBtn) hmRestartBtn.addEventListener('click', function() { _sendHmPower('restart'); });
        if (hmOnBtn) hmOnBtn.addEventListener('click', function() { _sendHmPower('on'); });
        if (hmOffBtn) hmOffBtn.addEventListener('click', function() { _sendHmPower('off'); });
    }

    row2.appendChild(connCard);

    // Performance card
    var perfCard = document.createElement('div');
    perfCard.className = 've-card';
    perfCard.innerHTML =
        '<h2 class="ve-card-title">Today\'s Performance</h2>' +
        '<div class="ve-grid">' +
        '  <div><label>Energy</label><span class="ve-live-value ve-daily-energy">' + ((inv.daily_energy_wh || 0) / 1000).toFixed(1) + ' kWh</span></div>' +
        '  <div><label>Peak Power</label><span class="ve-live-value ve-peak-power">' + (inv.peak_power_w != null ? formatW(inv.peak_power_w) : '--') + '</span></div>' +
        '  <div><label>Status</label><span class="ve-live-value ve-inv-status">' + (inv.status || '--') + '</span></div>' +
        '  <div><label>Temperature</label><span class="ve-live-value ve-inv-temp">' + formatTemp(deviceTemp(inv)) + '</span></div>' +
        '</div>';
    row2.appendChild(perfCard);
    container.appendChild(row2);
}

function buildPhaseCard(data) {
    var inv = data.inverter || {};
    var card = document.createElement('div');
    card.className = 've-card ve-phases-card';

    var l1v = inv.ac_voltage_an_v, l1a = inv.ac_current_l1_a;
    var l2v = inv.ac_voltage_bn_v, l2a = inv.ac_current_l2_a;
    var l3v = inv.ac_voltage_cn_v, l3a = inv.ac_current_l3_a;

    function fmtV(v) { return v != null ? v.toFixed(1) + ' V' : '--'; }
    function fmtA(a) { return a != null ? a.toFixed(2) + ' A' : '--'; }
    function fmtW(v, a) { return (v != null && a != null) ? formatW(v * a) : '--'; }
    function fmtHz(f) { return f != null ? f.toFixed(1) + ' Hz' : '--'; }

    // AC summary row
    var eff = inv.efficiency_pct;
    var freq = inv.ac_frequency_hz;
    var dcV = inv.dc_voltage_v, dcA = inv.dc_current_a, dcW = inv.dc_power_w;
    var temp = deviceTemp(inv);

    card.innerHTML =
        '<h2 class="ve-card-title">AC Output' +
        '  <span class="ve-card-subtitle">' + fmtHz(freq) +
            ' · <span class="ve-se-eff">' + (eff != null ? eff.toFixed(1) + '%' : '--') + '</span> eff' +
            ' · <span class="ve-se-temp">' + formatTemp(temp) + '</span>' +
        '</span></h2>' +
        ((!l2v && !l2a && !l3v && !l3a) ?
        '<table class="ve-phase-table"><thead><tr><th>Voltage</th><th>Current</th><th>Power</th></tr></thead>' +
        '<tbody>' +
        '<tr><td class="ve-live-value ve-l1-voltage">' + fmtV(l1v) + '</td><td class="ve-live-value ve-l1-current">' + fmtA(l1a) + '</td><td class="ve-live-value ve-l1-power">' + fmtW(l1v, l1a) + '</td></tr>' +
        '</tbody></table>'
        :
        '<table class="ve-phase-table"><thead><tr><th></th><th>Voltage</th><th>Current</th><th>Power</th></tr></thead>' +
        '<tbody>' +
        '<tr><td class="ve-phase-label">L1</td><td class="ve-live-value ve-l1-voltage">' + fmtV(l1v) + '</td><td class="ve-live-value ve-l1-current">' + fmtA(l1a) + '</td><td class="ve-live-value ve-l1-power">' + fmtW(l1v, l1a) + '</td></tr>' +
        '<tr><td class="ve-phase-label">L2</td><td class="ve-live-value ve-l2-voltage">' + fmtV(l2v) + '</td><td class="ve-live-value ve-l2-current">' + fmtA(l2a) + '</td><td class="ve-live-value ve-l2-power">' + fmtW(l2v, l2a) + '</td></tr>' +
        '<tr><td class="ve-phase-label">L3</td><td class="ve-live-value ve-l3-voltage">' + fmtV(l3v) + '</td><td class="ve-live-value ve-l3-current">' + fmtA(l3a) + '</td><td class="ve-live-value ve-l3-power">' + fmtW(l3v, l3a) + '</td></tr>' +
        '</tbody></table>') +
        ((dcV || dcA || dcW) ?
        '<h2 class="ve-card-title" style="margin-top:12px">DC Input' +
        '  <span class="ve-card-subtitle"><span class="ve-se-dc-v">' + (dcV != null ? dcV.toFixed(0) + 'V' : '--') + '</span>' +
            ' · <span class="ve-se-dc-a">' + (dcA != null ? dcA.toFixed(1) + 'A' : '--') + '</span>' +
            ' · <span class="ve-se-dc-w">' + (dcW != null ? formatW(dcW) : '--') + '</span></span></h2>'
        : '');

    return card;
}

function buildDCChannelCard(data) {
    var card = document.createElement('div');
    card.className = 've-card ve-ac-dc-card';
    var channels = data.dc_channels || [];
    var inv = data.inverter || {};

    // AC section
    var acV = inv.ac_voltage_an_v, acA = inv.ac_current_l1_a, acW = inv.ac_power_w;
    var acFreq = inv.ac_frequency_hz, eff = inv.efficiency_pct;
    card.innerHTML =
        '<h2 class="ve-card-title">AC Output</h2>' +
        '<table class="ve-phase-table"><thead><tr><th>Voltage</th><th>Current</th><th>Power</th><th>Frequency</th></tr></thead>' +
        '<tbody><tr>' +
        '<td class="ve-live-value ve-ac-voltage">' + (acV != null ? acV.toFixed(1) + ' V' : '--') + '</td>' +
        '<td class="ve-live-value ve-ac-current">' + (acA != null ? acA.toFixed(2) + ' A' : '--') + '</td>' +
        '<td class="ve-live-value ve-ac-power">' + (acW != null ? formatW(acW) : '--') + '</td>' +
        '<td class="ve-live-value ve-ac-freq">' + (acFreq != null ? acFreq.toFixed(1) + ' Hz' : '--') + '</td>' +
        '</tr></tbody></table>' +
        (eff != null ? '<div style="margin-top:6px" class="ve-text-dim ve-live-value ve-ac-eff">Efficiency: ' + eff.toFixed(1) + '%</div>' : '');

    // DC Strings section
    if (channels.length > 0) {
        var stringNames = data.string_names || [];
        var strings = data.strings || [];
        var dcRows = '';
        for (var i = 0; i < channels.length; i++) {
            var ch = channels[i];
            var gName = (strings[i] && strings[i].name) || stringNames[i] || '';
            dcRows += '<tr>' +
                '<td>' + esc(ch.name || 'String ' + (i + 1)) + '</td>' +
                '<td class="ve-live-value ve-dc' + i + '-voltage">' + (ch.voltage_v != null ? ch.voltage_v.toFixed(1) + ' V' : '--') + '</td>' +
                '<td class="ve-live-value ve-dc' + i + '-current">' + (ch.current_a != null ? ch.current_a.toFixed(2) + ' A' : '--') + '</td>' +
                '<td class="ve-live-value ve-dc' + i + '-power">' + (ch.power_w != null ? formatW(ch.power_w) : '--') + '</td>' +
                '<td class="ve-live-value ve-dc' + i + '-yield">' + (ch.yield_day_wh != null ? ch.yield_day_wh + ' Wh' : '--') + '</td>' +
                '<td class="ve-dc-panels">' + esc(_formatPanelGroups(strings[i])) + '</td>' +
                '<td><input type="text" class="ve-input ve-string-name" data-idx="' + i + '" value="' + esc(gName) + '" placeholder="—" maxlength="64"></td>' +
                '</tr>';
        }
        card.innerHTML +=
            '<h2 class="ve-card-title" style="margin-top:16px">DC Strings</h2>' +
            '<table class="ve-phase-table"><thead><tr><th>String</th><th>Voltage</th><th>Current</th><th>Power</th><th>Today</th><th>Panels</th><th>Gruppe</th></tr></thead>' +
            '<tbody>' + dcRows + '</tbody></table>' +
            '<div class="ve-string-group-foot">' +
            '  <span class="ve-string-group-hint">Strings mit gleichem Gruppennamen erscheinen als eigene Einheit unter „Standorte". Leer = zählt zum Gerät.</span>' +
            '  <span class="ve-btn-pair ve-string-save-pair" style="display:none">' +
            '    <button class="ve-btn ve-btn--sm ve-btn--cancel ve-string-cancel">Abbrechen</button>' +
            '    <button class="ve-btn ve-btn--sm ve-btn--save ve-string-save">Speichern</button>' +
            '  </span>' +
            '</div>';
        _wireStringGroups(card, data, channels.length);
    } else if (inv.dc_voltage_v || inv.dc_current_a || inv.dc_power_w) {
        // Fallback: single DC from inverter data (only if DC data exists)
        card.innerHTML +=
            '<h2 class="ve-card-title" style="margin-top:16px">DC Input</h2>' +
            '<table class="ve-phase-table"><thead><tr><th>Voltage</th><th>Current</th><th>Power</th></tr></thead>' +
            '<tbody><tr>' +
            '<td class="ve-live-value">' + (inv.dc_voltage_v != null ? inv.dc_voltage_v.toFixed(1) + ' V' : '--') + '</td>' +
            '<td class="ve-live-value">' + (inv.dc_current_a != null ? inv.dc_current_a.toFixed(2) + ' A' : '--') + '</td>' +
            '<td class="ve-live-value">' + (inv.dc_power_w != null ? formatW(inv.dc_power_w) : '--') + '</td>' +
            '</tr></tbody></table>';
    }

    return card;
}

// Wire the DC-string group-name inputs: dirty tracking + save (PUT
// string_names) so named strings show as sub-units under "Standorte".
function _wireStringGroups(card, data, channelCount) {
    var inputs = card.querySelectorAll('.ve-string-name');
    var savePair = card.querySelector('.ve-string-save-pair');
    if (!inputs.length || !savePair) return;
    var orig = [];
    inputs.forEach(function(inp) {
        orig[parseInt(inp.getAttribute('data-idx'), 10)] = inp.value;
    });
    function checkDirty() {
        var dirty = false;
        inputs.forEach(function(inp) {
            var idx = parseInt(inp.getAttribute('data-idx'), 10);
            if (inp.value !== (orig[idx] || '')) dirty = true;
        });
        savePair.style.display = dirty ? '' : 'none';
    }
    inputs.forEach(function(inp) { inp.addEventListener('input', checkDirty); });
    card.querySelector('.ve-string-cancel').addEventListener('click', function() {
        inputs.forEach(function(inp) {
            var idx = parseInt(inp.getAttribute('data-idx'), 10);
            inp.value = orig[idx] || '';
        });
        checkDirty();
    });
    card.querySelector('.ve-string-save').addEventListener('click', function() {
        var out = [];
        for (var k = 0; k < channelCount; k++) out.push('');
        inputs.forEach(function(inp) {
            var idx = parseInt(inp.getAttribute('data-idx'), 10);
            if (idx >= 0 && idx < channelCount) out[idx] = inp.value.trim();
        });
        var devId = data.device_id || _activeDeviceId;
        fetch('/api/devices/' + devId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ string_names: out })
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) { showToast('Speichern fehlgeschlagen: ' + d.error, 'error'); return; }
                showToast('String-Gruppen gespeichert', 'success');
                inputs.forEach(function(inp) {
                    var idx = parseInt(inp.getAttribute('data-idx'), 10);
                    orig[idx] = inp.value;
                });
                checkDirty();
            })
            .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
    });
}

// ===== Update Active Device Dashboard =====

function updateActiveDeviceDashboard(data) {
    if (!_activeDeviceContainer) return;
    var inv = data.inverter || {};
    var ratedW = _gaugeMaxW(data);
    var acPower = inv.ac_power_w || 0;
    var pct = Math.min(acPower / ratedW, 1.0);
    var arcLength = GAUGE_ARC_LENGTH;
    var offset = arcLength * (1 - pct);
    var gc = gaugeColor(pct);

    // Update gauge
    var gaugeFill = _activeDeviceContainer.querySelector('.ve-gauge-fill');
    if (gaugeFill) {
        gaugeFill.style.strokeDashoffset = offset;
        gaugeFill.style.stroke = gc;
    }
    var gaugeVal = _activeDeviceContainer.querySelector('.ve-gauge-value-text');
    if (gaugeVal) gaugeVal.textContent = formatW(acPower);

    // Update phase values (SolarEdge)
    function updatePhaseVal(cls, val) {
        var el = _activeDeviceContainer.querySelector('.' + cls);
        if (el && val != null) el.textContent = val;
    }
    if (inv.ac_voltage_an_v != null) {
        updatePhaseVal('ve-l1-voltage', inv.ac_voltage_an_v.toFixed(1) + ' V');
        updatePhaseVal('ve-l1-current', inv.ac_current_l1_a != null ? inv.ac_current_l1_a.toFixed(2) + ' A' : '-- A');
        var l1w = (inv.ac_voltage_an_v != null && inv.ac_current_l1_a != null) ? formatW(inv.ac_voltage_an_v * inv.ac_current_l1_a) : '--';
        updatePhaseVal('ve-l1-power', l1w);
    }
    if (inv.ac_voltage_bn_v != null) {
        updatePhaseVal('ve-l2-voltage', inv.ac_voltage_bn_v.toFixed(1) + ' V');
        updatePhaseVal('ve-l2-current', inv.ac_current_l2_a != null ? inv.ac_current_l2_a.toFixed(2) + ' A' : '-- A');
        var l2w = (inv.ac_voltage_bn_v != null && inv.ac_current_l2_a != null) ? formatW(inv.ac_voltage_bn_v * inv.ac_current_l2_a) : '--';
        updatePhaseVal('ve-l2-power', l2w);
    }
    if (inv.ac_voltage_cn_v != null) {
        updatePhaseVal('ve-l3-voltage', inv.ac_voltage_cn_v.toFixed(1) + ' V');
        updatePhaseVal('ve-l3-current', inv.ac_current_l3_a != null ? inv.ac_current_l3_a.toFixed(2) + ' A' : '-- A');
        var l3w = (inv.ac_voltage_cn_v != null && inv.ac_current_l3_a != null) ? formatW(inv.ac_voltage_cn_v * inv.ac_current_l3_a) : '--';
        updatePhaseVal('ve-l3-power', l3w);
    }

    // Update SolarEdge AC summary + DC input
    updatePhaseVal('ve-se-freq', inv.ac_frequency_hz != null ? inv.ac_frequency_hz.toFixed(1) + ' Hz' : null);
    updatePhaseVal('ve-se-eff', inv.efficiency_pct != null ? inv.efficiency_pct.toFixed(1) + '%' : null);
    updatePhaseVal('ve-se-temp', formatTemp(deviceTemp(inv)));
    updatePhaseVal('ve-se-total-a', inv.ac_current_a != null ? inv.ac_current_a.toFixed(1) + ' A' : null);
    updatePhaseVal('ve-se-dc-v', inv.dc_voltage_v != null ? inv.dc_voltage_v.toFixed(1) + ' V' : null);
    updatePhaseVal('ve-se-dc-a', inv.dc_current_a != null ? inv.dc_current_a.toFixed(2) + ' A' : null);
    updatePhaseVal('ve-se-dc-w', inv.dc_power_w != null ? formatW(inv.dc_power_w) : null);

    // Update performance values
    var energyEl = _activeDeviceContainer.querySelector('.ve-daily-energy');
    if (energyEl) energyEl.textContent = ((inv.daily_energy_wh || 0) / 1000).toFixed(1) + ' kWh';
    var peakEl = _activeDeviceContainer.querySelector('.ve-peak-power');
    if (peakEl && inv.peak_power_w != null) peakEl.textContent = formatW(inv.peak_power_w);
    var statusEl = _activeDeviceContainer.querySelector('.ve-inv-status');
    if (statusEl) statusEl.textContent = inv.status || '--';
    var tempEl = _activeDeviceContainer.querySelector('.ve-inv-temp');
    if (tempEl) tempEl.textContent = formatTemp(deviceTemp(inv));

    // Update AC/DC values for OpenDTU
    updatePhaseVal('ve-ac-voltage', inv.ac_voltage_an_v != null ? inv.ac_voltage_an_v.toFixed(1) + ' V' : null);
    updatePhaseVal('ve-ac-current', inv.ac_current_l1_a != null ? inv.ac_current_l1_a.toFixed(2) + ' A' : null);
    updatePhaseVal('ve-ac-power', inv.ac_power_w != null ? formatW(inv.ac_power_w) : null);
    updatePhaseVal('ve-ac-freq', inv.ac_frequency_hz != null ? inv.ac_frequency_hz.toFixed(1) + ' Hz' : null);
    updatePhaseVal('ve-ac-eff', inv.efficiency_pct != null ? 'Efficiency: ' + inv.efficiency_pct.toFixed(1) + '%' : null);
    var dcs = data.dc_channels || [];
    for (var di = 0; di < dcs.length; di++) {
        updatePhaseVal('ve-dc' + di + '-voltage', dcs[di].voltage_v != null ? dcs[di].voltage_v.toFixed(1) + ' V' : null);
        updatePhaseVal('ve-dc' + di + '-current', dcs[di].current_a != null ? dcs[di].current_a.toFixed(2) + ' A' : null);
        updatePhaseVal('ve-dc' + di + '-power', dcs[di].power_w != null ? formatW(dcs[di].power_w) : null);
        updatePhaseVal('ve-dc' + di + '-yield', dcs[di].yield_day_wh != null ? dcs[di].yield_day_wh + ' Wh' : null);
    }

    // SENEC connection-card status block (live)
    var snU = data.senec_status;
    if (snU) {
        updatePhaseVal('ve-senec-state', snU.state_name || ('Code ' + snU.state_code));
        updatePhaseVal('ve-senec-pv', (snU.pv_power_w || 0) + ' W');
        updatePhaseVal('ve-senec-house', (snU.house_power_w || 0) + ' W');
        var snUg = snU.grid_power_w || 0;
        updatePhaseVal('ve-senec-grid', snUg >= 0 ? (snUg + ' W Bezug') : ((-snUg) + ' W Einspeisung'));
        updatePhaseVal('ve-senec-bat',
            (snU.battery_temp_c == null && !snU.soc_pct && !snU.battery_power_w)
                ? 'offline'
                : (snU.soc_pct + ' % · ' + (snU.battery_power_w || 0) + ' W'));
        updatePhaseVal('ve-senec-temp', senecTempStr(snU));
    }

    // Update OpenDTU status from cached snapshot data (instant, no extra fetch)
    var dtuS = data.opendtu_status;
    if (dtuS) {
        var prodEl = _activeDeviceContainer.querySelector('.ve-opendtu-producing');
        var reachEl = _activeDeviceContainer.querySelector('.ve-opendtu-reachable');
        var limEl = _activeDeviceContainer.querySelector('.ve-opendtu-limit');
        if (prodEl) prodEl.textContent = dtuS.producing ? 'Yes' : 'No';
        if (reachEl) reachEl.textContent = dtuS.reachable ? 'Yes' : 'No';
        if (limEl) limEl.textContent = dtuS.limit_relative + '% (' + dtuS.limit_absolute + ' W)';
    }
}

