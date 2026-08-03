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
    """Report that scanning produced no usable pages (empty feeder or all blank)."""

    exit_code = 2


class DeviceError(ScanMoleError):
    """Report a scanner acquisition failure or an unsupported device option."""

    exit_code = 3


class MissingDependencyError(ScanMoleError):
    """Report that a required external tool is not installed."""

    exit_code = 4


class ProcessingError(ScanMoleError):
    """Report an img2pdf or ocrmypdf failure after pages were acquired."""

    exit_code = 5
