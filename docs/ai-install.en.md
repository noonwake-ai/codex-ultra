# Install Codex Ultra for me

> **To the AI reading this:** this file is a complete task. Read all of it before acting,
> then work through it in order. Your user is a real person who does not know the internals
> and does not need to — make the decisions, do the checking, and report back in plain words.

---

## What this is

Codex Ultra is a local adapter that lets Codex use models other than GPT
(DeepSeek, Gemini, Claude, Grok, MiniMax, Kimi, GLM and more) and gives them Codex's
native abilities: automatic context compaction, tool calling and image bridging.

**It is not a model gateway.** The models come from the user's own gateway
(a self-hosted Sub2API, for example). Codex Ultra only adapts and forwards on the
local machine.

---

## Before anything, check three things

```bash
sw_vers -productVersion          # macOS 13 or newer
python3 --version                # Python 3.11 or newer
git --version
```

If any of these fails, **stop and tell the user what is missing.** Do not work around it.
On non-macOS systems the adapter can still be run by hand, but there is no one-command
installer — say so honestly.

---

## Then ask the user two questions

**Question 1: what is your gateway address?**

Something like `https://xxx.example/v1`. The user may not know; ask them to find it in
their gateway dashboard. **Never guess, never invent, and never try an address you have
seen elsewhere.**

**Question 2: which model should handle context compaction?**

It must genuinely exist in their gateway. See step 3 for how to recommend one.

---

## Steps

### 1. Clone the source

```bash
cd ~/Downloads
git clone https://github.com/noonwake-ai/codex-ultra.git
cd codex-ultra
```

### 2. Help the user set the credential (be careful here)

**Never** write the key into a file, shell history, or a log, and never `echo` it.

Have the user run this in their own terminal:

```bash
export CODEX_ULTRA_API_KEY="your-gateway-key"
```

If the user pasted the key into chat, **tell them clearly** that it now lives in the chat
history, that they should rotate it in their gateway, and that an environment variable is
the better habit going forward.

### 3. Build the model catalog

```bash
python3 build_catalog.py --gateway <their gateway address> --out models.json
```

It prints a summary: how many models were found, which vendors they belong to, and which
ones need image bridging.

- `gateway_http_401` / `403`: wrong key or missing permission. Ask the user to check.
  **Do not try a different key.**
- `gateway_returned_non_json`: that address is not the endpoint Codex Ultra expects.
  Ask the user to confirm it (a common mistake is a missing `/v1`, or landing on a login page).
- `requested_model_not_offered:xxx`: their key cannot see that model.

Once you have the list, **you choose the compaction model**: prefer one with a full set of
reasoning levels and a lightweight marker such as `flash`, `mini` or `lite` in its name.
If you genuinely cannot tell, give the user two or three candidates and let them pick.
Explain your choice in one sentence.

### 4. Run the project's own tests (do not skip this)

```bash
python3 -m unittest discover -s . -p 'test_*.py'
```

You should see `OK`. This runs fully offline and spends no model credits.
If it fails, show the user the failure verbatim and stop — **do not install on top of a
failing test run.**

### 5. Install

```bash
python3 install.py \
  --upstream <gateway address> \
  --compactor-model <the model you picked in step 3> \
  --compactor-effort medium
```

On macOS, if the user manages providers with CC Switch, add:

```bash
  --cc-switch-db ~/.cc-switch/cc-switch.db
```

Explain to the user, before you run it, that this will:

1. create an isolated environment under `~/Library/Application Support/Codex Ultra/`,
2. register a background service that starts at login,
3. create an encryption key in the system Keychain (this is *not* their API key),
4. **change exactly one `base_url`** in their config so requests point at the local adapter.

macOS may show a Keychain prompt. **That is expected, not a failure.**

### 6. Verify

```bash
python3 configure.py status
```

Check that `routed_locally` is `true`.

Then confirm the service is alive:

```bash
curl -s http://127.0.0.1:15731/health
```

You should see `"status":"ok"` and the currently effective `media_models` list.

---

## What to tell the user when you are done

**Four things, in plain words:**

1. **It is installed; restart Codex.** Existing chats are not lost. Open a new chat and
   their models will be in the model picker.
2. **It is reversible, one command:**
   ```bash
   python3 configure.py rollback
   ```
   That restores the single network address and leaves every other change they made intact.
3. **Their key and chat history were not touched.** Codex Ultra forwards and reshapes
   requests; it does not modify credentials or history.
4. **Where to complain:** paste the error into a GitHub issue.

---

## Hard rules (breaking these hurts the user)

- **Never print, upload or commit a credential.** If you find what looks like a key in a
  file, stop and say so.
- **Do not touch the user's gateway config, DNS or proxy.** This project edits one local
  `base_url` and nothing else.
- **Never silently degrade.** If a model cannot handle images, or a route is flaky, say so
  instead of pretending it worked.
- **Do not continue past a failing test.** Fix it or report exactly where you are stuck.
- **Do not change the user's model choice, reasoning effort, skills, plugins or MCP
  settings.** The installer leaves them alone; so do you.
- **Do not patch the project's source to force the flow through.** If you hit a real bug,
  ask the user to file an issue rather than hot-fixing their machine.

---

## When something goes wrong

| Symptom | Meaning |
|---|---|
| `upstream_must_be_https` | The gateway address must start with `https://` |
| `upstream_must_not_be_loopback` | The gateway cannot be `localhost` / `127.0.0.1`; that would create a proxy loop |
| `service_not_healthy_route_unchanged` | The service did not start. **The client config was not modified** — the failure is safe. Check `~/Library/Application Support/Codex Ultra/service-error.log` |
| `already_installed_use_configure_status` | A previous install exists. Have the user run `python3 configure.py status`; do not overwrite |
| Port 15731 in use | Use another port: `--port 15741`, plus `configure.py apply --local http://127.0.0.1:15741` |

Full details in [Install](INSTALL.md); model policy in [Model policy](MODELS.md).

---

**Start now.** Your first step is asking the user those two questions.
