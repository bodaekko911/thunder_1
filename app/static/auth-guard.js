/**
 * auth-guard.js
 *
 * Shared auth utility included in every authenticated page.
 *
 * Behaviours:
 *
 *  1. PROACTIVE REFRESH — schedules a POST /auth/refresh at
 *     (ACCESS_TOKEN_EXPIRE_MINUTES - 2) minutes after page load so that a tab
 *     left open longer than the token lifetime stays alive without any user
 *     action.  The timer is cancelled on beforeunload.
 *
 *  2. FETCH WRAPPER — wraps window.fetch() to catch 401 responses that arrive
 *     while the page is already open.  On 401:
 *       a. Attempt one POST /auth/refresh.
 *       b. If refresh succeeds, retry the original request once and return its
 *          result.  (No infinite loop: the refresh call itself and any retry
 *          are not re-intercepted.)
 *       c. If refresh fails, or the retry also returns 401, redirect to login
 *          with ?reason=expired.
 *     Multiple concurrent 401s share one refresh attempt (deduplication via a
 *     shared promise that clears after settlement).
 *
 *  3. REDIRECT HELPER — _redirectToLogin(reason) encodes the current internal
 *     URL as ?next= and navigates to the login page.  Passing reason='expired'
 *     causes the login page to show a friendly "session expired" banner.
 *
 * NOT called by logout() — a deliberate logout should land on the plain login
 * page without pre-filling a return destination.
 *
 * Open-redirect safety: _redirectToLogin() only stores
 * location.pathname + location.search, both of which are always same-origin
 * values provided by the browser — never user-controlled.  The login page
 * validates the ?next= value before using it (_isSafeReturnUrl).
 */

// Must match ACCESS_TOKEN_EXPIRE_MINUTES - 2 in server config (30 - 2 = 28).
var PROACTIVE_REFRESH_MS = 28 * 60 * 1000;

function _redirectToLogin(reason) {
    var next = encodeURIComponent(location.pathname + location.search);
    var url = '/?next=' + next;
    if (reason) { url += '&reason=' + encodeURIComponent(reason); }
    window.location.href = url;
}

/** POST /auth/refresh once.  Returns a Promise<boolean> — true on success. */
function _refreshSession() {
    return window._origFetch('/auth/refresh', { method: 'POST', credentials: 'include' })
        .then(function (r) { return r.ok; })
        .catch(function () { return false; });
}

/* ── Fetch wrapper ─────────────────────────────────────────────────────── */
(function () {
    var _origFetch = window.fetch;
    // Expose on window so _refreshSession() can bypass the wrapper.
    window._origFetch = _origFetch;

    // Shared refresh promise — deduplicates concurrent 401 responses so only
    // one POST /auth/refresh fires even if several requests fail at once.
    var _pendingRefresh = null;

    window.fetch = function (url, opts) {
        var self = this;
        var args = arguments;

        // Identify the refresh endpoint so we never intercept it (avoids loops).
        var urlStr = (typeof url === 'string') ? url
                   : (url && typeof url.url === 'string') ? url.url : '';
        var isRefreshCall = urlStr.indexOf('/auth/refresh') !== -1;

        return _origFetch.apply(self, args).then(function (res) {
            if (res.status !== 401 || isRefreshCall) {
                return res;
            }

            // First 401: kick off a refresh (or reuse an in-flight one).
            if (!_pendingRefresh) {
                _pendingRefresh = _refreshSession().then(function (ok) {
                    _pendingRefresh = null;
                    return ok;
                }, function () {
                    _pendingRefresh = null;
                    return false;
                });
            }

            return _pendingRefresh.then(function (ok) {
                if (!ok) {
                    _redirectToLogin('expired');
                    // Return the original 401 response so any calling code can
                    // inspect it rather than hanging on an unresolved promise.
                    return res;
                }
                // Retry the original request exactly once with the new cookie.
                return _origFetch.apply(self, args).then(function (retryRes) {
                    if (retryRes.status === 401) {
                        _redirectToLogin('expired');
                    }
                    return retryRes;
                });
            });
        });
    };
}());

/* ── Proactive background refresh ─────────────────────────────────────── */
(function () {
    var _timer = setTimeout(function () {
        // Fire-and-forget: the server sets an updated access_token cookie.
        // If the call fails (e.g. refresh_token also expired) the next fetch
        // 401 will handle the redirect, so no special error handling needed.
        _refreshSession();
    }, PROACTIVE_REFRESH_MS);

    window.addEventListener('beforeunload', function () {
        clearTimeout(_timer);
    });
}());
