# Smoke test — does a personal @gmail.com work with the Google Chat API?

Goal: settle the one open question before we redesign PLAN.md around "3 personal
Google accounts in one Space, driven by user OAuth." The docs conflict, so we
test it for real. **Nothing here uses glo.com** — you sign in with a personal
Gmail; the script aborts if it ever sees a glo.com token.

You run the steps below in *your* terminal (they open a browser for consent).
In this Claude session you can prefix each with `!` to run it inline, e.g.
`! gcloud auth login`.

---

## Fast path — gcloud ADC (no OAuth client to create)

```bash
# 0) See current accounts. You're probably on glo.com now — we will NOT use it.
gcloud auth list

# 1) Add + switch to your PERSONAL Gmail (browser opens; pick personal account)
gcloud auth login
gcloud config set account YOUR_PERSONAL@gmail.com      # <-- your gmail

# 2) Create a throwaway project under the personal account, enable Chat API
PROJ="chat-smoke-$(date +%s)"                            # globally-unique id
gcloud projects create "$PROJ" --name="chat-smoke"
gcloud config set project "$PROJ"
gcloud services enable chat.googleapis.com

# 3) Get USER OAuth creds (uses gcloud's own client — no consent screen to build).
#    Sign in with the SAME personal Gmail when the browser opens.
gcloud auth application-default login \
  --scopes="https://www.googleapis.com/auth/chat.spaces.readonly,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform"

# 4) Run the smoke test
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
python smoke/smoke_test_chat.py --quota-project "$PROJ"
```

### Reading the result
- **`✅ Consumer account CAN use the Chat API`** → the 3-accounts design is viable.
  Tell Claude; it patches PLAN.md to the user-OAuth design.
- **`❌ Consumer account appears to be BLOCKED`** → not viable; we fall back to a
  Workspace (your own trial) or a local/Slack demo.
- **`⚠️ API not enabled`** → token is fine, just run `gcloud services enable
  chat.googleapis.com` on **your** project and re-run step 4.
- **`⚠️ Token lacks the Chat scope`** → redo step 3 (gcloud may have refused the
  scope for its built-in client — use the fallback below).

---

## Optional — prove the real read+write loop

Only meaningful once step 4 prints PASS.

```bash
# a) In the Google Chat UI (web/app), signed in as your personal Gmail, create a
#    Space (the named kind, with threads). Note its id from the URL: spaces/XXXX
#    — or just run step 4 again; PASS lists your spaces with their ids.

# b) Re-consent with the WRITE scope added:
gcloud auth application-default login \
  --scopes="https://www.googleapis.com/auth/chat.messages,https://www.googleapis.com/auth/chat.spaces.readonly,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform"

# c) Post + list a message in that space:
python smoke/smoke_test_chat.py --quota-project "$PROJ" --space spaces/XXXX
```

`✅ Full read+write loop works` confirms the entire demo path on a personal account.

---

## Fallback — if gcloud refuses the Chat scope in step 3

gcloud's built-in OAuth client may decline `chat.*` scopes. Two options:

1. **OAuth 2.0 Playground** — open <https://developers.google.com/oauthplayground>,
   gear icon → tick *"Use your own OAuth credentials"* only if needed, authorize
   `https://www.googleapis.com/auth/chat.spaces.readonly` with your personal Gmail,
   click *Exchange authorization code for tokens*, copy the **access token**, then:
   ```bash
   GOOGLE_OAUTH_TOKEN="ya29.PASTE" python smoke/smoke_test_chat.py
   ```
2. **Your own OAuth client** — in your personal GCP project: APIs & Services →
   OAuth consent screen (External, Testing, add your Gmail as a test user) →
   Credentials → Create OAuth client → **Desktop app** → use that client_id/secret
   to mint a token (or paste the client into the Playground). Then run as in (1).

This is also exactly the credential the full demo will use, so it is not throwaway
work if you go this route.
