/* Aquarius Road Department -- account linking (Tier B, PROTOCOL.md SS6.2) AND
 * the admin dashboard's Discord login, sharing this one page because it's the
 * single redirect URI actually registered with Discord's OAuth app.
 *
 * Two phases on one page/URL, distinguished by query params:
 *   1. Fresh visit: show the link-code input, redirect to Discord on submit.
 *      The pending linkCode rides through Discord's OAuth `state` param -- no
 *      server-side session needed for this page at all. (The admin dashboard
 *      instead sends state="admin:<nonce>" before redirecting here -- see
 *      website/admin/app.js -- so this page never shows the link-code form
 *      for that flow.)
 *   2. Discord's redirect back (?code=&state=): resolve state and either
 *      complete the Tier B link, or (state starts with "admin:") complete an
 *      admin dashboard login -- the nonce after "admin:" is checked against
 *      sessionStorage, set by admin/app.js in the SAME tab right before the
 *      Discord redirect, as a login-CSRF guard tying this callback to the tab
 *      that actually initiated it.
 */
(function () {
  "use strict";

  var qs = new URLSearchParams(location.search);
  var $ = function (id) { return document.getElementById(id); };

  function show(id) {
    ["step-code", "step-working", "step-done", "step-admin-done", "step-error"].forEach(function (s) {
      $(s).classList.toggle("hidden", s !== id);
    });
  }

  function showError(msg) {
    $("error-text").textContent = msg;
    show("step-error");
  }

  // Attached unconditionally, before the phase branching below (which returns
  // early on several paths) -- this needs to be live whichever phase we land in.
  $("copy-btn").addEventListener("click", function () {
    var text = $("token-box").textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(text);
  });

  var discordCode = qs.get("code");
  var discordError = qs.get("error");
  var state = qs.get("state") || qs.get("linkCode") || "";

  if (discordError) {
    showError("Discord sign-in didn't complete (" + discordError + "). You can try again.");
    return;
  }

  if (discordCode && state.indexOf("admin:") === 0) {
    // Admin dashboard login completion.
    show("step-working");
    var nonce = state.slice("admin:".length);
    var expected = sessionStorage.getItem("ard_admin_nonce");
    sessionStorage.removeItem("ard_admin_nonce");
    if (!expected || nonce !== expected) {
      showError("This sign-in link didn't originate from this browser tab. Start over from the dashboard.");
      return;
    }
    fetch("/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ discordCode: discordCode }),
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) { showError(res.body.error || "Sign-in failed."); return; }
        // Straight on to the dashboard rather than lingering on this page --
        // this page's plain styling predates the dashboard's Mission-Control
        // look and is only ever meant to be a momentary OAuth-callback hop,
        // not somewhere a signed-in admin actually looks at.
        show("step-admin-done");
        location.replace("/admin/");
      })
      .catch(function () { showError("Couldn't reach the server. Try again in a moment."); });
    return;
  }

  var linkCode = state;
  if (discordCode) {
    // Phase 2: Discord sent us back with an authorization code.
    show("step-working");
    if (!linkCode) {
      showError("Missing link code -- the Discord redirect didn't carry it through. Start over.");
      return;
    }
    fetch("/link/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ linkCode: linkCode, discordCode: discordCode }),
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) { showError(res.body.error || "Linking failed."); return; }
        $("token-box").textContent = res.body.token;
        show("step-done");
      })
      .catch(function () { showError("Couldn't reach the server. Try again in a moment."); });
    return;
  }

  // Phase 1: show the code-entry form.
  show("step-code");
  var input = $("code-input");
  var btn = $("continue-btn");
  if (linkCode) input.value = linkCode;
  function refreshBtn() { btn.disabled = input.value.trim().length === 0; }
  input.addEventListener("input", refreshBtn);
  refreshBtn();

  btn.addEventListener("click", function () {
    var code = input.value.trim();
    if (!code) return;
    btn.disabled = true;
    fetch("/link/config")
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        if (!cfg.configured) {
          $("step-code-error").textContent = "Discord login isn't set up on this server yet.";
          btn.disabled = false;
          return;
        }
        var url = cfg.authorizeUrl
          + "?client_id=" + encodeURIComponent(cfg.clientId)
          + "&redirect_uri=" + encodeURIComponent(cfg.redirectUri)
          + "&response_type=code"
          + "&scope=" + encodeURIComponent(cfg.scope)
          + "&state=" + encodeURIComponent(code);
        location.href = url;
      })
      .catch(function () {
        $("step-code-error").textContent = "Couldn't reach the server. Try again in a moment.";
        btn.disabled = false;
      });
  });
})();
