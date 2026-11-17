// ===== Register Viewer =====

var SUNSPEC_DECODE = {
    40071: { unit: 'A', sf_addr: 40075, label: 'Total AC current' },
    40072: { unit: 'A', sf_addr: 40075, label: 'Phase L1 current' },
    40073: { unit: 'A', sf_addr: 40075, label: 'Phase L2 current' },
    40074: { unit: 'A', sf_addr: 40075, label: 'Phase L3 current' },
    40075: { is_sf: true },
    40076: { unit: 'V', sf_addr: 40082, label: 'Line voltage L1-L2' },
    40077: { unit: 'V', sf_addr: 40082, label: 'Line voltage L2-L3' },
    40078: { unit: 'V', sf_addr: 40082, label: 'Line voltage L3-L1' },
    40079: { unit: 'V', sf_addr: 40082, label: 'Phase voltage L1-N' },
    40080: { unit: 'V', sf_addr: 40082, label: 'Phase voltage L2-N' },
    40081: { unit: 'V', sf_addr: 40082, label: 'Phase voltage L3-N' },
    40082: { is_sf: true },
    40083: { unit: 'W', sf_addr: 40084, label: 'AC power output', signed: true },
    40084: { is_sf: true },
    40085: { unit: 'Hz', sf_addr: 40086, label: 'Grid frequency' },
    40086: { is_sf: true },
    40087: { unit: 'VA', sf_addr: 40088, label: 'Apparent power', signed: true },
    40088: { is_sf: true },
    40089: { unit: 'var', sf_addr: 40090, label: 'Reactive power', signed: true },
    40090: { is_sf: true },
    40091: { unit: '%', sf_addr: 40092, label: 'Power factor', signed: true },
    40092: { is_sf: true },
    40093: { unit: 'Wh', sf_addr: 40095, label: 'Lifetime energy', size: 2 },
    40095: { is_sf: true },
    40096: { unit: 'A', sf_addr: 40097, label: 'DC current' },
    40097: { is_sf: true },
    40098: { unit: 'V', sf_addr: 40099, label: 'DC voltage' },
    40099: { is_sf: true },
    40100: { unit: 'W', sf_addr: 40101, label: 'DC power', signed: true },
    40101: { is_sf: true },
    40102: { unit: '\u00B0C', sf_addr: 40106, label: 'Cabinet temperature', signed: true },
    40103: { unit: '\u00B0C', sf_addr: 40106, label: 'Heatsink temperature', signed: true },
    40104: { unit: '\u00B0C', sf_addr: 40106, label: 'Transformer temperature', signed: true },
    40105: { unit: '\u00B0C', sf_addr: 40106, label: 'Other temperature', signed: true },
    40106: { is_sf: true },
    40107: { enum: { 1: 'Off', 2: 'Sleeping', 3: 'Starting', 4: 'Producing (MPPT)', 5: 'Throttled', 6: 'Shutting down', 7: 'Fault', 8: 'Standby' }, label: 'Operating state' },
    40108: { label: 'Vendor-specific status code' },
    40123: { enum: { 4: 'PV', 82: 'Storage', 83: 'PV+Storage' }, label: 'DER type' },
    40124: { unit: 'W', sf_addr: 40125, label: 'Max power rating' },
    40125: { is_sf: true },
    40126: { unit: 'VA', sf_addr: 40127, label: 'Max apparent power' },
    40127: { is_sf: true },
    40133: { unit: 'A', sf_addr: 40134, label: 'Max current rating' },
    40134: { is_sf: true },
    40153: { enum: { 0: 'Disconnect', 1: 'Connect' }, label: 'Connection control' },
    40154: { unit: '%', sf_fixed: -2, label: 'Power limit setpoint' },
    40158: { enum: { 0: 'Disabled', 1: 'Enabled' }, label: 'Power limit enable' }
};

var sfCache = {};

function decodeRegisterValue(addr, rawValue) {
    var meta = SUNSPEC_DECODE[addr];
    if (!meta || rawValue === null || rawValue === undefined) return '';
    if (meta.is_sf) return 'Scale Factor';
    if (meta.enum) {
        var label = meta.enum[rawValue];
        return label ? label : 'Unknown (' + rawValue + ')';
    }
    if (meta.unit) {
        if (rawValue === 32768 || rawValue === 32767 || rawValue === 65535) return 'N/A';
        var sf = 0;
        if (meta.sf_fixed !== undefined) {
            sf = meta.sf_fixed;
        } else if (meta.sf_addr) {
            sf = sfCache[meta.sf_addr];
            if (sf === undefined || sf === null) return rawValue + ' ' + meta.unit + ' (raw)';
            if (sf > 32767) sf = sf - 65536;
        }
        var numValue = (typeof rawValue === 'string') ? parseInt(rawValue.replace(/,/g, ''), 10) : rawValue;
        if (isNaN(numValue)) return '';
        var decoded = numValue * Math.pow(10, sf);
        var decimals = sf < 0 ? Math.abs(sf) : 0;
        if (Math.abs(decoded) >= 1000000) return (decoded / 1000000).toFixed(1) + ' M' + meta.unit;
        if (Math.abs(decoded) >= 10000) return (decoded / 1000).toFixed(1) + ' k' + meta.unit;
        return decoded.toFixed(decimals) + ' ' + meta.unit;
    }
    return '';
}

function buildSfCache(models) {
    models.forEach(function(model) {
        model.fields.forEach(function(field) {
            var meta = SUNSPEC_DECODE[field.addr];
            if (meta && meta.is_sf && field.fronius_value !== null) {
                sfCache[field.addr] = field.fronius_value;
            }
        });
    });
}

function buildRegisterViewer(container, models) {
    buildSfCache(models);
    models.forEach(function(model) {
        var group = document.createElement('div');
        group.className = 've-model-group';

        var header = document.createElement('div');
        header.className = 've-model-header';
        header.innerHTML = '<span>' + model.name + '</span><span>\u25BC</span>';
        header.addEventListener('click', function() {
            var fields = group.querySelector('.ve-model-fields');
            fields.classList.toggle('collapsed');
            header.querySelector('span:last-child').textContent = fields.classList.contains('collapsed') ? '\u25B6' : '\u25BC';
        });
        group.appendChild(header);

        var fields = document.createElement('div');
        fields.className = 've-model-fields';

        var headerRow = document.createElement('div');
        headerRow.className = 've-reg-header';
        var sourceLabel = 'Device Source';
        var targetLabel = 'Proxy Output';
        if (_activeDeviceType === 'solaredge') { sourceLabel = 'SE30K Source'; targetLabel = 'Fronius Target'; }
        else if (_activeDeviceType === 'opendtu') { sourceLabel = 'OpenDTU Source'; targetLabel = 'Proxy Output'; }
        else if (_activeDeviceType === 'hoymiles') { sourceLabel = 'Hoymiles HMS Source'; targetLabel = 'Proxy Output'; }
        else if (_activeDeviceType === 'shelly') { sourceLabel = 'Shelly Source'; targetLabel = 'Proxy Output'; }
        headerRow.innerHTML = '<span>Addr</span><span>Name</span><span class="ve-reg-se-value">' + sourceLabel + '</span><span class="ve-reg-fronius-value">' + targetLabel + '</span><span class="ve-reg-decoded">Decoded</span>';
        fields.appendChild(headerRow);

        model.fields.forEach(function(field) {
            var row = document.createElement('div');
            row.className = 've-reg-row';
            row.id = 'reg-' + field.addr;

            var seVal = formatValue(field.se_value);
            var frVal = formatValue(field.fronius_value);
            var seClass = field.se_value === null ? 've-reg-se-value null-value' : 've-reg-se-value';
            var decoded = decodeRegisterValue(field.addr, field.fronius_value);
            var meta = SUNSPEC_DECODE[field.addr];
            var tooltip = meta && meta.label ? ' title="' + meta.label + '"' : '';

            var isEmpty = (field.se_value === null || field.se_value === 0) &&
                          (field.fronius_value === null || field.fronius_value === 0) &&
                          (!decoded || decoded === '0 W' || decoded === '0 A' || decoded === '0 VA' ||
                           decoded === '0 var' || decoded === '0 %' || decoded === '0.00 A' ||
                           decoded === '0.0 W' || decoded === '0 V' || decoded === '' ||
                           decoded === 'N/A' || decoded === '0.00 %');
            if (isEmpty) row.classList.add('ve-empty');

            row.innerHTML =
                '<span class="ve-reg-addr">' + field.addr + '</span>' +
                '<span class="ve-reg-name"' + tooltip + '>' + field.name + '</span>' +
                '<span class="' + seClass + '" id="se-val-' + field.addr + '">' + seVal + '</span>' +
                '<span class="ve-reg-fronius-value" id="fr-val-' + field.addr + '">' + frVal + '</span>' +
                '<span class="ve-reg-decoded" id="dec-val-' + field.addr + '">' + decoded + '</span>';
            fields.appendChild(row);
            previousRegValues[field.addr] = { se: field.se_value, fr: field.fronius_value };
        });

        group.appendChild(fields);
        container.appendChild(group);
    });
}

function updateRegisterValues(models) {
    buildSfCache(models);
    models.forEach(function(model) {
        model.fields.forEach(function(field) {
            var seEl = document.getElementById('se-val-' + field.addr);
            var frEl = document.getElementById('fr-val-' + field.addr);
            var decEl = document.getElementById('dec-val-' + field.addr);
            var changed = false;

            if (seEl) {
                var newSeVal = formatValue(field.se_value);
                if (seEl.textContent !== newSeVal) { seEl.textContent = newSeVal; changed = true; }
                seEl.className = field.se_value === null ? 've-reg-se-value null-value' : 've-reg-se-value';
            }
            if (frEl) {
                var newFrVal = formatValue(field.fronius_value);
                if (frEl.textContent !== newFrVal) { frEl.textContent = newFrVal; changed = true; }
            }
            if (decEl) {
                var newDec = decodeRegisterValue(field.addr, field.fronius_value);
                if (decEl.textContent !== newDec) decEl.textContent = newDec;
            }
            if (changed) {
                var row = document.getElementById('reg-' + field.addr);
                if (row) { row.classList.remove('ve-changed'); void row.offsetWidth; row.classList.add('ve-changed'); }
            }
            previousRegValues[field.addr] = { se: field.se_value, fr: field.fronius_value };
        });
    });
}

function formatValue(val) {
    if (val === null || val === undefined) return '--';
    if (typeof val === 'string') return val;
    return val.toString();
}

// ===== Venus OS dbus write =====

function formatW(watts) {
    if (watts == null) return '--';
    if (Math.abs(watts) >= 1000) {
        var kw = watts / 1000;
        return (kw % 1 === 0 ? kw.toFixed(0) : kw.toFixed(1)) + ' kW';
    }
    return Math.round(watts) + ' W';
}

// Best available device temperature. Synthetic plugins (SENEC, OpenDTU,
// Hoymiles, Shelly, Sungrow) leave the SunSpec heat-sink register at 0
// and only fill the cabinet register — so fall back to it.
function deviceTemp(inv) {
    if (inv.temperature_sink_c != null && inv.temperature_sink_c !== 0) {
        return inv.temperature_sink_c;
    }
    return inv.temperature_c;
}

function formatTemp(t) {
    return t != null ? t.toFixed(1) + ' °C' : '--';
}

// SENEC temperature summary: case + MCU always, battery only when the
// storage subsystem is online (it reports the -126 °C sentinel offline).
function senecTempStr(s) {
    if (!s) return '…';
    var parts = [];
    if (s.case_temp_c != null) parts.push('Gehäuse ' + formatTemp(s.case_temp_c));
    if (s.mcu_temp_c != null) parts.push('MCU ' + formatTemp(s.mcu_temp_c));
    if (s.battery_temp_c != null && s.battery_temp_c > -100) {
        parts.push('Batterie ' + formatTemp(s.battery_temp_c));
    }
    return parts.length ? parts.join(' · ') : '--';
}

async function writeVenusDbus(path, value) {
    try {
        var res = await fetch('/api/venus-dbus', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path, value: value })
        });
        var data = await res.json();
        if (!data.success) showToast(data.error || 'Write failed', 'error');
    } catch (e) {
        showToast('Request failed: ' + e.message, 'error');
    }
}

