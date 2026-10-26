"""Reuse the tested supervisor with one owner API and one sequential worker."""
import os
from scripts.run_twilio_pilot import child_commands as legacy_commands,supervise


def child_commands(environment):
    commands=legacy_commands(environment)
    commands[0][3]='api.twilio_review_pilot:app'
    commands[1][2]='scripts.twilio_review_pilot_worker'
    return commands


def main():
    os.umask(0o077)
    return supervise(command_factory=child_commands)


if __name__=='__main__':raise SystemExit(main())
