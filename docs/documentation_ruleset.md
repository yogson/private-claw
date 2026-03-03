# Project Documentation Ruleset

This document defines the standards and patterns for creating comprehensive project documentation that supports large feature development tracking.

## Document Structure Standards

### 0. High Level Architecture Analysis

When we deal not just with a large feature, but with a brand-new software project, there is a need for comprehensive project-level architectural analysis.

**Purpose**: Comprehensive architectural analysis and decomposition of the overall system
**Filename Pattern**: `HIGH_LEVEL_ARCHITECTURE.md`

**Required Sections**:
- **Overview**: Brief description and project goals
- **Executive Summary**: Key components/subsystems goals, interaction patterns
- **Proposed Architecture**: High level architecture design with focus on large components/subsystems and their cooperation.
- **Implementation Strategy**: High level approach and patterns
- **Risk Assessment**: Potential risks and mitigation strategies

### 1. Architecture Analysis Document
**Purpose**: Comprehensive architectural analysis and decomposition  
**Filename Pattern**: `[FEATURE_NAME]_ARCHITECTURE_ANALYSIS.md`

**Required Sections**:
- **Overview**: Brief description and transformation goals
- **Executive Summary**: Key transformation goals, patterns, models, and strategies
- **Current Architecture Analysis**: 
  - Existing components assessment (✅ Strengths, ⚠️ Constraints)
  - Component dependency analysis with visual diagrams
- **Proposed Architecture**: New architecture design with clear separation of concerns
- **Implementation Strategy**: Detailed approach and patterns
- **Risk Assessment**: Potential risks and mitigation strategies

**Standards**:
- Use clear visual component diagrams in text format
- Mark architectural constraints explicitly with warning symbols
- Provide dependency trees showing relationships
- Include both current state and target state analysis
- **Assign unique IDs to all components/modules** for cross-referencing (see Component Identification section)

### 2. Technical Specification Document
**Purpose**: Detailed technical contracts, data structures, interfaces, and implementation requirements  
**Filename Pattern**: `[FEATURE_NAME]_TECHNICAL_SPECIFICATION.md`

**Core Required Sections**:
- **Overview**: Purpose, scope, and technical context
- **System Interfaces**: External integration points and contracts
- **Data Structures**: Core data models, entities, and formats
- **Business Logic Specifications**: Processing rules, algorithms, and workflows
- **Validation & Constraints**: Input validation, business rules, and system limits
- **Error Handling**: Error scenarios, recovery strategies, and failure modes
- **Performance Requirements**: Response times, throughput, and resource constraints
- **Implementation Details**: Technology-specific requirements and configurations

**WARNING!** No code at these documents!

**Project-Type Specific Sections** (include as applicable):

**For API/Service Development**:
- **API Specification**: Endpoint definitions, request/response formats
- **Authentication & Authorization**: Security requirements and access patterns
- **Rate Limiting & Throttling**: Usage limits and protection mechanisms

**For UI/Frontend Development**:
- **User Interface Specifications**: Component behaviors, state management
- **User Experience Flows**: Navigation, interactions, and feedback mechanisms
- **Accessibility Requirements**: WCAG compliance and assistive technology support

**For Data Processing Systems**:
- **Data Pipeline Specifications**: Input sources, transformations, output targets
- **Data Quality Requirements**: Validation rules, cleansing, and monitoring
- **Batch vs. Streaming**: Processing patterns and timing requirements

**For Library/Framework Development**:
- **Public API Contracts**: Exported functions, classes, and modules
- **Configuration Options**: Settings, parameters, and customization points
- **Extension Points**: Plugin interfaces and customization mechanisms

**Standards**:
- Use semantic explanations rather than code examples for clarity
- Structure information in tables for complex data relationships
- Include comprehensive validation rules with specific constraints
- Document all error scenarios with appropriate response mechanisms
- Specify exact requirements and acceptable value ranges
- **Assign component IDs to all interfaces, data models, and processing units** for traceability
- Adapt section selection based on project type while maintaining core structure


### 3. Implementation Plan Document
**Purpose**: Phase-by-phase implementation roadmap with detailed tasks  
**Filename Pattern**: `[FEATURE_NAME]_IMPLEMENTATION_PLAN.md`

**Required Sections**:
- **Overview**: Implementation strategy and core principles
- **Parallel development**: Parallel work opportunities
- **Phase Details**: For each phase:
  - **Risk Level**: LOW/MEDIUM/HIGH
  - **Priority**: CRITICAL/HIGH/MEDIUM/LOW
  - **Objectives**: Clear phase goals
  - **Tasks**: Numbered tasks with specific deliverables
  - **Dependencies**: Phase and task dependencies
  - **Deliverables**: Concrete outputs
  - **Success Criteria**: Measurable completion criteria
- **Quality Gates**: Checkpoints after key phases
- **Success Metrics**: Overall project success measurements

**Standards**:
- Each phase should have 3-5 focused tasks maximum
- Dependencies must be explicitly listed
- Success criteria must be measurable and testable
- Include risk assessment for each phase

## Content Standards

### Code Examples
- Avoid code examples in project documentation
- Use code only in cases where it's the most expressive solution (like describing data formats, DTOs)

### Visual Elements
- Use text-based diagrams for component relationships
- Include tables for structured data (APIs, configurations, etc.)
- Use consistent symbols:
  - ✅ for strengths/completed items
  - ⚠️ for constraints/warnings
  - 📦 for deliverables
  - 🚀 for objectives
  - 🎯 for success criteria

### Component Identification
**Purpose**: Establish traceable links between documentation and code implementation

**ID Assignment Rules**:
- **Format**: Use `CMP_[FEATURE]_[COMPONENT_TYPE]_[NAME]` pattern
- **Examples**: 
  - `CMP_AUTH_SERVICE_TOKEN_VALIDATOR`
  - `CMP_USER_CONTROLLER_PROFILE_MANAGER`
  - `CMP_DATA_MODEL_USER_ENTITY`

**Documentation Usage**:
- **First Mention**: Define component with full ID: `UserAuthenticationService [CMP_AUTH_SERVICE_USER_AUTH]`
- **Subsequent References**: Use short form with ID: `UserAuth [CMP_AUTH_SERVICE_USER_AUTH]`
- **Cross-Document References**: Link to specific components: `@ARCHITECTURE_ANALYSIS.md#CMP_AUTH_SERVICE_USER_AUTH`

**Code Implementation**:
- **Class/Module Comments**: Include component ID in header comments
  ```
  /**
   * Component ID: CMP_AUTH_SERVICE_TOKEN_VALIDATOR
   */
  ```
- **File Naming**: When practical, include abbreviated ID in filename
- **Configuration**: Use component IDs as keys in configuration files

**Traceability Requirements**:
- Each component ID must appear in both documentation and implementation
- Component dependencies must reference specific IDs
- Implementation tasks must specify which component IDs they deliver
- Testing specifications must reference component IDs being tested

### Writing Style
- **Clear, actionable language**
- Use imperative mood for instructions
- Avoid ambiguous terms like "maybe", "should", "might"
- Be specific about file names, paths, and technical details
- Use consistent terminology throughout all documents

### Validation Requirements
- **Include comprehensive validation rules**
- Specify exact formats (UUID, paths, etc.)
- Define acceptable value ranges
- Document required vs. optional fields
- Provide example valid and invalid inputs

## Documentation Organization

### File Naming Convention
```
docs/[feature_name]/
├── [FEATURE_NAME]_ARCHITECTURE_ANALYSIS.md
├── [FEATURE_NAME]_TECHNICAL_SPECIFICATION.md  
├── [FEATURE_NAME]_IMPLEMENTATION_PLAN.md
├── project_tracker.json
└── project_tracker_viewer.html
```

### Cross-Referencing
- Reference other documents using `@filename.md` syntax
- Include section links for specific parts of documents
- **Reference components using IDs**: `[CMP_AUTH_SERVICE_USER_AUTH]` for inline references
- **Link to component definitions**: `@ARCHITECTURE_ANALYSIS.md#CMP_AUTH_SERVICE_USER_AUTH`
- Maintain consistency in terminology across all documents
- Update cross-references when structure changes
- **Component dependency diagrams** must use component IDs for all connections

## Quality Assurance Checklist

### Completeness Check
- [ ] All required sections are present
- [ ] If code examples are present, they are complete and compilable
- [ ] All validation rules are specified
- [ ] Dependencies are clearly documented
- [ ] Success criteria are measurable
- [ ] **All components/modules have assigned IDs**
- [ ] **Component IDs follow the standard naming pattern**

### Consistency Check
- [ ] Terminology is consistent across all documents
- [ ] File paths and names match across documents
- [ ] API specifications match implementation examples
- [ ] Phase dependencies align with task requirements
- [ ] **Component IDs are consistent across documentation and code**
- [ ] **Component references use proper ID syntax**
- [ ] **Dependency diagrams reference components by ID**

### Clarity Check
- [ ] Technical concepts are explained clearly
- [ ] Examples are realistic and helpful
- [ ] Instructions are actionable and specific
- [ ] Risk assessments are concrete and useful

## Integration with Project Tracking

Create a project tracking JSON file using `project_tracker-example.json` as a reference, name it `project_tracker.json`.

### Project Tracker Alignment
- **Each phase in implementation plan must have corresponding phase in `project_tracker.json`**
- Task IDs must match between documents and tracker
- Success criteria should align with tracker success_criteria
- Deliverables must match files_delivered tracking
- For feature-scoped documentation under `docs/[feature_name]/`, treat `docs/[feature_name]/project_tracker.json` as authoritative for that feature scope

### Status Tracking
- Use consistent status values: not_started, in_progress, implemented, completed, on_hold, cancelled
- Document which tasks produce which files
- **Specify component IDs in task deliverables** to track implementation progress
- Maintain traceability from requirements to implementation
- Track dependencies between phases and tasks
- **Link component IDs to specific implementation tasks** in project tracker

## Maintenance Guidelines

### Version Control
- Track changes to requirements and specifications
- Maintain changelog of significant updates
- Ensure documentation stays synchronized with implementation
- Review and update during each phase completion

### Review Process
- Technical review for accuracy and completeness
- Architecture review for design consistency
- Implementation review for feasibility
- Documentation review for clarity and organization

This ruleset ensures that project documentation supports effective large-scale feature development with clear tracking, measurable progress, and comprehensive technical guidance.
