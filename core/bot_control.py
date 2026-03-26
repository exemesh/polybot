"""
Bot Control — reads/writes the control file (data/bot_control.json).
Provides the bridge between the dashboard/workflow_dispatch and the bot runtime.

The control file determines:
  - mode: "dry_run" or "live" (overrides DRY_RUN env var)
  - trading_enabled: true/false (emergency halt / kill switch)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger("polybot.control")

CONTROL_PATH = "data/bot_control.json"


@dataclass
class ControlState:
    mode: str = "dry_run"             # "dry_run" or "live"
    trading_enabled: bool = True      # False = emergency halt
    updated_by: str = "system"        # "system", "workflow_dispatch", "bot"
    updated_at: str = ""
    last_bot_run: Optional[str] = None
    halt_reason: Optional[str] = None

    @property
    def is_dry_run(self) -> bool:
        return self.mode != "live"

    @property
    def is_halted(self) -> bool:
        return not self.trading_enabled


def _strip_conflict_markers(text: str) -> str:
    """Remove git merge-conflict markers and keep the 'ours' (HEAD) section."""
    if "<<<<<<< " not in text:
        return text
    logger.warning("Conflict markers detected in control file — keeping HEAD section")
    lines = []
    section = "ours"          # keep lines from HEAD side by default
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            section = "ours"
        elif line.startswith("======="):
            section = "theirs"
        elif line.startswith(">>>>>>> "):
            section = "ours"   # reset for any subsequent blocks
        elif section == "ours":
            lines.append(line)
    return "\n".join(lines)


def load_control(path: str = CONTROL_PATH) -> ControlState:
    """Load control state from JSON file. Returns defaults if file missing or corrupt."""
    try:
        raw = Path(path).read_text()
        raw = _strip_conflict_markers(raw)
        data = json.loads(raw)
        state = ControlState(
            mode=data.get("mode", "dry_run"),
            trading_enabled=data.get("trading_enabled", True),
            updated_by=data.get("updated_by", "system"),
            updated_at=data.get("updated_at", ""),
            last_bot_run=data.get("last_bot_run"),
            halt_reason=data.get("halt_reason"),
        )
        logger.info(
            f"Control loaded: mode={state.mode}, "
            f"trading_enabled={state.trading_enabled}, "
            f"updated_by={state.updated_by}"
        )
        return state
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Control file not found or invalid ({e}), using defaults")
        return ControlState(
            updated_at=datetime.now(timezone.utc).isoformat()
        )


def save_control(state: ControlState, path: str = CONTROL_PATH):
    """Write control state back to JSON file."""
    data = {
        "mode": state.mode,
        "trading_enabled": state.trading_enabled,
        "updated_by": state.updated_by,
        "updated_at": state.updated_at,
        "last_bot_run": state.last_bot_run,
        "halt_reason": state.halt_reason,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    logger.info(f"Control saved: mode={state.mode}, trading_enabled={state.trading_enabled}")
