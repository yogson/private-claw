# Development Guide


## 🚀 Quick Reference - Incremental Development Commands

**Most frequently used commands for smooth development:**

```bash
# Start development
make start                  # Start API service (single process, no instances.yaml)
make up / status / down     # Instances from instances.yaml (see "Parallel instances")

# Incremental quality checks (run these often!)
make format                 # Auto-format code (instant)
make lint                   # Linting and type checking
```

**💡 Pro tip**: Run `make format lint` after editing a few files to catch issues early!


## Parallel instances (equal instances)

Run several assistant instances side by side, for example one per workstream. All of
them run the same code from the same checkout and `.venv` and share one config dir (the
active `ASSISTANT_CONFIG_DIR`, e.g. `config.local/`). This is not strict isolation; see
"What is shared" below.

There are two modes, chosen by whether `<ASSISTANT_CONFIG_DIR>/instances.yaml` exists:

- **No `instances.yaml`: single process, as before.** `make start` runs one instance in
  the foreground (`0.0.0.0:8000`, `./data`, token in `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`,
  channels from the shared config: `enabled` in `channel.telegram.yaml` and
  `agent_node.yaml`). `make stop` stops whatever listens on port 8000.
- **`instances.yaml` exists: the launcher manages every instance.** All instances are
  declared in that file and are equal: none is special, and any of them may use port
  8000 and `data_dir: data`. Use `make up` / `make down` / `make status`. `make start` and
  `make stop` refuse with a message (exit code 2), so there is exactly one way to start
  and stop instances. The file decides, even if it lists no instances; remove or rename
  it to go back to `make start`.

### Add an instance

1. For a Telegram instance, talk to [@BotFather](https://t.me/BotFather): `/newbot`, pick a
   name and a username, and copy the token. Use one bot per instance.
2. Put the token in `.env` (or your shell env) under a name that does **not** start with
   `ASSISTANT_`, e.g. `TG_BOT_TOKEN_RESEARCH=123456:ABC...`. `ASSISTANT_*` names are read by
   the app as config overrides (`ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN_X` would be an unknown
   field and silently ignored), so the launcher rejects them.
3. Add the instance to `<ASSISTANT_CONFIG_DIR>/instances.yaml`. Copy
   `config/instances.example.yaml` there first. The file is gitignored.

```yaml
instances:
  main:
    port: 8000                          # unique, free; 8000 is an ordinary port
    token_env: TG_BOT_TOKEN_MAIN        # variable name, never the token itself
    data_dir: data                      # the classic ./data is allowed too
  research:
    port: 8001
    token_env: TG_BOT_TOKEN_RESEARCH
    # data_dir: data/instances/research # default: data/instances/<name>
    # instance_name: research           # label in logs / "System started (research)."
    # telegram: true                    # Telegram channel, on by default
    # agent_node: false                 # agent_node HTTP ingress, off by default
    # env:                              # optional extra non-secret overrides
    #   ASSISTANT_MEMORY_DEFAULT_USER_ID: research
```

The allowlist comes from the shared `channel.telegram.yaml`, so the same users can use
every bot. Launcher instances always bind `127.0.0.1`.

### Channels per instance: Telegram and HTTP ingress

Each instance switches its two inputs independently. The launcher always sets both, so
`enabled` in the shared `channel.telegram.yaml` and `agent_node.yaml` does not matter for
launcher instances (it still does for `make start`).

- `telegram` (default `true`): the instance polls its own bot. `token_env` is required.
  With `telegram: false` there is no bot: `token_env` is optional and ignored, and the
  token checks below are skipped.
- `agent_node` (default `false`): the agent-node HTTP ingress (`/jobs`) on the instance's
  own port, `http://127.0.0.1:<port>`. It uses the shared `agent_node.yaml` (`auth_token`,
  `repositories`, ...). The app's own checks still apply: if that file is missing the
  ingress stays off, and with an empty `auth_token` the app logs `agent_node.disabled` and
  serves no `/jobs`. For an ingress-only instance `make up` checks both (see below).

An instance with both off would serve nothing, so the launcher rejects `instances.yaml`.

Every instance gets its bot token only through the launcher's
`ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN` override: the value of its `token_env`, or empty
with `telegram: false`. The override replaces a bare `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`
from the shell env or `.env` and the `bot_token` in the shared `channel.telegram.yaml`, so
no instance inherits a token it was not given. If the bare variable is still set,
`make up` prints a note: move the token to a `token_env` variable and remove it.
(`load_dotenv` still loads the other `.env` variables, such as `TG_BOT_TOKEN_*`, into each
instance's process env.)

```yaml
instances:
  writing:                              # Telegram only (the defaults)
    port: 8002
    token_env: TG_BOT_TOKEN_WRITING
  jobs:                                 # HTTP ingress only: no bot, no token
    port: 8003
    telegram: false
    agent_node: true
  research:                             # both
    port: 8001
    token_env: TG_BOT_TOKEN_RESEARCH
    agent_node: true
```

### Commands

```bash
make up                  # start every instance (detached)
make up NAME=research    # start one
make status [NAME=...]   # name, port, running/stopped/stale, PID, /health/live, token var, paths
make down [NAME=...]     # stop all instances (or one)
```

`make up` first checks **all** instances and starts **none** if any check fails:
- the token variable is missing or empty, or its name starts with `ASSISTANT_`
  (instances with `telegram: true` only);
- two instances share a bot token (instances with `telegram: true` only);
- an ingress-only instance (`telegram: false`, `agent_node: true`) has no
  `agent_node.yaml` in the config dir, or no `auth_token` (neither in that file nor in
  `ASSISTANT_AGENT_NODE_AUTH_TOKEN`), so it would serve nothing;
- two instances share a port, or a port is already taken (for example by a process left
  over from `make start`);
- two instances share a data dir.

Instances that are already running are skipped. Tokens are looked up in the process env
first, then in `.env`. `make status` prints token variable names, never token values.

Each instance runs `uvicorn assistant.api.main:app --host 127.0.0.1 --port <port>` with
the repo as working dir and these env overrides (the existing `ASSISTANT_<DOMAIN>_<FIELD>`
mechanism; they show up as `env_override` in `/admin/config/effective`):

| Variable | Value |
|---|---|
| `ASSISTANT_CONFIG_DIR` | the shared config dir |
| `ASSISTANT_CHANNEL_TELEGRAM_ENABLED` | `telegram` (`true` / `false`) |
| `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN` | the value of `token_env`; empty with `telegram: false` |
| `ASSISTANT_APP_DATA_ROOT` | the instance data dir |
| `ASSISTANT_APP_INSTANCE_NAME` | `instance_name` (default: the instance name) |
| `ASSISTANT_AGENT_NODE_ENABLED` | `agent_node` (`true` / `false`, default `false`) |
| `LOGFIRE_SERVICE_NAME` | `private-claw-<name>` |
| `env:` entries | as given (the `ASSISTANT_*` variables above are reserved: use the YAML keys) |

`make down` sends SIGTERM to the PID in the PID file, but only if `ps` still shows this
instance's uvicorn on its port, so a reused PID is never killed. It waits up to 30 s for
a graceful shutdown, then sends SIGKILL. It does not stop processes it did not start
(such as one from `make start`). There is no autostart and no restart-on-crash: run
`make status` after a reboot.

### Migrating from a "main" instance plus extra instances

Before this change the main instance ran outside the launcher (`make start`, port 8000,
`./data`, `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`). To bring it under the launcher without
losing its sessions or memory:

1. Stop the old main instance while `make stop` still works (before updating the
   checkout), or afterwards with `kill -TERM $(lsof -ti :8000)`. Otherwise `make up`
   refuses: port 8000 is in use.
2. In `.env`, rename `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN=...` to a non-`ASSISTANT_`
   name, e.g. `TG_BOT_TOKEN_MAIN=...`.
3. Add it to `instances.yaml`. Set `agent_node: true` if the old main instance served
   `/jobs` (i.e. `agent_node.yaml` had `enabled: true`):

   ```yaml
   instances:
     main:
       port: 8000
       token_env: TG_BOT_TOKEN_MAIN
       data_dir: data
   ```

4. `make up NAME=main`, then `make status`.

Differences from `make start`: the instance binds `127.0.0.1` instead of `0.0.0.0` (it is
no longer reachable from other machines on port 8000), runs detached with its log in
`data/logs/uvicorn-<date>.log` (the same file as before), says "System started (main)."
and reports to Logfire as `private-claw-main`.

### What is per instance

Everything under the instance's data dir (`data/instances/<name>/` by default): sessions,
tasks and task logs, delegations, the idempotency ledger, locks, the active session /
model / capability context, usage archive, verbose state, vocabulary, memory artifacts,
the agent_node store, app logs (`logs/assistant-*.log`), the uvicorn log
(`logs/uvicorn-<date>.log`) and the PID file (`run/uvicorn.pid`). The bot (and therefore
which chats it serves), the port, and the "System started" message (sent through the
instance's own bot to the chats in its own data dir) are per instance too.

A data dir must never be used by two live processes: on startup an instance marks the
running delegations in its store as failed, so a second process on the same data dir
would kill the first one's delegations. The launcher enforces separate data dirs. A data
dir may contain another instance's (e.g. `data` and the default `data/instances/<name>`).

### What is shared

- **Config dir**, including approved `capability_edit`s: an edit approved in any instance
  changes every instance (each picks it up on its next turn). The edit lock is per process,
  so approving edits to the same manifest in two instances at the same moment can race.
  The admin UI of **any** instance can save config: `POST /admin/config/apply` writes only
  the files' own keys plus the payload, and the admin UI edit form is pre-filled with the
  file's values (or schema defaults), not with the instance's env overrides. Saving a form
  section therefore never writes an instance's `ASSISTANT_*` overrides (e.g.
  `ASSISTANT_MEMORY_DEFAULT_USER_ID`) to the shared config. Fields that this instance
  overrides from the env are marked `env` in the form; editing one changes the shared
  file, while this instance keeps using its override.
- **Mem0**: `memory.default_user_id` is shared, so memories from one workstream are visible
  in all of them. For separate memories set `ASSISTANT_MEMORY_DEFAULT_USER_ID` under `env:`.
- **`~/.claude/settings.json`**: every instance writes it at startup and on capability
  changes. With one shared config the content is the same; if an instance overrides
  `ASSISTANT_CAPABILITIES_*`, the last writer wins.
- **Admin login**: the same `ASSISTANT_ADMIN_TOKEN` everywhere. Browser cookies are not
  scoped by port, so logging in on one port logs you in on all of them.
- **Delegation token caps** are computed per instance, so the effective global cap is
  N × `delegation_global_token_cap`.
- Code, `.venv`, prompts. Each instance is a full app process with its own sub-agents.

### Telegram routing and 409 Conflict

Telegram delivers a bot's updates to whoever polls that bot's token, so routing is per
token: each instance answers only through its own bot. Telegram allows one `getUpdates`
consumer per token. If two processes poll the same token (e.g. an instance started by
hand with a token a launcher instance also uses), Telegram returns 409 Conflict, both
lose updates, and the app logs `telegram.polling.conflict` at error level while it keeps
backing off. Stop one of the processes. `make up` refuses duplicate tokens.


## Software Architecture Principles

The API follows established software engineering principles to ensure maintainability, scalability, and code quality. 
Our architecture emphasizes **modularity**, **SOLID principles**, and **DRY (Don't Repeat Yourself)** practices.

### 🔧 Refactoring Guidelines

#### When to Create New Modules

**✅ Create a new API module when:**
- Adding a new business domain (e.g., sessions, events, analytics)
- Endpoint group has >5 related endpoints
- Module would have distinct authentication/authorization needs
- Feature can be independently developed and tested

**❌ Don't create new modules for:**
- Single endpoints (add to existing appropriate module)
- Temporary or experimental endpoints (use debug module)
- Tightly coupled functionality (keep in same module)

### 🚀 Future Architecture Considerations

As the project grows, consider these architectural patterns:

1. **Service Layer**: Extract business logic from API handlers
2. **Repository Pattern**: Abstract database operations
3. **Factory Pattern**: For creating complex objects
4. **Observer Pattern**: For event-driven functionality
5. **Strategy Pattern**: For algorithm variations (e.g., different auth methods)

## Development Loop

**🔄 WE RUN INCREMENTAL DEVELOPMENT CYCLE:**

1. **Small Code Changes**: Edit ONE MODULE in the codebase
2. **Quick Format & Lint**: Right after finishing with the every SINGLE MODULE, run `make format lint` (prevents accumulation of sanity issues)
3. **Hot Reload**: FastAPI development server automatically reloads on changes (in debug mode)
4. **Repeat Steps 1-3** for next small batch of changes
5. **Pre-Commit Check**: `make format lint` (before git commit)
6. **Git add**: If new files/modules arrive, add them to Git
7. **Database migration**: If the database schema was changed, add the appropriate migration `make migrations migrate`
8. **Documentation**: Keep the project documentation up to date, update relevant docs
9. **Tests on feature complete**: Add concise unit tests for implemented behavior; add integration tests when the feature interacts with existing functionality
10. **Progress tracking**: Update tracking docs to reflect the progress achieved

**🚨 WORKFLOW ANTI-PATTERN TO AVOID:**
- Making large changes across many files without intermediate checks
- Running quality tools only at the end (leads to overwhelming error lists)
- Skipping type checking until commit time (harder to debug context)

## Code style and RULES
- Write docstrings for the public classes, methods, and functions
- DO NOT add docstrings for private methods, classes, and functions
- Plan modules to be self-contained according to OPC, and **low coupling, high cohesion**
- Keep Python modules concise. Plan module to be one-purpose-oriented. 
- IMPORTANT: If a module grows over 200 lines, consider splitting
- Interfaces to be named according to Python way: `SomeThingInterface`, and NOT `ISomething`
- Each Python module should have a brif header pointing to the documentation framework entities
- If the database schema was changed, add the appropriate migration and roll it
- All imports at the top of the module!!!
- **Class/Module Comments**: Include component ID in header comments
  ```
  /**
   * Component ID: CMP_AUTH_SERVICE_TOKEN_VALIDATOR
   */
  ```

**🚨 CODE ANTI-PATTERN TO AVOID:**
- Do not use late importing to prevent circular imports. Proper plan and implement modules instead.

## Quality Standards

### 📋 SOLID Principles Checklist
When planning the code, follow the principles:

- ✅ **Single Responsibility**: Each function/class has one clear purpose AND one responsible party
- ✅ **Open/Closed**: New features extend existing code without modification
- ✅ **Liskov Substitution**: Implementations are interchangeable
- ✅ **Interface Segregation**: Modules only depend on what they need
- ✅ **Dependency Inversion**: Depend on abstractions, not concrete implementations

### 🔄 DRY Implementation
- Reuse existing configuration patterns
- Follow established router structure
- Use existing error handling patterns
- Leverage shared middleware and utilities
- ALWAYS CHECK code for duplications on planning stage and review after implementation

### Configuration Management
- Use environment variables for all configuration
- Document all configuration options
- Provide sensible defaults
- Validate configuration on startup

### Logging
- Use structured logging with correlation IDs
- Log at appropriate levels
- Include relevant context in log messages
- Avoid logging sensitive information

## Testing Expectations

- Add tests when a feature is complete; do not postpone test coverage to a later phase.
- Keep unit tests concise, focused, and behavior-oriented.
- Add integration tests when feature behavior crosses existing boundaries or depends on existing workflows.
- Cover happy paths and major corner cases.
- Avoid overcomplicated, brittle, or overly broad tests.
