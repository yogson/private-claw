# Development Guide


## 🚀 Quick Reference - Incremental Development Commands

**Most frequently used commands for smooth development:**

```bash
# Start development
make start                  # Start API service

# Incremental quality checks (run these often!)
make format                 # Auto-format code (instant)
make lint                   # Linting and type checking
```

**💡 Pro tip**: Run `make format lint` after editing a few files to catch issues early!


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

#### Code Organization Best Practices

1. **Function Organization:**
   ```python
   # Good: Single responsibility, clear naming
   async def get_health_status() -> HealthStatus:
       """Get comprehensive health status."""

   # Bad: Multiple responsibilities
   async def get_health_and_update_cache_and_log():
       """Do everything."""
   ```

2. **Import Organization:**
   ```python
   # Good: Explicit, organized imports
   from app.core.config import settings
   from app.core.health import health_manager

   # Bad: Wildcard imports
   from app.core import *
   ```

3. **Error Handling:**
   ```python
   # Good: Specific error handling per module
   try:
       return await health_manager.run_all_checks()
   except HealthCheckError as e:
       raise HTTPException(status_code=503, detail=str(e))
   ```

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
8. **Documentation**: Keep the project documentation up to date, update relevant docs according to `docs/FRAMEWORK.md`
9. **Progress tracking**: Update tracking docs to reflect the progress achieved

**🚨 WORKFLOW ANTI-PATTERN TO AVOID:**
- Making large changes across many files without intermediate checks
- Running quality tools only at the end (leads to overwhelming error lists)
- Skipping type checking until commit time (harder to debug context)

## Code style and RULES
- Write docstrings for the public classes, methods, and functions
- DO NOT add docstrings for private methods, classes, and functions
- Plan modules to be self-contained according to OPC, and **low coupling, high cohesion**
- Keep Python modules concise. Plan module to be one-purpose-oriented. 
- If a module grows over 200 lines, consider splitting
- Interfaces to be named according to Python way: `SomeThingInterface`, and NOT `ISomething`
- Each Python module should have a brif header pointing to the documentation framework entities (MOD)
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

### Debugging

```bash
# View logs
make logs
```

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
