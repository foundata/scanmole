"""Exception hierarchy mapping domain failures onto process exit codes.

Every ScanMole failure carries the exit code the command should return, so the
CLI's top-level handler can translate an exception into the documented status
without a lookup table.
"""


class ScanMoleError(RuntimeError):
    """Base error for ScanMole failures.

    Attributes:
        message: Human-readable description, reused for the ``error`` event and
            the process's diagnostic output.
        exit_code: Process exit status a command returns for this failure.
    """

    exit_code = 1

    def __init__(self, message: str) -> None:
        """Store the message for reporting and exception chaining."""
        super().__init__(message)
        self.message = message


class InputError(ScanMoleError):
    """Report invalid command input, such as a malformed page size."""

    exit_code = 2


class NoPagesError(ScanMoleError):
    """Report that there was nothing to scan: empty feeder, or every page blank.

    Deliberately distinct from :class:`InputError`: the invocation was fine and
    nothing malfunctioned, there just was no content. Automation can retry or
    ignore this, while a usage error means the calling script has a bug.
    """

    exit_code = 6


class DeviceError(ScanMoleError):
    """Report a scanner acquisition failure or an unsupported device option."""

    exit_code = 3


class MissingDependencyError(ScanMoleError):
    """Report that a required external tool is not installed."""

    exit_code = 4


class ProcessingError(ScanMoleError):
    """Report an img2pdf or ocrmypdf failure after pages were acquired."""

    exit_code = 5


class Terminated(Exception):
    """Raised by the CLI's SIGTERM handler so cleanup runs before exit.

    The GUI (and process supervisors) stop a run with SIGTERM. Python's
    default disposition would kill the interpreter without unwinding,
    leaving the scanimage child running and the work directory behind.
    Shared here because the scanner's shutdown drain must recognize it as
    a termination interrupt (retry the drain step) rather than a cleanup
    failure (never retried).
    """
