# Development Guide


## 🚀 Quick Reference - Incremental Development Commands

**Most frequently used commands for smooth development:**

```bash
# Start development
make start                  # Start API service
make up / status / down     # Extra instances, one Telegram bot each (see "Parallel instances")

# Incremental quality checks (run these often!)
make format                 # Auto-format code (instant)
make lint                   # Linting and type checking
```

**💡 Pro tip**: Run `make format lint` after editing a few files to catch issues early!


## Parallel instances (one Telegram bot each)

Run several assistant instances side by side, for example one per workstream. Each
instance has its own Telegram bot. All of them run the same code from the same checkout
and `.venv` and share one config dir (the active `ASSISTANT_CONFIG_DIR`, e.g.
`config.local/`). This is not strict isolation; see "What is shared" below.

The **main instance does not change**: `make start` / `make stop` (port 8000, `./data`,
token in `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`). The launcher only manages the extra
instances and never touches the main one.

### Add a bot

1. In Telegram, talk to [@BotFather](https://t.me/BotFather): `/newbot`, pick a name and a
   username, and copy the token. Use one new bot per instance. Never reuse the main bot.
2. Put the token in `.env` (or your shell env) under a name that does **not** start with
   `ASSISTANT_`, e.g. `TG_BOT_TOKEN_RESEARCH=123456:ABC...`. `ASSISTANT_*` names are read by
   the app as config overrides (`ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN_X` would be an unknown
   field and silently ignored), so the launcher rejects them.
3. Add the instance to `<ASSISTANT_CONFIG_DIR>/instances.yaml`. Copy
   `config/instances.example.yaml` there first. The file is gitignored.

```yaml
instances:
  research:
    port: 8001                          # unique, free, not 8000
    token_env: TG_BOT_TOKEN_RESEARCH    # variable name, never the token itself
    # data_dir: data/instances/research # default: data/instances/<name>
    # instance_name: research           # label in logs / "System started (research)."
    # agent_node: false                 # agent_node ingress, off by default
    # env:                              # optional extra non-secret overrides
    #   ASSISTANT_MEMORY_DEFAULT_USER_ID: research
```

The allowlist comes from the shared `channel.telegram.yaml`, so the same users can use
every bot. Extra instances always bind `127.0.0.1`.

### Commands

```bash
make up                  # start every extra instance (detached)
make up NAME=research    # start one
make status [NAME=...]   # name, port, running/stopped/stale, PID, /health/live, token var, paths
make down [NAME=...]     # stop all extra instances (or one); the main instance keeps running
```

`make up` first checks **all** instances and starts **none** if any check fails:
- the token variable is missing or empty, or its name starts with `ASSISTANT_`;
- two instances share a token, or an instance uses the main instance's token;
- a port is used twice, is already taken, or is 8000;
- two instances share a data dir, or one uses the main data dir (`./data`).

Instances that are already running are skipped. Tokens are looked up in the process env
first, then in `.env`. `make status` prints token variable names, never token values.

Each instance runs `uvicorn assistant.api.main:app --host 127.0.0.1 --port <port>` with
the repo as working dir and these env overrides (the existing `ASSISTANT_<DOMAIN>_<FIELD>`
mechanism; they show up as `env_override` in `/admin/config/effective`):

| Variable | Value |
|---|---|
| `ASSISTANT_CONFIG_DIR` | the shared config dir |
| `ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN` | the value of `token_env` |
| `ASSISTANT_APP_DATA_ROOT` | the instance data dir |
| `ASSISTANT_APP_INSTANCE_NAME` | `instance_name` (default: the instance name) |
| `ASSISTANT_AGENT_NODE_ENABLED` | `false`, unless `agent_node: true` |
| `LOGFIRE_SERVICE_NAME` | `private-claw-<name>` |
| `env:` entries | as given |

`make down` sends SIGTERM to the PID in the PID file, but only if `ps` still shows this
instance's uvicorn on its port, so a reused PID is never killed. It waits up to 30 s for
a graceful shutdown, then sends SIGKILL. There is no autostart and no restart-on-crash:
run `make status` after a reboot.

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
would kill the first one's delegations. The launcher enforces separate data dirs.

### What is shared

- **Config dir**, including approved `capability_edit`s: an edit approved in any instance
  changes every instance (each picks it up on its next turn). The edit lock is per process,
  so approving edits to the same manifest in two instances at the same moment can race.
  `POST /admin/config/apply` writes the shared YAML files. It writes only the files' own
  keys plus the change, never env overrides such as an instance's data dir or token.
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
hand with the main token), Telegram returns 409 Conflict, both lose updates, and the app
logs `telegram.polling.conflict` at error level while it keeps backing off. Stop one of
the processes. `make up` refuses duplicate tokens, including the main one.


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
