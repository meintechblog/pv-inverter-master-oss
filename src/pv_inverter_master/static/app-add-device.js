// ===== Add Device Flow =====

// Add device button is now inline in the INVERTERS sidebar group header

function showAddDeviceModal(presetLocationId) {
    var modal = document.createElement('div');
    modal.className = 've-add-modal';
    modal.innerHTML =
        '<div class="ve-add-modal-content">' +
        '  <div class="ve-add-modal-title">Add Device</div>' +
        '  <div class="ve-add-type-picker">' +
        '    <div class="ve-add-type-card" data-type="solaredge">SolarEdge Inverter</div>' +
        '    <div class="ve-add-type-card" data-type="opendtu">OpenDTU Inverter</div>' +
        '    <div class="ve-add-type-card" data-type="hoymiles">Hoymiles HMS (WLAN)</div>' +
        '    <div class="ve-add-type-card" data-type="shelly">Shelly Device</div>' +
        '    <div class="ve-add-type-card" data-type="sungrow">Sungrow Inverter</div>' +
        '    <div class="ve-add-type-card" data-type="senec">SENEC.Home</div>' +
        '  </div>' +
        '  <div class="ve-add-form-area"></div>' +
        '  <div class="ve-add-modal-actions">' +
        '    <button class="ve-btn ve-add-cancel">Cancel</button>' +
        '  </div>' +
        '</div>';
    document.body.appendChild(modal);

    var selectedType = null;
    var formArea = modal.querySelector('.ve-add-form-area');
    var actions = modal.querySelector('.ve-add-modal-actions');

    // Type picker
    modal.querySelectorAll('.ve-add-type-card').forEach(function(card) {
        card.addEventListener('click', function() {
            modal.querySelectorAll('.ve-add-type-card').forEach(function(c) { c.classList.remove('ve-add-type-card--selected'); });
            card.classList.add('ve-add-type-card--selected');
            selectedType = card.getAttribute('data-type');
            showAddForm(formArea, actions, selectedType, modal, presetLocationId);
        });
    });

    // Cancel
    modal.querySelector('.ve-add-cancel').addEventListener('click', function() {
        modal.remove();
    });

    // Close on overlay click
    modal.addEventListener('click', function(e) {
        if (e.target === modal) modal.remove();
    });
}

function showAddForm(formArea, actions, type, modal, presetLocationId) {
    formArea.innerHTML = '';

    // Remove old add/discover buttons
    actions.querySelectorAll('.ve-add-submit, .ve-add-discover').forEach(function(b) { b.remove(); });

    if (type === 'solaredge') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="e.g. SE30K"></div>' +
            '<div class="ve-form-group"><label>Host IP</label><input type="text" class="ve-input ve-add-host" placeholder="198.51.100.100"></div>' +
            '<div class="ve-form-group"><label>Port</label><input type="number" class="ve-input ve-add-port" value="1502" min="1" max="65535"></div>' +
            '<div class="ve-form-group"><label>Unit ID</label><input type="number" class="ve-input ve-add-unit" value="1" min="1" max="247"></div>';
    } else if (type === 'opendtu') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="e.g. HM-800"></div>' +
            '<div class="ve-form-group"><label>Gateway Host</label><input type="text" class="ve-input ve-add-host" placeholder="198.51.100.100"></div>' +
            '<div class="ve-form-group"><label>Username</label><input type="text" class="ve-input ve-add-gw-user" placeholder="admin (default)"></div>' +
            '<div class="ve-form-group"><label>Password</label><input type="password" class="ve-input ve-add-gw-pass" placeholder="openDTU42 (default)"></div>' +
            '<div class="ve-hint-card ve-add-auth-hint" style="display:none"></div>';
    } else if (type === 'shelly') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="e.g. Shelly Plus 1PM"></div>' +
            '<div class="ve-form-group"><label>Host IP</label><input type="text" class="ve-input ve-add-host" placeholder="198.51.100.50"></div>' +
            '<div class="ve-form-group"><label>Rated Power (W)</label><input type="number" class="ve-input ve-add-rated-power" value="" min="0" placeholder="0 (optional)"></div>' +
            '<div class="ve-hint-card ve-add-probe-hint" style="display:none"></div>';
    } else if (type === 'sungrow') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="e.g. Sungrow SG-RT"></div>' +
            '<div class="ve-form-group"><label>Host IP</label><input type="text" class="ve-input ve-add-host" placeholder="203.0.113.151"></div>' +
            '<div class="ve-form-group"><label>Port</label><input type="number" class="ve-input ve-add-port" value="502" min="1" max="65535"></div>' +
            '<div class="ve-form-group"><label>Unit ID</label><input type="number" class="ve-input ve-add-unit" value="1" min="1" max="247"></div>' +
            '<div class="ve-form-group"><label>Rated Power (W)</label><input type="number" class="ve-input ve-add-rated-power" value="8000" min="0"></div>' +
            '<div class="ve-hint-card ve-add-probe-hint" style="display:none"></div>';
    } else if (type === 'hoymiles') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="e.g. HMS-800W-2T"></div>' +
            '<div class="ve-form-group"><label>Host IP</label><input type="text" class="ve-input ve-add-host" placeholder="198.51.100.128"></div>' +
            '<div class="ve-form-group"><label>Rated Power (W)</label><input type="number" class="ve-input ve-add-rated-power" value="800" min="0"></div>' +
            '<div class="ve-form-group"><label>Panel-Wp (optional)</label><input type="number" class="ve-input ve-add-panel-wp" value="" min="0" placeholder="z.B. 880"></div>' +
            '<div class="ve-hint-card ve-add-probe-hint" style="display:none"></div>';
    } else if (type === 'senec') {
        formArea.innerHTML =
            '<div class="ve-form-group"><label>Name (optional)</label><input type="text" class="ve-input ve-add-name" placeholder="z.B. SENEC remote-site"></div>' +
            '<div class="ve-form-group"><label>Host IP</label><input type="text" class="ve-input ve-add-host" placeholder="203.0.113.101"></div>' +
            '<div class="ve-form-group"><label>Wechselrichter-Nennleistung (W, optional)</label><input type="number" class="ve-input ve-add-rated-power" value="" min="0" placeholder="z.B. 10000"></div>' +
            '<div class="ve-hint-card ve-add-probe-hint" style="display:none"></div>';
    }

    // Standort assignment (all device types). Pre-selected when the flow was
    // opened from a Standort's sidebar "+"; the field stays editable and
    // "— Kein Standort —" remains a valid choice.
    formArea.innerHTML += _locationFieldHtml(presetLocationId || '');

    // Remote-subnet override — available for every device type, so a
    // VPN-reachable Standort network can be discovered, not just the LAN.
    formArea.innerHTML +=
        '<div class="ve-form-group ve-add-subnet-row">' +
        '  <label>Subnet (für Discover-Scan)</label>' +
        '  <input type="text" class="ve-input ve-add-subnet"' +
        '    placeholder="z.B. 192.0.2.0/24">' +
        '  <span class="ve-form-hint" style="font-size:0.75rem;color:var(--ve-text-dim);margin-top:4px;display:block">' +
        '    Wird beim Standort-Wechsel gesetzt: dessen Subnetz, sonst aus einem ' +
        '    dort vorhandenen Gerät abgeleitet, sonst lokales Subnetz. ' +
        '    Formate: 198.51.100.0/24 · 198.51.100.x · 198.51.100.101' +
        '  </span>' +
        '</div>';

    // Discover area
    formArea.innerHTML += '<div class="ve-add-scan-area" style="display:none"><div class="ve-scan-progress" style="display:none"><div class="ve-scan-bar"><div class="ve-scan-bar-fill ve-add-scan-fill"></div></div><span class="ve-scan-status ve-add-scan-status"></span></div><div class="ve-add-scan-results"></div></div>';

    // Move the discover area to the TOP of the form, right above the fields
    // it fills — so a discovered pick and the filled fields stay together.
    var scanAreaNode = formArea.querySelector('.ve-add-scan-area');
    if (scanAreaNode) formArea.insertBefore(scanAreaNode, formArea.firstChild);

    // Prefill the subnet field AFTER the last innerHTML rebuild — an earlier
    // call would set a node that the rebuild then discards.
    _prefillLocalSubnet(formArea);

    // Add buttons
    var discoverBtn = document.createElement('button');
    discoverBtn.className = 've-btn ve-btn--primary ve-add-discover';
    discoverBtn.textContent = 'Discover';
    actions.insertBefore(discoverBtn, actions.firstChild);

    var addBtn = document.createElement('button');
    addBtn.className = 've-btn ve-btn--save ve-add-submit';
    addBtn.textContent = 'Add';
    actions.insertBefore(addBtn, actions.querySelector('.ve-add-cancel'));

    // Wire the Standort picker (incl. inline "+ Neuer Standort…")
    _wireLocationField(formArea);

    // Pre-selected Standort: point the Discover scan at its network too
    // (mirrors what a manual Standort change does via _wireLocationField).
    if (presetLocationId) _syncDiscoverSubnet(formArea, presetLocationId);

    // Add button handler
    addBtn.addEventListener('click', function() {
        // Shelly: when discovered relay channels are ticked, add one device
        // entry per channel — each can live on its own host, so this path
        // skips the single-host form entirely.
        if (type === 'shelly') {
            var shellyChecks = formArea.querySelectorAll('.ve-scan-check:checked');
            if (shellyChecks.length > 0) {
                _addShellyCheckedChannels(formArea, modal, addBtn, shellyChecks);
                return;
            }
        }

        var host = formArea.querySelector('.ve-add-host');
        var port = formArea.querySelector('.ve-add-port');
        var unit = formArea.querySelector('.ve-add-unit');
        var name = formArea.querySelector('.ve-add-name');

        if (!host || !host.value.trim()) {
            showToast('Host is required', 'error');
            return;
        }

        var payload = {
            host: host.value.trim(),
            type: type
        };
        payload.location_id = _getLocationFieldValue(formArea);
        if (name && name.value.trim()) payload.name = name.value.trim();
        if (port) payload.port = parseInt(port.value) || 1502;
        if (unit) payload.unit_id = parseInt(unit.value) || 1;

        // OpenDTU: include gateway credentials
        if (type === 'opendtu') {
            var gwUser = formArea.querySelector('.ve-add-gw-user');
            var gwPass = formArea.querySelector('.ve-add-gw-pass');
            payload.gateway_host = host.value.trim();
            if (gwUser && gwUser.value.trim()) payload.gateway_user = gwUser.value.trim();
            if (gwPass && gwPass.value.trim()) payload.gateway_password = gwPass.value.trim();
        }

        function _doAdd() {
            fetch('/api/devices', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.error) {
                    showToast('Add failed: ' + data.error, 'error');
                    return;
                }
                showToast('Device added', 'success');
                modal.remove();
                if (data.id) navigateTo(data.id, 'dashboard');
            })
            .catch(function(e) { showToast('Add failed: ' + e.message, 'error'); });
        }

        // Add several OpenDTU inverters of one gateway in a single action —
        // one device entry per inverter, sequentially to avoid config races.
        function _doAddDtuInverters(list) {
            addBtn.disabled = true;
            addBtn.textContent = 'Hinzufügen…';
            var added = 0;
            var firstId = null;
            function _next(idx) {
                if (idx >= list.length) {
                    addBtn.disabled = false;
                    addBtn.textContent = 'Add';
                    showToast(added + ' von ' + list.length + ' Invertern hinzugefügt',
                        added ? 'success' : 'error');
                    if (added) {
                        modal.remove();
                        if (firstId) navigateTo(firstId, 'dashboard');
                    }
                    return;
                }
                var it = list[idx];
                var p = {
                    host: payload.host,
                    type: 'opendtu',
                    gateway_host: payload.gateway_host,
                    location_id: payload.location_id,
                    serial: it.serial,
                    name: it.name || it.serial
                };
                if (payload.gateway_user) p.gateway_user = payload.gateway_user;
                if (payload.gateway_password) p.gateway_password = payload.gateway_password;
                fetch('/api/devices', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(p)
                })
                    .then(function(r) { return r.json(); })
                    .then(function(d) { if (d && d.id) { added++; if (!firstId) firstId = d.id; } })
                    .catch(function() {})
                    .finally(function() { _next(idx + 1); });
            }
            _next(0);
        }

        // OpenDTU: auto-test credentials before adding
        if (type === 'opendtu') {
            var authHint = formArea.querySelector('.ve-add-auth-hint');
            addBtn.disabled = true;
            addBtn.textContent = 'Testing...';
            fetch('/api/opendtu/test-auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    host: payload.gateway_host || payload.host,
                    user: payload.gateway_user || 'admin',
                    password: payload.gateway_password || 'openDTU42'
                })
            }).then(function(r) { return r.json(); }).then(function(result) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (!result.success) {
                    if (authHint) { authHint.style.display = 'block'; authHint.className = 've-hint-card ve-add-auth-hint'; authHint.innerHTML = '<div class="ve-hint-header">' + esc(result.error) + '</div><p class="ve-hint-subtext">Please enter valid username and password.</p>'; }
                    return;
                }
                var dtuInvs = result.inverters || [];
                var checks = formArea.querySelectorAll('.ve-add-dtu-check');
                if (dtuInvs.length <= 1) {
                    // 0 or 1 inverter — adopt its serial automatically.
                    if (dtuInvs.length === 1) payload.serial = dtuInvs[0].serial;
                    if (authHint) { authHint.style.display = 'block'; authHint.className = 've-hint-card ve-hint-card--success ve-add-auth-hint'; authHint.innerHTML = '<div class="ve-hint-header">Connected — ' + dtuInvs.length + ' inverter(s) found</div>'; }
                    _doAdd();
                } else if (checks.length) {
                    // Multi-inverter gateway, the picker is shown — add every
                    // ticked inverter as its own device.
                    var chosen = [];
                    checks.forEach(function(cb) {
                        if (cb.checked) {
                            chosen.push({
                                serial: cb.getAttribute('data-serial'),
                                name: cb.getAttribute('data-name')
                            });
                        }
                    });
                    if (!chosen.length) {
                        showToast('Mindestens einen Inverter auswählen', 'error');
                        return;
                    }
                    _doAddDtuInverters(chosen);
                } else if (authHint) {
                    // Multi-inverter gateway — show a checklist (all ticked)
                    // so the user can adopt several inverters at once.
                    authHint.style.display = 'block';
                    authHint.className = 've-hint-card ve-hint-card--success ve-add-auth-hint';
                    var rows = dtuInvs.map(function(it) {
                        return '<label class="ve-add-dtu-row">' +
                            '<input type="checkbox" class="ve-add-dtu-check" checked' +
                            ' data-serial="' + esc(it.serial) + '"' +
                            ' data-name="' + esc(it.name || it.serial) + '">' +
                            '<span>' + esc(it.name || it.serial) + ' (' + esc(it.serial) + ')</span>' +
                            '</label>';
                    }).join('');
                    authHint.innerHTML = '<div class="ve-hint-header">' + dtuInvs.length +
                        ' Inverter am Gateway — auswählen, dann erneut „Add"</div>' +
                        '<div class="ve-add-dtu-list">' + rows + '</div>';
                }
            }).catch(function(e) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (authHint) { authHint.style.display = 'block'; authHint.className = 've-hint-card ve-add-auth-hint'; authHint.innerHTML = '<div class="ve-hint-header">Connection failed: ' + esc(e.message) + '</div>'; }
            });
        } else if (type === 'shelly') {
            var probeHint = formArea.querySelector('.ve-add-probe-hint');
            var ratedPower = formArea.querySelector('.ve-add-rated-power');
            addBtn.disabled = true;
            addBtn.textContent = 'Probing...';
            fetch('/api/shelly/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ host: host.value.trim() })
            }).then(function(r) { return r.json(); }).then(function(result) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (result.success) {
                    if (probeHint) {
                        probeHint.style.display = 'block';
                        probeHint.className = 've-hint-card ve-hint-card--success ve-add-probe-hint';
                        var probeTitle = 'Detected ' + esc(result.gen_display) + ' — ' + esc(result.model);
                        if (result.switch_name) probeTitle += ' (' + esc(result.switch_name) + ')';
                        probeHint.innerHTML = '<div class="ve-hint-header">' + probeTitle + '</div>';
                    }
                    payload.shelly_gen = result.generation;
                    if (ratedPower && ratedPower.value) payload.rated_power = parseInt(ratedPower.value) || 0;
                    _doAdd();
                } else {
                    if (probeHint) {
                        probeHint.style.display = 'block';
                        probeHint.className = 've-hint-card ve-add-probe-hint';
                        probeHint.innerHTML = '<div class="ve-hint-header">Could not reach device</div><p class="ve-hint-subtext">' + esc(result.error) + '</p>';
                    }
                }
            }).catch(function(e) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (probeHint) {
                    probeHint.style.display = 'block';
                    probeHint.className = 've-hint-card ve-add-probe-hint';
                    probeHint.innerHTML = '<div class="ve-hint-header">Connection failed: ' + esc(e.message) + '</div>';
                }
            });
        } else if (type === 'hoymiles') {
            var hmProbeHint = formArea.querySelector('.ve-add-probe-hint');
            var hmRated = formArea.querySelector('.ve-add-rated-power');
            var hmPanelWp = formArea.querySelector('.ve-add-panel-wp');
            if (hmRated && hmRated.value) payload.rated_power = parseInt(hmRated.value) || 0;
            if (hmPanelWp && hmPanelWp.value) payload.panel_power_wp = parseInt(hmPanelWp.value) || 0;
            addBtn.disabled = true;
            addBtn.textContent = 'Probing...';
            fetch('/api/hoymiles/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ host: host.value.trim() })
            }).then(function(r) { return r.json(); }).then(function(result) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (result.success) {
                    if (hmProbeHint) {
                        hmProbeHint.style.display = 'block';
                        hmProbeHint.className = 've-hint-card ve-hint-card--success ve-add-probe-hint';
                        var hdr = 'Detected Hoymiles HMS';
                        if (result.serial) hdr += ' — Serial ' + esc(result.serial);
                        if (result.panels) hdr += ' · ' + result.panels + ' Panel(s)';
                        if (result.firmware) hdr += ' · FW ' + esc(result.firmware);
                        hmProbeHint.innerHTML = '<div class="ve-hint-header">' + hdr + '</div>';
                    }
                    if (result.serial) payload.serial = result.serial;
                    _doAdd();
                } else if (result.sleeping) {
                    // Save anyway — inverter will be polled when it wakes up.
                    if (hmProbeHint) {
                        hmProbeHint.style.display = 'block';
                        hmProbeHint.className = 've-hint-card ve-add-probe-hint';
                        hmProbeHint.innerHTML = '<div class="ve-hint-header">Inverter sleeping or unreachable</div><p class="ve-hint-subtext">' + esc(result.error || '') + ' Entry will be saved and polled automatically when the inverter wakes up.</p>';
                    }
                    _doAdd();
                } else {
                    if (hmProbeHint) {
                        hmProbeHint.style.display = 'block';
                        hmProbeHint.className = 've-hint-card ve-add-probe-hint';
                        hmProbeHint.innerHTML = '<div class="ve-hint-header">Could not reach inverter</div><p class="ve-hint-subtext">' + esc(result.error || 'unknown error') + '</p>';
                    }
                }
            }).catch(function(e) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (hmProbeHint) {
                    hmProbeHint.style.display = 'block';
                    hmProbeHint.className = 've-hint-card ve-add-probe-hint';
                    hmProbeHint.innerHTML = '<div class="ve-hint-header">Connection failed: ' + esc(e.message) + '</div>';
                }
            });
        } else if (type === 'sungrow') {
            var sgProbeHint = formArea.querySelector('.ve-add-probe-hint');
            var sgRatedPower = formArea.querySelector('.ve-add-rated-power');
            addBtn.disabled = true;
            addBtn.textContent = 'Probing...';
            payload.port = parseInt(port.value) || 502;
            payload.unit_id = parseInt(unit.value) || 1;
            if (sgRatedPower && sgRatedPower.value) payload.rated_power = parseInt(sgRatedPower.value) || 0;
            fetch('/api/sungrow/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ host: host.value.trim(), port: payload.port, unit_id: payload.unit_id })
            }).then(function(r) { return r.json(); }).then(function(result) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (result.success) {
                    if (sgProbeHint) {
                        sgProbeHint.style.display = 'block';
                        sgProbeHint.className = 've-hint-card ve-hint-card--success ve-add-probe-hint';
                        sgProbeHint.innerHTML = '<div class="ve-hint-header">Connected — ' + esc(result.model) + '</div>';
                    }
                    payload.manufacturer = result.manufacturer;
                    payload.model = result.model;
                    _doAdd();
                } else {
                    if (sgProbeHint) {
                        sgProbeHint.style.display = 'block';
                        sgProbeHint.className = 've-hint-card ve-add-probe-hint';
                        sgProbeHint.innerHTML = '<div class="ve-hint-header">Could not reach inverter</div><p class="ve-hint-subtext">' + esc(result.error) + '</p>';
                    }
                }
            }).catch(function(e) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (sgProbeHint) {
                    sgProbeHint.style.display = 'block';
                    sgProbeHint.className = 've-hint-card ve-add-probe-hint';
                    sgProbeHint.innerHTML = '<div class="ve-hint-header">Connection failed: ' + esc(e.message) + '</div>';
                }
            });
        } else if (type === 'senec') {
            var snProbeHint = formArea.querySelector('.ve-add-probe-hint');
            var snRated = formArea.querySelector('.ve-add-rated-power');
            if (snRated && snRated.value) payload.rated_power = parseInt(snRated.value) || 0;
            addBtn.disabled = true;
            addBtn.textContent = 'Probing...';
            fetch('/api/senec/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ host: host.value.trim() })
            }).then(function(r) { return r.json(); }).then(function(result) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (result.success) {
                    if (snProbeHint) {
                        snProbeHint.style.display = 'block';
                        snProbeHint.className = 've-hint-card ve-hint-card--success ve-add-probe-hint';
                        var snHdr = 'SENEC erkannt';
                        if (result.serial) snHdr += ' — ' + esc(result.serial);
                        snHdr += ' · PV ' + (result.pv_power_w || 0) + ' W';
                        snProbeHint.innerHTML = '<div class="ve-hint-header">' + snHdr + '</div>';
                    }
                    if (result.serial) payload.serial = result.serial;
                    _doAdd();
                } else {
                    if (snProbeHint) {
                        snProbeHint.style.display = 'block';
                        snProbeHint.className = 've-hint-card ve-add-probe-hint';
                        snProbeHint.innerHTML = '<div class="ve-hint-header">SENEC nicht erreichbar</div><p class="ve-hint-subtext">' + esc(result.error || '') + '</p>';
                    }
                }
            }).catch(function(e) {
                addBtn.disabled = false;
                addBtn.textContent = 'Add';
                if (snProbeHint) {
                    snProbeHint.style.display = 'block';
                    snProbeHint.className = 've-hint-card ve-add-probe-hint';
                    snProbeHint.innerHTML = '<div class="ve-hint-header">Verbindung fehlgeschlagen: ' + esc(e.message) + '</div>';
                }
            });
        } else {
            _doAdd();
        }
    });

    // Discover button handler
    discoverBtn.addEventListener('click', function() {
        if (type === 'shelly') {
            triggerShellyDiscover(formArea);
        } else if (type === 'hoymiles') {
            triggerHoymilesDiscover(formArea);
        } else if (type === 'senec') {
            triggerSenecDiscover(formArea);
        } else {
            triggerAddModalScan(formArea);
        }
    });
}

function triggerAddModalScan(formArea) {
    var scanArea = formArea.querySelector('.ve-add-scan-area');
    var progress = formArea.querySelector('.ve-scan-progress');
    var fill = formArea.querySelector('.ve-add-scan-fill');
    var status = formArea.querySelector('.ve-add-scan-status');
    var results = formArea.querySelector('.ve-add-scan-results');

    _scanRunning = true;
    scanArea.style.display = '';
    formArea.scrollTop = 0;
    progress.style.display = '';
    fill.style.width = '0%';
    status.textContent = 'Starting scan...';
    results.innerHTML = '';

    var subnetInput = formArea.querySelector('.ve-add-subnet');
    var subnetVal = subnetInput && subnetInput.value.trim();
    var body = { ports: [502, 1502] };
    if (subnetVal) {
        body.subnet = subnetVal;
        status.textContent = 'Scanning ' + subnetVal + '...';
    }

    fetch('/api/scanner/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(res) {
        if (res.status === 409) {
            showToast('Scan already running', 'warning');
            return null;
        }
        return res.json();
    })
    .then(function(data) {
        if (data && data.error) {
            showToast(data.error, 'error');
        }
    })
    .catch(function(e) {
        showToast('Scan failed: ' + e.message, 'error');
    });
}

// Discovery keeps already-configured devices in the results (greyed via
// .ve-scan-result--configured) instead of hiding them, so the user can
// see the full picture. Returns the row class suffix and a badge.
function _addedRowClass(alreadyAdded) {
    return alreadyAdded ? ' ve-scan-result--configured' : '';
}
function _addedBadge(alreadyAdded) {
    return alreadyAdded
        ? '<span class="ve-scan-added-badge">Bereits hinzugefügt</span>'
        : '';
}

// Briefly halo the form fields just filled from a discovery result and
// scroll the host field into view — so it is obvious the click took
// effect even when the field sits above the results list.
function _flashFilledFields(root) {
    var scope = root || document;
    scope.querySelectorAll('.ve-add-host, .ve-add-port, .ve-add-unit, .ve-add-name')
        .forEach(function(f) {
            if (!f.value) return;
            f.classList.remove('ve-input--flash');
            void f.offsetWidth;            // restart the animation
            f.classList.add('ve-input--flash');
            setTimeout(function() { f.classList.remove('ve-input--flash'); }, 1100);
        });
    var host = scope.querySelector('.ve-add-host');
    if (host && host.scrollIntoView) {
        host.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
}

// Wire discovery result rows for click-to-fill — one consistent gesture
// across every device type: click a row, the form fills, the row marks
// itself selected. Data travels on the row's data-host/-name/-port/-unit.
function _wireScanResultRows(formArea, results) {
    results.querySelectorAll('.ve-scan-result').forEach(function(row) {
        row.addEventListener('click', function() {
            results.querySelectorAll('.ve-scan-result').forEach(function(r) {
                r.classList.remove('ve-scan-result--selected');
            });
            row.classList.add('ve-scan-result--selected');
            var map = { '.ve-add-host': 'data-host', '.ve-add-port': 'data-port',
                        '.ve-add-unit': 'data-unit' };
            Object.keys(map).forEach(function(sel) {
                var input = formArea.querySelector(sel);
                var val = row.getAttribute(map[sel]);
                if (input && val) input.value = val;
            });
            var nameInput = formArea.querySelector('.ve-add-name');
            var nameVal = row.getAttribute('data-name');
            if (nameInput && nameVal && !nameInput.value) nameInput.value = nameVal;
            _flashFilledFields(formArea);
        });
    });
}

// Add one Shelly device entry per ticked relay channel. Each checkbox
// carries its own host + channel, so a multi-relay device — or several
// devices at once — each become individual entries.
function _addShellyCheckedChannels(formArea, modal, addBtn, checks) {
    var chosen = [];
    checks.forEach(function(cb) {
        chosen.push({
            host: cb.getAttribute('data-host'),
            channel: parseInt(cb.getAttribute('data-channel'), 10) || 0,
            name: cb.getAttribute('data-name') || '',
            gen: cb.getAttribute('data-gen') || ''
        });
    });
    var ratedEl = formArea.querySelector('.ve-add-rated-power');
    var rated = ratedEl && ratedEl.value ? (parseInt(ratedEl.value, 10) || 0) : 0;
    var locId = _getLocationFieldValue(formArea);

    addBtn.disabled = true;
    addBtn.textContent = 'Hinzufügen…';
    var added = 0;
    var firstId = null;

    function next(idx) {
        if (idx >= chosen.length) {
            addBtn.disabled = false;
            addBtn.textContent = 'Add';
            showToast(added + ' von ' + chosen.length + ' Kanälen hinzugefügt',
                added ? 'success' : 'error');
            if (added) {
                modal.remove();
                if (firstId) navigateTo(firstId, 'dashboard');
            }
            return;
        }
        var it = chosen[idx];
        var p = {
            host: it.host,
            type: 'shelly',
            shelly_channel: it.channel,
            shelly_gen: it.gen,
            name: it.name,
            location_id: locId
        };
        if (rated) p.rated_power = rated;
        fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(p)
        })
            .then(function(r) { return r.json(); })
            .then(function(d) { if (d && d.id) { added++; if (!firstId) firstId = d.id; } })
            .catch(function() {})
            .finally(function() { next(idx + 1); });
    }
    next(0);
}

function triggerShellyDiscover(formArea) {
    var scanArea = formArea.querySelector('.ve-add-scan-area');
    var progress = formArea.querySelector('.ve-scan-progress');
    var fill = formArea.querySelector('.ve-add-scan-fill');
    var status = formArea.querySelector('.ve-add-scan-status');
    var results = formArea.querySelector('.ve-add-scan-results');

    var subnetInput = formArea.querySelector('.ve-add-subnet');
    var subnetVal = subnetInput && subnetInput.value.trim();

    scanArea.style.display = '';
    formArea.scrollTop = 0;
    progress.style.display = '';
    fill.style.width = '50%';
    // mDNS is link-local only — with a subnet given the backend IP-sweeps
    // it instead, so a VPN-reachable Standort network can be scanned.
    status.textContent = subnetVal
        ? 'Scanne ' + subnetVal + ' nach Shelly-Geräten...'
        : 'Suche Shelly-Geräte via mDNS...';
    results.innerHTML = '';

    var body = {};
    if (subnetVal) body.subnet = subnetVal;

    fetch('/api/shelly/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        progress.style.display = 'none';
        if (!data.success) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">' + esc(data.error) + '</div></div>';
            return;
        }
        if (data.devices.length === 0) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Keine Shelly-Geräte gefunden</div><p class="ve-hint-subtext">In anderem Subnetz? Oben das Subnetz angeben — dann wird per IP-Scan gesucht (auch Gen1 ohne mDNS).</p></div>';
            return;
        }
        // One checkbox per relay channel: a multi-relay Shelly yields one
        // row per channel, each addable on its own. Tick the channels you
        // want, then "Add" — each ticked channel becomes its own entry.
        var html = '<div class="ve-scan-results-hint">' + data.devices.length +
            ' Gerät(e) gefunden — Kanäle anhaken, dann „Add":</div>';
        data.devices.forEach(function(dev) {
            var genLabel = dev.generation === 'gen2' ? 'Gen2' : (dev.generation === 'gen3' ? 'Gen3' : 'Gen1');
            var deviceName = dev.switch_name || dev.name || dev.model || 'Shelly';
            var channels = (dev.channels && dev.channels.length)
                ? dev.channels
                : [{ id: 0, name: null, power_w: 0, output: false, already_added: dev.already_added }];
            var multi = channels.length > 1;
            channels.forEach(function(ch) {
                var chName = ch.name || ('Kanal ' + ch.id);
                var label = multi
                    ? (esc(deviceName) + ' · ' + esc(chName))
                    : esc(deviceName);
                var entryName = multi ? (ch.name || (deviceName + ' CH' + ch.id)) : deviceName;
                var dotColor = ch.output ? 'var(--ve-green)' : 'var(--ve-text-dim)';
                html += '<label class="ve-scan-result' + _addedRowClass(ch.already_added) + '">' +
                    '<input type="checkbox" class="ve-scan-check"' +
                        ' data-host="' + esc(dev.host) + '"' +
                        ' data-channel="' + ch.id + '"' +
                        ' data-name="' + esc(entryName) + '"' +
                        ' data-gen="' + esc(dev.generation) + '">' +
                    '<span class="ve-scan-result-identity">' +
                        '<span class="ve-scan-channel-dot" style="background:' + dotColor + '"></span>' +
                        label + _addedBadge(ch.already_added) +
                    '</span>' +
                    '<span class="ve-scan-result-host">' + esc(dev.host) + '</span>' +
                    '<span class="ve-scan-result-unit">' + formatW(ch.power_w) + ' · ' + genLabel + '</span>' +
                    '</label>';
            });
        });
        results.innerHTML = html;
        // Native <label> toggles the checkbox; mirror its state onto the row.
        results.querySelectorAll('.ve-scan-check').forEach(function(cb) {
            cb.addEventListener('change', function() {
                var row = cb.closest('.ve-scan-result');
                if (row) row.classList.toggle('ve-scan-result--selected', cb.checked);
            });
        });
    })
    .catch(function(e) {
        progress.style.display = 'none';
        results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Discovery failed: ' + esc(e.message) + '</div></div>';
    });
}

// Cached detected local subnet so switching device type in the Add modal
// doesn't re-hit the API each time.
var _localSubnetCache = null;

function _prefillLocalSubnet(formArea) {
    var field = formArea.querySelector('.ve-add-subnet');
    if (!field) return;
    if (_localSubnetCache) {
        if (!field.value) field.value = _localSubnetCache;
        return;
    }
    fetch('/api/scanner/config')
        .then(function(r) { return r.json(); })
        .then(function(c) {
            if (c && c.local_subnet) {
                _localSubnetCache = c.local_subnet;
                if (!field.value) field.value = c.local_subnet;
            }
        })
        .catch(function() {});
}

function triggerHoymilesDiscover(formArea) {
    var scanArea = formArea.querySelector('.ve-add-scan-area');
    var progress = formArea.querySelector('.ve-scan-progress');
    var fill = formArea.querySelector('.ve-add-scan-fill');
    var status = formArea.querySelector('.ve-add-scan-status');
    var results = formArea.querySelector('.ve-add-scan-results');

    var subnetInput = formArea.querySelector('.ve-add-subnet');
    var subnetVal = subnetInput && subnetInput.value.trim();

    scanArea.style.display = '';
    formArea.scrollTop = 0;
    progress.style.display = '';
    fill.style.width = '50%';
    status.textContent = 'Scanne ' + (subnetVal || 'lokales Subnetz') + ' nach Hoymiles HMS (Port 10081)...';
    results.innerHTML = '';

    var body = {};
    if (subnetVal) body.subnet = subnetVal;

    fetch('/api/hoymiles/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        progress.style.display = 'none';
        if (!data.success) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">' + esc(data.error) + '</div></div>';
            return;
        }
        if (!data.devices || data.devices.length === 0) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Keine Hoymiles HMS gefunden</div><p class="ve-hint-subtext">In anderem Subnetz? Oben das Subnetz angeben — z.B. 198.51.100.0/24.</p></div>';
            return;
        }
        var html = '<div class="ve-scan-results-hint">' + data.devices.length +
            ' gefunden — zum Übernehmen anklicken:</div>';
        data.devices.forEach(function(dev) {
            var stateTxt = dev.sleeping ? 'schläft' : (dev.producing ? dev.ac_power_w + ' W' : 'bereit (0 W)');
            var idTxt = dev.serial ? esc(dev.serial) : 'HMS';
            html += '<div class="ve-scan-result' + _addedRowClass(dev.already_added) + '"' +
                ' data-host="' + esc(dev.host) + '">' +
                '<span class="ve-scan-result-identity">' + idTxt + (dev.panels ? ' <span style="color:var(--ve-text-dim);font-size:0.8rem">(' + dev.panels + ' Panel)</span>' : '') + _addedBadge(dev.already_added) + '</span>' +
                '<span class="ve-scan-result-host">' + esc(dev.host) + '</span>' +
                '<span class="ve-scan-result-unit">' + stateTxt + '</span>' +
                '</div>';
        });
        results.innerHTML = html;
        _wireScanResultRows(formArea, results);
    })
    .catch(function(e) {
        progress.style.display = 'none';
        results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Discovery fehlgeschlagen: ' + esc(e.message) + '</div></div>';
    });
}

function triggerSenecDiscover(formArea) {
    var scanArea = formArea.querySelector('.ve-add-scan-area');
    var progress = formArea.querySelector('.ve-scan-progress');
    var fill = formArea.querySelector('.ve-add-scan-fill');
    var status = formArea.querySelector('.ve-add-scan-status');
    var results = formArea.querySelector('.ve-add-scan-results');

    var subnetInput = formArea.querySelector('.ve-add-subnet');
    var subnetVal = subnetInput && subnetInput.value.trim();

    scanArea.style.display = '';
    formArea.scrollTop = 0;
    progress.style.display = '';
    fill.style.width = '50%';
    status.textContent = 'Scanne ' + (subnetVal || 'lokales Subnetz') + ' nach SENEC-Anlagen (Port 443)...';
    results.innerHTML = '';

    var body = {};
    if (subnetVal) body.subnet = subnetVal;

    fetch('/api/senec/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        progress.style.display = 'none';
        if (!data.success) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">' + esc(data.error) + '</div></div>';
            return;
        }
        if (!data.devices || data.devices.length === 0) {
            results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Keine SENEC-Anlage gefunden</div><p class="ve-hint-subtext">In anderem Subnetz? Oben das Subnetz angeben — z.B. 203.0.113.0/24.</p></div>';
            return;
        }
        var html = '<div class="ve-scan-results-hint">' + data.devices.length +
            ' gefunden — zum Übernehmen anklicken:</div>';
        data.devices.forEach(function(dev) {
            var stateTxt = dev.producing ? dev.pv_power_w + ' W' : 'bereit (0 W)';
            var idTxt = dev.serial ? esc(dev.serial) : 'SENEC';
            html += '<div class="ve-scan-result' + _addedRowClass(dev.already_added) + '"' +
                ' data-host="' + esc(dev.host) + '">' +
                '<span class="ve-scan-result-identity">' + idTxt + _addedBadge(dev.already_added) + '</span>' +
                '<span class="ve-scan-result-host">' + esc(dev.host) + '</span>' +
                '<span class="ve-scan-result-unit">' + stateTxt + '</span>' +
                '</div>';
        });
        results.innerHTML = html;
        _wireScanResultRows(formArea, results);
    })
    .catch(function(e) {
        progress.style.display = 'none';
        results.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Discovery fehlgeschlagen: ' + esc(e.message) + '</div></div>';
    });
}

// ===== Delete with Undo Toast =====

function deleteDeviceWithUndo(deviceId, deviceName) {
    // Cache device data for undo
    var cachedDevice = null;
    for (var i = 0; i < _devices.length; i++) {
        if (_devices[i].id === deviceId) { cachedDevice = JSON.parse(JSON.stringify(_devices[i])); break; }
    }

    fetch('/api/devices/' + deviceId, { method: 'DELETE' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Delete failed: ' + data.error, 'error');
                return;
            }
            // Navigate away
            navigateTo('virtual', 'dashboard');

            // Show undo toast
            showToast('Device geloescht', 'warning', 'Rueckgaengig', function() {
                // Undo: re-add device
                if (cachedDevice) {
                    var payload = {
                        host: cachedDevice.host,
                        port: cachedDevice.port,
                        unit_id: cachedDevice.unit_id,
                        name: cachedDevice.name,
                        type: cachedDevice.type,
                        enabled: cachedDevice.enabled
                    };
                    fetch('/api/devices', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    })
                    .then(function(res) { return res.json(); })
                    .then(function(d) {
                        if (d.id) {
                            showToast('Device wiederhergestellt', 'success');
                            navigateTo(d.id, 'dashboard');
                        }
                    })
                    .catch(function() { showToast('Undo failed', 'error'); });
                }
            }, 5000);
        })
        .catch(function(e) { showToast('Delete failed: ' + e.message, 'error'); });
}

