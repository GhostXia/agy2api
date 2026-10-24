# Security Research: Google Antigravity CLI (`agy`) Headless Mode, Permission Automation, and RCE Mitigation in `agy2api`

- **Author**: Security Research Team
- **Date**: 2026-10-21
- **Status**: Complete & Verified
- **Target Component**: `agy2api` (`server.py`, `agy_runner.py`, `config.py`) & Google Antigravity CLI (`agy` v1.4.4)

---

## 1. Problem Statement & Background

`agy2api` acts as a local bridge exposing an OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) backed by the official Google Antigravity CLI (`agy`). The client software consuming `agy2api` (e.g. SillyTavern, Chat UIs, coding plugins) expects standard text or chat completions.

However, `agy` is fundamentally an **autonomous agentic coding assistant**, equipped with tools for shell execution (`command`), file reading/writing/editing (`view_file`, `edit_file`, `list_directory`), and MCP servers. 

When invoking `agy` non-interactively via its headless print mode (`--print`), a critical conflict emerges between:
1. **Interactive permission prompts** designed for human confirmation in a terminal.
2. **Headless fail-closed behavior** that aborts with an auto-denial error when tools are called without prior grants.
3. **Severe security risks** (Arbitrary Command Execution / RCE) when all permissions are globally auto-approved via `--dangerously-skip-permissions`.

---

## 2. Research Question 1: Headless Print Mode (`--print`) & Tool Permissions

### Why does headless mode trigger "a tool required permission that headless mode cannot prompt for"?

#### A. Interactive vs. Headless Execution Modes
In standard interactive TUI mode, when an LLM decides to run a shell command (e.g., `git status` or `python run.py`), `agy` halts execution and prompts the user on `stdin`/TTY:
```
Do you want to run `git status`? [y/n/always/...]
```
In headless print mode (`--print=<prompt>` or `-p`), `agy` executes in batch mode without an attached interactive TTY user loop.

#### B. The Fail-Closed Permission Gate (`jetski`)
`agy`'s internal execution engine (historically named `jetski`) evaluates each tool call against:
1. Active session flags (e.g., `--dangerously-skip-permissions`).
2. Global permission grants in `~/.gemini/config/config.json` (`userSettings.globalPermissionGrants.allow`).
3. CLI settings in `~/.gemini/antigravity-cli/settings.json` (`permissions.allow`).

If an action matches an allow-rule, it proceeds. However, if an action requires confirmation:
- Because the process is running headless, **it cannot ask the user**.
- To prevent silent, unauthorized execution of arbitrary code, the engine enforces a strict **fail-closed** policy.
- It immediately aborts the tool call and writes the following sentinel error to `stderr`:
```
jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. command(<target>)). Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.
```
- Because the tool step was aborted, `agy` exits (often exit code 0 or 1) without printing a completion message or writing assistant content to the conversation database.

#### C. Why LLMs Trigger Tool Calls in a Text API Wrapper
Even though `agy2api` is used solely as a text completion API, `agy` registers tool definitions with the LLM. If a user asks a coding question, asks to check a file, or if the model hallucinates that it should verify a snippet using a bash command, the LLM generates a tool call proposal instead of plain text. In headless mode without `--dangerously-skip-permissions`, this immediately crashes the request.

---

## 3. Research Question 2: Flag Inventory for Sandboxing, Permissions & Tool Restriction

The official `agy` binary (`C:\Users\Administrator\AppData\Local\agy\bin\agy.EXE`, version 1.4.4) was analyzed via CLI introspection, changelog inspection, and binary decompilation/string analysis.

| Flag / Option | Supported in `agy`? | Exact Function & Behavior |
| :--- | :---: | :--- |
| `--sandbox` | **Yes** | Activates terminal sandboxing. On Windows 11 24H2+ (build 26100+) and Linux (user namespaces), file modifications inside the workspace are overlaid/virtualized in memory and discarded upon exit; paths outside the workspace fail with `EPERM`. **Crucial**: It does *not* auto-approve tool execution in headless mode. |
| `--dangerously-skip-permissions` | **Yes** | Causes `agy`'s permission assessor to auto-approve all tool permission requests without prompting. Without a sandbox, this grants unrestricted RCE to the agent. |
| `--add-dir <dir>` | **Yes** | Explicitly registers an absolute directory into the workspace. Repeatable. Files within this directory are indexed, read, and subject to edits. |
| `--read-only` | **NO (Synthetic)** | **Not a native `agy` flag.** Running `agy --read-only` exits with `flags provided but not defined: -read-only`. This flag was created in `relay.mjs` (`agy-delegate` skill) as a wrapper composite that passes `--sandbox --dangerously-skip-permissions`. |
| `--mode <mode>` | **Yes** | Options: `plan`, `auto`, `accept-edits`. In headless `--print` mode, empirical testing proves that `--mode plan` still attempts tool execution and times out rather than stripping tools cleanly. |
| `--agent <name>` | **Yes** | Selects a specific agent definition defined in `.agents/agents/<name>/agent.md`. Allows declaring an explicit `tools:` list in frontmatter. |
| `--disable-slash-commands` | **Yes** | Disables interactive slash commands (`/search`, `/git`) and skill expansions. |
| `--json-schema <schema>` | **Yes** | Enforces structured JSON output conforming to a schema. |
| `--print-timeout <dur>` | **Yes** | Time limit for print mode (e.g. `300s`). |

### Deep Dive: Terminal Sandbox Behavior on Windows
As documented in the Antigravity 1.4.3 changelog:
> *"Fixed the CLI offering options that need the terminal sandbox on systems where it can't run, such as Linux systems that block unprivileged user namespaces and Windows before Windows 11 24H2."*

On Windows 11 24H2+, `--sandbox` confines terminal processes and uses memory overlays. However:
1. Under `--sandbox` alone, headless `--print` **still auto-denies unapproved commands**.
2. Sandboxed command execution incurs process spin-up latency (often 15–30s+), increasing the risk of request timeouts.

---

## 4. Research Question 3: Current `agy2api` Configuration Audit

An inspection of `E:\github\repos\agy2api\config.py` and `E:\github\repos\agy2api\agy_runner.py` revealed critical vulnerabilities:

### 1. Working Directory Exposure (`workdir`)
- **Code (`config.py`, lines 165–167)**:
  ```python
  workdir: Path = Path(
      os.getenv("AGY_WORKDIR", str(Path(__file__).resolve().parent.parent))
  )
  ```
- **Code (`agy_runner.py`, lines 140, 168)**:
  ```python
  command.extend(["--add-dir", str(Path(workdir).resolve())])
  ...
  proc = subprocess.Popen(command, cwd=workdir, ...)
  ```
- **Vulnerability**:
  `__file__` is `E:\github\repos\agy2api\config.py`.
  `parent` is `E:\github\repos\agy2api`.
  `parent.parent` is **`E:\github\repos`**!
  `E:\github\repos` is the root container of **all repositories on the user's system**. `agy` is launched with `cwd=E:\github\repos` and `--add-dir E:\github\repos`, exposing all sibling projects, source code, and git repositories to inspection and modification.

### 2. Permissions Bypass Discrepancy
- **Code (`config.py`, line 170)**:
  ```python
  dangerously_skip_permissions: bool = _bool_env("AGY2API_SKIP_PERMISSIONS", True)
  ```
- **Documentation (`README.md`, line 128)**:
  ```markdown
  - AGY2API_SKIP_PERMISSIONS: auto-approve tool permission requests without interactive prompt, default false
  ```
- **Vulnerability**:
  While the README claims the default is `false`, the code actively defaults to **`True`**!
  Every invocation of `agy` automatically receives `--dangerously-skip-permissions`.

### 3. Sandbox Status
- **Code (`config.py`, line 169)**:
  ```python
  sandbox: bool = _bool_env("AGY2API_SANDBOX", False)
  ```
- Defaults to `False`.

### 4. Threat Matrix: Full Remote Code Execution (RCE)
Under default settings:
$$\text{workdir} = \text{E:\textbackslash github\textbackslash repos} \quad + \quad \text{skip\_permissions} = \text{True} \quad + \quad \text{sandbox} = \text{False}$$

When `agy2api` receives a request at `POST /v1/chat/completions`:
1. If the prompt contains a direct command or indirect prompt injection (e.g. from untrusted web pages, documents, or roleplay characters) instructing the model to execute a shell command:
2. The LLM issues a `command` tool call (e.g. `powershell.exe -c "Invoke-WebRequest ..."` or destructive file deletion).
3. `agy` executes the command with **no user confirmation**, **no sandbox containment**, and the **full privileges of the Windows user account**.

---

## 5. Research Question 4: Safest Architecture for `agy2api`

To completely eliminate ACE / RCE while preserving seamless OpenAI chat completion functionality, `agy2api` must implement a defense-in-depth model:

```
[ Incoming Request (/v1/chat/completions) ]
                   │
                   ▼
  Layer 1: Prompt Hardening Guard (Prepend Text-Only Assistant Instruction)
                   │
                   ▼
  Layer 2: Workspace Quarantine (cwd = Empty Isolated Directory; Never Parent Repos)
                   │
                   ▼
  Layer 3: Strict Sandbox / Permission Enforcement (Refuse skip_permissions without sandbox)
                   │
                   ▼
           [ Subprocess agy ] ──> Plain Text Output / SQLite Extraction
```

### Layer 1: Workspace Quarantine
- **Rule**: `agy` must NEVER run inside `E:\github\repos` or any real project folder.
- **Implementation**:
  Default `workdir` to a dedicated empty sandbox scratch directory inside `agy2api`:
  ```python
  default_workdir = Path(__file__).resolve().parent / "isolated_workspace"
  default_workdir.mkdir(parents=True, exist_ok=True)
  ```
- **Rule**: Never pass `--add-dir` pointing to `E:\github\repos` or parent directories. Only anchor the isolated workspace.

### Layer 2: Prompt Hardening Guard
- In `format_messages()`, prepend a system instruction:
  ```
  [System Instruction: You are an OpenAI-compatible text generator. Do NOT execute shell commands or use any tools. Return pure text.]
  ```

### Layer 3: Configuration Defaults Hardening
- In `config.py`:
  - Set `dangerously_skip_permissions: bool = _bool_env("AGY2API_SKIP_PERMISSIONS", False)`.
  - Set `sandbox: bool = _bool_env("AGY2API_SANDBOX", True)`.
  - In `__post_init__`, enforce safety invariants:
    - If `dangerously_skip_permissions` is `True` while `sandbox` is `False`, force `workdir` to the quarantined workspace and log a security alert, or require explicit confirmation.

---

## 6. Primary Sources & Code References

1. **`agy2api` Repository**:
   - `config.py`: `E:\github\repos\agy2api\config.py` (Lines 165–177: `workdir`, `sandbox`, `dangerously_skip_permissions`)
   - `agy_runner.py`: `E:\github\repos\agy2api\agy_runner.py` (Lines 130–156: subprocess command construction; Lines 227–234: headless permission denial detection)
   - `README.md`: `E:\github\repos\agy2api\README.md` (Lines 126–128: configuration defaults)
2. **Google Antigravity CLI (`agy`)**:
   - Binary Path: `C:\Users\Administrator\AppData\Local\agy\bin\agy.EXE`
   - Active Version: 1.4.4 (confirmed via `agy changelog`)
   - Official CLI Documentation References: `https://antigravity.google/docs/cli/reference`
3. **`delegate-skills / agy-delegate`**:
   - `C:\Users\Administrator\.gemini\config\skills\agy-delegate\SKILL.md` (Lines 118–149: permission model and `--read-only` composition)
   - `C:\Users\Administrator\.gemini\config\skills\agy-delegate\references\dispatch-and-poll.md` (Lines 40–42, 143–211: permission engine traps)
   - `C:\Users\Administrator\.gemini\config\skills\agy-delegate\scripts\relay.mjs` (Lines 24–47, 465–475: implementation of `--read-only` wrapper)
