from __future__ import annotations

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self' data:; style-src 'self' 'nonce-__NONCE__'; script-src 'self' 'nonce-__NONCE__'; connect-src 'self'; font-src 'self' data:; base-uri 'none'; frame-ancestors 'none'">
<title>__PRODUCT_NAME__</title>
<style nonce="__NONCE__">__PAGE_STYLE__</style>
</head>
<body>
<main>
<div class="topbar">
<div class="brand"><span class="brand-mark"></span><span id="brandName">__PRODUCT_NAME__</span></div>
<div class="top-controls">
<a class="mini-control logout-button session-chip" id="sessionChip" href="#" hidden></a>
<button type="button" class="mini-control logout-button" id="logoutButton" hidden>Log out</button>
<button type="button" class="mini-control theme-toggle" id="themeToggle" aria-label="Toggle theme">__DARK_ICON__</button>
</div>
</div>

<div class="stack">
<section class="popup hero-card" id="bootCard">
<div class="popup-inner hero-inner">
<p class="eyebrow">__PRODUCT_NAME__</p>
<h1>Loading your workspace.</h1>
<p class="lead">Checking your current session and Tailnet access.</p>
</div>
</section>

<section class="popup hero-card" id="authCard" hidden>
<div class="popup-inner hero-inner">
<p class="eyebrow">__PRODUCT_NAME__</p>
<h1 id="heroTitle">Sign in with Tailscale.</h1>
<p class="lead" id="heroLead">This app is available only through your tailnet and authenticates with tsidp.</p>
<div class="actions">
<a class="button" id="loginButton" href="/api/auth/login">Sign in with Tailscale</a>
</div>
<div id="authMessage" class="message"></div>
</div>
</section>

<section class="popup shell-card" id="chatCard" hidden>
<div class="popup-inner">
<div class="section-head">
<div>
<p class="eyebrow">Chat</p>
<h2>Your Agent</h2>
</div>
</div>
<div id="chatMessage" class="message"></div>
<div class="shell">
<div class="chat-log" id="chatLog"><div class="empty-state">No messages yet.</div></div>
<form class="chat-form" id="chatForm">
<label class="field full">
<span>Message</span>
<textarea id="chatInput" placeholder="Write a message to your agent." required></textarea>
</label>
<div class="actions">
<button class="button" id="chatSubmit" type="submit">Send</button>
</div>
</form>
</div>
</div>
</section>

<section class="popup shell-card" id="workspaceCard" hidden>
<div class="popup-inner">
<div class="section-head workspace-head">
<div>
<p class="eyebrow">Workspace</p>
<h2>Shared Files</h2>
</div>
<div class="usage-block">
<div class="usage-copy"><span id="workspaceUsageLabel">0 B / 0 B used</span></div>
<div class="usage-meter" aria-hidden="true"><span id="workspaceUsageBar"></span></div>
</div>
</div>
<p class="lead small">Files and folders created here are written directly into the live-mounted runtime workspace so your agent can see them immediately.</p>
<div id="workspaceMessage" class="message"></div>
<div class="workspace-layout">
<section class="shell workspace-explorer-shell">
<div class="workspace-toolbar">
<div class="workspace-toolbar-left">
<button class="workspace-round-button" id="workspaceUpButton" type="button" aria-label="Go up one folder">&#8593;</button>
<code class="token-link workspace-path" id="workspacePathLabel">/</code>
</div>
<div class="workspace-toolbar-right">
<button class="workspace-round-button" id="workspaceFolderButton" type="button" aria-label="Create folder">&#65291;</button>
<button class="workspace-round-button" id="workspaceUploadButton" type="button" aria-label="Upload files">&#8682;</button>
</div>
</div>
<input id="workspaceFileInput" type="file" multiple hidden>
<form class="workspace-inline-form" id="workspaceFolderForm" hidden>
<label class="field full">
<span>New folder name</span>
<input id="workspaceFolderName" type="text" placeholder="reports">
</label>
<div class="actions">
<button class="button secondary-button" id="workspaceFolderSubmitButton" type="submit">Create folder</button>
</div>
</form>
<form id="workspaceUploadForm" hidden></form>
<div class="workspace-dropzone" id="workspaceDropzone">
<div class="workspace-drop-copy"><strong>Drop files here</strong><span>or use the upload button</span></div>
</div>
<div class="workspace-list" id="workspaceTable"><div class="workspace-empty muted-cell">No files yet.</div></div>
</section>
</div>
</div>
</section>

<section class="popup shell-card" id="accountCard" hidden>
<div class="popup-inner">
<div class="section-head">
<div>
<p class="eyebrow">Account</p>
<h2>Tailscale Access</h2>
</div>
</div>
<p class="lead small">Your product account is authenticated directly by your Tailscale identity on this branch.</p>
<div id="accountMessage" class="message"></div>
<section class="shell admin-token-shell">
<div class="token-row">
<span class="table-badge is-success">Your current Tailscale Identity:</span>
<code class="token-link" id="accountTailnetLogin"></code>
</div>
<div class="token-row">
<span class="table-badge">Tailnet app URL</span>
<code class="token-link" id="accountTailnetUrl"></code>
</div>
</section>
</div>
</section>

<section class="popup shell-card" id="adminCard" hidden>
<div class="popup-inner">
<div class="section-head">
<div>
<p class="eyebrow">Admin</p>
<h2>User Management</h2>
</div>
</div>
<p class="lead small">Invite users by their Tailscale login or email. Only invited Tailscale identities can claim an account.</p>
<section class="shell admin-token-shell" id="adminNetworkCard" hidden>
<div class="section-head compact-head">
<div>
<p class="eyebrow">Network</p>
<h2>Tailnet Access</h2>
</div>
</div>
<p class="lead small" id="adminNetworkLead">Tailnet exposure controls whether the app is published through Tailscale Serve.</p>
<div id="adminNetworkMessage" class="message"></div>
<div class="token-row" id="adminNetworkTailnetRow" hidden>
<code class="token-link" id="adminNetworkTailnetUrl"></code>
<button class="button" id="adminTailnetActivateButton" type="button">Enable Tailnet</button>
<button class="button secondary-button" id="adminTailnetDisableButton" type="button" hidden>Disable Tailnet</button>
</div>
</section>
<div class="admin-layout">
<form class="admin-form shell" id="adminCreateUserForm">
<label class="field full">
<span>Tailscale login or email</span>
<input id="adminTailscaleLoginInput" type="text" placeholder="alice@example.com" required>
</label>
<label class="field full">
<span>Display name</span>
<input id="adminDisplayNameInput" type="text" placeholder="Alice Example">
</label>
<div class="actions">
<button class="button" id="adminCreateUserButton" type="submit">Create invite link</button>
</div>
<div id="adminMessage" class="message"></div>
</form>
<section class="shell admin-token-shell" id="adminSignupTokenCard" hidden>
<div class="section-head compact-head">
<div>
<p class="eyebrow">Invite Link</p>
<h2>One-time claim URL</h2>
</div>
</div>
<p class="lead small">The link is valid for one claim and currently uses a seven-day lifetime.</p>
<div class="token-row">
<code class="token-link" id="adminSignupTokenUrl"></code>
<button class="button secondary-button" id="adminCopySignupLinkButton" type="button">Copy</button>
</div>
</section>
</div>
<div class="table-shell">
<table>
<thead>
<tr><th>User</th><th>Tailscale login</th><th>Status</th><th>Action</th></tr>
</thead>
<tbody id="adminUsersTable"><tr><td colspan="4" class="muted-cell">No users loaded yet.</td></tr></tbody>
</table>
</div>
</div>
</section>
</div>
</main>
<script nonce="__NONCE__">__PAGE_SCRIPT__</script>
</body>
</html>"""
