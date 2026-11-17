// ===== Toast Notifications =====

var toastContainer = null;
var MAX_TOASTS = 4;

function getToastContainer() {
    if (!toastContainer) toastContainer = document.getElementById('toast-container');
    return toastContainer;
}

function showToast(message, type, actionLabel, actionCallback, duration) {
    var container = getToastContainer();
    if (!container) return;

    // Duplicate suppression
    var existing = container.querySelectorAll('.ve-toast:not(.ve-toast--exiting)');
    for (var i = 0; i < existing.length; i++) {
        if (existing[i]._message === message) return;
    }

    // Enforce max
    while (container.querySelectorAll('.ve-toast:not(.ve-toast--exiting)').length >= MAX_TOASTS) {
        var toasts = container.querySelectorAll('.ve-toast:not(.ve-toast--exiting)');
        var oldest = null;
        for (var k = toasts.length - 1; k >= 0; k--) {
            if (!toasts[k].classList.contains('ve-toast--error')) { oldest = toasts[k]; break; }
        }
        if (!oldest) oldest = toasts[toasts.length - 1];
        dismissToast(oldest);
    }

    if (!duration) duration = (type === 'error') ? 8000 : (type === 'warning') ? 5000 : 3000;

    var toast = document.createElement('div');
    toast.className = 've-toast ve-toast--' + (type || 'info');
    toast._message = message;

    if (actionLabel && actionCallback) {
        toast.innerHTML = '<span>' + message + '</span> <button style="margin-left:12px;padding:2px 10px;background:rgba(255,255,255,0.2);border:1px solid rgba(255,255,255,0.3);border-radius:4px;color:inherit;cursor:pointer;font-weight:600;font-size:0.85em">' + actionLabel + '</button>';
        var actionBtn = toast.querySelector('button');
        actionBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            clearTimeout(timer);
            dismissToast(toast);
            actionCallback();
        });
    } else {
        toast.textContent = message;
    }

    toast.setAttribute('role', 'alert');
    container.prepend(toast);

    var timer = setTimeout(function() { dismissToast(toast); }, duration);
    toast.addEventListener('click', function() {
        clearTimeout(timer);
        dismissToast(toast);
    });
}

function dismissToast(toast) {
    if (!toast || toast.classList.contains('ve-toast--exiting')) return;
    toast.classList.add('ve-toast--exiting');
    toast.addEventListener('animationend', function() { toast.remove(); }, { once: true });
}

