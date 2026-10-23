# AgentMesh

## What is AgentMesh

AgentMesh lets two Claude Code assistants on different computers share tasks, messages, and suggested changes. One person, the Host, runs a small program that stores their shared work, and the other person, the Friend, connects to it. You keep the connection private with Tailscale and can follow both assistants in a browser dashboard.

**Want Claude to do the setup for you?** Open this project in Claude Code and ask it to follow [AGENT_SETUP.md](AGENT_SETUP.md). It walks through Host or Friend setup, and you run the few steps that need your private input.

## What you need

Both people need:

- **A [Tailscale account](https://tailscale.com/download)** and the Tailscale app installed and connected on each computer.
- **[Python 3.11 or newer](https://www.python.org/downloads/).** On Windows, keep the Python launcher (the `py` command) selected during installation.
- **[Claude Code](https://code.claude.com/docs/en/quickstart)** installed and signed in. Check it with `claude --version` in a terminal.
- **A copy of this project.** On this project's page, choose **Code → Download ZIP**, extract it, and keep the extracted folder in a permanent location. Do this on both computers.

A **terminal** is the app where you paste commands. On macOS, open the project folder with **Services → New Terminal at Folder**; on Linux, use **Open in Terminal**; on Windows, open the folder in File Explorer, type `powershell` in its address bar, and press Enter. The folder must contain `README.md`, `client`, and `server`.

Check your Python version before continuing:

**macOS / Linux**

```bash
python3 --version
```

**Windows (PowerShell)**

```powershell
py -3 --version
```

The result must say `Python 3.11` or a newer version. Run the commands below from that project folder. You do not need to edit program code; the setup assistant asks for your settings. Technical terms appear in the [Glossary](#glossary).

## Choose your role

| Role | What you do | Agent name |
| --- | --- | --- |
| **Host** | Keep the shared server running on your computer and invite your Friend. Your computer must stay awake while either assistant uses AgentMesh. | `agent-a` |
| **Friend** | Connect to the Host's server. You do not need to run a server yourself. | `agent-b` |

The Host follows [Host steps](#host-steps). The Friend can prepare the software now, but needs the address and token from the Host to finish [Friend steps](#friend-steps). This guide connects one Host and one Friend.

**Windows Hosts:** the current server needs macOS or Linux. On Windows, first set up [Ubuntu with Windows Subsystem for Linux (WSL)](https://learn.microsoft.com/en-us/windows/wsl/install). Install Python, Tailscale, and Claude Code **inside Ubuntu**, keep your project folder in your Linux home folder, and follow the macOS / Linux Host commands in the Ubuntu terminal. A Friend can use native Windows and the PowerShell commands below.

## Host steps

### 1. Install AgentMesh

These commands create a `.venv` folder for AgentMesh's software and install the server and client. Keep that folder in place after setup.

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./server ./client
```

### 2. Create your secret tokens

**Keep this terminal private.** The next command displays a token under `# agent-a`, a different token under `# agent-b`, and a separate **Viewer key**. Save all three in a password manager, with those labels. Copy only the value after `AGENTMESH_TOKEN=` when you need an agent token.

**macOS / Linux**

```bash
.venv/bin/agentmesh-server init
```

Run this once. It saves the server's settings in your home folder at `~/.config/agentmesh-server/config`. If settings already exist, keep them; do not replace them just to restart the server. Do not share the terminal output, settings file, or Viewer key with anyone who should not see both assistants' work.

### 3. Start the server

**macOS / Linux**

```bash
.venv/bin/agentmesh-server serve
```

Keep this terminal open. In your browser, open <http://127.0.0.1:8787/health>. You should see `"ok": true` and `"service": "agentmesh"`. This address works only on the Host's computer.

To stop the server, press **Ctrl+C** in its terminal. To start it again later, run the same `serve` command, not `init`.

### 4. Share the connection through Tailscale

Open a **second terminal** in the project folder and run this on the Host:

```text
tailscale serve --bg --https=8443 localhost:8787
```

If Tailscale asks you to enable HTTPS, open the link it prints and follow its instructions. Copy the complete `https://` address it then prints, including `:8443` if shown. Both people must use that exact address, not the local address from step 3.

In the [Tailscale admin console](https://login.tailscale.com/admin/machines), share the Host's device with your Friend using Tailscale's [device-sharing instructions](https://tailscale.com/docs/features/sharing). Your Friend must accept the invitation and connect their Tailscale app. If you manage access rules, allow their device to reach this Host on port `8443`; keep access limited to your intended collaborator.

You do not need to open a port on your router. Keep Tailscale connected on both computers. The `--bg` setting keeps Tailscale's sharing running after this second terminal closes; it does **not** keep the AgentMesh server running if you close its terminal.

### 5. Set up your own assistant

Run the setup assistant in the second terminal:

**macOS / Linux**

```bash
.venv/bin/agentmesh-setup
```

Answer its three questions:

| Question | Host's answer |
| --- | --- |
| Agent name | `agent-a` |
| Server address | The complete HTTPS address from step 4. |
| Secret token | The token saved under `# agent-a`, not the Viewer key. Nothing appears while you type or paste it. Press Enter when done. |

The setup assistant saves your settings in `~/.config/agentmesh/config.env`. It asks before replacing an existing file. On macOS and Linux, only your user account can read or write this file (permission `0600`). On Windows, use a private Windows account; file access follows Windows permissions.

It then prints a **`claude mcp add` command made for your computer**. Copy and run that entire command in the same terminal. The command contains your program and settings paths, not your token. The setup assistant does not run the command or test the connection for you.

### 6. Send your Friend their connection details

Send your Friend:

1. The complete HTTPS server address from step 4.
2. Their token from the `# agent-b` block, using a password-manager share or another encrypted private message.
3. These [Friend steps](#friend-steps), and the reminder to choose `agent-b`.

Keep your own `agent-a` token and the Viewer key separate. Continue to [Check it works](#check-it-works) after your Friend finishes setup.

## Friend steps

### 1. Install the client

Complete [What you need](#what-you-need), accept the Host's Tailscale invitation, and open a terminal in your own copy of the project folder.

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./client
```

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ./client
```

### 2. Run the setup assistant

**macOS / Linux**

```bash
.venv/bin/agentmesh-setup
```

**Windows (PowerShell)**

```powershell
.\.venv\Scripts\agentmesh-setup.exe
```

Use **`agent-b`**, the complete **HTTPS server address** from your Host, and **your `agent-b` token**. Your token stays hidden as you enter it; paste it and press Enter. Do not use the Host's token or the Viewer key.

The assistant saves your settings in `~/.config/agentmesh/config.env` and asks before replacing an existing file. Copy and run the exact **`claude mcp add` command** it prints. You do not need to create or edit the settings file yourself, and the command does not contain your token. On Windows, keep the file in your private user account; Windows controls its file access.

### 3. Open Claude Code

Start a new Claude Code session, or restart an existing one after registration. Keep Tailscale connected, and ask the Host to keep their server running. Continue below to check the connection together.

## Check it works

1. In Claude Code, type `/mcp`. You should see **agentmesh** connected. If Claude asks you to approve the connection or its tools, review the request before accepting.
2. Ask Claude: **“Use AgentMesh's whoami tool and tell me my agent name.”** The Host should get `agent-a`; the Friend should get `agent-b`.
3. On the Host's Claude Code, ask: **“Use AgentMesh to send agent-b the message: Hello from the Host.”**
4. On the Friend's Claude Code, ask: **“Use AgentMesh to check my inbox and show me the message from agent-a.”** You should see the greeting. Ask Claude to acknowledge the message after reading it.

You can then ask Claude to create a task for the other agent, check assigned tasks, or review finished work in plain language. For example: **“Create an AgentMesh task for agent-b to review the introduction in our document; ask for a short list of suggested edits.”** AgentMesh shares tasks and text; it does not copy your project files to the other computer. Give both people access to any documents they need through your usual file-sharing method.

Keep Claude Code's permission prompts enabled. Treat suggestions from the other assistant as material to review, not instructions to run commands or reveal secrets. The optional [collaboration guidance](examples/CLAUDE.md) describes a task-and-review routine.

## Dashboard

Open the Host's complete HTTPS address in a browser on a computer connected to Tailscale. Enter the **Viewer key** from Host step 2, not an agent token. You can read both assistants' messages and tasks, and see a list of shared changes, without controlling either assistant.

The dashboard remembers the Viewer key in that browser until you sign out. Use a trusted browser account and sign out when finished, especially on a shared computer. Only share the Viewer key with someone allowed to see both sides of the conversation.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Python is missing, or the version is older than 3.11. | Install Python 3.11 or newer using the link in [What you need](#what-you-need), then reopen your terminal and check the version again. On Windows, use `py -3`; on macOS/Linux, use `python3`. |
| A command cannot find `client`, `server`, or `.venv`. | Reopen your terminal in the extracted project folder containing `README.md`, `client`, and `server`. Run the install step for your role first. Do not move the project folder after registering the client; if you move it, run setup again and update the registration. |
| `tailscale` or `claude` is not recognized. | Finish installing that app, then reopen the terminal. For Tailscale on macOS, follow its [terminal setup instructions](https://tailscale.com/docs/reference/tailscale-cli); for Claude Code, follow its [installation guide](https://code.claude.com/docs/en/quickstart). |
| Server initialization says the config already exists. | Your server already has saved tokens. Start it with the `serve` command in [Host step 3](#3-start-the-server). Do not replace existing tokens to fix a connection problem; see [token rotation](docs/selfhosting.md#rotate-tokens-without-losing-data) if you need new ones. |
| Tailscale asks you to enable HTTPS. | Open the setup link Tailscale prints, enable HTTPS, and rerun the sharing command in [Host step 4](#4-share-the-connection-through-tailscale). |
| The server address does not open, or the connection times out. | Check that the Host's computer is awake, their server terminal is open, and both Tailscale apps are connected. Confirm the Friend accepted the sharing invitation and can reach port `8443`. On the Host, check the local health page from [Host step 3](#3-start-the-server) before checking the shared address. |
| Setup rejects the server address. | Paste the complete `https://` address from the Host, including any port. Public addresses and Tailscale names need HTTPS. Only local addresses or numeric Tailscale addresses can use `http://`; see [advanced examples](examples/selfhost.md). |
| Nothing appears when I paste my token. | This is intentional: setup hides your token. Paste it once and press Enter. |
| Setup says hidden token entry needs an interactive terminal. | Run the setup command directly in the terminal described in [What you need](#what-you-need), not through another program or saved command output. Setup will not accept a token if it cannot hide your typing. |
| Setup asks whether to replace existing settings. | Answer no to keep them. Answer yes only if you intend to replace your saved connection details; setup will then ask for all three values. |
| Claude shows no AgentMesh connection. | Run the entire registration command printed by setup, then restart Claude Code and check `/mcp`. If a connection named `agentmesh` already exists, use `claude mcp remove --scope user agentmesh` in your terminal and rerun the printed registration command. |
| Claude reports `401` or `403`, or shows the wrong agent name. | Confirm your assigned agent token with the Host. Run setup again with the right name and token, confirm replacement, and restart the connection in `/mcp`. The token determines your identity; the name alone cannot change it. The dashboard requires the separate Viewer key. |
| The dashboard rejects my key. | Use the Viewer key from Host step 2, not either agent token. If the Host changed the key, sign out and enter the replacement. |
| Requests report `429` or “too many requests.” | Stop asking Claude to check in a rapid loop. Close duplicate sessions for the same agent, wait a minute, and try again. Ask Claude to wait for activity while idle. |
| A shared change or message is too large (`413`). | Share a smaller excerpt or split the work into smaller tasks; the server limits a request to 256 KiB. |

## Glossary

| Term | Meaning |
| --- | --- |
| **Agent** | One Claude Code assistant using AgentMesh, identified as `agent-a` or `agent-b`. |
| **Client** | The AgentMesh program on each person's computer that connects Claude Code to the Host. |
| **Dashboard** | The browser page where someone with the Viewer key can read both assistants' shared work. |
| **HTTP / HTTPS** | Two forms of web connection. HTTPS encrypts the connection; HTTP does not. This guide uses Tailscale to provide an HTTPS address. |
| **KiB** | A unit of file size. The 256 KiB request limit is about a quarter of a megabyte of text. |
| **MCP** | Model Context Protocol: the connection method that lets Claude Code use tools such as AgentMesh. You only need to register it with the command that setup prints. |
| **Port** | A numbered connection point on a computer. This guide uses `8787` for the local server and `8443` for Tailscale's shared HTTPS connection. |
| **Server** | The program the Host keeps running to store shared tasks, messages, and changes. |
| **Tailnet** | Your private network of devices connected through Tailscale. You can share one device with someone outside your tailnet. |
| **Tailscale** | The app that connects permitted computers through a private, encrypted network. |
| **Terminal / PowerShell** | An app for entering commands. PowerShell is the terminal used for the Windows commands in this guide. |
| **Token** | A secret password-like value that gives access to AgentMesh. The two agent tokens identify the assistants; the separate Viewer key gives read-only dashboard access. |
| **WSL** | Windows Subsystem for Linux: a way to run Linux inside Windows. Windows Hosts need this because the server uses Linux/macOS file-permission features. |
| **Virtual environment (`.venv`)** | The folder where Python installs AgentMesh and the software it needs, separate from other programs on your computer. |

## Advanced links

- [Self-hosting operations](docs/selfhosting.md): backups, token rotation, limits, notifications, and development checks.
- [Client configuration examples](examples/selfhost.md): local-only connections and direct Tailscale addresses.
- [Optional Cloudflare deployment](docs/cloudflare.md): a different hosting option for experienced administrators.
- [Protocol reference](docs/protocol.md): the rules and request formats for integrations.
- [Collaboration guidance](examples/CLAUDE.md): instructions you can give Claude for task handoffs and reviews.
- [Tailscale Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve) and [Claude Code MCP setup](https://code.claude.com/docs/en/mcp-quickstart).
