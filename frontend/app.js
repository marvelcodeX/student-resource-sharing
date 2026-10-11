// StudyShare frontend.
//  - Sign-in / sign-up / email verification talk straight to Amazon Cognito (public API, no SDK).
//  - Every call to our Lambda API carries the Cognito access token as a Bearer token.
//  - Files go directly to/from S3 using presigned URLs.

const API = (window.API_URL || "").replace(/\/+$/, "");
const REGION = window.COGNITO_REGION;
const CLIENT_ID = window.COGNITO_CLIENT_ID;
const SESSION_KEY = "studyshare-session";
const MAX_BYTES = 10 * 1024 * 1024;
const DEFAULT_SUBJECTS = [
  "Cloud Computing", "Data Structures", "DBMS", "Operating Systems",
  "Computer Networks", "Mathematics", "Machine Learning",
];

let session = null;        // { access, id, refresh, exp }
let pendingUser = "";      // username waiting for verification
let pendingEmail = "";     // email the code was sent to (display only)
let allResources = [];

const $ = (id) => document.getElementById(id);
const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;

// ------------------------------------------------------------- generic helpers

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function setMsg(el, text, kind = "") {
  el.textContent = text;
  el.className = `msg ${kind}`;
}

function decodeJwt(token) {
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(escape(atob(payload))));
  } catch { return {}; }
}

// ------------------------------------------------------------- Cognito

async function cognito(target, body) {
  const res = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || "Request failed");
    err.code = (data.__type || "").split("#").pop();
    throw err;
  }
  return data;
}

function friendlyAuthError(err) {
  const known = {
    NotAuthorizedException: "Incorrect username or password.",
    UserNotFoundException: "Incorrect username or password.",
    UsernameExistsException: "That username is already taken. Try another or sign in.",
    InvalidPasswordException: "Password must be 8+ characters with upper and lower case letters, a number and a symbol.",
    CodeMismatchException: "That code is not correct. Check your email and try again.",
    ExpiredCodeException: "That code has expired. Click Resend code.",
    LimitExceededException: "Too many attempts. Please wait a few minutes.",
    TooManyRequestsException: "Too many attempts. Please wait a few minutes.",
    InvalidParameterException: err.message,
  };
  return known[err.code] || err.message || "Something went wrong.";
}

function storeSession(result, previousRefresh) {
  session = {
    access: result.AccessToken,
    id: result.IdToken,
    refresh: result.RefreshToken || previousRefresh,
    exp: Date.now() + (result.ExpiresIn || 3600) * 1000,
  };
  try { localStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch { /* private mode */ }
}

function clearSession() {
  session = null;
  try { localStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
}

async function refreshSession() {
  if (!session?.refresh) throw new Error("No session");
  const { AuthenticationResult } = await cognito("InitiateAuth", {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { REFRESH_TOKEN: session.refresh },
  });
  storeSession(AuthenticationResult, session.refresh);
}

async function validToken() {
  if (!session) throw new Error("Please sign in");
  if (Date.now() > session.exp - 60 * 1000) {
    try { await refreshSession(); } catch { signOut(); throw new Error("Session expired, please sign in again"); }
  }
  return session.access;
}

async function signIn(username, password) {
  const { AuthenticationResult } = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  storeSession(AuthenticationResult);
}

function signOut() {
  clearSession();
  allResources = [];
  showAuth("signin");
}

// ------------------------------------------------------------- API (Lambda)

async function api(path, options = {}) {
  const token = await validToken();
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { signOut(); throw new Error(data.error || "Please sign in again"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// XMLHttpRequest (not fetch) so we can show upload progress.
function uploadToS3(upload, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file); // must be the last field
    const xhr = new XMLHttpRequest();
    xhr.open("POST", upload.url);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
      ? resolve()
      : reject(new Error(`S3 upload failed (${xhr.status})`)));
    xhr.onerror = () => reject(new Error("S3 upload failed. Check the files bucket CORS settings."));
    xhr.send(form);
  });
}

// ------------------------------------------------------------- views

function showAuth(which) {
  $("app-view").classList.add("hidden");
  $("auth-view").classList.remove("hidden");
  ["signin", "signup", "confirm"].forEach((name) => {
    $(`${name}-form`).classList.toggle("hidden", name !== which);
    const msg = $(`${name}-form`).querySelector("[data-msg]");
    if (msg) setMsg(msg, "");
  });
}

function showApp() {
  $("auth-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
  const claims = decodeJwt(session.id);
  $("user-name").textContent = claims.name || claims["cognito:username"] || "Student";
  refresh();
}

function renderStats() {
  const subjects = new Set(allResources.map((r) => r.subject.toLowerCase()));
  const downloads = allResources.reduce((sum, r) => sum + (r.downloads || 0), 0);
  $("stat-count").textContent = allResources.length;
  $("stat-subjects").textContent = subjects.size;
  $("stat-downloads").textContent = downloads;
}

function renderSubjects() {
  const subjects = [...new Set([...DEFAULT_SUBJECTS, ...allResources.map((r) => r.subject)])].sort();
  $("subject-list").innerHTML = subjects.map((s) => `<option value="${escapeHtml(s)}">`).join("");

  const select = $("subject-filter");
  const current = select.value;
  const used = [...new Set(allResources.map((r) => r.subject))].sort();
  select.innerHTML = `<option value="">All subjects</option>` +
    used.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  select.value = used.includes(current) ? current : "";
}

function renderList() {
  const query = $("search").value.trim().toLowerCase();
  const subject = $("subject-filter").value;

  const visible = allResources.filter((r) => {
    if (subject && r.subject !== subject) return false;
    if (!query) return true;
    return [r.title, r.description, r.uploader, r.subject].join(" ").toLowerCase().includes(query);
  });

  setMsg($("list-msg"), visible.length ? "" : (allResources.length
    ? "No resources match your search."
    : "No resources yet. Be the first to share one."));

  $("resource-list").innerHTML = visible.map((r) => `
    <article class="card resource" data-id="${escapeHtml(r.resourceId)}">
      <span class="chip">${escapeHtml(r.subject)}</span>
      <h3>${escapeHtml(r.title)}</h3>
      ${r.description ? `<p class="desc">${escapeHtml(r.description)}</p>` : ""}
      <div class="meta">
        <div>${icon("file")} ${escapeHtml(r.fileName)}${r.fileSize ? ` &middot; ${formatSize(r.fileSize)}` : ""}</div>
        <div>${icon("user")} ${escapeHtml(r.uploader)}</div>
        <div>${icon("calendar")} ${new Date(r.createdAt).toLocaleDateString()} &middot; ${r.downloads || 0} downloads</div>
      </div>
      <div class="actions">
        <button class="btn primary" data-action="download">${icon("download")} Download</button>
        <button class="btn soft ${r.liked ? "on" : ""}" data-action="like">${icon("heart")} ${r.likes || 0}</button>
        ${r.isOwner ? `<button class="btn danger" data-action="delete">${icon("trash")} Delete</button>` : ""}
      </div>
    </article>`).join("");
}

async function refresh() {
  try {
    const data = await api("/resources");
    allResources = data.resources;
    renderStats();
    renderSubjects();
    renderList();
  } catch (err) {
    if (session) setMsg($("list-msg"), `Could not load resources: ${err.message}`, "error");
  }
}

// ------------------------------------------------------------- auth form handlers

function bindAuthForms() {
  document.querySelectorAll("[data-goto]").forEach((link) =>
    link.addEventListener("click", (e) => { e.preventDefault(); showAuth(link.dataset.goto); }));

  $("signin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const msg = form.querySelector("[data-msg]");
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    setMsg(msg, "Signing in...");
    try {
      pendingUser = form.username.value.trim();
      await signIn(pendingUser, form.password.value);
      form.reset();
      showApp();
    } catch (err) {
      if (err.code === "UserNotConfirmedException") {
        $("confirm-sub").textContent = `Your account is not verified yet. Enter the code we emailed you.`;
        showAuth("confirm");
        await resendCode(true);
      } else {
        setMsg(msg, friendlyAuthError(err), "error");
      }
    } finally { btn.disabled = false; }
  });

  $("signup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const msg = form.querySelector("[data-msg]");
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    setMsg(msg, "Creating account...");
    try {
      const fullName = form.name.value.trim();
      pendingUser = form.username.value.trim();
      pendingEmail = form.email.value.trim().toLowerCase();
      await cognito("SignUp", {
        ClientId: CLIENT_ID,
        Username: pendingUser,
        Password: form.password.value,
        UserAttributes: [
          { Name: "email", Value: pendingEmail },
          { Name: "name", Value: fullName },
          { Name: "given_name", Value: fullName.split(/\s+/)[0] },
        ],
      });
      // keep the password only in memory to sign in right after verification
      $("confirm-form").dataset.pw = form.password.value;
      form.reset();
      $("confirm-sub").textContent = `We emailed a 6-digit code to ${pendingEmail}.`;
      showAuth("confirm");
    } catch (err) {
      setMsg(msg, friendlyAuthError(err), "error");
    } finally { btn.disabled = false; }
  });

  $("confirm-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const msg = form.querySelector("[data-msg]");
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    setMsg(msg, "Verifying...");
    try {
      await cognito("ConfirmSignUp", {
        ClientId: CLIENT_ID, Username: pendingUser, ConfirmationCode: form.code.value.trim(),
      });
      const pw = form.dataset.pw;
      delete form.dataset.pw;
      form.reset();
      if (pw) {
        await signIn(pendingUser, pw);
        showApp();
      } else {
        showAuth("signin");
        setMsg($("signin-form").querySelector("[data-msg]"), "Email verified. Please sign in.", "ok");
      }
    } catch (err) {
      setMsg(msg, friendlyAuthError(err), "error");
    } finally { btn.disabled = false; }
  });

  $("resend-code").addEventListener("click", (e) => { e.preventDefault(); resendCode(false); });
  $("logout-btn").addEventListener("click", signOut);
}

async function resendCode(quiet) {
  const msg = $("confirm-form").querySelector("[data-msg]");
  try {
    await cognito("ResendConfirmationCode", { ClientId: CLIENT_ID, Username: pendingUser });
    if (!quiet) setMsg(msg, "A new code is on its way.", "ok");
  } catch (err) {
    setMsg(msg, friendlyAuthError(err), "error");
  }
}

// ------------------------------------------------------------- app actions

async function handleUpload(event) {
  event.preventDefault();
  const form = event.target;
  const file = form.file.files[0];
  const msg = $("upload-msg");
  const progress = $("upload-progress");

  if (!file) return setMsg(msg, "Choose a file first.", "error");
  if (file.size > MAX_BYTES) return setMsg(msg, "File is larger than 10 MB.", "error");

  $("upload-btn").disabled = true;
  try {
    setMsg(msg, "Requesting upload link...");
    const created = await api("/resources", {
      method: "POST",
      body: JSON.stringify({
        title: form.title.value,
        subject: form.subject.value,
        description: form.description.value,
        fileName: file.name,
        contentType: file.type || "application/octet-stream",
      }),
    });

    setMsg(msg, "Uploading to S3...");
    progress.classList.remove("hidden");
    await uploadToS3(created.upload, file, (pct) => { progress.value = pct; });

    setMsg(msg, "Saving...");
    await api(`/resources/${created.resourceId}/confirm`, { method: "POST" });

    form.reset();
    setMsg(msg, "Uploaded successfully.", "ok");
    await refresh();
  } catch (err) {
    setMsg(msg, err.message, "error");
  } finally {
    $("upload-btn").disabled = false;
    progress.classList.add("hidden");
    progress.value = 0;
  }
}

async function handleCardClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.closest("[data-id]").dataset.id;
  const action = button.dataset.action;

  button.disabled = true;
  try {
    if (action === "download") {
      const { downloadUrl } = await api(`/resources/${id}/download`);
      window.location.href = downloadUrl;
    } else if (action === "like") {
      await api(`/resources/${id}/like`, { method: "POST" });
    } else if (action === "delete") {
      if (!confirm("Delete this resource permanently?")) return;
      await api(`/resources/${id}`, { method: "DELETE" });
    }
    await refresh();
  } catch (err) {
    alert(err.message);
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------- init

function init() {
  bindAuthForms();
  $("upload-form").addEventListener("submit", handleUpload);
  $("resource-list").addEventListener("click", handleCardClick);
  $("search").addEventListener("input", renderList);
  $("subject-filter").addEventListener("change", renderList);

  const configured = API && CLIENT_ID && !CLIENT_ID.includes("PASTE_YOUR");
  if (!configured) {
    showAuth("signin");
    $("config-warning").classList.remove("hidden");
    return;
  }

  try { session = JSON.parse(localStorage.getItem(SESSION_KEY)); } catch { session = null; }
  if (session?.access) showApp(); else showAuth("signin");
}

init();
