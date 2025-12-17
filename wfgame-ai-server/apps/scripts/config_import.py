# Simple config import fix
import os, sys

# Get absolute path to the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))

# Add to path and import
sys.path.insert(0, project_root)

# 导入配置管理器
try:
    from utils.config_helper import ConfigManager
    # Create a singleton instance
    config_manager = ConfigManager()
    print("Successfully imported ConfigManager")
except ImportError as e:
    print(f"Error importing ConfigManager: {e}")
    config_manager = None

# Explicitly export the ConfigManager class and config_manager instance
__all__ = ['ConfigManager', 'config_manager']
