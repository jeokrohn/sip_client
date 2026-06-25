"""Exception hierarchy for Webex Calling scenario execution."""


class WxCallsError(Exception):
    """Base error raised by the Webex Calling test framework."""


class ConfigError(WxCallsError):
    """Raised when framework configuration is invalid."""


class ScenarioError(WxCallsError):
    """Raised when a scenario document or step is invalid."""


class BackendError(WxCallsError):
    """Raised when a SIP backend cannot perform an operation."""


class TimeoutError(WxCallsError):
    """Raised when an expected scenario event does not occur in time."""


class UnsupportedFeature(WxCallsError):
    """Raised when a requested backend capability is unavailable."""


class MediaError(WxCallsError):
    """Raised when media generation, playback, recording, or analysis fails."""
