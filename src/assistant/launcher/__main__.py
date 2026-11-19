"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Module entry point: ``python -m assistant.launcher up|down|status [name]``.
"""

from assistant.launcher.cli import main

raise SystemExit(main())
