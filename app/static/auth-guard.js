/**
 * auth-guard.js
 *
 * Shared auth-redirect utility included in every authenticated page.
 *
 * _redirectToLogin()  – saves the current internal URL as ?next= and
 *                       sends the browser to the login page.  Called on:
 *                         • missing/expired logged_in cookie (page load)
 *                         • 401 from /auth/me during initUser()
 *                         • 401 from any mid-page API call (via fetch wrapper)
 *
 * Open-redirect safety: _redirectToLogin() only stores
 * location.pathname + location.search, both of which are always
 * same-origin values provided by the browser – never user-controlled.
 * The login page validates the ?next= value before using it.
 *
 * NOT called by logout() – a deliberate logout should land on the plain
 * login page without pre-filling a return destination.
 */

function _redirectToLogin() {
    var next = encodeURIComponent(location.pathname + location.search);
    window.location.href = '/?next=' + next;
}

/* Intercept fetch() to catch 401s that happen while the page is already
   open (token expired mid-session, e.g. left a tab open for > 30 min).
   Any 401 from an authenticated endpoint means the access token is gone –
   redirect the user to login so they can come back to where they were. */
(function () {
    var _origFetch = window.fetch;
    window.fetch = function (url, opts) {
        return _origFetch.apply(this, arguments).then(function (res) {
            if (res.status === 401) {
                _redirectToLogin();
            }
            return res;
        });
    };
}());
