/* Aquarius Road Department -- admin dashboard.
 *
 * Two ways in, both ending at the same server-scoped panels:
 *   1. Login with Discord -- redirects through /link.html (the one redirect
 *      URI actually registered with Discord's OAuth app; see link.js's
 *      "admin:" state branch), lands back here with a session cookie set by
 *      POST /admin/login. This is the normal path for a moderator or admin
 *      whose Discord identity already has a trust.py discord_grants scope.
 *   2. Paste the Owner secret directly ("quick access" on the sign-in panel).
 *      No session, no cookie -- the token is kept in a page-memory variable
 *      only and sent as a plain Authorization header, same as any bearer-
 *      token API caller. This is the bootstrap/recovery path: it's how the
 *      very first admin grant ever gets minted (see the "Owner actions"
 *      panel), and it works even if no Discord admin grant exists yet.
 *
 * Neither path stores a long-lived secret anywhere persistent in the
 * browser -- the session cookie is HttpOnly/short-TTL/server-revocable, and
 * quickOwnerToken lives only in this tab's JS heap until reload.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var quickOwnerToken = null;
  var currentGrants = [];   // [{server, scope}]
  var allServers = [];      // from /health, used in owner-quick mode

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers ? Object.assign({}, opts.headers) : {};
    if (opts.json !== undefined) headers["Content-Type"] = "application/json";
    if (quickOwnerToken && !headers["Authorization"]) headers["Authorization"] = quickOwnerToken;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      credentials: "same-origin",
      body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined,
    }).then(function (r) {
      return r.text().then(function (t) {
        var body = {};
        try { body = t ? JSON.parse(t) : {}; } catch (e) { /* non-JSON error page */ }
        return { ok: r.ok, status: r.status, body: body };
      });
    });
  }

  function toast(msg, isError) {
    var t = $("toast");
    t.textContent = msg;
    t.className = isError ? "toast-error" : "toast-ok";
    clearTimeout(toast._h);
    toast._h = setTimeout(function () { t.classList.add("hidden"); }, 4000);
  }

  // ---- gate / login ----
  function showGate(errMsg) {
    $("gate").classList.remove("hidden");
    $("dash").classList.add("hidden");
    $("admin-who").classList.add("hidden");
    if (errMsg) $("gate-error").textContent = errMsg;
  }

  $("login-btn").addEventListener("click", function () {
    fetch("/link/config").then(function (r) { return r.json(); }).then(function (cfg) {
      if (!cfg.configured) {
        $("gate-error").textContent = "Discord login isn't set up on this server yet.";
        return;
      }
      var nonce = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()) + Date.now());
      sessionStorage.setItem("ard_admin_nonce", nonce);
      var url = cfg.authorizeUrl
        + "?client_id=" + encodeURIComponent(cfg.clientId)
        + "&redirect_uri=" + encodeURIComponent(cfg.redirectUri)
        + "&response_type=code"
        + "&scope=" + encodeURIComponent(cfg.scope)
        + "&state=" + encodeURIComponent("admin:" + nonce);
      location.href = url;
    }).catch(function () {
      $("gate-error").textContent = "Couldn't reach the server. Try again in a moment.";
    });
  });

  var ownerQuickInput = document.createElement("input");
  ownerQuickInput.type = "password";
  ownerQuickInput.placeholder = "Owner token (bootstrap / recovery access)";
  ownerQuickInput.id = "owner-quick-input";
  ownerQuickInput.autocomplete = "off";
  var ownerQuickBtn = document.createElement("button");
  ownerQuickBtn.id = "owner-quick-btn";
  ownerQuickBtn.className = "btn go wide";
  ownerQuickBtn.style.marginTop = ".5rem";
  ownerQuickBtn.textContent = "Continue with Owner token";
  var ownerQuickWrap = document.createElement("details");
  ownerQuickWrap.innerHTML = "<summary>Use the Owner token instead</summary>";
  ownerQuickWrap.appendChild(ownerQuickInput);
  ownerQuickWrap.appendChild(ownerQuickBtn);
  $("gate-card").appendChild(ownerQuickWrap);

  ownerQuickBtn.addEventListener("click", function () {
    var tok = ownerQuickInput.value.trim();
    if (!tok) return;
    quickOwnerToken = tok;
    api("/health").then(function (res) {
      if (!res.ok) { showGate("Couldn't verify -- try again."); quickOwnerToken = null; return; }
      allServers = res.body.servers || [];
      var grants = allServers.map(function (s) { return { server: s, scope: "admin" }; });
      $("admin-discord-id").textContent = "owner (token)";
      enterDash(grants);
    });
  });

  $("logout-btn").addEventListener("click", function () {
    quickOwnerToken = null;
    api("/admin/logout", { method: "POST" }).then(function () { showGate(); });
  });

  // ---- dashboard shell ----
  function enterDash(grants) {
    currentGrants = grants;
    $("gate").classList.add("hidden");
    $("dash").classList.remove("hidden");
    $("admin-who").classList.remove("hidden");
    var servers = Array.from(new Set(grants.map(function (g) { return g.server; })));
    var sel = $("server-tabs");
    sel.innerHTML = "";
    servers.forEach(function (s) {
      var opt = document.createElement("option");
      opt.value = s; opt.textContent = s;
      sel.appendChild(opt);
    });
    sel.onchange = renderForServer;
    if (servers.length) renderForServer();
  }

  function scopesFor(server) {
    return new Set(currentGrants.filter(function (g) { return g.server === server; })
      .map(function (g) { return g.scope; }));
  }

  function renderForServer() {
    var server = $("server-tabs").value;
    var scopes = scopesFor(server);
    var isAdmin = scopes.has("admin");
    var isMod = scopes.has("moderator") || isAdmin;
    $("server-scope-badge").textContent = Array.from(scopes).join(", ") || "no scope";
    $("panel-moderation").classList.toggle("hidden", !isMod);
    $("panel-identity").classList.toggle("hidden", !isMod);
    $("panel-registry").classList.toggle("hidden", !isAdmin);
    $("panel-grants").classList.toggle("hidden", !isAdmin);
    if (isMod) loadModeration(server);
    if (isAdmin) { loadRegistry(server); loadGrants(server); }
  }

  // ---- moderation queue ----
  function itemRow(icon, name, sub, tag) {
    var row = document.createElement("div");
    row.className = "a-item";
    row.innerHTML = "<span class=\"a-ic\">" + icon + "</span>"
      + "<div class=\"a-bd\"><div class=\"n\"></div><div class=\"c\"></div></div>"
      + (tag ? "<span class=\"a-tag " + tag + "\">" + tag + "</span>" : "");
    row.querySelector(".n").textContent = name;
    row.querySelector(".c").textContent = sub;
    return row;
  }

  function loadModeration(server) {
    api("/moderation/" + encodeURIComponent(server)).then(function (res) {
      var el = $("mod-list");
      if (!res.ok) { el.textContent = res.body.error || "couldn't load"; return; }
      var pending = res.body.pending || [];
      $("mod-count").textContent = pending.length;
      el.innerHTML = "";
      if (!pending.length) { el.innerHTML = "<div class=\"a-empty\">Queue is empty.</div>"; return; }
      pending.forEach(function (m) {
        var row = itemRow("🚧", m.payload.cond, "#" + m.id + " road " + m.payload.road
          + " seg " + m.payload.seg + " y=" + (m.observedY == null ? "-" : m.observedY), m.kind);
        var actions = document.createElement("div");
        actions.className = "a-actions";
        var approve = document.createElement("button");
        approve.className = "btn go small";
        approve.textContent = "Approve";
        approve.onclick = function () { resolveModeration(server, m.id, "approve"); };
        var reject = document.createElement("button");
        reject.className = "btn danger small";
        reject.textContent = "Reject";
        reject.onclick = function () { resolveModeration(server, m.id, "reject"); };
        actions.appendChild(approve); actions.appendChild(reject);
        row.appendChild(actions);
        el.appendChild(row);
      });
    });
  }

  function resolveModeration(server, id, action) {
    api("/moderation/" + id + "/" + action, { method: "POST" }).then(function (res) {
      toast(res.ok ? "Resolved" : (res.body.error || "failed"), !res.ok);
      if (res.ok) loadModeration(server);
    });
  }

  // ---- registry tokens ----
  function loadRegistry(server) {
    api("/registry").then(function (res) {
      var el = $("registry-list");
      if (!res.ok) { el.textContent = res.body.error || "couldn't load"; return; }
      var active = (res.body.active || []).filter(function (t) { return t.server === server; });
      $("reg-count").textContent = active.length;
      el.innerHTML = "";
      if (!active.length) { el.innerHTML = "<div class=\"a-empty\">No active tokens on this server.</div>"; return; }
      active.forEach(function (t) {
        var row = itemRow("🔑", t.holderLabel, t.tokenId, t.scope);
        var actions = document.createElement("div");
        actions.className = "a-actions";
        var revoke = document.createElement("button");
        revoke.className = "btn danger small";
        revoke.textContent = "Revoke";
        revoke.onclick = function () {
          api("/registry/" + t.tokenId, { method: "DELETE" }).then(function (r) {
            toast(r.ok ? "Revoked" : (r.body.error || "failed"), !r.ok);
            if (r.ok) loadRegistry(server);
          });
        };
        actions.appendChild(revoke);
        row.appendChild(actions);
        el.appendChild(row);
      });
    });
  }

  $("reg-issue-btn").addEventListener("click", function () {
    var server = $("server-tabs").value;
    var holderLabel = $("reg-label").value.trim();
    var scope = $("reg-scope").value;
    if (!holderLabel) return;
    api("/registry", { method: "POST", json: { holderLabel: holderLabel, scope: scope, server: server } })
      .then(function (res) {
        if (!res.ok) { toast(res.body.error || "failed", true); return; }
        $("reg-new-token").classList.remove("hidden");
        $("reg-new-token-box").textContent = res.body.token;
        $("reg-label").value = "";
        loadRegistry(server);
      });
  });

  // ---- discord dashboard grants ----
  function loadGrants(server) {
    api("/admin/grants?server=" + encodeURIComponent(server)).then(function (res) {
      var el = $("grants-list");
      if (!res.ok) { el.textContent = res.body.error || "couldn't load"; return; }
      var grants = res.body.grants || [];
      $("grants-count").textContent = grants.length;
      el.innerHTML = "";
      if (!grants.length) { el.innerHTML = "<div class=\"a-empty\">No dashboard grants on this server.</div>"; return; }
      grants.forEach(function (g) {
        var row = itemRow("🛂", g.discordId, "granted by " + g.grantedBy, g.scope);
        if (g.scope !== "admin") {
          var actions = document.createElement("div");
          actions.className = "a-actions";
          var revoke = document.createElement("button");
          revoke.className = "btn danger small";
          revoke.textContent = "Revoke";
          revoke.onclick = function () {
            api("/admin/grants/" + g.grantId, { method: "DELETE" }).then(function (r) {
              toast(r.ok ? "Revoked" : (r.body.error || "failed"), !r.ok);
              if (r.ok) loadGrants(server);
            });
          };
          actions.appendChild(revoke);
          row.appendChild(actions);
        } else {
          var note = document.createElement("span");
          note.className = "a-dim";
          note.style.marginLeft = "auto";
          note.style.fontSize = ".68rem";
          note.textContent = "needs Owner token to revoke";
          row.appendChild(note);
        }
        el.appendChild(row);
      });
    });
  }

  $("grant-issue-btn").addEventListener("click", function () {
    var server = $("server-tabs").value;
    var discordId = $("grant-discord-id").value.trim();
    if (!discordId) return;
    api("/admin/grants", { method: "POST", json: { discordId: discordId, server: server, scope: "moderator" } })
      .then(function (res) {
        toast(res.ok ? "Granted" : (res.body.error || "failed"), !res.ok);
        if (res.ok) { $("grant-discord-id").value = ""; loadGrants(server); }
      });
  });

  $("admin-grant-issue-btn").addEventListener("click", function () {
    var server = $("server-tabs").value;
    var discordId = $("admin-grant-discord-id").value.trim();
    var ownerTok = $("owner-token-input").value.trim();
    if (!discordId || !ownerTok) { toast("Discord ID and Owner token both required", true); return; }
    api("/admin/grants", {
      method: "POST", headers: { Authorization: ownerTok },
      json: { discordId: discordId, server: server, scope: "admin" },
    }).then(function (res) {
      toast(res.ok ? "Granted admin" : (res.body.error || "failed"), !res.ok);
      if (res.ok) { $("admin-grant-discord-id").value = ""; loadGrants(server); }
    });
  });

  // ---- identity suspend/reinstate ----
  function identityAction(action) {
    var server = $("server-tabs").value;
    var discordId = $("identity-discord-id").value.trim();
    if (!discordId) return;
    api("/identity/" + encodeURIComponent(server) + "/" + encodeURIComponent(discordId) + "/" + action,
      { method: "POST" }).then(function (res) {
      $("identity-result").textContent = res.ok ? (action + "d") : (res.body.error || "failed");
    });
  }
  $("identity-suspend-btn").addEventListener("click", function () { identityAction("suspend"); });
  $("identity-reinstate-btn").addEventListener("click", function () { identityAction("reinstate"); });

  // ---- boot ----
  api("/admin/session").then(function (res) {
    if (!res.ok) { showGate(); return; }
    $("admin-discord-id").textContent = res.body.discordId;
    enterDash(res.body.grants || []);
  });
})();
