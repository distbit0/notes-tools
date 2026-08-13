from __future__ import annotations

import json
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_model_name() -> str:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Model configuration not found at {CONFIG_PATH}.") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Model configuration at {CONFIG_PATH} is invalid: {error}") from error

    if not isinstance(payload, dict):
        raise RuntimeError(f"Model configuration at {CONFIG_PATH} must be an object.")
    model_name = payload.get("model")
    if not isinstance(model_name, str) or not model_name.strip():
        raise RuntimeError(
            f"Model configuration at {CONFIG_PATH} must contain a non-empty model string."
        )
    return model_name.strip()
