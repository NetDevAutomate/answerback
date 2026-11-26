"""Process exit codes of the bridge CLI (docs/NATIVE-CLI.md "Exit codes").

Shared by cli.main() and the in-tree consumers that run the CLI as a subprocess
(the scripted peers of the collaboration harness), so no caller carries a magic
number. 0 is a completed task or a successful command; 130 is Ctrl-C.
"""

EXIT_FAILURE = 1
EXIT_BLOCKED = 2
EXIT_WAIT_TIMED_OUT = 3
