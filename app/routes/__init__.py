from .dashboard import bp as dashboard_bp
from .analysis import bp as analysis_bp
from .history import bp as history_bp
from .sync import bp as sync_bp
from .discovery import bp as discovery_bp

__all__ = ["dashboard_bp", "analysis_bp", "history_bp", "sync_bp", "discovery_bp"]
