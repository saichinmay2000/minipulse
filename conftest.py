import sys
from pathlib import Path

# Add ai_service to sys.path so test_tools.py can import tools
sys.path.insert(0, str(Path(__file__).parent / "ai_service"))
