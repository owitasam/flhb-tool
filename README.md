# flhb-tool — Fedora Lutris Loader Bridge

A Python-based orchestration and wrapper utility that integrates with
Lutris on Fedora Linux. Manages environment variables, validates system
dependencies, intercepts execution calls, and safely orchestrates
user-space loaders/bypasses alongside Wine/Proton prefixes.

## Phase 1 — Environment & Dependency Validation

The `flhb_tool.system_check` module provides `check_system_dependencies()`,
a pre-flight check that verifies:

1. Required system packages via `rpm`/`dnf` (wine-core, vulkan-loader,
   mesa-vulkan-drivers).
2. Active Vulkan device availability (`vulkaninfo` or `/sys/class/drm/`).
3. CPU virtualization flags (vmx/svm) in `/proc/cpuinfo`.

Missing dependencies are logged with explicit fix instructions.

### Run standalone

```bash
# From the project root
python -m flhb_tool

# With a log file and extra packages
python -m flhb_tool --log-file flhb_debug.log --package dxvk
```

### Import in your own code

```python
from flhb_tool import check_system_dependencies, print_report

report = check_system_dependencies()
print_report(report)

if not report.all_passed:
    # handle missing deps / abort launch
    raise SystemExit(1)
```

## Roadmap

- Phase 2: Core Orchestration & Wrapper Engine (`bridge.py`, `config.json`)
- Phase 3: Automated Log Monitoring & Error Remediation
- Phase 4: Lutris Integration & Packaging (`install.sh`, user guide)
