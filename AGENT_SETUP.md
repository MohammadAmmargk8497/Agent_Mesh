# AgentMesh setup instructions for Claude Code

Read this file if a user asked you to set up AgentMesh on their computer. The human-readable guide is [README.md](README.md). Follow the steps below in order, and keep the user informed at each step.

## Ground rules (read first)

- **Never ask the user to paste a token, key or Viewer key into the chat**, and never print one. Tokens are typed hidden into the setup prompts or read from files the user controls.
- **Do not run commands that display or copy secrets** (`cat` on token files, `env`, printing `~/.config`).
- **Interactive prompts must be run by the user.** `agentmesh-server init` and `agentmesh-setup` ask for hidden input you cannot type for them. Ask the user to run those exact commands in their own terminal, or with the `!` prefix in Claude Code, and wait for them to finish.
- **Never run `tailscale` commands that change the tailnet** (`funnel`, `reset`, sharing, ACL changes). Only the documented `tailscale serve --bg --https=8443 localhost:8787` may be proposed, and the user runs it.
- **Never call the live AgentMesh API** (`agents.zahid.win`, `agentmesh.zahid.win`) or read messages. Only check local health.
- **Ask before anything that changes an existing setup** (replacing `config.env`, rotating tokens, reinstalling Claude's MCP registration).

## Step 0: Choose the role

Ask the user one question: **"Are you the Host (you run the server on this computer) or the Friend (you connect to someone else's server)?"**

- Host → go to Part A.
- Friend → go to Part B.

If the user does not know, read the "Choose your role" section of README.md to them.

## Step 1: Check prerequisites (both roles)

Run these checks and report the results:

```bash
python3 --version        # need 3.11 or newer (on Windows: py -3 --version)
tailscale status         # need Tailscale connected
claude --version         # need Claude Code installed
```

If anything is missing, stop and tell the user which link in README.md "What you need" to follow. Do not install system software without asking.

Confirm the project folder contains `README.md`, `client/` and `server/`. Work from that folder for every later command.

## Part A: Host

1. **Create the virtual environment and install** (safe to run):
   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install ./server ./client
   ```
2. **Initialize the server** (user runs it; it asks no questions but writes tokens):
   ```bash
   .venv/bin/agentmesh-server init
   ```
   If it says settings already exist, do **not** re-run with `--force`. Keep the existing settings.
3. **Start the server.** This blocks, so ask the user to run it in a separate terminal and keep it open:
   ```bash
   .venv/bin/agentmesh-server serve
   ```
4. **Check it locally** (safe, read-only):
   ```bash
   curl -s http://127.0.0.1:8787/health
   ```
   Expect `"ok": true` and `"service": "agentmesh"`.
5. **Share through Tailscale.** Ask the user to run:
   ```bash
   tailscale serve --bg --https=8443 localhost:8787
   ```
   Then tell them to share the device with their Friend using the Tailscale admin console (README.md, Host step 4). Do not change access rules yourself.
6. **Register the Host's own client** (only if the Host also uses Claude Code): follow Part B steps 2–4.

Report to the user: server running, health check result, and the Friend address to send (`https://<machine>.<tailnet>.ts.net:8443`). The Host never sends their token; the Friend gets their token from the Host through a private channel (README.md, "6. Send your Friend their connection details").

## Part B: Friend

1. **Install the client** (safe to run):
   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install ./client
   ```
   Windows: `py -3 -m venv .venv` then `.\.venv\Scripts\python.exe -m pip install .\client`.
2. **Ask the user for two non-secret values:** the complete `https://` server address from the Host, and confirmation they have their `agent-b` token ready. Do not ask for the token itself.
3. **Run the guided setup** (the user runs it, because it hides the token prompt):
   ```bash
   .venv/bin/agentmesh-setup
   ```
   Answers: `agent-b`, the server address, and their token (typed hidden). The tool saves `~/.config/agentmesh/config.env` and prints a `claude mcp add` command.
4. **Run the printed command.** Ask the user to paste the exact command it printed into their terminal. Do not modify it and do not edit `config.env`.
5. **Verify.** Ask the user to start a new Claude Code session and type `/mcp`. They should see **agentmesh** connected. Do not call any AgentMesh tool yourself to test it.

## If something fails

Use the troubleshooting table in README.md. Common cases:

- **Python missing or too old:** install from the link in README.md, reopen the terminal.
- **Command not found:** the user is not in the project folder containing `client/` and `server/`.
- **Setup rejects the address:** the address must be complete `https://…` for public or Tailscale names.
- **Connection times out:** the Host's computer must be awake, the server terminal open, and both Tailscale apps connected.

Stop and report the exact error to the user instead of guessing at fixes that change tokens, settings or Tailscale configuration.

## Done checklist

- [ ] Role chosen and prerequisites confirmed
- [ ] Host: `init` done, `serve` running, local health OK, Tailscale sharing set up by the user
- [ ] Friend: `agentmesh-setup` completed, `claude mcp add` run by the user, `/mcp` shows agentmesh connected
- [ ] No tokens were printed, pasted into chat or sent anywhere
- [ ] The user has been told what, if anything, remains for them to do
