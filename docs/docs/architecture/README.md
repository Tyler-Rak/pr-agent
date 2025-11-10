# PR-Agent Architecture Documentation

This folder contains architectural documentation and diagrams for understanding the pr-agent codebase.

## Documentation Files

### Architecture Overview
- **[architecture-overview.md](architecture-overview.md)** - High-level system architecture with component diagram
- **[sequence-diagrams.md](sequence-diagrams.md)** - Detailed sequence flows for different commands
- **[class-diagrams.md](class-diagrams.md)** - Class structure and relationships
- **[data-flow.md](data-flow.md)** - Data flow and processing pipelines

### Specific Topics
- **[bitbucket-rate-limiting.md](bitbucket-rate-limiting.md)** - Bitbucket Server rate limiting issue analysis

## Quick Links

### Key Components

- **Entry Points**: `pr_agent/cli.py`, `pr_agent/servers/`
- **Orchestration**: `pr_agent/agent/pr_agent.py`
- **Tools**: `pr_agent/tools/`
- **Git Providers**: `pr_agent/git_providers/`
- **Core Algorithms**: `pr_agent/algo/`

### Common Flows

1. **CLI Review**: CLI → PRAgent → PRReviewer → GitProvider → AI → Publish
2. **Webhook Review**: Webhook → JWT Auth → PRAgent → [same as above]
3. **File Fetching**: get_diff_files() → get_changes() → get_file() (per file) → build FilePatchInfo

## For New Contributors

Start with:
1. [architecture-overview.md](architecture-overview.md) - Understand the big picture
2. [sequence-diagrams.md](sequence-diagrams.md) - See how a review command flows
3. [class-diagrams.md](class-diagrams.md) - Dive into class structure

## Viewing Diagrams

All diagrams use [Mermaid](https://mermaid.js.org/) syntax and will render automatically in:
- GitHub/GitLab markdown viewers
- VS Code (with Mermaid extension)
- Online at [mermaid.live](https://mermaid.live)
