"""Rules loading and parsing for system prompt customization."""

from __future__ import annotations

import logging
from ssr.config import Settings

logger = logging.getLogger(__name__)

def load_rules(settings: Settings) -> str:
    """Load and concatenate global and project-level rules files.
    
    Returns:
        Concatenated rules as a string, or an empty string if no rules exist.
    """
    rules_blocks = []
    
    # 1. Global Rules
    if settings.rules_file.exists():
        try:
            content = settings.rules_file.read_text(encoding="utf-8").strip()
            if content:
                rules_blocks.append(f"### Global Rules\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read global rules from {settings.rules_file}: {e}")
            
    # 2. Project Rules
    if settings.project_rules_file.exists():
        try:
            content = settings.project_rules_file.read_text(encoding="utf-8").strip()
            if content:
                rules_blocks.append(f"### Project Rules\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read project rules from {settings.project_rules_file}: {e}")
            
    if not rules_blocks:
        return ""
        
    return "\n\n".join(rules_blocks)
