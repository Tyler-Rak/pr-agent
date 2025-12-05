# Data Flow Diagrams

This document shows how data flows through PR-Agent during various operations.

## Table of Contents

1. [File Fetching Flow (Bitbucket Server)](#file-fetching-flow-bitbucket-server)
2. [PR Diff Processing Pipeline](#pr-diff-processing-pipeline)
3. [Compression Strategy](#compression-strategy)
4. [AI Request/Response Flow](#ai-requestresponse-flow)

---

## File Fetching Flow (Bitbucket Server)

Shows the hybrid REST API + Git clone approach for efficient file fetching with minimal API calls.

### Current Implementation: Hybrid Approach

```mermaid
sequenceDiagram
    participant Webhook as Webhook
    participant Provider as BitbucketServerGitProvider
    participant API as Bitbucket REST API
    participant Git as Git Clone
    participant FS as File System

    Webhook->>Provider: Initialize with PR info

    Note over Provider,API: Phase 1: Get Merge-Base (2-3 API calls)
    Provider->>API: get_pull_requests_commits(limit=100)
    API-->>Provider: PR commits list [1-2 calls]
    Provider->>API: get_commits(limit=100)
    API-->>Provider: Target branch commits [1-2 calls]
    Provider->>Provider: calculate_best_common_ancestor()
    Note over Provider: base_sha determined

    Note over Provider,Git: Phase 2: Clone Repository (0 API calls)
    Provider->>Git: git clone --filter=blob:none --depth=1
    Git-->>FS: Shallow clone (metadata only)
    Provider->>Git: git fetch --depth=1 head_sha base_sha
    Git-->>FS: Fetch 2 specific commits

    Note over Provider,API: Phase 3: Get Changed Files (1 API call)
    Provider->>API: get_pull_requests_changes()
    API-->>Provider: List of changed files

    Note over Provider,Git: Phase 4: Get File Contents (0 API calls)
    loop For each changed file
        Provider->>Git: git show {sha}:{path}
        Git-->>Provider: File content (from local clone)
        Provider->>Provider: Generate unified diff
        Provider->>Provider: Create FilePatchInfo
    end

    Provider-->>Webhook: List[FilePatchInfo]

    Note over Provider: Total: 3-4 REST API calls<br/>(regardless of PR size)
```

### Performance Comparison

| Approach | API Calls (50 files) | Rate Limit Impact | Scalability | File Access Speed |
|----------|---------------------|-------------------|-------------|------------------|
| **Old: Pure REST API** | 103 | ❌ Exceeds 60 limit | ❌ Fails on large PRs | ~30s |
| **New: Hybrid Git Clone** | 3-4 | ✅ Well under limit | ✅ Works for any size | ~5-8s |

### Benefits

- **96% fewer API calls**: 3-4 vs 103 calls for 50-file PR
- **No rate limiting**: Always stays under Bitbucket's 60-token burst limit
- **Faster**: 5-8s vs 30s for large PRs
- **Scalable**: Works for PRs of any size (100+ files)
- **Handles long-lived branches**: Merge-base calculation works regardless of branch divergence
- **No quality loss**: Full file contents always available via git

### Data Structures at Each Stage

```mermaid
flowchart LR
    subgraph API Response
        PR[PR Metadata<br/>---<br/>fromRef: commit<br/>toRef: commit<br/>title: str<br/>description: str]

        Changes[Changes List<br/>---<br/>path: PathObject<br/>type: str<br/>srcPath: PathObject]
    end

    subgraph File Content
        BaseFile[Base File Content<br/>---<br/>Raw text/bytes<br/>from base commit]

        HeadFile[Head File Content<br/>---<br/>Raw text/bytes<br/>from head commit]
    end

    subgraph Generated Data
        Patch[Unified Diff Patch<br/>---<br/>@@ hunks<br/>- removed lines<br/>+ added lines<br/>context lines]

        FilePatch[FilePatchInfo<br/>---<br/>base_file: str<br/>head_file: str<br/>patch: str<br/>filename: str<br/>edit_type: EDIT_TYPE<br/>tokens: int<br/>num_plus_lines: int<br/>num_minus_lines: int]
    end

    PR --> Changes
    Changes --> BaseFile
    Changes --> HeadFile
    BaseFile --> Patch
    HeadFile --> Patch
    Patch --> FilePatch

    style FilePatch fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px
```

---

## PR Diff Processing Pipeline

Shows how FilePatchInfo objects are transformed into AI-ready prompts.

```mermaid
flowchart TD
    Input[list of FilePatchInfo<br/>from git_provider] --> Filter[Filter & Validate]

    Filter --> Sort[Sort by Priority<br/>pr_processing.sort_files_by_main_languages]

    Sort --> MainLang{Detect Main<br/>Language}
    MainLang -->|Python| PriorityPy[Priority:<br/>1. Python files<br/>2. Config files<br/>3. Tests<br/>4. Other]
    MainLang -->|JavaScript| PriorityJS[Priority:<br/>1. JS/TS files<br/>2. Package files<br/>3. Tests<br/>4. Other]
    MainLang -->|Other| PriorityOther[Default Priority]

    PriorityPy --> Extend[Extend Patches<br/>git_patch_processing.extend_patch]
    PriorityJS --> Extend
    PriorityOther --> Extend

    Extend --> ExtendDetail[Add Context Lines<br/>- Search for enclosing function<br/>- Add lines before hunk<br/>- Add line numbers]

    ExtendDetail --> CountTokens[Count Tokens<br/>token_handler.count_tokens]

    CountTokens --> CheckLimit{Total Tokens<br/>< max_model_tokens?}

    CheckLimit -->|Yes| FullDiff[pr_generate_extended_diff<br/>Include all files with full context]

    CheckLimit -->|No| Compress[pr_generate_compressed_diff<br/>Apply compression strategy]

    Compress --> CompressSteps[Compression Steps:<br/>1. Keep high priority files<br/>2. Clip context from medium priority<br/>3. Skip low priority files<br/>4. Add file lists for skipped]

    FullDiff --> Format[Format as String<br/>With line numbers and headers]
    CompressSteps --> Format

    Format --> Output[Formatted Diff String<br/>Ready for AI]

    Output --> AIPrompt[Insert into Prompt Template<br/>variables.diff = formatted_diff]

    style Input fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    style Output fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px
    style Compress fill:#fff3e0,stroke:#f57c00,stroke-width:2px
```

### Token Budget Allocation

```mermaid
flowchart LR
    subgraph Token Budget
        Total[max_model_tokens<br/>Default: 32000]

        Total --> System[System Prompt<br/>~500 tokens]
        Total --> User[User Prompt<br/>~300 tokens]
        Total --> Diff[Diff Content<br/>~30000 tokens]
        Total --> Response[AI Response<br/>~1200 tokens]
    end

    subgraph If Over Budget
        Diff --> Compress[Compression<br/>Strategies]

        Compress --> Clip[Clip Context<br/>Remove surrounding lines]
        Compress --> Skip[Skip Files<br/>Low priority files]
        Compress --> Summary[Add Summaries<br/>List of skipped files]
    end

    style Total fill:#fff9c4,stroke:#f57f17,stroke-width:3px
    style Compress fill:#ffccbc,stroke:#d84315,stroke-width:2px
```

---

## Compression Strategy

Detailed breakdown of the compression algorithm when PRs exceed token limits.

```mermaid
flowchart TD
    Start[get_pr_diff called<br/>with list of FilePatchInfo] --> Sort[Sort files by:<br/>1. Main language<br/>2. Number of changes<br/>3. File type priority]

    Sort --> InitBudget[Initialize Token Budget<br/>available = max_model_tokens - prompts]

    InitBudget --> Process[Process files in priority order]

    Process --> Loop{For each file}

    Loop --> Extend[Extend patch with context<br/>extend_patch]

    Extend --> CountFile[Count tokens for this file]

    CountFile --> CheckFit{Total tokens<br/>+ this file<br/>< budget?}

    CheckFit -->|Yes| IncludeFull[Include with full context<br/>Add to patches_extended]

    CheckFit -->|No| TryClip{Can clip<br/>context?}

    TryClip -->|Yes| Clip[Remove context lines<br/>Keep only hunks<br/>Add to patches_compressed]

    TryClip -->|No| Skip[Skip file entirely<br/>Add to skipped_files list]

    IncludeFull --> UpdateCount[Update token count]
    Clip --> UpdateCount
    Skip --> UpdateCount

    UpdateCount --> MoreFiles{More files?}
    MoreFiles -->|Yes| Loop
    MoreFiles -->|No| Generate[Generate final diff string]

    Generate --> AddExtended[Add extended files section<br/>Full diffs with context]
    AddExtended --> AddCompressed[Add compressed files section<br/>Minimal context]
    AddCompressed --> AddSkipped[Add skipped files list<br/>File names only]

    AddSkipped --> Return[Return formatted diff string]

    style IncludeFull fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style Clip fill:#fff9c4,stroke:#f57f17,stroke-width:2px
    style Skip fill:#ffcdd2,stroke:#c62828,stroke-width:2px
    style Return fill:#e1f5fe,stroke:#0277bd,stroke-width:3px
```

### File Priority Calculation

```mermaid
flowchart LR
    File[File] --> LangCheck{Matches main<br/>language?}

    LangCheck -->|Yes| HighPrio[Base Priority:<br/>High]
    LangCheck -->|No| LowPrio[Base Priority:<br/>Low]

    HighPrio --> Changes[Adjust by:<br/>num_plus_lines +<br/>num_minus_lines]
    LowPrio --> Changes

    Changes --> Type{File Type}

    Type -->|Config| ConfigAdj[Priority +10]
    Type -->|Test| TestAdj[Priority -5]
    Type -->|Source| NoAdj[No adjustment]
    Type -->|Generated| GenAdj[Priority -20]

    ConfigAdj --> Final[Final Priority Score]
    TestAdj --> Final
    NoAdj --> Final
    GenAdj --> Final

    style HighPrio fill:#c8e6c9,stroke:#2e7d32
    style LowPrio fill:#ffcdd2,stroke:#c62828
    style Final fill:#e1f5fe,stroke:#0277bd,stroke-width:3px
```

---

## AI Request/Response Flow

Shows the complete flow from diff to published review.

```mermaid
flowchart TD
    Start[Formatted Diff String] --> LoadPrompts[Load Prompt Templates<br/>from *_prompts.toml]

    LoadPrompts --> PrepVars[Prepare Variables<br/>- diff<br/>- pr_title<br/>- pr_description<br/>- language<br/>- extra_instructions]

    PrepVars --> RenderSys[Render System Prompt<br/>Jinja2 template + variables]
    RenderSys --> RenderUser[Render User Prompt<br/>Jinja2 template + variables]

    RenderUser --> BuildMsg[Build Messages Array<br/>system: rendered_system<br/>user: rendered_user]

    BuildMsg --> SelectModel{Model Type}

    SelectModel -->|LiteLLM| LiteLLM[LiteLLMAIHandler]
    SelectModel -->|OpenAI| OpenAI[OpenAIAIHandler]
    SelectModel -->|Anthropic| Anthropic[AnthropicAIHandler]

    LiteLLM --> CallAPI[chat_completion<br/>model, messages, temperature]
    OpenAI --> CallAPI
    Anthropic --> CallAPI

    CallAPI --> Response[AI Response<br/>YAML formatted text]

    Response --> Parse[Parse YAML<br/>load_yaml]

    Parse --> Extract[Extract Sections:<br/>- review<br/>- key_issues_to_review<br/>- security_concerns<br/>- score]

    Extract --> Format[Format as Markdown<br/>- Headers<br/>- Tables<br/>- Code blocks<br/>- Labels]

    Format --> Publish[publish_comment<br/>via git_provider]

    Publish --> PublishAPI[Git Provider API Call<br/>e.g. add_pull_request_comment]

    PublishAPI --> End([✓ Review Posted])

    style Response fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px
    style Parse fill:#fff9c4,stroke:#f57f17,stroke-width:2px
    style End fill:#c8e6c9,stroke:#2e7d32,stroke-width:3px
```

### AI Response Structure

```mermaid
flowchart TD
    AIResponse[AI Response<br/>YAML String] --> ParseRoot[Parse Root Keys]

    ParseRoot --> Review[review:<br/>- estimated_effort_to_review: 1-5<br/>- relevant_tests: Added/No<br/>- possible_issues: text<br/>- security_concerns: Yes/No]

    ParseRoot --> Issues[key_issues_to_review:<br/>- headline: str<br/>- description: str<br/>- start_line: int<br/>- end_line: int]

    ParseRoot --> Security[security_concerns:<br/>- headline: str<br/>- description: str<br/>- severity: str<br/>- files: list]

    Review --> FormatReview[Format Review Table<br/>Markdown table with scores]
    Issues --> FormatIssues[Format Issues List<br/>Numbered list with code refs]
    Security --> FormatSecurity[Format Security Warnings<br/>Collapsible sections]

    FormatReview --> Combine[Combine into<br/>Final Comment]
    FormatIssues --> Combine
    FormatSecurity --> Combine

    Combine --> Labels[Add Labels:<br/>- Review effort<br/>- Security flag<br/>- Language tags]

    Labels --> Final[Final Markdown Comment]

    style AIResponse fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px
    style Final fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px
```

---

## Configuration Loading Flow

Shows how configuration is loaded and merged from multiple sources.

```mermaid
flowchart TD
    Start([Application Start]) --> LoadDefaults[Load Default Config<br/>pr_agent/settings/configuration.toml]

    LoadDefaults --> LoadGlobal{Global config<br/>exists?}

    LoadGlobal -->|Yes| MergeGlobal[Merge Global Settings<br/>~/.pr_agent/configuration.toml]
    LoadGlobal -->|No| CheckRepo

    MergeGlobal --> CheckRepo{Repo config<br/>exists?}

    CheckRepo -->|Yes| MergeRepo[Merge Repo Settings<br/>.pr_agent.toml in repo root]
    CheckRepo -->|No| CheckCLI

    MergeRepo --> CheckCLI{CLI args<br/>provided?}

    CheckCLI -->|Yes| MergeCLI[Apply CLI Overrides<br/>--config.key=value]
    CheckCLI -->|No| LoadSecrets

    MergeCLI --> LoadSecrets[Load Secrets<br/>.secrets.toml]

    LoadSecrets --> Final[Final Merged Configuration]

    Final --> Use[Used by:<br/>- Git providers<br/>- Tools<br/>- AI handlers]

    style LoadDefaults fill:#e3f2fd,stroke:#1976d2
    style MergeGlobal fill:#f3e5f5,stroke:#7b1fa2
    style MergeRepo fill:#fff3e0,stroke:#f57c00
    style MergeCLI fill:#ffebee,stroke:#c62828
    style Final fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px
```

---

## See Also

- [architecture-overview.md](architecture-overview.md) - System architecture
- [sequence-diagrams.md](sequence-diagrams.md) - Execution sequences
- [class-diagrams.md](class-diagrams.md) - Class structure
